"""Reviewer factory — binds a prompt file to a chat model.

``build_reviewer(name)`` turns a prompt file in
``agents/reviewer_agents/prompts/`` into a runnable reviewer. No tools
and no conversation history yet — the prompt stays a single turn.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agents.llm import get_llm
from agents.reviewer_agents.parser import FindingParseError, parse_review
from models.finding import Finding
from models.pr_context import PRContext
from models.review import ReviewResult

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class Reviewer:
    """A reviewer agent: one system prompt bound to one chat model.

    ``name`` is the prompt file's stem (e.g. ``"logic-reviewer"``); with
    multiple reviewers it becomes each finding's ``source_reviewer`` tag.
    """

    name: str
    system_prompt: str
    llm: BaseChatModel

    def review_pr(self, context: PRContext) -> AIMessage:
        """One review turn: system prompt + rendered PR context. Stateless."""
        user_message = render_pr_context(context)
        # DEBUG preview of what we actually send: length + head. The tail is
        # just more diff, so truncating keeps the log useful without dumping
        # the full multi-file diff into it.
        logger.debug(
            "Sending reviewer prompt (%d chars):\n%s…",
            len(user_message),
            user_message[:1500],
        )
        return self.llm.invoke(
            [
                SystemMessage(self.system_prompt),
                HumanMessage(user_message),
            ]
        )

    def review(self, context: PRContext) -> ReviewResult:
        """Review a PR and return the verdict: overall rating + findings.

        On malformed output, retries once with the parse error fed back to
        the model; a second failure raises ``FindingParseError`` — a
        fatal review error, reported to the user as a failed run.
        """
        reply = self.review_pr(context)
        # Token counts are the receipt that the context actually reached the
        # model: input_tokens should scale with the diff size (roughly
        # chars/4). A few hundred input tokens means the context was dropped
        # somewhere upstream.
        usage = getattr(reply, "usage_metadata", None) or {}
        if usage:
            logger.info(
                "LLM usage: input=%s output=%s tokens",
                usage.get("input_tokens"),
                usage.get("output_tokens"),
            )
        try:
            return parse_review(reply.content)
        except FindingParseError as exc:
            logger.warning(
                "Reviewer %s produced unparseable output (%s) — retrying once",
                self.name,
                exc,
            )
            retry_reply = self.llm.invoke(
                [
                    SystemMessage(self.system_prompt),
                    HumanMessage(render_pr_context(context)),
                    reply,
                    HumanMessage(
                        f"Your reply could not be parsed as a JSON object with "
                        f"an overall_rating and a findings array: {exc}. "
                        f"Respond again with ONLY the JSON object, no prose "
                        f"and no code fences."
                    ),
                ]
            )
            return parse_review(retry_reply.content)


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
    object the model must reply with, so parsing never has to distinguish
    output JSON from prompt text.
    """
    lines = [
        f"# PR #{context.pr_number}: {context.title}",
        f"Author: {context.author}",
        # Base → head with short SHAs: the agent needs branch/commit context
        # to reference them in its verdict.
        f"Base: {context.base_branch} @ {context.base_sha[:10]}",
        f"Head: {context.head_branch} @ {context.head_sha[:10]}",
        f"Changed files ({len(context.summary.file_list)}): "
        + ", ".join(context.summary.file_list),
    ]
    if context.body.strip():
        lines += ["", "## PR description", context.body.strip()]
    lines += ["", "## Diff", "```diff", context.diff, "```"]
    return "\n".join(lines)
