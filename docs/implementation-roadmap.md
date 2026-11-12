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
- [x] Step 8 — Incremental diff
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

**Goal:** The first implementation loop: PR → LLM → comment.

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
1. Finding model: id, status, severity, file, line, title, description, confidence — plus the status enum from design section 6.3.
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

**Storage decision (2026-10-25):** a local `memory/pr-<n>.json` does not survive between Actions runs (fresh runner; `memory/` is gitignored), and the reusable workflow runs in other people's repos. Rejected alternatives: committing state to the repo / an orphan branch (needs `contents: write` on callers' repos), `actions/cache` (evicted after 7 days unused, 10 GB/repo cap, scoped to `refs/pull/N/merge`), artifacts (90-day default retention, awkward lookup of the previous run). State therefore lives in the bot's own PR comments — no extra infra or permissions. See design section 6.2.

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
5. Finding lifecycle as an explicit allowed-moves table (design section 6.3): `new→open`, `open→resolved|rejected`, `resolved→reopened|verified`, `reopened→resolved|rejected`, `verified→closed`; illegal transition raises. `rejected_by: critic | developer` records who rejected it.
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
4. Comment rendering shows two sections: "Previous findings" and "New findings" (design section 8.4 output shape).
5. `reviewed_sha` / `base_merge_sha` / `file_shas` updated in the state after each successful post. Two client changes:
   - `get_pr_files` returns each file's blob `sha` (it currently doesn't) → `file_shas`.
   - New `get_merge_base_sha(base_ref, head_sha)` → `repo.compare(base_ref, head_sha).merge_base_commit.sha` → `base_merge_sha`. (Step 8 reuses the same compare call for its decision rule.)

   `reopened` events take the same stateful path as `synchronize` (comments survive while the PR is closed; the head may have moved).
6. Skip posting when `head_sha == reviewed_sha` (job re-run at the same SHA) — log it and exit 0 without posting. This is the **only** skip rule in Step 7.

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

### [x] Step 8 — Incremental diff

**Goal:** Review only `previous_sha → head_sha` on synchronize instead of the whole PR.

**Depends on:** Step 7.

**Creates:**

```text
orchestrator/diff.py
tests/orchestrator/test_diff.py
```

