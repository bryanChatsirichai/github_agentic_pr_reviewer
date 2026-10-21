"""Baseline tests for the orchestrator entry point."""


from orchestrator.main import main, parse_args


def test_parse_args() -> None:
    args = parse_args(["--pr", "123", "--event", "opened"])
    assert args.pr == 123
    assert args.event == "opened"


def test_main_exits_cleanly() -> None:
    assert main(["--pr", "123", "--event", "synchronize"]) == 0
