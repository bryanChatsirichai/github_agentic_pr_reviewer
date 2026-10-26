"""Per-PR review memory, embedded in the bot's own PR comments.

Every Actions run starts on a fresh runner, so the ``ReviewState`` rides
inside each posted review comment as a hidden HTML comment (design §6)::

    🤖 AI Review
    ...visible findings...

    <!-- ai-review-state:v1 {"schema_version":1,"next_id":2,"findings":[...]} -->

GitHub hides the ``<!-- -->`` part on the rendered page, but the API returns
the full comment text, so the next run can read the state back.

Status (Step 6):

- ``render_state_block`` — done (Task 2).
- ``parse_state_block`` / ``load_memory`` — done (Task 3).
- ``save_memory`` — done (Task 4): posts the review with the block
  appended; there is no other write.
"""

import logging
import os

from github import Github
from github.Repository import Repository
from pydantic import ValidationError

from clients import github
from models.finding import FindingStatus
from models.review_state import ReviewState, StoredFinding, finding_number

__all__ = [
    "BOT_LOGIN_ENV",
    "DEFAULT_BOT_LOGIN",
    "MAX_STATE_BLOCK_CHARS",
    "STATE_BLOCK_PREFIX",
    "STATE_BLOCK_SUFFIX",
    "StateTooLargeError",
    "load_memory",
    "parse_state_block",
    "render_state_block",
    "save_memory",
]

logger = logging.getLogger("pr-reviewer.memory")

# --- Block format -------------------------------------------------------

#: Opening of the hidden block. ``v1`` matches ``ReviewState.schema_version``.
STATE_BLOCK_PREFIX = "<!-- ai-review-state:v1 "
STATE_BLOCK_SUFFIX = " -->"

#: Cap on the whole block. Comment bodies max out at 65,536 chars, and the
#: visible review needs the rest.
MAX_STATE_BLOCK_CHARS = 20_000

#: Finished findings — the only ones safe to drop when the block is too big.
_DROPPABLE_STATUSES = (FindingStatus.CLOSED, FindingStatus.REJECTED)

#: Login whose comments may carry state. ``secrets.GITHUB_TOKEN`` posts as
#: ``github-actions[bot]``; a GitHub App would post as ``<app-slug>[bot]``.
DEFAULT_BOT_LOGIN = "github-actions[bot]"

#: Environment variable that overrides ``DEFAULT_BOT_LOGIN``.
BOT_LOGIN_ENV = "REVIEW_BOT_LOGIN"


class StateTooLargeError(ValueError):
    """The block exceeds the cap even with every droppable finding removed."""


# --- Rendering ----------------------------------------------------------


def render_state_block(state: ReviewState) -> str:
    """Render ``state`` as ``<!-- ai-review-state:v1 {json} -->``.

    Escaping:
        ``<`` and ``>`` are written as ``\\u003c`` / ``\\u003e``. That is still
        valid JSON for the same text, and it means a snippet containing
        ``-->`` cannot end the HTML comment early and spill the rest of the
        JSON onto the page.

    Size cap:
        Over ``MAX_STATE_BLOCK_CHARS``, the oldest (lowest-ID) closed and
        rejected findings are dropped one at a time until the block fits.
        ``next_id`` is kept, so dropped IDs are never reused, and ``state``
        itself is not modified.

    Raises:
        StateTooLargeError: the block still does not fit with every closed
            and rejected finding dropped. Open findings are never dropped —
            losing one would make the next run re-report it under a new ID.
    """
    findings = list(state.findings)
    droppable = _droppable_oldest_first(findings)

    while True:
        block = _render(state.model_copy(update={"findings": findings}))
        if len(block) <= MAX_STATE_BLOCK_CHARS:
            return block

        if not droppable:
            raise StateTooLargeError(
                f"state block is {len(block)} chars (cap {MAX_STATE_BLOCK_CHARS}) "
                "with no closed/rejected findings left to drop"
            )
        findings.remove(droppable.pop(0))


