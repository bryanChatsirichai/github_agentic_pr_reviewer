# Agent Framework Decision: LangGraph

> **Status:** Decided (2026-10-22)
> **Decision:** Use **LangGraph 1.x** (pin `langgraph>=1.2,<2`) with a hand-built `StateGraph` and a Pydantic `state_schema`.
> **Scope:** Replaces the "LangGraph (candidate)" entry in the system design and resolves the "Agent runtime" open question.

---

## Why this decision was needed

The agentic layer (Phase 6 of the roadmap) needs to express a very specific shape:

1. **Deterministic, explicit control flow** — a fixed pipeline: load memory → compute incremental diff → run 3–4 specialized reviewers (parallel) → critic with a *conditional loop back* (re-investigate if evidence is insufficient, bounded by `MAX_REVIEW_ITERATIONS`) → judge → update memory → post GitHub review.
2. **Persistent structured state between runs** — previous findings, finding lifecycle (open/resolved/rejected/reopened), reviewed SHA — stored as JSON/SQLite and passed in/out of the workflow as a typed object.
3. **Orchestrator control, not agent autonomy** — agents never decide what happens next; the graph does. Agents receive pre-fetched context (`PRContext`) and may optionally call read-only investigation tools.
4. **LLM-agnostic** — Claude / GPT / Gemini via the required `MODEL_PROVIDER` + per-provider key/model env config (missing provider logs and exits).
5. **Bounded loops, auditability, offline unit testing** with mocked LLM calls.

Three candidates were evaluated (versions as of October 2026): LangGraph 1.2.16, CrewAI 1.15.28, and plain LangChain 1.4.6.

---

## Evaluation

### LangGraph 1.2.16 — chosen

- **Stability:** 1.0 shipped Oct 2025 and is an LTS release ("no breaking changes until 2.0"; 1.0 enters maintenance for at least 1 year after 2.0). Core `StateGraph` APIs unchanged in 1.x.
- **Deterministic control flow:** exactly what it is for. Nodes + edges + conditional edges are explicit, inspectable, and replayable. Nothing happens that isn't a node/edge — no hidden agent autonomy.
- **Typed state:** `StateGraph` accepts a Pydantic `BaseModel` as `state_schema`, with runtime validation on inputs. `ProjectReviewState` (findings lifecycle, reviewed SHA) maps directly.
- **Parallel fan-out:** the `Send` API is the documented map-reduce primitive for running the specialized reviewers in parallel, each with scoped input (exactly the "pre-fetched context per reviewer" requirement), with an implicit barrier at the next node (critic).
- **Bounded loop:** the critic loop-back is a conditional edge; bounds are expressed via `recursion_limit` (a per-run hard cap on super-steps — maps to `MAX_REVIEW_ITERATIONS`) and/or an explicit counter in state.
- **Persistence:** optional checkpointer (`InMemorySaver` for tests, `SqliteSaver`, `PostgresSaver`) gives per-super-step audit snapshots, `update_state`, and time-travel replay. For a short-lived CLI per PR event, simple state in/out serialization is sufficient; the checkpointer is optional polish.
- **LLM-agnostic:** first-class `langchain-anthropic`, `langchain-openai`, `langchain-google-genai` providers.
- **Testing:** nodes are plain functions (`state → partial dict`), so each reviewer/critic/judge node is unit-testable in isolation with a mocked ChatModel; `InMemorySaver` covers checkpointer behavior in tests.
- **Footprint:** moderate (langchain-core + graph; provider packages optional) — fine inside a GitHub Actions runner.

### CrewAI 1.15.28 — credible second choice, not selected

