"""PRContext — the single object every agent consumes.

``orchestrator/context_builder.py`` builds one of these per run by combining
the raw GitHub data (``clients/github.py``) into a flat, agent-ready shape.
Nothing here talks to GitHub directly — this module only defines the data.
"""

from typing import Any

from pydantic import BaseModel, Field

from models.review_state import StoredFinding


class ContextSummary(BaseModel):
    """Precomputed stats about the diff, so later steps can decide whether to
    truncate a huge PR before handing it to an LLM — without re-scanning
    the full diff string each time.
    """

    #: Character length of ``PRContext.diff`` — a fast proxy for review size,
    #: since token-exact counting needs a model-specific tokenizer.
    diff_size: int
    #: Filenames touched by the PR, in the same order as ``changed_files``.
    file_list: list[str]


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
    #: Unified diff for the whole PR (see ``clients.github.get_pr_diff``).
    diff: str
    #: Raw entries from ``clients.github.get_pr_files`` (filename, sha,
    #: status, additions, deletions, previous_filename, patch).
    changed_files: list[dict[str, Any]]
    #: Prior bot comments (see ``clients.github.MARKER``), oldest first, as
    #: returned by ``get_existing_review_comments``.
    existing_comments: list[dict[str, Any]]
    #: Findings from the bot's earlier reviews, as loaded by ``load_memory``.
    #: ``build_context`` leaves it empty; ``run_review`` fills it from the
    #: state. Untrusted: it was parsed from an editable PR comment.
    previous_findings: list[StoredFinding] = Field(default_factory=list)
    summary: ContextSummary
