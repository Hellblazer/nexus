---
title: "RDR-079: Operator Dispatch + Plan Execution End-to-End"
status: draft
type: feature
priority: P1
created: 2026-04-15
related: [RDR-042, RDR-067, RDR-078]
reviewed-by: self
---

# RDR-079: Operator Dispatch + Plan Execution End-to-End

RDR-078 shipped the plan-centric retrieval infrastructure — `plan_match`, `plan_run`, typed-graph `traverse`, nine YAML seed plans, the four-tier loader, the plan-first skill family, and the CI schema check. Post-ship critique established that the scenarios it was designed for **cannot execute end-to-end**. The infrastructure is real; the feature is not. This RDR closes the gap.

The core move: a new MCP server, **`nexus-operators`**, owns a pool of **long-running `claude` workers** driven via the streaming stdin/stdout RPC protocol built into the `claude` CLI. Each operator (`extract`, `rank`, `compare`, `generate`, `summarize`) becomes a first-class MCP tool that dispatches a structured turn to a pool worker and returns the validated JSON response. The runner gains nothing new in shape — it already dispatches step operators by name — but every operator name now resolves to a pool-backed tool call. In parallel, core MCP tools gain additive structured returns so retrieval steps produce real `{tumblers, ids, distances}` output instead of the current `{"text": str}` wrapper.

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

A third FastMCP server alongside `nexus` and `nexus-catalog`. Registration follows the existing pattern:
- Entry point: `nx-mcp-operators = "nexus.mcp.operators:main"` in `pyproject.toml`.
- `nx/.mcp.json` gains a third server block.
- Module: `src/nexus/mcp/operators.py` instantiates `FastMCP("nexus-operators")` and registers five tools.

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

Pool startup runs `claude auth status --json`. If `loggedIn == true` (any `authMethod`), pool starts. Otherwise `nexus-operators` fails fast with a clear error: "Operator pool requires authenticated `claude` — run `claude auth login` or set `ANTHROPIC_API_KEY`." No new secret management.

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

`_default_dispatcher` routes operator-named steps to the corresponding `nexus-operators` MCP tool; retrieval steps continue dispatching to `nexus` / `nexus-catalog`. Core MCP tools unchanged. Gap A is closed by P1: the same core tools gain an additive `_structured: bool = False` parameter; when True, they return the dict shape the runner expects. Existing callers unaffected.

## Phases

- **P1** — Tool-output contract for core MCP tools. Add `_structured` flag to `search`, `query`, `store_put`, `memory_get`, `memory_search`, `memory_put`. Runner passes `_structured=True` on retrieval dispatches. Additive; no breaking change.
- **P2** — `nexus-operators` MCP server scaffold. FastMCP registration, async worker pool (`src/nexus/operators/pool.py`), streaming stdin/stdout JSON parser, worker retirement on token threshold, health probe (periodic no-op turn, timeout → respawn).
- **P3** — Operator implementations. Five MCP tools, each dispatches a user turn to a pool worker with `--json-schema` matching the operator's output shape, intercepts the `StructuredOutput` tool_use event per Finding 3, returns dict.
- **P4** — Runner integration. `_default_dispatcher` routes operators through `nexus-operators`. No changes to `plan_run`, `plan_match`, or plan JSON schema.
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

## Assumptions

- `claude` CLI v2.1+ on the host, `claude auth status` returns `loggedIn: true` under any `authMethod`.
- `tmux` NOT required (streaming RPC supersedes it for this use case).
- ChromaDB Cloud + Voyage AI unchanged from RDR-078.
- RDR-078 infrastructure is frozen — no modifications to `plan_match` / `plan_run` / `traverse` / `plans` schema / YAML loader / skills. This RDR is purely additive.
- MCP stdio transport is adequate for operator-pool IPC; no new HTTP bus introduced.
- `ANTHROPIC_API_KEY` remains optional; OAuth sessions are the default path for interactive users.

## Deviations Register

*(empty at draft time)*

## References

- RDR-078 — Unified Context Graph and Retrieval. Infrastructure this RDR completes.
- RDR-067 — Cross-Project RDR Audit Loop. Finding 4 established `claude -p` as the headless dispatch pattern; RDR-079 extends it to the streaming persistent-session variant.
- RDR-042 — AgenticScholar-Inspired Enhancements. Origin of the analytical-operator subagent, whose output contract P3 preserves when porting to MCP tools.
- `nx/agents/analytical-operator.md` — per-operator I/O shapes (canonical source for P3 tool schemas).
- `scripts/batch-label-taxonomy.py` — prior-art evidence for programmatic `claude` dispatch in this repo.
- `src/nexus/commands/taxonomy_cmd.py:862` — parallel-worker `claude -p` batch pattern (one-shot, not streaming — P3 is the streaming upgrade).
