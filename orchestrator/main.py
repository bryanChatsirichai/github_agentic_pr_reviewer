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
    parser.add_argument("--pr", type=int, required=True, help="Pull request number")
    # Why we're running: opened = new PR, synchronize = new commit pushed,
    # reopened = closed PR opened again. Drives incremental review + re-checks later.
    parser.add_argument(
        "--event",
        choices=["opened", "synchronize", "reopened"],
        required=True,
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
