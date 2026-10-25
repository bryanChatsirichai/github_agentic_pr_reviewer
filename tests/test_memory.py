"""Tests for the Step 4 memory stub: empty load, no-op save."""

from orchestrator.memory import load_memory, save_memory
from tests.helpers import make_finding


def test_load_memory_returns_empty_state() -> None:
    assert load_memory(42) == []


def test_save_memory_is_a_no_op() -> None:
    save_memory(42, [make_finding()])
    # Nothing persists yet, so a reload is still empty.
    assert load_memory(42) == []
