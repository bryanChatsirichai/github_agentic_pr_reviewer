"""Tests for the orchestrator entry point (arg parsing + review wiring)."""

from types import SimpleNamespace

import orchestrator.main as main_module
from agents.reviewer_agents.parser import FindingParseError
from models.finding import Finding, FindingStatus, Severity
from models.pr_context import ContextSummary, PRContext
from models.review import ReviewResult
from orchestrator.main import main, parse_args, render_comment


def make_finding(**overrides) -> Finding:
    defaults = dict(
        id="F001",
        status=FindingStatus.NEW,
        severity=Severity.MEDIUM,
        file="src/cli.py",
        line=12,
        title="Missing input validation",
        description="argv[1] is used without checking length.",
        confidence=0.9,
    )
    defaults.update(overrides)
    return Finding(**defaults)


def make_context() -> PRContext:
    return PRContext(
        pr_number=42,
        title="Add fibonacci CLI",
        body="Compute fib numbers from the command line.",
        author="octocat",
        base_branch="main",
        head_branch="feature/fibonacci",
        base_sha="baseabc123",
        head_sha="headdef456",
        diff="",
        changed_files=[],
        existing_comments=[],
        previous_findings=[],
        summary=ContextSummary(diff_size=0, file_list=["src/cli.py", "src/fibonacci.py"]),
    )


def test_parse_args() -> None:
    args = parse_args(["--pr", "123", "--event", "opened"])
    assert args.pr == 123
    assert args.event == "opened"


def test_main_manual_dispatch_no_pr_is_a_clean_noop(monkeypatch) -> None:
    # --pr 0 must not touch GitHub or the LLM at all.
    def fail(name):
        raise AssertionError(f"{name} should not be called with --pr 0")

    monkeypatch.setattr(main_module, "verify_connection", lambda: fail("verify_connection"))
    monkeypatch.setattr(main_module, "build_context", lambda pr: fail("build_context"))

    assert main(["--pr", "0", "--event", "push"]) == 0


def test_main_posts_review_comment_end_to_end(monkeypatch) -> None:
    """Full happy path: context → reviewer → rendered comment posted."""
    posted = {}

    monkeypatch.setattr(main_module, "verify_connection", lambda: "test-bot")
    monkeypatch.setattr(main_module, "build_context", lambda pr: make_context())
    monkeypatch.setattr(
        main_module,
        "build_reviewer",
        lambda name: SimpleNamespace(
            review=lambda ctx: ReviewResult(
                overall_rating="comment", findings=[make_finding()]
            )
        ),
    )
    monkeypatch.setattr(
        main_module,
        "post_review_comment",
        lambda pr, body: posted.update(pr=pr, body=body) or 123,
    )

    assert main(["--pr", "42", "--event", "opened"]) == 0
    assert posted["pr"] == 42
    # The 🤖 AI Review MARKER is prepended by the real post_review_comment
    # (covered in test_github_client.py); the stub here captures the raw body.
    assert "F001 — Missing input validation" in posted["body"]
    assert "src/cli.py:12" in posted["body"]
    # Metadata header: verdict, branch, commit, files-checked count.
    assert "**Overall: comment**" in posted["body"]
    assert "`feature/fibonacci` @ `headdef456`" in posted["body"]
    assert "2 file(s) checked" in posted["body"]
    assert "src/cli.py, src/fibonacci.py" in posted["body"]


def test_main_exits_1_on_llm_config_error(monkeypatch) -> None:
    # resolve_model raises RuntimeError for missing provider config — main
    # must log it and exit 1 (which fails the GitHub Action on the same
    # message) without posting anything.
    def raise_config_error():
        raise RuntimeError("MODEL_PROVIDER is not set")

    monkeypatch.setattr(main_module, "verify_connection", lambda: "test-bot")
    monkeypatch.setattr(main_module, "resolve_model", raise_config_error)
    monkeypatch.setattr(
        main_module, "post_review_comment",
        lambda pr, body: (_ for _ in ()).throw(AssertionError("must not post")),
    )

    assert main(["--pr", "42", "--event", "opened"]) == 1


def test_main_exits_1_on_unparseable_reviewer_output(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "verify_connection", lambda: "test-bot")
    monkeypatch.setattr(main_module, "resolve_model", lambda: ("google", "test-model"))
    monkeypatch.setattr(main_module, "build_context", lambda pr: make_context())

    def raise_parse_error(ctx):
        raise FindingParseError("invalid JSON: junk")

    monkeypatch.setattr(
        main_module,
        "build_reviewer",
        lambda name: SimpleNamespace(review=raise_parse_error),
    )
    monkeypatch.setattr(
        main_module, "post_review_comment",
        lambda pr, body: (_ for _ in ()).throw(AssertionError("must not post")),
    )

    assert main(["--pr", "42", "--event", "opened"]) == 1


def test_render_comment_lists_findings() -> None:
    result = ReviewResult(
        overall_rating="request_changes",
        findings=[make_finding(), make_finding(id="F002", line=30)],
    )
    body = render_comment(make_context(), result)

    assert "**Overall: request_changes**" in body
    assert "`feature/fibonacci` @ `headdef456`" in body
    assert "2 file(s) checked" in body
    assert "Files checked: src/cli.py, src/fibonacci.py" in body
    assert "Found 2 issue(s):" in body
    assert "F001 — Missing input validation" in body
    assert "F002 — Missing input validation" in body
    assert "severity: **medium**" in body
    assert "confidence: 0.90" in body
    # Description is included so the comment is actionable on its own.
    assert "argv[1] is used without checking length." in body


def test_render_comment_no_findings() -> None:
    body = render_comment(
        make_context(), ReviewResult(overall_rating="approve", findings=[])
    )

    assert "No issues found. 🎉" in body
    assert "**Overall: approve**" in body
    assert "2 file(s) checked" in body


def test_render_comment_collapses_long_file_lists() -> None:
    context = make_context()
    context.summary = ContextSummary(diff_size=0, file_list=[f"f{i}.py" for i in range(12)])
    body = render_comment(context, ReviewResult(overall_rating="approve", findings=[]))

    assert "<details>" in body
    assert "<summary>Files checked (12)</summary>" in body
