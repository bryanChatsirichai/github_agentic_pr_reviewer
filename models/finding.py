"""Finding - the contract every later step (memory, re-check, judge) builds on.

A ``Finding`` is one reviewer-reported issue in a PR. Its ``status`` moves it
through the lifecycle of design section 11 (``new -> open -> resolved/rejected/
reopened -> verified -> closed``); ``orchestrator/memory.py`` (Step 6) persists
that lifecycle and moves findings across commits.

Field ``description``s below are not just documentation: they flow into the
JSON schema that ``with_structured_output`` sends to the reviewer LLM, so they
double as prompt guidance for producing well-formed findings.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class FindingStatus(StrEnum):
    """Finding lifecycle (design section 11).

    ``new`` is transient — it becomes ``open`` once the orchestrator files
    the finding into memory. Transitions (e.g. ``resolved -> reopened`` on
    regression) are enforced in ``orchestrator/memory.py``, not here.
    """

    NEW = "new"
    OPEN = "open"
    RESOLVED = "resolved"
    REOPENED = "reopened"
    REJECTED = "rejected"
    VERIFIED = "verified"
    CLOSED = "closed"


class Severity(StrEnum):
    """How bad the issue would be if real, independent of reviewer certainty."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Finding(BaseModel):
    """One issue reported by a reviewer agent for a specific file/line.

    ``severity`` (how bad if real) and ``confidence`` (how sure the reviewer
    is) are independent axes — keep them separate when judging or filtering.
    """

    id: str = Field(
        description=(
            "Sequence ``F001``, ``F002``, ... in report order. The orchestrator "
            "may renumber it so the ID stays stable across commits."
        )
    )
    status: FindingStatus = Field(
        description="Lifecycle state; starts as ``new`` for fresh findings."
    )
    severity: Severity = Field(
        description="How bad the issue is: low | medium | high | critical."
    )
    file: str = Field(
        description="Path relative to the repo root, matching ``PRContext.changed_files``."
    )
    line: int = Field(description="1-based line number in ``file`` the issue refers to.")
    title: str = Field(description="One-line summary of the issue.")
    description: str = Field(
        description="What the issue is, why it matters, and how to fix it."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Reviewer's certainty that this is a real issue, in [0.0, 1.0].",
    )