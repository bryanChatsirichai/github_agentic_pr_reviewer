"""Tests for the orchestrator entry point: arg parsing, wiring, rendering.

GitHub and the LLM are replaced by stubs via monkeypatch. Posting is stubbed
at ``clients.github`` so the real ``save_memory`` still appends the state.
"""

from types import SimpleNamespace

import pytest
from github import GithubException

import clients.github as github_client
import orchestrator.main as main_module
from agents.reviewer_agents.parser import FindingParseError
from models.context_summary import ContextSummary
from models.finding import FindingStatus
from models.review_result import ReviewResult
from models.review_state import ReviewState
from orchestrator.main import main, parse_args, render_comment
from orchestrator.memory import StateTooLargeError, parse_state_block
from tests.helpers import (
    make_context,
    make_finding,
    make_review_state,
    make_stored_finding,
)


def must_not_post(pr, body, **kwargs):
    raise AssertionError("a failed review must not post a comment")


def earlier_review():
    """State from a review of an older commit than the test PR's head."""
    return make_review_state(reviewed_sha="oldsha0001")


@pytest.fixture
def stub_review(monkeypatch):
    """Stub everything run_review touches; returns the dict of posted comments."""
    posted = {}
    monkeypatch.setattr(main_module, "verify_connection", lambda: "owner/repo")
    monkeypatch.setattr(main_module, "resolve_model", lambda: ("google", "test-model"))
    monkeypatch.setattr(main_module, "load_memory", lambda pr: ReviewState())
    monkeypatch.setattr(main_module, "build_context", lambda pr: make_context())
    monkeypatch.setattr(main_module, "get_file_content", lambda path, ref: "line\n" * 20)
    monkeypatch.setattr(main_module, "get_merge_base_sha", lambda base, head: "mergebase789")
    monkeypatch.setattr(
        main_module,
        "build_reviewer",
        lambda name: SimpleNamespace(
            review=lambda ctx: ReviewResult(overall_rating="comment", findings=[make_finding()])
        ),
    )
    monkeypatch.setattr(
        github_client,
        "post_review_comment",
        lambda pr, body, **kwargs: posted.update(pr=pr, body=body),
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


def test_main_files_new_findings_into_the_posted_state(monkeypatch, stub_review) -> None:
    # Saving is the post itself: the block carries the previous findings
    # plus the new one, which gets the next ID and a snippet from the head.
    previous = earlier_review()  # holds F001, next_id=2
    monkeypatch.setattr(main_module, "load_memory", lambda pr: previous)
    new_issue = make_finding(title="Off-by-one in loop")
    monkeypatch.setattr(
        main_module,
        "build_reviewer",
        lambda name: SimpleNamespace(
            review=lambda ctx: ReviewResult(overall_rating="comment", findings=[new_issue])
        ),
    )
    fetched = []
    monkeypatch.setattr(
        main_module,
        "get_file_content",
        lambda path, ref: fetched.append((path, ref)) or "line\n" * 20,
    )

    assert main(["--pr", "42", "--event", "opened"]) == 0

    state = parse_state_block(stub_review["body"])
    assert [f.id for f in state.findings] == ["F001", "F002"]
    assert state.next_id == 3
    new = state.findings[1]
    assert new.status is FindingStatus.OPEN
    assert new.snippet == "\n".join(["line"] * 7)
    assert fetched == [("src/cli.py", "headdef456")]
    # The visible comment uses the stored ID, not the LLM's F001.
    assert "### F002 — Off-by-one in loop" in stub_review["body"]


def test_main_shows_the_stored_line_when_it_was_coerced(monkeypatch, stub_review) -> None:
    monkeypatch.setattr(
        main_module,
        "build_reviewer",
        lambda name: SimpleNamespace(
            review=lambda ctx: ReviewResult(
                overall_rating="comment", findings=[make_finding(line=0)]
            )
        ),
    )

    assert main(["--pr", "42", "--event", "opened"]) == 0

    assert parse_state_block(stub_review["body"]).findings[0].line == 1
    assert "`src/cli.py:1`" in stub_review["body"]
    assert "`src/cli.py:0`" not in stub_review["body"]


def test_main_does_not_refile_a_known_issue(monkeypatch, stub_review) -> None:
    # The stub reviewer repeats stored F001 (same file, line and title).
    previous = earlier_review()
    monkeypatch.setattr(main_module, "load_memory", lambda pr: previous)

    assert main(["--pr", "42", "--event", "synchronize"]) == 0

    state = parse_state_block(stub_review["body"])
    assert [f.id for f in state.findings] == ["F001"]
    assert state.next_id == 2  # no ID used up by the duplicate
    assert "### F002" not in stub_review["body"]
    # Still visible as a previous finding — never "No issues found".
    assert "- **F001** — Missing input validation · `src/cli.py:12` · open" in stub_review["body"]
    assert "No new issues found." in stub_review["body"]
    assert "No issues found. 🎉" not in stub_review["body"]


def test_main_records_the_reviewed_code_in_the_state(monkeypatch, stub_review) -> None:
    # Run as reopened: it takes the same stateful path as synchronize (the
    # head may have moved while the PR was closed). run_review never reads
    # the event, so one event covers all three.
    monkeypatch.setattr(main_module, "load_memory", lambda pr: earlier_review())
    files = [
        {"filename": "src/cli.py", "sha": "blob-new"},
        {"filename": "assets/logo.png", "sha": None},  # no blob SHA → left out
    ]
    monkeypatch.setattr(
        main_module, "build_context", lambda pr: make_context(changed_files=files)
    )
    merge_base_calls = []
    monkeypatch.setattr(
        main_module,
        "get_merge_base_sha",
        lambda base, head: merge_base_calls.append((base, head)) or "mergebase789",
    )

    assert main(["--pr", "42", "--event", "reopened"]) == 0

    state = parse_state_block(stub_review["body"])
    assert state.reviewed_sha == "headdef456"
    assert state.base_ref == "main"
    assert state.base_merge_sha == "mergebase789"
    # Replaced, not merged: files no longer in the PR drop out.
    assert state.file_shas == {"src/cli.py": "blob-new"}
    assert merge_base_calls == [("main", "headdef456")]


def test_main_skips_a_commit_that_was_already_reviewed(monkeypatch, stub_review) -> None:
    # "Re-run job" at the same SHA: no LLM call, no second comment, exit 0.
    monkeypatch.setattr(
        main_module, "load_memory", lambda pr: make_review_state(reviewed_sha="headdef456")
    )

    def must_not_review(name):
        raise AssertionError("an already-reviewed commit must not reach the LLM")

    monkeypatch.setattr(main_module, "build_reviewer", must_not_review)
    monkeypatch.setattr(github_client, "post_review_comment", must_not_post)

    assert main(["--pr", "42", "--event", "synchronize"]) == 0


def test_main_does_not_post_when_the_merge_base_lookup_fails(monkeypatch, stub_review) -> None:
    def raise_not_found(base, head):
        raise GithubException(404, {"message": "Not Found"})

    monkeypatch.setattr(main_module, "get_merge_base_sha", raise_not_found)
    monkeypatch.setattr(github_client, "post_review_comment", must_not_post)

    assert main(["--pr", "42", "--event", "synchronize"]) == 1


def test_main_shows_previous_findings_to_the_reviewer(monkeypatch, stub_review) -> None:
    previous = earlier_review()
    monkeypatch.setattr(main_module, "load_memory", lambda pr: previous)
    seen = {}

    def review(ctx):
        seen["previous"] = ctx.previous_findings
        return ReviewResult(overall_rating="approve")

    monkeypatch.setattr(
        main_module, "build_reviewer", lambda name: SimpleNamespace(review=review)
    )

    assert main(["--pr", "42", "--event", "synchronize"]) == 0
    assert seen["previous"] == previous.findings


def test_main_fetches_each_file_with_findings_once(monkeypatch, stub_review) -> None:
    findings = [
        make_finding(file="src/cli.py", line=3),
        make_finding(file="src/fibonacci.py", line=4),
        make_finding(file="src/cli.py", line=9, title="Unclosed file handle"),
    ]
    monkeypatch.setattr(
        main_module,
        "build_reviewer",
        lambda name: SimpleNamespace(
            review=lambda ctx: ReviewResult(overall_rating="comment", findings=findings)
        ),
    )
    fetched = []
    monkeypatch.setattr(
        main_module,
        "get_file_content",
        lambda path, ref: fetched.append(path) or "line\n" * 20,
    )

    assert main(["--pr", "42", "--event", "opened"]) == 0

    assert fetched == ["src/cli.py", "src/fibonacci.py"]
    state = parse_state_block(stub_review["body"])
    assert [f.id for f in state.findings] == ["F001", "F002", "F003"]


def test_main_manual_dispatch_does_not_review(monkeypatch, stub_review) -> None:
    # --pr 0 is the workflow_dispatch smoke test: it checks the token and
    # pings the LLM, but never builds a context or posts a review.
    monkeypatch.setattr(main_module.llm, "get_llm", lambda: SimpleNamespace(invoke=lambda msg: SimpleNamespace(content="ok")))
    monkeypatch.setattr(github_client, "post_review_comment", must_not_post)

    assert main(["--pr", "0", "--event", "push"]) == 0


def test_main_manual_dispatch_exits_1_on_config_error(monkeypatch, stub_review) -> None:
    # The --pr 0 smoke test takes the same logged failure path as a review.
    def raise_config_error():
        raise RuntimeError("MODEL_PROVIDER is not set")

    monkeypatch.setattr(main_module.llm, "get_llm", raise_config_error)

    assert main(["--pr", "0", "--event", "push"]) == 1


def test_main_exits_1_on_config_error(monkeypatch, stub_review) -> None:
    # Missing LLM config → exit 1 (fails the Action) before any GitHub write.
    def raise_config_error():
        raise RuntimeError("MODEL_PROVIDER is not set")

    monkeypatch.setattr(main_module, "resolve_model", raise_config_error)
    monkeypatch.setattr(github_client, "post_review_comment", must_not_post)

    assert main(["--pr", "42", "--event", "opened"]) == 1


def test_main_exits_1_on_github_api_error(monkeypatch, stub_review) -> None:
    # PyGithub errors (forbidden, missing PR) take the logged failure path,
    # not an uncaught traceback.
    def raise_github_error(pr, body, **kwargs):
        raise GithubException(403, {"message": "Forbidden"})

    monkeypatch.setattr(github_client, "post_review_comment", raise_github_error)

    assert main(["--pr", "42", "--event", "opened"]) == 1


def test_main_exits_1_on_unparseable_reviewer_output(monkeypatch, stub_review) -> None:
    # An empty comment must never look like a clean review — fail instead.
    def raise_parse_error(ctx):
        raise FindingParseError("invalid JSON")

    monkeypatch.setattr(
        main_module, "build_reviewer", lambda name: SimpleNamespace(review=raise_parse_error)
    )
    monkeypatch.setattr(github_client, "post_review_comment", must_not_post)

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


def test_render_comment_shows_previous_then_new_findings() -> None:
    previous = [
        make_stored_finding(),
        make_stored_finding(
            id="F002", status=FindingStatus.REJECTED, rejected_by="developer", line=30
        ),
    ]
    result = ReviewResult(
        overall_rating="comment",
        findings=[make_finding(id="F003", title="Off-by-one in loop")],
    )

    body = render_comment(make_context(previous_findings=previous), result)

    assert (
        "**Previous findings (2):**\n\n"
        "- **F001** — Missing input validation · `src/cli.py:12` · open\n"
        "- **F002** — Missing input validation · `src/cli.py:30` · rejected (developer)\n\n"
        "**New findings (1):**\n\n"
        "### F003 — Off-by-one in loop"
    ) in body
    assert "Found 1 issue(s)" not in body


def test_render_comment_with_only_previous_findings() -> None:
    body = render_comment(
        make_context(previous_findings=[make_stored_finding()]),
        ReviewResult(overall_rating="comment"),
    )

    assert "**New findings (0):**\n\nNo new issues found." in body
    assert "No issues found. 🎉" not in body


def test_render_comment_leaves_out_verified_and_closed_findings() -> None:
    previous = [
        make_stored_finding(status=FindingStatus.VERIFIED),
        make_stored_finding(id="F002", status=FindingStatus.CLOSED),
    ]

    body = render_comment(
        make_context(previous_findings=previous), ReviewResult(overall_rating="approve")
    )

    assert "Previous findings" not in body
    assert "No issues found. 🎉" in body


def test_render_comment_collapses_long_previous_lists() -> None:
    previous = [make_stored_finding(id=f"F{n:03d}") for n in range(1, 13)]

    body = render_comment(
        make_context(previous_findings=previous), ReviewResult(overall_rating="comment")
    )

    assert "<summary>Show all 12</summary>" in body
    assert "- **F012** —" in body


def test_render_comment_keeps_previous_titles_on_one_line() -> None:
    # Titles come from an editable comment: no injected headings.
    finding = make_stored_finding(title="Bad check\n\n## Approved")

    body = render_comment(
        make_context(previous_findings=[finding]), ReviewResult(overall_rating="comment")
    )

    assert "- **F001** — Bad check ## Approved ·" in body
    assert "\n## Approved" not in body


def test_render_comment_collapses_long_file_lists() -> None:
    files = [f"f{i}.py" for i in range(12)]
    context = make_context(summary=ContextSummary(diff_size=0, file_list=files))

    body = render_comment(context, ReviewResult(overall_rating="approve"))

    assert "<summary>Files checked (12)</summary>" in body


def test_main_exits_1_when_the_state_block_is_too_large(monkeypatch, stub_review) -> None:
    # A review whose memory can't be saved must not be posted without it —
    # the next run would lose every open finding and restart at F001.
    def raise_too_large(pr):
        raise StateTooLargeError("state block is 30000 chars")

    monkeypatch.setattr(main_module, "save_memory", lambda pr, body, state: raise_too_large(pr))
    monkeypatch.setattr(github_client, "post_review_comment", must_not_post)

    assert main(["--pr", "42", "--event", "opened"]) == 1
