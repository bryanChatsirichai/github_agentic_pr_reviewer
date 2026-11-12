"""Entry point for the agentic PR reviewer.

Pipeline (``run_review``), with the flow-log tag each step writes, so a CI
log line leads back to its step:

====  ===========================  =====================================
step  what                         log tags
====  ===========================  =====================================
1     load memory                  ``[memory]``
2     build PR context, pick diff  ``[route]``, ``[exit]`` on noop
2b    read inline review threads   ``[threads]``
3a    re-check previous findings   ``[skip-check]`` ``[recheck]`` ``[status]``
3b    developer explanations       ``[explain]`` ``[status]``
4     logic reviewer               ``[review]``
5     drop duplicates              ``[dedupe]``
6     file new findings            ``[file]``
7     post comment + state block   ``[post]``, ``[exit]``
8     sync inline threads          ``[threads]``
====  ===========================  =====================================

Every LLM agent built or skipped logs an ``[agent]`` line, and every
status change a ``[status]`` line. ``main`` adds ``[start]``,
``[github]``, ``[config]`` and the final ``[done]`` / ``[failed]``. Run as a module from the repo root:
``python -m orchestrator.main``
"""

import argparse
import logging
import os
import re

from dotenv import load_dotenv
from github import GithubException

from agents import llm
from agents.llm import resolve_model
from agents.reviewer_agents.factory import build_reviewer
from agents.reviewer_agents.parser import FindingParseError
from clients.github import (
    get_file_content,
    get_merge_base_sha,
    get_pr_comments,
    get_review_threads,
    post_inline_comments,
    reply_to_review_comment,
    set_thread_resolved,
    verify_connection,
)
from models.finding import FindingStatus
from models.pr_context import PRContext
from models.review_result import ReviewResult, derived_rating, worst_rating
from models.review_state import ReviewState
from models.stored_finding import KNOWN_ISSUE_STATUSES, StoredFinding
from orchestrator.context_builder import build_context
from orchestrator.explanations import (
    EXPLAINABLE_STATUSES,
    apply_explanations,
    build_explanation_judge,
    collect_explanations,
    developer_replies,
)
from orchestrator.memory import (
    StateTooLargeError,
    drop_duplicates,
    file_new_findings,
    load_memory,
    resolve_bot_login,
    save_memory,
)
from orchestrator.recheck import (
    apply_outcomes,
    build_rechecker,
    recheck_findings,
    skip_unchanged,
)
from orchestrator.threads import (
    commentable_lines,
    plan_thread_actions,
    thread_replies,
    threads_by_finding,
)

load_dotenv()

