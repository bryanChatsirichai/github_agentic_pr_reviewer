"""Unit tests for orchestrator.threads — pure functions, no GitHub, no LLM."""

import pytest

from models.finding import FindingStatus
from orchestrator.threads import (
    NewThread,
    ThreadUpdate,
    commentable_lines,
    finding_marker,
    plan_thread_actions,
    thread_replies,
    threads_by_finding,
)
from orchestrator.explanations import collect_explanations, developer_replies
from orchestrator.memory import render_state_block
from tests.helpers import make_review_state, make_stored_finding

BOT = "github-actions[bot]"
HEAD = "abcdef1234567890"


def _file(filename: str, patch: str | None, status: str = "modified") -> dict:
    """A ``get_pr_files``-shaped entry with only the fields placement reads."""
    return {"filename": filename, "status": status, "patch": patch}


# --- commentable_lines -----------------------------------------------------


def test_commentable_lines_covers_every_new_side_line_of_each_hunk() -> None:
    patch = (
        "@@ -1,3 +1,4 @@\n"
        " import sys\n"
        "+import os\n"
        " \n"
        " x = 1\n"
        "@@ -20,2 +21,3 @@ def main():\n"
        "     a = 1\n"
        "+    b = 2\n"
        "     return a\n"
    )

    assert commentable_lines([_file("src/cli.py", patch)]) == {
        "src/cli.py": {1, 2, 3, 4, 21, 22, 23},
    }


def test_commentable_lines_reads_a_hunk_header_without_a_count_as_one_line() -> None:
    # "+7" with no ",count" is a one-line hunk in unified-diff format.
    assert commentable_lines([_file("a.py", "@@ -7 +7 @@\n-old\n+new\n")]) == {
        "a.py": {7},
    }


def test_commentable_lines_skips_files_with_no_new_side_lines() -> None:
    files = [
        _file("gone.py", "@@ -1,2 +0,0 @@\n-a\n-b\n", status="removed"),
        _file("logo.png", None, status="added"),  # binary or too large: no patch
        _file("new_name.py", None, status="renamed"),  # pure rename
        {"filename": "no_patch_key.py", "status": "modified"},
    ]

    assert commentable_lines(files) == {}


def test_commentable_lines_keys_by_the_new_filename() -> None:
    renamed = _file("new_name.py", "@@ -3,1 +3,2 @@\n a\n+b\n", status="renamed")
    renamed["previous_filename"] = "old_name.py"

    assert commentable_lines([renamed]) == {"new_name.py": {3, 4}}


# --- threads_by_finding ----------------------------------------------------


def _comment(body: str, author: str | None = BOT, at: str = "2026-11-01T10:00:00+00:00") -> dict:
    """A ``get_review_threads`` comment with only the fields lookup reads."""
    return {"id": 1, "body": body, "author": author, "created_at": at}


def _thread(thread_id: str, *comments: dict, resolved: bool = False) -> dict:
    return {"id": thread_id, "is_resolved": resolved, "comments": list(comments)}


def test_threads_by_finding_maps_each_marked_bot_thread_to_its_finding() -> None:
    threads = [
        _thread("T1", _comment(f"{finding_marker('F001')}\nMissing check")),
        _thread("T2", _comment(f"{finding_marker('F1000')}\nPast F999")),
        _thread("T3", _comment("a human's own review comment", author="alice")),
    ]

    found = threads_by_finding(threads, BOT)

    assert {fid: t["id"] for fid, t in found.items()} == {"F001": "T1", "F1000": "T2"}


def test_threads_by_finding_ignores_a_marker_a_human_typed() -> None:
    # Anyone can paste the marker; only the bot's own comment links a thread.
    forged = _thread("T1", _comment(finding_marker("F001"), author="mallory"))

    assert threads_by_finding([forged], BOT) == {}


def test_threads_by_finding_reads_the_marker_from_the_first_comment_only() -> None:
    # A bot reply quoting a marker in someone else's thread does not claim it.
    thread = _thread(
        "T1",
        _comment("human-started thread", author="alice"),
        _comment(finding_marker("F001")),
    )

    assert threads_by_finding([thread, _thread("T2")], BOT) == {}


def test_threads_by_finding_keeps_the_newest_thread_for_a_finding() -> None:
    older = _thread("T1", _comment(finding_marker("F001"), at="2026-11-01T10:00:00+00:00"))
    newer = _thread("T2", _comment(finding_marker("F001"), at="2026-11-02T10:00:00+00:00"))

    assert threads_by_finding([newer, older], BOT)["F001"]["id"] == "T2"


