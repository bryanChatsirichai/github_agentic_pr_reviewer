"""Tests for orchestrator/recheck.py — re-checking previous findings.

The re-check LLM is a scripted fake, so no provider key or network is needed.
"""

from langchain_core.messages import AIMessage

from models.finding import FindingStatus
from models.recheck_verdict import RecheckVerdict
from orchestrator.memory import cut_snippet
from orchestrator.recheck import (
    RecheckOutcome,
    Rechecker,
    apply_outcomes,
    build_rechecker,
    code_window,
    file_diff,
    locate_line,
    recheck_findings,
    render_recheck,
    skip_unchanged,
)
from tests.helpers import make_context, make_stored_finding

# --- Stage 1: skip_unchanged ------------------------------------------------

#: The last review's blob SHA per file.
FILE_SHAS = {"auth.py": "A1", "utils.py": "U1", "old.py": "O1", "helper.py": "H1"}


def _file(name: str, sha: str, status: str = "modified", previous: str | None = None) -> dict:
    """A get_pr_files-shaped entry (patch left out — not read here)."""
    return dict(filename=name, sha=sha, status=status, previous_filename=previous)


def _finding(id: str, file: str, status: FindingStatus = FindingStatus.OPEN, **kw):
    return make_stored_finding(id=id, file=file, status=status, **kw)


def ids(findings) -> list[str]:
    return [f.id for f in findings]


def test_unchanged_file_keeps_status_and_skips_recheck() -> None:
    result = skip_unchanged(
        [_finding("F001", "utils.py")], FILE_SHAS, [_file("utils.py", "U1")]
    )

    assert result.to_recheck == []
    assert result.findings[0].status is FindingStatus.OPEN


def test_changed_file_goes_to_recheck_with_status_kept() -> None:
    result = skip_unchanged(
        [_finding("F001", "auth.py")], FILE_SHAS, [_file("auth.py", "A2")]
    )

    assert ids(result.to_recheck) == ["F001"]
    assert result.findings[0].status is FindingStatus.OPEN


def test_deleted_file_resolves_open_and_reopened_findings() -> None:
    findings = [
        _finding("F001", "old.py"),
        _finding("F002", "old.py", FindingStatus.REOPENED),
    ]

    result = skip_unchanged(findings, FILE_SHAS, [_file("old.py", "O1", "removed")])

    # GitHub reports a deleted file with its old SHA; status, not SHA, decides.
    assert [f.status for f in result.findings] == [FindingStatus.RESOLVED] * 2
    assert result.to_recheck == []


def test_deleted_file_leaves_a_resolved_finding_alone() -> None:
    finding = _finding("F001", "old.py", FindingStatus.RESOLVED)

    result = skip_unchanged([finding], FILE_SHAS, [_file("old.py", "O1", "removed")])

    assert result.findings == [finding]
    assert result.to_recheck == []


def test_pure_rename_follows_the_new_name_without_recheck() -> None:
    files = [_file("helpers.py", "H1", "renamed", previous="helper.py")]

    result = skip_unchanged([_finding("F001", "helper.py")], FILE_SHAS, files)

    assert result.findings[0].file == "helpers.py"
    assert result.to_recheck == []


def test_rename_with_edits_follows_the_new_name_and_rechecks() -> None:
    files = [_file("helpers.py", "H2", "renamed", previous="helper.py")]

    result = skip_unchanged([_finding("F001", "helper.py")], FILE_SHAS, files)

    assert result.findings[0].file == "helpers.py"
    # Stage 2 must fetch the new name, so the re-check copy carries it too.
    assert [f.file for f in result.to_recheck] == ["helpers.py"]


def test_resolved_findings_in_changed_files_are_rechecked() -> None:
    # The fix may have been undone: resolved → reopened is Task 2's call.
    finding = _finding("F001", "auth.py", FindingStatus.RESOLVED)

    result = skip_unchanged([finding], FILE_SHAS, [_file("auth.py", "A2")])

    assert ids(result.to_recheck) == ["F001"]


