"""StoredFinding — one finding as kept in the per-PR state block.

``ReviewState`` (``models/review_state.py``) holds a list of these. The
block is parsed back from an editable PR comment, so the model forbids
unknown fields and enforces its invariants on load.

Also home to the finding-ID helpers and the status set that counts as
"already reported", since both describe stored findings.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.finding import FindingStatus, Severity

#: Cap on a stored description. Comment bodies max out at 65,536 chars and
#: the visible review shares that budget with the state block.
MAX_DESCRIPTION_CHARS = 300

#: Finding IDs: ``F`` + at least three digits (``F001`` … ``F999``, ``F1000``).
_ID_PATTERN = r"^F\d{3,}$"

#: Statuses of findings that count as already reported: the reviewer is
#: shown them and must not repeat them, and repeats are dropped. ``verified``
#: and ``closed`` are left out — their fix was confirmed, so if the issue
#: comes back it is a new finding.
KNOWN_ISSUE_STATUSES = frozenset(
    {
        FindingStatus.OPEN,
        FindingStatus.REOPENED,
        FindingStatus.RESOLVED,
        FindingStatus.REJECTED,
    }
)


def finding_number(finding_id: str) -> int:
    """Return the numeric part of a finding ID: ``"F007"`` → ``7``."""
    return int(finding_id[1:])


def format_finding_id(number: int) -> str:
    """Return the finding ID for ``number``: ``7`` → ``"F007"``.

    The inverse of ``finding_number``. Past 999 the ID simply grows a digit
    (``F1000``), which ``_ID_PATTERN`` allows.
    """
    if number < 1:
        raise ValueError(f"finding numbers start at 1, got {number}")
    return f"F{number:03d}"


class StoredFinding(BaseModel):
    """One finding as kept in the state block.

    Carries the same core fields as ``Finding`` (the LLM's output), plus
    three that only the orchestrator may fill in:

    - ``rejected_by`` — who dismissed the finding;
    - ``snippet`` — the code the finding points at;
    - ``thread_id`` — its inline review thread on GitHub.

    Invariants (checked on load):

    - ``status`` is never ``new``;
    - ``rejected_by`` is set exactly when ``status`` is ``rejected``.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Identity & lifecycle -------------------------------------------

    #: Stable ID (``F001``, ``F002``, …) that survives across commits.
    id: str = Field(pattern=_ID_PATTERN)

    #: Lifecycle state (design section 11). Never ``new`` — that status is
    #: transient and becomes ``open`` once the finding is filed into memory.
    status: FindingStatus

    #: Who dismissed the finding: the Step 12 critic or a developer's
    #: explanation (Step 9). Set exactly when ``status`` is ``rejected``.
    rejected_by: Literal["critic", "developer"] | None = None

    # --- What & where (same meaning as on ``Finding``) ------------------

    severity: Severity

    #: Path relative to the repo root.
    file: str

    #: 1-based line in ``file`` the finding refers to.
    line: int = Field(ge=1)

    title: str

    description: str = Field(max_length=MAX_DESCRIPTION_CHARS)

    #: Reviewer certainty in [0.0, 1.0]. Kept for the Step 13 policy on
    #: later runs: below-threshold findings are stored but never posted, and
    #: 0.75–0.89 "suggestions" keep their label when listed again.
    confidence: float = Field(ge=0.0, le=1.0)

    # --- Orchestrator-only ----------------------------------------------

    #: Code around ``line`` in the head file, cut by the orchestrator — the
    #: LLM is not trusted to copy code. Step 9 compares it with the new code
    #: to decide resolved vs. reopened.
    snippet: str = ""

    #: GitHub inline review comment id. ``None`` until Step 9b posts threads.
    thread_id: int | None = None

    @model_validator(mode="after")
    def _check_status(self) -> "StoredFinding":
        """Enforce the status invariants listed in the class docstring."""
        if self.status is FindingStatus.NEW:
            raise ValueError("'new' is transient and must not be stored")

        is_rejected = self.status is FindingStatus.REJECTED
        has_rejecter = self.rejected_by is not None
        if is_rejected != has_rejecter:
            raise ValueError(
                "rejected_by must be set exactly when status is 'rejected'"
            )
        return self
