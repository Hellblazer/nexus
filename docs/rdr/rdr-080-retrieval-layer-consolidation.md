---
title: "RDR-080: Retrieval Layer Consolidation — `nx_answer` + Agent/Skill Pruning"
status: draft
type: feature
priority: P2
created: 2026-04-15
related: [RDR-042, RDR-070, RDR-078, RDR-079]
reviewed-by: self
---

# RDR-080: Retrieval Layer Consolidation

RDR-042 established a three-layer retrieval architecture: user-facing skill (`/nx:query`) → planner agent (`query-planner`) → operator agent (`analytical-operator`), with step outputs relayed through T1 scratch. The three layers exist because RDR-042 chose to keep the MCP server LLM-free — operators couldn't live inside MCP tools without coupling the server to `ANTHROPIC_API_KEY`, and the planner couldn't call the operators directly without being an agent itself. RDR-079's empirical Finding 4 formally dissolved that constraint: the `claude` CLI inherits OAuth session auth, so an MCP tool can spawn a worker with no new secret. **The three-layer architecture survives only as inertia.** This RDR consolidates it into a single `nx_answer(question, ...)` MCP tool and prunes the agents/skills that exist solely to wire it together.

The center of gravity moves from "agent-orchestrated retrieval DAG" to "MCP tool with internal dispatch." The plan-first discipline survives, but is enforced by the tool contract rather than duplicated across ten agent preambles. Users still call `/nx:query` or dispatch retrieval-shaped agents; the skill/agent now resolves to one MCP call instead of a four-layer coordination dance.

## Problem Statement

### Enumerated gaps to close

#### Gap A: The three-layer relay is fragile and slow

Today's path for a question like *"how does projection quality work in nexus?"*:

1. User invokes `/nx:query` skill (257 lines of orchestration in `nx/skills/query/SKILL.md`).
2. Skill dispatches `query-planner` agent (279 lines; writes plan JSON to T1 scratch).
3. For each step, skill dispatches `analytical-operator` agent (reads step inputs from scratch tag `query-step,step-N-1`; writes outputs to `query-step,step-N`).
4. Skill reads final scratch entry, formats response.

Four agent spawns per multi-step answer. Each spawn re-loads context, burns a SubagentStart hook, and crosses the T1-scratch serialization boundary. The relay is a correctness risk (scratch tag conventions can drift; step-output shapes are implicit) and a latency tax (each spawn adds seconds; there is no prompt-cache continuity between them).

#### Gap B: The plan-first preamble is duplicated across 10 agents

RDR-078 shipped the plan-match-first discipline by injecting a preamble into the ten retrieval-shaped agents (`nx/retrieval-agents.txt`). Each agent `.md` cites `plan_match` independently so the discipline survives hook-context trimming. This is ten copies of the same instruction. When a single MCP tool can enforce the discipline internally (call `plan_match` before anything else, fall through to planner only on miss), the preamble becomes redundant in 8 of the 10 agents.

#### Gap C: Thin-wrapper agents

Five agents exist primarily to orchestrate a single MCP call chain:

| Agent | Lines | Core behaviour |
|---|---|---|
| `query-planner.md` | 279 | Decompose question → plan JSON |
| `analytical-operator.md` | ~300 | Execute one of five operator types |
| `pdf-chromadb-processor.md` | 125 | Wrap `nx index pdf` |
| `knowledge-tidier.md` | ~250 | Dedupe T3 → persist consolidated doc |
| `plan-enricher.md` | ~200 | Expand bead → enriched markdown |
| `plan-auditor.md` | ~250 | Audit plan JSON → structured verdict |

All have a pure-function I/O shape: relay in → structured artefact out. None require user clarification mid-execution. Each is a clear MCP candidate.

#### Gap D: Skill layer is misaligned with tool boundary

Several skills (`knowledge-tidying`, `enrich-plan`, `plan-validation`, `pdf-processing`) describe themselves as "invoke agent X," not "follow this discipline." When the agent folds into an MCP tool, the skill either becomes a 10-line trigger pointer or goes away entirely.

## Key Insight — RDR-042 Constraint Is Obsolete

RDR-042 §Alternatives Considered (verbatim):
> *"MCP tools with direct LLM calls (rejected). Operators as MCP tools that call Anthropic/OpenAI APIs directly. Rejected: couples MCP server to LLM credentials, adds failure mode, breaks the deterministic tool contract."*
> *"MCP server remains LLM-free"*

