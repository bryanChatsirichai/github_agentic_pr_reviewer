# Multi-repo integration — using the reviewer on other repositories

The reviewer can review PRs in **any repository**, not just this one. A target
("caller") repo contains a thin workflow that invokes this repo's reusable
workflow, which checks out the reviewer code here and runs it against the
caller's PR.

```text
┌──────────────────────────────────────┐
│           CALLER REPOSITORY           │
│        (the repo under review)        │
│                                       │
│     PR opened / new commit pushed     │
└───────────────────┬──────────────────┘
                    │ pull_request event
                    ▼
┌──────────────────────────────────────┐      ┌──────────────────────────────┐
│ .github/workflows/pr-review.yml      │      │  passed explicitly by caller │
│ (thin caller workflow — copy of      │      │   with:    model_provider    │
│  templates/pr-review-caller.yml)     ├─────►│   secrets: provider API key  │
└───────────────────┬──────────────────┘      └──────────────────────────────┘
                    │ jobs.review.uses: …pr-review-reusable.yml@main
                    ▼
┌──────────────────────────────────────────────────────────────────┐
│                   REVIEWER REPOSITORY (this repo)                  │
│             bryanChatsirichai/github_agentic_pr_reviewer           │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │  .github/workflows/pr-review-reusable.yml   (workflow_call)  │ │
│  │                                                               │ │
│  │   1. Checkout pinned to THIS repo @main                       │ │
│  │      (default checkout would fetch the caller's repo)         │ │
│  │   2. uv sync                                                  │ │
│  │   3. uv run python -m orchestrator.main                       │ │
│  │         --pr     github.event.pull_request.number              │ │
│  │         --event  github.event.action                           │ │
│  └──────────────────────────────┬────────────────────────────────┘ │
└─────────────────────────────────┼──────────────────────────────────┘
                                  │ runs with the CALLER's github context:
                                  │   GITHUB_TOKEN       → caller-scoped token
                                  │   GITHUB_REPOSITORY  → caller's owner/repo
                                  │   github.event.*     → caller's PR
                                  ▼
                       ┌────────────────────┐
                       │    Orchestrator    │
                       │  (LangGraph graph) │
                       └─────────┬──────────┘
                                 │ GitHub API
                                 ▼
                       ┌────────────────────┐
                       │   Review comments   │
                       │   posted on the     │
                       │   CALLER's PR       │
                       └────────────────────┘
```

## How context flows from caller to reusable workflow

A *called* workflow runs with the **caller's** `github` context. Nothing needs
to be forwarded for the GitHub side — only the LLM secrets are explicitly passed.

| Value | How it reaches the reusable workflow | What it resolves to |
|---|---|---|
| `GITHUB_TOKEN` | Inherited — read as `${{ github.token }}` | Installation token scoped to the **caller's** repo, with exactly the permissions the caller grants |
| `GITHUB_REPOSITORY` | Inherited — read as `${{ github.repository }}` | The **caller's** `owner/repo` — so the reviewer reviews the caller's PRs |
| `--pr` / `--event` | Inherited — read from `${{ github.event.* }}` | The caller's triggering PR event (number + action) |
| Provider API keys | **Explicitly passed** via the caller's `secrets:` block | Called workflows cannot read caller secrets unless forwarded |
| `MODEL_PROVIDER` | Passed via the `with: model_provider` input | Only the key matching the provider needs a value |

The reusable workflow's checkout is **pinned to this repo**
(`repository: bryanChatsirichai/github_agentic_pr_reviewer`, `ref: main`):
the default checkout would fetch the caller's repo, where `orchestrator/` does
not exist. This is why the reviewer code always runs from this repo's `main`
regardless of which repo is being reviewed.

## Setup for a new target repository

1. Copy [`templates/pr-review-caller.yml`](../templates/pr-review-caller.yml)
   to the target repo as `.github/workflows/pr-review.yml`.
2. Add the provider API key as a repo **secret** (e.g. `GOOGLE_API_KEY`).
   The unused provider keys may be omitted or left empty.
3. Push the caller file to the **default branch** — `pull_request` workflows
   only fire from workflows on the default branch.
4. Open a PR; the review run appears in the target repo's Actions tab.

Total per-repo cost: one small file, one secret. Token and repository context
come along automatically.

## Security gates

The caller template ships with commented-out gates — uncomment the ones you
want:

- **Fork blocking** — skip the review entirely for fork PRs (see caveat below).
- **Author allowlist** — only review PRs from specific authors.
- **Target-branch restriction** — only review PRs merging into specific
  branches.

## Caveats and limitations

| Constraint | Detail |
|---|---|
| Caller permissions | A called workflow **cannot elevate** permissions. The caller must grant `contents: read` + `pull-requests: write` or comment-posting fails with 403. |
| Fork PRs | On `pull_request` events from forks the auto `GITHUB_TOKEN` is **read-only** — the review can read the PR but cannot post comments. Same-repo branches work fine. |
| This repo must stay **public** | The pinned checkout uses the caller's default token; a private reviewer repo requires callers to pass a PAT for that checkout. |
| Upgrade timing | Reviewer code always runs from `main` here. Pin the caller's `uses:` to a tag to control when consumer repos pick up changes. |
| Wrong-sha pitfall | In a called workflow `github.event.pull_request.head.sha` belongs to the **caller's** PR — which is why self-review and multi-repo modes live in two separate workflow files rather than one conditional file. |

## Alternatives considered

| Approach | Setup per repo | Trade-off |
|---|---|---|
| **Reusable workflow (chosen)** | One file + one secret | Zero infra; current design |
| Composite / Docker Action | One step in a workflow | Marketable/versioned, but must fetch its own source or ship images |
| GitHub App + webhook service | Install app only | Zero per-repo files, works on fork PRs — but requires a hosted, always-on service and app credentials |
| Org required workflows | None | Only available to organizations, not personal-account repos |

The GitHub App is the natural upgrade if fork-PR support or clean bot
attribution becomes important; the reviewer core (`orchestrator/`, `agents/`)
stays identical in every approach — only how events reach it changes.
