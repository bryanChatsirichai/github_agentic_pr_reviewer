"""Shared test builders — fakes for GitHub objects plus model factories.

Everything here is offline: GitHub objects are SimpleNamespace fakes shaped
like PyGithub's, built from ``fixtures/pr-42.json``.
"""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from models.finding import Finding, FindingStatus, Severity
from models.pr_context import ContextSummary, PRContext
from models.review_state import ReviewState, StoredFinding

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pr-42.json"


def load_pr_fixture() -> dict:
    # Explicit UTF-8: the fixture holds the 🤖 marker, and Windows' default
    # encoding (cp1252) would garble it so the bot comment never matches.
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def make_fake_pr(fixture: dict) -> SimpleNamespace:
    """A PyGithub-shaped PullRequest built from the fixture."""
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
                user=SimpleNamespace(login=c["user"]["login"]),
            )
            for c in fixture["issue_comments"]
        ],
        create_issue_comment=MagicMock(return_value=SimpleNamespace(id=777)),
    )


def make_repo(pr) -> MagicMock:
    """A mock Repository whose ``get_pull`` returns ``pr``."""
    repo = MagicMock()
    repo.get_pull.return_value = pr
    return repo


def make_finding(**overrides) -> Finding:
    """A valid Finding; pass keyword overrides for the field under test."""
    fields = dict(
        id="F001",
        status=FindingStatus.NEW,
        severity=Severity.MEDIUM,
        file="src/cli.py",
        line=12,
        title="Missing input validation",
        description="argv[1] is used without checking length.",
        confidence=0.9,
    )
    return Finding(**(fields | overrides))


def make_stored_finding(**overrides) -> StoredFinding:
    """A valid StoredFinding (state-block shape); pass keyword overrides."""
    fields = dict(
        id="F001",
        status=FindingStatus.OPEN,
        severity=Severity.MEDIUM,
        file="src/cli.py",
        line=12,
        title="Missing input validation",
        description="argv[1] is used without checking length.",
        confidence=0.9,
        snippet="n = int(sys.argv[1])",
    )
    return StoredFinding(**(fields | overrides))


def make_review_state(**overrides) -> ReviewState:
    """A populated ReviewState holding one open finding; pass overrides."""
    fields = dict(
        reviewed_sha="headdef456",
        base_ref="main",
        base_merge_sha="baseabc123",
        next_id=2,
        file_shas={"src/cli.py": "blob111", "src/fibonacci.py": "blob222"},
        findings=[make_stored_finding()],
    )
    return ReviewState(**(fields | overrides))


def make_context(**overrides) -> PRContext:
    """A small, valid PRContext; pass keyword overrides as needed."""
    fields = dict(
        pr_number=42,
        title="Add fibonacci CLI",
        body="Compute fib numbers from the command line.",
        author="octocat",
        base_branch="main",
        head_branch="feature/fibonacci",
        base_sha="baseabc123",
        head_sha="headdef456",
        diff="diff --git a/src/cli.py b/src/cli.py",
        changed_files=[],
        existing_comments=[],
        summary=ContextSummary(diff_size=36, file_list=["src/cli.py", "src/fibonacci.py"]),
    )
    return PRContext(**(fields | overrides))
