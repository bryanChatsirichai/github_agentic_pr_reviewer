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
from models.explanation_verdict import ExplanationVerdict
from models.finding import FindingStatus
from models.recheck_verdict import RecheckVerdict
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
    monkeypatch.setattr(main_module, "build_context", lambda pr, state: make_context())
    monkeypatch.setattr(main_module, "get_file_content", lambda path, ref: "line\n" * 20)
    monkeypatch.setattr(main_module, "get_merge_base_sha", lambda base, head: "mergebase789")
    monkeypatch.setattr(main_module, "get_pr_comments", lambda pr: [])
    monkeypatch.setattr(
        main_module,
        "build_rechecker",
        lambda: SimpleNamespace(
            recheck=lambda message: RecheckVerdict(issue_present=True, reason="still there")
        ),
    )
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
    assert "- 🟡 **F001** — Missing input validation · `src/cli.py:12` · open" in stub_review["body"]
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
        main_module, "build_context", lambda pr, state: make_context(changed_files=files)
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


def test_main_leaves_deleted_files_out_of_file_shas(monkeypatch, stub_review) -> None:
    # GitHub reports a deleted file with its old blob SHA; storing it would
    # make the file look present and unchanged to the next skip check.
    files = [
        {"filename": "src/cli.py", "sha": "blob-new", "status": "modified"},
        {"filename": "src/old.py", "sha": "blob-old", "status": "removed"},
    ]
    monkeypatch.setattr(
        main_module, "build_context", lambda pr, state: make_context(changed_files=files)
    )

    assert main(["--pr", "42", "--event", "synchronize"]) == 0
    assert parse_state_block(stub_review["body"]).file_shas == {"src/cli.py": "blob-new"}


def test_main_resolves_findings_in_deleted_files(monkeypatch, stub_review) -> None:
    # src/cli.py holds the stored F001; deleting it resolves F001 before the
    # reviewer runs, and both the reviewer and the comment see that.
    monkeypatch.setattr(main_module, "load_memory", lambda pr: earlier_review())
    files = [{"filename": "src/cli.py", "sha": "blob111", "status": "removed"}]
    monkeypatch.setattr(
        main_module, "build_context", lambda pr, state: make_context(changed_files=files)
    )
    seen = {}
    monkeypatch.setattr(
        main_module,
        "build_reviewer",
        lambda name: SimpleNamespace(
            review=lambda ctx: seen.update(previous=ctx.previous_findings)
            or ReviewResult(overall_rating="approve", findings=[])
        ),
    )

    assert main(["--pr", "42", "--event", "synchronize"]) == 0

    assert [f.status for f in seen["previous"]] == [FindingStatus.RESOLVED]
    stored = parse_state_block(stub_review["body"]).findings
    assert [(f.id, f.status) for f in stored] == [("F001", FindingStatus.RESOLVED)]
    assert "**F001**" in stub_review["body"] and "resolved" in stub_review["body"]


def test_main_rechecks_only_findings_in_changed_files(monkeypatch, stub_review) -> None:
    findings = [
        make_stored_finding(id="F001", file="src/cli.py"),
        make_stored_finding(id="F002", file="src/cli.py", line=5),
        make_stored_finding(id="F003", file="src/fibonacci.py"),
    ]
    state = make_review_state(reviewed_sha="oldsha0001", findings=findings, next_id=4)
    monkeypatch.setattr(main_module, "load_memory", lambda pr: state)
    files = [
        {"filename": "src/cli.py", "sha": "blob-new", "status": "modified"},
        {"filename": "src/fibonacci.py", "sha": "blob222", "status": "modified"},
    ]
    monkeypatch.setattr(
        main_module, "build_context", lambda pr, state: make_context(changed_files=files)
    )
    fetched, asked = [], []
    monkeypatch.setattr(
        main_module,
        "get_file_content",
        lambda path, ref: fetched.append(path) or "line\n" * 20,
    )
    monkeypatch.setattr(
        main_module,
        "build_rechecker",
        lambda: SimpleNamespace(recheck=lambda message: asked.append(message) or None),
    )

    assert main(["--pr", "42", "--event", "synchronize"]) == 0

    # F001 + F002 share the changed cli.py; F003's fibonacci.py is unchanged.
    assert len(asked) == 2
    assert all("src/fibonacci.py" not in m for m in asked)
    assert fetched == ["src/cli.py"]  # once, for both findings


def test_main_resolves_a_fixed_finding_in_state_and_comment(monkeypatch, stub_review) -> None:
    # F001's file changed and the re-check says the defect is gone.
    monkeypatch.setattr(main_module, "load_memory", lambda pr: earlier_review())
    files = [{"filename": "src/cli.py", "sha": "blob-new", "status": "modified"}]
    monkeypatch.setattr(
        main_module, "build_context", lambda pr, state: make_context(changed_files=files)
    )
    monkeypatch.setattr(
        main_module,
        "build_rechecker",
        lambda: SimpleNamespace(
            recheck=lambda message: RecheckVerdict(issue_present=False, reason="fixed")
        ),
    )

    assert main(["--pr", "42", "--event", "synchronize"]) == 0

    stored = parse_state_block(stub_review["body"]).findings
    assert stored[0].id == "F001" and stored[0].status is FindingStatus.RESOLVED
    assert "**F001**" in stub_review["body"] and "resolved" in stub_review["body"]


