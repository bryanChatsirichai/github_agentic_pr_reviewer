"""Unit tests for orchestrator.github_client.

All GitHub objects are mocked (or hand-rolled fakes configured from the
``tests/fixtures/pr-42.json`` fixture) so no test ever touches the network.
The module's public functions accept injectable ``repo`` / ``client`` keyword
args for exactly this purpose.
"""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import orchestrator.github_client as github_client
from orchestrator.github_client import (
    MARKER,
    get_existing_review_comments,
    get_pr_diff,
    get_pr_files,
    get_pull_request,
    post_review_comment,
    verify_connection,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE_PATH = FIXTURE_DIR / "pr-42.json"


def load_pr_fixture() -> dict:
    """Load the shared PR fixture as plain dicts (shaped like PyGithub data)."""
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture
def pr_fixture() -> dict:
    return load_pr_fixture()


def make_pr(fixture: dict) -> SimpleNamespace:
    """Build a hand-rolled PR object from the fixture, PyGithub-shaped."""
    return SimpleNamespace(
        number=fixture["number"],
        title=fixture["title"],
        body=fixture["body"],
        base=SimpleNamespace(ref=fixture["base"]["ref"], sha=fixture["base"]["sha"]),
        head=SimpleNamespace(ref=fixture["head"]["ref"], sha=fixture["head"]["sha"]),
        user=SimpleNamespace(login=fixture["user"]["login"]),
        get_files=lambda: [SimpleNamespace(**f) for f in fixture["files"]],
        get_issue_comments=lambda: [
            SimpleNamespace(
                id=c["id"],
                body=c["body"],
                created_at=datetime.fromisoformat(c["created_at"]),
            )
            for c in fixture["issue_comments"]
        ],
        create_issue_comment=MagicMock(return_value=SimpleNamespace(id=777)),
    )


def make_repo(pr) -> MagicMock:
    """Build a mock repo that returns ``pr`` from get_pull."""
    repo = MagicMock()
    repo.get_pull.return_value = pr
    return repo


# --- verify_connection ------------------------------------------------------


def test_verify_connection_returns_repo_full_name(monkeypatch) -> None:
    monkeypatch.setattr(
        github_client, "_get_repo", lambda: SimpleNamespace(full_name="owner/repo")
    )
    assert verify_connection() == "owner/repo"


def test_verify_connection_raises_without_token(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        verify_connection()


def test_verify_connection_raises_without_repository(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    with pytest.raises(RuntimeError, match="GITHUB_REPOSITORY"):
        verify_connection()


def test_verify_connection_raises_on_bad_repository_format(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "not-a-slug")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    with pytest.raises(RuntimeError, match="owner/repo"):
        verify_connection()


# --- get_pull_request -------------------------------------------------------


def test_get_pull_request_returns_metadata(pr_fixture) -> None:
    repo = make_repo(make_pr(pr_fixture))
    result = get_pull_request(42, repo=repo)

    repo.get_pull.assert_called_once_with(42)
    assert result == {
        "number": 42,
        "title": pr_fixture["title"],
        "body": pr_fixture["body"],
        "base_branch": "main",
        "head_branch": "feature/fibonacci",
        "base_sha": pr_fixture["base"]["sha"],
        "head_sha": pr_fixture["head"]["sha"],
        "author": "octocat",
    }


def test_get_pull_request_uses_injected_client(monkeypatch, pr_fixture) -> None:
    pr = make_pr(pr_fixture)
    repo = MagicMock()
    repo.get_pull.return_value = pr
    client = MagicMock()
    client.get_repo.return_value = repo
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    result = get_pull_request(42, client=client)

    client.get_repo.assert_called_once_with("owner/repo")
    assert result["number"] == 42


# --- get_pr_files -----------------------------------------------------------


def test_get_pr_files_returns_file_dicts(pr_fixture) -> None:
    repo = make_repo(make_pr(pr_fixture))
    files = get_pr_files(42, repo=repo)

    assert len(files) == 3
    expected_keys = {
        "filename",
        "status",
        "additions",
        "deletions",
        "previous_filename",
        "patch",
    }
    for f in files:
        assert set(f) == expected_keys

    modified, added, binary = files
    assert modified["filename"] == "src/cli.py"
    assert modified["status"] == "modified"
    assert modified["additions"] == 5
    assert modified["deletions"] == 1
    assert modified["previous_filename"] is None
    assert modified["patch"] is not None

    assert added["filename"] == "src/fibonacci.py"
    assert added["status"] == "added"
    assert added["patch"] is not None

    assert binary["filename"] == "assets/logo.png"
    assert binary["patch"] is None


# --- get_pr_diff ------------------------------------------------------------


def test_get_pr_diff_renders_unified_diff_headers_and_hunks(pr_fixture) -> None:
    repo = make_repo(make_pr(pr_fixture))
    diff = get_pr_diff(42, repo=repo)

    # Modified file: full git-style header + hunk with added/removed lines.
    assert "diff --git a/src/cli.py b/src/cli.py" in diff
    assert "--- a/src/cli.py" in diff
    assert "+++ b/src/cli.py" in diff
    assert "@@ -12,6 +12,10 @@" in diff
    assert '+    parser.add_argument("--fib"' in diff
    assert '-    logger.info("Starting review for PR #%s", args.pr)' in diff

    # Added file: /dev/null source side.
    assert "diff --git a/src/fibonacci.py b/src/fibonacci.py" in diff
    assert "--- /dev/null" in diff
    assert "+++ b/src/fibonacci.py" in diff
    assert "@@ -0,0 +1,11 @@" in diff

    # Binary / no-patch file: placeholder line, no ---/+++ headers.
    assert "diff --git a/assets/logo.png b/assets/logo.png" in diff
    assert (
        "# no text patch available for assets/logo.png (binary or too large)" in diff
    )


# --- get_existing_review_comments -------------------------------------------


def test_get_existing_review_comments_only_returns_bot_comments(
    pr_fixture,
) -> None:
    repo = make_repo(make_pr(pr_fixture))
    comments = get_existing_review_comments(42, repo=repo)

    # The human comment (id 502) must be filtered out.
    assert [c["id"] for c in comments] == [501]
    assert MARKER in comments[0]["body"]
    assert comments[0]["created_at"] == "2026-10-20T09:30:00+00:00"


def test_get_existing_review_comments_empty_when_no_bot_comments(
    pr_fixture,
) -> None:
    pr = make_pr(pr_fixture)
    pr.get_issue_comments = lambda: [
        SimpleNamespace(
            id=503,
            body="just a human comment",
            created_at=datetime.fromisoformat("2026-10-22T08:00:00+00:00"),
        )
    ]
    repo = make_repo(pr)
    assert get_existing_review_comments(42, repo=repo) == []


# --- post_review_comment ----------------------------------------------------


def test_post_review_comment_prepends_marker(pr_fixture) -> None:
    pr = make_pr(pr_fixture)
    repo = make_repo(pr)

    comment_id = post_review_comment(42, "Summary of findings.", repo=repo)

    posted_body = pr.create_issue_comment.call_args.args[0]
    assert posted_body == f"{MARKER}\n\nSummary of findings."
    pr.create_issue_comment.assert_called_once()
    assert comment_id == 777


def test_post_review_comment_does_not_double_prepend(pr_fixture) -> None:
    pr = make_pr(pr_fixture)
    repo = make_repo(pr)
    body = f"{MARKER}\n\nAlready marked."

    post_review_comment(42, body, repo=repo)

    pr.create_issue_comment.assert_called_once_with(body)


def test_post_review_comment_leading_whitespace_not_double_prepended(
    pr_fixture,
) -> None:
    pr = make_pr(pr_fixture)
    repo = make_repo(pr)
    body = f"   {MARKER}\n\nLeading whitespace before the marker."

    post_review_comment(42, body, repo=repo)

    pr.create_issue_comment.assert_called_once_with(body)