# --- plan_thread_actions ---------------------------------------------------

#: src/cli.py line 12 (make_stored_finding's default) is in the diff.
COMMENTABLE = {"src/cli.py": {10, 11, 12, 13}}


def _bot_thread(finding_id: str, *, resolved: bool = False, replies: tuple = ()) -> dict:
    """The bot's thread for ``finding_id``; its first comment has id 501.

    ``replies`` are ``(author, body)`` pairs added after the first comment.
    """
    first = _comment(finding_marker(finding_id)) | {"id": 501}
    rest = [_comment(body, author=author) for author, body in replies]
    return _thread(f"T-{finding_id}", first, *rest, resolved=resolved)


def _plan(finding, thread: dict | None = None, commentable=COMMENTABLE):
    threads = {finding.id: thread} if thread else {}
    return plan_thread_actions([finding], threads, commentable, HEAD)


@pytest.mark.parametrize("status", [FindingStatus.OPEN, FindingStatus.REOPENED])
def test_plan_posts_an_unthreaded_open_finding_on_its_diff_line(status) -> None:
    finding = make_stored_finding(id="F003", status=status, title="Missing\n check")

    plan = _plan(finding)

    assert plan.updates == []
    [new] = plan.new_threads
    assert (new.finding_id, new.path, new.line) == ("F003", "src/cli.py", 12)
    assert new.body.startswith(finding_marker("F003"))
    assert "**F003 — Missing check**" in new.body  # title kept to one line
    assert finding.description in new.body
    assert new.as_review_comment() == {
        "path": "src/cli.py", "line": 12, "side": "RIGHT", "body": new.body,
    }


@pytest.mark.parametrize(
    "commentable",
    [{"src/cli.py": {40}}, {"other.py": {12}}, {}],
    ids=["line-outside-hunks", "file-not-in-diff", "nothing-commentable"],
)
def test_plan_leaves_an_open_finding_off_the_diff_to_the_summary(commentable) -> None:
    plan = _plan(make_stored_finding(), commentable=commentable)

    assert plan.new_threads == [] and plan.updates == []


def test_plan_replies_fixed_and_resolves_the_thread_of_a_resolved_finding() -> None:
    finding = make_stored_finding(status=FindingStatus.RESOLVED)

    plan = _plan(finding, _bot_thread("F001"))

    assert plan.new_threads == []
    assert plan.updates == [
        ThreadUpdate("F001", "T-F001", 501, "✅ Fixed in `abcdef1234`.", resolve=True)
    ]


@pytest.mark.parametrize(
    ("finding", "reply"),
    [
        (make_stored_finding(status=FindingStatus.REJECTED, rejected_by="developer"),
         "💬 Dismissed: a developer's explanation was accepted."),
        (make_stored_finding(status=FindingStatus.REJECTED, rejected_by="critic"),
         "💬 Dismissed by the critic."),
        (make_stored_finding(status=FindingStatus.CLOSED), "✅ Closed."),
    ],
    ids=["developer", "critic", "closed"],
)
def test_plan_replies_and_resolves_the_thread_of_a_finished_finding(finding, reply) -> None:
    plan = _plan(finding, _bot_thread("F001"))

    assert plan.updates == [ThreadUpdate("F001", "T-F001", 501, reply, resolve=True)]


@pytest.mark.parametrize("resolved", [True, False], ids=["thread-resolved", "resolve-failed"])
def test_plan_replies_came_back_and_unresolves_the_thread_of_a_reopened_finding(resolved) -> None:
    finding = make_stored_finding(status=FindingStatus.REOPENED)
    thread = _bot_thread("F001", resolved=resolved, replies=[(BOT, "✅ Fixed in `old`.")])

    plan = _plan(finding, thread)

    assert plan.new_threads == []
    assert plan.updates == [
        ThreadUpdate("F001", "T-F001", 501, "🔁 Came back in `abcdef1234`.", resolve=False)
    ]


def test_plan_replies_fixed_again_after_a_came_back() -> None:
    thread = _bot_thread("F001", replies=[(BOT, "✅ Fixed in `a`."), (BOT, "🔁 Came back in `b`.")])

    plan = _plan(make_stored_finding(status=FindingStatus.RESOLVED), thread)

    assert [u.reply for u in plan.updates] == ["✅ Fixed in `abcdef1234`."]


