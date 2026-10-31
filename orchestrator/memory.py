"""Per-PR review memory, embedded in the bot's own PR comments.

Every Actions run starts on a fresh runner, so the ``ReviewState`` rides
inside each posted review comment as a hidden HTML comment (design section 10)::

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
- ``ALLOWED_TRANSITIONS`` / ``transition`` — done (Task 5).
- ``allocate_ids`` — done (Task 6).
- ``cut_snippet`` — done (Task 1): the orchestrator-side snippet cutter.

Step 7:

- ``file_new_findings`` — done (Task 1): turns reviewer findings into
  stored ones (IDs, ``new → open``, snippets).
- ``drop_duplicates`` — done (Task 3): removes repeats of already-reported
  issues before IDs are handed out.
"""

import logging
import os
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Literal

from github import Github
from github.Repository import Repository
from pydantic import ValidationError

from clients import github
from models.finding import Finding, FindingStatus
from models.review_state import ReviewState
from models.stored_finding import (
    KNOWN_ISSUE_STATUSES,
    MAX_DESCRIPTION_CHARS,
    StoredFinding,
    finding_number,
    format_finding_id,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "BOT_LOGIN_ENV",
    "DEFAULT_BOT_LOGIN",
    "IllegalTransitionError",
    "MAX_STATE_BLOCK_CHARS",
    "STATE_BLOCK_PREFIX",
    "STATE_BLOCK_SUFFIX",
    "StateTooLargeError",
    "allocate_ids",
    "SNIPPET_CONTEXT_LINES",
    "check_transition",
    "cut_snippet",
    "DUPLICATE_LINE_WINDOW",
    "DUPLICATE_TITLE_SIMILARITY",
    "drop_duplicates",
    "file_new_findings",
    "load_memory",
    "parse_state_block",
    "render_state_block",
    "save_memory",
    "transition",
]

logger = logging.getLogger("pr-reviewer.memory")
flow = logging.getLogger("pr-reviewer.flow")

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
            dropped = len(state.findings) - len(findings)
            if dropped:
                logger.warning(
                    "State block over %d chars: dropped %d oldest "
                    "closed/rejected finding(s)",
                    MAX_STATE_BLOCK_CHARS,
                    dropped,
                )
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
        # Skip comments not authored by the bot login.
        if comment["author"] != login:
            continue
        state = parse_state_block(comment["body"])
        if state is not None:
            flow.info(
                "[memory] state from comment #%s: %d finding(s) [%s], reviewed_sha=%s",
                comment["id"],
                len(state.findings),
                _status_counts(state.findings) or "none",
                state.reviewed_sha,
            )
            return state
        logger.warning("Comment #%s has no valid state block", comment["id"])

    flow.info("[memory] no previous state on PR #%s — first review", pr_number)
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
    comment_id = github.post_review_comment(
        pr_number, f"{body}\n\n{block}", repo=repo, client=client
    )
    # Logged after the post: only a posted block counts as saved.
    flow.info(
        "[post] comment #%s saved state: %d finding(s) [%s], next_id=%d, "
        "reviewed_sha=%s, base_merge_sha=%s, block %d chars",
        comment_id,
        len(state.findings),
        _status_counts(state.findings) or "none",
        state.next_id,
        state.reviewed_sha,
        state.base_merge_sha,
        len(block),
    )
    return comment_id


def _status_counts(findings: list[StoredFinding]) -> str:
    """``"open=2 resolved=1"`` — statuses with no findings are left out."""
    counts = Counter(f.status for f in findings)
    return " ".join(f"{s.value}={counts[s]}" for s in FindingStatus if counts[s])


# --- Finding lifecycle (design section 11) -------------------------------

#: Every legal status move; anything else raises. ``rejected`` and
#: ``closed`` are terminal. ``new`` only ever leaves for ``open`` — that
#: move happens when a fresh reviewer finding is filed into the state.
ALLOWED_TRANSITIONS: dict[FindingStatus, frozenset[FindingStatus]] = {
    FindingStatus.NEW: frozenset({FindingStatus.OPEN}),
    FindingStatus.OPEN: frozenset({FindingStatus.RESOLVED, FindingStatus.REJECTED}),
    FindingStatus.RESOLVED: frozenset({FindingStatus.REOPENED, FindingStatus.VERIFIED}),
    FindingStatus.REOPENED: frozenset({FindingStatus.RESOLVED, FindingStatus.REJECTED}),
    FindingStatus.VERIFIED: frozenset({FindingStatus.CLOSED}),
    FindingStatus.REJECTED: frozenset(),
    FindingStatus.CLOSED: frozenset(),
}


