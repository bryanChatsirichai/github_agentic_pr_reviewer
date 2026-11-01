"""Re-check of previous findings against the new code (design section 11).

Each run, every previous finding that is still in play goes through these
stages, cheapest first:

1. ``skip_unchanged`` (no LLM) compares each finding's file blob SHA with
   the one saved by the last review. Unchanged file → status kept. Deleted
   file → ``resolved``. Changed file → stage 2.
2. ``recheck_findings`` (one LLM call per finding) shows the LLM the
   finding, the code it was reported on, and that code as it is now, and
   asks whether the defect is still there.
3. ``apply_outcomes`` (no LLM) turns each yes/no answer into a status
   move through the lifecycle table: fixed → ``resolved``, back after a
   fix → ``reopened``.

Step 9 status:

- ``skip_unchanged`` — done (Task 1).
- ``recheck_findings`` — done (Task 2).
- ``apply_outcomes`` — done (Task 3).
"""

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from agents.llm import get_llm
from clients import github
from agents.reviewer_agents.factory import PROMPTS_DIR
from models.finding import FindingStatus
from models.pr_context import PRContext
from models.recheck_verdict import RecheckVerdict
from models.stored_finding import StoredFinding
from orchestrator.memory import SNIPPET_CONTEXT_LINES, cut_snippet, transition

logger = logging.getLogger("pr-reviewer.recheck")
flow = logging.getLogger("pr-reviewer.flow")

__all__ = [
    "LOCATE_SIMILARITY",
    "RECHECK_STATUSES",
    "RecheckOutcome",
    "Rechecker",
    "SkipCheck",
    "WINDOW_LINES",
    "apply_outcomes",
    "build_rechecker",
    "code_window",
    "file_diff",
    "locate_line",
    "recheck_findings",
    "render_recheck",
    "skip_unchanged",
]

#: Statuses the re-check looks at. ``resolved`` is included so a fix that
#: is later undone can be caught (``resolved → reopened``). ``rejected``,
#: ``verified`` and ``closed`` are settled and never re-checked.
RECHECK_STATUSES = frozenset(
    {FindingStatus.OPEN, FindingStatus.REOPENED, FindingStatus.RESOLVED}
)


# --- Stage 1: skip findings whose file did not change ---------------------


@dataclass(frozen=True)
class SkipCheck:
    """Outcome of ``skip_unchanged``."""

    #: Every finding, in input order, with this stage's moves applied:
    #: deleted file → ``resolved``, renamed file → ``file`` updated.
    findings: list[StoredFinding]

    #: The findings (as in ``findings``) that only an LLM re-check can
    #: decide, because their file changed or cannot be compared.
    to_recheck: list[StoredFinding]


def skip_unchanged(
    findings: list[StoredFinding],
    file_shas: dict[str, str],
    changed_files: list[dict[str, Any]],
) -> SkipCheck:
    """Sort previous findings by whether their file changed since the last review.

    ``file_shas`` is the last review's ``{filename: blob_sha}``;
    ``changed_files`` is this run's ``get_pr_files`` output, which always
    covers the whole PR, so the check works in full and incremental runs
    alike. A blob SHA is Git's hash of the file's contents: an equal SHA
    means the file is byte-for-byte unchanged.

    For each finding in a ``RECHECK_STATUSES`` status:

    - file renamed → ``file`` follows the new name first; a pure rename
      keeps its SHA, so it then counts as unchanged;
    - file unchanged → status kept;
    - file deleted (``status == "removed"``) → ``resolved``, unless it
      already is. GitHub lists a deleted file with its old SHA, so the
      status, not the SHA, decides this case;
    - otherwise → re-check: the file changed, has no stored SHA, or is no
      longer in the PR. The last covers a file the PR added and then
      deleted (it drops out of the list instead of showing ``removed``)
      and a file whose changes were all reverted; only the head file can
      tell those apart.
    """
    by_name = {f["filename"]: f for f in changed_files}
    by_old_name = {
        f["previous_filename"]: f for f in changed_files if f.get("previous_filename")
    }

    kept: list[StoredFinding] = []
    to_recheck: list[StoredFinding] = []
    for finding in findings:
        if finding.status not in RECHECK_STATUSES:
            kept.append(finding)
            continue

        # The stored SHA is keyed by the name at the last review.
        stored_sha = file_shas.get(finding.file)
        entry = by_name.get(finding.file)
        if entry is None and finding.file in by_old_name:
            entry = by_old_name[finding.file]
            finding = finding.model_copy(update={"file": entry["filename"]})

        if entry is not None and entry.get("status") == "removed":
            if finding.status is not FindingStatus.RESOLVED:
                finding = transition(finding, FindingStatus.RESOLVED)
            logger.debug("%s: %s deleted → resolved", finding.id, finding.file)
        elif entry is not None and stored_sha and entry.get("sha") == stored_sha:
            logger.debug("%s: %s unchanged → kept", finding.id, finding.file)
        else:
            to_recheck.append(finding)
            logger.debug("%s: %s changed → re-check", finding.id, finding.file)
        kept.append(finding)

    return SkipCheck(findings=kept, to_recheck=to_recheck)


