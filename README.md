# Agentic PR Reviewer.......

An agentic pull-request review system: GitHub PR events trigger LLM-driven reviewers with memory of previous findings, incremental diffs, and a critic/judge loop.

Design: [`docs/agentic-pr-review-system-design.v1.md`](docs/agentic-pr-review-system-design.v1.md)
Roadmap: [`docs/implementation-roadmap.md`](docs/implementation-roadmap.md)
Framework decision: [`docs/framework-decision.md`](docs/framework-decision.md)
Multi-repo integration: [`docs/multi-repo-integration.md`](docs/multi-repo-integration.md)

## Quickstart

Requires [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env   # fill in GITHUB_TOKEN, GITHUB_REPOSITORY, and the LLM key for your provider
uv sync                # install deps (Python 3.12 managed by uv)
uv run pytest          # test suite
uv run python -m orchestrator.main --pr 123 --event opened
```

## Usage

Run as a module from the repo root with `uv run` (activating `.venv` manually also works — `uv run` handles it):

```bash
uv run python -m orchestrator.main --pr <PR_NUMBER> --event <EVENT>
```

### Examples

```bash
# Full review of PR #123 (as if the PR was just opened)
uv run python -m orchestrator.main --pr 123 --event opened

# Incremental review of PR #123 (as if a new commit was pushed)
uv run python -m orchestrator.main --pr 123 --event synchronize

# Manual dispatch path — no PR context, LLM + GitHub connectivity check only
uv run python -m orchestrator.main
```

Omitting both arguments is equivalent to `--pr 0 --event push` (the defaults).

| Argument | Required | Values | Meaning |
|---|---|---|---|
| `--pr` | no (default `0`) | any integer | Pull request number to review (the number in the GitHub URL: `github.com/you/repo/pull/123`). In CI this is filled in automatically by the GitHub Actions workflow. `0` means no PR context (manual dispatch run). |
| `--event` | no (default `push`) | see table below | The GitHub event that triggered the review. |

**Event values** (exactly what the GitHub Actions workflow can send):

| Value | Sent by | When |
|---|---|---|
| `opened` | `pull_request` trigger | PR created |
| `synchronize` | `pull_request` trigger | Commit pushed to open PR branch |
| `reopened` | `pull_request` trigger | Closed PR opened again |
| `push` | `workflow_dispatch` (manual "Run workflow") | Manual test run with no PR context |

The workflow has **no `push` trigger** — commits to branches without an open PR trigger nothing. Manual testing uses the "Run workflow" button (or `gh workflow run pr-review.yml`), which runs the `push` code path with `--pr 0`. Rapid pushes to a PR branch cancel superseded in-flight runs via `concurrency`.

Invalid values (e.g. `--pr abc`, or an unknown event) are rejected with a usage message and exit code 2. A successful run logs a startup line and exits 0.

### Required environment variables

Read from `.env` (loaded automatically) or the surrounding environment. See [`.env.example`](.env.example) and [`docs/llm-providers.md`](docs/llm-providers.md) for the full LLM provider scheme.

| Variable | Required | Meaning |
|---|---|---|
| `GITHUB_TOKEN` | yes | GitHub token with repo/PR access (`gh auth token` locally, `secrets.GITHUB_TOKEN` in CI) |
| `GITHUB_REPOSITORY` | yes | Repo under review, as `owner/repo` — set automatically by GitHub Actions |
| `MODEL_PROVIDER` | yes | `google` \| `openai` \| `claude` — selects the LLM provider; if unset (or unknown) the run logs an error and exits 1 |
| `GOOGLE_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | one, matching `MODEL_PROVIDER` | API key for the chosen provider |
| `GOOGLE_MODEL` / `OPENAI_MODEL` / `CLAUDE_MODEL` | no | Model override; a sensible default per provider is used when unset. In CI these are repo *variables* (not secrets) |

## Using the reviewer on other repositories

The reviewer reads only `GITHUB_TOKEN` / `GITHUB_REPOSITORY` from the
environment and talks to the GitHub API, so it can review any repo. To review
PRs in another repo, copy the caller workflow template
[`templates/pr-review-caller.yml`](templates/pr-review-caller.yml) into that
repo as `.github/workflows/pr-review.yml` (push to the default branch). The
template ships with commented-out security gates — fork blocking, author
allowlists, target-branch restrictions — uncomment the ones you want:

Full guide — how token/repository context flows from caller to reusable
workflow, setup steps, caveats (fork PRs, permissions, private-repo
limitations), and the alternatives considered:
[`docs/multi-repo-integration.md`](docs/multi-repo-integration.md)

Notes:
- This repo must stay **public** for the checkout in `pr-review-reusable.yml`
  to work without a PAT; a private move requires passing a PAT instead.
- `pull_request` workflows only fire from a repo's **default branch** — push the
  caller file to `main` first, then open a PR to see it run.
- The reviewer code always runs from this repo's `main` (see the reusable
  workflow's checkout `ref`). Pin to a tag to control upgrade timing.