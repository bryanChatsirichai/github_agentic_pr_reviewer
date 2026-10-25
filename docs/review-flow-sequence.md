# Review Flow — Sequence Diagram

> **What this shows:** the end-to-end sequence when a GitHub Action runs a
> review — from PR event to posted `🤖 AI Review` comment, as of Step 4
> (stateless first review; memory, incremental diff, and re-check arrive in
> later steps).
>
> **Companion to:** `implementation-roadmap.md` (Step 4) and
> `agentic-pr-review-system-design.v1.md` (§29 first implementation loop).

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Developer
    participant GH as GitHub
    participant CI as GitHub Actions<br/>(pr-review.yml)
    participant M as orchestrator/main.py
    participant GC as clients/github.py
    participant RV as Reviewer<br/>(agents/reviewer_agents/factory.py)
    participant LLM as agents/llm.py → Provider API

    Dev->>GH: open PR / push commit<br/>(opened · synchronize · reopened)
    GH->>CI: trigger workflow<br/>(concurrency: 1 run per PR,<br/>newer cancels older)
    CI->>CI: checkout → setup-uv (Python 3.12)<br/>→ uv sync
    CI->>M: uv run python -m orchestrator.main<br/>--pr N --event opened

    Note over M: parse_args → verify token
    M->>GC: verify_connection()
    GC->>GH: GET repo (token check)
    GH-->>GC: 200 OK

    Note over M: run_review(N)
    M->>M: resolve_model()<br/>(MODEL_PROVIDER + key from env)
    M->>GC: get_pull_request(N)
    M->>GC: get_pr_files(N) + get_pr_diff(N)
    M->>GC: get_existing_review_comments(N)
    GH-->>GC: PR metadata, files, patches,<br/>prior 🤖 comments
    GC-->>M: PRContext (diff, changed_files, summary)

    M->>RV: build_reviewer("logic-reviewer")
    Note over RV: loads prompts/logic-reviewer.md<br/>+ get_llm() → init_chat_model

    M->>RV: review_findings(PRContext)
    RV->>LLM: invoke([System(prompt),<br/>Human(rendered context)])
    LLM-->>RV: JSON array of findings
    RV->>RV: parse_findings()

    alt output unparseable
        RV->>LLM: retry once — error fed back:<br/>"Respond with ONLY the JSON array"
        LLM-->>RV: corrected JSON
        Note over RV: 2nd failure → raises<br/>FindingParseError → main exits 1<br/>(Action fails, nothing posted)
    end

    RV-->>M: list[Finding] (validated)

    M->>M: render_comment(findings)<br/>("## Review Findings" …)
    M->>GC: post_review_comment(N, body)
    Note over GC: prepends "🤖 AI Review" MARKER
    GC->>GH: POST issue comment
    GH-->>Dev: bot comment on PR

    M-->>CI: exit 0
    CI->>CI: run green ✅
```

## Step-by-step trace

1. **Developer** opens a PR or pushes a commit — `pull_request` event with
   type `opened`, `synchronize`, or `reopened`
   (`.github/workflows/pr-review.yml`).
2. **GitHub Actions** starts the `pr-review` workflow. The `concurrency`
   group (`pr-review-<PR number>`) means a newer push cancels any
   in-flight run for the same PR.
3. **Job steps**: checkout → `setup-uv` with Python 3.12 + cache →
   `uv sync` → `python -m orchestrator.main --pr N --event opened`. Env is
   injected here: `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, `MODEL_PROVIDER`
   (repo variable), and the provider API key secrets.
4. **`main()`** (`orchestrator/main.py`): parses args, then
   `verify_connection()` hits GitHub once to fail fast on a bad token.
   `--pr 0` (manual dispatch) short-circuits to a clean no-op before any
   of this.
5. **`run_review()`** starts: `resolve_model()` validates LLM config
   *before* any GitHub write.
6. **`build_context()`** fetches everything through the client module —
   PR metadata, changed files with patches, the unified diff, and prior
   bot comments — and packs it into a `PRContext`.
7. **`build_reviewer("logic-reviewer")`** loads the system prompt from
   the `.md` file and constructs the chat model via `get_llm()` /
   `init_chat_model`.
8. **`review_findings()`** invokes the LLM with
   `[SystemMessage(prompt), HumanMessage(rendered PR context)]` — the
   single real provider call.
9. **Parse + validate** the reply into `list[Finding]`. On a
   `FindingParseError`, exactly one retry with the error message fed back
   to the model; a second failure propagates up and the run exits 1 — no
   empty "clean" review is ever posted.
10. **`render_comment()`** turns findings into the `## Review Findings`
    markdown body; **`post_review_comment()`** prepends the
    `🤖 AI Review` marker (so future runs can find it) and POSTs it.
11. **Exit 0** → the Action shows green and the bot comment appears on
    the PR. Exit 1 (config or parse error) → red Action, nothing posted.

## Known limitations (by design, for now)

- The flow ignores `--event`: `opened` and `synchronize` run the identical
  stateless review. Differentiating them (incremental diff + re-check of
  previous findings) is what roadmap Steps 7–9 add.
- Every run re-reports still-open issues from scratch — memory doesn't
  exist yet (Step 6).
