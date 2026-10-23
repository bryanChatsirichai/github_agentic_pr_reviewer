"""PR context builder — turns raw GitHub data into one ``PRContext``.

Step 3 of the implementation roadmap. ``build_context`` is the single place
that calls the Step 2 ``github_client`` functions and assembles their output
into the object every later agent/graph step consumes. Like
``github_client``, the GitHub client/repo can be injected for offline tests.
"""

import logging

from github import Github
from github.Repository import Repository

try:  # imported as a package (tests, python -m orchestrator.context)
    from . import github_client
except ImportError:  # run as a script
    import github_client

from models.pr_context import ContextSummary, PRContext

logger = logging.getLogger("pr-reviewer.context")

__all__ = ["build_context"]


def build_context(
    pr_number: int,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> PRContext:
    """Fetch PR #``pr_number`` via ``github_client`` and pack it into a ``PRContext``.

    Combines PR metadata, the unified diff, changed files, and existing bot
    comments into one object. ``previous_findings`` stays empty — memory
    lands in Step 6.
    """
    metadata = github_client.get_pull_request(pr_number, repo=repo, client=client)
    changed_files = github_client.get_pr_files(pr_number, repo=repo, client=client)
    diff = github_client.get_pr_diff(pr_number, repo=repo, client=client)
    existing_comments = github_client.get_existing_review_comments(
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
    return context
