"""Unit tests for agents.reviewer_agents.factory.

The chat model is a scripted fake, so no provider key or network is needed.
"""

import pytest
from langchain_core.messages import AIMessage

from agents.reviewer_agents.factory import Reviewer, build_reviewer, render_pr_context
from agents.reviewer_agents.parser import FindingParseError
from tests.helpers import make_context


class FakeChatModel:
    """Returns the queued replies in order and records every invoke call."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return AIMessage(content=self.replies.pop(0))


def make_reviewer(*replies: str) -> Reviewer:
    return Reviewer(name="logic-reviewer", system_prompt="prompt", llm=FakeChatModel(*replies))


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
    reviewer = make_reviewer('{"overall_rating": "approve", "findings": []}')

    result = reviewer.review(make_context())

    assert result.overall_rating == "approve"
    [messages] = reviewer.llm.calls
    assert [m.type for m in messages] == ["system", "human"]
    assert messages[1].content.startswith("# PR #42")


def test_review_retries_once_with_parse_error() -> None:
    reviewer = make_reviewer("not json", "[]")

    assert reviewer.review(make_context()).findings == []
    # The retry replays the bad reply plus the error so the model can fix it.
    retry = reviewer.llm.calls[1]
    assert retry[2].content == "not json"
    assert "could not be parsed" in retry[3].content


def test_review_raises_after_second_failure() -> None:
    reviewer = make_reviewer("garbage", "still garbage")

    with pytest.raises(FindingParseError):
        reviewer.review(make_context())
    assert len(reviewer.llm.calls) == 2