def _droppable_oldest_first(findings: list[StoredFinding]) -> list[StoredFinding]:
    """Closed/rejected findings, lowest ID (= oldest) first."""
    droppable = [f for f in findings if f.status in _DROPPABLE_STATUSES]
    return sorted(droppable, key=lambda f: finding_number(f.id))


def _render(state: ReviewState) -> str:
    """Wrap ``state``'s escaped JSON in the block prefix and suffix."""
    payload = _escape_json(state.model_dump_json())
    return f"{STATE_BLOCK_PREFIX}{payload}{STATE_BLOCK_SUFFIX}"


def _escape_json(payload: str) -> str:
    """Escape ``<`` / ``>`` as JSON unicode escapes (see ``render_state_block``).

    Safe on the whole document: ``<`` and ``>`` are not JSON syntax, so they
    can only occur inside strings, where ``\\u003c`` decodes to the same text.
    """
    return payload.replace("<", "\\u003c").replace(">", "\\u003e")


# --- Loading -------------------------------------------------------------


def parse_state_block(body: str) -> ReviewState | None:
    """Return the state hidden in a comment ``body``, or ``None``.

    ``None`` covers every way a block can be unusable: missing, cut off
    before ``-->``, not JSON, or failing ``ReviewState`` validation. The
    block is looked up from the end, since it is appended after the visible
    review. Its JSON holds no raw ``>``, so the first ``-->`` after the
    prefix is the real end.
    """
    start = body.rfind(STATE_BLOCK_PREFIX)
    if start == -1:
        return None

    payload_start = start + len(STATE_BLOCK_PREFIX)
    end = body.find(STATE_BLOCK_SUFFIX, payload_start)
    if end == -1:
        return None

    try:
        # Decodes the \\u003c / \\u003e escapes back to < and >.
        return ReviewState.model_validate_json(body[payload_start:end])
    except ValidationError:
        return None


def load_memory(
    pr_number: int,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
    bot_login: str | None = None,
) -> ReviewState:
    """Return the latest valid review state for PR #``pr_number``.

    Scans the PR's marked comments newest-first and returns the state from
    the first one that is authored by the bot login and holds a valid
    block. The login is ``bot_login``, else ``$REVIEW_BOT_LOGIN``, else
    ``github-actions[bot]``.

    Fallbacks:
        - Other users' comments are skipped, even with a valid block —
          anyone can paste the marker and a block.
        - A bot comment with a missing or corrupt block is skipped, so an
          older valid state is used and IDs don't restart at ``F001``.
        - Nothing usable → ``ReviewState()``, i.e. a full review.

    The result passed validation but came from an editable comment, so
    callers still treat it as untrusted data.
    """
    login = bot_login or os.environ.get(BOT_LOGIN_ENV) or DEFAULT_BOT_LOGIN
    comments = github.get_existing_review_comments(
        pr_number, repo=repo, client=client
    )
    newest_first = sorted(
        comments, key=lambda c: (c["created_at"], c["id"]), reverse=True
    )

    for comment in newest_first:
        if comment["author"] != login:
            continue
        state = parse_state_block(comment["body"])
        if state is not None:
            logger.info(
                "Loaded state from comment #%s: %d finding(s), reviewed_sha=%s",
                comment["id"],
                len(state.findings),
                state.reviewed_sha,
            )
            return state
        logger.warning("Comment #%s has no valid state block", comment["id"])

    logger.info("No previous state on PR #%s — full review", pr_number)
    return ReviewState()


# --- Saving --------------------------------------------------------------


def save_memory(
    pr_number: int,
    body: str,
    state: ReviewState,
    *,
    repo: Repository | None = None,
    client: Github | None = None,
) -> int:
    """Post the review ``body`` with ``state`` appended; return the comment id.

    There is no separate write: the posted comment *is* the save, so the
    stored state can never disagree with what the developer saw, and a
    failed post saves nothing. The block is rendered before posting, so a
    ``StateTooLargeError`` means nothing was posted.

    Raises:
        StateTooLargeError: see ``render_state_block``.
    """
    block = render_state_block(state)
    return github.post_review_comment(
        pr_number, f"{body}\n\n{block}", repo=repo, client=client
    )
