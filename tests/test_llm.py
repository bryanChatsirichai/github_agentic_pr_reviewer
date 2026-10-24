"""Tests for agents/llm.py env-driven provider resolution.

init_chat_model is stubbed so no network/API key is needed — these tests
cover ONLY the env → (model string, key) resolution, not real provider
clients.
"""

from unittest import mock

import pytest

from agents.llm import get_llm

ALL_KEY_ENVS = [
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AI_API_KEY",
]


@pytest.fixture
def init_spy(monkeypatch):
    """Stub init_chat_model and clear every LLM-related env var."""
    for var in [
        "MODEL_PROVIDER",
        "GOOGLE_MODEL",
        "OPENAI_MODEL",
        "CLAUDE_MODEL",
        "MODEL_NAME",
        *ALL_KEY_ENVS,
    ]:
        monkeypatch.delenv(var, raising=False)
    with mock.patch("agents.llm.init_chat_model") as spy:
        yield spy


def test_google_default(init_spy, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    get_llm()
    init_spy.assert_called_once_with(
        "google_genai:gemini-3.5-flash-lite", api_key="g-key"
    )


def test_openai_with_model_override(init_spy, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "o-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4-mini")
    get_llm()
    init_spy.assert_called_once_with("openai:gpt-5.4-mini", api_key="o-key")


def test_claude_alias_and_case(init_spy, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "Anthropic")  # alias + case-insensitive
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    get_llm()
    init_spy.assert_called_once_with(
        "anthropic:claude-haiku-4.5", api_key="a-key"
    )


def test_claude_model_override(init_spy, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5")
    get_llm()
    init_spy.assert_called_once_with("anthropic:claude-sonnet-5", api_key="a-key")


def test_missing_provider_key(init_spy, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")  # but no OPENAI_API_KEY
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not set"):
        get_llm()


def test_unknown_provider(init_spy, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "deepseek")
    with pytest.raises(RuntimeError, match="not a known provider"):
        get_llm()


def test_legacy_fallback(init_spy, monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "legacy-key")
    monkeypatch.setenv("MODEL_NAME", "openai:gpt-5.6-luna")
    get_llm()
    init_spy.assert_called_once_with("openai:gpt-5.6-luna", api_key="legacy-key")


def test_legacy_default_model(init_spy, monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "legacy-key")
    get_llm()
    init_spy.assert_called_once_with(
        "google_genai:gemini-3.5-flash-lite", api_key="legacy-key"
    )


def test_no_key_at_all(init_spy):
    with pytest.raises(RuntimeError, match="No LLM key found"):
        get_llm()
