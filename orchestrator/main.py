"""Entry point for the agentic PR reviewer.

Step 0: parse --pr and --event, log a startup line, exit cleanly.
Later steps grow the review pipeline into this skeleton.
"""

import argparse
import logging

from dotenv import load_dotenv

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
    # Review pipeline lands here in later steps.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
