"""Entry point for the agentic PR reviewer.

Pipeline: load memory → build PR context → run the logic reviewer → render
findings into a ``🤖 AI Review`` comment → post it with the hidden state
block (``save_memory``). Run as a module from the repo root:
``python -m orchestrator.main``.
"""

import argparse
import logging
import os

from dotenv import load_dotenv
from github import GithubException

from agents import llm
from agents.llm import resolve_model
from agents.reviewer_agents.factory import build_reviewer
from agents.reviewer_agents.parser import FindingParseError
from clients.github import verify_connection
from models.pr_context import PRContext
from models.review import ReviewResult
from orchestrator.context_builder import build_context
from orchestrator.memory import StateTooLargeError, load_memory, save_memory

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Debug verbosity for OUR loggers only (pr-reviewer.* — e.g. the truncated
# PR context preview in context_builder). Scoping to this namespace keeps
# LOG_LEVEL=DEBUG from also enabling very chatty httpx/google_genai output.
logging.getLogger("pr-reviewer").setLevel(
    os.environ.get("LOG_LEVEL", "INFO").upper()
)
logger = logging.getLogger("pr-reviewer")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args. Returns Namespace with `.pr` (int) and `.event` (str)."""
    parser = argparse.ArgumentParser(description="Agentic PR reviewer")
    # Which PR to review — GitHub PR number, e.g. 123 in github.com/you/repo/pull/123.
    # Defaults to 0 for manual dispatch runs (no PR context).
    parser.add_argument("--pr", type=int, default=0, help="Pull request number")
    # Why we're running — the GitHub Actions event types the workflow sends:
    #   opened      → pull_request trigger: PR created (full review).
    #   synchronize → pull_request trigger: commit pushed to open PR branch
    #                 (incremental review + re-check of previous findings).
    #   reopened    → pull_request trigger: closed PR opened again (full review).
    #   push        → manual dispatch only: "Run workflow" button / gh CLI
    #                 (testing path — the push trigger itself is not enabled).
    parser.add_argument(
        "--event",
        choices=["opened", "synchronize", "reopened", "push"],
        default="push",
        help="PR event that triggered the review",
    )
    return parser.parse_args(argv)


def render_comment(context: PRContext, result: ReviewResult) -> str:
    """Render the review verdict into the body of a ``🤖 AI Review`` comment.

    Metadata (branch, commit, files) comes from the PRContext, not the
    agent — known facts must not be restated by a model that could
    misremember them. The overall rating is the agent's judgment. The
    MARKER line is prepended by ``post_review_comment``.
    """
    files = context.summary.file_list
    lines = [
        "## Review Findings",
        "",
        f"**Overall: {result.overall_rating}** · "
        f"branch `{context.head_branch}` @ `{context.head_sha[:10]}` · "
        f"{len(files)} file(s) checked",
        "",
        _files_section(files),
        "",
    ]
    findings = result.findings
    if not findings:
        lines += ["No issues found. 🎉"]
        return "\n".join(lines).rstrip()
    lines += [f"Found {len(findings)} issue(s):", ""]
    for f in findings:
        lines += [
            f"### {f.id} — {f.title}",
            f"`{f.file}:{f.line}` · severity: **{f.severity}** · "
            f"confidence: {f.confidence:.2f}",
            "",
            f.description,
            "",
        ]
    return "\n".join(lines).rstrip()


def _files_section(files: list[str], inline_limit: int = 10) -> str:
    """List the checked files, collapsed once the list gets long."""
    if len(files) <= inline_limit:
        return "Files checked: " + (", ".join(files) if files else "(none)")
    listed = "\n".join(f"- {name}" for name in files)
    return f"<details>\n<summary>Files checked ({len(files)})</summary>\n\n{listed}\n\n</details>"


def run_review(pr_number: int) -> ReviewResult:
    """Full review pass for one PR: context → reviewer → comment on GitHub."""
    # Config errors (missing provider/key) raise RuntimeError here, before
    # any GitHub write — fail fast rather than half way through a review.
    provider, model = resolve_model()
    logger.info("Using LLM provider=%s model=%s", provider, model)

    # State from the bot's last review comment (empty → full review). Not
    # used yet: Step 7 feeds its findings into the reviewer prompt.
    state = load_memory(pr_number)
    logger.info("Loaded %d previous finding(s) from memory", len(state.findings))

    context = build_context(pr_number)
    reviewer = build_reviewer("logic-reviewer")
    result = reviewer.review(context)
    logger.info(
        "Reviewer returned %d finding(s), rating=%s",
        len(result.findings),
        result.overall_rating,
    )

    # Posting is saving: the comment carries the state block, so a failed
    # post records nothing. Step 7 updates the state (new findings, SHAs)
    # before this; until then it is carried forward unchanged.
    save_memory(pr_number, render_comment(context, result), state)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger.info("Starting review for PR #%s (event: %s)", args.pr, args.event)
    
    try:
        repo_full_name = verify_connection()
        logger.info("GitHub token OK for repo: %s", repo_full_name)
        if args.pr == 0:
            # Manual workflow-dispatch test path: proves the runner boots,
            # without needing a real PR to review.
            chat_model = llm.get_llm()
            response = chat_model.invoke("Testing what model is being used")
            logger.info("Response: %s", response)
            logger.info("Manual dispatch (--pr 0): no PR to review, done.")
            return 0
        run_review(args.pr)
    except (
        RuntimeError,
        GithubException,
        FindingParseError,
        StateTooLargeError,
    ) as exc:
        # RuntimeError = env/config (token, provider, repo); GithubException
        # = GitHub API failure (forbidden, missing PR/repo); FindingParseError
        # = the reviewer produced unparseable output twice; StateTooLargeError
        # = too many open findings to fit the state block. All fail the run
        # loudly — an empty or missing comment must never look like a clean
        # review.
        logger.error("Review failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
