"""LLM factory — the one place that turns env config into a chat model.

Bare-minimum slice of Step 4 (see docs/implementation-roadmap.md): just
enough to call an LLM from the GitHub Action. No prompts, parsing, or
Finding model yet — those land with the rest of Step 4.
"""

import os

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

#: Per-provider env wiring. MODEL_PROVIDER selects the entry; the entry's
#: key_env/model_env are then read (model falls back to default_model, chosen
#: from docs/llm-providers.md). prefix is the langchain provider id used to
#: build the "provider:model" string for init_chat_model.
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

#: Alternate spellings accepted for MODEL_PROVIDER (case-insensitive anyway).
ALIASES = {
    "anthropic": "claude",
    "google_genai": "google",
    "gemini": "google",
    "open_ai": "openai",
}

__all__ = ["get_llm"]


def get_llm() -> BaseChatModel:
    """Build the reviewer's chat model from env config.

    ``MODEL_PROVIDER`` picks the provider: ``google`` | ``openai`` |
    ``claude`` (``anthropic`` accepted as an alias). The matching
    ``<PROVIDER>_API_KEY`` env var is required; ``<PROVIDER>_MODEL``
    (``GOOGLE_MODEL`` / ``OPENAI_MODEL`` / ``CLAUDE_MODEL``) overrides the
    provider's default model. Switching providers means changing one value —
    keep all three keys configured and flip ``MODEL_PROVIDER``.

    Legacy fallback (pre-provider config): when ``MODEL_PROVIDER`` is unset,
    ``MODEL_NAME`` ("provider:model" string) + ``AI_API_KEY`` are used, so
    existing CI secrets keep working.

    Raises if the required key is missing so failures surface immediately
    instead of inside the LLM call.
    """
    provider_raw = os.environ.get("MODEL_PROVIDER", "").strip().lower()
    if provider_raw:
        provider = ALIASES.get(provider_raw, provider_raw)
        if provider not in PROVIDERS:
            valid = ", ".join(sorted(PROVIDERS))
            raise RuntimeError(
                f"MODEL_PROVIDER={provider_raw!r} is not a known provider — "
                f"expected one of: {valid} (aliases: anthropic→claude)"
            )
        spec = PROVIDERS[provider]
        api_key = os.environ.get(spec["key_env"])
        if not api_key:
            raise RuntimeError(
                f"{spec['key_env']} is not set — add it to .env or the CI "
                f"secret (selected via MODEL_PROVIDER={provider_raw})"
            )
        model = os.environ.get(spec["model_env"]) or spec["default_model"]
        return init_chat_model(f"{spec['prefix']}:{model}", api_key=api_key)

    # Legacy: MODEL_NAME + AI_API_KEY (see git history / older CI setups).
    api_key = os.environ.get("AI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No LLM key found — set MODEL_PROVIDER plus the matching "
            "<PROVIDER>_API_KEY (e.g. GOOGLE_API_KEY), or the legacy "
            "AI_API_KEY pair. Add it to .env or the CI secret"
        )
    google_spec = PROVIDERS["google"]
    model = os.environ.get("MODEL_NAME") or (
        f"{google_spec['prefix']}:{google_spec['default_model']}"
    )
    return init_chat_model(model, api_key=api_key)
