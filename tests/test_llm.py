"""Tests for agents.llm env-driven provider resolution.

init_chat_model is stubbed, so these cover only env → (model string, key)
resolution — no network, no real provider clients.
"""

from unittest import mock

import pytest

from agents.llm import get_llm


@pytest.fixture
def init_spy(monkeypatch):
    """Clear every LLM env var and stub init_chat_model."""
    for var in [
        "MODEL_PROVIDER",
        "GOOGLE_MODEL",
        "OPENAI_MODEL",
        "CLAUDE_MODEL",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ]:
        monkeypatch.delenv(var, raising=False)
    with mock.patch("agents.llm.init_chat_model") as spy:
        yield spy


@pytest.mark.parametrize(
    "provider, env, expected_model",
    [
        # Default model when no *_MODEL override is set.
        ("google", {"GOOGLE_API_KEY": "k"}, "google_genai:gemini-3.5-flash-lite"),
        # Provider name is case-insensitive.
        ("Google", {"GOOGLE_API_KEY": "k"}, "google_genai:gemini-3.5-flash-lite"),
        # *_MODEL overrides the default, for each provider.
        ("google", {"GOOGLE_API_KEY": "k", "GOOGLE_MODEL": "gemini-x"}, "google_genai:gemini-x"),
        ("openai", {"OPENAI_API_KEY": "k", "OPENAI_MODEL": "gpt-x"}, "openai:gpt-x"),
        ("claude", {"ANTHROPIC_API_KEY": "k", "CLAUDE_MODEL": "claude-x"}, "anthropic:claude-x"),
    ],
)
def test_resolves_model_and_key(init_spy, monkeypatch, provider, env, expected_model):
    monkeypatch.setenv("MODEL_PROVIDER", provider)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    get_llm()

    init_spy.assert_called_once_with(expected_model, api_key="k")


@pytest.mark.parametrize(
    "provider, error",
    [
        (None, "MODEL_PROVIDER is not set"),
        ("deepseek", "not a known provider"),
        ("openai", "OPENAI_API_KEY is not set"),  # provider set, key missing
    ],
)
def test_config_errors_raise(init_spy, monkeypatch, provider, error):
    if provider:
        monkeypatch.setenv("MODEL_PROVIDER", provider)
    with pytest.raises(RuntimeError, match=error):
        get_llm()
