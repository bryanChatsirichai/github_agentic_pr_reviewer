"""PRContext — the single object every agent consumes.

Step 3 of the implementation roadmap. ``orchestrator/context_builder.py`` builds one
of these per run by combining the raw GitHub data (Step 2's
``clients/github.py``) into a flat, agent-ready shape. Nothing here talks to
GitHub directly — this module only defines the data.
"""

from typing import Any

from pydantic import BaseModel, Field


class ContextSummary(BaseModel):
    """Cheap, precomputed stats about the diff so later steps can decide
    whether to truncate a huge PR before handing it to an LLM, without
    re-scanning the full diff string each time.
    """

    #: Character length of ``PRContext.diff`` — a fast proxy for "how big is
    #: this review", since token-exact counting needs a model-specific
    #: tokenizer we don't have yet.
    diff_size: int
    #: Filenames touched by the PR, in the same order as ``changed_files``.
    file_list: list[str]


class PRContext(BaseModel):
    """Everything an agent needs to review one PR, in one object.

    Built once per run by ``orchestrator.context_builder.build_context``.
    ``previous_findings`` is always empty until Step 6+ introduces memory —
    kept here now so the field never has to be bolted on later.
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
    #: Raw entries from ``clients.github.get_pr_files`` (filename, status,
    #: additions, deletions, previous_filename, patch).
    changed_files: list[dict[str, Any]]
    #: Prior comments from our bot (see ``clients.github.MARKER``), oldest
    #: first, as returned by ``get_existing_review_comments``.
    existing_comments: list[dict[str, Any]]
    #: Memory doesn't exist until Step 6 — always empty for now.
    previous_findings: list[Any] = Field(default_factory=list)
    summary: ContextSummary
