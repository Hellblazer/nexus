---
title: "RDR-079: Operator Dispatch + Plan Execution End-to-End"
status: draft
type: feature
priority: P1
created: 2026-04-15
revised: 2026-04-15
related: [RDR-042, RDR-067, RDR-078, RDR-080]
reviewed-by: self
---

# RDR-079: Operator Dispatch + Plan Execution End-to-End

RDR-078 shipped the plan-centric retrieval infrastructure — `plan_match`, `plan_run`, typed-graph `traverse`, nine YAML seed plans, the four-tier loader, the plan-first skill family, and the CI schema check. Post-ship critique established that the scenarios it was designed for **cannot execute end-to-end**. The infrastructure is real; the feature is not. This RDR closes the gap.

The core move: an **operator pool** — `src/nexus/operators/pool.py` — owning a set of long-running `claude` workers driven via the streaming stdin/stdout RPC protocol built into the `claude` CLI. Five operators (`extract`, `rank`, `compare`, `generate`, `summarize`) register as first-class tools on the **existing `nexus` MCP server** (no third MCP server — see Amendment 1 below). Each tool dispatches a structured turn to a pool worker with `--json-schema` enforcement and returns the validated JSON response. The runner gains nothing new in shape — it already dispatches step operators by name — but every operator name now resolves to a pool-backed tool call. In parallel, core MCP tools gain additive structured returns so retrieval steps produce real `{tumblers, ids, distances}` output instead of the current `{"text": str}` wrapper.

## Amendment 1 — pool lives in the `nexus` MCP server, not a third server

