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
]


@pytest.fixture
def init_spy(monkeypatch):
    """Stub init_chat_model and clear every LLM-related env var."""
    for var in [
        "MODEL_PROVIDER",
        "GOOGLE_MODEL",
        "OPENAI_MODEL",
        "CLAUDE_MODEL",
        *ALL_KEY_ENVS,
    ]:
        monkeypatch.delenv(var, raising=False)
    with mock.patch("agents.llm.init_chat_model") as spy:
        yield spy


def test_missing_provider(init_spy):
    with pytest.raises(RuntimeError, match="MODEL_PROVIDER is not set"):
        get_llm()


def test_unknown_provider(init_spy, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "deepseek")
    with pytest.raises(RuntimeError, match="not a known provider"):
        get_llm()


def test_google_with_model_override(init_spy, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    monkeypatch.setenv("GOOGLE_MODEL", "gemini-3.8-flash")
    get_llm()
    init_spy.assert_called_once_with("google_genai:gemini-3.8-flash", api_key="g-key")


def test_google_default_model(init_spy, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    get_llm()
    init_spy.assert_called_once_with(
        "google_genai:gemini-3.5-flash-lite", api_key="g-key"
    )


def test_provider_case_insensitive(init_spy, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "Google")
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


def test_claude_with_model_override(init_spy, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5")
    get_llm()
    init_spy.assert_called_once_with("anthropic:claude-sonnet-5", api_key="a-key")


def test_missing_provider_key(init_spy, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")  # no OPENAI_API_KEY set
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not set"):
        get_llm()
