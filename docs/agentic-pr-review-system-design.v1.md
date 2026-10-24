# Agentic PR Review System

> **Status:** Draft / Design Proposal  
> **Purpose:** Learning project and evolving architecture  
> **Last updated:** 2026-10-22 (agent framework decision: LangGraph — see [`framework-decision.md`](framework-decision.md))

---

## 1. Project Overview

Build a GitHub-based **agentic PR review system** that automatically reviews a Pull Request when it is opened and reviews it again whenever new commits are pushed.

The important difference from a basic AI code reviewer is that the system keeps track of **previous findings** and checks whether those findings were actually resolved.

### Core idea

```text
PR opened
   ↓
AI reviews PR
   ↓
Findings posted
   ↓
Developer fixes code
   ↓
New commit pushed
   ↓
AI reviews the new changes
   ↓
AI re-checks previous findings
   ↓
Resolved findings are marked resolved
   ↓
New findings are identified
   ↓
Developer fixes again
   ↓
Repeat until ready
```

The project should eventually support:

- Incremental PR reviews
- Previous-finding tracking
- Multiple specialized reviewers
- Tool usage
- Verification loops
- Critic/judge agents
- CI/test integration
- Persistent PR memory
- GitHub review comments
- Human approval before merge

---

# 2. Goals

## Primary goals

1. Automatically review PRs.
2. Review the PR again when new commits are pushed.
3. Remember previous findings.
4. Determine whether previous findings were resolved.
5. Detect new problems introduced by later commits.
6. Use tools to verify findings instead of relying only on the LLM.
7. Reduce false-positive comments.
8. Keep a clear audit trail of the agent's decisions.

## Learning goals

This project is intended to demonstrate:

- Agentic workflows
- AI orchestration
- Agent memory
- Tool calling
- MCP
- GitHub Actions
- Git/GitHub APIs
- Multi-agent systems
- Verification loops
- Structured agent state
- AI coding/review harnesses

---

# 3. High-Level Architecture

```text
                         ┌─────────────────┐
                         │     GitHub      │
                         │   Repository    │
                         └────────┬────────┘
                                  │
                       PR opened / updated
                                  │
                                  ▼
                         ┌─────────────────┐
                         │ GitHub Actions  │
                         └────────┬────────┘
                                  │
                                  ▼
                      ┌──────────────────────┐
                      │     Orchestrator     │
                      │                      │
                      │ Controls workflow    │
                      └──────────┬───────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
              ▼                  ▼                  ▼
          PR Memory          Git / Diff          GitHub API
              │                  │                  │
              └──────────────────┼──────────────────┘
                                 │
                                 ▼
                      ┌──────────────────────┐
                      │    Review Agents     │
                      ├──────────────────────┤
                      │ Logic Reviewer       │
                      │ Security Reviewer    │
                      │ Testing Reviewer     │
                      │ Performance Reviewer │
                      └──────────┬───────────┘
                                 │
                                 ▼
                      ┌──────────────────────┐
                      │   Verification /     │
                      │      Critic          │
                      └──────────┬───────────┘
                                 │
                                 ▼
                      ┌──────────────────────┐
                      │       Judge          │
                      │                      │
                      │ Final findings       │
                      └──────────┬───────────┘
                                 │
                    ┌────────────┴────────────┐
                    ▼                         ▼
              Update Memory            GitHub Review
```

---

# 4. Responsibility of Each Layer

## GitHub

GitHub is the source of truth for:

- Pull Requests
- Commits
- Files
- Review comments
- Review threads
- Branches
- CI status

## GitHub Actions

GitHub Actions is the event-driven trigger.

Example events:

```yaml
on:
  pull_request:
    types:
      - opened
      - synchronize
      - reopened
```

`opened` starts the first review.

`synchronize` runs the workflow when the PR branch receives a new commit.

`reopened` handles a PR that was previously closed and then reopened.

## GitHub Actions starts the Python orchestrator

A useful implementation pattern is to let **GitHub Actions handle the event and execution environment**, while a **Python program controls the actual agentic workflow**.

In other words, GitHub Actions does not need to contain all of the agent logic. The workflow can simply start the Python application.

