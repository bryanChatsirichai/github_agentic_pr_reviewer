"""Entry point for the agentic PR reviewer.

Step 0 skeleton + Step 2 sanity check (verify the GitHub token) + bare-minimum
Step 4 (call the LLM once and log its reply). Later steps grow this into the
full review pipeline. Run as a module from the repo root:
``python -m orchestrator.main``.
"""

import argparse
import logging

from dotenv import load_dotenv

from agents.llm import get_llm
from clients.github import verify_connection

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger.info("Starting review for PR #%s (event: %s)", args.pr, args.event)
    # Sanity check: fail fast if the token is missing/bad before doing work.
    # Returns the repo full name — CI's secrets.GITHUB_TOKEN has no user login.
    repo_full_name = verify_connection()
    logger.info("GitHub token OK for repo: %s", repo_full_name)
    # Bare-minimum LLM call — proves the Action can reach the model. No
    # prompt/context/parsing yet; that's the rest of Step 4. Config errors
    # (missing provider/key) are logged and exit 1 — the GitHub Action then
    # fails on that same message.
    try:
        llm = get_llm()
    except RuntimeError as exc:
        logger.error("LLM config error: %s", exc)
        return 1
    response = llm.invoke("What LLM Agent are you?")
    logger.info("LLM response: %s", response.content)
    # Full review pipeline (context → prompt → findings → comment) lands
    # here in the rest of Step 4.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