Three concerns:
1. **Credential coupling** — eliminated by OAuth inheritance. `claude auth status` reports existing session; no new secret.
2. **Failure mode** — remains real but scoped. Retrieval tools (`search`, `query`, `store_put`, etc.) stay deterministic and LLM-free. Only operator-requiring tools can fail due to worker unavailability; they fail fast and gracefully per RDR-079 SC-10.
3. **Contract integrity** — maintained per-tool. Tools that dispatch workers declare it; tools that don't, don't. The MCP server is no longer universally LLM-free, but each individual tool retains a clear contract.

RDR-042's decision was correct given its constraints. Its constraints changed. This RDR accepts the updated trade-off.

## Design

### The `nx_answer` MCP tool

```
nx_answer(
    question: str,
    *,
    scope: str = "",           # catalog subtree or corpus filter
    context: str = "",         # supplementary caller-supplied context
    max_steps: int = 6,        # cap on DAG size
    budget_usd: float = 0.25,  # per-invocation cost cap
) -> str
```

Internal flow (all in-process; no subagent spawn):

1. **Plan-match gate**: call `plan_match(intent=question, ...)`. On hit (confidence ≥ threshold or FTS5 sentinel), proceed to execution. On miss, dispatch an in-MCP planning call (one worker turn with `--json-schema` for plan JSON).
2. **Execute plan**: reuse `plan_run` runner from RDR-078. Step outputs accumulate in a local Python dict; no scratch round-trips. Retrieval steps call other MCP tools directly (via the runner's `_default_dispatcher`). Operator steps call the `operator_*` tools from RDR-079.
3. **Synthesize** (if the plan ends with a synthesis step, which most do): the final step's output is the user-visible answer.
4. **Record**: write the plan's run metrics back to T2 (existing `plan_match_metrics` / `plan_run_metrics` paths). If the plan was newly planned (miss path), `plan_save` the new plan at scope=session.

**Plan-first enforcement lives in the tool contract**, not in ten agent preambles. Any caller — skill, agent, Python script, another MCP tool — gets the discipline by default.

### Other consolidation tools

| Tool | Replaces | Skill wrapper |
|---|---|---|
| `nx_answer` | `/nx:query` skill + `query-planner` + `analytical-operator` | `/nx:query` shrinks to a 10-line pointer |
| `nx_tidy` | `knowledge-tidier` agent | `/nx:knowledge-tidying` shrinks to trigger pointer |
| `nx_enrich_beads` | `plan-enricher` agent | `/nx:enrich-plan` shrinks to trigger pointer |
| `nx_plan_audit` | `plan-auditor` agent | `/nx:plan-validation` shrinks to trigger pointer |

Each new MCP tool:
- Dispatches to the operator pool from RDR-079 (worker reuse = free amortised cost).
- Uses `--json-schema` for structured output (no parse fragility).
- Returns a well-typed dict (for plan_run consumption) or a formatted string (for direct caller consumption); `_structured: bool = False` flag picks.
- Emits structured logs with `session_id`, `duration_ms`, `cost_usd`, `usage`.

### The boundary rule

Codifies what belongs where:

> A capability belongs in an **MCP tool** when (i) its I/O is a pure function of relay + nx state, (ii) it completes without user clarification mid-turn, and (iii) schema conformance is valuable to callers.
>
> In an **agent** when the user or another agent is expected to interject during execution, or when the output is a conversation rather than a document.
>
> In a **skill** when the value is the trigger — the discipline of invoking it — rather than the logic it runs.

Applied to the current inventory:
- **MCP**: `nx_answer`, `nx_tidy`, `nx_enrich_beads`, `nx_plan_audit`, the five `operator_*` tools.
- **Agent**: `debugger`, `deep-analyst`, `codebase-deep-analyzer`, `deep-research-synthesizer`, `developer`, `code-review-expert`, `substantive-critic`, `test-validator`, `strategic-planner`, `architect-planner`. These iterate with a human or require sustained judgment across turns.
- **Skill**: `plan-first`, `plan-inspect`, `plan-promote`, `plan-author`, `brainstorming-gate`, `composition-probe`, and all `pure-doc` skills that are reference material. Skills trigger disciplines; they don't wrap single function calls.

### Plan-first preamble pruning

The ten retrieval-shaped agents in `nx/retrieval-agents.txt` currently carry an identical plan-match-first preamble. Post-consolidation:
- **Keep preamble in 2**: agents that users dispatch directly for exploratory work and that might call raw retrieval tools — `deep-analyst`, `deep-research-synthesizer`. These retain the discipline as a self-reminder.
- **Drop preamble in 8**: `strategic-planner`, `architect-planner`, `code-review-expert`, `substantive-critic`, `debugger`, `plan-auditor`, `codebase-deep-analyzer`, `query-planner` (deleted; entry removed).
- **Update `retrieval-agents.txt`**: 10 → 2 entries. The SubagentStart hook stops injecting the preamble for the 8 that no longer need it.

The discipline itself moves inside `nx_answer`; an agent that calls `nx_answer` gets the discipline for free. An agent that calls `search` directly bypasses it — which is correct: `search` is the raw tool; `nx_answer` is the plan-aware one.

## Phases

- **P1** — `nx_answer` MCP tool, plan-match-first enforced internally, operator dispatch via RDR-079 pool. Deterministic runner inherited from RDR-078. Test coverage: unit-level for the dispatch logic with stubbed pool, integration-level for one end-to-end question per scenario verb.
- **P2** — Skill + preamble pruning. Collapse `/nx:query` SKILL.md from 257 lines to ~15 lines that describe `nx_answer` as the entry point. Delete `query-planner.md`. Strip the plan-match-first preamble from 8 of the 10 retrieval-shaped agents. Update `nx/retrieval-agents.txt` to 2 entries. Update `test_plan_first_skills.py` to reflect the new smaller set.
- **P3** — `nx_tidy`, `nx_enrich_beads`, `nx_plan_audit` MCP tools. Each dispatches to the operator pool with per-tool `--json-schema`. Agent files shrunk to doc stubs pointing at the tool; skills collapsed to trigger pointers.
- **P4** — Delete `pdf-chromadb-processor.md` and `/nx:pdf-processing` skill. Single-PDF users run `nx index pdf` directly; batch users script it.
- **P5** — Cost-budget surfacing. `.nexus.yml: operators.daily_budget_usd` (new key), pool WARNs at 80%, refuses at 100%. `nx doctor --operators` command reports today's spend by tool. Protects against an `nx_answer` loop eating the subscription.

## Success Criteria

- **SC-1** — Every question that today routes through `/nx:query` returns an equivalent answer via `nx_answer` — tested on a ≥20-question paraphrase set reused from RDR-079 P5 calibration. "Equivalent" = same plan match (for plan-hit questions) or a functionally complete plan (for plan-miss questions), with synthesized final step containing the same cited evidence to a human rater.
- **SC-2** — Latency: `nx_answer` p50 ≤ `/nx:query` p50 on warm operator pool. (Target: wins by eliminating three agent spawns. Measurement includes pool warm-start; cold-worker path compared separately.)
- **SC-3** — Cost: `nx_answer` per-invocation cost ≤ `/nx:query` per-invocation cost on warm pool. (Baseline from RDR-042's tested queries; new measurement on RDR-080's paraphrase set.)
- **SC-4** — Five agent files deleted: `query-planner.md`, `analytical-operator.md` (folded into operator tools per RDR-079 P3), `pdf-chromadb-processor.md`, `knowledge-tidier.md`, `plan-enricher.md`. ~1500 lines of coordination markdown removed. `plan-auditor.md` folds into `nx_plan_audit` but file stays as a trivial pointer because the plan-auditor skill remains the discipline trigger.
- **SC-5** — `nx/retrieval-agents.txt` is 2 entries (down from 10). Test `test_plan_first_skills.py::RETRIEVAL_AGENTS` updated, plugin structure test passes.
- **SC-6** — `/nx:query SKILL.md` is ≤ 20 lines. No T1-scratch relay references. All step-output coordination is in-process Python inside `nx_answer`.
- **SC-7** — Budget surfacing works: `.nexus.yml: operators.daily_budget_usd: 1.00` causes 80% warn at $0.80 cumulative, 100% refuse at $1.00 cumulative. `nx doctor --operators` reports correct cumulative.
- **SC-8** — RDR-078 and RDR-079 test suites pass unchanged. No regression on `plan_match`, `plan_run`, `traverse`, or the scenario seeds themselves.
- **SC-9** — Graceful degradation: without `claude auth status == loggedIn`, `nx_answer` returns a clear "operator pool unavailable" error for plan-miss questions, but still works for plan-hit questions whose steps are entirely retrieval (search + traverse, no operators). Tested.
- **SC-10** — The boundary rule is committed to `docs/architecture.md` with the three test cases from §Design and the current inventory classification. Future agent/skill/tool additions consult it.

## Research Findings

### RF-1 — `nx_answer` is not a new pattern; it's the deletion of an intermediate layer

The three components `nx_answer` subsumes — `/nx:query`, `query-planner`, `analytical-operator` — already pass data to each other via scratch. Moving from "scratch relay between agents" to "in-process dict between function calls" is a transport swap, not a semantic change. The plan_match-first gate, the plan_run execution loop, and the operator dispatch interfaces are all stable. This RDR does not re-derive any algorithm; it removes the coordination overhead around them.

### RF-2 — The current test suite masks the cost of the relay

RDR-078 and RDR-042 tests use stubbed dispatchers for `plan_run` and mocked subagent spawns for `/nx:query`. End-to-end `/nx:query` with real agent spawning has no unit-level regression coverage. The consolidation is effectively "make the common path testable" — `nx_answer` is directly unit-testable because it has no subagent spawns; only the operator worker calls are boundary I/O.

### RF-3 — RDR-079's cost baseline generalizes

RDR-079's operator prototype showed 3 extractions at ~$0.037/call amortised on Haiku with the streaming protocol. A multi-step `nx_answer` call does 1 plan-match (free; no LLM) + N retrieval steps (free; no LLM) + M operator steps (~$0.04 each on Haiku). A realistic 5-step answer with 1 synthesis step costs ~$0.04-0.10. This is within the range the existing `taxonomy_cmd.py` batch-labeling pattern already spends without controversy.

### RF-4 — "Keep" agents earn their keep on user interjection

The ten agents this RDR keeps (`debugger`, `deep-analyst`, `developer`, etc.) all have a shared property: mid-execution, the user may redirect them ("actually, check this other file too" / "try a different hypothesis"). That's the definition of an agent vs. a tool. A `debugger_diagnose(bug_description)` MCP tool would be a much worse debugger than the current subagent, because you can't course-correct mid-run. The boundary rule captures this as "expected to interject mid-execution."

### RF-5 — Session isolation composes correctly across the two-RDR stack

**Source**: RDR-079 acceptance gate chain (5-gate history, final critic `a51d131a93ab9a870`). T2 memory: `nexus_rdr/080-research-5-session-isolation-composition`.

`nx_answer` runs inside the `nexus` MCP server process, which is attached to the user's T1 session (discovered via PPID-walk — the default path when `NEXUS_T1_SESSION_ID` is unset). When `nx_answer` dispatches an operator step, the call goes through RDR-079's operator pool, which spawns a worker with `NEXUS_T1_SESSION_ID=pool-<uuid>` — the worker joins the pool's isolated session, not the user's. This composition is correct and requires no new design in RDR-080:

- `nx_answer` controller reads/writes the user's T1 for legitimate purposes (plan_match cache, plan_save mid-session, run metrics).
- Operator workers never touch the user's T1 — their writes land in the pool session per RDR-079 invariants I-1..I-4.
- Plan-first enforcement inside `nx_answer` is authoritative; worker-mode MCP config strips the `plan_*` / `operator_*` tools from workers, so recursion is impossible by construction (RDR-079 SC-12).

**Implication**: RDR-080 does NOT need its own T1/isolation design. The two RDRs compose correctly if RDR-079 ships first.

### RF-6 — `store_get_many` batching is a hard constraint for `nx_answer`, not an optional optimisation

**Source**: RDR-079 Risks ("Controller `$stepN.*` hydration — optional optimisation, not a requirement") + ChromaDB Cloud quota `MAX_QUERY_RESULTS = 300` (`src/nexus/db/chroma_quotas.py`). T2 memory: `nexus_rdr/080-research-6-hydration-batching`.

RDR-079 framed `store_get_many` as an optional controller-side optimisation because workers could self-hydrate. RDR-080's `nx_answer`, however, runs plan steps in-process — there is no worker to offload hydration to. When a plan step references `$stepN.ids` with a list longer than 300 (realistic for broad-corpus research queries), `nx_answer` MUST batch the `store_get` calls at ≤ 300 IDs per ChromaDB query or get a silent quota truncation.

**Implication**: P1 or P3 of this RDR's implementation must ship `store_get_many(doc_ids: list) -> list[dict]` with explicit batching, and the synthesis step of every scenario seed must consume it. Add to SC-8 (or new SC): assert no truncation on a 500-ID hydration test.

### RF-7 — The pure-compute boundary is symmetric: operators AND coordinator earn their placement

**Source**: architectural review of RDR-079's RF-4 (operators as pure-compute workers) + this RDR's boundary rule. T2 memory: `nexus_rdr/080-research-7-boundary-symmetry`.

RDR-079 established that operators are pure compute (text → JSON) and therefore belong in MCP tools with `--json-schema` enforcement, not as agents. The symmetric argument validates `nx_answer`'s MCP placement: it is an orchestration capability whose I/O is a pure function of (question, scope, bindings, nx state) → answer string. It completes without user clarification mid-turn. It has deterministic step output (the final step's result). It fits the MCP side of the boundary rule cleanly.