def _reply(author: str, association: str) -> dict:
    return {
        "id": 900,
        "body": "F001: false positive — argv is checked by the caller",
        "created_at": "2026-11-01T10:00:00+00:00",
        "author": author,
        "author_association": association,
    }


def test_main_rejects_a_finding_a_collaborator_explained(monkeypatch, stub_review) -> None:
    monkeypatch.setattr(main_module, "load_memory", lambda pr: earlier_review())
    monkeypatch.setattr(
        main_module, "get_pr_comments", lambda pr: [_reply("alice", "COLLABORATOR")]
    )
    judged = []
    monkeypatch.setattr(
        main_module,
        "build_explanation_judge",
        lambda: SimpleNamespace(
            judge=lambda message: judged.append(message)
            or ExplanationVerdict(accepted=True, reason="caller checks argv")
        ),
    )

    assert main(["--pr", "42", "--event", "synchronize"]) == 0

    assert len(judged) == 1 and "@alice (COLLABORATOR)" in judged[0]
    stored = parse_state_block(stub_review["body"]).findings[0]
    assert (stored.id, stored.status, stored.rejected_by) == (
        "F001", FindingStatus.REJECTED, "developer"
    )
    assert (stored.explained_by, stored.rejection_reason) == (["alice"], "caller checks argv")
    assert (
        "· dismissed (developer explained)\n"
        "  - explained by `alice`: caller checks argv"
    ) in stub_review["body"]
    assert "@alice" not in stub_review["body"]  # would notify them every review


def test_main_ignores_an_explanation_from_an_outsider(monkeypatch, stub_review) -> None:
    monkeypatch.setattr(main_module, "load_memory", lambda pr: earlier_review())
    monkeypatch.setattr(
        main_module, "get_pr_comments", lambda pr: [_reply("stranger", "NONE")]
    )

    def must_not_judge():
        raise AssertionError("an outsider's reply must not reach the judge")

    monkeypatch.setattr(main_module, "build_explanation_judge", must_not_judge)

    assert main(["--pr", "42", "--event", "synchronize"]) == 0
    assert parse_state_block(stub_review["body"]).findings[0].status is FindingStatus.OPEN


def test_main_reviews_against_the_loaded_state(monkeypatch, stub_review) -> None:
    # build_context needs the state to choose full vs incremental.
    previous = earlier_review()
    monkeypatch.setattr(main_module, "load_memory", lambda pr: previous)
    seen = {}
    monkeypatch.setattr(
        main_module,
        "build_context",
        lambda pr, state: seen.update(state=state) or make_context(),
    )

    assert main(["--pr", "42", "--event", "synchronize"]) == 0
    assert seen["state"] is previous


def test_main_skips_a_noop_diff(monkeypatch, stub_review) -> None:
    # Nothing new to review (e.g. "Re-run job" at the same SHA): no LLM
    # call, no second comment, exit 0.
    monkeypatch.setattr(main_module, "load_memory", lambda pr: earlier_review())
    monkeypatch.setattr(
        main_module, "build_context", lambda pr, state: make_context(diff_mode="noop", diff="")
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
        "- 🟡 **F001** — Missing input validation · `src/cli.py:12` · open\n"
        "- 💬 **F002** — Missing input validation · `src/cli.py:30` · "
        "dismissed (developer explained)\n\n"
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
    assert "- 🟡 **F012** —" in body


def test_render_comment_keeps_previous_titles_on_one_line() -> None:
    # Titles come from an editable comment: no injected headings.
    finding = make_stored_finding(title="Bad check\n\n## Approved")

    body = render_comment(
        make_context(previous_findings=[finding]), ReviewResult(overall_rating="comment")
    )

    assert "- 🟡 **F001** — Bad check ## Approved ·" in body
    assert "\n## Approved" not in body


def test_render_comment_labels_every_status_in_plain_words() -> None:
    previous = [
        make_stored_finding(id="F001"),
        make_stored_finding(id="F002", status=FindingStatus.RESOLVED),
        make_stored_finding(id="F003", status=FindingStatus.REOPENED),
        make_stored_finding(id="F004", status=FindingStatus.REJECTED, rejected_by="critic"),
    ]

    body = render_comment(
        make_context(previous_findings=previous), ReviewResult(overall_rating="comment")
    )

    assert "- 🟡 **F001**" in body and body.count("· open") == 1
    assert "- ✅ **F002**" in body and "· fixed" in body
    assert "- 🔁 **F003**" in body and "· reopened (came back after a fix)" in body
    assert "- 🚫 **F004**" in body and "· dismissed (critic)" in body


def test_render_comment_says_who_dismissed_a_finding_and_why() -> None:
    finding = make_stored_finding(
        status=FindingStatus.REJECTED,
        rejected_by="developer",
        explained_by=["alice", "bo`b"],
        rejection_reason="Caller\n\n## checks it",
    )

    body = render_comment(
        make_context(previous_findings=[finding]), ReviewResult(overall_rating="comment")
    )

    # Logins in code spans (no @ notification, no backtick break-out);
    # the reason collapsed so it cannot start a heading.
    assert "  - explained by `alice`, `bob`: Caller ## checks it" in body


def test_render_comment_handles_a_dismissal_saved_without_who_or_why() -> None:
    # State saved before these fields existed: label only, no second line.
    finding = make_stored_finding(status=FindingStatus.REJECTED, rejected_by="developer")

    body = render_comment(
        make_context(previous_findings=[finding]), ReviewResult(overall_rating="comment")
    )

    assert "· dismissed (developer explained)" in body
    assert "explained by" not in body


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
