"""ExplanationVerdict — the judge's answer to a developer's explanation.

Also the schema passed to ``with_structured_output``, so the field
``description``s are sent to the LLM as guidance.
"""

from pydantic import BaseModel, ConfigDict, Field


class ExplanationVerdict(BaseModel):
    """Whether a developer's explanation shows a finding should be dismissed."""

    model_config = ConfigDict(frozen=True)

    accepted: bool = Field(
        description=(
            "True if the explanation, checked against the code, shows the "
            "finding is wrong or the behaviour is intentional and safe; "
            "false if it is vague, unsupported, or contradicted by the code."
        )
    )
    reason: str = Field(
        description="One sentence on why the explanation does or does not hold up."
    )
