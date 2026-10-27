"""Tests for finding ID allocation: ``F001``, ``F002``, … continuing from next_id."""

import pytest

from models.review_state import ReviewState, finding_number, format_finding_id
from orchestrator.memory import allocate_ids
from tests.helpers import make_review_state, make_stored_finding


@pytest.mark.parametrize(
    "number, finding_id", [(1, "F001"), (42, "F042"), (999, "F999"), (1000, "F1000")]
)
def test_format_finding_id(number: int, finding_id: str) -> None:
    assert format_finding_id(number) == finding_id
    assert finding_number(finding_id) == number  # the two are inverses


def test_format_finding_id_rejects_numbers_below_one() -> None:
    with pytest.raises(ValueError):
        format_finding_id(0)


def test_ids_continue_from_next_id() -> None:
    # next_id, not the stored findings, decides: F001–F008 may have been
    # trimmed out of the block, and their IDs must still never be reused.
    state = make_review_state(next_id=9, findings=[])

    ids, new_state = allocate_ids(state, 2)

    assert ids == ["F009", "F010"]
    assert new_state.next_id == 11


def test_negative_count_raises() -> None:
    # Would move next_id backwards and hand out IDs that are already in use.
    with pytest.raises(ValueError):
        allocate_ids(ReviewState(), -1)


def test_ids_past_f999_are_valid_stored_finding_ids() -> None:
    ids, state = allocate_ids(make_review_state(next_id=999, findings=[]), 2)

    assert ids == ["F999", "F1000"]
    findings = [make_stored_finding(id=i) for i in ids]
    ReviewState.model_validate(state.model_dump() | {"findings": findings})
