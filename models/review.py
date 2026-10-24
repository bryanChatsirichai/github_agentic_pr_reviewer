"""ReviewResult — one reviewer agent's verdict for one PR.

Pairs the findings with the overall rating. Ratings mirror the GitHub
review states (approve / comment / request_changes) so a later step can
post a real GitHub review instead of a plain comment.
"""

from dataclasses import dataclass, field

from models.finding import Finding, Severity

#: Allowed values for ``ReviewResult.overall_rating``.
RATINGS = ("approve", "comment", "request_changes")

#: Ratings implied by the finding mix when the model returned a bare
#: findings array without the rating field.
_BLOCKING_SEVERITIES = (Severity.HIGH, Severity.CRITICAL)

@dataclass(frozen=True)
class ReviewResult:
    """What one review pass concluded: verdict plus the findings behind it."""

    overall_rating: str
    findings: list[Finding] = field(default_factory=list)


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
