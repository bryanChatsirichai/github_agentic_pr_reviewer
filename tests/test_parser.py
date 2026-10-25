"""Unit tests for agents.reviewer_agents.parser.parse_review."""

import json

import pytest

from agents.reviewer_agents.parser import FindingParseError, parse_review

FINDING = {
    "id": "F001",
    "status": "new",
    "severity": "medium",
    "file": "src/JobService.java",
    "line": 82,
    "title": "Page size is not validated",
    "description": "pageSize is read without an upper bound.",
    "confidence": 0.87,
}


def reply(rating="comment", findings=(FINDING,)) -> str:
    """A full-contract reply: {"overall_rating", "findings"}."""
    return json.dumps({"overall_rating": rating, "findings": list(findings)})


def test_parse_full_contract_reply() -> None:
    result = parse_review(reply("request_changes"))

    assert result.overall_rating == "request_changes"
    assert result.findings[0].id == "F001"


@pytest.mark.parametrize(
    "raw, expected_rating",
    [
        # Bare findings array (old contract): rating derived from severities.
        ("[]", "approve"),
        (json.dumps([dict(FINDING, severity="low")]), "comment"),
        (json.dumps([dict(FINDING, severity="high")]), "request_changes"),
        # Object without overall_rating: derived the same way.
        (json.dumps({"findings": [FINDING]}), "comment"),
    ],
)
def test_parse_derives_missing_rating(raw, expected_rating) -> None:
    assert parse_review(raw).overall_rating == expected_rating


@pytest.mark.parametrize(
    "raw",
    [
        f"```json\n{reply()}\n```",  # markdown code fence
        f"Here are my findings:\n{reply()}\nHope this helps.",  # surrounding prose
        # Gemini sometimes returns content as a list of blocks; non-text
        # blocks are dropped and text blocks joined, even mid-token.
        [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": reply()}],
        [reply()[:10], reply()[10:]],
    ],
)
def test_parse_repairs_common_llm_quirks(raw) -> None:
    assert len(parse_review(raw).findings) == 1


@pytest.mark.parametrize(
    "raw, error",
    [
        ("[not json at all", "invalid JSON"),
        ("I found no issues, the code looks good.", "invalid JSON"),  # no payload at all
        (reply("excellent"), "overall_rating must be one of"),
        ('{"overall_rating": "approve"}', "missing required key: findings"),
        (reply(findings=[dict(FINDING, confidence=1.5)]), "failed validation"),
    ],
)
def test_parse_rejects_bad_replies(raw, error) -> None:
    with pytest.raises(FindingParseError, match=error):
        parse_review(raw)
