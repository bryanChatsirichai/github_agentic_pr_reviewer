"""GitHub client module — all GitHub reads/writes behind one module.

Every public function accepts an optional injected ``repo``
(:class:`github.Repository.Repository`) or ``client`` (:class:`github.Github`);
when neither is given, a client and the repository are built lazily from the
``GITHUB_TOKEN`` / ``GITHUB_REPOSITORY`` environment variables (GitHub CI sets
both; locally GITHUB_TOKEN comes from ``.env``). This keeps the module
testable offline: tests inject a mock client/repo and never touch the network.
No globals are mutated at import time.
"""

import logging
import os
from typing import Any

from dotenv import load_dotenv
from github import Auth, Github, GithubException
from github.Repository import Repository

load_dotenv()  # idempotent — no-op when main.py has already loaded .env

logger = logging.getLogger("pr-reviewer.github")

#: Marks a comment as ours. Every comment the bot posts starts with this line,
#: so later runs can find previous reviews via ``get_existing_review_comments``.
MARKER = "🤖 AI Review"

#: The module's public API — anything not listed (e.g. the _helpers) is
#: internal plumbing.
__all__ = [
    "MARKER",
    "verify_connection",
    "get_pull_request",
    "get_pr_files",
    "get_pr_diff",
    "get_file_content",
    "get_merge_base_sha",
    "get_existing_review_comments",
    "post_review_comment",
]


