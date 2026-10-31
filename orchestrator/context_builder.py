"""PR context builder — turns raw GitHub data into one ``PRContext``.

``build_context`` is the single place that calls the GitHub client
(``clients/github.py``) and assembles its output into the object every later
agent/graph step consumes. Like the client, the GitHub ``repo`` / ``client``
can be injected for offline tests.
"""

import logging

from github import Github
from github.Repository import Repository

from clients import github
from models.context_summary import ContextSummary
from models.pr_context import PRContext
from models.review_state import ReviewState
from orchestrator.diff import choose_diff

logger = logging.getLogger("pr-reviewer.context_builder")
flow = logging.getLogger("pr-reviewer.flow")

__all__ = ["build_context"]

#: Plain-English name of each diff mode for the [route] flow line.
_ROUTE_LABELS = {
    "full": "FULL CHECK (whole PR)",
    "incremental": "NEW COMMITS CHECK (only changes since last review)",
    "noop": "NO CHECK (nothing new to review)",
}


def build_context(
    pr_number: int,
    state: ReviewState | None = None,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> PRContext:
    """Fetch PR #``pr_number`` via the GitHub client and pack it into a ``PRContext``.

    Combines PR metadata, the diff, changed files, and existing bot comments
    into one object. ``state`` is the last review's memory: it decides
    whether the diff is the whole PR or only the commits since then
    (``choose_diff``); without it the review is full. ``previous_findings``
    is left empty: it comes from memory, not GitHub, so ``run_review``
    fills it in.
    """
    metadata = github.get_pull_request(pr_number, repo=repo, client=client)
    changed_files = github.get_pr_files(pr_number, repo=repo, client=client)
    choice = choose_diff(
        state or ReviewState(),
        metadata["base_branch"],
        metadata["head_sha"],
        changed_files,
        repo=repo,
        client=client,
    )
    flow.info("[route] %s — %s", _ROUTE_LABELS[choice.mode], choice.reason)
    existing_comments = github.get_existing_review_comments(
        pr_number, repo=repo, client=client
    )

    summary = ContextSummary(
        diff_size=len(choice.diff),
        file_list=[f["filename"] for f in changed_files],
    )

    context = PRContext(
        pr_number=metadata["number"],
        title=metadata["title"],
        body=metadata["body"],
        author=metadata["author"],
        base_branch=metadata["base_branch"],
        head_branch=metadata["head_branch"],
        base_sha=metadata["base_sha"],
        head_sha=metadata["head_sha"],
        diff=choice.diff,
        diff_mode=choice.mode,
        changed_files=changed_files,
        existing_comments=existing_comments,
        summary=summary,
    )
    flow.info(
        "[context] %s @ %s → %s: %d file(s), %d char %s diff",
        context.head_branch,
        context.head_sha[:10],
        context.base_branch,
        len(changed_files),
        summary.diff_size,
        context.diff_mode,
    )
    logger.debug("%d prior bot comment(s) on PR #%s", len(existing_comments), pr_number)
    # Cap the list so huge PRs (hundreds of files) can't flood one log line.
    shown, hidden = summary.file_list[:30], len(summary.file_list) - 30
    logger.info(
        "Changed files: %s%s",
        ", ".join(shown),
        f" (+{hidden} more)" if hidden > 0 else "",
    )
    logger.debug("PRContext for PR #%s:\n%s", pr_number, _debug_preview(context))
    return context


#: Max chars of each long section (body, diff) shown in the DEBUG preview.
_PREVIEW_LIMIT = 1500


def _truncated(text: str) -> str:
    """Shorten a section for the debug preview, noting what was cut."""
    if not text:
        return "(empty)"
    if len(text) <= _PREVIEW_LIMIT:
        return text
    return f"{text[:_PREVIEW_LIMIT]}\n… [truncated {_PREVIEW_LIMIT}/{len(text)} chars]"


def _debug_preview(context: PRContext) -> str:
    """Human-readable, truncated dump of a PRContext for DEBUG logging.

    Shows structure (metadata, file list, body, diff head) — never the full
    payload, which would bury the log lines around it.
    """
    return "\n".join(
        [
            f"  title:   {context.title}",
            f"  author:  {context.author}",
            f"  branches: {context.base_branch}…{context.head_branch}",
            f"  shas:     {context.base_sha[:10]}…{context.head_sha[:10]}",
            f"  files:    {', '.join(context.summary.file_list)}",
            f"  body:     {_truncated(context.body)}",
            f"  diff ({context.diff_mode}):",
            _truncated(context.diff),
        ]
    )
