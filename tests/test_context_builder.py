"""Unit tests for orchestrator.context_builder.build_context.

Uses the same fixture-backed fake PR as test_github_client.py (injected via
the ``repo`` kwarg) so no test touches the network.
"""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.context_builder import build_context

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pr-42.json"


def load_pr_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def make_repo(fixture: dict) -> MagicMock:
    """Mock repo returning a PyGithub-shaped PR built from the fixture."""
    pr = SimpleNamespace(
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
    )
    repo = MagicMock()
    repo.get_pull.return_value = pr
    return repo


def test_build_context_populates_every_field() -> None:
    fixture = load_pr_fixture()
    ctx = build_context(42, repo=make_repo(fixture))

    # Every field populated from the fixture.
    assert ctx.pr_number == 42
    assert ctx.title == fixture["title"]
    assert ctx.body == fixture["body"]
    assert ctx.author == "octocat"
    assert ctx.base_branch == "main"
    assert ctx.head_branch == "feature/fibonacci"
    assert ctx.base_sha == fixture["base"]["sha"]
    assert ctx.head_sha == fixture["head"]["sha"]
    assert len(ctx.changed_files) == 3
    # Only the bot comment (id 501) survives; the human one (502) is filtered.
    assert [c["id"] for c in ctx.existing_comments] == [501]
    # Memory doesn't exist yet — see roadmap Step 6.
    assert ctx.previous_findings == []


def test_build_context_diff_is_unified_and_matches_summary() -> None:
    fixture = load_pr_fixture()
    ctx = build_context(42, repo=make_repo(fixture))

    assert ctx.diff.startswith("diff --git a/src/cli.py b/src/cli.py")
    assert ctx.summary.diff_size == len(ctx.diff)
    assert ctx.summary.file_list == [f["filename"] for f in fixture["files"]]
