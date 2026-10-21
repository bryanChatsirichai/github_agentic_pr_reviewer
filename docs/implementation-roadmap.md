# Implementation Roadmap — Setup to Prototype

> **Status:** Draft
> **Companion to:** `agentic-pr-review-system-design.v1.md`
> **Purpose:** Break the design into small, modular, verifiable steps. Each step is self-contained and can only depend on earlier steps. Finish a step → verify it → move on. No step requires hopping between unrelated parts of the codebase.

## How to read this roadmap

Each step follows the same shape:

| Field | Meaning |
|---|---|
| **Goal** | What the step achieves in one sentence |
| **Depends on** | Which steps must be done first (and only those) |
| **Creates** | The folders and files this step adds — nothing else appears until its own step |
| **Tasks** | Concrete units of work, each independently checkable |
| **Done when** | Objective verification — if you can't demonstrate this, the step isn't finished |

General rules:

- One step = one PR-sized change. If a step feels too big, split it before starting.
- Never start a step whose dependency isn't verified.
- Every step keeps the project runnable end-to-end. No long-lived broken state.
- Step 0 is **general setup only** (project, tooling, dependencies). Every specific folder and file is created by the step that needs it.

---

## Phase A — Setup

### Step 0 — General project setup

**Goal:** A working Python project with tooling and configuration in place — no application code beyond a trivial entry point.

**Depends on:** nothing.

**Creates:**

```text
pyproject.toml          # uv-managed project: pydantic, python-dotenv, pytest
uv.lock
.gitignore             # .env, __pycache__/, .venv/, memory state
.env.example           # GITHUB_TOKEN, AI_API_KEY, MODEL_NAME placeholders
README.md              # quickstart + link to docs/
orchestrator/main.py   # parses --pr / --event, logs a startup line, exits 0
tests/                 # empty pytest package (baseline)
```

**Tasks:**
1. Init the Python project with uv: `uv init` → `uv add pydantic python-dotenv pytest` (creates `pyproject.toml` + `uv.lock`), plus `.gitignore`, `.env.example`, README quickstart.
2. Minimal entry point: parse `--pr` and `--event` args, log them, exit cleanly.
3. Empty pytest package so `pytest` runs green with zero tests.
4. Keep dependencies minimal — `uv add` a package only at the step that needs it.

**Done when:** `uv sync && uv run python orchestrator/main.py --pr 123 --event opened` prints a startup line and exits 0, and `uv run pytest` passes with no tests.

---

### Step 1 — GitHub Actions trigger

**Goal:** A PR event on this repo boots the Python entry point in CI.

**Depends on:** Step 0.

**Creates:**

```text
.github/workflows/pr-review.yml
```

**Tasks:**
1. Workflow: `on: pull_request: [opened, synchronize, reopened]`.
2. Job: checkout → setup Python 3.12 (or use `astral-sh/setup-uv`, which handles both) → `uv sync` → `uv run python orchestrator/main.py`, with PR number and event name passed via env.
3. Provide `GITHUB_TOKEN` / `AI_API_KEY` from secrets (unused for now, wired for later steps).

**Done when:** Open a draft PR against this repo; the Actions run goes green and the log shows the startup line with the correct PR number and event type.

---

## Phase B — Minimal end-to-end review (stateless)

### Step 2 — GitHub client module

**Goal:** All GitHub reads/writes the rest of the system needs, behind one module, testable offline.

**Depends on:** Step 0.

**Creates:**

```text
orchestrator/github_client.py
tests/test_github_client.py
tests/fixtures/            # one real-shaped PR fixture JSON
```
Also adds `pygithub` via `uv add pygithub`.

**Tasks:**
1. `get_pull_request(pr_number)` → PR metadata (title, body, base/head branch + SHA).
2. `get_pr_files(pr_number)` → changed files with patch blobs.
3. `get_pr_diff(pr_number)` → unified diff.
4. `get_existing_review_comments(pr_number)` → prior comments from our bot (identified by a marker like `🤖 AI Review`).
5. `post_review_comment(pr_number, body)` → single summary comment.
6. Client is injected so tests can mock it; fixture covers one realistic PR.