- Two paradigms: **Crews** (role-based teams with autonomous decision-making — the opposite of this project's requirement) and **Flows** (`@start`/`@listen`/`@router`, `@persist` for SQLite-backed cross-run state, Pydantic `Flow[State]`).
- Flows *can* express the critic loop-back (`@router`) and typed state, but:
  - no built-in step cap equivalent to `recursion_limit` — iteration bounds are your own counter;
  - parallel reviewer fan-out is not first-class (you hand-roll concurrent `Agent.kickoff` calls and join them);
  - an embedded Crew still runs autonomously inside a Flow — you fight the framework's default leaning;
  - heavy dependency footprint (LiteLLM + providers);
  - fast release cadence with refactors between minor versions (higher maintenance/churn risk than LangGraph's LTS).
- LLM-agnostic via LiteLLM (Claude/GPT/Gemini fine).

### Plain LangChain 1.4.6 — insufficient as the orchestration layer

- LangChain 1.x is positioned as the substrate: a fast agent builder (`create_agent`, autonomous) plus middleware/integrations, *built on* the LangGraph runtime.
- Bare `RunnableSequence`/`RunnableBranch` cannot express first-class loops (critic loop-back), parallel fan-out, or persistence.
- **Verdict:** not a contender on its own; the real choice was LangGraph vs CrewAI Flows.

---

## Comparison summary

| Criterion | LangGraph 1.2.16 | CrewAI 1.15.28 (Flows) |
|---|---|---|
| Orchestrator-controlled, no agent autonomy | **Native** — every transition is a wired node/edge | Requires care — Crews are autonomous; sequence single agents from a Flow |
| Conditional loop-back + bound | **Native** — conditional edge + `recursion_limit` / state counter | `@router` loop + own counter; no built-in step cap |
| Parallel reviewer fan-out | **Native `Send`** with per-branch scoped state | Hand-rolled |
| Typed Pydantic state | ✅ (read/write gotchas, see below) | ✅ Pydantic `Flow[State]` |
| Cross-run persistence | Checkpointer or simple in/out serialization | `@persist` (SQLite default) |
| Offline/mocked unit testing | **Best** — nodes are plain functions; `InMemorySaver` | Mock at the LiteLLM boundary; invoke whole Flow |
| Per-step auditability | **Superior** — named graph, super-step snapshots, replay | Flow traces; no replay |
| Maintenance/deprecation risk | **Low** — LTS, backward compatible | **Higher** — rapid releases and refactors |
| Install footprint (GH Actions runner) | Moderate | **Heavier** — LiteLLM + providers |

---

## Alternatives considered (brief)

- **Pydantic AI (2.58)** — the strongest "less framework" alternative: typed agents with Pydantic-validated outputs, dependency injection for trivial mocking, model-agnostic, plus Pydantic Graph for graph-based workflows. Lighter than LangGraph, but for orchestrator-controlled multi-agent orchestration with conditional loops, parallel fan-out, and checkpointing, LangGraph's graph runtime is the more purpose-built and battle-tested abstraction.
- **OpenAI Agents SDK (0.24)** — shorter primitives, leans autonomous (model-selected handoffs): the wrong default for deterministic control.
- **Google ADK** — now graph-workflow based and deterministic, but best under Gemini/Vertex.

---

## Decision and implementation notes

**Use LangGraph 1.x with a hand-built `StateGraph`.**

1. Define `ProjectReviewState` as a Pydantic model used as the `state_schema`.
2. Model each pipeline step (load memory, diff, reviewers, critic, judge, update memory, post) as a named node; wire conditional edges for the critic loop-back.
3. Use `Send` for the parallel specialized reviewers, each receiving scoped, pre-fetched context.
4. Enforce `MAX_REVIEW_ITERATIONS` via `recursion_limit` and/or an explicit state counter.
5. Persist between runs by serializing state in/out (JSON/SQLite as per the design doc); a checkpointer is optional.
6. Select the ChatModel provider (anthropic/openai/google-genai) from env config.

**Do NOT use `create_agent` / `create_react_agent`:**
- `create_react_agent` is deprecated in LangGraph 1.0 (replaced by LangChain's `create_agent`);
- both are autonomous ReAct loops where the model decides next tool calls, and `create_agent` does not support Pydantic state schemas — precisely wrong for an orchestrator-controlled design.

**Pydantic state gotchas to plan around:**
- Nodes *read* a Pydantic state as an object (`state.x`) but write back plain partial dicts.
- `graph.invoke()` returns a dict, not a model instance — re-wrap with `ProjectReviewState(**result)` before persisting.
- Decide node state-access style up front (`state.attr` vs `state["attr"]`); changing the schema type later forces edits across every node.

---

## References

- LangGraph docs: StateGraph, Send API, conditional edges, persistence/checkpointing (docs.langchain.com / langchain-ai.github.io, accessed 2026-10-22).
- CrewAI docs: Crews vs Flows, `@router`, `@persist`, "Evaluating Use Cases" complexity–precision matrix (docs.crewai.com, accessed 2026-10-22).
- LangChain 1.0 release notes (Oct 2025): LangChain vs LangGraph positioning.
