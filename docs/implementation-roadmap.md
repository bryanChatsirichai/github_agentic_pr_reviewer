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

## Progress

- [x] Step 0 — General project setup
- [x] Step 1 — GitHub Actions trigger
- [x] Step 2 — GitHub client module
- [x] Step 3 — PR context builder
- [x] Step 4 — First LLM review (no memory, no graph)
- [x] Step 5 — Re-review on every commit (still stateless)
- [x] Step 6 — Finding store (JSON memory)
- [x] Step 7 — Wire memory into the review flow
- [ ] Step 8 — Incremental diff
- [ ] Step 9 — Finding re-check pass
- [ ] Step 9b — Inline review threads
- [ ] Step 10 — LangGraph graph skeleton
- [ ] Step 11 — Tools layer
- [ ] Step 12 — Critic (verification loop)
- [ ] Step 13 — Judge + confidence policy
- [ ] Step 14 — Specialized reviewers

---

## Phase A — Setup

### [x] Step 0 — General project setup

**Goal:** A working Python project with tooling and configuration in place — no application code beyond a trivial entry point.

**Depends on:** nothing.

**Creates:**

```text
pyproject.toml          # uv-managed project: pydantic, python-dotenv, pytest
uv.lock
.gitignore             # .env, __pycache__/, .venv/, memory state
.env.example           # GITHUB_TOKEN, MODEL_PROVIDER + per-provider key/model placeholders
README.md              # quickstart + link to docs/
orchestrator/main.py   # parses --pr / --event, logs a startup line, exits 0
tests/                 # empty pytest package (baseline)
```

**Tasks:**
1. Init the Python project with uv: `uv init` → `uv add pydantic python-dotenv pytest` (creates `pyproject.toml` + `uv.lock`), plus `.gitignore`, `.env.example`, README quickstart.
2. Minimal entry point: parse `--pr` and `--event` args, log them, exit cleanly.
3. Empty pytest package so `pytest` runs green with zero tests.
4. Keep dependencies minimal — `uv add` a package only at the step that needs it.

**Done when:** `uv sync && uv run python -m orchestrator.main --pr 123 --event opened` prints a startup line and exits 0, and `uv run pytest` passes with no tests.

---

### [x] Step 1 — GitHub Actions trigger

**Goal:** A PR event (opened / new commit / reopened) boots the Python entry point in CI; a manual dispatch provides a no-commit test path.

**Depends on:** Step 0.

**Creates:**

```text
.github/workflows/pr-review.yml
```

**Tasks:**
1. Workflow: `on: pull_request: [opened, synchronize, reopened]` (real review path — pushes to branches without an open PR trigger nothing) + `workflow_dispatch` (manual testing) + a `concurrency` group per PR that cancels superseded in-flight runs.
2. Job: checkout → setup Python 3.12 (or use `astral-sh/setup-uv`, which handles both) → `uv sync` → `uv run python -m orchestrator.main`, with PR number and event name passed via env.
3. Provide `GITHUB_TOKEN` / the provider API key (`GOOGLE_API_KEY` etc.) from secrets (unused for now, wired for later steps).

**Done when:** Open a draft PR against this repo and push a commit to it (review runs, correct PR number and event type in logs); trigger a manual dispatch run (test path, `--pr 0`); both go green.

---

## Phase B — Minimal end-to-end review (stateless)

### [x] Step 2 — GitHub client module

**Goal:** All GitHub reads/writes the rest of the system needs, behind one module, testable offline.

**Depends on:** Step 0.

**Creates:**

