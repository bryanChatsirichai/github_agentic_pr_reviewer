"""Parse reviewer LLM output into ``list[Finding]``.

The prompt pins a JSON-array contract, but models drift: they wrap output
in markdown fences, add prose around it, or emit schema-invalid fields.
``parse_findings`` repairs the cosmetic cases, validates the rest strictly
against the ``Finding`` model, and raises ``FindingParseError`` for
anything it can't fix — the caller (``Reviewer.review_findings``) then
uses that signal to retry once.
"""

import json

from pydantic import ValidationError

from models.finding import Finding

__all__ = ["FindingParseError", "parse_findings"]


class FindingParseError(Exception):
    """Reviewer output could not be parsed into a list of Findings."""


def parse_findings(raw: str | list) -> list[Finding]:
    """Parse raw model output into ``list[Finding]``.

    Repairs common LLM quirks before strict validation: markdown code
    fences around the JSON, prose before/after the array, and LangChain
    returning ``AIMessage.content`` as a list of content blocks (Gemini
    does this intermittently) instead of a string. Anything else that
    isn't a JSON array of Finding-shaped objects raises
    ``FindingParseError`` with the reason.
    """
    text = _extract_json_array(_strip_code_fences(_content_to_text(raw)))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FindingParseError(f"invalid JSON: {exc.msg} at pos {exc.pos}") from exc
    if not isinstance(data, list):
        raise FindingParseError(
            f"expected a JSON array of findings, got {type(data).__name__}"
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


def _extract_json_array(text: str) -> str:
    """Slice from the first ``[`` to the last ``]`` to drop surrounding prose.

    Safe for brackets inside strings: the array's own ``[``/``]`` are
    always the outermost ones in a reply whose payload is a JSON array.
    """
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text
