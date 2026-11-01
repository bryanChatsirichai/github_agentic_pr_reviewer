"""RecheckVerdict — the re-check LLM's answer about one previous finding.

Also the schema passed to ``with_structured_output``, so the field
``description``s are sent to the LLM as guidance.

The LLM answers a yes/no question — is the defect still in the code? — not
a status. The status move follows from that answer plus the finding's
current status (Step 9 Task 3), so the LLM cannot ask for a move the
lifecycle table forbids (e.g. ``open → reopened``).
"""

from pydantic import BaseModel, ConfigDict, Field


class RecheckVerdict(BaseModel):
    """Whether a previously reported defect is still present at head."""

    model_config = ConfigDict(frozen=True)

    issue_present: bool = Field(
        description=(
            "True if the defect described by the finding is still in the "
            "current code (even if moved or reformatted); false if it was "
            "fixed or the code containing it was removed."
        )
    )
    reason: str = Field(
        description="One sentence pointing at the code that shows the answer."
    )
