"""ReviewState — the per-PR memory carried from one review run to the next.

Why it exists
    Every GitHub Actions run starts on a fresh runner, so nothing written to
    disk survives. Instead, the state rides inside the bot's own PR comment
    as a hidden ``<!-- ai-review-state:v1 {json} -->`` block (design section 10):
    invisible on the rendered page, returned verbatim by the API.

How it is shaped
    ``ReviewState`` is the whole block — one per PR. It holds PR-level facts
    (last reviewed commit, base branch, ID counter, file blob SHAs) plus a
    list of ``StoredFinding`` (``models/stored_finding.py``), one per issue
    ever reported on the PR.

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

from models.stored_finding import StoredFinding, finding_number


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