# --- Stage 2: ask the LLM about findings whose file changed ---------------

#: Lines of head code shown above and below the finding's line.
WINDOW_LINES = 30

#: ``difflib`` ratio the stored snippet needs against a stretch of the head
#: file to count as found there. Same starting guess as the duplicate
#: check's title similarity; tune it against real snippets.
LOCATE_SIMILARITY = 0.6


@dataclass(frozen=True)
class RecheckOutcome:
    """What the re-check concluded about one finding."""

    issue_present: bool

    #: One line, for the log and the next comment.
    reason: str

    #: Where the finding's code is now: the located line, or the stored
    #: line when its code was not found.
    line: int


@dataclass(frozen=True)
class Rechecker:
    """The re-check agent: the ``recheck.md`` prompt bound to a chat model."""

    system_prompt: str
    llm: BaseChatModel

    def recheck(self, user_message: str) -> RecheckVerdict | None:
        """Ask about one finding. Returns None when no usable answer came back.

        Retries once with the error fed back. Unlike the reviewer, a second
        failure does not fail the run: the finding just keeps its status.
        """
        messages = [SystemMessage(self.system_prompt), HumanMessage(user_message)]
        verdict, error = self._invoke(messages)
        if verdict is None:
            retry = HumanMessage(
                f"Your previous reply could not be parsed as a JSON object "
                f"with issue_present and reason: {error}. Respond again with "
                f"ONLY the JSON object."
            )
            verdict, error = self._invoke(messages + [retry])
        if verdict is None:
            logger.warning("Re-check output unusable twice (%s)", error)
        return verdict

    def _invoke(self, messages: list[BaseMessage]) -> tuple[RecheckVerdict | None, str]:
        """One structured LLM call. Returns ``(verdict, "")`` or ``(None, error)``."""
        output = self.llm.with_structured_output(
            RecheckVerdict, include_raw=True
        ).invoke(messages)
        if output["parsed"] is not None:
            return output["parsed"], ""
        return None, str(output["parsing_error"] or "no structured output")


def build_rechecker() -> Rechecker:
    """Build the re-check agent from ``prompts/recheck.md`` + ``get_llm()``."""
    prompt = (PROMPTS_DIR / "recheck.md").read_text(encoding="utf-8").strip()
    return Rechecker(system_prompt=prompt, llm=get_llm())


def recheck_findings(
    findings: list[StoredFinding],
    file_texts: dict[str, str | None],
    context: PRContext,
    recheck_agent: Rechecker,
) -> dict[str, RecheckOutcome]:
    """Re-check each finding against its file at head.

    ``file_texts`` maps each finding's ``file`` to its head content, as
    ``clients.github.get_file_content`` returned it (None = not readable).

    Returns ``{finding_id: outcome}``. Findings without an outcome keep
    their status; see ``_recheck_one`` for when that happens.
    """
    in_pr = {f["filename"] for f in context.changed_files}
    outcomes: dict[str, RecheckOutcome] = {}
    for finding in findings:
        text = file_texts.get(finding.file)
        outcome = _recheck_one(finding, text, finding.file in in_pr, context, recheck_agent)
        if outcome is None:
            continue
        outcomes[finding.id] = outcome
        flow.info(
            "[recheck] %s %s — %s",
            finding.id,
            "still present" if outcome.issue_present else "gone",
            outcome.reason,
        )
    return outcomes