class IllegalTransitionError(ValueError):
    """A status move that ``ALLOWED_TRANSITIONS`` does not permit."""


def check_transition(src: FindingStatus, dst: FindingStatus) -> None:
    """Raise ``IllegalTransitionError`` unless ``src → dst`` is allowed.

    A move to the same status is illegal too: a "still open" re-check
    verdict means *don't call this*, not "move open → open".
    """
    if dst not in ALLOWED_TRANSITIONS[src]:
        raise IllegalTransitionError(f"illegal finding transition {src} → {dst}")


def transition(
    finding: StoredFinding,
    to: FindingStatus,
    *,
    rejected_by: Literal["critic", "developer"] | None = None,
) -> StoredFinding:
    """Return a copy of ``finding`` moved to status ``to``.

    ``rejected_by`` must be given exactly when ``to`` is ``rejected``. The
    copy is re-validated (``model_copy`` would skip that), so breaking the
    rule raises ``ValidationError``.

    Raises:
        IllegalTransitionError: ``finding.status → to`` is not allowed.
        pydantic.ValidationError: ``rejected_by`` given or missing wrongly.
    """
    check_transition(finding.status, to)
    fields = finding.model_dump() | {"status": to, "rejected_by": rejected_by}
    return StoredFinding.model_validate(fields)


# --- Finding IDs ---------------------------------------------------------


def allocate_ids(state: ReviewState, count: int) -> tuple[list[str], ReviewState]:
    """Hand out ``count`` new finding IDs, continuing from ``state.next_id``.

    Returns the IDs and a copy of ``state`` with ``next_id`` moved past them
    (``state`` itself is not modified). The counter only grows, so an ID is
    never reused — not even one whose finding was trimmed from the block.

    The reviewer LLM numbers its findings ``F001``… on every run; those
    numbers are thrown away and replaced with IDs from here.
    """
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")

    first = state.next_id
    ids = [format_finding_id(n) for n in range(first, first + count)]
    return ids, state.model_copy(update={"next_id": first + count})


# --- Snippets ------------------------------------------------------------

#: Lines kept above and below the finding's line: 7 lines in all.
SNIPPET_CONTEXT_LINES = 3


def cut_snippet(
    file_text: str, line: int, context: int = SNIPPET_CONTEXT_LINES
) -> str:
    """Return lines ``line - context`` … ``line + context`` of ``file_text``.

    ``line`` is 1-based, as on ``Finding``. The window is clamped at both
    ends of the file, and a ``line`` past the end gives ``""`` (the LLM
    pointed at a line that does not exist). Line endings are normalised to
    ``\n`` so a CRLF checkout compares equal to an LF one in Step 9.

    The snippet comes from the real head file, never from the LLM, which
    tends to paraphrase or re-indent code it quotes.

    Raises:
        ValueError: ``line`` is below 1 or ``context`` is negative.
    """
    if line < 1:
        raise ValueError(f"line numbers start at 1, got {line}")
    if context < 0:
        raise ValueError(f"context must be >= 0, got {context}")

    lines = file_text.splitlines()
    if line > len(lines):
        return ""

    start = max(line - 1 - context, 0)
    return "\n".join(lines[start : line + context])


# --- Dropping duplicates -------------------------------------------------

#: How far a repeat may drift from the stored line and still match on a
#: merely similar title.
DUPLICATE_LINE_WINDOW = 3

#: ``difflib`` ratio two normalised titles need to count as similar. A
#: starting guess: "Input validation missing" vs "Missing input
#: validation" scores 0.67, so it is not far above real rewordings — tune it
#: against real reviewer output.
DUPLICATE_TITLE_SIMILARITY = 0.6


