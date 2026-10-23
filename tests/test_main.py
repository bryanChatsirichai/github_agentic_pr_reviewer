"""Baseline tests for the orchestrator entry point."""

import orchestrator.main as main_module
from orchestrator.main import main, parse_args


def test_parse_args() -> None:
    args = parse_args(["--pr", "123", "--event", "opened"])
    assert args.pr == 123
    assert args.event == "opened"


def test_main_exits_cleanly(monkeypatch) -> None:
    # verify_connection hits the GitHub API — stub it to keep the test offline.
    monkeypatch.setattr(main_module, "verify_connection", lambda: "test-bot")
    assert main(["--pr", "123", "--event", "synchronize"]) == 0
