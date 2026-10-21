# Agentic PR Reviewer

An agentic pull-request review system: GitHub PR events trigger LLM-driven reviewers with memory of previous findings, incremental diffs, and a critic/judge loop.

Design: [`docs/agentic-pr-review-system-design.v1.md`](docs/agentic-pr-review-system-design.v1.md)
Roadmap: [`docs/implementation-roadmap.md`](docs/implementation-roadmap.md)

## Quickstart

Requires [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env   # fill in GITHUB_TOKEN, AI_API_KEY, MODEL_NAME
uv sync                # install deps (Python 3.12 managed by uv)
uv run python orchestrator/main.py --pr 123 --event opened
uv run pytest          # test suite
```

## Usage

```bash
uv run python orchestrator/main.py --pr <PR_NUMBER> --event <EVENT>
```

| Argument | Required | Values | Meaning |
|---|---|---|---|
| `--pr` | yes | any integer | Pull request number to review (the number in the GitHub URL: `github.com/you/repo/pull/123`). In CI this is filled in automatically by the GitHub Actions workflow. Defaults to `0` for smoke-test runs. |
| `--event` | yes | see table below | The GitHub event that triggered the review. |

**Event values** (exactly what the GitHub Actions workflow can send):

| Value | Sent by | When |
|---|---|---|
| `opened` | `pull_request` trigger | PR created |
| `synchronize` | `pull_request` trigger | Commit pushed to open PR branch |
| `reopened` | `pull_request` trigger | Closed PR opened again |
| `push` | `push` trigger (via `\|\| 'push'` fallback) | Any branch push with no PR |

Invalid values (e.g. `--pr abc`, a missing argument, or an unknown event) are rejected with a usage message and exit code 2. A successful run logs a startup line and exits 0.