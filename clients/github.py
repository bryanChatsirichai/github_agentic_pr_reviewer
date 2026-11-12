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
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from github import Auth, Github, GithubException
from github.File import File
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
    "render_diff",
    "compare_commits",
    "get_file_content",
    "file_exists",
    "get_merge_base_sha",
    "get_existing_review_comments",
    "get_pr_comments",
    "post_review_comment",
    "get_review_threads",
    "post_inline_comments",
    "reply_to_review_comment",
    "set_thread_resolved",
]

#: Review threads and their comments read per run. PyGithub has no review-
#: thread API (threads and their resolved flag exist only in GraphQL).
_REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first: 100) {
            pageInfo { hasNextPage }
            nodes {
              databaseId
              body
              createdAt
              authorAssociation
              author { __typename login }
            }
          }
        }
      }
    }
  }
}
"""


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
    logger.debug("GitHub connection OK — repo %s", repo.full_name)
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
    files = [_file_entry(f) for f in pr.get_files()]
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
    return render_diff(get_pr_files(pr_number, repo=repo, client=client))


def render_diff(files: list[dict[str, Any]]) -> str:
    """Render ``get_pr_files``-shaped entries as one unified diff. No API call."""
    return "".join(_diff_section(f) for f in files)


def compare_commits(
    base: str,
    head: str,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> dict[str, Any]:
    """Compare commit ``base`` with ``head``: ``{"status", "files"}``.

    ``status`` is ``ahead`` / ``behind`` / ``identical`` / ``diverged``;
    ``files`` has ``get_pr_files``-shaped entries for what changed between
    them — at most 300, GitHub's cap per compare. PyGithub's comparison is
    lazy, so an API error (404 when ``base`` no longer exists) is raised
    here, on the first attribute read, not by ``repo.compare``.
    """
    comparison = (repo or _get_repo(client)).compare(base, head)
    status = comparison.status
    return {"status": status, "files": [_file_entry(f) for f in comparison.files]}


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


def file_exists(
    path: str,
    ref: str,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> bool:
    """Return whether ``path`` exists at commit ``ref`` (file or directory).

    Tells apart the cases ``get_file_content`` folds into None: False only
    on a 404; a directory or a file over 1 MB exists. Other API errors
    propagate.
    """
    try:
        (repo or _get_repo(client)).get_contents(path, ref=ref)
    except GithubException as exc:
        if exc.status == 404:
            return False
        raise
    return True


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


def get_pr_comments(
    pr_number: int,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> list[dict[str, Any]]:
    """Return every PR-conversation comment, oldest first, from anyone.

    Each entry is ``{"id", "body", "created_at" (ISO-8601 str), "author"
    (login or None), "author_association"}``. The association is GitHub's
    ``OWNER`` / ``MEMBER`` / ``COLLABORATOR`` / ``CONTRIBUTOR`` / ``NONE`` /
    …, which callers use to decide whose words carry weight. Inline review
    comments on the diff are a different API and not included.
    """
    pr = (repo or _get_repo(client)).get_pull(pr_number)
    return [
        {
            "id": comment.id,
            "body": comment.body or "",
            "created_at": comment.created_at.isoformat(),
            "author": comment.user.login if comment.user else None,
            "author_association": comment.author_association,
        }
        for comment in pr.get_issue_comments()
    ]


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
    logger.debug("Posted review comment #%s on PR #%s", comment.id, pr_number)
    return comment.id


def get_review_threads(
    pr_number: int,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> list[dict[str, Any]]:
    """Return every inline review thread on PR ``pr_number``.

    Each entry is ``{"id": GraphQL node id (for ``set_thread_resolved``),
    "is_resolved": bool, "comments": [...]}``, comments oldest first and
    shaped like ``get_pr_comments`` entries, so both can feed the same
    explanation cutoff. A comment's ``id`` is its REST review-comment id
    (for ``reply_to_review_comment``) — a separate id space from issue
    comments.

    Threads are read page by page (100 per GraphQL call) until the last:
    a thread left out would look like "no thread yet" and the finding
    would get a duplicate inline comment. Comments are read up to 100 per
    thread — only replies past that are missed, logged as a warning.
    """
    repo = repo or _get_repo(client)
    threads: list[dict[str, Any]] = []
    cursor = None
    while True:
        _, data = repo.requester.graphql_query(
            _REVIEW_THREADS_QUERY,
            {"owner": repo.owner.login, "name": repo.name, "number": pr_number,
             "cursor": cursor},
        )
        page = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        for node in page["nodes"]:
            if node["comments"]["pageInfo"]["hasNextPage"]:
                logger.warning(
                    "Review thread %s has over 100 comments; only the first 100 comments are read",
                    node["id"],
                )
            threads.append(
                {
                    "id": node["id"],
                    "is_resolved": node["isResolved"],
                    "comments": [_thread_comment(c) for c in node["comments"]["nodes"]],
                }
            )
        return threads


def post_inline_comments(
    pr_number: int,
    head_sha: str,
    body: str,
    comments: list[dict[str, Any]],
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> int:
    """Post ``comments`` as one review on commit ``head_sha``; return its id.

    Each comment is ``{"path", "line", "side": "RIGHT", "body"}``. The review
    is always ``COMMENT``: approving or requesting changes is left to
    humans, and for ``github-actions[bot]`` it is blocked by a repo setting
    that is off by default. GitHub rejects the whole review with 422 if any
    comment's line is outside the diff, so callers place comments only on
    commentable lines.
    """
    repo = repo or _get_repo(client)
    pr = repo.get_pull(pr_number)
    # create_review asserts a Commit object; a bare SHA string fails.
    review = pr.create_review(
        commit=repo.get_commit(head_sha),
        body=body,
        event="COMMENT",
        comments=comments,
    )
    logger.debug("Posted review #%s with %d inline comment(s)", review.id, len(comments))
    return review.id


def reply_to_review_comment(
    pr_number: int,
    comment_id: int,
    body: str,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> int:
    """Reply in the thread of review comment ``comment_id``; return the reply id."""
    pr = (repo or _get_repo(client)).get_pull(pr_number)
    reply = pr.create_review_comment_reply(comment_id, body)
    return reply.id


def set_thread_resolved(
    thread_id: str,
    resolved: bool,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> bool:
    """Resolve (or unresolve) review thread ``thread_id``, a GraphQL node id.

    Returns the thread's resolved flag as GitHub reports it afterwards.
    """
    mutation = "resolveReviewThread" if resolved else "unresolveReviewThread"
    _, data = (repo or _get_repo(client)).requester.graphql_named_mutation(
        mutation, {"threadId": thread_id}, "thread { isResolved }"
    )
    return data["thread"]["isResolved"]


def _file_entry(f: File) -> dict[str, Any]:
    """One PyGithub ``File`` as the plain dict ``get_pr_files`` documents."""
    return {
        "filename": f.filename,
        "sha": f.sha,
        "status": f.status,
        "additions": f.additions,
        "deletions": f.deletions,
        "previous_filename": f.previous_filename,
        "patch": f.patch,
    }


def _thread_comment(node: dict[str, Any]) -> dict[str, Any]:
    """One GraphQL review comment in ``get_pr_comments``' shape.

    Two GraphQL differences are normalised away, because callers compare
    these values with REST ones: GraphQL names a GitHub App by its bare
    slug (``github-actions``) where REST says ``github-actions[bot]``, and
    it writes UTC as ``Z`` where ``datetime.isoformat`` writes ``+00:00``.
    """
    author = node["author"]  # None when the account was deleted
    login = None
    if author:
        login = author["login"]
        if author["__typename"] == "Bot":
            login += "[bot]"
    return {
        "id": node["databaseId"],
        "body": node["body"] or "",
        "created_at": datetime.fromisoformat(node["createdAt"]).isoformat(),
        "author": login,
        "author_association": node["authorAssociation"],
    }


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