```text
GitHub PR opened / new commit
            ↓
     GitHub Actions
            ↓
     setup Python
            ↓
   python -m orchestrator.main
            ↓
        LangGraph
            ↓
   Agentic review workflow
            ↓
 Agents + Tools + Memory + LLM
```

Example GitHub Actions workflow:

```yaml
name: AI PR Review

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  review:
    runs-on: ubuntu-latest

    permissions:
      contents: read
      pull-requests: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run PR agent
        run: python -m orchestrator.main
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          AI_API_KEY: ${{ secrets.AI_API_KEY }}
```

The important separation is:

| Layer | Responsibility |
|---|---|
| GitHub Actions | Detect PR events and start the program |
| Python | Application/orchestrator logic |
| LangGraph | Control agent state, branches, and loops |
| Agents | Perform specialized review reasoning |
| Tools | GitHub, Git, tests, scanners, repository search, etc. |
| Memory | Persist previous review state |
| LLM | Provides the reasoning capability |

For this learning project, **Python + LangGraph is a good place to put the main agentic control logic**. OpenCode can be experimented with later as an agent runtime/harness rather than making it a required part of the first version.

### Example Python flow

Conceptually, `orchestrator/main.py` could do something like:

```python
def main():
    pr = github.get_pull_request()
    memory = load_memory(pr.number)

    context = build_context(pr, memory)

    result = review_graph.invoke(context)

    update_memory(result)
    github.post_review(result)
```

With LangGraph, the graph can control the more detailed agentic loop:

```text
Load memory
    ↓
Get diff
    ↓
Reviewers
    ↓
Use tools
    ↓
Critic
    ↓
Need more investigation? ── Yes ──→ Tools / reviewer
    │
    No
    ↓
Judge
    ↓
Update memory
    ↓
Post GitHub review
```

GitHub Actions is therefore the **event trigger and runner**, while the Python/LangGraph application is the **agentic brain/orchestrator**.

---

## Applying the Reviewer to Multiple Projects

A GitHub Actions workflow does **not automatically apply to every repository**.

There are three main ways to use the same AI reviewer across multiple projects.

### Option 1 — One workflow per repository

The simplest approach is to put the workflow inside each repository:

```text
project-a/
└── .github/
    └── workflows/
        └── pr-review.yml

project-b/
└── .github/
    └── workflows/
        └── pr-review.yml

project-c/
└── .github/
    └── workflows/
        └── pr-review.yml
```

Each repository then runs the same Python/LangGraph reviewer when its own PR changes.

```text
Project A ──┐
Project B ──┼──> GitHub Actions ──> Python/LangGraph ──> AI Review
Project C ──┘
```

**Advantages**
- Very easy to understand.
- Good for the first learning version.
- Each repository is independent.

**Disadvantages**
- The workflow YAML is duplicated.
- Updating the workflow means updating multiple repositories.
- Shared configuration can become harder to manage.

### Option 2 — Shared reusable GitHub Actions workflow

Once the reviewer works, a better approach for multiple repositories is to define the workflow once in a central repository and let other repositories call it.

For example:

```text
my-org/
├── project-a/
├── project-b/
├── project-c/
└── .github/
    └── workflows/
        └── ai-pr-review.yml
```

The central workflow contains the common setup:

```text
Reusable workflow
        ↓
Setup Python
        ↓
Run orchestrator
        ↓
LangGraph
        ↓
AI review
```

Individual repositories only need a small workflow that calls the shared workflow:

```yaml
jobs:
  ai-review:
    uses: my-org/.github/.github/workflows/ai-pr-review.yml@main
    secrets: inherit
```

Conceptually:

```text
project-a ──┐
project-b ──┼──> Shared GitHub Actions workflow
project-c ──┘                 ↓
                         Python orchestrator
                                ↓
                            LangGraph
                                ↓
                            AI Review
```

This means the reviewer logic can be maintained in one place while many repositories use it.

**This is the recommended next step after the single-repository learning version.**

### Option 3 — Central GitHub App / webhook service

For a more production-style architecture, repositories can send PR events to one central service.

```text
Project A ──┐
Project B ──┼──> GitHub App / Webhook
Project C ──┘             ↓
                    Central Python service
                            ↓
                        LangGraph
                            ↓
                      AI Review System
                            ↓
                         GitHub API
```

