"""Entry point for the agentic PR reviewer.

Wires the Step 4 pipeline end-to-end: build PR context → run the logic
reviewer → render findings into a ``🤖 AI Review`` comment → post it.
Run as a module from the repo root: ``python -m orchestrator.main``.
"""

import argparse
import logging
import os

from dotenv import load_dotenv

from agents.llm import resolve_model
from agents.reviewer_agents.factory import build_reviewer
from agents.reviewer_agents.parser import FindingParseError
from clients.github import post_review_comment, verify_connection
from models.finding import Finding
from orchestrator.context_builder import build_context

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


def render_comment(findings: list[Finding]) -> str:
    """Render findings into the body of a ``🤖 AI Review`` comment.

    The MARKER line is prepended by ``post_review_comment``. Step 7 splits
    this into "Previous findings" / "New findings" sections once memory
    exists; until then one flat list is the whole story.
    """
    if not findings:
        return "## Review Findings\n\nNo issues found. 🎉"
    lines = ["## Review Findings", "", f"Found {len(findings)} issue(s):", ""]
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


def run_review(pr_number: int) -> list[Finding]:
    """Full review pass for one PR: context → reviewer → comment on GitHub."""
    # Config errors (missing provider/key) raise RuntimeError here, before
    # any GitHub write — fail fast rather than half way through a review.
    provider, model = resolve_model()
    logger.info("Using LLM provider=%s model=%s", provider, model)
    
    context = build_context(pr_number)
    reviewer = build_reviewer("logic-reviewer")
    findings = reviewer.review_findings(context)
    logger.info("Reviewer returned %d finding(s)", len(findings))

    post_review_comment(pr_number, render_comment(findings))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger.info("Starting review for PR #%s (event: %s)", args.pr, args.event)
    if args.pr == 0:
        # Manual workflow-dispatch test path: proves the runner boots, without
        # needing a real PR to review.
        logger.info("Manual dispatch (--pr 0): no PR to review; done.")
        return 0
    try:
        repo_full_name = verify_connection()
        logger.info("GitHub token OK for repo: %s", repo_full_name)
        run_review(args.pr)
    except (RuntimeError, FindingParseError) as exc:
        # RuntimeError = env/config (token, provider, repo) or GitHub API
        # trouble; FindingParseError = the reviewer produced unparseable
        # output twice. Both fail the run loudly — an empty or missing
        # comment must never look like a clean review.
        logger.error("Review failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