**Tasks:**
1. `full_diff(changed_files)` — the PR diff (per-file patches, so base-branch changes are excluded). What Phase B used. Rendered from the files `build_context` already fetched via `clients.github.render_diff`, the same renderer behind `get_pr_diff`, so the files are not fetched twice. **Done (2026-10-31).**
2. Decision rule (do **not** use `pr.base.sha` — it is a snapshot from PR open/retarget and does not move with the base branch):
   - `cmp = repo.compare(base_ref, head_sha)`; `inc = repo.compare(reviewed_sha, head_sha)`.
   - **incremental** when `base_ref` is unchanged, `cmp.merge_base_commit.sha == state.base_merge_sha`, and `inc.status == "ahead"`. Diff = `inc` restricted to the current PR files.
   - **full** when `inc.status == "diverged"` (rebase / force-push), the merge-base moved (base merged into the branch, "Update branch"), `base_ref` changed (retarget), or a `GithubException` with status 404/422 is raised (old SHA gone). PyGithub's `compare()` is lazy — the exception surfaces on first attribute access (e.g. `.status`), not on the call itself.
   - **noop** when `inc.status == "identical"` (job re-run at the same SHA) → exit without posting.

   **Done (2026-10-31):** `choose_diff` in `orchestrator/diff.py` returns `DiffChoice(mode, diff, reason)`. A new client function, `compare_commits`, reads `.status` itself, so the lazy 404/422 is raised inside it. Additions to the rule above: `behind` (branch reset to an older commit) → full; a compare listing 300 files (GitHub's per-compare cap, so possibly cut off) → full; a retarget is checked before noop, so a retargeted PR at the same head still gets a full review.
3. `context_builder.py` picks the right diff and records `diff_mode: full | incremental | noop` in `PRContext` so later agents know.

   **Done (2026-10-31):**
   - `build_context(pr_number, state)` calls `choose_diff` and logs `[route] <mode> — <reason>`. The whole-PR diff is rendered from the files already fetched, so `get_pr_files` now runs once per run instead of twice.
   - `run_review` replaces Step 7's `head_sha == reviewed_sha` skip with `diff_mode == "noop"`, so a PR retargeted at the same head is no longer skipped.
   - New rule: if the new commits touch no file that is still in the PR, the result is noop rather than an empty diff for the LLM. `reviewed_sha` stays put, so the next run's range covers those commits.
4. Catch `github.GithubException` in `main.py` alongside `RuntimeError` / `FindingParseError` (today it escapes uncaught). **Already done** (in `main()`'s except list since the Step 4 fixes). Step 8's new error source, the compare call in `choose_diff`, is covered in two layers: `test_other_api_errors_propagate` (non-404/422 errors escape `choose_diff`) and `test_main_exits_1_on_github_api_error` (main turns a `GithubException` into exit 1).

**Done when:** Test PR with three commits: third run's `PRContext.diff` contains only commit-3 changes; merging the base branch into the PR branch → full; merging a child PR into the PR branch → incremental containing only its changes; force-push/rebase → full.

**Verified (2026-10-31):** `uv run pytest` passed with 223 tests. The four live checks were replayed against the real GitHub API on PR #8, read-only (no pushes to the PR, no comments, no LLM). They call `build_context` / `choose_diff` exactly as CI does. The CI logs need a login, so the `[route]` line was read from a local run instead.

- [x] **Third commit → incremental.** `build_context(8, <state saved by the 783b4db review>)` at head `4528b32` gave `[route] NEW COMMITS CHECK — 5 file(s) changed since 783b4db1e1`. The diff's 5 files match `git diff 783b4db..4528b32` exactly.
- [x] **Base branch merged in → full.** State recorded merge-base `7d3cb51` (main before PR #7); the real merge-base is now `d39ebdc`. Result: `full — merge-base moved 7d3cb51… → d39ebdc…`. main has not moved since the branch was cut, so this used real commits with that recorded state rather than a fresh "Update branch".
- [x] **Child PR merged into the branch → incremental, only its changes.** Range `7d3cb51..d39ebdc` is the merge commit that brought PR #7's branch in. GitHub reported `ahead`, and the diff's 20 files match `git diff 7d3cb51..d39ebdc` exactly.
- [x] **Force-push / rebase → full**, both outcomes:
  - Old head still on GitHub: a temporary sibling commit of `4528b32` gave `full — history rewritten since 9bd97f7fb0 (diverged)`. It was pushed to `tmp/step8-diverged`, and that branch has been deleted.
  - Old head gone: `c1e8d49`, a local-only commit, gave `full — cannot compare from c1e8d49a01 (HTTP 404, commit gone?)`.

Bot finding F001 on this PR (an identical head skips the merge-base check) was considered and not changed. Same head means no new code to review, and noop leaves `base_merge_sha` stale, so the next push with new commits sees the move and runs a full review. It stays `open` in the state until Step 9 can read a developer's `F001:` reply.

---

### [ ] Step 9 — Finding re-check pass (the core differentiator)

**Goal:** Previous findings are explicitly re-checked and their statuses updated — including regression detection.

**Depends on:** Step 8.

**Creates:**

```text
agents/reviewer_agents/prompts/recheck.md
agents/reviewer_agents/prompts/explanation-judge.md   # Task 4
agents/structured.py                  # ask_structured: one structured question, retried once
models/recheck_verdict.py             # RecheckVerdict {issue_present, reason}
models/explanation_verdict.py         # ExplanationVerdict {accepted, reason}
orchestrator/recheck.py               # Tasks 1–3
orchestrator/explanations.py          # Task 4
tests/orchestrator/test_recheck.py
tests/orchestrator/test_explanations.py
```
(Plus edits: `clients/github.py` gains `file_exists` and `get_pr_comments`; `memory.py` gains `resolve_bot_login`; `StoredFinding` gains `explained_by` and `rejection_reason`; `main.py` gains the re-check and explanation steps.)

Every run with new code posts a fresh summary comment, so the latest bot comment always matches the latest commit and is the one place a reader needs to look. Step 8's `noop` (no new code → no post) still applies.

**Tasks:**
1. Skip check (works in every diff mode): compare each finding's file blob SHA against `state.file_shas`. Unchanged → status kept, no LLM call. File deleted → `resolved` ("code removed"). Follow `previous_filename` for renames. First, stop `run_review` from writing `status == "removed"` files into `file_shas` (found in the Step 7 verification). Otherwise a deleted file looks present, carrying its old blob SHA, and this check never fires.

   **Done (2026-11-01):** `skip_unchanged` in `orchestrator/recheck.py` returns the updated findings plus the ones that need the LLM. A file that left the PR entirely (added then deleted, or all changes reverted) goes to the re-check rather than being resolved, since only the head file can tell those apart. `run_review` no longer stores removed files in `file_shas`.
2. Dedicated re-check agent call for open, reopened **and resolved** findings whose file changed (resolved ones are needed to catch regressions): given the finding + stored snippet + current head file content (`get_file_content(path, ref)`, added in Step 7) + this file's part of the diff. Lines above a finding may have been added or removed, so locate the stored snippet near the old `line` rather than cutting at exactly that line. The re-check never outputs a dismissal.

   **Done (2026-11-01):** `recheck_findings` makes one call per finding (prompt `recheck.md`). Changed from the plan: the LLM answers yes/no (`issue_present` + reason) instead of `resolved | still_open | reopened`. The three-word form allowed answers that make no sense for the current status (`reopened` for an open finding would raise in the lifecycle table; `still_open` for a resolved one is unclear), so the status move is worked out in code (Task 3). `locate_line` slides the stored snippet over the head file (indentation ignored, `difflib` ratio ≥ 0.6, nearest match wins, stops at the first exact match; ~0.07 s on a 10,000-line file). The LLM sees ~60 numbered lines around the located line. A file gone from head and from the PR resolves without a call; a file that exists but is unreadable (directory, over 1 MB) keeps its status — `clients.github.file_exists` tells those apart when the file is outside the PR (bot finding F001 on the Step 9 PR). An unusable reply twice keeps the status and does not fail the run.
3. Apply transitions through the Step 6 lifecycle table; a finding whose problem reappears goes `resolved → reopened`.

   **Done (2026-11-01):** `apply_outcomes`: open/reopened + gone → `resolved`; resolved + present → `reopened`; anything else unchanged. A still-present finding whose code moved gets its new `line` **and** a fresh `snippet` together — `locate_line` reads the finding's position inside the snippet from `line`, so updating one without the other would mislocate the next search. A resolved finding keeps its old snippet: that is the faulty code a regression would bring back.
4. Developer explanations: read human issue comments created after the last valid state comment that mention a finding ID (`F\d{3}`). Accept only from the PR author or `author_association` OWNER / MEMBER / COLLABORATOR; pass the text to the LLM as untrusted data. A justification the LLM accepts → `rejected` with `rejected_by=developer`, acknowledged in the next comment.

   **Done (2026-11-01):** `orchestrator/explanations.py`, run after the re-check so a finding that was just resolved is not judged. Comments are only fetched when some finding is `open` or `reopened` (the lifecycle table has no `resolved → rejected`). The cutoff is the bot's newest comment with a valid state block — the same rule `load_memory` uses, via the shared `resolve_bot_login`. A separate explanation judge (prompt `explanation-judge.md`) makes one call per explained finding; replies are capped at 2,000 chars and fenced so they cannot pose as prompt sections. An accepted explanation also stores who explained it (`explained_by`) and the judge's reason (`rejection_reason`, ≤ 300 chars) for the comment. A reply alone does not start a run; it is read on the next push.
5. Comment format from design section 8.4: previous findings with status markers, then new findings. The "Overall" rating must take still-open previous findings into account. Today it rates only the new findings, so a comment can say `approve` next to open medium findings.

   **Partly done (2026-11-01):** markers and plain-English labels are in — 🟡 open, ✅ fixed, 🔁 reopened (came back after a fix), 💬 dismissed (developer explained) with an `explained by` line (login in a code span, not an `@` mention, so nobody is notified on every review). **Still to do:** the "Overall" rating, and escaping Markdown in the shown rejection reason (bot finding F005 on the Step 9 PR).

**Done when:** Full scenario test on a real PR: commit 1 plants F001 → commit 2 fixes it (F001 → resolved in state + comment) → commit 3 reintroduces it (F001 → reopened). Plus: a collaborator's "F00x: intentional because …" reply leads to `rejected (developer)`; the same reply from a non-collaborator is ignored. This is design section 6.3 proven end-to-end.

**Verification so far (2026-11-01):** `uv run pytest` — 292 passed, offline. Live on the Step 9 PR (branch `step-9/finding-recheck-pass`):

- [x] Fix → resolved: bot finding F001 went `open → resolved` (shown `✅ fixed`) on the push that fixed it.
- [x] Collaborator explanation → dismissed: F002–F004 were explained in PR comments and all three went to `rejected (developer)`.
- [x] Regression → reopened (2026-11-02): `d7480d9` put F001's bug back (dropped the `file_exists` check). The next review moved F001 `resolved → reopened` (shown `🔁 reopened (came back after a fix)`), and the state block agrees. The revert `e414cc2` moved it `reopened → resolved` again. Seen along the way: "Overall" still said `approve` with F001 reopened (Task 5's open item), and the located line moved to `recheck.py:205`, some 35 lines above the reintroduced check.
- [x] Non-collaborator reply ignored: accepted on the offline test `test_main_ignores_an_explanation_from_an_outsider`; not tried live (no non-collaborator account to hand).

**Not ticked yet (2026-11-12):** the live scenarios all pass, but Task 5 is still open: the "Overall" rating ignores still-open previous findings, and the dismissal reason is shown without Markdown escaping. Step 9 is done when both are fixed.

---

### [ ] Step 9b — Inline review threads

**Goal:** Each open finding also gets an inline review thread on its line. The thread is resolved when the finding is fixed or dismissed, and a reply in the thread counts as a developer explanation. The summary comment stays exactly as it is.

**Why here:** before Step 9 the bot couldn't tell fixed findings from open ones, so every push would re-post every inline comment. Coming before Step 10 means the LangGraph refactor is checked against the final posting behaviour.

**Depends on:** Step 9.

**Design (2026-11-02):**
- **Threads are added next to the summary comment.** State stays in the summary comment, so `load_memory` and `save_memory` don't change.
- **Threads are found by marker, not stored.** Each inline comment carries a hidden marker, `<!-- ai-review-finding:F003 -->`, and each run reads the PR's threads with one GraphQL query. `thread_id` stays in the model, always `null`: every saved block contains it, so removing it would make them all fail validation.
- **Thread actions bring threads in line with the state.** A failed action is retried by the next run.
- **No extra LLM calls.**

Rationale: design section 11.2.

**Status (2026-11-12):** Tasks 1–7 built and partly verified live on PR #10. Not ticked: three live checks remain (below), and the bot can't resolve threads with `GITHUB_TOKEN`, so developers resolve them by hand (Task 6).

**Creates:**

```text
orchestrator/threads.py            # pure: placement, thread lookup, action plan, thread replies
tests/orchestrator/test_threads.py
```
(Plus edits to `clients/github.py` and `main.py`.)

**Tasks:**
1. **Client functions** (`clients/github.py`):
   - `get_review_threads(pr_number)`: GraphQL through `requester.graphql_query`. Normalise the author login and `created_at` to match `get_pr_comments`, because the explanations cutoff compares timestamps as strings.
   - `post_inline_comments(pr_number, head_sha, body, comments)`: `pr.create_review(..., event="COMMENT")`.
   - `reply_to_review_comment(pr_number, comment_id, body)`: `pr.create_review_comment_reply`.
   - `set_thread_resolved(thread_id, resolved)`: GraphQL `resolveReviewThread` / `unresolveReviewThread` through `requester.graphql_named_mutation`.

   **Done (2026-11-12):** all four. `create_review` asserts a `Commit` object, so `post_inline_comments` looks the head commit up first. GraphQL names the Actions bot `github-actions` (`__typename: Bot`), so `[bot]` is appended for bots only; `Z` timestamps become `+00:00`. Changed from the plan after bot finding F002 on PR #10: `get_review_threads` reads **every** page of threads, not just the first 100 — a thread left out looks like "no thread yet" and its finding would get a duplicate inline comment. Comments are still read up to 100 per thread, with a warning past that.
2. **Placement:** `commentable_lines(changed_files)` reads the new-side ranges from the hunk headers of the whole PR diff, even on incremental runs. A comment outside the diff gets a 422 error, which fails the whole review.

   **Done (2026-11-12)** in `orchestrator/threads.py`. Files with no new-side lines (deleted, binary, too large for a patch, pure renames) are left out.
3. **Thread lookup:** `threads_by_finding(threads, bot_login)` reads the marker from a thread's first comment, and only when the bot wrote it.

   **Done (2026-11-12).** `finding_marker(id)` builds the marker, so posting and lookup share it; IDs past `F999` match. If two threads carry the same marker, the newest wins.
4. **Action plan:** `plan_thread_actions(findings, threads, commentable, head_sha)`:

   | Finding status | Thread | Action |
   |---|---|---|
   | `open` / `reopened` | none, line in the diff | new inline comment |
   | `open` / `reopened` | none, line outside the diff | nothing: summary only |
   | `resolved` | not yet told "Fixed" | reply "✅ Fixed in `<sha>`", try to resolve |
   | `rejected` / `closed` | not yet told so | reply "💬 Dismissed" / "✅ Closed.", try to resolve |
   | `reopened` | last told fixed, dismissed or closed | reply "🔁 Came back in `<sha>`", try to unresolve |
   | anything else | | nothing |

   **Done (2026-11-12).** Changed from the plan after the PR #10 live run: what a thread was "told" is read from the bot's **latest status reply** in it, not from its resolved flag. `GITHUB_TOKEN` was refused the resolve (see Task 6), so the flag never changes, and planning from it repeated the "Fixed" / "Dismissed" replies on every push. Now each status change gets exactly one reply plus one best-effort resolve or unresolve. Only the bot's own replies count, so a human typing "✅ Fixed" changes nothing. A `closed` finding is told "✅ Closed." rather than "Dismissed", since closed means fixed and verified.
5. **Explanations:** `thread_replies(...)` turns each non-bot thread reply into a PR-comment-shaped dict, its body prefixed `"<finding id>: "`, and adds them to the comments passed to `developer_replies`. `explanations.py` doesn't change.

   In practice: a developer can now reply "this is intentional because…" right in the thread. Before, they had to write `F003: …` in a PR comment. The same rules apply: only the PR author or collaborators count, and the same judge decides.

   **Done (2026-11-12).** `main.py` merges thread replies with the PR comments, sorted oldest first, before `developer_replies` applies the cutoff and the trust rule.
6. **Run order in `main.py`:**
   1. read the threads;
   2. re-check and explanations;
   3. review, then post the summary (this saves the state);
   4. thread actions.

   Thread failures are logged as warnings and don't fail the run.

   **Done (2026-11-12).** Two rules added:
   - If the threads can't be read, thread actions are skipped for that run. Treating an unread list as "no threads" would post a duplicate comment for every open finding.
   - A failed reply is retried next run. A failed resolve or unresolve is not: the reply already records the status, and the warning says to resolve by hand.

   A noop run doesn't read threads.

   **Decision (2026-11-12): threads are resolved by hand.** On PR #10, `resolveReviewThread` returned `FORBIDDEN — Resource not accessible by integration` for `GITHUB_TOKEN` with `contents: read` + `pull-requests: write`. Community reports say the mutation needs `contents: write`; GitHub doesn't document it. That would also let the token push code, here and in every repo using the reusable workflow, so it isn't granted. The bot replies and the developer clicks "Resolve". A GitHub App token used only for resolving is the option if auto-resolve is wanted later.
7. **Offline tests:**
   - hunk parsing;
   - a marker from a non-bot comment is ignored;
   - each row of the action table;
   - thread replies reach the judge, and an outsider's reply is dropped;
   - the client functions normalise their output.

   **Done (2026-11-12):** `tests/orchestrator/test_threads.py` covers placement, lookup, every row of the action table, the no-repeat rule and thread replies through `developer_replies`. `test_main.py` covers the wiring: post after the summary, nothing off the diff, fixed → reply, thread reply → dismissed, unreadable threads, a failed post, a refused resolve, noop. `uv run pytest`: 343 passed.

**Out of scope:**
- re-posting outdated threads at their moved line;
- links from the summary to threads;
- more than 100 comments in one thread (warned, not read);
- retiring the `F00x:` convention;
- resolving threads automatically (needs `contents: write` or an App token; see Task 6).

**Verified live (2026-11-12), on PR #10:**
- GraphQL reads threads with `GITHUB_TOKEN`; the bot is `{"__typename": "Bot", "login": "github-actions"}` and timestamps look like `2026-11-12T13:19:24Z`, both normalised as planned.
- `GITHUB_TOKEN` can post inline comments and reply in threads, but **not** resolve them (above).

**Not yet verified:** the exact 422 behaviour for a line outside the diff (placement avoids it, so it hasn't happened).

**Done when:** offline tests pass, and on a real PR:
- [x] each open finding appears as a thread on its line, and the summary comment still lists everything (PR #10: F001 and F002);
- [ ] a finding whose line is outside the diff appears in the summary only, and the review still posts;
- [x] fixing a finding gets a "✅ Fixed" reply (PR #10: F002 at `10b4dfc`). The thread stays unresolved by design (Task 6) — the developer resolves it;
- [ ] reintroducing it gets a "🔁 Came back" reply;
- [x] a collaborator's reply in a thread dismisses the finding (PR #10: F001 → `💬 dismissed (developer explained)`, reply "💬 Dismissed" posted);
- [ ] a push with no status changes adds no thread comments. Offline: `test_plan_does_not_repeat_a_reply_when_resolving_failed`; a dry run of the planner on PR #10's live threads and state plans no actions. Still to see on the next push.

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
1. Nodes: `load_memory` → `build_diff` → `recheck` → `explain` → `review` → `judge_placeholder` (passthrough) → `update_memory` → `post_review` → `sync_threads` (Step 9b's thread actions). `recheck` and `explain` come before `review` because Step 9 runs them first: the reviewer is shown the updated statuses and must not repeat dismissed or resolved findings.
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
2. Bind tools to the reviewer via the agent factory; add a "use tools to verify before reporting" instruction to the reviewer prompts.
3. Enforce `MAX_TOOL_CALLS` (design section 11.5) in the tool-binding loop.

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
2. Bounded loop (design section 11.5): `MAX_REVIEW_ITERATIONS = 3` — uncertain findings go back to the reviewer with the critic's doubt, at most N times.
3. Graph edge: after critic, route `has_uncertain and iterations_left → review` else `→ judge`.
4. Rejected findings are kept in memory with status `rejected` (audit trail, design section 6.3) but not posted.

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
2. Confidence policy from design section 11.5 as config constants (`REPORT_THRESHOLD=0.90`, `SUGGESTION_THRESHOLD=0.75`).
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
1. Security + Testing reviewer prompts from the design section 11.6 focus lists.
2. Fan-out node: run all three reviewers (parallel or sequential — sequential is fine for the prototype, note the choice), tag findings with `source_reviewer`.
3. Merge function: dedupe overlapping findings (same file+line+category) keeping the higher confidence.
4. Re-check pass (Step 9) routes each previous finding back to its original reviewer.

**Done when:** A PR containing one logic bug, one security issue (e.g., injection), and one missing test produces three findings from three different `source_reviewer` values in one review comment — the design section 11 target experience.

---

## After the prototype

Deliberately out of scope until Step 14 is green:

- Performance Reviewer (fourth agent)
- MCP tool interface (tools are plain functions until this matters)
- Testing tools that execute repo code (`npm test` etc.) — security review needed first
- Reusable workflow / GitHub App for multi-repo (design section 4, deployment options)
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
