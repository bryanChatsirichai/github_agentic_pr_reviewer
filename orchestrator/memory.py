"""Per-PR review memory — Step 4 no-op stub.

Exists so ``run_review`` already has its final shape: load memory → review
→ post → save memory. Step 6 replaces both functions with a real store
(state embedded in the bot's PR comments) without touching the call sites.
"""

from models.finding import Finding

__all__ = ["load_memory", "save_memory"]


def load_memory(pr_number: int) -> list[Finding]:
    """Return the previous findings for PR #``pr_number``.

    Stub: always empty, so every run is a full, stateless review.
    """
    return []


def save_memory(pr_number: int, findings: list[Finding]) -> None:
    """Persist ``findings`` for PR #``pr_number``. Stub: no-op."""
