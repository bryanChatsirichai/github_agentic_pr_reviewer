"""Tests for orchestrator/diff.py — choosing the diff the reviewer sees."""

from unittest.mock import MagicMock

import pytest
from github import GithubException

from models.review_state import ReviewState
from orchestrator.diff import COMPARE_FILE_LIMIT, choose_diff
from tests.helpers import GoneComparison, fake_compare

#: The last review's state: head old111 on main, split off main at mb000.
STATE = ReviewState(reviewed_sha="old111", base_ref="main", base_merge_sha="mb000")


def _file(name: str, line: str = "+x") -> dict:
    """A get_pr_files-shaped entry with a one-line patch."""
    return dict(
        filename=name,
        sha=f"blob-{name}",
        status="modified",
        additions=1,
        deletions=0,
        previous_filename=None,
        patch=f"@@ -1 +1 @@\n{line}",
    )


#: The PR's files at head — what the full diff is built from.
PR_FILES = [_file("a.py", "+a whole-PR"), _file("b.py", "+b whole-PR")]


def _repo(**compare) -> MagicMock:
    """A repo whose compare answers per ``fake_compare(**compare)``."""
    repo = MagicMock()
    repo.compare.side_effect = fake_compare(**compare)
    return repo


def _choose(state=STATE, base_ref="main", repo=None):
    return choose_diff(state, base_ref, "new222", PR_FILES, repo=repo or _repo())


def _assert_full(choice) -> None:
    """A full review: every PR file's whole-PR patch is in the diff."""
    assert choice.mode == "full"
    assert "+a whole-PR" in choice.diff
    assert "+b whole-PR" in choice.diff


def test_new_commits_on_top_give_an_incremental_diff() -> None:
    repo = _repo(files=[_file("a.py", "+a since last review")])

    choice = _choose(repo=repo)

    assert choice.mode == "incremental"
    assert "+a since last review" in choice.diff
    assert "whole-PR" not in choice.diff
    repo.compare.assert_any_call("old111", "new222")


def test_incremental_diff_keeps_only_files_still_in_the_pr() -> None:
    # c.py changed in the range but is no longer part of the PR (e.g. a
    # change that was reverted back to the base version).
    repo = _repo(files=[_file("a.py"), _file("c.py")])

    choice = _choose(repo=repo)

    assert "b/a.py" in choice.diff
    assert "c.py" not in choice.diff


def test_range_touching_no_pr_file_is_a_noop() -> None:
    # Only c.py changed, and it is not part of the PR: nothing to review.
    # noop keeps reviewed_sha, so the next run's range still covers this.
    choice = _choose(repo=_repo(files=[_file("c.py")]))

    assert choice.mode == "noop"
    assert choice.diff == ""


def test_same_head_is_a_noop() -> None:
    choice = _choose(repo=_repo(status="identical"))

    assert choice.mode == "noop"
    assert choice.diff == ""


def test_first_review_is_full_without_a_compare_call() -> None:
    repo = _repo()

    _assert_full(_choose(state=ReviewState(), repo=repo))
    repo.compare.assert_not_called()


def test_retargeted_pr_is_full() -> None:
    _assert_full(_choose(base_ref="release"))


@pytest.mark.parametrize("status", ["diverged", "behind"])
def test_rewritten_history_is_full(status) -> None:
    # diverged = rebase / force-push; behind = branch reset to older commit.
    _assert_full(_choose(repo=_repo(status=status, files=[_file("a.py")])))


def test_moved_merge_base_is_full() -> None:
    # Base branch merged into the PR ("Update branch"): the range now holds
    # other people's changes too.
    _assert_full(_choose(repo=_repo(files=[_file("a.py")], merge_base="mb999")))


def test_possibly_truncated_compare_is_full() -> None:
    # GitHub lists at most 300 files per compare; a full list may be cut off.
    files = [_file(f"f{i}.py") for i in range(COMPARE_FILE_LIMIT)]

    _assert_full(_choose(repo=_repo(files=files)))


@pytest.mark.parametrize("http_status", [404, 422])
def test_old_commit_gone_is_full(http_status) -> None:
    repo = MagicMock()
    repo.compare.return_value = GoneComparison(http_status)

    _assert_full(_choose(repo=repo))


def test_other_api_errors_propagate() -> None:
    repo = MagicMock()
    repo.compare.return_value = GoneComparison(500)

    with pytest.raises(GithubException):
        _choose(repo=repo)