def _get_client() -> Github:
    """Build an authenticated Github client from GITHUB_TOKEN."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN is not set — add it to .env or the CI environment"
        )
    return Github(auth=Auth.Token(token))


def _get_repo(client: Github | None = None) -> Repository:
    """Resolve the repository under review from GITHUB_REPOSITORY (owner/repo)."""
    full_name = os.environ.get("GITHUB_REPOSITORY")
    if not full_name:
        raise RuntimeError(
            "GITHUB_REPOSITORY is not set — expected 'owner/repo' "
            "(GitHub CI sets it; locally set it in the environment)"
        )
    if "/" not in full_name:
        raise RuntimeError(
            f"GITHUB_REPOSITORY must be 'owner/repo', got {full_name!r}"
        )
    return (client or _get_client()).get_repo(full_name)


def verify_connection() -> str:
    """Sanity check: token works and the repo is reachable.

    Returns the repo full name (e.g. "owner/repo"). Verified by fetching the
    repo itself rather than GET /user, because the CI ``secrets.GITHUB_TOKEN``
    is an installation token (github-actions[bot]) with no user identity —
    GET /user returns 403 "Resource not accessible by integration" there.
    Raises on bad/missing token or unreachable repo.
    """
    repo = _get_repo()
    logger.info("GitHub connection OK — repo %s", repo.full_name)
    return repo.full_name


def get_pull_request(
    pr_number: int,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> dict[str, Any]:
    """Return metadata for PR ``pr_number`` as a plain dict.

    Keys: number, title, body, base_branch, head_branch, base_sha, head_sha,
    author (the PR author's GitHub login).
    """
    pr = (repo or _get_repo(client)).get_pull(pr_number)
    return {
        "number": pr.number,
        "title": pr.title,
        "body": pr.body or "",
        "base_branch": pr.base.ref,
        "head_branch": pr.head.ref,
        "base_sha": pr.base.sha,
        "head_sha": pr.head.sha,
        "author": pr.user.login,
    }


def get_pr_files(
    pr_number: int,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> list[dict[str, Any]]:
    """Return the files changed in PR ``pr_number``.

    Each entry has: filename, ``sha`` (the file's blob SHA at the head —
    unchanged content keeps the same SHA, which Step 9 uses to skip
    re-checks), status (added / removed / modified / renamed / ...),
    additions, deletions, previous_filename (None unless renamed), and
    ``patch`` — the hunk-level diff blob with context, or None for binary or
    oversized files.
    """
    pr = (repo or _get_repo(client)).get_pull(pr_number)
    files: list[dict[str, Any]] = []
    for f in pr.get_files():
        files.append(
            {
                "filename": f.filename,
                "sha": f.sha,
                "status": f.status,
                "additions": f.additions,
                "deletions": f.deletions,
                "previous_filename": f.previous_filename,
                "patch": f.patch,
            }
        )
    logger.debug("Loaded %d changed file(s) for PR #%s", len(files), pr_number)
    return files


def get_pr_diff(
    pr_number: int,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> str:
    """Return the PR's unified diff as a single string.

    Assembled from the per-file patch blobs (see ``get_pr_files``) with
    git-style ``diff --git`` / ``---`` / ``+++`` headers, so the result is a
    valid unified diff and needs no extra API call.
    """
    files = get_pr_files(pr_number, repo=repo, client=client)
    return "".join(_diff_section(f) for f in files)


def get_merge_base_sha(
    base_ref: str,
    head_sha: str,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> str:
    """Return the merge-base of branch ``base_ref`` and commit ``head_sha``.

    That is the commit the PR branch split off from (or last merged in)
    the target branch. Unlike ``pr.base.sha`` — a snapshot from when the PR
    was opened or retargeted — it moves when the base is merged into the
    PR, which is how Step 8 detects that an incremental diff is unsafe.
    """
    comparison = (repo or _get_repo(client)).compare(base_ref, head_sha)
    return comparison.merge_base_commit.sha


def get_file_content(
    path: str,
    ref: str,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> str | None:
    """Return the text of file ``path`` at commit ``ref``, or None.

    None means there is no text to read: the path does not exist at ``ref``
    (404 — deleted, or a path the LLM made up), it is a directory, or the
    file is over the contents API's 1 MB limit (returned with encoding
    ``"none"`` and no content). Other API errors propagate. Bytes that are
    not UTF-8 are replaced rather than raising, so a binary file yields
    junk text instead of failing the run.
    """
    try:
        content = (repo or _get_repo(client)).get_contents(path, ref=ref)
    except GithubException as exc:
        if exc.status == 404:
            logger.debug("%s not found at %s", path, ref)
            return None
        raise
    if isinstance(content, list) or content.encoding != "base64":
        logger.debug("%s at %s is a directory or too large to fetch", path, ref)
        return None
    return content.decoded_content.decode("utf-8", errors="replace")


def get_existing_review_comments(
    pr_number: int,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> list[dict[str, Any]]:
    """Return prior PR-conversation comments authored by our bot.

    A comment counts as ours if its body contains the ``MARKER`` line, oldest
    first. Each entry is ``{"id": int, "body": str, "created_at": ISO-8601
    str, "author": login or None}``. Anyone can type the marker, so callers
    that trust the content (``load_memory``) must also check ``author``.
    """
    pr = (repo or _get_repo(client)).get_pull(pr_number)
    comments: list[dict[str, Any]] = []
    for comment in pr.get_issue_comments():
        if MARKER in (comment.body or ""):
            comments.append(
                {
                    "id": comment.id,
                    "body": comment.body,
                    "created_at": comment.created_at.isoformat(),
                    # None when the author's account was deleted.
                    "author": comment.user.login if comment.user else None,
                }
            )
    logger.debug("Found %d prior bot comment(s) on PR #%s", len(comments), pr_number)
    return comments


def post_review_comment(
    pr_number: int,
    body: str,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> int:
    """Post a single summary comment on PR ``pr_number`` and return its id.

    The body is made to start with the ``MARKER`` line (prepended if missing)
    so future runs can find this comment via ``get_existing_review_comments``.
    """
    if not body.lstrip().startswith(MARKER):
        body = f"{MARKER}\n\n{body}"
    pr = (repo or _get_repo(client)).get_pull(pr_number)
    comment = pr.create_issue_comment(body)
    logger.info("Posted review comment #%s on PR #%s", comment.id, pr_number)
    return comment.id


def _diff_section(f: dict[str, Any]) -> str:
    """Render one changed file (a get_pr_files entry) as a unified-diff section."""
    name = f["filename"]
    old = f["previous_filename"] or name
    lines = [f"diff --git a/{old} b/{name}"]
    if f["status"] == "renamed":
        lines += [f"rename from {old}", f"rename to {name}"]
    if f["patch"]:
        src = "/dev/null" if f["status"] == "added" else f"a/{old}"
        dst = "/dev/null" if f["status"] == "removed" else f"b/{name}"
        # Strip only newlines: a trailing " " is a blank context line, and
        # removing it corrupts the hunk's line count.
        lines += [f"--- {src}", f"+++ {dst}", f["patch"].rstrip("\n")]
    elif f["status"] != "renamed":
        lines.append(f"# no text patch available for {name} (binary or too large)")
    return "\n".join(lines) + "\n"