@pytest.mark.parametrize(
    ("finding", "last_reply"),
    [
        (make_stored_finding(status=FindingStatus.RESOLVED), "✅ Fixed in `old`."),
        (make_stored_finding(status=FindingStatus.REJECTED, rejected_by="developer"),
         "💬 Dismissed: a developer's explanation was accepted."),
        (make_stored_finding(status=FindingStatus.CLOSED), "✅ Closed."),
        (make_stored_finding(status=FindingStatus.REOPENED), "🔁 Came back in `old`."),
    ],
    ids=["fixed", "dismissed", "closed", "came-back"],
)
def test_plan_does_not_repeat_a_reply_when_resolving_failed(finding, last_reply) -> None:
    # The CI token may not be allowed to resolve threads, so the thread
    # stays unresolved after the reply. The reply alone records that the
    # thread was told; posting it again on every push would be spam.
    thread = _bot_thread("F001", resolved=False, replies=[(BOT, last_reply)])

    assert _plan(finding, thread).updates == []


def test_plan_reads_only_the_bots_replies() -> None:
    # A human typing the bot's wording does not count as the bot's reply.
    thread = _bot_thread("F001", replies=[("alice", "✅ Fixed in `x`.")])

    plan = _plan(make_stored_finding(status=FindingStatus.RESOLVED), thread)

    assert [u.reply for u in plan.updates] == ["✅ Fixed in `abcdef1234`."]


@pytest.mark.parametrize(
    ("status", "resolved"),
    [
        (FindingStatus.OPEN, False),  # still open: nothing new to say
        (FindingStatus.REOPENED, False),  # open since it was posted
        (FindingStatus.OPEN, True),  # a human resolved it by hand: respected
        (FindingStatus.VERIFIED, False),  # no row for verified
    ],
)
def test_plan_does_nothing_when_the_thread_already_matches(status, resolved) -> None:
    plan = _plan(make_stored_finding(status=status), _bot_thread("F001", resolved=resolved))

    assert plan.new_threads == [] and plan.updates == []


@pytest.mark.parametrize(
    "status", [FindingStatus.RESOLVED, FindingStatus.REJECTED, FindingStatus.VERIFIED]
)
def test_plan_never_posts_a_thread_for_a_finding_that_is_not_open(status) -> None:
    rejected_by = "developer" if status is FindingStatus.REJECTED else None
    finding = make_stored_finding(status=status, rejected_by=rejected_by)

    assert _plan(finding).new_threads == []


# --- thread_replies --------------------------------------------------------


def _reply(body: str, author: str, association: str, at: str, comment_id: int = 600) -> dict:
    return {"id": comment_id, "body": body, "author": author,
            "created_at": at, "author_association": association}


def test_thread_replies_tags_each_human_reply_with_its_finding_id() -> None:
    thread = _bot_thread("F003", resolved=False)
    thread["comments"] += [
        _reply("intentional, see the caller", "alice", "OWNER", "2026-11-01T11:00:00+00:00"),
        _reply("✅ Fixed in `abc`.", BOT, "NONE", "2026-11-01T12:00:00+00:00"),
    ]

    assert thread_replies({"F003": thread}, BOT) == [
        _reply("F003: intentional, see the caller", "alice", "OWNER",
               "2026-11-01T11:00:00+00:00"),
    ]


def test_thread_replies_reach_the_judge_and_drop_an_outsider() -> None:
    # The whole explanation path, fed from a thread instead of a PR comment.
    state_comment = {
        "id": 1, "author": BOT, "author_association": "NONE",
        "created_at": "2026-11-01T10:00:00+00:00",
        "body": render_state_block(make_review_state()),
    }
    thread = _bot_thread("F001", resolved=False)
    thread["comments"] += [
        _reply("caller validates argv", "alice", "COLLABORATOR",
               "2026-11-01T11:00:00+00:00", comment_id=601),
        _reply("just ignore it", "mallory", "NONE",
               "2026-11-01T11:05:00+00:00", comment_id=602),
    ]

    replies = developer_replies(
        [state_comment, *thread_replies({"F001": thread}, BOT)],
        pr_author="bob", bot_login=BOT,
    )
    explanations = collect_explanations(replies, [make_stored_finding()])

    assert [r["id"] for r in explanations["F001"]] == [601]