**Done when:** Unit tests with mocked client pass for all six functions, and a manual run against a real test PR returns the correct diff.

---

### Step 3 — PR context builder

**Goal:** Turn raw GitHub data into one context object every agent consumes.

**Depends on:** Step 2.

**Creates:**

```text
orchestrator/context.py
models/pr_context.py       # PRContext: pr number, SHAs, diff, changed files, prev findings
tests/test_context.py
```

**Tasks:**
1. `build_context(pr_number)` → combines PR metadata + diff + changed files + existing bot comments into `PRContext`.
2. Compute a stable context summary (diff size, file list) so later steps can truncate huge PRs.
3. For now `previous_findings` stays an empty list — memory doesn't exist yet.

**Done when:** `build_context` on the fixture PR produces a fully populated `PRContext`, and a test asserts every field is non-empty.

---

### Step 4 — First LLM review (no memory, no graph)

**Goal:** The complete design §29 "first implementation" loop: PR → LLM → comment.

**Depends on:** Step 3.

**Creates:**

```text
models/finding.py          # Finding + statuses — the contract every later step uses
models/review_result.py
agents/prompts/logic-reviewer.md
agents/factory.py          # build a reviewer from prompt + model
orchestrator/memory.py     # no-op stub only, keeps the main flow shape final
```
Also adds `langchain` / `langchain-core` via `uv add langchain langchain-core`.

**Tasks:**
1. Finding model: id, status, severity, file, line, title, description, confidence — plus the status enum from design §20.
2. `agents/factory.py`: `build_reviewer(name)` → model + system prompt loaded from the `.md` file, no tools yet.
3. Prompt: paste PR context and require **structured JSON findings output** matching the Finding model.
4. Parse and validate LLM output into `list[Finding]` (reject/repair malformed JSON, one retry).
5. `main.py`: build context → call reviewer → render findings into a `🤖 AI Review` comment → post via `post_review_comment`.
6. `memory.py`: `load_memory()` returns empty state, `save_memory()` is a no-op.

**Done when:** Open a PR with an intentionally broken change (e.g., unvalidated input); the bot posts one comment containing at least the planted issue as a structured finding.

---

### Step 5 — Re-review on every commit (still stateless)

**Goal:** `synchronize` events produce a fresh review — proves the repeat half of the loop works, even without memory.

**Depends on:** Step 4, Step 1.

**Creates:** nothing — verification step only.

**Tasks:**
1. Push a new commit to the test PR.
2. Confirm a new review runs and posts, and that the old comment isn't duplicated or deleted.

