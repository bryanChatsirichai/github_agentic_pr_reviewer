"""Reviewer factory — binds a prompt file to a chat model.

``build_reviewer(name)`` turns a prompt file in
``agents/reviewer_agents/prompts/`` into a runnable reviewer. No tools yet
(Step 11 adds them); no conversation history either (Step 7 instead grows
the prompt with a "previous findings" section).
"""

from dataclasses import dataclass
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agents.llm import get_llm
from models.pr_context import PRContext

PROMPTS_DIR = Path(__file__).parent / "prompts"


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

    def review_pr(self, context: PRContext) -> AIMessage:
        """One review turn: system prompt + rendered PR context. Stateless."""
        return self.llm.invoke(
            [
                SystemMessage(self.system_prompt),
                HumanMessage(render_pr_context(context)),
            ]
        )


def build_reviewer(name: str) -> Reviewer:
    """Build a reviewer from ``agents/reviewer_agents/prompts/<name>.md`` + ``get_llm()``.

    Raises FileNotFoundError if no prompt file exists for ``name``.
    """
    prompt_path = PROMPTS_DIR / f"{name}.md"
    return Reviewer(
        name=name,
        system_prompt=prompt_path.read_text().strip(),
        llm=get_llm(),
    )


def render_pr_context(context: PRContext) -> str:
    """Render a PRContext into the reviewer's user message.

    Plain text on purpose: the only JSON in the exchange is the findings
    array the model must reply with, so parsing (Task 4) never has to
    distinguish output JSON from prompt text.
    """
    lines = [
        f"# PR #{context.pr_number}: {context.title}",
        f"Author: {context.author}",
    ]
    if context.body.strip():
        lines += ["", "## PR description", context.body.strip()]
    lines += ["", "## Diff", "```diff", context.diff, "```"]
    return "\n".join(lines)
