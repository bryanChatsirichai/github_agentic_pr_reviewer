"""Baseline tests for the orchestrator entry point."""

from types import SimpleNamespace

import orchestrator.main as main_module
from orchestrator.main import main, parse_args


def test_parse_args() -> None:
    args = parse_args(["--pr", "123", "--event", "opened"])
    assert args.pr == 123
    assert args.event == "opened"


def test_main_exits_cleanly(monkeypatch) -> None:
    # verify_connection hits the GitHub API; get_llm hits the LLM provider —
    # stub both to keep the test offline.
    monkeypatch.setattr(main_module, "verify_connection", lambda: "test-bot")
    fake_llm = SimpleNamespace(invoke=lambda prompt: SimpleNamespace(content="OK"))
    monkeypatch.setattr(main_module, "get_llm", lambda: fake_llm)
    assert main(["--pr", "123", "--event", "synchronize"]) == 0
