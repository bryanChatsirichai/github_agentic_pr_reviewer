"""Tests for ``cut_snippet``: the orchestrator cuts snippets, not the LLM."""

import pytest

from orchestrator.memory import cut_snippet

#: Ten lines: "l1" … "l10".
FILE = "\n".join(f"l{n}" for n in range(1, 11)) + "\n"


def _lines(first: int, last: int) -> str:
    return "\n".join(f"l{n}" for n in range(first, last + 1))


@pytest.mark.parametrize(
    "line, expected",
    [(5, _lines(2, 8)), (1, _lines(1, 4)), (10, _lines(7, 10))],
    ids=["middle", "clamped-at-start", "clamped-at-end"],
)
def test_cuts_three_lines_either_side(line: int, expected: str) -> None:
    assert cut_snippet(FILE, line) == expected


@pytest.mark.parametrize("text, line", [(FILE, 11), ("", 1)])
def test_line_past_end_of_file_gives_empty_snippet(text: str, line: int) -> None:
    assert cut_snippet(text, line) == ""


def test_crlf_is_normalised_to_lf() -> None:
    # A CRLF checkout must compare equal to an LF one in Step 9.
    assert cut_snippet(FILE.replace("\n", "\r\n"), 5) == _lines(2, 8)


@pytest.mark.parametrize("line, context", [(0, 3), (1, -1)])
def test_invalid_arguments_raise(line: int, context: int) -> None:
    with pytest.raises(ValueError):
        cut_snippet(FILE, line, context)
