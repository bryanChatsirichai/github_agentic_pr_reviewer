"""Parse reviewer LLM output into a ``ReviewResult``.

The prompt pins a JSON-object contract (``{"overall_rating": ...,
"findings": [...]}``), but models drift: they wrap output in markdown
fences, add prose around it, emit schema-invalid fields, or fall back to
a bare findings array. ``parse_review`` repairs the cosmetic cases,
validates the rest strictly against the ``Finding`` model, and raises
``FindingParseError`` for anything it can't fix — the caller
(``Reviewer.review``) then uses that signal to retry once.
"""

import json

from pydantic import ValidationError

from models.finding import Finding
from models.review import RATINGS, ReviewResult, derived_rating

__all__ = ["FindingParseError", "parse_review"]


class FindingParseError(Exception):
    """Reviewer output could not be parsed into a ReviewResult."""


def parse_review(raw: str | list) -> ReviewResult:
    """Parse raw model output into a ``ReviewResult``.

    Repairs common LLM quirks before strict validation: markdown code
    fences around the JSON, prose before/after the payload, and LangChain
    returning ``AIMessage.content`` as a list of content blocks (Gemini
    does this intermittently) instead of a string. A bare findings array
    is accepted as a degraded reply — the rating is then derived from the
    finding severities (see ``models.review.derived_rating``). Anything
    else raises ``FindingParseError`` with the reason.
    """
    text = _extract_json_payload(_strip_code_fences(_content_to_text(raw)))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FindingParseError(f"invalid JSON: {exc.msg} at pos {exc.pos}") from exc

    if isinstance(data, list):
        # Bare array: old contract without the overall rating.
        return ReviewResult(overall_rating=derived_rating(_validate(data)), findings=_validate(data))
    if not isinstance(data, dict):
        raise FindingParseError(
            f"expected a JSON object with overall_rating and findings, got {type(data).__name__}"
        )

    rating = data.get("overall_rating")
    if rating is not None and rating not in RATINGS:
        raise FindingParseError(
            f"overall_rating must be one of {RATINGS}, got {rating!r}"
        )
    if "findings" not in data:
        raise FindingParseError("missing required key: findings")
    findings = _validate(data["findings"])
    if rating is None:
        rating = derived_rating(findings)
    return ReviewResult(overall_rating=rating, findings=findings)


def _validate(data: object) -> list[Finding]:
    """Validate a findings array, raising FindingParseError on any issue."""
    if not isinstance(data, list):
        raise FindingParseError(
            f"findings must be a JSON array, got {type(data).__name__}"
        )
    try:
        return [Finding.model_validate(item) for item in data]
    except ValidationError as exc:
        first = exc.errors()[0]
        raise FindingParseError(
            f"finding {first.get('loc')} failed validation: {first['msg']}"
        ) from exc


def _content_to_text(raw: str | list) -> str:
    """Flatten ``AIMessage.content`` into a plain string.

    LangChain models return ``content`` as either a string or a list of
    content blocks (``{"type": "text", "text": ...}`` dicts or bare
    strings); Gemini returns the list form intermittently. Non-text
    blocks (thinking, tool calls) are dropped.
    """
    if isinstance(raw, str):
        return raw
    parts: list[str] = []
    for block in raw:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _strip_code_fences(text: str) -> str:
    """Drop a ``` / ```json fence wrapper if the whole reply is fenced."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    # Last line may or may not be a closing fence — slice accordingly.
    if lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines[1:])


def _extract_json_payload(text: str) -> str:
    """Slice out the JSON payload (object or array), dropping surrounding prose.

    Slices both candidates (first ``{`` to last ``}``, first ``[`` to last
    ``]``) and returns the first that is valid JSON, trying whichever opens
    first before the other — so prose like "I found [one issue]." before
    the object doesn't hijack the slice. If neither parses, the
    first-opening slice is returned so the caller reports its JSON error.
    """
    obj_start, arr_start = text.find("{"), text.find("[")
    obj = text[obj_start : text.rfind("}") + 1] if obj_start != -1 else ""
    arr = text[arr_start : text.rfind("]") + 1] if arr_start != -1 else ""
    if obj_start != -1 and (arr_start == -1 or obj_start < arr_start):
        candidates = (obj, arr)
    else:
        candidates = (arr, obj)
    for candidate in candidates:
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return candidate
    return candidates[0] or text
