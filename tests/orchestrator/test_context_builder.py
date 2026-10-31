"""Unit tests for orchestrator.context_builder.

The GitHub client functions themselves are covered in test_github_client.py,
and the diff decision in test_diff.py; here we only check that
build_context assembles their output correctly.
"""

from unittest.mock import MagicMock

from models.review_state import ReviewState
from orchestrator.context_builder import _debug_preview, build_context
from tests.helpers import fake_compare, load_pr_fixture, make_fake_pr, make_repo


def test_build_context_populates_every_field() -> None:
    fixture = load_pr_fixture()

    ctx = build_context(42, repo=make_repo(make_fake_pr(fixture)))

    assert (ctx.pr_number, ctx.title, ctx.author) == (42, fixture["title"], "octocat")
    assert (ctx.base_branch, ctx.base_sha) == ("main", fixture["base"]["sha"])
    assert (ctx.head_branch, ctx.head_sha) == ("feature/fibonacci", fixture["head"]["sha"])
    assert len(ctx.changed_files) == 3
    # Only the bot comment survives the MARKER filter.
    assert [c["id"] for c in ctx.existing_comments] == [501]
    # Filled from memory by run_review, not from GitHub.
    assert ctx.previous_findings == []
    # No state → first review → the whole PR.
    assert ctx.diff_mode == "full"
    # Summary stays in sync with the diff and file list.
    assert ctx.diff.startswith("diff --git a/src/cli.py b/src/cli.py")
    assert ctx.summary.diff_size == len(ctx.diff)
    assert ctx.summary.file_list == [f["filename"] for f in fixture["files"]]


def test_build_context_fetches_the_pr_files_once() -> None:
    # The full diff is rendered from the files already fetched, not by a
    # second get_pr_files call through get_pr_diff.
    pr = make_fake_pr(load_pr_fixture())
    pr.get_files = MagicMock(side_effect=pr.get_files)

    build_context(42, repo=make_repo(pr))

    pr.get_files.assert_called_once()


def test_build_context_uses_the_incremental_diff_after_a_review() -> None:
    fixture = load_pr_fixture()
    head = fixture["head"]["sha"]
    repo = make_repo(make_fake_pr(fixture))
    since_last_review = fixture["files"][0] | {"patch": "@@ -1 +1 @@\n+only the new commit"}
    repo.compare.side_effect = fake_compare(files=[since_last_review])
    state = ReviewState(reviewed_sha="old111", base_ref="main", base_merge_sha="mb000")

    ctx = build_context(42, state=state, repo=repo)

    assert ctx.diff_mode == "incremental"
    assert "+only the new commit" in ctx.diff
    assert ctx.summary.diff_size == len(ctx.diff)
    repo.compare.assert_any_call("old111", head)
    # The file list stays the whole PR: it drives file_shas and the comment.
    assert len(ctx.changed_files) == 3


def test_debug_preview_truncates_large_sections() -> None:
    ctx = build_context(42, repo=make_repo(make_fake_pr(load_pr_fixture())))
    ctx = ctx.model_copy(update={"diff": "x" * 5000})

    preview = _debug_preview(ctx)

    # Head of the section kept, tail replaced by a size marker.
    assert "x" * 1500 in preview
    assert "x" * 1501 not in preview
    assert "[truncated 1500/5000 chars]" in preview
