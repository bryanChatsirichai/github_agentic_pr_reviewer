"""LLM factory — the one place that turns env config into a chat model.

Bare-minimum slice of Step 4 (see docs/implementation-roadmap.md): just
enough to call an LLM from the GitHub Action. No prompts, parsing, or
Finding model yet — those land with the rest of Step 4.
"""

import os

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

#: See docs/llm-providers.md — free tier, no card required, good for PoC dev.
DEFAULT_MODEL = "google_genai:gemini-3.8-flash"

__all__ = ["get_llm"]


def get_llm() -> BaseChatModel:
    """Build the reviewer's chat model from env config.

    ``MODEL_NAME`` picks the provider:model string (e.g. "openai:gpt-5.6-luna"),
    defaulting to the free-tier Gemini model recommended for development.
    ``AI_API_KEY`` is the matching provider's API key. Raises if the key is
    missing so failures surface immediately instead of inside the LLM call.
    """
    api_key = os.environ.get("AI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "AI_API_KEY is not set — add it to .env or the CI secret"
        )
    model = os.environ.get("MODEL_NAME", DEFAULT_MODEL)
    return init_chat_model(model, api_key=api_key)
