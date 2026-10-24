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


def test_main_logs_and_exits_on_llm_config_error(monkeypatch) -> None:
    # Missing provider/key raises RuntimeError in get_llm — main must log
    # it and exit 1 (which fails the GitHub Action on the same message).
    def raise_config_error():
        raise RuntimeError("MODEL_PROVIDER is not set")

    monkeypatch.setattr(main_module, "verify_connection", lambda: "test-bot")
    monkeypatch.setattr(main_module, "get_llm", raise_config_error)
    assert main(["--pr", "123", "--event", "opened"]) == 1