The same argument rules OUT the kept agents: `debugger`, `deep-analyst`, etc. have non-deterministic, mid-turn-interactive behaviour by nature. A user says "actually, check this other file too" — a tool cannot accept that mid-execution; an agent can.

**Implication**: the boundary rule is not a one-direction heuristic ("push things to MCP when possible") but a symmetric fitness test. Some agents MUST stay agents; some MCP tools MUST stay MCP tools. RDR-080 doesn't just move things into MCP — it validates that each moved thing actually belongs there by the test in §Boundary rule.

## Proposed Questions

- **PQ-1** — Should `nx_answer` emit a run trace as a first-class artifact (T2 row or T3 doc) so users can inspect what plan ran, which step produced what, and the cost? Proposed default: yes, persist to T2 `nx_answer_runs` table, TTL 7 days.
- **PQ-2** — What's the right default for `max_steps`? RDR-078 plans are 3-6 steps; `/nx:query` has no documented cap. Propose 6; cap configurable.
- **PQ-3** — Should `budget_usd` default 0.25 be global (per call) or cumulative (per session)? Per-call simpler and scopes the blast radius of a broken plan; cumulative better for whole-session accounting. Propose per-call.
- **PQ-4** — Should the plan-miss planner call go to Haiku (cheap, fast) or Sonnet (better at structured decomposition)? Propose Haiku for P1; allow override via `.nexus.yml: operators.planner_model`.

