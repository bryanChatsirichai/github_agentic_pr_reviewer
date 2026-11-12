"""Inline review threads (Step 9b) — pure helpers, no GitHub calls.

Decides where a finding's inline comment can go and which thread actions
bring the PR's threads in line with the review state. ``main.py`` does the
I/O through ``clients.github``.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from models.finding import FindingStatus
from models.stored_finding import StoredFinding

#: Statuses whose finding should have an open thread on its line.
_OPEN_STATUSES = frozenset({FindingStatus.OPEN, FindingStatus.REOPENED})

#: Hidden tag at the start of each finding's inline comment, linking the
#: thread to its finding without storing thread ids in the state.
_FINDING_MARKER = re.compile(r"<!-- ai-review-finding:(F\d{3,}) -->")

#: A unified-diff hunk header's new side: "@@ -a,b +c,d @@" → c (and d).
#: A missing ",d" means a one-line hunk.
_HUNK_NEW_SIDE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def commentable_lines(changed_files: list[dict[str, Any]]) -> dict[str, set[int]]:
    """Map each file to the head-side lines an inline comment may target.

    ``changed_files`` are ``get_pr_files`` entries for the whole PR — pass
    those even on incremental runs, since GitHub places comments against
    the whole PR diff. A line is commentable when a hunk shows it on the
    new side (an added or context line); GitHub rejects the whole review
    with 422 for any other line. Files with no such line (deleted, binary,
    too large for a patch, pure renames) are left out.
    """
    lines: dict[str, set[int]] = {}
    for f in changed_files:
        new_side: set[int] = set()
        for match in _HUNK_NEW_SIDE.finditer(f.get("patch") or ""):
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            new_side.update(range(start, start + count))
        if new_side:
            lines[f["filename"]] = new_side
    return lines


def finding_marker(finding_id: str) -> str:
    """The hidden tag that links an inline comment to ``finding_id``."""
    return f"<!-- ai-review-finding:{finding_id} -->"


def threads_by_finding(
    threads: list[dict[str, Any]], bot_login: str
) -> dict[str, dict[str, Any]]:
    """Map finding ids to their review thread (``get_review_threads`` entries).

    A thread belongs to a finding only when its first comment was written
    by ``bot_login`` and carries ``finding_marker`` — anyone can type the
    marker, and a marker in a later reply does not make the thread ours.
    If several threads claim one finding, the one started last wins.
    """
    found: dict[str, dict[str, Any]] = {}
    for thread in sorted(threads, key=_started_at):
        if not thread["comments"]:
            continue
        first = thread["comments"][0]
        if first["author"] != bot_login:
            continue
        match = _FINDING_MARKER.search(first["body"])
        if match:
            found[match.group(1)] = thread
    return found


def _started_at(thread: dict[str, Any]) -> str:
    """When ``thread`` was started; "" for a thread with no comments left."""
    comments = thread["comments"]
    return comments[0]["created_at"] if comments else ""


@dataclass(frozen=True)
class NewThread:
    """An inline comment to post for a finding that has no thread yet."""

    finding_id: str
    path: str
    line: int
    body: str

    def as_review_comment(self) -> dict[str, Any]:
        """The comment as ``clients.github.post_inline_comments`` takes it."""
        return {"path": self.path, "line": self.line, "side": "RIGHT", "body": self.body}


@dataclass(frozen=True)
class ThreadUpdate:
    """A reply to post in a finding's thread, then resolve or unresolve it."""

    finding_id: str
    thread_id: str
    #: The thread's first comment: GitHub only takes replies to that one.
    comment_id: int
    reply: str
    resolve: bool


@dataclass(frozen=True)
class ThreadPlan:
    """What ``plan_thread_actions`` decided; empty when threads match the state."""

    new_threads: list[NewThread] = field(default_factory=list)
    updates: list[ThreadUpdate] = field(default_factory=list)


def plan_thread_actions(
    findings: list[StoredFinding],
    threads: dict[str, dict[str, Any]],
    commentable: dict[str, set[int]],
    head_sha: str,
) -> ThreadPlan:
    """Decide the thread actions that bring the PR's threads in line with ``findings``.

    ``threads`` is ``threads_by_finding`` output, ``commentable`` is
    ``commentable_lines`` output. Per finding:

    ====================  ===================  ==============================
    status                thread               action
    ====================  ===================  ==============================
    open / reopened       none, line in diff   new inline comment
    open / reopened       none, off the diff   nothing (summary only)
    resolved              unresolved           reply "Fixed", resolve
    rejected / closed     unresolved           reply "Dismissed"/"Closed",
                                               resolve
    reopened              resolved             reply "Came back", unresolve
    anything else                              nothing
    ====================  ===================  ==============================

    Planning from the current state rather than from this run's changes
    makes it self-healing: an action that failed is planned again next run.
    An ``open`` finding whose thread a human resolved is left alone.
    """
    plan = ThreadPlan()
    for finding in findings:
        thread = threads.get(finding.id)
        if thread is None:
            if finding.status in _OPEN_STATUSES and finding.line in commentable.get(
                finding.file, ()
            ):
                plan.new_threads.append(
                    NewThread(finding.id, finding.file, finding.line, _thread_body(finding))
                )
            continue
        reply = _status_reply(finding, thread["is_resolved"], head_sha)
        if reply is not None:
            text, resolve = reply
            plan.updates.append(
                ThreadUpdate(finding.id, thread["id"], thread["comments"][0]["id"], text, resolve)
            )
    return plan


def _status_reply(
    finding: StoredFinding, is_resolved: bool, head_sha: str
) -> tuple[str, bool] | None:
    """``(reply, resolve)`` when the thread no longer matches ``finding``, else None."""
    short_sha = head_sha[:10]
    if not is_resolved:
        if finding.status is FindingStatus.RESOLVED:
            return f"✅ Fixed in `{short_sha}`.", True
        if finding.status is FindingStatus.REJECTED:
            if finding.rejected_by == "developer":
                return "💬 Dismissed: a developer's explanation was accepted.", True
            return f"💬 Dismissed by the {finding.rejected_by}.", True
        if finding.status is FindingStatus.CLOSED:
            return "✅ Closed.", True
    elif finding.status is FindingStatus.REOPENED:
        return f"🔁 Came back in `{short_sha}`.", False
    return None


def _thread_body(finding: StoredFinding) -> str:
    """The inline comment for ``finding``, starting with its marker."""
    # Title on one line: it came from an editable comment (the state block).
    title = " ".join(finding.title.split())
    return "\n".join(
        [
            finding_marker(finding.id),
            f"**{finding.id} — {title}** · severity: **{finding.severity}**",
            "",
            finding.description,
            "",
            "_If this is intentional, reply here explaining why; an accepted "
            "explanation dismisses the finding on the next push._",
        ]
    )


def thread_replies(
    threads: dict[str, dict[str, Any]], bot_login: str
) -> list[dict[str, Any]]:
    """Turn replies in the findings' threads into explanation candidates.

    ``threads`` is ``threads_by_finding`` output. Each reply after the
    bot's first comment, not by ``bot_login``, comes back as a
    ``get_pr_comments``-shaped dict whose body starts ``"<finding id>: "``,
    so ``developer_replies`` and ``collect_explanations`` treat it like an
    ``F003: …`` PR comment: same cutoff, same trust rule, same judge.
    """
    replies: list[dict[str, Any]] = []
    for finding_id, thread in threads.items():
        for comment in thread["comments"][1:]:
            if comment["author"] == bot_login:
                continue
            replies.append(comment | {"body": f"{finding_id}: {comment['body']}"})
    return replies
