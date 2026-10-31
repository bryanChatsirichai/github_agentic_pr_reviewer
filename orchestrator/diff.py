"""Which diff the reviewer sees — full PR or only what changed since the last review.

Step 8 status:

- ``full_diff`` — done (Task 1): the whole PR, as Phase B reviewed it.
- ``choose_diff`` — done (Task 2): the full / incremental / noop decision.
"""

import logging
from dataclasses import dataclass
from typing import Any

from github import Github, GithubException
from github.Repository import Repository

from clients import github
from models.pr_context import DiffMode
from models.review_state import ReviewState

logger = logging.getLogger("pr-reviewer.diff")

__all__ = ["COMPARE_FILE_LIMIT", "DiffChoice", "choose_diff", "full_diff"]

#: Most files GitHub lists in one compare. A compare that reaches it may be
#: cut off, so it is not trusted as an incremental diff.
COMPARE_FILE_LIMIT = 300


@dataclass(frozen=True)
class DiffChoice:
    """The diff to review, how it was chosen, and why (for the flow log)."""

    mode: DiffMode
    diff: str
    reason: str


def full_diff(changed_files: list[dict[str, Any]]) -> str:
    """Return the whole PR as one unified diff.

    Built from the PR's own per-file patches (``get_pr_files`` entries), so
    changes that landed on the base branch are never included. Takes the
    files ``build_context`` already fetched rather than calling
    ``get_pr_diff``, which would fetch them a second time.
    """
    return github.render_diff(changed_files)


def choose_diff(
    state: ReviewState,
    base_ref: str,
    head_sha: str,
    changed_files: list[dict[str, Any]],
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> DiffChoice:
    """Pick the diff for this run: incremental when it is safe, else full.

    - **incremental** — the PR only gained commits since ``state.reviewed_sha``
      (compare status ``ahead``), on the same base branch, with the same
      merge-base. The diff is that range, kept to files still in the PR.
    - **noop** — the head is the commit already reviewed (``identical``), or
      the new commits touch no file that is still in the PR.
    - **full** — anything else: first review, retargeted PR, rebase or
      force-push (``diverged`` / ``behind``), base branch merged in
      (merge-base moved), old commit gone (404 / 422), or a compare that may
      be cut off at ``COMPARE_FILE_LIMIT`` files.

    The merge-base comes from comparing ``base_ref`` with the head, never
    from ``pr.base.sha``: that is a snapshot from when the PR was opened and
    does not move with the base branch. Other API errors propagate.
    """

    def full(reason: str) -> DiffChoice:
        return DiffChoice("full", full_diff(changed_files), reason)

    if state.reviewed_sha is None:
        return full("no previous review")
    if state.base_ref != base_ref:
        return full(f"base branch changed {state.base_ref} → {base_ref}")

    since = state.reviewed_sha[:10]
    try:
        inc = github.compare_commits(
            state.reviewed_sha, head_sha, repo=repo, client=client
        )
    except GithubException as exc:
        if exc.status in (404, 422):
            return full(f"cannot compare from {since} (HTTP {exc.status}, commit gone?)")
        raise

    if inc["status"] == "identical":
        return DiffChoice("noop", "", f"head is {since}, already reviewed")
    if inc["status"] != "ahead":
        return full(f"history rewritten since {since} ({inc['status']})")

    merge_base = github.get_merge_base_sha(base_ref, head_sha, repo=repo, client=client)
    if merge_base != state.base_merge_sha:
        return full(f"merge-base moved {state.base_merge_sha} → {merge_base}")
    if len(inc["files"]) >= COMPARE_FILE_LIMIT:
        return full(f"{len(inc['files'])} files since {since}, list may be cut off")

    # Keep only files that are still part of the PR: one changed in the
    # range and then reverted to the base version is no longer the PR's.
    in_pr = {f["filename"] for f in changed_files}
    files = [f for f in inc["files"] if f["filename"] in in_pr]
    dropped = len(inc["files"]) - len(files)
    if dropped:
        logger.debug("Left out %d file(s) no longer in the PR", dropped)
    if not files:
        # Nothing for the reviewer. noop leaves reviewed_sha in place, so
        # the next run's range still starts before these commits.
        return DiffChoice("noop", "", f"no PR file changed since {since}")
    return DiffChoice(
        "incremental",
        github.render_diff(files),
        f"{len(files)} file(s) changed since {since}",
    )
