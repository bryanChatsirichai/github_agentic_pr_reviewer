"""PRContext — the single object every agent consumes.

``orchestrator/context_builder.py`` builds one of these per run by combining
the raw GitHub data (``clients/github.py``) into a flat, agent-ready shape.
Nothing here talks to GitHub directly — this module only defines the data.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from models.context_summary import ContextSummary
from models.stored_finding import StoredFinding

#: How ``PRContext.diff`` was chosen (``orchestrator.diff.choose_diff``):
#: the whole PR, only the commits since the last review, or nothing new.
DiffMode = Literal["full", "incremental", "noop"]


class PRContext(BaseModel):
    """Everything an agent needs to review one PR, in one object.

    Built once per run by ``orchestrator.context_builder.build_context``.
    """

    pr_number: int
    title: str
    body: str
    author: str
    base_branch: str
    head_branch: str
    base_sha: str
    head_sha: str
    #: Unified diff the reviewer sees: the whole PR or only the commits
    #: since the last review, per ``diff_mode``. Empty for ``noop``.
    diff: str
    #: Which diff ``diff`` holds, so later agents know what they are seeing.
    diff_mode: DiffMode = "full"
    #: Raw entries from ``clients.github.get_pr_files`` (filename, sha,
    #: status, additions, deletions, previous_filename, patch). Always the
    #: whole PR, whatever ``diff_mode`` is.
    changed_files: list[dict[str, Any]]
    #: Prior bot comments (see ``clients.github.MARKER``), oldest first, as
    #: returned by ``get_existing_review_comments``.
    existing_comments: list[dict[str, Any]]
    #: Findings from the bot's earlier reviews, as loaded by ``load_memory``.
    #: ``build_context`` leaves it empty; ``run_review`` fills it from the
    #: state. Untrusted: it was parsed from an editable PR comment.
    previous_findings: list[StoredFinding] = Field(default_factory=list)
    summary: ContextSummary