def test_settled_findings_are_never_touched() -> None:
    findings = [
        _finding("F001", "auth.py", FindingStatus.REJECTED, rejected_by="developer"),
        _finding("F002", "auth.py", FindingStatus.VERIFIED),
        _finding("F003", "old.py", FindingStatus.CLOSED),
    ]
    files = [_file("auth.py", "A2"), _file("old.py", "O1", "removed")]

    result = skip_unchanged(findings, FILE_SHAS, files)

    assert result.findings == findings
    assert result.to_recheck == []


def test_file_without_a_stored_sha_is_rechecked() -> None:
    # e.g. state saved before file_shas existed: nothing to compare against.
    result = skip_unchanged([_finding("F001", "auth.py")], {}, [_file("auth.py", "A1")])

    assert ids(result.to_recheck) == ["F001"]


def test_file_gone_from_the_pr_is_rechecked_not_resolved() -> None:
    # Added then deleted, or every change reverted: both leave the PR's
    # file list, and only the head file can tell them apart.
    result = skip_unchanged([_finding("F001", "auth.py")], FILE_SHAS, [])

    assert ids(result.to_recheck) == ["F001"]
    assert result.findings[0].status is FindingStatus.OPEN


def test_mixed_findings_keep_input_order() -> None:
    findings = [
        _finding("F001", "auth.py"),
        _finding("F002", "utils.py"),
        _finding("F003", "old.py"),
    ]
    files = [
        _file("auth.py", "A2"),
        _file("utils.py", "U1"),
        _file("old.py", "O1", "removed"),
    ]

    result = skip_unchanged(findings, FILE_SHAS, files)

    assert [f.id for f in result.findings] == ["F001", "F002", "F003"]
    assert [f.status for f in result.findings] == [
        FindingStatus.OPEN,
        FindingStatus.OPEN,
        FindingStatus.RESOLVED,
    ]
    assert ids(result.to_recheck) == ["F001"]


# --- Stage 2: the LLM re-check ----------------------------------------------

#: A 40-line file; line N reads "x = N".
FILE = "".join(f"x = {n}\n" for n in range(1, 41))