def _recheck_one(
    finding: StoredFinding,
    text: str | None,
    in_pr: bool,
    context: PRContext,
    recheck_agent: Rechecker,
) -> RecheckOutcome | None:
    """Re-check one finding; None when there is no answer to act on.

    - No head text and the file is missing at head: it was added, then
      deleted, so the defect is gone with it. No LLM call.
    - No head text but the file exists (a directory, or over the 1 MB
      contents limit): unreadable. None. Being in the PR proves it exists;
      outside the PR (all its changes reverted) only GitHub can tell
      missing from too large, so it is asked.
    - Otherwise one LLM call; None when it gives no usable answer.
    """
    if text is None:
        if in_pr or github.file_exists(finding.file, context.head_sha):
            flow.warning(
                "[recheck] %s: %s unreadable at head — no LLM call, status kept",
                finding.id,
                finding.file,
            )
            return None
        flow.info("[recheck] %s: %s gone from head — no LLM call", finding.id, finding.file)
        return RecheckOutcome(False, "file no longer exists", finding.line)

    located = locate_line(text, finding.snippet, finding.line)
    flow.info(
        "[recheck] %s: asking recheck_agent about %s:%d (%s)",
        finding.id,
        finding.file,
        located or finding.line,
        f"moved from line {finding.line}" if located and located != finding.line
        else "code found at the stored line" if located
        else "stored code not found",
    )
    verdict = recheck_agent.recheck(render_recheck(finding, text, located, context))
    if verdict is None:
        flow.warning("[recheck] %s: no usable answer — status kept", finding.id)
        return None
    return RecheckOutcome(
        issue_present=verdict.issue_present,
        # LLM-written: collapsed so it cannot break the log line or comment.
        reason=" ".join(verdict.reason.split()),
        line=located or finding.line,
    )


def render_recheck(
    finding: StoredFinding, file_text: str, located: int | None, context: PRContext
) -> str:
    """Render the re-check user message for one finding.

    Then (the stored finding and snippet) next to now (the head code and
    this file's diff). ``located`` is where ``locate_line`` found the
    finding's code; None shows the code around the stored line instead,
    and says the original code was not found.
    """
    if located:
        where = f"around line {located}"
    else:
        where = (
            f"around the reported line {finding.line}; the reported code "
            f"was not found in the current file"
        )
    diff_scope = (
        "since the last review"
        if context.diff_mode == "incremental"
        else "in the whole pull request"
    )
    lines = [
        f"## Finding {finding.id} (status: {finding.status})",
        f"File: {finding.file}, reported at line {finding.line}",
        f"Severity: {finding.severity}",
        # Title on one line: it comes from an editable comment, so a newline
        # must not start a fake section.
        f"Title: {' '.join(finding.title.split())}",
        f"Description: {finding.description}",
        "",
        "## Code when the finding was reported",
        "```",
        finding.snippet or "(not recorded)",
        "```",
        "",
        f"## Current code, {where}",
        "```",
        code_window(file_text, located or finding.line),
        "```",
        "",
        f"## Changes to this file {diff_scope}",
        "```diff",
        file_diff(context.diff, finding.file) or "(none in this diff)",
        "```",
    ]
    return "\n".join(lines)


def locate_line(file_text: str, snippet: str, old_line: int) -> int | None:
    """Return the line in ``file_text`` where the finding's code is now, or None.

    Lines added or removed above a finding shift it, so its stored line
    number may be stale. This slides the stored snippet over the file,
    compares each same-sized stretch with it (indentation ignored), and
    keeps the best match scoring at least ``LOCATE_SIMILARITY``. A tie
    goes to the stretch nearest ``old_line``, for code that repeats.

    None when the snippet is empty (none was stored) or nothing matches
    well enough: the code was rewritten or removed.
    """
    snippet_lines = [line.strip() for line in snippet.splitlines()]
    if not any(snippet_lines):
        return None
    file_lines = [line.strip() for line in file_text.splitlines()]
    size = len(snippet_lines)
    # cut_snippet starts the snippet SNIPPET_CONTEXT_LINES above the
    # finding's line, or at line 1 if that is closer.
    line_in_snippet = min(SNIPPET_CONTEXT_LINES, old_line - 1)

    matcher = SequenceMatcher(autojunk=False)
    # SequenceMatcher caches seq2, so it holds the side that stays fixed.
    matcher.set_seq2("\n".join(snippet_lines))
    best_ratio, best_start = 0.0, None

    def beats_best(ratio: float) -> bool:
        return ratio >= LOCATE_SIMILARITY and ratio > best_ratio

    # Nearest stretch first. A later one must score strictly higher, which
    # settles ties by distance, lets the cheap upper bounds skip most of a
    # large file, and allows stopping at the first exact match.
    expected_start = old_line - 1 - line_in_snippet
    starts = sorted(
        range(max(len(file_lines) - size + 1, 1)),
        key=lambda start: abs(start - expected_start),
    )
    for start in starts:
        matcher.set_seq1("\n".join(file_lines[start : start + size]))
        # real_quick_ratio >= quick_ratio >= ratio, cheapest first.
        if not beats_best(matcher.real_quick_ratio()) or not beats_best(
            matcher.quick_ratio()
        ):
            continue
        ratio = matcher.ratio()
        if beats_best(ratio):
            best_ratio, best_start = ratio, start
            if ratio == 1.0:
                break

    if best_start is None:
        return None
    return min(best_start + line_in_snippet + 1, len(file_lines))


