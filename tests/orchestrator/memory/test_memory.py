"""Tests for orchestrator.memory: rendering, loading and saving the state block.

GitHub is faked with PyGithub-shaped SimpleNamespaces — no network involved.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from clients.github import MARKER
from models.review_state import ReviewState
from orchestrator.memory import (
    BOT_LOGIN_ENV,
    MAX_STATE_BLOCK_CHARS,
    STATE_BLOCK_PREFIX,
    STATE_BLOCK_SUFFIX,
    StateTooLargeError,
    load_memory,
    parse_state_block,
    render_state_block,
    save_memory,
)
from tests.helpers import (
    load_pr_fixture,
    make_fake_pr,
    make_repo,
    make_review_state,
    make_stored_finding,
)

BOT = "github-actions[bot]"
_T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)


def _review_body(state: ReviewState) -> str:
    """A bot comment body as it is posted: marker, visible review, state block."""
    return f"{MARKER}\n\n## Review Findings\n\n{render_state_block(state)}"


def _repo_with_comments(*comments: tuple[str, str]):
    """A mock repo whose PR holds ``(author, body)`` comments, oldest first."""
    fakes = [
        SimpleNamespace(
            id=1000 + i,
            body=body,
            created_at=_T0 + timedelta(hours=i),
            user=SimpleNamespace(login=author),
        )
        for i, (author, body) in enumerate(comments)
    ]
    return make_repo(SimpleNamespace(get_issue_comments=lambda: fakes))


def _big_finding(number: int, status: str = "open"):
    """A finding whose snippet alone takes ~3k chars of the block budget."""
    rejected_by = "critic" if status == "rejected" else None
    return make_stored_finding(
        id=f"F{number:03d}", status=status, rejected_by=rejected_by, snippet="x" * 3_000
    )


# --- render / parse ------------------------------------------------------


def test_state_round_trips_through_a_comment_body() -> None:
    state = make_review_state()
    assert parse_state_block(_review_body(state)) == state


def test_snippet_with_comment_terminator_cannot_close_block_early() -> None:
    snippet = "html = '<!-- note -->'  # a > b"
    state = make_review_state(findings=[make_stored_finding(snippet=snippet)])

    block = render_state_block(state)

    # The only "-->" is the real terminator, and no raw < or > remains inside.
    inner = block[len(STATE_BLOCK_PREFIX) : -len(STATE_BLOCK_SUFFIX)]
    assert "<" not in inner and ">" not in inner
    assert block.count("-->") == 1
    assert parse_state_block(block).findings[0].snippet == snippet


def test_oversized_state_drops_oldest_closed_and_rejected_first() -> None:
    # 8 × ~3k > 20k cap: F001–F004 are droppable, F005–F008 must survive.
    findings = [
        _big_finding(1, "closed"),
        _big_finding(2, "rejected"),
        _big_finding(3, "closed"),
        _big_finding(4, "rejected"),
        *(_big_finding(n) for n in range(5, 9)),
    ]
    state = make_review_state(next_id=9, findings=findings)

    block = render_state_block(state)
    loaded = parse_state_block(block)

    assert len(block) <= MAX_STATE_BLOCK_CHARS
    # Only as many as needed are dropped, oldest first.
    assert [f.id for f in loaded.findings] == ["F003", "F004", "F005", "F006", "F007", "F008"]
    assert loaded.next_id == 9  # dropped IDs are never handed out again
    assert len(state.findings) == 8  # the caller's state is not modified


@pytest.mark.parametrize(
    "body",
    [
        f"{MARKER}\n\nNo block at all.",
        f"{MARKER}\n\n{STATE_BLOCK_PREFIX}{{\"schema_version\": 1}}",  # no suffix
        f"{MARKER}\n\n{STATE_BLOCK_PREFIX}{{not json{STATE_BLOCK_SUFFIX}",
        f"{MARKER}\n\n{STATE_BLOCK_PREFIX}{{\"next_id\": 0}}{STATE_BLOCK_SUFFIX}",
        f"{MARKER}\n\n{STATE_BLOCK_PREFIX}[]{STATE_BLOCK_SUFFIX}",
    ],
    ids=["missing", "unterminated", "bad-json", "invalid-state", "not-an-object"],
)
def test_parse_returns_none_for_missing_or_invalid_blocks(body: str) -> None:
    assert parse_state_block(body) is None


# --- load_memory ---------------------------------------------------------


def test_load_with_no_valid_block_is_the_empty_state() -> None:
    repo = _repo_with_comments((BOT, f"{MARKER}\n\nNo block."))
    assert load_memory(42, repo=repo) == ReviewState()


def test_load_returns_the_newest_valid_state() -> None:
    old = make_review_state(reviewed_sha="sha1")
    new = make_review_state(reviewed_sha="sha2")
    repo = _repo_with_comments((BOT, _review_body(old)), (BOT, _review_body(new)))

    assert load_memory(42, repo=repo).reviewed_sha == "sha2"


def test_load_skips_a_newer_bot_comment_without_a_state_block() -> None:
    state = make_review_state(reviewed_sha="sha1")
    repo = _repo_with_comments(
        (BOT, _review_body(state)), (BOT, f"{MARKER}\n\nNo block here.")
    )

    assert load_memory(42, repo=repo) == state


def test_load_falls_back_when_the_newest_block_is_corrupt() -> None:
    # Restarting at F001 would re-file every old finding under a new ID.
    state = make_review_state(reviewed_sha="sha1")
    corrupt = _review_body(state).replace('"next_id":2', '"next_id":"two"')
    repo = _repo_with_comments((BOT, _review_body(state)), (BOT, corrupt))

    assert load_memory(42, repo=repo) == state


def test_load_ignores_state_blocks_posted_by_other_users() -> None:
    # Anyone can post a comment with the marker and a block; only the bot's count.
    real = make_review_state(reviewed_sha="sha1")
    forged = make_review_state(reviewed_sha="forged")
    repo = _repo_with_comments(
        (BOT, _review_body(real)), ("mallory", _review_body(forged))
    )

    assert load_memory(42, repo=repo) == real


def test_bot_login_can_be_overridden(monkeypatch) -> None:
    # A future GitHub App posts as its own login, e.g. "my-reviewer[bot]".
    state = make_review_state()
    repo = _repo_with_comments(("my-reviewer[bot]", _review_body(state)))

    assert load_memory(42, repo=repo) == ReviewState()
    assert load_memory(42, repo=repo, bot_login="my-reviewer[bot]") == state

    monkeypatch.setenv(BOT_LOGIN_ENV, "my-reviewer[bot]")
    assert load_memory(42, repo=repo) == state


# --- save_memory ---------------------------------------------------------


def test_save_posts_the_review_with_the_state_block_appended() -> None:
    pr = make_fake_pr(load_pr_fixture())
    state = make_review_state()

    comment_id = save_memory(42, "## Review Findings", state, repo=make_repo(pr))

    (body,), _ = pr.create_issue_comment.call_args
    assert comment_id == 777
    assert body.startswith(MARKER)  # the client still adds the marker
    assert "## Review Findings" in body
    assert parse_state_block(body) == state


def test_save_posts_nothing_when_the_state_is_too_large() -> None:
    # Open findings are never dropped — losing one would make the next run
    # re-report it under a new ID — so an over-cap state fails before posting.
    pr = make_fake_pr(load_pr_fixture())
    state = make_review_state(
        next_id=9, findings=[_big_finding(n) for n in range(1, 9)]
    )

    with pytest.raises(StateTooLargeError):
        save_memory(42, "## Review Findings", state, repo=make_repo(pr))

    pr.create_issue_comment.assert_not_called()
