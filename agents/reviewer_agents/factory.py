"""Reviewer factory — binds a prompt file to a chat model.

``build_reviewer(name)`` is the one place that turns a prompt file in
``agents/reviewer_agents/prompts/`` into a runnable reviewer. No tools are
bound yet (Step 11 adds them); no conversation history either (Step 7
instead grows the prompt itself with a "previous findings" section).
"""

from dataclasses import dataclass
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agents.llm import get_llm

PROMPTS_DIR = Path(__file__).parent / "prompts"

__all__ = ["Reviewer", "build_reviewer"]

# Object Immutable
@dataclass(frozen=True)
class Reviewer:
    """A reviewer agent: one system prompt bound to one chat model.

    ``name`` is the prompt file's stem (e.g. ``"logic-reviewer"``); once
    Step 14 adds more reviewers, it also becomes each finding's
    ``source_reviewer`` tag.
    """

    name: str
    system_prompt: str
    llm: BaseChatModel

    def review(self, user_message: str) -> AIMessage:
        """Run one reviewer turn: system prompt + the given PR context.

        Stateless — each call is independent, with no memory of prior
        calls baked into the model itself.
        """
        return self.llm.invoke(
            [SystemMessage(self.system_prompt), HumanMessage(user_message)]
        )


def build_reviewer(name: str) -> Reviewer:
    """Build a reviewer from ``agents/reviewer_agents/prompts/<name>.md`` + ``get_llm()``.

    Raises FileNotFoundError if no prompt file exists for ``name``.
    """
    prompt_path = PROMPTS_DIR / f"{name}.md"
    system_prompt = prompt_path.read_text().strip()
    return Reviewer(name=name, system_prompt=system_prompt, llm=get_llm())
