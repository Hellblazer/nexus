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

### Pool home: the `nexus` MCP server

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

### Worker isolation — explicit session identity via `NEXUS_T1_SESSION_ID`

Pool workers are `claude` subprocesses descended from the `nexus` MCP server, itself descended from the user's `claude` session. Without mitigation, nexus's PPID-walk session discovery (implemented in `src/nexus/session.py`) would make each worker a peer in the user's session — sharing T1 scratch, racing writes, receiving the plan-first preamble, firing auto-linker on any `store_put` against the user's catalog.

**The load-bearing mitigation is explicit session identity.** Nexus's T1 session-discovery mechanism already has two layers: the SessionStart hook writes a session file at `~/.config/nexus/sessions/{ppid}.session`; child processes walk the OS PPID chain to find the nearest ancestor. This RDR adds a **third, higher-priority** layer: a `NEXUS_T1_SESSION_ID` environment variable that overrides PPID-walk entirely. The pool spawns workers with that variable set to a pool-scoped session UUID, not the user's PPID.

#### Pool session lifecycle

At first-operator-call the pool singleton:
1. Generates a UUID (e.g. `pool-7f3a...`), writes `~/.config/nexus/sessions/pool-7f3a...session` containing the pool's own T1 endpoint **and** `pool_pid: os.getpid()` (the pool-owner process's PID — required for liveness reconciliation, see step 4).
2. Retains the UUID as `pool.session_id` for the lifetime of the pool.
3. Writes the pool's own T1 endpoint (a dedicated ChromaDB HTTP server — required for SC-11's cross-process scratch-sentinel test, since an `EphemeralClient` cannot be queried from outside the owning process).
4. **Startup reconciliation**: before writing its own session file, the pool scans `~/.config/nexus/sessions/pool-*.session`; for each one, reads the JSON, extracts `pool_pid`, and probes liveness with `os.kill(pool_pid, 0)` (`OSError` → dead). Dead-pool session files are removed. PID reuse is possible but unlikely at scale; the combination of UUID-in-filename + PID-in-record means a reused PID would have to match a different pool's stored PID exactly, which is vanishingly rare. If belt-and-suspenders is wanted, include `created_at` and age-check alongside the liveness probe.
5. On pool shutdown (graceful — MCP server stop): removes its own session file, closes the T1 HTTP server.

Session record schema extension (add to existing `write_session_record`):
```python
{
  "session_id": "pool-<uuid>",         # existing field, new namespace
  "server_pid": <chromadb-pid>,        # existing field — the T1 HTTP server
  "pool_pid": <pool-owner-pid>,        # NEW: this pool's own process PID
  "pool_session": true,                # NEW: marker that distinguishes pool from user
  "created_at": "...",                 # existing
  "endpoint": "http://127.0.0.1:..."   # existing
}
```

Worker spawn:
```
claude -p \
  ... (streaming flags) ...
  # env:
  NEXUS_T1_SESSION_ID=pool-7f3a...
```

#### T1 client change — four call sites, not one

The env-var guard must be applied at **every** point that resolves a T1 session. `find_ancestor_session` in `src/nexus/session.py` is called from four sites across three files:

| Site | File:Line | Role |
|---|---|---|
| T1 client init | `src/nexus/db/t1.py:140` | First T1 connection |
| T1 client reconnect | `src/nexus/db/t1.py:175` | After server-side failure — CRITICAL, skipping here silently re-attaches worker to user session |
| Hook `session_end` | `src/nexus/hooks.py:153` | Cleanup; needs to look up the right session |
| Hook `session_start` | `src/nexus/hooks.py:91` | Inside lock-acquire path; must check env BEFORE spawning a new T1 server |

Implementation shape is a new public function `resolve_t1_session()` that encapsulates the env-first logic and replaces all four call sites. ~15-20 lines across `session.py` + `t1.py` + `hooks.py`, not 5 in one place. Shape:

