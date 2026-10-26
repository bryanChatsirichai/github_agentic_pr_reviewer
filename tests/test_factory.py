"""Unit tests for agents.reviewer_agents.factory.

The chat model is a scripted fake, so no provider key or network is needed.
"""

import pytest
from langchain_core.messages import AIMessage

from agents.reviewer_agents.factory import Reviewer, build_reviewer, render_pr_context
from agents.reviewer_agents.parser import FindingParseError
from models.review import ReviewResult
from tests.helpers import make_context, make_finding


class FakeChatModel:
    """Scripted stand-in for ``llm.with_structured_output(..., include_raw=True)``.

    Each queued reply is either a ``ReviewResult`` (the provider parsed the
    schema) or a string (raw text the structured parser could not use, so
    ``parsed`` is None). Records every invoke call and the schema requested.
    """

    def __init__(self, *replies, parsing_error: Exception | None = None):
        self.replies = list(replies)
        self.parsing_error = parsing_error
        self.calls = []
        self.schemas = []

    def with_structured_output(self, schema, *, include_raw=False):
        assert include_raw, "Reviewer needs the raw reply for fallback + usage"
        self.schemas.append(schema)
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, ReviewResult):
            return {"raw": AIMessage(content=""), "parsed": reply, "parsing_error": None}
        return {"raw": AIMessage(content=reply), "parsed": None, "parsing_error": self.parsing_error}


def make_reviewer(*replies, parsing_error: Exception | None = None) -> Reviewer:
    llm = FakeChatModel(*replies, parsing_error=parsing_error)
    return Reviewer(name="logic-reviewer", system_prompt="prompt", llm=llm)


# --- build_reviewer --------------------------------------------------------


def test_build_reviewer_loads_prompt_file(monkeypatch) -> None:
    fake = FakeChatModel()
    monkeypatch.setattr("agents.reviewer_agents.factory.get_llm", lambda: fake)

    reviewer = build_reviewer("logic-reviewer")

    assert reviewer.system_prompt.startswith("# Logic Reviewer")
    assert reviewer.llm is fake


def test_build_reviewer_unknown_name_raises(monkeypatch) -> None:
    monkeypatch.setattr("agents.reviewer_agents.factory.get_llm", FakeChatModel)
    with pytest.raises(FileNotFoundError):
        build_reviewer("does-not-exist")


def test_logic_reviewer_prompt_pins_json_contract(monkeypatch) -> None:
    # The parser relies on this exact output shape, so the prompt must ask for it.
    monkeypatch.setattr("agents.reviewer_agents.factory.get_llm", FakeChatModel)
    prompt = build_reviewer("logic-reviewer").system_prompt

    assert "ONLY a JSON object" in prompt
    for field in ("overall_rating", "id", "status", "severity", "file", "line",
                  "title", "description", "confidence"):
        assert field in prompt


# --- render_pr_context -----------------------------------------------------


def test_render_pr_context_includes_all_sections() -> None:
    rendered = render_pr_context(make_context())

    assert rendered.startswith("# PR #42: Add fibonacci CLI")
    assert "Author: octocat" in rendered
    assert "## PR description" in rendered
    assert "```diff\ndiff --git a/src/cli.py b/src/cli.py\n```" in rendered


def test_render_pr_context_omits_empty_description() -> None:
    assert "## PR description" not in render_pr_context(make_context(body=""))


# --- review ----------------------------------------------------------------


def test_review_sends_system_prompt_then_context() -> None:
    reviewer = make_reviewer(ReviewResult(overall_rating="approve"))

    result = reviewer.review(make_context())

    assert result.overall_rating == "approve"
    assert reviewer.llm.schemas == [ReviewResult]
    [messages] = reviewer.llm.calls
    assert [m.type for m in messages] == ["system", "human"]
    assert messages[1].content.startswith("# PR #42")


def test_review_returns_structured_output_as_is() -> None:
    expected = ReviewResult(overall_rating="comment", findings=[make_finding()])
    reviewer = make_reviewer(expected)

    assert reviewer.review(make_context()) is expected
    assert len(reviewer.llm.calls) == 1


def test_review_falls_back_to_text_repair() -> None:
    # e.g. the model answered in text instead of the forced tool call.
    reviewer = make_reviewer('```json\n{"overall_rating": "approve", "findings": []}\n```')

    assert reviewer.review(make_context()).overall_rating == "approve"
    assert len(reviewer.llm.calls) == 1


def test_review_retries_once_with_parse_error() -> None:
    reviewer = make_reviewer("not json", "[]")

    assert reviewer.review(make_context()).findings == []
    # The retry describes the failure in a new human turn; the bad reply is
    # not replayed (a tool_use turn without tool_result breaks Anthropic).
    retry = reviewer.llm.calls[1]
    assert [m.type for m in retry] == ["system", "human", "human"]
    assert "could not be parsed" in retry[2].content


def test_review_retry_prefers_structured_parsing_error() -> None:
    reviewer = make_reviewer(
        "", ReviewResult(overall_rating="approve"),
        parsing_error=ValueError("findings.0.confidence: less than or equal to 1"),
    )

    reviewer.review(make_context())

    assert "confidence: less than or equal to 1" in reviewer.llm.calls[1][2].content


def test_review_raises_after_second_failure() -> None:
    reviewer = make_reviewer("garbage", "still garbage")

    with pytest.raises(FindingParseError):
        reviewer.review(make_context())
    assert len(reviewer.llm.calls) == 2
