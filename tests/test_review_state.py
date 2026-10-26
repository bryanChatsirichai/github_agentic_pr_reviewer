"""Unit tests for models.review_state — the per-PR state hidden in bot comments.

The state block is parsed from a PR comment, so it is untrusted input:
these tests pin down what validation must reject.
"""

import pytest
from pydantic import ValidationError

from tests.helpers import make_review_state, make_stored_finding


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "new"},  # transient — never stored
        {"status": "rejected"},  # rejected without rejected_by
        {"status": "open", "rejected_by": "critic"},  # rejected_by on non-rejected
        {"status": "rejected", "rejected_by": "bot"},  # unknown rejecter
        {"description": "x" * 301},  # over the 300-char cap
        {"line": 0},  # lines are 1-based
        {"id": "X001"},  # not an F-number
        {"confidence": 1.01},  # outside [0, 1]
        {"source": "llm"},  # unknown field
    ],
)
def test_stored_finding_rejects_invalid_fields(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        make_stored_finding(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},  # unknown version — needs migration, not a guess
        {"next_id": 0},  # IDs start at F001
        {"unknown": "field"},
    ],
)
def test_state_rejects_invalid_fields(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        make_review_state(**overrides)


def test_state_rejects_duplicate_finding_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        make_review_state(
            next_id=3,
            findings=[make_stored_finding(id="F001"), make_stored_finding(id="F001")],
        )


def test_state_rejects_finding_id_at_or_above_next_id() -> None:
    # next_id=2 means F001 is the last issued ID; a stored F002 would be
    # handed out again as a "new" finding.
    with pytest.raises(ValidationError, match="next_id"):
        make_review_state(next_id=2, findings=[make_stored_finding(id="F002")])