```python
def resolve_t1_session() -> SessionRecord | None:
    explicit = os.environ.get("NEXUS_T1_SESSION_ID")
    if explicit:
        session_file = SESSIONS_DIR / f"{explicit}.session"
        if session_file.exists():
            return _load_session_record(session_file)
        # explicit-but-missing: fall through to PPID-walk
        # (documented behavior; tested by SC-14(b))
    return find_ancestor_session()  # existing PPID-walk
```

Every call site replaces `find_ancestor_session()` with `resolve_t1_session()`. The SessionStart hook's behavior becomes: "if `resolve_t1_session()` returns a live session, use its endpoint; only spawn a new T1 server if both env is unset AND no ancestor session is found." This was the existing semantics; the new function just adds the explicit-env-first layer.

#### Worker tool surface

Workers retain MCP access — this is now safe because their scratch/store_put/catalog writes land in the pool's T1 (isolated from the user's), not the user's T1. This opens up legitimate uses (e.g. a `summarize` operator reading chunk text via `store_get`, or multi-step operator pipelines using pool-scoped scratch for coordination).

**However**, workers must NOT be able to re-enter the pool recursively. Restrict the exposed tool set to exclude the dispatch surface:

```
--tools "Read,Grep,Glob"                                   # built-in Claude tools for limited file access
--mcp-config <pool-worker-mcp.json>                        # custom MCP config
--strict-mcp-config                                        # only the listed servers attach
```

`pool-worker-mcp.json` attaches the `nexus` and `nexus-catalog` MCP servers but hides the recursion-enabling tools. Achievable via MCP's allow-list mechanism, or via a worker-scoped `nexus` entry point that registers every tool EXCEPT `plan_match` / `plan_run` / `operator_extract` / `operator_rank` / `operator_compare` / `operator_summarize` / `operator_generate`. The simplest shape is a new `nx-mcp --worker-mode` flag that does the filtering at registration time.

#### What the controller (not worker) still owns

1. Plan execution: `plan_run` never runs inside a worker — only inside the controller process.
2. Operator dispatch routing: deciding which pool worker to send a step to.
3. Run metrics: `operator_runs` T2 writes happen in the controller, attributed to the user's session (intentional — user's session accumulates cost history).
4. Hydration optimisation: when a step's input is a list of IDs, the controller MAY pre-hydrate by calling `store_get` before dispatch (avoids N round-trips). Workers CAN also fetch themselves; pre-hydration is an optimisation, not a requirement.

#### Invariants

- **I-1**: Worker writes (scratch, store_put, catalog links) never appear in the user's T1. Tested by writing from a worker, asserting absence from user-session scratch list.
- **I-2**: A worker cannot call `plan_match`, `plan_run`, or any `operator_*` tool (no recursion). Tested by worker-scope tool listing assertion.
- **I-3**: When the pool shuts down cleanly, no orphaned `pool-*.session` files remain. Tested by starting + stopping a pool and grepping the sessions directory.
- **I-4**: If `NEXUS_T1_SESSION_ID` is unset (user runs `claude` normally), session discovery falls back to PPID-walk — no regression on RDR-078 behavior. Tested by running existing T1 tests with the env var clear.

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
- **P2** — Operator pool inside `nexus`. Five deliverables:
  1. `src/nexus/operators/pool.py`: async worker pool, streaming stdin/stdout JSON parser, worker retirement on token threshold, health probe (periodic no-op turn, timeout → respawn), `PoolConfigError`, `spawn_worker()` asserts `NEXUS_T1_SESSION_ID` is set before launching subprocess (SC-15). Singleton bound via `mcp_infra.py`, same pattern as T1/T3.
  2. Pool session lifecycle: generate pool UUID at first-operator-call, write `~/.config/nexus/sessions/pool-<uuid>.session` with pool's dedicated T1 HTTP endpoint AND `pool_pid = os.getpid()` in the record JSON. Graceful shutdown removes the file. Startup reconciliation scans `pool-*.session` files, probes `pool_pid` liveness via `os.kill(pid, 0)`, removes dead entries.
  3. Explicit session-identity support: new `resolve_t1_session()` function in `src/nexus/session.py` that checks `NEXUS_T1_SESSION_ID` first and falls through to `find_ancestor_session()` otherwise. Replaces `find_ancestor_session()` calls at **four sites**: `src/nexus/db/t1.py:140` (T1 client init), `src/nexus/db/t1.py:175` (T1 client reconnect — critical: skipping here silently re-attaches worker to user session), `src/nexus/hooks.py:91` (session_start lock-path), `src/nexus/hooks.py:153` (session_end cleanup). Total ~15-20 lines across three files.
  4. Worker-mode MCP entry point (`nx-mcp --worker-mode` or equivalent env-checked registration): exposes `nexus` and `nexus-catalog` tools EXCEPT `plan_match`, `plan_run`, and the five `operator_*` tools — prevents pool-recursion by construction. Workers spawn with `--mcp-config <worker-mode.json> --strict-mcp-config`.
  5. Session-record schema extension: add `pool_pid: int` and `pool_session: bool` fields to `write_session_record` for pool-scoped sessions. User-scoped session records leave these absent (backward compatible).
