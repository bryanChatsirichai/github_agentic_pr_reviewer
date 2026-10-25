"""Unit tests for agents.reviewer_agents.factory (build_reviewer + prompt rendering)."""

import pytest
from langchain_core.messages import AIMessage

from agents.reviewer_agents.factory import build_reviewer, render_pr_context
from models.pr_context import ContextSummary, PRContext


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    """Stub get_llm so tests never need a real provider key or network call."""

    class FakeChatModel:
        def __init__(self):
            self.invoke_calls = []

        def invoke(self, messages):
            self.invoke_calls.append(messages)
            return AIMessage(content="[]")

    instance = FakeChatModel()
    monkeypatch.setattr("agents.reviewer_agents.factory.get_llm", lambda: instance)
    return instance


def make_context() -> PRContext:
    return PRContext(
        pr_number=42,
        title="Add fibonacci CLI",
        body="Compute fib numbers from the command line.",
        author="octocat",
        base_branch="main",
        head_branch="feature/fibonacci",
        base_sha="baseabc",
        head_sha="headdef",
        diff="diff --git a/src/cli.py b/src/cli.py\n--- a/src/cli.py\n+++ b/src/cli.py",
        changed_files=[],
        existing_comments=[],
        previous_findings=[],
        summary=ContextSummary(diff_size=73, file_list=["src/cli.py"]),
    )


def test_build_reviewer_loads_system_prompt_from_md_file(fake_llm) -> None:
    reviewer = build_reviewer("logic-reviewer")

    assert reviewer.name == "logic-reviewer"
    assert reviewer.system_prompt.startswith("# Logic Reviewer")
    assert reviewer.llm is fake_llm


def test_build_reviewer_unknown_name_raises_file_not_found(fake_llm) -> None:
    with pytest.raises(FileNotFoundError):
        build_reviewer("does-not-exist")


def test_render_pr_context_includes_all_sections() -> None:
    rendered = render_pr_context(make_context())

    assert rendered.startswith("# PR #42: Add fibonacci CLI")
    assert "Author: octocat" in rendered
    assert "## PR description" in rendered
    assert "```diff" in rendered
    assert "diff --git a/src/cli.py b/src/cli.py" in rendered


def test_render_pr_context_omits_empty_body_section() -> None:
    context = make_context()
    context.body = ""

    assert "## PR description" not in render_pr_context(context)


def test_review_pr_sends_system_prompt_then_rendered_context(fake_llm) -> None:
    reviewer = build_reviewer("logic-reviewer")

    reviewer.review_pr(make_context())

    [messages] = fake_llm.invoke_calls
    assert messages[0].type == "system"
    assert messages[0].content == reviewer.system_prompt
    assert messages[1].type == "human"
    assert "# PR #42: Add fibonacci CLI" in messages[1].content


def test_logic_reviewer_prompt_pins_json_contract(fake_llm) -> None:
    """The prompt must pin the exact JSON shape the Task-4 parser relies on."""

    prompt = build_reviewer("logic-reviewer").system_prompt

    assert "ONLY a JSON object" in prompt
    assert "overall_rating" in prompt
    for field in ("id", "status", "severity", "file", "line", "title",
                  "description", "confidence"):
        assert f'"{field}"' in prompt
    assert "\"findings\": []" in prompt


class ScriptedFakeChatModel:
    """Returns queued replies in order; records every invoke call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.invoke_calls = []

    def invoke(self, messages):
        self.invoke_calls.append(messages)
        return AIMessage(content=self.replies.pop(0))


def test_review_retries_once_on_malformed_output(monkeypatch) -> None:
    from agents.reviewer_agents.factory import Reviewer

    model = ScriptedFakeChatModel(["not json", "[]"])
    monkeypatch.setattr(
        "agents.reviewer_agents.factory.get_llm", lambda: model
    )
    reviewer = Reviewer(
        name="logic-reviewer",
        system_prompt="prompt",
        llm=model,
    )

    result = reviewer.review(make_context())

    assert result.findings == []
    assert result.overall_rating == "approve"
    # First call + one retry, and the retry saw the bad reply + the error.
    assert len(model.invoke_calls) == 2
    retry_messages = model.invoke_calls[1]
    assert "not json" in retry_messages[2].content
    assert "could not be parsed" in retry_messages[3].content


def test_review_raises_after_second_failure(monkeypatch) -> None:
    from agents.reviewer_agents.factory import Reviewer
    from agents.reviewer_agents.parser import FindingParseError

    model = ScriptedFakeChatModel(["garbage", "still garbage"])
    monkeypatch.setattr(
        "agents.reviewer_agents.factory.get_llm", lambda: model
    )
    reviewer = Reviewer(
        name="logic-reviewer",
        system_prompt="prompt",
        llm=model,
    )

    with pytest.raises(FindingParseError):
        reviewer.review(make_context())
    assert len(model.invoke_calls) == 2


def test_review_logs_token_usage_when_reported(monkeypatch, caplog) -> None:
    from agents.reviewer_agents.factory import Reviewer

    class UsageFakeChatModel:
        def invoke(self, messages):
            return AIMessage(
                content="[]",
                usage_metadata={
                    "input_tokens": 15000,
                    "output_tokens": 12,
                    "total_tokens": 15012,
                },
            )

    model = UsageFakeChatModel()
    monkeypatch.setattr(
        "agents.reviewer_agents.factory.get_llm", lambda: model
    )
    reviewer = Reviewer(
        name="logic-reviewer",
        system_prompt="prompt",
        llm=model,
    )

    import logging

    with caplog.at_level(logging.INFO, logger="agents.reviewer_agents.factory"):
        reviewer.review(make_context())

    assert "input=15000 output=12 tokens" in "".join(
        r.getMessage() for r in caplog.records
    )


def test_review_pr_logs_truncated_prompt_at_debug(fake_llm, caplog) -> None:
    import logging

    reviewer = build_reviewer("logic-reviewer")
    context = make_context()
    context.diff = "x" * 5000

    with caplog.at_level(logging.DEBUG, logger="agents.reviewer_agents.factory"):
        reviewer.review_pr(context)

    log_text = "".join(r.getMessage() for r in caplog.records)
    assert "Sending reviewer prompt (" in log_text
    assert "x" * 100 in log_text  # head shown…
    assert "x" * 2000 not in log_text  # …tail cut