class FakeChatModel:
    """Scripted stand-in for ``llm.with_structured_output(..., include_raw=True)``.

    Each queued reply is a ``RecheckVerdict`` (parsed) or None (unusable).
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def with_structured_output(self, schema, *, include_raw=False):
        assert schema is RecheckVerdict and include_raw
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        error = None if reply else ValueError("bad json")
        return {"raw": AIMessage(content=""), "parsed": reply, "parsing_error": error}


class ScriptedRechecker:
    """Answers every finding the same way and records the messages."""

    def __init__(self, verdict: RecheckVerdict | None):
        self.verdict = verdict
        self.messages = []

    def recheck(self, message: str) -> RecheckVerdict | None:
        self.messages.append(message)
        return self.verdict


PRESENT = RecheckVerdict(issue_present=True, reason="still\n unchecked on line 14.")


def test_locate_line_finds_code_that_moved_down() -> None:
    snippet = "\n".join(f"x = {n}" for n in range(9, 16))  # cut around line 12
    moved = "new = 0\n" * 5 + FILE

    assert locate_line(moved, snippet, 12) == 17


def test_locate_line_ignores_indentation() -> None:
    snippet = "\n".join(f"x = {n}" for n in range(9, 16))
    indented = "".join(f"    x = {n}\n" for n in range(1, 41))

    assert locate_line(indented, snippet, 12) == 12


def test_locate_line_prefers_the_copy_nearest_the_old_line() -> None:
    # Two identical stretches: b = 2 sits on lines 7 and 30.
    text = "pad\n" * 5 + "a = 1\nb = 2\nc = 3\n" + "pad\n" * 20 + "a = 1\nb = 2\nc = 3\n" + "pad\n" * 5

    assert locate_line(text, cut_snippet(text, 30), 30) == 30
    assert locate_line(text, cut_snippet(text, 7), 7) == 7


def test_locate_line_none_when_code_is_gone_or_snippet_empty() -> None:
    assert locate_line(FILE, "def totally_different():\n    return None", 12) is None
    assert locate_line(FILE, "", 12) is None


def test_code_window_numbers_lines_and_clamps() -> None:
    window = code_window(FILE, 2, context=2).splitlines()

    assert window == ["    1| x = 1", "    2| x = 2", "    3| x = 3", "    4| x = 4"]


def test_file_diff_keeps_only_the_files_section() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+a\n"
        "diff --git a/old.py b/new.py\nrename from old.py\nrename to new.py\n"
        "diff --git a/b.py b/b.py\n+b\n"
    )

    assert file_diff(diff, "a.py").splitlines()[-1] == "+a"
    assert "b.py" not in file_diff(diff, "a.py")
    assert file_diff(diff, "new.py").startswith("diff --git a/old.py b/new.py")
    assert file_diff(diff, "missing.py") == ""


def test_render_recheck_holds_finding_snippet_window_and_diff() -> None:
    finding = make_stored_finding(title="Missing\ninput validation", line=12)
    context = make_context(diff_mode="incremental", diff="diff --git a/src/cli.py b/src/cli.py\n+x")

    message = render_recheck(finding, FILE, 17, context)

    assert "## Finding F001 (status: open)" in message
    assert "Title: Missing input validation" in message  # newline collapsed
    assert finding.snippet in message
    assert "around line 17" in message and "   17| x = 17" in message
    assert "since the last review" in message and "+x" in message


def test_render_recheck_says_when_the_code_was_not_found() -> None:
    message = render_recheck(make_stored_finding(line=12), FILE, None, make_context(diff=""))

    assert "was not found" in message and "   12| x = 12" in message
    assert "in the whole pull request" in message
    assert "(none in this diff)" in message


def test_recheck_findings_asks_once_per_finding() -> None:
    findings = [make_stored_finding(id="F001"), make_stored_finding(id="F002")]
    recheck_agent = ScriptedRechecker(PRESENT)

    outcomes = recheck_findings(findings, {"src/cli.py": FILE}, make_context(), recheck_agent)

    assert len(recheck_agent.messages) == 2
    assert outcomes["F001"].issue_present is True
    assert outcomes["F001"].reason == "still unchecked on line 14."  # one line


def test_recheck_findings_reports_the_located_line() -> None:
    snippet = "\n".join(f"x = {n}" for n in range(9, 16))
    finding = make_stored_finding(line=12, snippet=snippet)
    moved = "new = 0\n" * 5 + FILE

    outcomes = recheck_findings(
        [finding], {"src/cli.py": moved}, make_context(), ScriptedRechecker(PRESENT)
    )

    assert outcomes["F001"].line == 17


def test_recheck_findings_resolves_a_file_gone_from_head_and_pr(monkeypatch) -> None:
    # Added by the PR, then deleted: gone from head and from the file list.
    monkeypatch.setattr("clients.github.file_exists", lambda path, ref: False)
    recheck_agent = ScriptedRechecker(PRESENT)

    outcomes = recheck_findings(
        [make_stored_finding()], {"src/cli.py": None}, make_context(), recheck_agent
    )

    assert outcomes["F001"].issue_present is False
    assert recheck_agent.messages == []  # no LLM call


def test_recheck_findings_skips_an_unreadable_file_outside_the_pr(monkeypatch) -> None:
    # Over 1 MB with all PR changes reverted: it exists, so not resolved.
    monkeypatch.setattr("clients.github.file_exists", lambda path, ref: True)
    recheck_agent = ScriptedRechecker(PRESENT)

    outcomes = recheck_findings(
        [make_stored_finding()], {"src/cli.py": None}, make_context(), recheck_agent
    )

    assert outcomes == {}
    assert recheck_agent.messages == []


def test_recheck_findings_skips_an_unreadable_file_still_in_the_pr() -> None:
    # Too large or a directory: can't tell, so the finding keeps its status.
    context = make_context(changed_files=[{"filename": "src/cli.py", "sha": "s"}])
    recheck_agent = ScriptedRechecker(PRESENT)

    outcomes = recheck_findings([make_stored_finding()], {"src/cli.py": None}, context, recheck_agent)

    assert outcomes == {}
    assert recheck_agent.messages == []


def test_recheck_findings_skips_a_finding_without_a_usable_answer() -> None:
    outcomes = recheck_findings(
        [make_stored_finding()], {"src/cli.py": FILE}, make_context(), ScriptedRechecker(None)
    )

    assert outcomes == {}


def test_rechecker_retries_once_then_returns_the_verdict() -> None:
    llm = FakeChatModel(None, PRESENT)

    assert Rechecker("prompt", llm).recheck("msg") == PRESENT
    assert len(llm.calls) == 2
    assert "could not be parsed" in llm.calls[1][-1].content


def test_rechecker_gives_up_after_two_bad_replies() -> None:
    llm = FakeChatModel(None, None)

    assert Rechecker("prompt", llm).recheck("msg") is None
    assert len(llm.calls) == 2


def test_build_rechecker_loads_the_prompt(monkeypatch) -> None:
    fake = FakeChatModel()
    monkeypatch.setattr("orchestrator.recheck.get_llm", lambda: fake)

    recheck_agent = build_rechecker()

    assert recheck_agent.system_prompt.startswith("# Finding Re-checker")
    assert recheck_agent.llm is fake


# --- Stage 3: apply_outcomes -----------------------------------------------


def _outcome(present: bool, line: int = 12) -> RecheckOutcome:
    return RecheckOutcome(issue_present=present, reason="r", line=line)


def test_fixed_open_and_reopened_findings_become_resolved() -> None:
    findings = [
        make_stored_finding(id="F001"),
        make_stored_finding(id="F002", status=FindingStatus.REOPENED),
    ]
    outcomes = {"F001": _outcome(False), "F002": _outcome(False)}

    updated = apply_outcomes(findings, outcomes, {"src/cli.py": FILE})

    assert [f.status for f in updated] == [FindingStatus.RESOLVED] * 2


def test_a_resolved_finding_that_came_back_is_reopened() -> None:
    finding = make_stored_finding(status=FindingStatus.RESOLVED)

    updated = apply_outcomes([finding], {"F001": _outcome(True)}, {"src/cli.py": FILE})

    assert updated[0].status is FindingStatus.REOPENED


def test_answers_that_match_the_status_change_nothing() -> None:
    findings = [
        make_stored_finding(id="F001"),  # open, still present
        make_stored_finding(id="F002", status=FindingStatus.RESOLVED),  # still gone
    ]
    outcomes = {"F001": _outcome(True), "F002": _outcome(False)}

    assert apply_outcomes(findings, outcomes, {"src/cli.py": FILE}) == findings


def test_findings_without_an_outcome_are_unchanged() -> None:
    findings = [make_stored_finding(id="F001"), make_stored_finding(id="F002")]

    updated = apply_outcomes(findings, {"F002": _outcome(False)}, {"src/cli.py": FILE})

    assert updated[0] == findings[0]
    assert updated[1].status is FindingStatus.RESOLVED


def test_a_present_finding_follows_its_code_with_a_fresh_snippet() -> None:
    finding = make_stored_finding(line=12)

    updated = apply_outcomes([finding], {"F001": _outcome(True, line=17)}, {"src/cli.py": FILE})

    assert updated[0].line == 17
    assert updated[0].snippet == cut_snippet(FILE, 17)


def test_a_resolved_finding_keeps_its_line_and_faulty_snippet() -> None:
    # The snippet is the buggy code a later regression would bring back.
    finding = make_stored_finding(line=12)

    updated = apply_outcomes([finding], {"F001": _outcome(False, line=17)}, {"src/cli.py": FILE})

    assert (updated[0].line, updated[0].snippet) == (12, finding.snippet)
