"""Tests for the orchestrator entry point: arg parsing, wiring, rendering.

GitHub and the LLM are replaced by stubs via monkeypatch on orchestrator.main.
"""

from types import SimpleNamespace

import pytest

import orchestrator.main as main_module
from agents.reviewer_agents.parser import FindingParseError
from models.pr_context import ContextSummary
from models.review import ReviewResult
from orchestrator.main import main, parse_args, render_comment
from tests.helpers import make_context, make_finding


def must_not_post(pr, body):
    raise AssertionError("a failed review must not post a comment")


@pytest.fixture
def stub_review(monkeypatch):
    """Stub everything run_review touches; returns the dict of posted comments."""
    posted = {}
    monkeypatch.setattr(main_module, "verify_connection", lambda: "owner/repo")
    monkeypatch.setattr(main_module, "resolve_model", lambda: ("google", "test-model"))
    monkeypatch.setattr(main_module, "build_context", lambda pr: make_context())
    monkeypatch.setattr(
        main_module,
        "build_reviewer",
        lambda name: SimpleNamespace(
            review=lambda ctx: ReviewResult(overall_rating="comment", findings=[make_finding()])
        ),
    )
    monkeypatch.setattr(
        main_module, "post_review_comment", lambda pr, body: posted.update(pr=pr, body=body)
    )
    return posted


def test_parse_args() -> None:
    args = parse_args(["--pr", "123", "--event", "opened"])
    assert (args.pr, args.event) == (123, "opened")


# --- main ------------------------------------------------------------------


def test_main_posts_review_comment(stub_review) -> None:
    assert main(["--pr", "42", "--event", "opened"]) == 0
    assert stub_review["pr"] == 42
    assert "F001 — Missing input validation" in stub_review["body"]


def test_main_manual_dispatch_does_not_review(monkeypatch, stub_review) -> None:
    # --pr 0 is the workflow_dispatch smoke test: it checks the token and
    # pings the LLM, but never builds a context or posts a review.
    monkeypatch.setattr(main_module.llm, "get_llm", lambda: SimpleNamespace(invoke=lambda msg: "ok"))
    monkeypatch.setattr(main_module, "post_review_comment", must_not_post)

    assert main(["--pr", "0", "--event", "push"]) == 0


def test_main_exits_1_on_config_error(monkeypatch, stub_review) -> None:
    # Missing LLM config → exit 1 (fails the Action) before any GitHub write.
    def raise_config_error():
        raise RuntimeError("MODEL_PROVIDER is not set")

    monkeypatch.setattr(main_module, "resolve_model", raise_config_error)
    monkeypatch.setattr(main_module, "post_review_comment", must_not_post)

    assert main(["--pr", "42", "--event", "opened"]) == 1


def test_main_exits_1_on_unparseable_reviewer_output(monkeypatch, stub_review) -> None:
    # An empty comment must never look like a clean review — fail instead.
    def raise_parse_error(ctx):
        raise FindingParseError("invalid JSON")

    monkeypatch.setattr(
        main_module, "build_reviewer", lambda name: SimpleNamespace(review=raise_parse_error)
    )
    monkeypatch.setattr(main_module, "post_review_comment", must_not_post)

    assert main(["--pr", "42", "--event", "opened"]) == 1


# --- render_comment ---------------------------------------------------------


def test_render_comment_lists_findings() -> None:
    result = ReviewResult(
        overall_rating="request_changes",
        findings=[make_finding(), make_finding(id="F002", line=30)],
    )

    body = render_comment(make_context(), result)

    # Header metadata comes from PRContext, not the model.
    assert "**Overall: request_changes**" in body
    assert "`feature/fibonacci` @ `headdef456`" in body
    assert "Files checked: src/cli.py, src/fibonacci.py" in body
    # Each finding: id/title, location, severity, confidence, description.
    assert "Found 2 issue(s):" in body
    assert "### F002 — Missing input validation" in body
    assert "`src/cli.py:12` · severity: **medium** · confidence: 0.90" in body
    assert "argv[1] is used without checking length." in body


def test_render_comment_no_findings() -> None:
    body = render_comment(make_context(), ReviewResult(overall_rating="approve"))

    assert "**Overall: approve**" in body
    assert "No issues found. 🎉" in body


def test_render_comment_collapses_long_file_lists() -> None:
    files = [f"f{i}.py" for i in range(12)]
    context = make_context(summary=ContextSummary(diff_size=0, file_list=files))

    body = render_comment(context, ReviewResult(overall_rating="approve"))

    assert "<summary>Files checked (12)</summary>" in body
