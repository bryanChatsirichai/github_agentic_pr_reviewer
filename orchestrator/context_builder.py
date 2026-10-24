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
from models.pr_context import ContextSummary, PRContext

logger = logging.getLogger("pr-reviewer.context_builder")

__all__ = ["build_context"]


def build_context(
    pr_number: int,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> PRContext:
    """Fetch PR #``pr_number`` via the GitHub client and pack it into a ``PRContext``.

    Combines PR metadata, the unified diff, changed files, and existing bot
    comments into one object. ``previous_findings`` stays empty — memory
    isn't implemented yet.
    """
    metadata = github.get_pull_request(pr_number, repo=repo, client=client)
    changed_files = github.get_pr_files(pr_number, repo=repo, client=client)
    diff = github.get_pr_diff(pr_number, repo=repo, client=client)
    existing_comments = github.get_existing_review_comments(
        pr_number, repo=repo, client=client
    )

    summary = ContextSummary(
        diff_size=len(diff),
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
        diff=diff,
        changed_files=changed_files,
        existing_comments=existing_comments,
        summary=summary,
    )
    logger.info(
        "Built PRContext for PR #%s: %d changed file(s), %d char diff, %d prior comment(s)",
        pr_number,
        len(changed_files),
        summary.diff_size,
        len(existing_comments),
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

    Shows structure (metadata, file list, body, diff head) — never the
    full payload, which can be tens of thousands of chars and would bury
    the log lines around it.
    """
    return "\n".join(
        [
            f"  title:   {context.title}",
            f"  author:  {context.author}",
            f"  branches: {context.base_branch}…{context.head_branch}",
            f"  shas:     {context.base_sha[:10]}…{context.head_sha[:10]}",
            f"  files:    {', '.join(context.summary.file_list)}",
            f"  body:     {_truncated(context.body)}",
            f"  diff:",
            _truncated(context.diff),
        ]
    )