## Alternatives Considered

### Leave agent-skill layering as-is

**Rejected.** The relay overhead is real and grows with plan complexity. Every future retrieval feature either lives in the skill orchestration (where tests don't reach) or forks into a new agent (where 2000+ lines of coordination markdown accumulate). The consolidation is a one-time cost against ongoing drag.

### Deeper fold: put every agent in MCP

**Rejected.** The ten "keep" agents earn their keep (RF-4). `debugger_diagnose(bug)` as a single MCP call loses the iteration value. Folding them would be a category error: MCP tools are one-shot; agent conversations are multi-turn with human interjection.

### Keep `/nx:query` skill as an agent-dispatch orchestrator, just move operators to MCP

**Rejected.** The three-layer relay is the thing this RDR is deleting. Keeping the relay but swapping the leaves keeps the fragility (scratch-tag drift, per-spawn overhead). Half-measure.

### Build a separate `nexus-answer` MCP server for retrieval-heavy tools

**Rejected.** Same over-decomposition RDR-079 Amendment 1 rejected. `nx_answer` needs the operator pool, the plan library, the catalog, the T1/T2/T3 singletons. All of those are in `nexus`. Cross-server RPC back to itself has no benefit.

## Trade-offs

- **Latency on warm pool**: wins (no subagent spawn overhead). **Latency on cold pool**: neutral (first agent spawn was also a cold hit).
- **Cost**: neutral for most workloads (same operators; same model). Wins when warm pool has >1 cached operator across multi-step plans (agent-to-agent path re-pays cache tax per subagent).
- **Surface area**: fewer files, more per-tool code. Net LOC likely neutral; net moving parts strictly fewer.
- **User-facing behaviour change**: skills still trigger the same way (`/nx:query`, etc.). Agents still exist where they add value. The consolidation is internal.
- **Testability**: strictly wins. `nx_answer` has no subagent spawns to stub, so unit-level regression coverage is tractable.

## Success Criteria (SC-1..SC-10, see above)

## Risks / Open Questions

- **Plan-first discipline relocation**: enforcing it in `nx_answer` means an agent that calls `search` directly bypasses `plan_match`. This is the correct behaviour (raw tool vs plan-aware tool), but it needs documentation in `docs/architecture.md` so future agents don't get confused.
- **Run-trace cost**: PQ-1's "persist every answer run to T2" could balloon. Mitigate with TTL 7 days default and a `/nx:query --no-trace` override.
- **Agent authors confusion**: "when do I make an agent vs an MCP tool?" — the boundary rule answers this, but needs to land in `docs/architecture.md` and in the agent-creator plugin's guidance so new agents don't inadvertently become the next thin-wrapper deletion target.
- **Auth assumption creep**: `nx_answer` requires auth for plan-miss questions (the planner call). If a user has no auth and a question doesn't match any plan, they see "operator pool unavailable." Mitigate via a `plan_search` fallback that returns the top 5 plan descriptions so the user can disambiguate manually.
- **Nested MCP-tool-calls-MCP-tool observability**: when `nx_answer` internally calls `operator_extract` which internally spawns a worker, tracing across the layers needs structured logging with a shared correlation ID. Not a blocker for P1 but required before external adopters.

## Assumptions

- RDR-079 has landed (operator pool + `operator_*` MCP tools + `_structured` flag on core tools).
- `claude auth status` continues to be the single auth-presence signal.
- No changes to the plan JSON schema from RDR-078.
- No changes to the plan library schema from RDR-078/076.
- Users have read the plan-first discipline elsewhere; stripping the per-agent preamble does not degrade out-of-the-box behaviour because `nx_answer` enforces it.
- `docs/architecture.md` is the canonical place for the boundary rule to land (single source of truth for module layout).

## Deviations Register

*(empty at draft time)*

## Out of Scope (may spawn follow-on RDRs)

- **Caching `nx_answer` results**: distinct questions producing identical plans could share memoized outputs. Deferred.
- **Multi-LLM support for operators**: currently Haiku via `claude`. If we later need to route to Sonnet or to a different provider for specific operator types, that's an extension of RDR-079's pool, not of this RDR's consolidation.
- **Streaming `nx_answer` output back to callers**: the MCP protocol supports streaming responses; `nx_answer` currently returns string all-at-once. Deferred until user UX need emerges.
- **Auto-plan-promotion from `nx_answer` runs**: today's high-use plans get promoted via `nx plan promote` (RDR-079 P6). A future "learn plans from `nx_answer` misses that succeeded" auto-promotion loop is plausible but not in this scope.

## References

- RDR-042 — AgenticScholar-Inspired Enhancements. §Alternatives Considered contains the three-concern framing that this RDR's Key Insight formally dissolves. Design of `query-planner`, `analytical-operator`, `/nx:query` all originate here.
- RDR-067 — Cross-Project RDR Audit Loop. Finding 4 established `claude -p` headless dispatch; RDR-079 extended it; RDR-080 uses the pattern transitively.
- RDR-070 — Cross-Collection Topic Projection. `nx_answer`'s plan-miss planner may use the topic taxonomy for scope inference (`scope: topic=...`); the corpus-level projection is the enabler.
- RDR-078 — Plan-Centric Retrieval. Infrastructure this RDR reuses (plan library, schema, loader, runner, catalog traversal).
- RDR-079 — Operator Dispatch. Operator pool, `operator_*` MCP tools, `_structured` flag. Pre-requisite for P1.
- `nx/skills/query/SKILL.md` — the 257-line orchestration skill that collapses to ~15 lines in P2.
- `nx/agents/query-planner.md`, `nx/agents/analytical-operator.md` — agents deleted in P2 and P1 respectively (the latter folds into RDR-079 P3's operator tools).
- `nx/retrieval-agents.txt` — the canonical registry that shrinks 10 → 2 entries in P2.
- `src/nexus/mcp/core.py` — new home of `nx_answer`, `nx_tidy`, `nx_enrich_beads`, `nx_plan_audit` tools.
- `src/nexus/operators/pool.py` — RDR-079 pool, reused transitively.

## Revision History

- **2026-04-15** — Initial draft. Grounded in RDR-079 Finding 4 (OAuth inheritance) and deep-analysis output `analysis-deep-rdr079-boundary-redesign-2026-04-15` (T3 store).