Original draft proposed a third FastMCP server (`nexus-operators`) for the operator pool. Design review established this as over-decomposition: every MCP tool that will later call a worker (RDR-080's `nx_answer`, `nx_tidy`, `nx_plan_audit`) needs pool access, and making them cross-server RPC clients to their own process is silly. **The pool is core infrastructure, same tier as T2/T3.**

Concrete shape:
- Pool implementation: `src/nexus/operators/pool.py` (new module).
- Operator tools (`operator_extract`, `operator_rank`, `operator_compare`, `operator_summarize`, `operator_generate`) register via `@mcp.tool()` in `src/nexus/mcp/core.py`.
- `nx/.mcp.json` remains two servers (`nexus`, `nexus-catalog`). No new entry point.
- The pool is a module-level singleton, lazily initialised on first operator call, managed by `mcp_infra.py` (same pattern as T1/T3 singletons).

This amendment is strictly additive to the phase plan — fewer files, less configuration, identical behaviour.

## Problem Statement

### Enumerated gaps to close

#### Gap A: Tool-output contract

Core MCP tools (`search`, `query`, `store_put`, `memory_*`) return human-readable `str`. The RDR-078 Phase-1 runner contract expects `{tumblers, ids, distances}` for retrieval steps. The current `_default_dispatcher` wraps str as `{"text": str}`, which means plan references like `$step1.tumblers` and `$step1.ids` resolve to missing keys at runtime. Seven of the nine seeds chain retrieval into traversal; all seven fail at the second step.

#### Gap B: Operator dispatch

Five operators (`extract`, `rank`, `compare`, `generate`, `summarize`) are not MCP tools. The runner has no subagent-spawn capability, so any plan step naming one of those operators raises `PlanRunToolNotFoundError`. Six of the nine seed plans use at least one operator.

#### Gap C: Empirical `min_confidence` calibration

`min_confidence=0.85` (PQ-2) was seeded from RDR-042's cited AgenticScholar 90% reuse rule and declared calibratable during RDR-078 Phase 1. Calibration did not ship. Real paraphrase behaviour at the chosen threshold is unknown.

#### Gap D: Plan promotion lifecycle

Deferred from RDR-078. The scoped loader can read all four tiers, but writes always land in session, so learned plans decay with the session.

Gaps A and B are the execution blockers. C and D are closure obligations from RDR-078's explicit deferrals.

## Empirical Findings (2026-04-15)

Design decisions below are validated by live test, not speculation. Raw traces in T1 scratch tag `rdr-079-empirical`.

### Finding 1 — `claude -p --input-format stream-json` is a persistent RPC protocol

A single `claude` subprocess accepts multiple user turns on stdin and emits one structured `result` record per turn on stdout. Session ID stays stable across turns. Per-turn records carry `duration_ms`, `total_cost_usd`, `usage.input_tokens`, `usage.output_tokens`, `usage.cache_read_input_tokens`, `usage.cache_creation_input_tokens`. This is the RPC primitive; it is already built into the CLI. No tmux, no terminal emulation, no splash-screen polling.

### Finding 2 — prompt cache reuses across turns

Verified cache HIT: turn 1 created 68,461 cache-ephemeral tokens (system prompt + CLAUDE.md + tool defs); turn 2 read 68,461 tokens from cache. Operator prototype extended this: 3 extractions across one worker showed 207,091 cumulative cache-read tokens. Warm-cache cost is an order of magnitude below cold-cache.

### Finding 3 — `--json-schema` is enforced via a synthetic `StructuredOutput` tool

When `--json-schema '{...}'` is passed, the CLI registers a synthetic tool named `StructuredOutput`. The model emits a tool_use with the validated JSON as `input`. The controller MUST read the tool_use event, not the final `result` text — the last message may be pure thinking with an empty `result` string. This is a protocol detail that P3 implementation depends on.

### Finding 4 — No `ANTHROPIC_API_KEY` required

`claude mcp serve` and `claude -p` inherit whatever authentication the host user has configured: OAuth session via `claude auth login` (first-party Anthropic subscription, including `max`), `ANTHROPIC_API_KEY` env var, `apiKeyHelper`, or enterprise SSO. Verified empirically: `claude mcp serve` completed an MCP initialize/tools-list handshake with **zero** API key env vars set. The single exception is `--bare`, which disables OAuth by design; operator workers must NOT use `--bare`.

### Finding 5 — Operator prototype: 3 extractions in 11.85s server / 18s wall, $0.11, Haiku

Measured per-call amortized: ~4s, ~$0.037. Schema 100% enforced (every output matched `{title, year, author}`). First call is cold-cache cost; subsequent calls in the same worker are ~2× faster. Warm-worker p50 latency for a structured extraction is under 3s for Haiku.

## Design

### `nexus-operators` MCP server

Per Amendment 1: the pool is core infrastructure inside the existing `nexus` MCP server. Pool module at `src/nexus/operators/pool.py`; tool registrations via `@mcp.tool()` in `src/nexus/mcp/core.py`; singleton management in `src/nexus/mcp_infra.py` alongside the existing T1/T3 singletons. No new entry point. No new `.mcp.json` server block.

### Worker pool

Each worker is:
```
claude -p \
  --input-format stream-json \
  --output-format stream-json \
  --verbose \
  --no-session-persistence \
  --session-id <uuid> \
  --append-system-prompt "<operator-role-prompt>" \
  --max-budget-usd <cap> \
  --max-turns <cap> \
  --model haiku
```

Driven via Python `asyncio.create_subprocess_exec`. Controller writes JSON-encoded user turns to `worker.stdin`; a single async read loop parses the `worker.stdout` stream, dispatches `result` and `assistant.tool_use` events back to waiting futures.

Pool sizing from config (`.nexus.yml: operators.pool_size`, default 2). Workers retired at cumulative `input_tokens + output_tokens` threshold (default 150k, well below the 200k window); draining in-flight requests before kill; replacement spawned before the retiree exits to keep the pool saturated.

### Auth inheritance

On first operator call, the pool singleton runs `claude auth status --json`. If `loggedIn == true` (any `authMethod`), pool starts. Otherwise the operator tool returns a clear error: "Operator pool requires authenticated `claude` — run `claude auth login` or set `ANTHROPIC_API_KEY`." No new secret management. The `nexus` MCP server itself still starts without authentication — retrieval tools remain fully available in the unauthenticated path; only operator-requiring tools fail fast.

### Operator tool contract

Each of the five operator MCP tools accepts `(operation, inputs, params)` and returns a typed dict matching the per-operator output schema (from `nx/agents/analytical-operator.md`):

| Operator | Output shape |
|---|---|
| extract | `{extractions: [{...}]}` — object per input, fields per caller's schema |
| rank | `{ranked: [{rank, score, input_index, justification}]}` |
| compare | `{agreements: [...], conflicts: [...], gaps: [...]}` |
| summarize | `{text: str, citations: [{input_index, span}]}` |
| generate | `{text: str, citations: [...]}` |

Each tool's input relay includes a `$schema_version: 1` marker; mismatched schema versions fail with a named error.

### Runner integration

`_default_dispatcher` routes operator-named steps to the corresponding `operator_*` MCP tool on the `nexus` server; retrieval steps continue dispatching to `nexus` / `nexus-catalog`. Core MCP tools unchanged. Gap A is closed by P1: the same core tools gain an additive `_structured: bool = False` parameter; when True, they return the dict shape the runner expects. Existing callers unaffected.

## Phases

- **P1** — Tool-output contract for core MCP tools. Add `_structured` flag to `search`, `query`, `store_put`, `memory_get`, `memory_search`, `memory_put`. Runner passes `_structured=True` on retrieval dispatches. Additive; no breaking change.
- **P2** — Operator pool inside `nexus`. `src/nexus/operators/pool.py`: async worker pool, streaming stdin/stdout JSON parser, worker retirement on token threshold, health probe (periodic no-op turn, timeout → respawn). Singleton bound via `mcp_infra.py`, same pattern as T1/T3. No new MCP server.
- **P3** — Operator implementations. Five tools (`operator_extract`, `operator_rank`, `operator_compare`, `operator_summarize`, `operator_generate`) registered in `src/nexus/mcp/core.py`. Each dispatches a user turn to a pool worker with `--json-schema` matching the operator's output shape, intercepts the `StructuredOutput` tool_use event per Finding 3, returns dict.
- **P4** — Runner integration. `_default_dispatcher` routes operators through the new tools. No changes to `plan_run`, `plan_match`, or plan JSON schema.
- **P5** — Empirical `min_confidence` calibration. 40+-intent paraphrase dataset spanning all five verbs, ROC curve, chosen value recorded in `docs/rdr/rdr-079-calibration.md`. Closes Gap C.
- **P6** — Plan promotion lifecycle. `nx plan promote <plan_id>` CLI with gates (min use count, success rate, description lint). Dry-run mandatory. Closes Gap D.
- **P7** — End-to-end scenario-seed tests. One integration test per seed plan, asserting execution against the real default dispatcher + live operator pool (marked `@pytest.mark.integration` — opt-in, requires auth).

## Success Criteria

- **SC-1** — All 9 RDR-078 seed plans execute end-to-end via the default dispatcher with real MCP tools + live operator pool. Each step produces runner-contract-conformant output.
- **SC-2** — Pool survives hung worker: health probe detects within 10s, respawn within 30s, in-flight request re-queued to a healthy worker without caller-visible failure.
- **SC-3** — Worker retirement at token threshold drains in-flight turns and spawns a replacement without dropping requests. Verified with a deterministic 150k-token fixture.
- **SC-4** — `min_confidence` calibrated against the paraphrase set. ROC artefact committed.
- **SC-5** — RDR-078 test suite passes unchanged. No regression.
- **SC-6** — Cross-server coexistence: `nexus`, `nexus-catalog`, `nexus-operators` run concurrently through an 8-hour soak with no deadlock, no orphaned worker panes, and pool utilisation metrics logged.
- **SC-7** — `--json-schema` output validated per operator. Malformed structured output (`StructuredOutput.input` not matching schema) raises `PlanRunOperatorOutputError` with context, not silent corruption.
- **SC-8** — Median and p95 operator-dispatch latency documented per operator, with cold-pool and warm-pool baselines. Measurement from Finding 5 is the baseline; P3 confirms or updates it.
- **SC-9** — Plan promotion gate rejects sub-threshold plans; `nx plan promote --dry-run` reports the gate verdict without side effects.
- **SC-10** — Graceful degradation without auth: `nexus-operators` pool startup fails fast with a named error; retrieval steps (`search`/`query`/`traverse`) continue working. Plans with no operator steps still execute.

## Risks / Open Questions

- **Cold-worker startup cost** (~5–10s including `claude` init + session warm-up). Mitigated by pool pre-warm at MCP server start and keeping workers alive across many turns.
- **Token-threshold retirement race**: in-flight turns must drain before kill. Worker with 149k tokens accepting one more turn that pushes to 160k is acceptable; push to 200k+ aborts mid-response. Margin of safety in the threshold default.
- **Prompt-cache boundary per worker**: warm cache is per-worker. Dispatch affinity by operator type can raise hit rate at the cost of tail latency when an operator's workers are saturated. Start with unbiased dispatch; measure.
- **Operator output-schema evolution**: `schema_version: 1` pinned at ship. v2 requires compatibility shim or dual-schema support.
- **Concurrent worker auth**: under OAuth, N concurrent `claude` workers share the subscription's rate limit. Under API key, they share the key's rate limit. `429` handling is per-worker with backoff.
- **Final-turn thinking-only response**: Finding 3 showed the `result` text field can be empty when the model's final action is thinking after a tool_use. Controller must not block on the `result` field — wait for the `StructuredOutput` tool_use event or the `result` record with `subtype: success`, whichever comes first.

## Research Findings

Analysis-derived findings (vs the live-test Empirical Findings above). Each records a T2 memory reference for cross-session recall.

### RF-1 — Operator pool belongs inside `nexus`, not a third MCP server (validates Amendment 1)

**Source**: `deep-analyst` architectural analysis 2026-04-15 (T3 store title `analysis-deep-rdr079-boundary-redesign-2026-04-15`). T2 memory: `nexus_rdr/079-research-1-mcp-boundary-analysis`.

**Question investigated**: the initial draft proposed a third FastMCP server (`nexus-operators`) for the operator pool, following the pattern of `nexus` + `nexus-catalog`. Is that the right decomposition?

**Finding**: no. Every downstream MCP tool that will later use worker dispatch (RDR-080's `nx_answer`, `nx_tidy`, `nx_enrich_beads`, `nx_plan_audit`, and any future LLM-backed tool) needs pool access. Making those tools into cross-server MCP clients pointing at their own sibling process is pure overhead — extra `.mcp.json` entry, extra process boundary, extra transport serialisation, identical behaviour. The pool is core infrastructure at the same architectural tier as T1/T2/T3 singletons, not an external tool surface.

**Impact on RDR-079**: Amendment 1 at top of document. `src/nexus/operators/pool.py` (pool module), `src/nexus/mcp/core.py` (tool registrations), `src/nexus/mcp_infra.py` (singleton binding). No new entry point, no third `.mcp.json` block.

**Counterfactual check**: the reasoning for keeping a separate server would be "operator workloads may need independent restart / isolation." This is a valid concern for a latency-critical pool running at production scale; at the current scale (2 workers, personal nexus usage), it's premature. Revisit if operational pain surfaces.

### RF-2 — RDR-042's "MCP server stays LLM-free" constraint is obsolete at the blanket level

**Source**: RDR-042 §Alternatives Considered (verbatim quote in RDR-080 §Key Insight). Empirical Finding 4 above. T2 memory: `nexus_rdr/079-research-2-rdr042-constraint-dissolution`.

**Original constraint** (RDR-042, verbatim): *"MCP tools with direct LLM calls (rejected). Operators as MCP tools that call Anthropic/OpenAI APIs directly. Rejected: couples MCP server to LLM credentials, adds failure mode, breaks the deterministic tool contract."*

**Three concerns and current status**:
1. **Credential coupling** — eliminated. `claude auth status` reports existing host-user session (OAuth from `claude auth login`, `ANTHROPIC_API_KEY`, enterprise SSO, or `apiKeyHelper`). No new secret required, no new config surface.
2. **Failure mode** — scoped, not eliminated. Retrieval tools (`search`, `query`, `store_put`, `memory_*`, `traverse`, catalog tools) remain deterministic and LLM-free. Only operator-requiring tools can fail due to pool unavailability, and they fail fast + gracefully per SC-10.
3. **Contract integrity** — maintained per-tool. Tools that dispatch workers declare it explicitly in their signature and return type. The MCP server is no longer universally LLM-free, but each tool retains a clear contract and the non-LLM subset is preserved.

**Impact on RDR-079**: unblocks P2–P3. Impact on RDR-080: provides the constraint-dissolution reasoning for folding the `query-planner` and `analytical-operator` agents into MCP tools.

### RF-3 — Cost-model change is within existing acceptance tolerance

**Source**: empirical operator prototype (Empirical Finding 5) + repository survey. T2 memory: `nexus_rdr/079-research-3-cost-model-survey`.

**Observation**: RDR-042 assumed ~$0 incremental cost because every operator call happened in the user's own `claude` session. Post-RDR-079, operator calls spend ~$0.037/call on Haiku via the pool. This is a real cost increase per call, but:

- **Baseline already exists**: `src/nexus/commands/taxonomy_cmd.py:862` already spends via parallel `claude -p --model haiku` batch labeling. Users tolerate this; it's in production today.
- **Hot paths stay deterministic**: auto-linker on `store_put`, `taxonomy_assign_hook` post-store, `catalog_auto_link` scratch scanner — none spawn workers. Their determinism contract is preserved.
- **Interactive cost is modest**: a 5-step `nx_answer` call with 1–2 operator steps costs ~$0.05–0.10, well below typical user expectation for a research-grade answer.
- **Budget guard planned**: RDR-080 P5 surfaces daily spend via `.nexus.yml: operators.daily_budget_usd` with 80%/100% thresholds.

**Conclusion**: the cost tradeoff is acceptable. Document the per-tool dispatch-or-not classification in `docs/architecture.md` so future tool authors know which category their addition falls into.

## Assumptions

- `claude` CLI v2.1+ on the host, `claude auth status` returns `loggedIn: true` under any `authMethod`.
- `tmux` NOT required (streaming RPC supersedes it for this use case).
- ChromaDB Cloud + Voyage AI unchanged from RDR-078.
- RDR-078 infrastructure is frozen — no modifications to `plan_match` / `plan_run` / `traverse` / `plans` schema / YAML loader / skills. This RDR is purely additive.
- MCP stdio transport is adequate for operator-pool IPC; no new HTTP bus introduced.
- `ANTHROPIC_API_KEY` remains optional; OAuth sessions are the default path for interactive users.

## Deviations Register

- **Amendment 1 (2026-04-15)** — Moved operator pool from a proposed third MCP server (`nexus-operators`) into the existing `nexus` server. See top of document. Motivation: every tool that will later use workers (including RDR-080's `nx_answer`) needs pool access; cross-process RPC to a sibling server is overhead with no benefit. Pool is core infrastructure, not an external surface.

## References

- RDR-078 — Unified Context Graph and Retrieval. Infrastructure this RDR completes.
- RDR-080 — Retrieval Layer Consolidation. Builds on RDR-079's operator pool to collapse the 3-layer skill→planner→operator relay into a single `nx_answer` MCP tool.
- RDR-067 — Cross-Project RDR Audit Loop. Finding 4 established `claude -p` as the headless dispatch pattern; RDR-079 extends it to the streaming persistent-session variant.
- RDR-042 — AgenticScholar-Inspired Enhancements. Origin of the analytical-operator subagent, whose output contract P3 preserves when porting to MCP tools. §Alternatives Considered rejected "MCP tools with direct LLM calls" on credential-coupling grounds; Finding 4 establishes that OAuth inheritance obsoletes that constraint.
- `nx/agents/analytical-operator.md` — per-operator I/O shapes (canonical source for P3 tool schemas).
- `scripts/batch-label-taxonomy.py`, `src/nexus/commands/taxonomy_cmd.py:862` — prior-art evidence for programmatic `claude` dispatch (one-shot, not streaming — P3 is the streaming upgrade).