```text
clients/github.py         # (originally orchestrator/github_client.py — moved in the Step 3 refactor)
tests/clients/test_github_client.py   # (originally tests/test_github_client.py — moved in Step 7)
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

Note: this module is **orchestrator plumbing, not agent tools**. The pipeline calls it deterministically every run (Step 3 packs the results into `PRContext` for the agents). Agents never fetch PR data or post comments themselves — see design "Orchestrator data vs. agent tools". Agent tools arrive in Step 11 and work against the repo on disk.

**Done when:** Unit tests with mocked client pass for all six functions, and a manual run against a real test PR returns the correct diff.

---

### [x] Step 3 — PR context builder

**Goal:** Turn raw GitHub data into one context object every agent consumes.

**Depends on:** Step 2.

**Creates:**

```text
orchestrator/context_builder.py  # build_context — combines client data into PRContext
models/pr_context.py       # PRContext: pr number, SHAs, diff, changed files, prev findings
models/context_summary.py  # ContextSummary (split out of pr_context.py on 2026-10-28)
tests/orchestrator/test_context_builder.py   # (originally tests/ — moved in Step 7)
```

**Tasks:**
1. `build_context(pr_number)` → combines PR metadata + diff + changed files + existing bot comments into `PRContext`.
2. Compute a stable context summary (diff size, file list) so later steps can truncate huge PRs.
3. For now `previous_findings` stays an empty list — memory doesn't exist yet.

**Done when:** `build_context` on the fixture PR produces a fully populated `PRContext`, and a test asserts every field is non-empty.

---

### [x] Step 4 — First LLM review (no memory, no graph)

**Goal:** The first implementation loop (design section 28): PR → LLM → comment.

**Depends on:** Step 3.

**Creates:**

```text
models/finding.py          # Finding + statuses — the contract every later step uses
models/review_result.py    # ReviewResult (was models/review.py until 2026-10-28)
agents/reviewer_agents/prompts/logic-reviewer.md
agents/reviewer_agents/factory.py          # build a reviewer from prompt + model
agents/reviewer_agents/parser.py           # JSON repair + validation into ReviewResult
orchestrator/memory.py     # no-op stub only, keeps the main flow shape final
```
Also adds `langchain` / `langchain-core` via `uv add langchain langchain-core`.

**Tasks:**
1. Finding model: id, status, severity, file, line, title, description, confidence — plus the status enum from design section 11.
2. `agents/reviewer_agents/factory.py`: `build_reviewer(name)` → model + system prompt loaded from the `.md` file, no tools yet.
3. Prompt: paste PR context and require **structured JSON findings output** matching the Finding model.
4. Parse and validate LLM output into `list[Finding]` (reject/repair malformed JSON, one retry).
5. `main.py`: build context → call reviewer → render findings into a `🤖 AI Review` comment → post via `post_review_comment`.
6. `memory.py`: `load_memory()` returns empty state, `save_memory()` is a no-op.

**Done when:** Open a PR with an intentionally broken change (e.g., unvalidated input); the bot posts one comment containing at least the planted issue as a structured finding.

**Verified (2026-10-25):** on PR #4 instead of a planted bug — the bot reported three real issues as structured findings (uncaught `GithubException`, bracket-in-prose JSON recovery, manual-dispatch error path), each confirmed by hand and fixed with a regression test.

---

### [x] Step 5 — Re-review on every commit (still stateless)

**Goal:** `synchronize` events produce a fresh review — proves the repeat half of the loop works, even without memory.

**Depends on:** Step 4, Step 1.

**Creates:** nothing — verification step only.

**Tasks:**
1. Push a new commit to the test PR.
2. Confirm a new review runs and posts, and that the old comment isn't duplicated or deleted.

**Done when:** Two bot comments exist on the PR, one per commit, each reflecting the diff at that point. (Yes, it re-reports fixed issues — that's the problem Step 7 solves.)

**Verified (2026-10-25):** on PR #4 — each `synchronize` push (`3fbe67a` → `2e98f07` → `ba55883` → `6b8ddb0` → `baaf6b4`) produced its own new bot comment stamped with that head SHA, earlier comments untouched (the bot only calls `create_issue_comment`; two comments were deleted manually by the author, not the bot). Findings tracked the code — issues fixed in `ba55883` stopped being reported. Observed: a job re-run at the same SHA (`baaf6b4`) posts a second comment — handled by Step 7 task 6 / Step 8 `noop`.

---

## Phase C — Memory

### [x] Step 6 — Finding store (state in bot PR comments)

**Goal:** Persist per-PR review state across workflow runs — which start on a fresh runner every time — by embedding it in each bot comment.

**Depends on:** Step 4 (for the Finding model shape).

**Storage decision (2026-10-25):** a local `memory/pr-<n>.json` does not survive between Actions runs (fresh runner; `memory/` is gitignored), and the reusable workflow runs in other people's repos. Rejected alternatives: committing state to the repo / an orphan branch (needs `contents: write` on callers' repos), `actions/cache` (evicted after 7 days unused, 10 GB/repo cap, scoped to `refs/pull/N/merge`), artifacts (90-day default retention, awkward lookup of the previous run). State therefore lives in the bot's own PR comments — no extra infra or permissions. See design section 10.

**Creates:**

```text
orchestrator/memory.py     # load_memory / save_memory (replaces the Step 4 stub)
models/review_state.py     # ReviewState pydantic model
models/stored_finding.py   # StoredFinding + ID helpers (split out of review_state.py on 2026-10-28)
tests/orchestrator/memory/test_memory.py   # (originally tests/ — moved in Step 7)
```

**Tasks:**
1. State model: `schema_version`, `reviewed_sha`, `base_ref`, `base_merge_sha` (merge-base with the target branch at review time), `next_id`, `file_shas` (`{filename: blob_sha}` from the PR files API), and `findings[id, status, rejected_by, severity, file, line, title, description (≤300 chars), snippet, thread_id (null until Step 9b's inline threads)]`. The orchestrator cuts `snippet` from the head file around `line` — the LLM is not trusted to copy it. Step 6 provides the pure cutter `cut_snippet(file_text, line, context=3)` in `memory.py` (`line ± 3`, clamped at file ends, `""` past EOF, CRLF normalised to LF); Step 7 task 1 is the first caller.
2. `render_state_block(state)` → `<!-- ai-review-state:v1 {json} -->` appended to the comment body. After `json.dumps`, escape `<`/`>` as `<`/`>` (still valid JSON) so a snippet containing `-->` cannot close the HTML comment early. Cap the block at ~20k chars (comment bodies max out at 65,536), dropping the oldest `closed`/`rejected` findings first.
3. `load_memory(pr_number)` scans issue comments newest-first and returns the first one that (a) is authored by the configured bot login (default `github-actions[bot]`), (b) contains `MARKER`, and (c) has a state block that passes pydantic validation. A corrupt newest block falls back to an older valid one (so IDs don't restart at F001); none found → empty state (= full review). Treat the parsed state as untrusted.
4. `save_memory` is not a separate write: state is saved only by a successful `post_review_comment` that carries the block.
5. Finding lifecycle as an explicit allowed-moves table (design section 11): `new→open`, `open→resolved|rejected`, `resolved→reopened|verified`, `reopened→resolved|rejected`, `verified→closed`; illegal transition raises. `rejected_by: critic | developer` records who rejected it.
6. Finding ID generator (`F001`, `F002`, …) continuing from `next_id`.

**Done when:** Unit tests cover round-trip through a rendered comment body, a snippet containing `-->` surviving, another `github-actions[bot]` comment without a state block being skipped, a corrupt newest block falling back to an older one, all legal transitions, rejection of illegal ones, ID continuation, and `cut_snippet` (window, clamping at file ends, line past EOF, CRLF). No LLM or real GitHub involved.

**Verified (2026-10-26):** `uv run pytest` — 206 passed (`test_memory.py`, `test_review_state.py`, `test_lifecycle.py`, `test_finding_ids.py`, `test_snippet.py` cover every item above). State is loaded and carried forward unchanged in `run_review`; filling it (new findings, snippets, SHAs) is Step 7.

---

### [x] Step 7 — Wire memory into the review flow

**Goal:** The agent becomes stateful: loads previous findings before reviewing, saves after posting.

**Depends on:** Step 6 (which needs Step 4).

**Creates:**

```text
agents/reviewer_agents/prompts/logic-reviewer.md   # gains a "previous findings" section
```
(Plus edits in `main.py` / `memory.py` / `clients/github.py` and their tests — no new modules. Client changes: new `get_file_content`, new `get_merge_base_sha`, and `get_pr_files` gains a `sha` field per file.)

**Tasks:**
1. Main flow: `load_memory` → include previous findings in the reviewer prompt → new findings appended (`new→open`) → state block written into the posted comment. When filing a new finding, fill its `snippet` with `cut_snippet` (Step 6) on the head file, fetched by a new client fn `get_file_content(path, ref=head_sha)` — once per file with findings, not per changed file. This must ship with Step 7: findings saved with an empty snippet can never be re-checked properly by Step 9.
2. Reviewer prompt gains a "previous findings" section (ID, file, line, title, status) with instruction: do not re-report an issue already listed. The reviewer does **not** output status verdicts for previous findings — that is Step 9's dedicated re-check. In Step 7 previous findings are carried forward with their stored status.
3. Dedupe reviewer output against stored findings before assigning new IDs — otherwise every full-diff fallback re-reports old issues under new IDs. A reviewer finding is a duplicate of a stored one when it is in the same file and either (a) the normalised titles are equal (lowercase, punctuation stripped, whitespace collapsed), or (b) the line is within ±3 of the stored line **and** the titles are similar (`difflib.SequenceMatcher` ratio ≥ 0.6). Snippets are not compared: the snippet is cut around whatever line the LLM reports, so a 2-line drift breaks an exact match. Line proximity alone is not enough either — two different issues on adjacent lines must not merge, since hiding a real finding is worse than a duplicate. Rewordings at a drifted line will still slip through; accept that for the prototype (Step 14's merge revisits dedupe).
4. Comment rendering shows two sections: "Previous findings" and "New findings" (design section 14 output shape).
5. `reviewed_sha` / `base_merge_sha` / `file_shas` updated in the state after each successful post. Two client changes:
   - `get_pr_files` returns each file's blob `sha` (it currently doesn't) → `file_shas`.
   - New `get_merge_base_sha(base_ref, head_sha)` → `repo.compare(base_ref, head_sha).merge_base_commit.sha` → `base_merge_sha`. (Step 8 reuses the same compare call for its decision rule.)

   `reopened` events take the same stateful path as `synchronize` (comments survive while the PR is closed; the head may have moved).
6. Skip posting when `head_sha == reviewed_sha` (job re-run at the same SHA) — log it and exit 0 without posting. This is the **only** skip rule in Step 7. The design section 19 rule "no status changed and no new findings → don't post" moves to Step 9: without a re-check pass no status can change, so applying it here would suppress every follow-up comment that has no new findings (including the one the Done-when check below needs).

**Done when:**
- Unit tests (mocked client, no LLM): `get_file_content`, `get_merge_base_sha`, and the `sha` field in `get_pr_files`; dedupe — exact-title match, near-line similar-title match, adjacent-line different-title *not* merged, different-file *not* merged; new findings get continuing IDs and a non-empty snippet; same-SHA run posts nothing; state SHAs updated after a post.
- On the test PR: commit 1 posts F001; developer fixes it and pushes; commit 2's comment lists F001 under "Previous findings" with its stored status (`open` — status updates are Step 9) and F001 is not re-filed under a new ID; the hidden state block in commit 2's comment matches its visible content, with `reviewed_sha` = commit 2's SHA.

**Verified (2026-10-27):** `uv run pytest` passed with 204 tests. The live check ran on the Step 7 PR (branch `step-7/wire-memory-into-review-flow`):

- The run at `0c5c752` listed F001–F006 (filed by an earlier run) under "Previous findings" as `open`, and filed one new finding, F007 (`next_id` 8).
- The next push, `9b54dfe`, listed F001–F007 as `open` with 0 new findings. `next_id` stayed at 8, so nothing was re-filed.
- In both comments, the hidden block matches the visible list, and `reviewed_sha` equals that comment's head SHA.
- F001–F003 describe gaps that Step 7 itself fixed. Both comments were produced by the fixed code and the findings were not re-filed, which covers the "fix it and push" half of the check.

Observed and handed to Step 9:

- (a) `file_shas` also records files the PR **removed**. `tests/test_main.py` was stored with its blob from `main`, because the PR files API reports deleted files with their old SHA.
- (b) "Overall" rates only the new findings, so a comment can say `approve` while still listing open ones.

Reviewer findings F004, F006 and F007 are real but low severity. F005 is a false positive: `FindingStatus` is a `StrEnum`, so the f-string already renders `open`.

---

## Phase D — Incremental review & finding re-check

### [ ] Step 8 — Incremental diff

**Goal:** Review only `previous_sha → head_sha` on synchronize instead of the whole PR.

**Depends on:** Step 7.

**Creates:**

```text
orchestrator/diff.py
tests/orchestrator/test_diff.py
```

**Tasks:**
1. `full_diff` — the PR diff from `get_pr_diff` (per-file patches, so base-branch changes are excluded). What Phase B used.
2. Decision rule (do **not** use `pr.base.sha` — it is a snapshot from PR open/retarget and does not move with the base branch):
   - `cmp = repo.compare(base_ref, head_sha)`; `inc = repo.compare(reviewed_sha, head_sha)`.
   - **incremental** when `base_ref` is unchanged, `cmp.merge_base_commit.sha == state.base_merge_sha`, and `inc.status == "ahead"`. Diff = `inc` restricted to the current PR files.
   - **full** when `inc.status == "diverged"` (rebase / force-push), the merge-base moved (base merged into the branch, "Update branch"), `base_ref` changed (retarget), or a `GithubException` with status 404/422 is raised (old SHA gone). PyGithub's `compare()` is lazy — the exception surfaces on first attribute access (e.g. `.status`), not on the call itself.
   - **noop** when `inc.status == "identical"` (job re-run at the same SHA) → exit without posting.
3. `context_builder.py` picks the right diff and records `diff_mode: full | incremental | noop` in `PRContext` so later agents know.
4. Catch `github.GithubException` in `main.py` alongside `RuntimeError` / `FindingParseError` (today it escapes uncaught).

**Done when:** Test PR with three commits: third run's `PRContext.diff` contains only commit-3 changes; merging the base branch into the PR branch → full; merging a child PR into the PR branch → incremental containing only its changes; force-push/rebase → full.

---

### [ ] Step 9 — Finding re-check pass (the core differentiator)

**Goal:** Previous findings are explicitly re-checked and their statuses updated — including regression detection.

**Depends on:** Step 8.

**Creates:**

```text
agents/reviewer_agents/prompts/recheck.md
orchestrator/recheck.py
tests/orchestrator/test_recheck.py
```

**Tasks:**
1. Skip check (works in every diff mode): compare each finding's file blob SHA against `state.file_shas`. Unchanged → status kept, no LLM call. File deleted → `resolved` ("code removed"). Follow `previous_filename` for renames. First, stop `run_review` from writing `status == "removed"` files into `file_shas` (found in the Step 7 verification). Otherwise a deleted file looks present, carrying its old blob SHA, and this check never fires.
2. Dedicated re-check agent call for open, reopened **and resolved** findings whose file changed (resolved ones are needed to catch regressions): given the finding + stored snippet + current head file content (`get_file_content(path, ref)`, added in Step 7) + incremental diff, output `resolved | still_open | reopened` + one-line justification. Lines above a finding may have been added or removed, so locate the stored snippet near the old `line` rather than cutting at exactly that line. `still_open` is a verdict, not a status — the finding keeps its current status. The re-check never outputs a dismissal.
3. Apply transitions through the Step 6 lifecycle table; a finding whose problem reappears goes `resolved → reopened`.
4. Developer explanations: read human issue comments created after the last valid state comment that mention a finding ID (`F\d{3}`). Accept only from the PR author or `author_association` OWNER / MEMBER / COLLABORATOR; pass the text to the LLM as untrusted data. A justification the LLM accepts → `rejected` with `rejected_by=developer`, acknowledged in the next comment.
5. Comment format from design section 14: previous findings with ✅/🟡 markers, then new findings. The "Overall" rating must take still-open previous findings into account. Today it rates only the new findings, so a comment can say `approve` next to open medium findings.
6. Comment strategy (design section 19, moved here from Step 7 because only now can statuses change): skip posting when no status changed and there are no new findings — `reviewed_sha` then stays put and the next run's range covers it. Otherwise post a new summary comment. This is in addition to Step 7's same-SHA skip and Step 8's `noop`. Inline review threads come in Step 9b; state storage and re-check logic don't change for them.

**Done when:** Full scenario test on a real PR: commit 1 plants F001 → commit 2 fixes it (F001 → resolved in state + comment) → commit 3 reintroduces it (F001 → reopened). Plus: a collaborator's "F00x: intentional because …" reply leads to `rejected (developer)`; the same reply from a non-collaborator is ignored. This is design section 11 proven end-to-end.

---

### [ ] Step 9b — Inline review threads

**Goal:** Each finding lives in an inline review thread on its line, and re-check outcomes become thread actions (resolve, stay quiet, reply) instead of only a new summary comment.

**Why here:** this is the earliest point where inline comments don't create noise. Before Step 9 the bot can't tell resolved from still-open findings, so every push would re-post N inline comments. Placing it before Step 10 means the LangGraph refactor is verified with the final posting behaviour, so posting isn't restructured twice.

**Depends on:** Step 9.

**Creates:** nothing new. Edits to `clients/github.py` (review/thread functions) and the posting path in `main.py`.

**Tasks:**
1. Only posting and thread tracking change; the state block and re-check logic from Steps 6–9 stay as they are.
2. Post via `pr.create_review(commit, body, event="COMMENT", comments=[{path, line, side, body}])`: the summary + state block goes in the review body, each finding becomes an inline comment. `event="COMMENT"` only — `APPROVE`/`REQUEST_CHANGES` from `github-actions[bot]` is gated by the repo setting "Allow GitHub Actions to create and approve pull requests" (off by default), and humans approve (design section 25).
3. Before posting, check each finding's `file`/`line` against the PR patch hunks. GitHub rejects inline comments on lines outside the diff with a 422 error, and one bad comment fails the whole review. Findings that can't be placed are listed in the summary only.
4. Store the inline comment id on the finding (`thread_id` in the state block).
5. Map re-check outcomes to thread actions:
   - resolved → reply "✅ Resolved in `<sha>`" in the thread (`create_review_comment_reply`), then resolve the thread through GraphQL `resolveReviewThread` (PyGithub has no wrapper);
   - still open → no new comment;
   - reopened → reply in the same thread.
6. Outdated threads: when a later commit moves the line, GitHub marks the thread outdated. A finding that is still open is re-posted at its new line, located via the stored `snippet`.
7. Developer explanations: thread replies (`get_review_comments()`, linked through `in_reply_to_id`) are tied to their finding directly, replacing the `F00x:` convention from Step 9. The same author-association filter applies.
8. Load state from the review body (it carries the state block now) as well as from issue comments, so PRs reviewed before this step keep their state.

**Done when:** On a real PR:
- each finding appears as a thread on its line;
- a finding whose line is outside the diff appears in the summary only, and the review still posts;
- fixing a finding in the next commit gets a "✅ Resolved" reply and a resolved thread;
- a collaborator's reply in a thread is picked up as an explanation.

---

## Phase E — Agentic structure (the prototype)

### [ ] Step 10 — LangGraph graph skeleton

**Goal:** Replace the linear main flow with an explicit graph, one node per stage — same behavior, new skeleton.

**Framework decision:** LangGraph 1.x, hand-built `StateGraph` with Pydantic state — see [`framework-decision.md`](framework-decision.md).

**Depends on:** Step 9b (behavior must be complete and green *before* restructuring).

**Creates:**

```text
graph/review_graph.py
graph/__init__.py
```
Also adds `langgraph` via `uv add langgraph`. `main.py` shrinks to: parse args → invoke the graph.

**Tasks:**
1. Nodes: `load_memory` → `build_diff` → `review` → `recheck` → `judge_placeholder` (passthrough) → `update_memory` → `post_review`.
2. State = a typed dict wrapping `PRContext` + intermediate results.
3. No behavior change — outputs identical to Step 9b's.

**Done when:** Re-running the Step 9 / 9b scenarios through the graph produces identical memory state, review content and thread actions. Refactor verified by equivalence, not by eye.

---

### [ ] Step 11 — Tools layer (agents can investigate)

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
2. Bind tools to the reviewer via the agent factory; add the "use tools to verify before reporting" instruction from design section 24.
3. Enforce `MAX_TOOL_CALLS` (design section 23) in the tool-binding loop.

**Done when:** A test PR where the bug is only visible outside the diff (e.g., caller passes bad input defined in another file) gets correctly found — impossible without tools.

---

### [ ] Step 12 — Critic (verification loop)

**Goal:** Suspicious findings get challenged with evidence before reaching GitHub.

**Depends on:** Step 11.

**Creates:**

```text
agents/reviewer_agents/prompts/critic.md
```
(Plus a new node + loop edge in `graph/review_graph.py`.)

**Tasks:**
1. Critic node: for each finding, decide `confirmed | uncertain | rejected` + evidence note, using tools.
2. Bounded loop (design section 23): `MAX_REVIEW_ITERATIONS = 3` — uncertain findings go back to the reviewer with the critic's doubt, at most N times.
3. Graph edge: after critic, route `has_uncertain and iterations_left → review` else `→ judge`.
4. Rejected findings are kept in memory with status `rejected` (audit trail, design section 10) but not posted.

**Done when:** A PR with a plausible-but-false issue (e.g., "missing null check" where the caller already guarantees non-null — visible via tools) produces no GitHub comment but a `rejected` finding in memory with the evidence recorded.

---

### [ ] Step 13 — Judge + confidence policy

**Goal:** Final posting decision is centralized and threshold-driven.

**Depends on:** Step 12.

**Creates:**

```text
agents/reviewer_agents/prompts/judge.md
```
(Plus the real judge node replacing the Step 10 placeholder.)

**Tasks:**
1. Judge node: takes confirmed + uncertain findings, outputs final per-finding verdict + adjusted confidence.
2. Confidence policy from design section 23 as config constants (`REPORT_THRESHOLD=0.90`, `SUGGESTION_THRESHOLD=0.75`).
3. Rendering: ≥0.90 normal, 0.75–0.89 labeled "suggestion / needs verification", <0.75 silently recorded in memory only.
4. Apply the policy to the inline threads from Step 9b: ≥0.90 posts a normal thread, 0.75–0.89 posts a thread labeled "suggestion / needs verification", <0.75 gets no thread (state only).

**Done when:** A mixed PR (one clear bug, one borderline style-adjacent issue, one vague concern) yields: one normal inline thread, one "suggestion"-labeled thread, and one finding present in memory but absent from GitHub.

---

### [ ] Step 14 — Specialized reviewers

**Goal:** Logic, Security, and Testing reviewers run in parallel; results merge into the critic.

**Depends on:** Step 13.

**Creates:**

```text
agents/reviewer_agents/prompts/security-reviewer.md
agents/reviewer_agents/prompts/testing-reviewer.md
```

**Tasks:**
1. Security + Testing reviewer prompts from design section 21 checklists.
2. Fan-out node: run all three reviewers (parallel or sequential — sequential is fine for the prototype, note the choice), tag findings with `source_reviewer`.
3. Merge function: dedupe overlapping findings (same file+line+category) keeping the higher confidence.
4. Re-check pass (Step 9) routes each previous finding back to its original reviewer.

**Done when:** A PR containing one logic bug, one security issue (e.g., injection), and one missing test produces three findings from three different `source_reviewer` values in one review comment — the design section 18 "final prototype" experience.

---

## After the prototype

Deliberately out of scope until Step 14 is green:

- Performance Reviewer (fourth agent)
- MCP tool interface (tools are plain functions until this matters)
- Testing tools that execute repo code (`npm test` etc.) — security review needed first
- Reusable workflow / GitHub App for multi-repo (design section 7 progression Phase 3–4)
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
                          Step 9b
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