# LOG_LEVEL sets the ROOT level, so DEBUG traces everything: our pr-reviewer.*
# loggers plus libraries (httpx, openai, langchain, PyGithub, …) — very
# verbose. `or` (not a .get default) so an empty value — e.g. an unset repo
# variable in CI — falls back to INFO instead of crashing on level "".
logging.basicConfig(
    level=(os.environ.get("LOG_LEVEL") or "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# PyGithub pins its own "github" logger to INFO on import, so the root level
# alone never reaches it. It masks the Authorization header in debug logs.
logging.getLogger("github").setLevel(logging.getLogger().level)
# Flow trace: one line per pipeline stage plus the route taken (skip /
# review, later full / incremental / noop). Pinned to INFO so the run's
# audit trail survives a quieter LOG_LEVEL (WARNING, ERROR).
logging.getLogger("pr-reviewer.flow").setLevel(logging.INFO)
logger = logging.getLogger("pr-reviewer")
flow = logging.getLogger("pr-reviewer.flow")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args. Returns Namespace with `.pr` (int) and `.event` (str)."""
    parser = argparse.ArgumentParser(description="Agentic PR reviewer")
    # Which PR to review — GitHub PR number, e.g. 123 in github.com/you/repo/pull/123.
    # Defaults to 0 for manual dispatch runs (no PR context).
    parser.add_argument("--pr", type=int, default=0, help="Pull request number")
    # Why we're running — the GitHub Actions event types the workflow sends:
    #   opened      → pull_request trigger: PR created (full review).
    #   synchronize → pull_request trigger: commit pushed to open PR branch
    #                 (incremental review + re-check of previous findings).
    #   reopened    → pull_request trigger: closed PR opened again (full review).
    #   push        → manual dispatch only: "Run workflow" button / gh CLI
    #                 (testing path — the push trigger itself is not enabled).
    parser.add_argument(
        "--event",
        choices=["opened", "synchronize", "reopened", "push"],
        default="push",
        help="PR event that triggered the review",
    )
    return parser.parse_args(argv)


def render_comment(context: PRContext, result: ReviewResult) -> str:
    """Render the review verdict into the body of a ``🤖 AI Review`` comment.

    Metadata (branch, commit, files) comes from the PRContext, not the
    agent — known facts must not be restated by a model that could
    misremember them. The overall rating is the agent's judgment. The
    MARKER line is prepended by ``post_review_comment``.

    With previous findings (``context.previous_findings`` in a
    ``KNOWN_ISSUE_STATUSES`` status) the body has two sections: those
    findings one line each, then this run's new ones in full — so an issue
    that was deduped away still shows, and "No issues found" is never
    claimed while one is open. Without them, only the new findings.
    """
    files = context.summary.file_list
    # The reviewer rates only this run's new findings; an earlier finding
    # that is still open must not be hidden behind an "approve".
    still_open = [f for f in context.previous_findings if f.status in EXPLAINABLE_STATUSES]
    overall = worst_rating(result.overall_rating, derived_rating(still_open))
    lines = [
        "## Review Findings",
        "",
        f"**Overall: {overall}** · "
        f"branch `{context.head_branch}` @ `{context.head_sha[:10]}` · "
        f"{len(files)} file(s) checked",
        "",
        _files_section(files),
        "",
    ]
    findings = result.findings
    previous = [
        f for f in context.previous_findings if f.status in KNOWN_ISSUE_STATUSES
    ]
    if previous:
        # Earlier reviews: old issues as one line each (the developer has
        # read the details), then this run's issues in full.
        lines += [
            f"**Previous findings ({len(previous)}):**",
            "",
            _previous_section(previous),
            "",
            f"**New findings ({len(findings)}):**",
            "",
        ]
        if not findings:
            lines += ["No new issues found."]
            return "\n".join(lines).rstrip()
    elif not findings:
        lines += ["No issues found. 🎉"]
        return "\n".join(lines).rstrip()
    else:
        lines += [f"Found {len(findings)} issue(s):", ""]
    for f in findings:
        lines += [
            f"### {f.id} — {f.title}",
            f"`{f.file}:{f.line}` · severity: **{f.severity}** · "
            f"confidence: {f.confidence:.2f}",
            "",
            f.description,
            "",
        ]
    return "\n".join(lines).rstrip()


def _previous_section(findings: list[StoredFinding], inline_limit: int = 10) -> str:
    """One line per previous finding, collapsed once the list gets long.

    A finding a developer explained away gets a second line saying who and
    why, so a reader does not need the CI log to trust the dismissal.
    """
    listed = "\n".join(_previous_line(f) for f in findings)
    if len(findings) <= inline_limit:
        return listed
    return f"<details>\n<summary>Show all {len(findings)}</summary>\n\n{listed}\n\n</details>"


def _previous_line(finding: StoredFinding) -> str:
    """Render one previous finding. Every field came from an editable comment,
    so free text is collapsed to one line before it is shown."""
    marker, label = _status_label(finding)
    line = (
        f"- {marker} **{finding.id}** — {_one_line(finding.title)} · "
        f"`{finding.file}:{finding.line}` · {label}"
    )
    if finding.rejected_by == "developer" and finding.explained_by:
        # Backticks, not @: a mention would notify them on every review.
        # Backticks are stripped from the login so it cannot end the span.
        who = ", ".join(f"`{login.replace('`', '')}`" for login in finding.explained_by)
        why = _escape_markdown(_one_line(finding.rejection_reason)) or "no reason recorded"
        line += f"\n  - explained by {who}: {why}"
    return line


def _status_label(finding: StoredFinding) -> tuple[str, str]:
    """``(marker, label)`` for a previous finding, worded for a reader who
    does not know the lifecycle names (``rejected`` reads as if the code
    was turned down)."""
    if finding.status is FindingStatus.REJECTED:
        if finding.rejected_by == "developer":
            return "💬", "dismissed (developer explained)"
        return "🚫", f"dismissed ({finding.rejected_by})"
    return _STATUS_LABELS.get(finding.status, ("•", str(finding.status)))


#: How each non-dismissed status reads in the comment.
_STATUS_LABELS: dict[FindingStatus, tuple[str, str]] = {
    FindingStatus.OPEN: ("🟡", "open"),
    FindingStatus.REOPENED: ("🔁", "reopened (came back after a fix)"),
    FindingStatus.RESOLVED: ("✅", "fixed"),
}


def _one_line(text: str) -> str:
    """Collapse all whitespace, newlines included, to single spaces."""
    return " ".join(text.split())


#: Characters that start Markdown or HTML syntax in a GitHub comment.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+!|~])")


def _escape_markdown(text: str) -> str:
    """Backslash-escape Markdown and HTML syntax so ``text`` renders literally.

    For text that came from a developer's reply via the judge: it must not
    add links, images, HTML or formatting to the bot's comment.
    """
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def _files_section(files: list[str], inline_limit: int = 10) -> str:
    """List the checked files, collapsed once the list gets long."""
    if len(files) <= inline_limit:
        return "Files checked: " + (", ".join(files) if files else "(none)")
    listed = "\n".join(f"- {name}" for name in files)
    return f"<details>\n<summary>Files checked ({len(files)})</summary>\n\n{listed}\n\n</details>"


def run_review(pr_number: int) -> ReviewResult | None:
    """Review one PR and post the result as a comment carrying the new state.

    A run takes one of three paths, decided by what changed since the last
    posted review:

    - **Nothing new** (``diff_mode == "noop"``): the head was already
      reviewed (e.g. "Re-run job"), or the new commits touch no file still
      in the PR. No agent runs, nothing is posted, returns None.
    - **New code, no previous finding in a changed file**: first review,
      or the edits are elsewhere. Only the reviewer agent runs. Findings in
      untouched files keep their status; findings in deleted files are
      resolved without an LLM.
    - **New code touching a file with a previous finding**: the re-check
      agent runs once per such finding ("is it still there?"), then the
      reviewer agent runs as above.

    On either path with new code, the explanation judge also runs once per
    open finding that the PR author or a collaborator has explained in a
    comment since the last review ("F003: false positive because …"); an
    accepted explanation dismisses the finding.

    Neither the re-check agent nor the judge runs alone: whenever there is
    new code, the reviewer looks at it for new issues, since a fix can
    introduce one. A reply alone does not start a run (only PR events do),
    so an explanation is read on the next push.

    Raises whatever the config, GitHub and LLM layers raise; ``main``
    turns those into exit 1. Nothing is posted on failure, so the state
    is not saved either.
    """
    # Config errors (missing provider/key) raise here, before any GitHub
    # write: fail fast rather than half way through a review.
    provider, model = resolve_model()
    flow.info("[config] provider=%s model=%s", provider, model)

    # 1. Memory: the state block from the bot's last review comment. Empty
    #    on the first review, which makes the diff full and IDs start at F001.
    state = load_memory(pr_number)

    # 2. Context: PR data from GitHub. The state decides whether the diff is
    #    the whole PR, only the commits since the last review, or nothing.
    context = build_context(pr_number, state)
    if context.diff_mode == "noop":
        # reviewed_sha stays put, so the next run's range still covers any
        # commits that were skipped here.
        flow.info(
            "[exit] nothing new to review — no agent called, nothing posted "
            "(reviewed_sha stays %s)",
            (state.reviewed_sha or "none")[:10],
        )
        return None

    head_files = _HeadFiles(context.head_sha)

    # 2b. Threads: read before the explanations, since a reply in a
    #     finding's thread counts as one. None when they could not be read.
    threads = _read_threads(pr_number)

    # 3. Previous findings are brought up to date before the reviewer runs,
    #    so it sees their current statuses and file names and does not
    #    repeat them.
    # 3a. Re-check: was it fixed (resolved), or did a fix come undone
    #     (reopened)?
    state, context = _recheck_previous(state, context, head_files)
    # 3b. Explanations: was it wrong in the first place (rejected)? After
    #     3a, so a finding that was just resolved is not judged.
    state, context = _apply_developer_explanations(
        pr_number, state, context, head_files, thread_replies(threads or {}, resolve_bot_login())
    )

    # 4. Review: the reviewer agent looks at the diff for new issues.
    reviewer_name = "logic-reviewer"
    reviewer_agent = build_reviewer(reviewer_name)
    flow.info(
        "[agent] calling reviewer_agent (%s) on the %s diff, %d chars",
        reviewer_name,
        context.diff_mode,
        len(context.diff),
    )
    result = reviewer_agent.review(context)
    flow.info(
        "[review] %d finding(s), rating=%s",
        len(result.findings),
        result.overall_rating,
    )

    # 5. Dedupe: the prompt asks the reviewer not to repeat known issues;
    #    this enforces it before any ID is handed out.
    new_findings = drop_duplicates(state.findings, result.findings)
    flow.info(
        "[dedupe] %d duplicate(s) dropped, %d new",
        len(result.findings) - len(new_findings),
        len(new_findings),
    )

    # 6. File: new findings get the next IDs and a snippet cut from the head
    #    file, which a later re-check compares against.
    file_texts = head_files.get([f.file for f in new_findings])
    filed, state = file_new_findings(state, new_findings, file_texts)
    flow.info("[file] %s", ", ".join(f.id for f in filed) or "nothing to file")
    # Show the stored IDs and lines, not the LLM's own, so the visible
    # comment matches the state block. Descriptions stay full-length here.
    result = result.model_copy(
        update={
            "findings": [
                f.model_copy(update={"id": stored.id, "line": stored.line})
                for f, stored in zip(new_findings, filed)
            ]
        }
    )

    # 7. Post: posting is saving. The comment carries the state block, so a
    #    failed post records nothing and the next run starts from the old
    #    state.
    #      context: the PR info, built at the start of the run
    #      result:  this run's new findings, after dedupe and the ID rewrite
    #      state:   everything to remember for the next run
    state = _record_reviewed_code(state, context)
    save_memory(pr_number, render_comment(context, result), state)
    flow.info(
        "[exit] review posted at %s — %d new finding(s), %d in state",
        context.head_sha[:10],
        len(filed),
        len(state.findings),
    )

    # 8. Threads: after the post, so they follow the state that was saved.
    #    A failure here is only a warning — the review itself is out.
    _sync_threads(pr_number, state, context, threads)
    return result


def _read_threads(pr_number: int) -> dict[str, dict] | None:
    """The bot's review threads by finding id, or None if they could not be read.

    None is not the same as "no threads": treating a failed read as empty
    would post a duplicate inline comment for every open finding.
    """
    try:
        threads = threads_by_finding(get_review_threads(pr_number), resolve_bot_login())
    except GithubException as exc:
        flow.warning("[threads] could not read review threads, skipping them — %s", exc)
        return None
    flow.info("[threads] %d finding thread(s) on the PR", len(threads))
    return threads


def _sync_threads(
    pr_number: int,
    state: ReviewState,
    context: PRContext,
    threads: dict[str, dict] | None,
) -> None:
    """Bring the inline threads in line with the saved state; never raises
    on a GitHub error. Whatever fails is planned again by the next run."""
    if threads is None:
        return
    plan = plan_thread_actions(
        state.findings, threads, commentable_lines(context.changed_files), context.head_sha
    )
    if plan.new_threads:
        try:
            post_inline_comments(
                pr_number,
                context.head_sha,
                "🤖 AI Review: findings on their lines. The summary comment lists all of them.",
                [t.as_review_comment() for t in plan.new_threads],
            )
            flow.info(
                "[threads] posted %s", ", ".join(t.finding_id for t in plan.new_threads)
            )
        except GithubException as exc:
            flow.warning("[threads] inline comments not posted — %s", exc)
    for update in plan.updates:
        try:
            reply_to_review_comment(pr_number, update.comment_id, update.reply)
        except GithubException as exc:
            flow.warning("[threads] %s reply not posted, retried next run — %s", update.finding_id, exc)
            continue
        # Best effort, not retried: the reply already records the status,
        # and GITHUB_TOKEN without contents: write may not (un)resolve.
        action = "resolved" if update.resolve else "unresolved"
        try:
            set_thread_resolved(update.thread_id, update.resolve)
            flow.info("[threads] %s replied and %s", update.finding_id, action)
        except GithubException as exc:
            flow.warning(
                "[threads] %s replied, but the thread could not be %s "
                "(do it by hand) — %s",
                update.finding_id,
                action,
                exc,
            )
    if not plan.new_threads and not plan.updates:
        flow.info("[threads] threads already match the state — nothing to do")


class _HeadFiles:
    """Head file texts, fetched from GitHub at most once per run.

    The re-check and the snippets of new findings often need the same
    file. Values are as ``get_file_content`` returns them: None when the
    file is missing at head, a directory, or too large.
    """

    def __init__(self, head_sha: str) -> None:
        self._head_sha = head_sha
        self._texts: dict[str, str | None] = {}

    def get(self, paths: list[str]) -> dict[str, str | None]:
        """Return ``{path: text}`` for ``paths``, fetching only unseen ones."""
        for path in paths:
            if path not in self._texts:
                self._texts[path] = get_file_content(path, ref=self._head_sha)
        return {path: self._texts[path] for path in paths}


def _recheck_previous(
    state: ReviewState, context: PRContext, head_files: _HeadFiles
) -> tuple[ReviewState, PRContext]:
    """Bring previous findings up to date with the head code.

    Returns the state and context with the updated findings. Three stages,
    cheapest first (see ``orchestrator/recheck.py``):

    1. Skip check, no LLM: a finding whose file is unchanged keeps its
       status; one whose file was deleted is resolved; a renamed file's
       findings follow the new name.
    2. Re-check agent, one LLM call per finding whose file changed: is the
       defect still there?
    3. Apply: fixed → ``resolved``, back after a fix → ``reopened``; a
       still-present finding whose code moved gets its new line.
    """
    skipped = skip_unchanged(state.findings, state.file_shas, context.changed_files)
    state, context = _with_findings(state, context, skipped.findings)
    flow.info(
        "[skip-check] %d finding(s) to re-check: %s",
        len(skipped.to_recheck),
        ", ".join(f.id for f in skipped.to_recheck) or "none",
    )

    if not skipped.to_recheck:
        flow.info("[agent] recheck_agent not built — no previous finding in a changed file")
        return state, context

    texts = head_files.get([f.file for f in skipped.to_recheck])
    recheck_agent = build_rechecker()
    flow.info(
        "[agent] calling recheck_agent for %d finding(s): %s",
        len(skipped.to_recheck),
        ", ".join(f.id for f in skipped.to_recheck),
    )
    # One LLM call per finding, not per file: a changed file holding three
    # findings costs three calls (each retried once on unparseable output).
    # The file text is fetched once and shared.
    outcomes = recheck_findings(skipped.to_recheck, texts, context, recheck_agent)
    flow.info(
        "[agent] recheck_agent done — %d answered, %d unanswered (status kept)",
        len(outcomes),
        len(skipped.to_recheck) - len(outcomes),
    )

    findings = apply_outcomes(skipped.findings, outcomes, texts)
    return _with_findings(state, context, findings)


def _apply_developer_explanations(
    pr_number: int,
    state: ReviewState,
    context: PRContext,
    head_files: _HeadFiles,
    in_thread_replies: list[dict],
) -> tuple[ReviewState, PRContext]:
    """Dismiss findings a developer explained, if the judge agrees.

    An explanation is a PR comment naming the finding (``F003: …``) or a
    reply in the finding's inline thread (``in_thread_replies``, from
    ``thread_replies``). Both go through the same cutoff and trust rule.
    Returns the state and context with the updated findings. Comments are
    only fetched when some finding could be dismissed (``open`` or
    ``reopened``). See ``orchestrator/explanations.py`` for whose replies
    count and why.
    """
    if not any(f.status in EXPLAINABLE_STATUSES for f in state.findings):
        flow.info("[explain] no open finding to explain — comments not read")
        return state, context

    # Oldest first, as developer_replies expects, with both sources mixed.
    comments = sorted(
        get_pr_comments(pr_number) + in_thread_replies, key=lambda c: c["created_at"]
    )
    replies = developer_replies(
        comments,
        pr_author=context.author,
        bot_login=resolve_bot_login(),
    )
    explanations = collect_explanations(replies, state.findings)
    if not explanations:
        flow.info(
            "[explain] explanation judge not built — no trusted reply since the "
            "last review names an open finding"
        )
        return state, context

    texts = head_files.get([f.file for f in state.findings if f.id in explanations])
    judge = build_explanation_judge()
    flow.info(
        "[agent] calling explanation judge for %d finding(s): %s",
        len(explanations),
        ", ".join(explanations),
    )
    findings = apply_explanations(state.findings, explanations, texts, judge)
    return _with_findings(state, context, findings)


def _with_findings(
    state: ReviewState, context: PRContext, findings: list[StoredFinding]
) -> tuple[ReviewState, PRContext]:
    """Return ``state`` and ``context`` both holding ``findings``.

    They must change together: the state is what gets saved, while
    ``context.previous_findings`` is what the reviewer and the comment
    show. Updating only one would post statuses that differ from the
    saved ones.
    """
    return (
        state.model_copy(update={"findings": findings}),
        context.model_copy(update={"previous_findings": findings}),
    )


def _record_reviewed_code(state: ReviewState, context: PRContext) -> ReviewState:
    """Return ``state`` stamped with the code this review covered.

    The next run reads these back: Step 8 diffs from ``reviewed_sha`` and
    checks ``base_ref`` / ``base_merge_sha`` to see if an incremental diff
    is safe; the skip check compares ``file_shas`` to find changed files.

    ``file_shas`` leaves out a file without a blob SHA (if GitHub ever
    sends none) rather than failing the run, and deleted files: GitHub
    reports those with their old blob SHA, which would make them look
    present and unchanged.
    """
    return state.model_copy(
        update={
            "reviewed_sha": context.head_sha,
            "base_ref": context.base_branch,
            "base_merge_sha": get_merge_base_sha(
                context.base_branch, context.head_sha
            ),
            "file_shas": {
                f["filename"]: f["sha"]
                for f in context.changed_files
                if f.get("sha") and f.get("status") != "removed"
            },
        }
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    flow.info("[start] PR #%s event=%s", args.pr, args.event)

    try:
        repo_full_name = verify_connection()
        flow.info("[github] connected to %s", repo_full_name)
        if args.pr == 0:
            # Manual workflow-dispatch test path: proves the runner boots,
            # without needing a real PR to review.
            flow.info("[route] manual dispatch (--pr 0) — LLM smoke test only")
            chat_model = llm.get_llm()
            response = chat_model.invoke("Testing what model is being used")
            logger.info("LLM replied: %.200s", response.content)
            logger.debug("Full LLM response: %r", response)
            flow.info(
                "[done] exit 0 — manual dispatch OK, no review, %d LLM call(s)",
                llm.llm_call_count(),
            )
            return 0
        result = run_review(args.pr)
    except (
        RuntimeError,
        GithubException,
        FindingParseError,
        StateTooLargeError,
    ) as exc:
        # RuntimeError = env/config (token, provider, repo); GithubException
        # = GitHub API failure (forbidden, missing PR/repo); FindingParseError
        # = the reviewer produced unparseable output twice; StateTooLargeError
        # = too many open findings to fit the state block. All fail the run
        # loudly — an empty or missing comment must never look like a clean
        # review.
        # Traceback only at DEBUG: the message names the cause; the stack
        # is for digging into it.
        flow.error(
            "[failed] exit 1, nothing posted, %d LLM call(s) — %s: %s",
            llm.llm_call_count(),
            type(exc).__name__,
            exc,
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
        return 1
    flow.info(
        "[done] exit 0 — %s, %d LLM call(s)",
        "skipped, nothing new" if result is None else "review posted",
        llm.llm_call_count(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