The central service receives information such as:

```text
repository
PR number
base SHA
head SHA
event type
```

It can then run the same reviewer for any repository.

**Advantages**
- One central service.
- No duplicated workflow logic.
- Easier to manage many repositories.
- Centralized memory and monitoring.

**Disadvantages**
- More infrastructure.
- Authentication and GitHub App permissions are more complicated.
- Requires a long-running service or externally hosted worker.

### Recommended progression

For this learning project, do **not** start with the central service.

Use this progression:

```text
Phase 1
One repository
    ↓
Local / GitHub Actions
    ↓
Python reviewer

Phase 2
One repository
    ↓
Python + LangGraph
    ↓
Memory + tools + verification

Phase 3
Multiple repositories
    ↓
Reusable GitHub Actions workflow
    ↓
One shared reviewer

Phase 4
Many repositories / organization-wide system
    ↓
GitHub App / webhook
    ↓
Central Python + LangGraph service
```

The important distinction is:

| Approach | Where workflow lives | Best for |
|---|---|---|
| Per-repository workflow | Each project | Learning / small number of repos |
| Reusable workflow | Central `.github` repository | Multiple projects |
| GitHub App / webhook | Central service | Organization-wide / production |

The **Python/LangGraph reviewer itself can remain the same** in all three approaches. What changes is how GitHub events reach the reviewer.

---

## Orchestrator

The orchestrator controls the workflow.

It decides:

1. What commit was reviewed previously?
2. What changed?
3. Which previous findings are still open?
4. Which agents should run?
5. Which tools should be used?
6. Which findings need verification?
7. What should be saved to memory?
8. What should be posted to GitHub?

The orchestrator should not be responsible for making every code-review decision itself.

---

# 5. Memory

## What memory means in this project

The LLM itself does not permanently remember a PR.

Instead, the system stores structured state and gives the relevant state back to the agent during the next run.

Example:

```json
{
  "pr": 123,
  "reviewed_sha": "abc456",
  "findings": [
    {
      "id": "F001",
      "status": "open",
      "severity": "medium",
      "file": "src/JobService.java",
      "line": 82,
      "title": "Page size is not validated"
    },
    {
      "id": "F002",
      "status": "resolved",
      "severity": "low",
      "file": "src/JobServiceTest.java",
      "line": 41,
      "title": "Missing empty-result test"
    }
  ]
}
```

## Memory should answer

- What commit was last reviewed?
- What findings were previously identified?
- Which findings are open?
- Which findings were resolved?
- Which findings were rejected?
- What evidence supported a finding?
- Which GitHub comment/thread corresponds to the finding?
- When was the finding last checked?

---

# 6. Where Memory Can Live

### Version 1 — GitHub + JSON

Simple learning implementation:

```text
agent-state/
└── pr-123.json
```

Advantages:

- Easy to understand
- Easy to debug
- No database required

Disadvantages:

- Less suitable for many repositories
- Concurrent workflow handling becomes harder

### Version 2 — Database

Example:

```text
PostgreSQL

PR
 ├── repository
 ├── pr_number
 ├── head_sha
 └── updated_at

Finding
 ├── finding_id
 ├── pr_id
 ├── file
 ├── line
 ├── severity
 ├── status
 ├── description
 └── confidence

Review
 ├── review_id
 ├── pr_id
 ├── commit_sha
 └── created_at
```

For the first implementation, use **JSON or a small SQLite/PostgreSQL database**.

---

# 7. Tools

Agents should be able to use tools to investigate findings.

## Orchestrator data vs. agent tools — who fetches what

A distinction that matters before reading the tool lists below:

**PR data is fetched by the orchestrator, not by agents.** Before any
agent runs, the pipeline (Steps 2–3) calls the GitHub client
(`clients/github.py`) and packs everything into a
`PRContext` object:

```text
PR event
  │
  ▼
orchestrator/main.py
  │  get_pull_request() / get_pr_files() / get_pr_diff()
  │  get_existing_review_comments()        ← pipeline calls these, every run
  ▼
PRContext  ──── handed to agents as prompt input
  │
  ▼
Agents review what is already in front of them
```