- **P3** — Operator implementations. Five tools (`operator_extract`, `operator_rank`, `operator_compare`, `operator_summarize`, `operator_generate`) registered in `src/nexus/mcp/core.py`. Each dispatches a user turn to a pool worker with `--json-schema` matching the operator's output shape, intercepts the `StructuredOutput` tool_use event per Finding 3, returns dict.
- **P4** — Runner integration. `_default_dispatcher` routes operators through the new tools. No changes to `plan_run`, `plan_match`, or plan JSON schema.
- **P5** — Empirical `min_confidence` calibration. 40+-intent paraphrase dataset spanning all five verbs, ROC curve, chosen value recorded in `docs/rdr/rdr-079-calibration.md`. Closes Gap C.
- **P6** — Plan promotion lifecycle. `nx plan promote <plan_id>` CLI with gates (min use count, success rate, description lint). Dry-run mandatory. Closes Gap D.
- **P7** — End-to-end scenario-seed tests. One integration test per seed plan, asserting execution against the real default dispatcher + live operator pool (marked `@pytest.mark.integration` — opt-in, requires auth).

## Success Criteria

- **SC-1** — All 9 RDR-078 seed plans execute end-to-end via the default dispatcher with real MCP tools + live operator pool. Each step produces runner-contract-conformant output. Verified by P7 integration tests in `tests/integration/test_rdr078_seeds_e2e.py` (one test per seed, `@pytest.mark.integration`).
- **SC-2** — Pool survives hung worker: health probe detects within 10s, respawn within 30s, in-flight request re-queued to a healthy worker without caller-visible failure.
- **SC-3** — Worker retirement at token threshold drains in-flight turns and spawns a replacement without dropping requests. Verified with a deterministic 150k-token fixture.
- **SC-4** — `min_confidence` calibrated against the paraphrase set. ROC artefact committed.
- **SC-5** — RDR-078 test suite passes unchanged. No regression.
- **SC-6** — Operator pool coexistence inside `nexus`: the two MCP servers (`nexus`, `nexus-catalog`) run with the in-process pool active for an 8-hour soak with no deadlock, no orphaned workers, and pool utilisation metrics logged. The pool's singleton lifecycle must not interfere with T1/T2/T3 singleton initialisation or teardown.
- **SC-7** — `--json-schema` output validated per operator. Malformed structured output (`StructuredOutput.input` not matching schema) raises `PlanRunOperatorOutputError` with context, not silent corruption.
- **SC-8** — Median and p95 operator-dispatch latency documented per operator, with cold-pool and warm-pool baselines. Instrumented via `structlog` on the pool worker (per-turn `duration_ms`, `total_cost_usd`, `usage.*` already emitted by `claude`'s streaming protocol per Empirical Finding 1), aggregated by `nx doctor --operators`, committed as a baseline table in `docs/rdr/rdr-079-latency-baselines.md`. Measurement from Finding 5 is the seed; P3 confirms or updates.
- **SC-9** — Plan promotion gate rejects sub-threshold plans; `nx plan promote --dry-run` reports the gate verdict without side effects.
- **SC-10** — Graceful degradation without auth: when `claude auth status` reports `loggedIn: false`, the first operator-requiring MCP call returns a named `PlanRunOperatorUnavailableError` with guidance ("run `claude auth login` or set `ANTHROPIC_API_KEY`"); retrieval steps (`search`/`query`/`traverse` + all of `nexus-catalog`) continue working unchanged. Plans with no operator steps still execute end-to-end. Testable via a fixture that patches `claude auth status` output.
- **SC-11** — Worker T1 isolation via explicit session identity. Behavioral test: (a) start a pool, spawn a worker with `NEXUS_T1_SESSION_ID=pool-<uuid>`; (b) from inside the worker, `scratch put` a sentinel tagged `sc11-isolation-probe`; (c) query user session scratch (PPID-walk discovery, no env) and assert the sentinel is absent; (d) query the pool session scratch (by session file path) and assert the sentinel is present. This validates invariants I-1 (worker writes not visible in user T1) and — indirectly — I-4 (PPID-walk still works for normal users by NOT setting the env).
- **SC-12** — Worker tool-surface restriction. Test: invoke a worker's MCP `tools/list` endpoint (via its stdio channel). Assert the returned tool names include `search` / `query` / `store_get` / `catalog_show` (workers have legitimate MCP access) AND exclude `plan_match` / `plan_run` / `operator_extract` / `operator_rank` / `operator_compare` / `operator_summarize` / `operator_generate` (workers cannot recurse into the pool). This validates invariant I-2.
- **SC-13** — Pool session cleanup + PID-liveness reconciliation. Three sub-assertions: (a) graceful stop — start a pool, capture the pool-session file path, gracefully stop the pool, assert the file no longer exists; (b) startup reconciliation — write a stale `pool-dead-uuid.session` file with `pool_pid` set to a dead PID (e.g., spawn + wait + reuse its PID), start a new pool, assert the stale file was removed by reconciliation; (c) startup preserves live peer — write a `pool-live-uuid.session` whose `pool_pid` IS alive (use the current test-process PID), start a new pool, assert the live-peer file is NOT removed. Validates invariant I-3.
- **SC-14** — PPID-walk regression + fall-through guard. Two sub-assertions that together actually exercise both code paths: (a) unset branch — run the full RDR-078 T1 session test suite with `NEXUS_T1_SESSION_ID` NOT set, assert zero failures (regression guard against catastrophic deletion of PPID-walk); (b) fall-through branch — unit test: set `NEXUS_T1_SESSION_ID=nonexistent-uuid-xyz`, call `resolve_t1_session()`, assert it returns the same result as `find_ancestor_session()` (the env-specified file doesn't exist so the code must fall through to PPID-walk). Without (b), (a) alone is a tautology — exercise the else-branch explicitly. Validates invariant I-4.
- **SC-15** — Pool refuses to spawn workers without an explicit session identity. Test: attempt to spawn a worker with `NEXUS_T1_SESSION_ID` missing from the env, assert the spawn function raises `PoolConfigError` (or equivalent named error) BEFORE launching the subprocess. This makes the single load-bearing safety mechanism a loud failure, not a silent one. The Risks section calls this "turning a silent correctness issue into a loud error" — this SC pins the loudness.

## Risks / Open Questions

- **Cold-worker startup cost** (~5–10s including `claude` init + session warm-up). Mitigated by pool pre-warm at MCP server start and keeping workers alive across many turns.
- **Token-threshold retirement race**: in-flight turns must drain before kill. Worker with 149k tokens accepting one more turn that pushes to 160k is acceptable; push to 200k+ aborts mid-response. Margin of safety in the threshold default.
- **Prompt-cache boundary per worker**: warm cache is per-worker. Dispatch affinity by operator type can raise hit rate at the cost of tail latency when an operator's workers are saturated. Start with unbiased dispatch; measure.
- **Operator output-schema evolution**: `schema_version: 1` pinned at ship. v2 requires compatibility shim or dual-schema support.
- **Concurrent worker auth**: under OAuth, N concurrent `claude` workers share the subscription's rate limit. Under API key, they share the key's rate limit. `429` handling is per-worker with backoff.
- **Final-turn thinking-only response**: Finding 3 showed the `result` text field can be empty when the model's final action is thinking after a tool_use. Controller must not block on the `result` field — wait for the `StructuredOutput` tool_use event or the `result` record with `subtype: success`, whichever comes first.
- **`claude auth status --json` schema drift**: the auth guard at pool startup parses this command's output. A future `claude` CLI release could rename `loggedIn` → `isLoggedIn` or similar. Mitigation: defensive parse (check key presence before trusting value), pin a minimum tested `claude` CLI version in `pyproject.toml` docs, add a CI smoke test that runs `claude auth status --json` and asserts the `loggedIn` key is present.
- **T1 isolation correctness is load-bearing**: the `NEXUS_T1_SESSION_ID` env var is the single mechanism that keeps worker writes out of the user's T1. If a worker is spawned WITHOUT the env var set, it falls back to PPID-walk and becomes a peer in the user's session. Mitigation: the pool's worker-spawn code MUST set the env var; an assertion in the spawn function (refuses to spawn without it) turns a silent correctness issue into a loud error. SC-11 tests the happy path; a negative test ("pool refuses to spawn worker without `NEXUS_T1_SESSION_ID`") would pin the invariant from the other side.
- **Stale pool-session files on crash**: if the nexus MCP server crashes (SIGKILL, segfault, OS power-cut), the pool's on-shutdown cleanup never runs. `~/.config/nexus/sessions/pool-*.session` files leak. Mitigation: pool startup does reconciliation — scan for `pool-*` session files whose PID is no longer alive, remove them. SC-13's "after SIGTERM" sub-assertion covers this.
- **Tool-surface restriction via worker-mode MCP config**: requires either a new `nx-mcp --worker-mode` flag OR per-tool allow-list support in FastMCP. The first is simpler (filter at tool registration time via an env var check). P2 must include this machinery.
- **Controller `$stepN.*` hydration — optional optimisation, not a requirement**: workers in the new design CAN call `store_get` themselves. Pre-hydration in the controller is a latency optimisation (saves MCP boundary crossings on lists of IDs). If implemented as `store_get_many(doc_ids: list)`, it MUST batch at ≤ 300 IDs per ChromaDB call per the `MAX_QUERY_RESULTS` quota; explicit error on larger lists, not silent truncation. Baseline the non-batched cost in P4 first; add batching if measured cost warrants it.
- **Pool session discoverable by name**: a curious user who runs `ls ~/.config/nexus/sessions/` will see `pool-*.session` files. Document this in `docs/architecture.md` so it's not mistaken for a leak when the pool is legitimately running.

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

### RF-4 — Explicit session identity is the right mitigation for T1 isolation (pivot)

**Source**: architectural review of worker-isolation risks + user feedback ("can't we pass the pid of the calling session in?"). T2 memory: `nexus_rdr/079-research-4-worker-isolation`.

**Question investigated**: how do pool workers avoid polluting the user's T1 session (scratch tag collisions, auto-linker leakage, plan-first recursion)?

**First attempt (rejected)**: lock workers down by denial — no MCP tools, no hooks, no skills, four independent guards. This was overfit to the risks. The post-isolation gate critic (agent `a50ab8f72b02a920b`) correctly identified three real problems with it: (i) SC-12 enumerated hypothetical `.sh` hook files that don't exist (real hooks are the 6 in `nx/hooks/scripts/*.sh`, most don't do T1 work at all); (ii) the PPID-walk logic lives in `src/nexus/session.py`, not shell scripts; (iii) SC-11's `lsof`/ChromaDB-client-count assertion was unverifiable.

**Pivot**: make session identity **explicit** via `NEXUS_T1_SESSION_ID`. Nexus already has a session-discovery protocol (SessionStart writes `~/.config/nexus/sessions/{ppid}.session`, PPID-walk finds the nearest ancestor). Adding a higher-priority env-var override is a ~5-line change in `session.py`. Pool spawns workers with `NEXUS_T1_SESSION_ID=pool-<uuid>`; workers join the pool's T1, not the user's.

**Why this is better than the four-guard lockout**:
- **One mitigation (explicit session identity) replaces four** (spawn-flags, env var, hook short-circuits, empty tool surface).
- **Workers retain MCP access** — legitimately useful for operators that need `store_get` or pool-scoped scratch, without polluting the user session.
- **Aligns with existing mechanisms** — nexus already tracks session identity; we're making it explicit, not inventing new machinery.
- **Cleaner regression story** — RDR-078 T1 tests keep passing unchanged; when `NEXUS_T1_SESSION_ID` is unset (the normal user case), PPID-walk still works exactly as before.

**Remaining constraint**: workers must not be able to recurse into the pool. Solved by the tool-surface restriction (`--mcp-config` with a worker-mode nexus entry point that omits `plan_match` / `plan_run` / `operator_*`).

**Implication**: the conceptual framing in the rejected version still holds (operators are pure-compute leaves; planner/coordinator live in the controller). But the ENFORCEMENT mechanism is a single load-bearing env var, not four overlapping guards. Simpler, cleaner, covered by SC-11..SC-14.

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
- RDR-078 infrastructure is frozen at its **external contracts** — no changes to the public signatures / return shapes of `plan_match`, `plan_run`, `traverse`, the `plans` schema, the YAML loader, or the plan-first skill family. P4's `_default_dispatcher` routing update is an internal implementation detail of `plan_run` (explicitly named as an integration point in Gap A and Design §Runner integration) and does not alter `plan_run`'s external contract. This RDR is additive from every caller's perspective.
- MCP stdio transport is adequate for operator-pool IPC; no new HTTP bus introduced.
- `ANTHROPIC_API_KEY` remains optional; OAuth sessions are the default path for interactive users.

## Out of Scope (may spawn follow-on RDRs)

- **`nx_answer` MCP tool** — belongs to RDR-080 (Retrieval Layer Consolidation). RDR-079 delivers the operator pool that `nx_answer` will consume; RDR-079 does NOT build `nx_answer` itself.
- **Streaming operator output back to MCP callers** — current design returns the full `StructuredOutput` payload after the worker finishes. Partial-output streaming is plausible but deferred.
- **Multi-LLM operator routing** — all operators dispatch to the same `claude` pool (currently Haiku). Routing a specific operator type to Sonnet or to a different provider is a later extension.
- **Auto-plan-promotion from pool runs** — P6 ships a manual `nx plan promote` CLI with gates. An automatic "learn-from-successful-runs" loop is out of scope.
- **Worker affinity by operator type** — uniform dispatch at P2; affinity may follow measurement.
- **Deleting the `analytical-operator` agent** — belongs to RDR-080 P1/P2. RDR-079 P3 ships the replacement MCP tools; the agent file stays until RDR-080 consolidates the consumers.

## Deviations Register

- **Amendment 1 (2026-04-15)** — Moved operator pool from a proposed third MCP server (`nexus-operators`) into the existing `nexus` server. See top of document. Motivation: every tool that will later use workers (including RDR-080's `nx_answer`) needs pool access; cross-process RPC to a sibling server is overhead with no benefit. Pool is core infrastructure, not an external surface.

## References

- RDR-078 — Unified Context Graph and Retrieval. Infrastructure this RDR completes.
- RDR-080 — Retrieval Layer Consolidation. Builds on RDR-079's operator pool to collapse the 3-layer skill→planner→operator relay into a single `nx_answer` MCP tool.
- RDR-067 — Cross-Project RDR Audit Loop. Finding 4 established `claude -p` as the headless dispatch pattern; RDR-079 extends it to the streaming persistent-session variant.
- RDR-042 — AgenticScholar-Inspired Enhancements. Origin of the analytical-operator subagent, whose output contract P3 preserves when porting to MCP tools. §Alternatives Considered rejected "MCP tools with direct LLM calls" on credential-coupling grounds; Finding 4 establishes that OAuth inheritance obsoletes that constraint.
- `nx/agents/analytical-operator.md` — per-operator I/O shapes (canonical source for P3 tool schemas).
- `scripts/batch-label-taxonomy.py`, `src/nexus/commands/taxonomy_cmd.py:862` — prior-art evidence for programmatic `claude` dispatch (one-shot, not streaming — P3 is the streaming upgrade).
