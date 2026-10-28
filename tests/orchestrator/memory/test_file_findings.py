"""Tests for ``file_new_findings``: reviewer findings → stored findings."""

from models.finding import FindingStatus
from models.review_state import ReviewState
from models.stored_finding import MAX_DESCRIPTION_CHARS
from orchestrator.memory import file_new_findings
from tests.helpers import make_finding, make_review_state

#: Twenty lines: "l1" … "l20".
CLI = "\n".join(f"l{n}" for n in range(1, 21)) + "\n"


def test_findings_get_fresh_ids_open_status_and_snippets() -> None:
    # The LLM numbers every run from F001; those IDs are replaced.
    findings = [make_finding(id="F001", line=12), make_finding(id="F001", line=2)]

    stored, state = file_new_findings(ReviewState(), findings, {"src/cli.py": CLI})

    assert [f.id for f in stored] == ["F001", "F002"]
    assert all(f.status is FindingStatus.OPEN for f in stored)
    assert stored[0].snippet == "\n".join(f"l{n}" for n in range(9, 16))
    assert stored[1].snippet == "\n".join(f"l{n}" for n in range(1, 6))
    assert state.findings == stored
    assert state.next_id == 3


def test_ids_continue_and_previous_findings_are_kept() -> None:
    previous = make_review_state()  # holds F001, next_id=2

    stored, state = file_new_findings(previous, [make_finding()], {"src/cli.py": CLI})

    assert stored[0].id == "F002"
    assert [f.id for f in state.findings] == ["F001", "F002"]
    assert state.next_id == 3
    assert previous.next_id == 2 and len(previous.findings) == 1  # not mutated


def test_missing_file_text_gives_empty_snippet() -> None:
    # None = deleted / made-up path / too large; absent key behaves the same.
    findings = [make_finding(file="gone.py"), make_finding(file="huge.bin")]

    stored, _ = file_new_findings(ReviewState(), findings, {"gone.py": None})

    assert [f.snippet for f in stored] == ["", ""]


def test_line_below_one_is_stored_as_one() -> None:
    stored, _ = file_new_findings(
        ReviewState(), [make_finding(line=0)], {"src/cli.py": CLI}
    )

    assert stored[0].line == 1
    assert stored[0].snippet == "\n".join(f"l{n}" for n in range(1, 5))


def test_long_description_is_capped() -> None:
    stored, _ = file_new_findings(
        ReviewState(), [make_finding(description="x" * 1000)], {"src/cli.py": CLI}
    )

    assert len(stored[0].description) == MAX_DESCRIPTION_CHARS
    assert stored[0].description.endswith("…")


def test_no_findings_leaves_state_unchanged() -> None:
    previous = make_review_state()

    stored, state = file_new_findings(previous, [], {})

    assert stored == []
    assert state == previous
