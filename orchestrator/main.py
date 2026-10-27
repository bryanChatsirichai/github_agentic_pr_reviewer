"""Entry point for the agentic PR reviewer.

Pipeline: load memory → build PR context → run the logic reviewer → render
findings into a ``🤖 AI Review`` comment → post it with the hidden state
block (``save_memory``). Run as a module from the repo root:
``python -m orchestrator.main``.
"""

import argparse
import logging
import os

from dotenv import load_dotenv
from github import GithubException

from agents import llm
from agents.llm import resolve_model
from agents.reviewer_agents.factory import build_reviewer
from agents.reviewer_agents.parser import FindingParseError
from clients.github import get_file_content, get_merge_base_sha, verify_connection
from models.pr_context import PRContext
from models.review import ReviewResult
from models.review_state import KNOWN_ISSUE_STATUSES, StoredFinding
from orchestrator.context_builder import build_context
from orchestrator.memory import (
    StateTooLargeError,
    drop_duplicates,
    file_new_findings,
    load_memory,
    save_memory,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Debug verbosity for OUR loggers only (pr-reviewer.* — e.g. the truncated
# PR context preview in context_builder). Scoping to this namespace keeps
# LOG_LEVEL=DEBUG from also enabling very chatty httpx/google_genai output.
logging.getLogger("pr-reviewer").setLevel(
    os.environ.get("LOG_LEVEL", "INFO").upper()
)
logger = logging.getLogger("pr-reviewer")


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
    lines = [
        "## Review Findings",
        "",
        f"**Overall: {result.overall_rating}** · "
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
    """One line per previous finding, collapsed once the list gets long."""
    listed = "\n".join(
        # Whitespace collapsed: the title comes from an editable comment.
        f"- **{f.id}** — {' '.join(f.title.split())} · `{f.file}:{f.line}` · "
        + (f"{f.status} ({f.rejected_by})" if f.rejected_by else f"{f.status}")
        for f in findings
    )
    if len(findings) <= inline_limit:
        return listed
    return f"<details>\n<summary>Show all {len(findings)}</summary>\n\n{listed}\n\n</details>"


def _files_section(files: list[str], inline_limit: int = 10) -> str:
    """List the checked files, collapsed once the list gets long."""
    if len(files) <= inline_limit:
        return "Files checked: " + (", ".join(files) if files else "(none)")
    listed = "\n".join(f"- {name}" for name in files)
    return f"<details>\n<summary>Files checked ({len(files)})</summary>\n\n{listed}\n\n</details>"


def run_review(pr_number: int) -> ReviewResult:
    """Full review pass for one PR: context → reviewer → comment on GitHub."""
    # Config errors (missing provider/key) raise RuntimeError here, before
    # any GitHub write — fail fast rather than half way through a review.
    provider, model = resolve_model()
    logger.info("Using LLM provider=%s model=%s", provider, model)

    # State from the bot's last review comment (empty → full review).
    state = load_memory(pr_number)
    logger.info("Loaded %d previous finding(s) from memory", len(state.findings))

    # The reviewer sees what was already reported so it doesn't repeat it.
    context = build_context(pr_number).model_copy(
        update={"previous_findings": state.findings}
    )
    reviewer = build_reviewer("logic-reviewer")
    result = reviewer.review(context)
    logger.info(
        "Reviewer returned %d finding(s), rating=%s",
        len(result.findings),
        result.overall_rating,
    )

    # The prompt asks the reviewer not to repeat known issues; this enforces
    # it, before any ID is handed out.
    new_findings = drop_duplicates(state.findings, result.findings)
    if len(new_findings) < len(result.findings):
        logger.info(
            "Dropped %d duplicate finding(s), %d new",
            len(result.findings) - len(new_findings),
            len(new_findings),
        )

    # Snippets come from the head file, fetched once per file with findings
    # (dict.fromkeys keeps order and drops repeats).
    paths = dict.fromkeys(f.file for f in new_findings)
    file_texts = {p: get_file_content(p, ref=context.head_sha) for p in paths}
    filed, state = file_new_findings(state, new_findings, file_texts)
    # Show the stored IDs, not the LLM's own numbering, so the visible
    # comment matches the state block. Descriptions stay full-length here.
    result = result.model_copy(
        update={
            "findings": [
                f.model_copy(update={"id": stored.id})
                for f, stored in zip(new_findings, filed)
            ]
        }
    )

    # Record which code this review covers; Step 8 diffs from here and
    # Step 9 skips files whose blob is unchanged. A file without a blob SHA
    # (if GitHub ever sends none) is left out rather than failing the run.
    state = state.model_copy(
        update={
            "reviewed_sha": context.head_sha,
            "base_ref": context.base_branch,
            "base_merge_sha": get_merge_base_sha(
                context.base_branch, context.head_sha
            ),
            "file_shas": {
                f["filename"]: f["sha"] for f in context.changed_files if f.get("sha")
            },
        }
    )

    # Posting is saving: the comment carries the state block, so a failed
    # post records nothing — the next run then starts from the old SHAs.
    save_memory(pr_number, render_comment(context, result), state)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger.info("Starting review for PR #%s (event: %s)", args.pr, args.event)
    
    try:
        repo_full_name = verify_connection()
        logger.info("GitHub token OK for repo: %s", repo_full_name)
        if args.pr == 0:
            # Manual workflow-dispatch test path: proves the runner boots,
            # without needing a real PR to review.
            chat_model = llm.get_llm()
            response = chat_model.invoke("Testing what model is being used")
            logger.info("Response: %s", response)
            logger.info("Manual dispatch (--pr 0): no PR to review, done.")
            return 0
        run_review(args.pr)
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
        logger.error("Review failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
