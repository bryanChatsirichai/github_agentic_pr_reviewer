"""ReviewState — the per-PR memory carried from one review run to the next.

Why it exists
    Every GitHub Actions run starts on a fresh runner, so nothing written to
    disk survives. Instead, the state rides inside the bot's own PR comment
    as a hidden ``<!-- ai-review-state:v1 {json} -->`` block (design §6):
    invisible on the rendered page, returned verbatim by the API.

How it is shaped
    ``ReviewState`` is the whole block — one per PR. It holds PR-level facts
    (last reviewed commit, base branch, ID counter, file blob SHAs) plus a
    list of ``StoredFinding``, one per issue ever reported on the PR.

Trust
    The block is parsed back from a PR comment that people with write access
    can edit, so it is untrusted input. Both models forbid unknown fields and
    enforce their invariants on load instead of trusting whatever JSON is
    found.

This module only defines the shape. ``orchestrator/memory.py`` renders the
block and (from Step 6 Task 3) loads it.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.finding import FindingStatus, Severity

#: Cap on a stored description. Comment bodies max out at 65,536 chars and
#: the visible review shares that budget with the state block.
MAX_DESCRIPTION_CHARS = 300

#: Finding IDs: ``F`` + at least three digits (``F001`` … ``F999``, ``F1000``).
_ID_PATTERN = r"^F\d{3,}$"


def finding_number(finding_id: str) -> int:
    """Return the numeric part of a finding ID: ``"F007"`` → ``7``."""
    return int(finding_id[1:])


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

    #: Lifecycle state (design §20). Never ``new`` — that status is
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


class ReviewState(BaseModel):
    """Everything one review run hands to the next, for one PR.

    ``ReviewState()`` with no arguments is the *empty state*: no previous
    review, so the next run does a full review and IDs start at ``F001``.

    Invariants (checked on load):

    - finding IDs are unique;
    - every finding ID is below ``next_id``, so the counter never hands out
      an ID that is already in use.
    """

    model_config = ConfigDict(extra="forbid")

    #: Format version. Bumped on incompatible changes; an unknown version
    #: fails validation, so the loader falls back instead of misreading it.
    schema_version: Literal[1] = 1

    # --- Where the last review stood (drives Step 8's diff choice) ------

    #: Head commit this state describes. The next incremental diff starts
    #: here.
    reviewed_sha: str | None = None

    #: Target branch at review time. A change means the PR was retargeted.
    base_ref: str | None = None

    #: Merge-base with ``base_ref`` at review time. A change means the base
    #: branch was merged in, so an incremental diff is no longer safe.
    base_merge_sha: str | None = None

    #: ``{filename: blob_sha}`` from the PR files API. Step 9 skips
    #: re-checking findings in files whose blob is unchanged.
    file_shas: dict[str, str] = Field(default_factory=dict)

    # --- Findings -------------------------------------------------------

    #: Number of the next finding ID (``F{next_id:03d}``). Only ever grows,
    #: even when old findings are dropped from the block.
    next_id: int = Field(default=1, ge=1)

    #: Every finding still kept for this PR, in any status except ``new``.
    findings: list[StoredFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_ids(self) -> "ReviewState":
        """Enforce the ID invariants listed in the class docstring."""
        ids = [f.id for f in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate finding ids in state")

        if ids and max(map(finding_number, ids)) >= self.next_id:
            raise ValueError("next_id must exceed every stored finding id")
        return self
