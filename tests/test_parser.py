"""Unit tests for agents.reviewer_agents.parser.parse_review."""

import pytest

from agents.reviewer_agents.parser import FindingParseError, parse_review

VALID_FINDING = {
    "id": "F001",
    "status": "new",
    "severity": "medium",
    "file": "src/JobService.java",
    "line": 82,
    "title": "Page size is not validated",
    "description": "pageSize is read without an upper bound.",
    "confidence": 0.87,
}


def obj(rating: str | None = None, findings: list | None = None) -> str:
    """Build a full-contract reply body: {"overall_rating", "findings"}."""
    payload: dict = {"findings": VALID if findings is None else findings}
    if rating is not None:
        payload = {"overall_rating": rating, **payload}
    import json

    return json.dumps(payload)


VALID = [VALID_FINDING]


def test_parse_clean_object_with_rating() -> None:
    result = parse_review(obj("request_changes"))

    assert result.overall_rating == "request_changes"
    assert len(result.findings) == 1
    assert result.findings[0].id == "F001"
    assert result.findings[0].confidence == 0.87


def test_parse_empty_findings_approve() -> None:
    result = parse_review(obj("approve", []))

    assert result.overall_rating == "approve"
    assert result.findings == []


def test_parse_bare_array_is_accepted_with_derived_rating() -> None:
    # Old contract without overall_rating: rating derives from severities.
    result = parse_review('[{"id": "F001", "status": "new", "severity": '
        '"high", "file": "a.py", "line": 1, "title": "t", "description": '
        '"d", "confidence": 0.9}]')

    assert result.overall_rating == "request_changes"
    assert len(result.findings) == 1

    clean = parse_review("[]")
    assert clean.overall_rating == "approve"


def test_parse_bare_array_low_severity_derives_comment() -> None:
    result = parse_review('[{"id": "F001", "status": "new", "severity": '
        '"low", "file": "a.py", "line": 1, "title": "t", "description": '
        '"d", "confidence": 0.4}]')

    assert result.overall_rating == "comment"


def test_parse_missing_rating_field_derives_it() -> None:
    result = parse_review(obj(None, [dict(VALID_FINDING, severity="low")]))

    assert result.overall_rating == "comment"


def test_parse_repairs_code_fences() -> None:
    raw = f"```json\n{obj('comment')}\n```"

    assert len(parse_review(raw).findings) == 1


def test_parse_repairs_surrounding_prose() -> None:
    raw = f"Here are my findings:\n{obj('comment')}\nLet me know if you need more detail."

    assert len(parse_review(raw).findings) == 1


def test_parse_rejects_unknown_rating() -> None:
    with pytest.raises(FindingParseError, match="overall_rating must be one of"):
        parse_review(obj("excellent"))


def test_parse_rejects_missing_findings_key() -> None:
    with pytest.raises(FindingParseError, match="missing required key: findings"):
        parse_review('{"overall_rating": "approve"}')


def test_parse_rejects_invalid_json() -> None:
    with pytest.raises(FindingParseError, match="invalid JSON"):
        parse_review("[not json at all")


def test_parse_rejects_schema_violations() -> None:
    bad = dict(VALID_FINDING, confidence=1.5)  # outside [0.0, 1.0]

    with pytest.raises(FindingParseError, match="failed validation"):
        parse_review(obj("comment", [bad]))


def test_parse_rejects_text_without_any_payload() -> None:
    with pytest.raises(FindingParseError):
        parse_review("I found no issues, the code looks good.")


def test_parse_accepts_content_block_list() -> None:
    # Gemini intermittently returns AIMessage.content as a list of
    # content blocks instead of a string — the parser must flatten it.
    raw = [
        {"type": "text", "text": "Here are my findings:\n"},
        {"type": "text", "text": obj("comment")},
    ]

    assert len(parse_review(raw).findings) == 1


def test_parse_content_block_list_drops_non_text_blocks() -> None:
    raw = [
        {"type": "thinking", "thinking": "internal reasoning"},
        {"type": "text", "text": obj("approve", [])},
    ]

    assert parse_review(raw).findings == []


def test_parse_accepts_list_of_bare_strings() -> None:
    raw = ['{"overall_ratin', 'g": "approve", "findings": []}']

    assert parse_review(raw).findings == []
