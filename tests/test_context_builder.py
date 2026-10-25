"""Unit tests for orchestrator.context_builder.

The GitHub client functions themselves are covered in test_github_client.py;
here we only check that build_context assembles their output correctly.
"""

from orchestrator.context_builder import _debug_preview, build_context
from tests.helpers import load_pr_fixture, make_fake_pr, make_repo


def test_build_context_populates_every_field() -> None:
    fixture = load_pr_fixture()

    ctx = build_context(42, repo=make_repo(make_fake_pr(fixture)))

    assert (ctx.pr_number, ctx.title, ctx.author) == (42, fixture["title"], "octocat")
    assert (ctx.base_branch, ctx.base_sha) == ("main", fixture["base"]["sha"])
    assert (ctx.head_branch, ctx.head_sha) == ("feature/fibonacci", fixture["head"]["sha"])
    assert len(ctx.changed_files) == 3
    # Only the bot comment survives the MARKER filter.
    assert [c["id"] for c in ctx.existing_comments] == [501]
    # Memory isn't wired in yet.
    assert ctx.previous_findings == []
    # Summary stays in sync with the diff and file list.
    assert ctx.diff.startswith("diff --git a/src/cli.py b/src/cli.py")
    assert ctx.summary.diff_size == len(ctx.diff)
    assert ctx.summary.file_list == [f["filename"] for f in fixture["files"]]


def test_debug_preview_truncates_large_sections() -> None:
    ctx = build_context(42, repo=make_repo(make_fake_pr(load_pr_fixture())))
    ctx = ctx.model_copy(update={"diff": "x" * 5000})

    preview = _debug_preview(ctx)

    # Head of the section kept, tail replaced by a size marker.
    assert "x" * 1500 in preview
    assert "x" * 1501 not in preview
    assert "[truncated 1500/5000 chars]" in preview