- PR metadata, changed files, the diff, and prior bot comments arrive in the
  prompt — agents never request them. Making an LLM decide to fetch data that
  every run needs would only burn tokens and risk the step being skipped.
- Posting to GitHub is orchestrator-only, after the judge decides what gets
  reported. Agents never post directly — one hallucination would otherwise
  mean public spam on the PR.
- The `repo` / `client` injection parameters on `clients/github.py` functions
  exist so **tests** can mock them and run offline. They are not an agent
  interface.

**Agent tools are for optional investigation** that the pre-fetched PR data
cannot cover: "the diff calls `parse_config()` — let me read that function to
check whether it can return None." Those tools (below) run against the
checked-out repository on disk — fast, free, no API rate limits — not the
GitHub API.

| Who | Fetches | When |
|---|---|---|
| Orchestrator | PR metadata, files, diff, prior comments | Always, before the review starts |
| Agents | Repo files, git history, code search | Only when they choose to investigate |

## Git tools

Examples:

```bash
git diff
git log
git show
git blame
git status
```

Useful for:

- Understanding changes
- Comparing commits
- Looking at history
- Finding why code exists

## Repository tools

Examples:

```text
search code
read file
find symbol
find references
inspect dependencies
```

## Test tools

Examples:

```bash
npm test
npm run lint
npm run build

mvn test
mvn verify
```

depending on the repository.

## GitHub tools

The GitHub client (`clients/github.py`) is **orchestrator
plumbing, not an agent tool** — see "Orchestrator data vs. agent tools" above.
For the prototype, agents get no GitHub API access at all: everything they
need about the PR is already in `PRContext`.

If a future step genuinely needs GitHub-API investigation from an agent
(e.g., fetching CI status mid-review), wrap the relevant client function in a
thin read-only adapter in `tools/`. Writes (`create review`, `create comment`,
`reply`, `resolve thread`) stay orchestrator-only permanently:

```text
read-only (adapter candidates):        orchestrator-only (never tools):
  get commits                           create review
  get review comments                   create comment
  get review threads                    reply to comment
  get CI status                         resolve thread
```


## Security tools

Potential future tools:

```text
dependency vulnerability scanner
secret scanner
SAST scanner
package audit
```

---

# 8. MCP

MCP can be used as the tool interface for the agent.

Conceptually:

```text
Agent
  │
  ▼
MCP
  │
  ├── GitHub
  ├── Git
  ├── Repository
  ├── Tests
  └── Security scanners
```

MCP is not the memory.

MCP is not the model.

MCP is primarily the **tool interface** through which the agent can interact with external capabilities.

---

# 9. Agent Roles

A first version can use one reviewer.

Later, split the review into specialized agents.

```text
                    PR
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
       Logic      Security    Testing
      Reviewer    Reviewer    Reviewer
          │          │          │
          └──────────┼──────────┘
                     ▼
                   Critic
                     │
                     ▼
                   Judge
```

## Logic Reviewer

Checks:

- Incorrect business logic
- Edge cases
- Null handling
- Race conditions
- Regression risks
- Incorrect assumptions

## Security Reviewer

Checks:

- Authentication
- Authorization
- Injection
- XSS
- Secrets
- Unsafe input
- Dependencies
- Sensitive data exposure

## Testing Reviewer

Checks:

- Missing tests
- Weak tests
- Incorrect mocks
- Edge cases
- Regression coverage

## Performance Reviewer

Checks:

- Expensive queries
- Unnecessary API calls
- N+1 problems
- Large loops
- Memory usage
- Caching opportunities

---

# 10. Why Multiple Agents?

A single agent may:

- Miss a problem
- Over-report a problem
- Anchor on its first conclusion
- Interpret code incorrectly

Multiple specialized reviewers provide independent perspectives.

However, simply adding more agents does not guarantee correctness.

Therefore use a **critic / judge stage**.

```text
Reviewer A ──┐
Reviewer B ──┼──> Critic ──> Judge ──> Final Review
Reviewer C ──┘
```

The critic asks:

> Is there enough evidence that this finding is real?

The judge asks:

> Should this finding actually be reported?

---

# 11. Verification Loop

The agent should not immediately post every suspected problem.

Example:

```text
Agent:
"Possible SQL injection."

        ↓

Search repository

        ↓

Inspect query implementation

        ↓

Check parameter binding

        ↓

Run relevant test if useful

        ↓

Evidence confirms issue?

      /       \
    YES        NO
     ↓          ↓
  Keep       Reject
```

This reduces false positives.

---

# 12. Review Flow — PR Opened

Example PR:

```text
PR #123

Add pagination to job search API
```

### Step 1 — GitHub event

```text
pull_request
action: opened
```

GitHub Actions starts.

### Step 2 — Create PR context

Collect:

```text
PR number
title
description
base branch
head branch
base SHA
head SHA
changed files
existing comments
CI status
```

### Step 3 — Load memory

No previous review exists.

```text
previous_sha = null
findings = []
```

### Step 4 — Calculate diff

Compare:

```text
base SHA
   ↓
head SHA
```

### Step 5 — Run reviewers

```text
Logic Reviewer
Security Reviewer
Testing Reviewer
```

### Step 6 — Reviewers use tools

They can inspect:

- Source files
- Tests
- Git history
- Dependencies
- Build output
- Static analysis

### Step 7 — Critic

The critic verifies suspicious findings.

### Step 8 — Judge

The judge decides which findings are valid enough to report.

### Step 9 — Save memory

Example:

```text
F001 → open
F002 → open
```

### Step 10 — Post GitHub review

Example:

```text
🤖 AI Review

2 issues found.

🟡 F001
JobService.java:82

Page number is not validated.
page=0 can result in an invalid offset.

🟡 F002
JobServiceTest.java:45

Missing test for empty search results.
```

---

# 13. Developer Fixes the PR

Developer fixes F001 and F002.

Example:

### Before

```java
int offset = page * size;
```

### After

```java
int safePage = Math.max(page, 1);
int offset = (safePage - 1) * size;
```

Developer also adds:

```java
@Test
void shouldReturnEmptyResult() {
    ...
}
```

Then pushes:

```text
commit abc789
```

---

# 14. New Commit Flow

GitHub emits:

```text
pull_request
action: synchronize
```

The workflow starts again.

This is where the system becomes stateful.

### Step 1 — Load memory

Previous state:

```text
previous_sha = abc456

F001 = open
F002 = open
```

Current SHA:

```text
abc789
```

### Step 2 — Calculate incremental diff

Instead of treating the PR as completely new:

```text
abc456 → abc789
```

Review the new changes.

### Step 3 — Re-check old findings

Ask:

```text
Is F001 resolved?
Is F002 resolved?
```

### Step 4 — Check for new findings

Review the new commit normally.

### Step 5 — Verify findings

Use tools where necessary.

### Step 6 — Judge

Example result:

```text
F001 → resolved
F002 → resolved
F003 → new finding
```

### Step 7 — Update memory

```text
F001 → resolved
F002 → resolved
F003 → open
```

### Step 8 — Update GitHub

Example:

```text
🤖 AI Review — Update

Previous findings:

🟡 F001 — Page validation
✅ Resolved

🟡 F002 — Missing empty-result test
✅ Resolved

New finding:

🟡 F003 — Page size is not capped

The API accepts an excessively large page size.
```

---

# 15. Another Commit

Developer fixes F003.

```text
commit def123
```

Again:

```text
GitHub
   ↓
synchronize
   ↓
Load memory
   ↓
Compare previous SHA → new SHA
   ↓
Check F003
   ↓
Run new review
   ↓
Critic
   ↓
Judge
   ↓
Update memory
   ↓
GitHub
```

Result:

```text
F001 ✅
F002 ✅
F003 ✅

No new findings.
```

The agent can report:

```text
🤖 AI Review

All previous findings have been resolved.

No new issues detected.

Ready for human review.
```

---

# 16. Complete Lifecycle

