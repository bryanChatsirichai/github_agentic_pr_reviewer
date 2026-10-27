"""Tests for the finding lifecycle table in orchestrator.memory (design section 20).

The legal moves are listed here by hand, not read from ``ALLOWED_TRANSITIONS``,
so a typo in the table fails a test instead of silently changing the rules.
"""

from itertools import product

import pytest
from pydantic import ValidationError

from models.finding import FindingStatus as S
from orchestrator.memory import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    check_transition,
    transition,
)
from tests.helpers import make_stored_finding

LEGAL = [
    (S.NEW, S.OPEN),
    (S.OPEN, S.RESOLVED),
    (S.OPEN, S.REJECTED),
    (S.RESOLVED, S.REOPENED),
    (S.RESOLVED, S.VERIFIED),
    (S.REOPENED, S.RESOLVED),
    (S.REOPENED, S.REJECTED),
    (S.VERIFIED, S.CLOSED),
]
#: Includes self-moves: Step 9's "still_open" verdict means *don't call*
#: ``transition``, so open → open is a caller bug.
ILLEGAL = [pair for pair in product(S, S) if pair not in LEGAL]


def _finding_in(status: S):
    """A stored finding currently in ``status`` (rejected needs a rejecter)."""
    rejected_by = "critic" if status is S.REJECTED else None
    return make_stored_finding(status=status, rejected_by=rejected_by)


def test_table_matches_design() -> None:
    table = {(src, dst) for src, dsts in ALLOWED_TRANSITIONS.items() for dst in dsts}
    assert table == set(LEGAL)
    assert set(ALLOWED_TRANSITIONS) == set(S)  # every status listed, even terminal


@pytest.mark.parametrize(
    "src, dst", [p for p in LEGAL if p[0] is not S.NEW], ids=lambda s: s.value
)
def test_transition_moves_a_stored_finding(src: S, dst: S) -> None:
    # new → open is excluded: a stored finding is never ``new`` (Step 7 files it).
    rejected_by = "developer" if dst is S.REJECTED else None

    moved = transition(_finding_in(src), dst, rejected_by=rejected_by)

    assert moved.status is dst
    assert moved.rejected_by == rejected_by


@pytest.mark.parametrize("src, dst", ILLEGAL, ids=lambda s: s.value)
def test_illegal_moves_raise(src: S, dst: S) -> None:
    with pytest.raises(IllegalTransitionError, match=f"{src.value} → {dst.value}"):
        check_transition(src, dst)


def test_rejecting_requires_who_rejected() -> None:
    with pytest.raises(ValidationError, match="rejected_by"):
        transition(_finding_in(S.OPEN), S.REJECTED)


def test_rejected_by_is_only_for_rejections() -> None:
    with pytest.raises(ValidationError, match="rejected_by"):
        transition(_finding_in(S.OPEN), S.RESOLVED, rejected_by="critic")
