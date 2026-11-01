"""One structured LLM question, retried once — shared by single-answer agents.

The re-check agent and the explanation judge each ask one question and
expect one small JSON object back. The reviewer keeps its own loop
(``agents/reviewer_agents/factory.py``) because it also repairs raw text.
"""

import logging
from typing import TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

logger = logging.getLogger("pr-reviewer.structured")

__all__ = ["ask_structured"]

T = TypeVar("T", bound=BaseModel)


def ask_structured(
    llm: BaseChatModel, system_prompt: str, user_message: str, schema: type[T]
) -> T | None:
    """Ask once for a ``schema`` object; None when no usable answer came back.

    The schema is enforced by the provider via ``with_structured_output``.
    An unusable reply is retried once with the error fed back; a second
    failure is logged and returns None, so the caller decides what "no
    answer" means instead of the run failing.
    """
    messages = [SystemMessage(system_prompt), HumanMessage(user_message)]
    answer, error = _invoke(llm, messages, schema)
    if answer is None:
        retry = HumanMessage(
            f"Your previous reply could not be parsed as a JSON object with "
            f"{', '.join(schema.model_fields)}: {error}. Respond again with "
            f"ONLY the JSON object."
        )
        answer, error = _invoke(llm, messages + [retry], schema)
    if answer is None:
        logger.warning("%s output unusable twice (%s)", schema.__name__, error)
    return answer


def _invoke(
    llm: BaseChatModel, messages: list[BaseMessage], schema: type[T]
) -> tuple[T | None, str]:
    """One structured LLM call. Returns ``(answer, "")`` or ``(None, error)``."""
    output = llm.with_structured_output(schema, include_raw=True).invoke(messages)
    if output["parsed"] is not None:
        return output["parsed"], ""
    return None, str(output["parsing_error"] or "no structured output")