```text
                    PR OPENED
                        │
                        ▼
                 Create context
                        │
                        ▼
                  Load memory
                        │
                        ▼
                    Get diff
                        │
                        ▼
               ┌────────────────┐
               │ Review Agents  │
               └───────┬────────┘
                       │
                       ▼
                     Tools
                       │
                       ▼
                    Critic
                       │
                       ▼
                     Judge
                       │
                       ▼
                Update memory
                       │
                       ▼
                GitHub comments
                       │
                       ▼
                Developer fixes
                       │
                       ▼
                  New commit
                       │
                       ▼
                PR synchronize
                       │
                       ▼
                  Load memory
                       │
                       ▼
               Get incremental diff
                       │
              ┌────────┴─────────┐
              ▼                  ▼
       Re-check old         Find new issues
         findings                 │
              │                  │
              └────────┬─────────┘
                       ▼
                     Critic
                       │
                       ▼
                     Judge
                       │
                       ▼
                Update memory
                       │
                       ▼
               GitHub comments
                       │
                       ▼
                  New commit?
                  /        \
                YES         NO
                 │           │
                 ▼           ▼
              Repeat       Human review
```

---

# 17. Suggested Technology Stack

## Initial version

| Component | Technology |
|---|---|
| Repository | GitHub |
| Event trigger | GitHub Actions |
| Orchestration | Python |
| Agent workflow | LangGraph 1.x — [decided](framework-decision.md) |
| Agent runtime / harness | OpenCode (optional / later experiment) |
| Model | Claude / GPT / Gemini |
| Tool protocol | MCP |
| Git operations | Git CLI |
| GitHub integration | GitHub API / GitHub CLI |
| Memory | JSON or SQLite |
| Tests | Repository's existing test framework |
| State format | JSON |
| Deployment | GitHub Actions |

## Later version

```text
GitHub
   ↓
GitHub Actions
   ↓
Orchestrator
   ↓
OpenCode / Agent Runtime
   ↓
Multiple specialized agents
   ↓
MCP tool layer
   ↓
PostgreSQL memory
   ↓
GitHub API
```

---

# 18. Suggested Repository Structure

```text
agentic-pr-reviewer/
│
├── .github/
│   └── workflows/
│       └── pr-review.yml
│
├── agents/
│   ├── logic-reviewer.md
│   ├── security-reviewer.md
│   ├── testing-reviewer.md
│   ├── critic.md
│   └── judge.md
│
├── orchestrator/
│   ├── main.py
│   ├── context.py
│   ├── memory.py
│   ├── diff.py
│   └── github.py
│
├── tools/
│   ├── git/
│   ├── github/
│   ├── testing/
│   └── security/
│
├── memory/
│   └── README.md
│
├── tests/
│
├── .opencode/
│   ├── rules/
│   │   ├── review.md
│   │   └── safety.md
│   │
│   └── skills/
│       └── pr-review/
│           └── SKILL.md
│
└── README.md
```

The exact structure is intentionally not final.

---

# 19. Agent Rules

The reviewer should have rules such as:

```text
1. Do not report an issue without evidence.
2. Prefer repository evidence over assumptions.
3. Check existing code before claiming something is missing.
4. Do not report style preferences as bugs.
5. Do not duplicate existing findings.
6. Re-check previous findings after every new commit.
7. Distinguish confirmed problems from uncertain concerns.
8. Use tools when they can verify a claim.
9. Keep comments concise and actionable.
10. Never modify the developer's code unless the workflow explicitly allows it.
```

---

# 20. Finding Lifecycle

Every finding should have a state.

```text
                ┌───────────┐
                │    New    │
                └─────┬─────┘
                      │
                      ▼
                   Open
                 /      \
                /        \
               ▼          ▼
          Resolved      Rejected
               │
               ▼
           Verified
               │
               ▼
             Closed
```

Potential states:

```text
new
open
resolved
reopened
rejected
verified
closed
```

A finding can be reopened if a later commit reintroduces the problem.

---

# 21. Important: Regressions

The system should detect this:

```text
Commit 1
F001 → open

Commit 2
F001 → resolved

Commit 3
F001 → problem introduced again
```

The state should become:

```text
F001 → reopened
```

This is one of the reasons persistent memory is useful.

---

# 22. Avoiding Review Spam

The agent should not post the same comment every time.

Bad:

```text
Commit 1:
"Missing validation."

Commit 2:
"Missing validation."

Commit 3:
"Missing validation."
```

Better:

```text
First review:
"Missing validation."

Second review:
"Previous finding still unresolved."

Third review:
"Previous finding resolved."
```

