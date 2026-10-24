"""LLM factory — the one place that turns env config into a chat model."""

import os

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

#: Per-provider env wiring. MODEL_PROVIDER picks the entry; prefix is the
#: langchain provider id for init_chat_model's "provider:model" string.
#: Defaults come from docs/llm-providers.md.
PROVIDERS: dict[str, dict[str, str]] = {
    "google": {
        "prefix": "google_genai",
        "key_env": "GOOGLE_API_KEY",
        "model_env": "GOOGLE_MODEL",
        "default_model": "gemini-3.5-flash-lite",  # free tier, PoC dev speed
    },
    "openai": {
        "prefix": "openai",
        "key_env": "OPENAI_API_KEY",
        "model_env": "OPENAI_MODEL",
        "default_model": "gpt-5.6-luna",  # cheapest paid, docs/llm-providers.md
    },
    "claude": {
        "prefix": "anthropic",
        "key_env": "ANTHROPIC_API_KEY",
        "model_env": "CLAUDE_MODEL",
        "default_model": "claude-haiku-4.5",  # safest tool-calling
    },
}

__all__ = ["get_llm"]


def get_llm() -> BaseChatModel:
    """Build the reviewer's chat model from env config.

    ``MODEL_PROVIDER`` (required, case-insensitive) picks the provider:
    ``google`` | ``openai`` | ``claude``. The matching ``<PROVIDER>_API_KEY``
    env var is required; ``<PROVIDER>_MODEL`` (``GOOGLE_MODEL`` /
    ``OPENAI_MODEL`` / ``CLAUDE_MODEL``) overrides the provider's default
    model. Switching providers means changing one value — flip
    ``MODEL_PROVIDER``.

    Raises RuntimeError (missing provider, unknown provider, or missing key)
    so failures surface immediately; orchestrator.main logs it and exits 1.
    """
    provider = os.environ.get("MODEL_PROVIDER", "").strip()
    if not provider:
        raise RuntimeError(
            "MODEL_PROVIDER is not set — set it in .env (local) or as the "
            "repo variable / model_provider input (CI). Valid values: "
            + ", ".join(sorted(PROVIDERS))
        )
    provider = provider.lower()
    if provider not in PROVIDERS:
        raise RuntimeError(
            f"MODEL_PROVIDER={provider!r} is not a known provider — "
            f"expected one of: {', '.join(sorted(PROVIDERS))}"
        )
    spec = PROVIDERS[provider]
    api_key = os.environ.get(spec["key_env"])
    if not api_key:
        raise RuntimeError(
            f"{spec['key_env']} is not set — add it to .env or the CI "
            f"secret (selected via MODEL_PROVIDER={provider})"
        )
    model = os.environ.get(spec["model_env"]) or spec["default_model"]
    return init_chat_model(f"{spec['prefix']}:{model}", api_key=api_key)
