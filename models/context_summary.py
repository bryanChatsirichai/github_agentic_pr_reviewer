"""ContextSummary — precomputed stats about a PR's diff.

Part of ``PRContext`` (``models/pr_context.py``); built alongside it by
``orchestrator.context_builder.build_context``.
"""

from pydantic import BaseModel


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