The finding ID should connect the AI finding to the GitHub review thread.

---

# 23. Confidence

Every finding can have a confidence score.

Example:

```text
0.96 → Very high confidence
0.87 → High confidence
0.72 → Medium confidence
0.51 → Low confidence
```

Possible policy:

```text
>= 0.90
    Report normally

0.75 - 0.89
    Report as suggestion / needs verification

< 0.75
    Do not automatically report
```

The exact thresholds should be tested rather than treated as permanent rules.

---

# 24. Agent Loop Limits

Agents should have bounded loops.

Example:

```text
MAX_REVIEW_ITERATIONS = 3
MAX_TOOL_CALLS = 20
MAX_REVIEW_TIME = 10 minutes
```

Conceptually:

```text
Investigate
   ↓
Evidence?
   ↓
No → investigate again
   ↓
Yes
   ↓
Critic
   ↓
Still uncertain?
   ↓
One more investigation
   ↓
Final decision
```

This prevents infinite agent loops and uncontrolled cost.

---

# 25. Security Considerations

The agent will have access to source code and potentially powerful tools.

Initially:

- Use read-only GitHub permissions where possible.
- Avoid giving the agent write access to the default branch.
- Restrict secrets.
- Do not expose production credentials.
- Run code in an isolated environment.
- Limit shell commands.
- Limit network access where possible.
- Require human approval before merging.
- Log agent actions.

The first version should preferably be:

```text
Agent can READ
Agent can COMMENT
Agent cannot MERGE
Agent cannot modify production
```

Later, controlled write access can be added.

---

# 26. Development Roadmap

## Phase 1 — Basic reviewer

```text
PR opened
   ↓
GitHub Action
   ↓
Python orchestrator
   ↓
LLM
   ↓
Review
   ↓
Comment
```

Goal: prove that GitHub Actions can trigger and run the Python reviewer.

Do not add LangGraph, MCP, multiple agents, and OpenCode all at once. First make the end-to-end Python workflow work.

## Phase 2 — Review on every commit

Add:

```text
pull_request.synchronize
```

Now every new commit triggers another review.

## Phase 3 — Memory

Add:

```text
previous SHA
findings
finding status
GitHub comment/thread ID
```

Now the agent understands previous reviews.

## Phase 4 — Incremental review

Review:

```text
previous SHA → current SHA
```

instead of blindly reviewing everything.

## Phase 5 — Tools

Add:

```text
Git
GitHub
tests
lint
build
repository search
```

## Phase 6 — Agentic workflow with LangGraph

Once the basic Python reviewer works, use LangGraph to model the agentic process as explicit state and nodes.

```text
GitHub Actions
      ↓
Python
      ↓
LangGraph
      ↓
Reviewer → Tools → Critic → Judge
               ↑        │
               └────────┘
```

The graph should own the review loop, while GitHub Actions remains responsible for starting the Python process.

## Phase 7 — Verification

Add:

```text
Reviewer
   ↓
Evidence gathering
   ↓
Critic
   ↓
Judge
```

## Phase 8 — Multiple agents

Add:

```text
Logic
Security
Testing
Performance
```

## Phase 9 — Production-style memory

Move from JSON/SQLite to PostgreSQL if needed.

## Phase 10 — Advanced automation

Potential future features:

- Automatically request re-review
- Detect resolved GitHub threads
- Run targeted tests based on findings
- Automatically summarize review history
- Detect regressions
- Track reviewer accuracy
- Measure false-positive rate
- Generate PR review metrics
- Allow `/ai-review` commands
- Allow `/ai-fix` as an explicit opt-in
- Automatically suggest tests

---

# 27. Example Final PR Experience

### Initial PR

```text
PR #123

🤖 AI Review

2 issues found.

🔴 Pagination accepts page=0.

🟡 Missing empty-result test.
```

### Developer commits fix

```text
Commit abc456
```

Agent:

```text
🤖 AI Review — Update

Previous findings:

🔴 Pagination accepts page=0
✅ Resolved

🟡 Missing empty-result test
✅ Resolved

New findings:

🟡 Page size has no maximum limit.
```

### Developer commits again

```text
Commit def789
```

Agent:

