"""Tests for ``drop_duplicates``: repeats of known issues never get new IDs."""

import pytest

from models.finding import FindingStatus
from orchestrator.memory import drop_duplicates
from tests.helpers import make_finding, make_stored_finding

#: Stored F001: src/cli.py:12 "Missing input validation", status open.
STORED = [make_stored_finding()]


@pytest.mark.parametrize(
    "line, title",
    [
        (12, "Missing input validation"),
        (40, "missing input validation."),  # same title: line distance ignored
        (14, "Input validation missing"),  # near line, similar title (0.67)
        (9, "Missing validation of input"),  # near line, similar title (0.71)
    ],
    ids=["identical", "same-title-far-line", "reordered-near", "reworded-near"],
)
def test_repeats_are_dropped(line: int, title: str) -> None:
    assert drop_duplicates(STORED, [make_finding(line=line, title=title)]) == []


@pytest.mark.parametrize(
    "finding",
    [
        make_finding(line=13, title="Off-by-one in loop"),  # adjacent, other bug
        make_finding(line=20, title="Input validation missing"),  # similar, far
        make_finding(file="src/utils.py"),  # same title, other file
    ],
    ids=["adjacent-different-title", "similar-title-far-line", "different-file"],
)
def test_different_issues_are_kept(finding) -> None:
    assert drop_duplicates(STORED, [finding]) == [finding]


@pytest.mark.parametrize(
    "status, rejected_by, dropped",
    [
        (FindingStatus.OPEN, None, True),
        (FindingStatus.REOPENED, None, True),
        (FindingStatus.RESOLVED, None, True),
        (FindingStatus.REJECTED, "developer", True),
        (FindingStatus.VERIFIED, None, False),
        (FindingStatus.CLOSED, None, False),
    ],
)
def test_only_known_issue_statuses_suppress(status, rejected_by, dropped) -> None:
    # A confirmed fix that comes back must be reported, not silently dropped.
    stored = [make_stored_finding(status=status, rejected_by=rejected_by)]

    kept = drop_duplicates(stored, [make_finding()])

    assert (kept == []) is dropped


def test_repeats_within_one_review_are_dropped() -> None:
    first = make_finding(line=12)
    repeat = make_finding(line=13, title="Missing input validation!")
    other = make_finding(line=30, title="Off-by-one in loop")

    assert drop_duplicates([], [first, repeat, other]) == [first, other]
