"""Unit tests for clients.github.

Every public function takes an injectable ``repo`` / ``client``, so tests
pass fakes built from the PR fixture and never touch the network.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

import clients.github as github
from clients.github import (
    MARKER,
    get_existing_review_comments,
    get_file_content,
    get_merge_base_sha,
    get_pr_diff,
    get_pr_files,
    get_pull_request,
    post_review_comment,
    verify_connection,
)
from tests.helpers import load_pr_fixture, make_fake_pr, make_repo


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
