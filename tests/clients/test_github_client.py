"""Unit tests for clients.github.

Every public function takes an injectable ``repo`` / ``client``, so tests
pass fakes built from the PR fixture and never touch the network.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

import clients.github as github
from clients.github import (
    MARKER,
    compare_commits,
    get_existing_review_comments,
    file_exists,
    get_file_content,
    get_merge_base_sha,
    get_pr_comments,
    get_pr_diff,
    get_pr_files,
    get_pull_request,
    get_review_threads,
    post_inline_comments,
    post_review_comment,
    reply_to_review_comment,
    set_thread_resolved,
    verify_connection,
)
from tests.helpers import GoneComparison, load_pr_fixture, make_fake_pr, make_repo


@pytest.fixture
def fixture() -> dict:
    return load_pr_fixture()


@pytest.fixture
def pr(fixture):
    return make_fake_pr(fixture)


# --- verify_connection / env config ----------------------------------------


def test_verify_connection_returns_repo_full_name(monkeypatch) -> None:
    monkeypatch.setattr(github, "_get_repo", lambda: SimpleNamespace(full_name="owner/repo"))
    assert verify_connection() == "owner/repo"


@pytest.mark.parametrize(
    "env, error",
    [
        ({"GITHUB_REPOSITORY": "owner/repo"}, "GITHUB_TOKEN"),  # token missing
        ({"GITHUB_TOKEN": "t"}, "GITHUB_REPOSITORY"),  # repo missing
        ({"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "no-slash"}, "owner/repo"),  # bad format
    ],
)
def test_verify_connection_rejects_bad_env(monkeypatch, env, error) -> None:
    # Config errors must surface as RuntimeError with a message naming the
    # variable, so a failed CI run says exactly what to fix.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match=error):
        verify_connection()


# --- reads -----------------------------------------------------------------


def test_get_pull_request_returns_metadata(pr, fixture) -> None:
    repo = make_repo(pr)

    result = get_pull_request(42, repo=repo)

    repo.get_pull.assert_called_once_with(42)
    assert result == {
        "number": 42,
        "title": fixture["title"],
        "body": fixture["body"],
        "base_branch": "main",
        "head_branch": "feature/fibonacci",
        "base_sha": fixture["base"]["sha"],
        "head_sha": fixture["head"]["sha"],
        "author": "octocat",
    }


def test_injected_client_resolves_repo_from_env(monkeypatch, pr) -> None:
    # With only a client injected, the repo comes from GITHUB_REPOSITORY.
    client = MagicMock()
    client.get_repo.return_value = make_repo(pr)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    assert get_pull_request(42, client=client)["number"] == 42
    client.get_repo.assert_called_once_with("owner/repo")


def test_get_pr_files_maps_each_file(pr) -> None:
    files = get_pr_files(42, repo=make_repo(pr))

    assert [f["filename"] for f in files] == ["src/cli.py", "src/fibonacci.py", "assets/logo.png"]
    assert files[0] == {
        "filename": "src/cli.py",
        "sha": "c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1",
        "status": "modified",
        "additions": 5,
        "deletions": 1,
        "previous_filename": None,
        "patch": files[0]["patch"],
    }
    # Binary files come back without a patch.
    assert files[2]["patch"] is None


def test_get_pr_diff_renders_unified_diff(pr) -> None:
    diff = get_pr_diff(42, repo=make_repo(pr))

    # Modified file: git-style headers followed by the hunk.
    assert "diff --git a/src/cli.py b/src/cli.py\n--- a/src/cli.py\n+++ b/src/cli.py" in diff
    assert "@@ -12,6 +12,10 @@" in diff
    # Added file: the source side is /dev/null.
    assert "--- /dev/null\n+++ b/src/fibonacci.py" in diff
    # Binary file: placeholder instead of ---/+++ headers.
    assert "# no text patch available for assets/logo.png (binary or too large)" in diff


def test_get_existing_review_comments_keeps_only_bot_comments(pr) -> None:
    # Fixture has one bot comment (501, has MARKER) and one human reply (502).
    comments = get_existing_review_comments(42, repo=make_repo(pr))

    assert [c["id"] for c in comments] == [501]
    assert comments[0]["created_at"] == "2026-10-20T09:30:00+00:00"
    # load_memory trusts only state blocks from the bot's own login.
    assert comments[0]["author"] == "github-actions[bot]"


def test_get_merge_base_sha_compares_base_branch_with_head() -> None:
    repo = MagicMock()
    repo.compare.return_value = SimpleNamespace(
        merge_base_commit=SimpleNamespace(sha="mergebase789")
    )

    assert get_merge_base_sha("main", "headdef456", repo=repo) == "mergebase789"
    repo.compare.assert_called_once_with("main", "headdef456")


def test_compare_commits_returns_status_and_files(fixture) -> None:
    repo = MagicMock()
    repo.compare.return_value = SimpleNamespace(
        status="ahead", files=[SimpleNamespace(**fixture["files"][0])]
    )

    result = compare_commits("old123", "headdef456", repo=repo)

    repo.compare.assert_called_once_with("old123", "headdef456")
    assert result["status"] == "ahead"
    # Same entry shape as get_pr_files, so render_diff can format it.
    assert result["files"] == [
        get_pr_files(42, repo=make_repo(make_fake_pr(fixture)))[0]
    ]


def test_compare_commits_raises_the_lazy_api_error_itself() -> None:
    repo = MagicMock()
    repo.compare.return_value = GoneComparison()

    with pytest.raises(GithubException):
        compare_commits("gone123", "headdef456", repo=repo)


def _content_file(text: str, encoding: str = "base64") -> SimpleNamespace:
    """A PyGithub-shaped ContentFile (only the fields get_file_content reads)."""
    return SimpleNamespace(encoding=encoding, decoded_content=text.encode("utf-8"))


def test_get_file_content_reads_the_file_at_ref() -> None:
    repo = MagicMock()
    repo.get_contents.return_value = _content_file("print('hi')\n")

    assert get_file_content("src/cli.py", "headdef456", repo=repo) == "print('hi')\n"
    repo.get_contents.assert_called_once_with("src/cli.py", ref="headdef456")


@pytest.mark.parametrize(
    "result",
    [[_content_file("a"), _content_file("b")], _content_file("", encoding="none")],
    ids=["directory", "over-1mb"],
)
def test_get_file_content_returns_none_without_file_text(result) -> None:
    repo = MagicMock()
    repo.get_contents.return_value = result

    assert get_file_content("src", "headdef456", repo=repo) is None


def test_get_file_content_returns_none_for_missing_path() -> None:
    # Deleted at head, or a path the LLM made up.
    repo = MagicMock()
    repo.get_contents.side_effect = GithubException(404, {"message": "Not Found"})

    assert get_file_content("nope.py", "headdef456", repo=repo) is None


def test_get_file_content_raises_other_api_errors() -> None:
    repo = MagicMock()
    repo.get_contents.side_effect = GithubException(403, {"message": "Forbidden"})

    with pytest.raises(GithubException):
        get_file_content("src/cli.py", "headdef456", repo=repo)


def test_get_file_content_replaces_invalid_utf8() -> None:
    repo = MagicMock()
    repo.get_contents.return_value = SimpleNamespace(
        encoding="base64", decoded_content=b"ok \xff"
    )

    assert get_file_content("bin.dat", "headdef456", repo=repo) == "ok �"


def test_get_pr_comments_returns_every_comment_with_its_association() -> None:
    at = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    comments = [
        SimpleNamespace(
            id=1, body="F001: why", created_at=at,
            user=SimpleNamespace(login="alice"), author_association="OWNER",
        ),
        SimpleNamespace(  # deleted account, empty body
            id=2, body=None, created_at=at, user=None, author_association="NONE",
        ),
    ]
    repo = make_repo(SimpleNamespace(get_issue_comments=lambda: comments))

    assert get_pr_comments(42, repo=repo) == [
        {"id": 1, "body": "F001: why", "created_at": at.isoformat(),
         "author": "alice", "author_association": "OWNER"},
        {"id": 2, "body": "", "created_at": at.isoformat(),
         "author": None, "author_association": "NONE"},
    ]


@pytest.mark.parametrize(
    "result",
    [[_content_file("a")], _content_file("", encoding="none")],
    ids=["directory", "over-1mb"],
)
def test_file_exists_for_paths_without_readable_text(result) -> None:
    # The cases get_file_content returns None for, yet the path is there.
    repo = MagicMock()
    repo.get_contents.return_value = result

    assert file_exists("big.bin", "headdef456", repo=repo) is True


def test_file_exists_false_only_on_404() -> None:
    repo = MagicMock()
    repo.get_contents.side_effect = GithubException(404, {"message": "Not Found"})

    assert file_exists("gone.py", "headdef456", repo=repo) is False


def test_file_exists_raises_other_api_errors() -> None:
    repo = MagicMock()
    repo.get_contents.side_effect = GithubException(403, {"message": "Forbidden"})

    with pytest.raises(GithubException):
        file_exists("src/cli.py", "headdef456", repo=repo)


# --- writes ----------------------------------------------------------------


def test_post_review_comment_prepends_marker(pr) -> None:
    comment_id = post_review_comment(42, "Summary of findings.", repo=make_repo(pr))

    pr.create_issue_comment.assert_called_once_with(f"{MARKER}\n\nSummary of findings.")
    assert comment_id == 777


@pytest.mark.parametrize("body", [f"{MARKER}\n\nmarked", f"   {MARKER}\n\nindented"])
def test_post_review_comment_keeps_existing_marker(pr, body) -> None:
    # Already-marked bodies (even with leading whitespace) are posted as-is.
    post_review_comment(42, body, repo=make_repo(pr))

    pr.create_issue_comment.assert_called_once_with(body)


# --- review threads (Step 9b) ----------------------------------------------


def _thread_node(
    thread_id: str, resolved: bool, comments: list[dict], more_comments: bool = False
) -> dict:
    return {"id": thread_id, "isResolved": resolved, "comments": {
        "nodes": comments, "pageInfo": {"hasNextPage": more_comments},
    }}


def _threads_page(threads: list[dict], next_cursor: str | None = None) -> tuple:
    """One GraphQL answer: a page of threads, and the cursor of the next page."""
    return (
        {},
        {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": threads,
            "pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor},
        }}}}},
    )


def _graphql_repo(*pages: tuple) -> MagicMock:
    """A repo whose requester answers the review-threads query with ``pages``."""
    repo = MagicMock()
    repo.owner.login = "octo"
    repo.name = "widgets"
    repo.requester.graphql_query.side_effect = list(pages)
    return repo


def test_get_review_threads_queries_the_pr_by_owner_name_and_number() -> None:
    repo = _graphql_repo(_threads_page([]))

    assert get_review_threads(42, repo=repo) == []

    _, variables = repo.requester.graphql_query.call_args.args
    assert variables == {"owner": "octo", "name": "widgets", "number": 42, "cursor": None}


def test_get_review_threads_normalises_comments_to_match_get_pr_comments() -> None:
    bot_comment = {
        "databaseId": 101, "body": "<!-- ai-review-finding:F001 --> issue",
        "createdAt": "2026-11-01T10:00:00Z", "authorAssociation": "NONE",
        # GraphQL names a GitHub App by its slug, without REST's "[bot]".
        "author": {"__typename": "Bot", "login": "github-actions"},
    }
    human_reply = {
        "databaseId": 102, "body": None,
        "createdAt": "2026-11-01T11:30:00Z", "authorAssociation": "OWNER",
        "author": {"__typename": "User", "login": "alice"},
    }
    ghost_reply = {
        "databaseId": 103, "body": "deleted account",
        "createdAt": "2026-11-01T12:00:00Z", "authorAssociation": "NONE",
        "author": None,
    }
    repo = _graphql_repo(
        _threads_page([_thread_node("PRRT_1", False, [bot_comment, human_reply, ghost_reply])])
    )

    [thread] = get_review_threads(42, repo=repo)

    at = lambda h, m: datetime(2026, 11, 1, h, m, tzinfo=timezone.utc).isoformat()
    assert thread == {
        "id": "PRRT_1",
        "is_resolved": False,
        "comments": [
            {"id": 101, "body": "<!-- ai-review-finding:F001 --> issue",
             "created_at": at(10, 0), "author": "github-actions[bot]",
             "author_association": "NONE"},
            {"id": 102, "body": "", "created_at": at(11, 30),
             "author": "alice", "author_association": "OWNER"},
            {"id": 103, "body": "deleted account", "created_at": at(12, 0),
             "author": None, "author_association": "NONE"},
        ],
    }


def test_get_review_threads_reads_every_page() -> None:
    # A thread missing from the result looks like "no thread yet" and gets
    # a duplicate inline comment, so a truncated list is not acceptable.
    repo = _graphql_repo(
        _threads_page([_thread_node("PRRT_1", True, [])], next_cursor="c1"),
        _threads_page([_thread_node("PRRT_2", False, [])]),
    )

    threads = get_review_threads(42, repo=repo)

    assert [t["id"] for t in threads] == ["PRRT_1", "PRRT_2"]
    cursors = [c.args[1]["cursor"] for c in repo.requester.graphql_query.call_args_list]
    assert cursors == [None, "c1"]


def test_get_review_threads_warns_when_a_thread_has_over_100_comments(caplog) -> None:
    repo = _graphql_repo(_threads_page([_thread_node("PRRT_1", False, [], more_comments=True)]))

    with caplog.at_level("WARNING", logger="pr-reviewer.github"):
        get_review_threads(42, repo=repo)

    assert "PRRT_1" in caplog.text and "first 100 comments" in caplog.text


def test_post_inline_comments_posts_one_comment_review_at_the_head(pr) -> None:
    head_commit = SimpleNamespace(sha="headdef456")
    repo = make_repo(pr)
    repo.get_commit.return_value = head_commit
    pr.create_review = MagicMock(return_value=SimpleNamespace(id=555))
    comments = [{"path": "src/cli.py", "line": 12, "side": "RIGHT", "body": "F001"}]

    review_id = post_inline_comments(42, "headdef456", "AI review", comments, repo=repo)

    repo.get_commit.assert_called_once_with("headdef456")
    pr.create_review.assert_called_once_with(
        commit=head_commit, body="AI review", event="COMMENT", comments=comments
    )
    assert review_id == 555


def test_reply_to_review_comment_replies_in_the_thread(pr) -> None:
    pr.create_review_comment_reply = MagicMock(return_value=SimpleNamespace(id=888))

    reply_id = reply_to_review_comment(42, 101, "✅ Fixed", repo=make_repo(pr))

    pr.create_review_comment_reply.assert_called_once_with(101, "✅ Fixed")
    assert reply_id == 888


@pytest.mark.parametrize(
    ("resolved", "mutation"),
    [(True, "resolveReviewThread"), (False, "unresolveReviewThread")],
)
def test_set_thread_resolved_sends_the_matching_mutation(resolved, mutation) -> None:
    repo = MagicMock()
    repo.requester.graphql_named_mutation.return_value = (
        {}, {"thread": {"isResolved": resolved}},
    )

    assert set_thread_resolved("PRRT_1", resolved, repo=repo) is resolved

    name, mutation_input, _ = repo.requester.graphql_named_mutation.call_args.args
    assert (name, mutation_input) == (mutation, {"threadId": "PRRT_1"})
