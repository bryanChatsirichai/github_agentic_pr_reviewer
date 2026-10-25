"""Unit tests for models.finding.Finding validation."""

import pytest
from pydantic import ValidationError

from models.finding import Finding, FindingStatus, Severity
from tests.helpers import make_finding


def test_finding_round_trips_through_json() -> None:
    finding = make_finding()
    assert Finding.model_validate_json(finding.model_dump_json()) == finding


def test_finding_accepts_plain_string_enums() -> None:
    # LLM output arrives as plain JSON strings, not enum members.
    finding = make_finding(status="resolved", severity="high")

    assert finding.status is FindingStatus.RESOLVED
    assert finding.severity is Severity.HIGH


@pytest.mark.parametrize(
    "overrides",
    [
        {"confidence": -0.01},  # below [0, 1]
        {"confidence": 1.01},  # above [0, 1]
        {"status": "in_progress"},  # not a lifecycle status
        {"severity": "urgent"},  # not a severity
    ],
)
def test_finding_rejects_invalid_values(overrides) -> None:
    with pytest.raises(ValidationError):
        make_finding(**overrides)