**Done when:** Two bot comments exist on the PR, one per commit, each reflecting the diff at that point. (Yes, it re-reports fixed issues — that's the problem Step 7 solves.)

---

## Phase C — Memory

### Step 6 — Finding store (JSON memory)

**Goal:** Persist per-PR review state that survives workflow runs.

**Depends on:** Step 4 (for the Finding model shape).

**Creates:**

```text
memory/                    # runtime state storage, gitignored (pr-<n>.json files)
tests/test_memory.py
```
Implement the real `orchestrator/memory.py` (replacing the Step 4 stub).

**Tasks:**
1. `load_memory(pr_number)` → state (previous reviewed SHA + findings list) from `memory/pr-<n>.json`; empty state if absent.
2. `save_memory(pr_number, state)` → atomic write (write temp file, rename).
3. Finding lifecycle transitions as an explicit allowed-moves table (design §20): `new→open`, `open→resolved/rejected/reopened`, `resolved→verified/closed`; illegal transition raises.
4. Finding ID generator (`F001`, `F002`, …) that continues from the previous state's max ID.

**Done when:** Unit tests cover round-trip save/load, all legal transitions, rejection of illegal transitions, and ID continuation. No LLM or GitHub involved.

---

### Step 7 — Wire memory into the review flow

**Goal:** The agent becomes stateful: loads previous findings before reviewing, saves after posting.

**Depends on:** Step 6 (which needs Step 4).

**Creates:**

```text
agents/prompts/logic-reviewer.md   # gains a "previous findings" section
```
(Plus edits in `main.py` / `memory.py` — no new modules.)

**Tasks:**
1. Main flow: `load_memory` → include previous open findings in the reviewer prompt → `save_memory` with new findings appended (`new→open`).
2. Reviewer prompt gains a "previous findings" section with instruction: re-check each against the new diff.
3. Comment rendering shows two sections: "Previous findings" and "New findings" (design §14 output shape).
4. Store `previous_sha` after each run.

**Done when:** On the test PR: commit 1 posts F001; developer fixes it and pushes; commit 2's comment lists F001 with a status (resolved or still-open — correctness of *which* comes later in Step 9), and `memory/pr-N.json` on disk matches the comment.

---

## Phase D — Incremental review & finding re-check

### Step 8 — Incremental diff

**Goal:** Review only `previous_sha → head_sha` on synchronize instead of the whole PR.

**Depends on:** Step 7.

**Creates:**

```text
orchestrator/diff.py
tests/test_diff.py
```

**Tasks:**
1. `full_diff(base_sha, head_sha)` — what Phase B used.
2. `incremental_diff(previous_sha, head_sha)` — new path when `previous_sha` exists and base hasn't changed (PR rebased → fall back to full diff; detect by comparing base SHA stored in memory).
3. `context.py` picks the right diff and labels which one it used (put it in `PRContext` so later agents know).

**Done when:** Test PR with three commits: third run's `PRContext.diff` contains only commit-3 changes; after a rebase, it correctly falls back to the full diff.

---

### Step 9 — Finding re-check pass (the core differentiator)

**Goal:** Previous findings are explicitly re-checked and their statuses updated — including regression detection.

**Depends on:** Step 8.

**Creates:**

```text
agents/prompts/recheck.md
orchestrator/recheck.py
tests/test_recheck.py
```

**Tasks:**
1. Dedicated re-check agent call: for each previous open/reopened finding, given the finding + current file content + incremental diff, output `resolved | still_open | reopened` + one-line justification.
2. Apply transitions through the Step 6 lifecycle table; a finding whose problem reappears in a later commit goes `resolved → reopened`.
3. Comment format from design §14: previous findings with ✅/🟡 markers, then new findings.
4. If a finding is resolved, try to link/echo the corresponding GitHub comment (bot-edit the summary comment rather than posting a new one, or reply — pick one, note the choice).

**Done when:** Full scenario test on a real PR: commit 1 plants F001 → commit 2 fixes it (F001 → resolved in memory + comment) → commit 3 reintroduces it (F001 → reopened). This is design §21 proven end-to-end.

---

## Phase E — Agentic structure (the prototype)

### Step 10 — LangGraph graph skeleton

**Goal:** Replace the linear main flow with an explicit graph, one node per stage — same behavior, new skeleton.

**Depends on:** Step 9 (behavior must be complete and green *before* restructuring).

**Creates:**

```text
graph/review_graph.py
graph/__init__.py
```
Also adds `langgraph` via `uv add langgraph`. `main.py` shrinks to: parse args → invoke the graph.

**Tasks:**
1. Nodes: `load_memory` → `build_diff` → `review` → `recheck` → `judge_placeholder` (passthrough) → `update_memory` → `post_review`.
2. State = a typed dict wrapping `PRContext` + intermediate results.
3. No behavior change — outputs identical to Step 9's.

**Done when:** Re-running the Step 9 scenario through the graph produces identical memory state and comment content. Refactor verified by equivalence, not by eye.

---

### Step 11 — Tools layer (agents can investigate)

**Goal:** Reviewer agents can call tools instead of trusting the diff alone.

**Depends on:** Step 10.

**Creates:**

```text
tools/git_tools.py         # git diff / log / show / blame (read-only subprocess wrappers)
tools/repo_tools.py        # read_file(path, ref), search_code(query)
tools/__init__.py
```

**Tasks:**
1. Implement the git and repo tools as plain functions (MCP-able later).
2. Bind tools to the reviewer via the agent factory; add the "use tools to verify before reporting" instruction from design §19.
3. Enforce `MAX_TOOL_CALLS` (design §24) in the tool-binding loop.

**Done when:** A test PR where the bug is only visible outside the diff (e.g., caller passes bad input defined in another file) gets correctly found — impossible without tools.

---

### Step 12 — Critic (verification loop)

**Goal:** Suspicious findings get challenged with evidence before reaching GitHub.

**Depends on:** Step 11.

**Creates:**

```text
agents/prompts/critic.md
```
(Plus a new node + loop edge in `graph/review_graph.py`.)

**Tasks:**
1. Critic node: for each finding, decide `confirmed | uncertain | rejected` + evidence note, using tools.
2. Bounded loop (design §11 + §24): `MAX_REVIEW_ITERATIONS = 3` — uncertain findings go back to the reviewer with the critic's doubt, at most N times.
3. Graph edge: after critic, route `has_uncertain and iterations_left → review` else `→ judge`.
4. Rejected findings are kept in memory with status `rejected` (audit trail, design §5) but not posted.

**Done when:** A PR with a plausible-but-false issue (e.g., "missing null check" where the caller already guarantees non-null — visible via tools) produces no GitHub comment but a `rejected` finding in memory with the evidence recorded.

---

### Step 13 — Judge + confidence policy

**Goal:** Final posting decision is centralized and threshold-driven.

**Depends on:** Step 12.

**Creates:**

```text
agents/prompts/judge.md
```
(Plus the real judge node replacing the Step 10 placeholder.)

**Tasks:**
1. Judge node: takes confirmed + uncertain findings, outputs final per-finding verdict + adjusted confidence.
2. Confidence policy from design §23 as config constants (`REPORT_THRESHOLD=0.90`, `SUGGESTION_THRESHOLD=0.75`).
3. Rendering: ≥0.90 normal, 0.75–0.89 labeled "suggestion / needs verification", <0.75 silently recorded in memory only.
4. Decide comment shape: summary comment vs. inline review comments on specific lines (design §30) — implement one, note the other as future.

**Done when:** A mixed PR (one clear bug, one borderline style-adjacent issue, one vague concern) yields: one normal comment, one "suggestion"-labeled comment, and one finding present in memory but absent from GitHub.

---

### Step 14 — Specialized reviewers

**Goal:** Logic, Security, and Testing reviewers run in parallel; results merge into the critic.

**Depends on:** Step 13.

**Creates:**

```text
agents/prompts/security-reviewer.md
agents/prompts/testing-reviewer.md
```

**Tasks:**
1. Security + Testing reviewer prompts from design §9 checklists.
2. Fan-out node: run all three reviewers (parallel or sequential — sequential is fine for the prototype, note the choice), tag findings with `source_reviewer`.
3. Merge function: dedupe overlapping findings (same file+line+category) keeping the higher confidence.
4. Re-check pass (Step 9) routes each previous finding back to its original reviewer.

**Done when:** A PR containing one logic bug, one security issue (e.g., injection), and one missing test produces three findings from three different `source_reviewer` values in one review comment — the design §27 "final prototype" experience.

---

## After the prototype

Deliberately out of scope until Step 14 is green:

- Performance Reviewer (fourth agent)
- MCP tool interface (tools are plain functions until this matters)
- Testing tools that execute repo code (`npm test` etc.) — security review needed first
- Reusable workflow / GitHub App for multi-repo (design §4 progression Phase 3–4)
- SQLite/PostgreSQL memory
- `/ai-review` commands, auto-fix, PR metrics

## Dependency graph

```text
Step 0
  ├──► Step 1    (Actions trigger — independent lane)
  ├──► Step 2 ──► Step 3 ──► Step 4 ──► Step 5
  │                             │
  │                             ▼
  └────────────────────► Step 6
                          │
                          ▼
                          Step 7
                          │
                          ▼
                          Step 8
                          │
                          ▼
                          Step 9
                          │
                          ▼
                          Step 10
                          │
                          ▼
                          Step 11
                          │
                          ▼
                          Step 12
                          │
                          ▼
                          Step 13
                          │
                          ▼
                          Step 14
```

Read it as:

- **Step 0 → then three lanes:** Step 1 (Actions) and Step 2→3 (GitHub + context) are independent of each other.
- **Step 6** (memory) depends on Step 4 for the Finding model shape, but is otherwise independent of Steps 2–3's details.
- **Steps 7 → 14 are strictly sequential** — each builds directly on the previous one's verified behavior.