```text
🤖 AI Review — Update

Previous findings:

🟡 Page size limit
✅ Resolved

No new findings.

All previous findings resolved.

Ready for human review.
```

### Human

```text
Approve
↓
Merge
```

---

# 28. Core Design Principle

The project should be thought of as:

```text
              ┌─────────────┐
              │    Event    │
              └──────┬──────┘
                     ↓
              ┌─────────────┐
              │   Memory    │
              └──────┬──────┘
                     ↓
              ┌─────────────┐
              │   Context   │
              └──────┬──────┘
                     ↓
              ┌─────────────┐
              │   Agents    │
              └──────┬──────┘
                     ↓
              ┌─────────────┐
              │    Tools    │
              └──────┬──────┘
                     ↓
              ┌─────────────┐
              │ Verification│
              └──────┬──────┘
                     ↓
              ┌─────────────┐
              │    Judge    │
              └──────┬──────┘
                     ↓
              ┌─────────────┐
              │    Memory   │
              └──────┬──────┘
                     ↓
              ┌─────────────┐
              │   GitHub    │
              └─────────────┘
```

The core loop is:

> **Observe → Remember → Investigate → Reason → Verify → Act → Remember**

That loop is the main learning objective of the project.

---

# 29. Recommended First Implementation

For learning, keep the first architecture intentionally simple:

```text
GitHub PR
   ↓
GitHub Actions
   ↓
Python orchestrator
   ↓
LangGraph
   ↓
One review agent
   ↓
GitHub API + Git + tests
   ↓
Review result
   ↓
Memory
   ↓
GitHub comment
```

Then add complexity one layer at a time:

1. GitHub Actions triggers Python.
2. Python calls the LLM.
3. Add PR memory.
4. Add incremental diffs.
5. Add LangGraph for explicit agentic loops.
6. Add tools.
7. Add critic/judge verification.
8. Add multiple specialized agents.
9. Experiment with MCP and OpenCode.

This progression makes it clear which component is responsible for each part of the system instead of hiding the workflow behind a single agent framework.

---

# 30. Open Design Questions

These should remain open while the project is being developed.

### Agent runtime

- ~~OpenCode? Custom Python agent? Other agent framework?~~ **Decided 2026-10-22: LangGraph 1.x** — see [`framework-decision.md`](framework-decision.md) (LangGraph vs CrewAI vs LangChain evaluation).

### Models

- One model?
- Different model per reviewer?
- Cheap model for initial review and stronger model for judging?

### Memory

- JSON?
- SQLite?
- PostgreSQL?
- GitHub as the primary source of truth?

### Tools

- MCP?
- Native GitHub API?
- GitHub CLI?
- Local shell tools?

### Multi-agent strategy

- Parallel reviewers?
- Sequential reviewers?
- Reviewer + critic?
- Reviewer + critic + judge?

### Review behavior

- Comment only?
- Review summary?
- Inline comments?
- GitHub suggestions?
- Automatically request changes?

### Automation level

```text
Level 1:
Review only

Level 2:
Review + verification

Level 3:
Review + suggest fixes

Level 4:
Review + automatically fix

Level 5:
Review + fix + test + update PR
```

The recommended starting point is **Level 1 → Level 2**, then gradually increase autonomy.

---

# 31. Target End State

The eventual system should behave like a small autonomous engineering team:

```text
                         GitHub PR
                             │
                             ▼
                       Orchestrator
                             │
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                  ▼
       Memory              Diff             GitHub
          │                  │                  │
          └──────────────────┼──────────────────┘
                             ▼
                  ┌────────────────────┐
                  │  Multiple Agents   │
                  ├────────────────────┤
                  │ Logic              │
                  │ Security           │
                  │ Testing             │
                  │ Performance        │
                  └─────────┬──────────┘
                            ▼
                          Tools
                            │
                            ▼
                          Critic
                            │
                            ▼
                           Judge
                            │
                            ▼
                       Update Memory
                            │
                            ▼
                      GitHub Review
                            │
                            ▼
                    Developer commits
                            │
                            └───────────────┐
                                            │
                                            ▼
                                      Review again
```

The developer remains the final decision maker.

The agent's job is to continuously **inspect, remember, verify, and report** as the PR evolves.