def drop_duplicates(
    stored: list[StoredFinding], findings: list[Finding]
) -> list[Finding]:
    """Return ``findings`` without the issues that were already reported.

    A finding is a duplicate when it is in the same file as an earlier one
    and either the normalised titles are equal, or it is within
    ``DUPLICATE_LINE_WINDOW`` lines and the titles are similar
    (``DUPLICATE_TITLE_SIMILARITY``). "Earlier" means a stored finding in a
    ``KNOWN_ISSUE_STATUSES`` status, or a finding kept earlier in this same
    batch — the reviewer sometimes reports one issue twice.

    Snippets are not compared: they are cut around whatever line the LLM
    names, so a 2-line drift breaks an exact match. Nearby lines alone are
    not enough either — two different bugs on adjacent lines must both be
    kept, since hiding a real finding is worse than showing a duplicate.
    A heavy rewording at a moved line still gets through.

    Dropped findings are logged with the ID they matched; stored findings
    are not changed (re-checking them is Step 9).
    """
    earlier = [
        (f.file, f.line, _normalise_title(f.title), f.id)
        for f in stored
        if f.status in KNOWN_ISSUE_STATUSES
    ]
    kept: list[Finding] = []
    for finding in findings:
        title = _normalise_title(finding.title)
        match = next(
            (
                label
                for file, line, other, label in earlier
                if file == finding.file
                and _same_issue(finding.line, title, line, other)
            ),
            None,
        )
        if match is not None:
            logger.info(
                "Dropping duplicate of %s: %s:%d %r",
                match,
                finding.file,
                finding.line,
                finding.title,
            )
            continue
        kept.append(finding)
        earlier.append((finding.file, finding.line, title, "a finding in this review"))
    return kept


def _same_issue(line: int, title: str, other_line: int, other_title: str) -> bool:
    """The matching rule of ``drop_duplicates``, for two findings in one file."""
    if title == other_title:
        return True
    return (
        abs(line - other_line) <= DUPLICATE_LINE_WINDOW
        and SequenceMatcher(None, title, other_title).ratio()
        >= DUPLICATE_TITLE_SIMILARITY
    )


def _normalise_title(title: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed."""
    return " ".join(re.sub(r"[^\w\s]", " ", title.lower()).split())


# --- Filing new findings ---------------------------------------------------


def file_new_findings(
    state: ReviewState,
    findings: list[Finding],
    file_texts: dict[str, str | None],
) -> tuple[list[StoredFinding], ReviewState]:
    """File fresh reviewer ``findings`` into ``state``.

    Each finding gets the next free ID (the LLM's own ``F001``… is thrown
    away), moves ``new → open``, and gets its ``snippet`` cut from
    ``file_texts[finding.file]`` — the head file, fetched once per file by
    the caller. Returns the stored findings, in the same order as
    ``findings``, and a copy of ``state`` with them appended (``state``
    itself is not modified).

    Untrusted LLM fields are coerced rather than failing the whole review:

    - a line below 1 is stored as 1;
    - a description over ``MAX_DESCRIPTION_CHARS`` is cut, ending in ``…``
      (the visible comment still shows it in full);
    - a file with no text (``None`` or missing — deleted, made up, binary
      or too large) gets an empty snippet, logged as a warning, since
      Step 9 cannot re-check it.
    """
    ids, state = allocate_ids(state, len(findings))
    stored: list[StoredFinding] = []
    for finding_id, finding in zip(ids, findings):
        check_transition(FindingStatus.NEW, FindingStatus.OPEN)
        line = finding.line
        if line < 1:
            logger.warning("%s: line %d is below 1, storing 1", finding_id, line)
            line = 1
        file_text = file_texts.get(finding.file)
        snippet = cut_snippet(file_text, line) if file_text is not None else ""
        if not snippet:
            logger.warning(
                "%s: no snippet for %s:%d (file missing at head or line past EOF)",
                finding_id,
                finding.file,
                line,
            )
        stored.append(
            StoredFinding(
                id=finding_id,
                status=FindingStatus.OPEN,
                severity=finding.severity,
                file=finding.file,
                line=line,
                title=finding.title,
                description=_cap_description(finding.description),
                confidence=finding.confidence,
                snippet=snippet,
            )
        )
    return stored, state.model_copy(update={"findings": state.findings + stored})


def _cap_description(text: str) -> str:
    """Cut ``text`` to ``MAX_DESCRIPTION_CHARS``, ending in ``…`` when cut."""
    if len(text) <= MAX_DESCRIPTION_CHARS:
        return text
    return text[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
