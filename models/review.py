"""ReviewResult — one reviewer agent's verdict for one PR.

Pairs the findings with the overall rating. Ratings mirror the GitHub
review states (approve / comment / request_changes) so a later step can
post a real GitHub review instead of a plain comment.

``ReviewResult`` is also the schema passed to ``with_structured_output``,
so its field ``description``s are sent to the reviewer LLM as guidance.
"""

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from models.finding import Finding, Severity

Rating = Literal["approve", "comment", "request_changes"]

#: Allowed values for ``ReviewResult.overall_rating``.
RATINGS: tuple[str, ...] = get_args(Rating)

#: Ratings implied by the finding mix when the model returned a bare
#: findings array without the rating field.
_BLOCKING_SEVERITIES = (Severity.HIGH, Severity.CRITICAL)


class ReviewResult(BaseModel):
    """What one review pass concluded: verdict plus the findings behind it."""

    model_config = ConfigDict(frozen=True)

    overall_rating: Rating = Field(
        description=(
            "Verdict for the PR as a whole: approve (nothing blocking), "
            "comment (worth noting, not blocking), or request_changes (real "
            "problems that should be fixed before merge)."
        )
    )
    findings: list[Finding] = Field(
        default_factory=list,
        description="Issues you can point to a file and line for; empty if none.",
    )


def derived_rating(findings: list[Finding]) -> str:
    """Mechanical fallback rating for models that skip the rating field.

    A bare JSON array of findings is still accepted (older contract), but
    then the rating is computed from the severities instead of trusted to
    the model: blocking findings → request_changes, otherwise comment, and
    a clean array → approve.
    """
    if not findings:
        return "approve"
    if any(f.severity in _BLOCKING_SEVERITIES for f in findings):
        return "request_changes"
    return "comment"