def code_window(file_text: str, line: int, context: int = WINDOW_LINES) -> str:
    """Lines ``line ± context`` of ``file_text``, each prefixed with its number.

    Clamped at both ends of the file. Numbered so the LLM can cite lines.
    """
    lines = file_text.splitlines()
    start = max(line - 1 - context, 0)
    return "\n".join(
        f"{n:>5}| {text}"
        for n, text in enumerate(lines[start : line + context], start=start + 1)
    )


def file_diff(diff: str, path: str) -> str:
    """Return ``path``'s section of unified ``diff`` (``render_diff`` output).

    A section runs from its ``diff --git a/<old> b/<new>`` header to the
    next header and is matched on the new name. Empty when the diff does
    not touch ``path``.
    """
    kept: list[str] = []
    in_section = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_section = line.endswith(f" b/{path}")
        if in_section:
            kept.append(line)
    return "\n".join(kept)


# --- Stage 3: apply the answers to the findings ----------------------------


def apply_outcomes(
    findings: list[StoredFinding],
    outcomes: dict[str, RecheckOutcome],
    file_texts: dict[str, str | None],
) -> list[StoredFinding]:
    """Return ``findings`` with each re-check outcome applied, in input order.

    ``outcomes`` comes from ``recheck_findings``; findings without one are
    returned unchanged. ``file_texts`` is the head content it was given.

    Status moves, all through the lifecycle table (``transition``):

    ===================  =============  ==============
    status               defect is      becomes
    ===================  =============  ==============
    open / reopened      gone           resolved
    resolved             present        reopened
    anything else        —              unchanged
    ===================  =============  ==============

    A finding whose defect is still present follows its code: when the code
    moved, ``line`` and ``snippet`` are updated together from the head file.
    They must change together: ``locate_line`` reads the finding's position
    inside the snippet from ``line``. A finding whose defect is gone keeps
    both, since its snippet is the faulty code a later regression would
    bring back.
    """
    updated: list[StoredFinding] = []
    for finding in findings:
        outcome = outcomes.get(finding.id)
        if outcome is not None:
            finding = _apply_one(finding, outcome, file_texts.get(finding.file))
        updated.append(finding)
    return updated


def _apply_one(
    finding: StoredFinding, outcome: RecheckOutcome, file_text: str | None
) -> StoredFinding:
    """Apply one outcome; see ``apply_outcomes`` for the rules."""
    target = _status_after(finding.status, outcome.issue_present)
    if target is not None:
        flow.info("[status] %s %s → %s", finding.id, finding.status, target)
        finding = transition(finding, target)

    if outcome.issue_present and outcome.line != finding.line and file_text:
        flow.info("[status] %s moved line %d → %d", finding.id, finding.line, outcome.line)
        finding = finding.model_copy(
            update={"line": outcome.line, "snippet": cut_snippet(file_text, outcome.line)}
        )
    return finding


def _status_after(status: FindingStatus, issue_present: bool) -> FindingStatus | None:
    """The status a re-check answer moves a finding to, or None to keep it."""
    if issue_present and status is FindingStatus.RESOLVED:
        return FindingStatus.REOPENED
    if not issue_present and status in (FindingStatus.OPEN, FindingStatus.REOPENED):
        return FindingStatus.RESOLVED
    return None
