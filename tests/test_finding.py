"""Unit tests for models.finding.Finding."""

import pytest
from pydantic import ValidationError

from models.finding import Finding, FindingStatus, Severity


def make_finding(**overrides) -> Finding:
    defaults = dict(
        id="F001",
        status=FindingStatus.OPEN,
        severity=Severity.MEDIUM,
        file="src/JobService.java",
        line=82,
        title="Page size is not validated",
        description="`pageSize` is read from the request without a bound, "
        "allowing callers to request unbounded result sets.",
        confidence=0.87,
    )
    defaults.update(overrides)
    return Finding(**defaults)


def test_finding_round_trips_through_json() -> None:
    finding = make_finding()

    restored = Finding.model_validate_json(finding.model_dump_json())

    assert restored == finding
    assert restored.status is FindingStatus.OPEN
    assert restored.severity is Severity.MEDIUM


def test_finding_accepts_plain_string_enum_values() -> None:
    # Reviewer LLM output arrives as plain JSON strings, not enum members.
    finding = make_finding(status="resolved", severity="high")

    assert finding.status is FindingStatus.RESOLVED
    assert finding.severity is Severity.HIGH


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_finding_rejects_confidence_outside_unit_range(confidence: float) -> None:
    with pytest.raises(ValidationError):
        make_finding(confidence=confidence)


def test_finding_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        make_finding(status="in_progress")


def test_finding_status_lifecycle_members() -> None:
    assert {s.value for s in FindingStatus} == {
        "new",
        "open",
        "resolved",
        "reopened",
        "rejected",
        "verified",
        "closed",
    }
