# Workflow engine + ext-apps: human-in-the-loop without durable state at v1

A design note exploring how a Parmar-style MCP workflow engine combines with
[`modelcontextprotocol/ext-apps`](https://github.com/modelcontextprotocol/ext-apps)
to support human-in-the-loop steps, and how cross-session survival can be
added incrementally rather than baked in from day one.

## Context

Two separate ideas:

**Workflow engine** ([Parmar 2026](https://arxiv.org/html/2605.00827v1)): LLM
reasoning happens once at design time, producing a declarative JSON blueprint.
The paper enumerates **four** DSL step types: `call`, `loop`, `parallel`,
`pipe`. This document adopts those four and adds a fifth, **`collect`**, for
batch aggregation across iterations — an extension, not part of Parmar's spec.
Subsequent executions run via a deterministic engine with no agent in the
loop. The engine is an **MCP Mediator** (the paper's own framing, Section 6.3)
— simultaneously an MCP server (to the agent) and an MCP client (to
downstream tool MCPs). Existing tools require no modification.

**Modeled wins** (the paper estimates per-step costs from conservative
assumptions; numbers are not instrumented from a live agent run, and the
evaluation covers a single Kubernetes CMDB synchronization domain): >99%
token reduction and 40–80× speedup amortized over 5+ runs. The paper's cost
table reports the *design* phase at ~4,150 tokens (one-time) and a single
*agent-driven execution* at ~54,750 tokens; the engine eliminates the
per-execution cost, so the savings are real but their magnitude is a
projection from one workload.

**ext-apps**: MCP servers expose `ui://` HTML resources rendered as sandboxed
iframes by the host (Claude, ChatGPT). PostMessage gives bidirectional comms —
the server pushes state into the iframe via notifications; the iframe calls
back into MCP tools through the host.

This page assumes the workflow engine is implemented and explores
how it combines with UI-emitting MCPs.

## Three combination patterns

**1. Workflow-as-app.** A compiled blueprint registers itself with the agent
as a single MCP tool *and* a single `ui://` resource. The agent calls the
workflow; the host renders the engine's iframe. Underneath, N steps execute
deterministically while the engine streams state into the iframe via
PostMessage notifications — progress, partial results, the live DAG. Keeps
the per-execution token cost near 150 tokens but recovers the running
narration the user loses by taking the agent out of the loop.

**2. Human-in-the-loop checkpoints.** A workflow can suspend at a UI
checkpoint, surface state to the user, and resume on user input. This is the
focus of the rest of this document.

**3. Blueprint authoring as an app.** The design phase (~4k tokens in the
paper, one-time per blueprint) currently emits JSON. An ext-apps editor —
DAG view, primitive palette, JMESPath inspector, inline runtime metrics from
`plan-inspect` — lets the LLM produce a draft and the user refine visually
before `plan_save`. Pairs with the existing `plan_match` / `plan_promote`
lifecycle.

The headline pairing is (1) + a streaming-progress iframe — smallest engine
change, highest UX delta, no DSL fork. (2) is the more interesting design
conversation and the bigger commitment. (3) is meta and orthogonal to the
others.

## Pattern (2): the blocking model

The temptation is to model human-in-the-loop as "the engine *suspends*
mid-blueprint and *resumes* later." That framing imports Temporal/Cadence
machinery — durable state, workflow-id-keyed checkpoint store, separate
resume tool, deferred-handle agent contract. All of which is overkill at v1.

The reframe: **every step the engine runs is already a blocking MCP call**.
The engine makes a synchronous RPC to a tool MCP, waits for the response,
then runs the next step. A human-in-the-loop step is just a blocking MCP
call to a UI-emitting MCP server, which doesn't return until the iframe
PostMessages a result back. From the engine's perspective, it's an ordinary
slow tool call. The complexity of "wait for human" lives inside the UI-MCP
server, which is already the kind of place async lives.

Consequences:

- Engine's variable bindings, step pointer, partial results stay in process
  memory. No checkpoint store needed.
- No `workflow_id`-keyed resume tool. The blocking RPC is the resume mechanism.
- The agent that invoked the workflow blocks the same way it blocks for any
  other long tool call. No deferred-handle protocol.
- `parallel { await_user; await_user }` is just two concurrent RPCs to
  UI-MCPs. The existing primitive composes for free.

So `await_user` is not a new primitive. Mechanically, it's `call(ui-mcp.confirm, ...)`
that happens to take human time. What it deserves is a **semantic annotation**
on `call`, used for static checks (warn about timeout exposure), policy
(human-in-loop steps may need different auth), observability (separate user
thinking time from tool latency), and audit (compliance often requires
explicit human-approval markers).

### The genuine alternative: yield-based `await_user`

The blocking model is the doc's chosen v1. The genuine alternative is not "a
sixth primitive that's a renamed slow `call`" — it's a different execution
shape:

- The `await_user` step returns immediately to the engine's caller with
  `{status: "pending_user_input", workflow_id, checkpoint_id, prompt,
  visible_state}`. No held-open MCP connection.
- Engine writes parked state, releases the connection, exits if it wants.
- Later, user input arrives via `engine.resume(workflow_id, checkpoint_id,
  payload)`. Engine loads state and continues.

Properties differ along three axes:

| | Blocking | Yield |
| --- | --- | --- |
| Transport timeout exposure | High (connection held for human time) | None (connection closes immediately) |
| Cross-session survival | Requires v2 disconnect detection + parking | Native — the engine is *always* free between steps |
| Agent contract | Single `tools/call` returns the final result | Caller receives `pending` and must poll, subscribe, or accept eventual completion via a separate channel |
| Connection / pool exhaustion | Real concern under autonomous-loop kicking off many approval-gated workflows | Not applicable |
| Engine process liveness during wait | Required | Irrelevant — engine can exit and restart |

v1 picks blocking because the agent contract stays simple (workflows look
like ordinary tools) and v1 explicitly disclaims cross-session survival. The
honest read of v2: the doc's v2 design *bolts disconnect-detection and
parking onto the blocking path*, recovering most yield properties at the
cost of additional machinery (heartbeat liveness, deferred handles,
`engine.resume`). Yield arrives at the same destination by design rather
than by retrofit.

**v2 default: yield, not blocking-with-park.** Three independent lines
of argument converge on this:

- **Human-in-the-loop taxonomy across the industry.** AWS Step Functions
  `.waitForTaskToken`, Restate awakeables, Inngest `waitForEvent`,
  Camunda BPMN user tasks in async mode — every mature workflow system
  that supports cross-session human input does so by releasing the
  connection and resuming via a separate call with a token. The
  blocking-with-park alternative exists primarily where the runtime
  cannot release compute (legacy server-bound engines). For an engine
  that can exit and restart freely between steps, yield is the
  industry-converged answer.
- **Multi-channel UI surface.** ext-apps iframes are one human-loop
  substrate. Voice (Twilio bidirectional), Slack thread, mobile push
  with deep-linked response, plain email with reply parsing all fit
  the same "deliver prompt, await callback" shape — but only the
  voice case can plausibly hold a connection. Any substrate with
  human-time response cycles measured in hours forces yield. If the
  engine wants to be a generic interaction substrate, not just an
  iframe-orchestration substrate, yield is the only viable model.
- **The "always-fresh" steelman.** Many tools are already idempotent
  by good practice (or can be made so with a dedup key). For
  workflows whose tools meet that bar, yield with agent-driven
  re-issue subsumes most of v2 durability's complexity — see the
  "Design space kept open" section's "no v2 durability ever"
  alternative.

The doc's previous framing of v2 as "add disconnect-detection and
parking to a blocking call" remains a valid alternative path; it is no
longer the assumed one. The v1 shape constraints (especially #6, the
discriminated union over `completed` / `parked` / `pending_user_input`)
admit either v2 path. The blocking model is what v1 ships; yield is what
v2 should adopt unless an experiment surfaces a reason to prefer parking.

### What the blocking model *does* require

Three operational constraints that are real but smaller than "build a
workflow runtime":

- **Transport timeouts end-to-end.** Three or four layers of
  blocking — host→engine, engine→UI-MCP, UI-MCP→iframe (plus the
  Sandbox-proxy hop on web hosts) — and any of them can time out.
  The *downward* direction (engine→UI-MCP) is solved by per-step
  timeouts declared in the blueprint with engine-side cancellation.
  The *upward* direction (host→engine, where the host's MCP client
  may impose a tool-call timeout that's neither configurable nor
  known in advance) is the harder problem.

  **What the MCP spec actually says.** Lifecycle § "Timeouts":
  implementations SHOULD set per-request timeouts and SHOULD enforce
  a maximum timeout regardless of progress. Implementations **MAY**
  reset the timeout clock on receiving a progress notification — the
  spec-sanctioned lever for keeping long-running tool calls alive.
  **stdio has no application-layer keepalive in the MCP spec**, but
  emitting `notifications/progress` periodically is the
  spec-blessed way to extend a host's tool-call window for hosts
  that honor the MAY clause. Hosts that don't will cancel anyway.

  **Streamable HTTP** (the current preferred HTTP transport,
  replacing the older HTTP+SSE transport from 2024-11-05) supports
  **explicit resumability**: servers MAY attach `id` to SSE events,
  clients reconnect with `Last-Event-ID`. Multiple SSE streams MAY
  be open simultaneously. For long-running tool calls, Streamable
  HTTP + periodic progress notifications + (where available)
  SEP-1686 Tasks is the structurally robust deployment.

  **The upstream gap is the primary motivator for v2's
  deferred-handle / parked-status return** — not just a
  nice-to-have. v1 ships with: (a) per-step timeouts mandatory for
  downstream, (b) `notifications/progress` emitted periodically for
  any step expected to exceed a configurable threshold, (c)
  verified upstream timeout numbers documented per-host, (d) a
  clear error if a blueprint declares `await_user` and the host's
  known timeout is shorter than a configurable threshold. If the
  upstream timeout is finite and cannot be raised via progress
  notifications, the v1-honest answer is to recommend Streamable
  HTTP for await-enabled workflows, lifting that decision out of
  "Mode B only" into "Mode A with HTTP when await is in scope."
- **Process liveness.** Engine crashes mid-wait, workflow dies. Same
  reliability profile as any other step — `await_user` just widens the
  exposure window from seconds to minutes-or-hours.
- **Concurrency.** Many simultaneous human-waits hold open many MCP
  connections. For an autonomous loop kicking off many approval-gated
  workflows, this can exhaust pools. Cap, or fail gracefully.

### Where the blocking model breaks

One assumption breaks the whole picture: **cross-session survival.** User
starts a workflow, closes the chat, expects to come back tomorrow and find a
pending approval. That cannot be a blocked MCP call — the agent host is gone.

That is the v2 feature.

## Cross-session survival, incrementally

Goal: ship v1 with no durable state, then add cross-session survival as a
purely additive feature in v2 — without rewriting the engine, the DSL, the
agent contract, or existing UI-MCPs.

The trick: v1 doesn't do anything durable, but it makes a handful of shape
decisions that keep v2 additive instead of a rewrite. None of these add
meaningful complexity at v1; they are discipline, not infrastructure.

### v1 shape constraints

1. **State is data, not code.** The engine's "current state" at any moment
   must be expressible as a JSON document: `{step_index, variable_bindings,
   results, frame_stack}`. No closures, no generator state, no Promise refs
   embedded in the state representation. The interpreter reads state →
   decides next step → calls tool → writes result back. v1 never serializes
   this; v2 just adds a writer. If v1 lets state be opaque, v2 cannot lift
   it out.

   **Mechanical grounding from the paper.** Parmar 2026 Algorithm 1 is a
   flat iterating loop (`foreach step s in B.steps do; switch s.type`), not
   a recursive descent over the JSON tree. The top-level step list has a
   serializable index. Nested structures (`loop`, `parallel`, `pipe`)
   require a small typed frame for the *currently-executing* nest:
   `{type: "loop", iteration: N, items_remaining: [...]} | {type: "parallel",
   branch_results: {...}} | {type: "pipe", index: N, accumulator: ...}`.
   Frames push/pop on a heap-allocated stack as nests enter and exit. This
   is ~50-100 LoC of frame types, not a wholesale interpreter rewrite —
   but it must be present from v1, because converting an existing
   recursive-descent interpreter that uses the JS call stack for execution
   position is a real refactor, not "add a writer."

2. **Workflow IDs exist from day one.** Every execution has a stable
   `workflow_id`. v1 uses it for logging only. v2 uses it as the durable-store
   key, threads it into the resume tool, surfaces it in "list pending
   workflows" UIs. Mint at v1 or patch into iframes, tool inputs, and audit
   logs forever.

3. **The execution log has the shape v2 will persist — but the snapshot
   is the source of truth for recovery.** Per-step records:
   `{step_id, parent_step_id, branch_id, loop_iteration, input, output,
   status, ts}`. The richer schema (parent / branch / iteration) is needed
   for observability and debugging across nested primitives, even though
   recovery itself reads the snapshot, not the log. **Recovery model,
   stated explicitly:** v2 reconstructs from the `WorkflowState` snapshot
   (`{step_index, frame_stack, bindings, results}`) written via
   `ExecutionStore.put`. The audit log is append-only history, not the
   recovery oracle. If v1's log is ad-hoc, v2 has to invent the shape and
   migrate; if v1 conflates "log is the source of truth" with "snapshot is
   the source of truth," v2 has to pick anyway and migrate the loser.

4. **Engine code path branches on the await annotation.** Even though
   `await_user` is mechanically a slow `call` in v1, the engine should
   branch:
   ```
   if step.await { handleAwait(...) } else { handleCall(...) }
   ```
   v1's `handleAwait` is a one-line pass-through to `handleCall`. v2's
   swaps in disconnect-detection-and-park. One function changes; the rest
   of the engine doesn't know.

5. **UI-MCPs are stateless re-renderers from day one.** The easiest
   constraint to violate accidentally. In v1, the iframe never disappears,
   so it's tempting to let it accumulate state. In v2, the host can
   disconnect and a *new* iframe materializes when the user resumes —
   possibly in a different session, possibly hours later. The UI-MCP must
   take everything it needs to render as input on each call, never store
   workflow state in iframe memory. Enforce this discipline at v1 even
   though it is strictly unnecessary, or v2 breaks every UI-MCP you wrote.

6. **The workflow tool's return schema admits parked outcomes from day one.**
   v1 always returns `{status: "completed", result: ...}`. v2 may also
   return `{status: "parked", workflow_id, ...}` (blocking-with-park) or
   `{status: "pending_user_input", workflow_id, checkpoint_id, ...}`
   (yield path). The schema is a discriminated union over all three from
   v1, even though v1 only emits `completed`. Agents handle the union from
   the start; either v2 path is then additive.

7. **All store calls and tool handlers thread an optional identity
   context.** Every method on `ExecutionStore` / `AuditLog` /
   `BlueprintStore`, and every tool handler, accepts
   `identity: UserContext | null`. v1 always passes `null` (single-user,
   single-machine assumed); the in-memory stores ignore it. Mode B's auth
   middleware injects an identity from the request context before
   dispatching to handlers; the SQLite stores use it for row-level
   scoping. Without this thread, Mode B is a pervasive refactor of every
   call site, not "same engine, different transport." Five-to-ten
   signatures get an extra optional parameter; cost at v1 is essentially
   zero, cost of *not* doing it at v1 is rewriting Mode B's promise.

### The single architectural commitment v1 must make

Two interfaces, deliberately split. `ExecutionStore` for mutable workflow
state (snapshot, with CAS semantics for the concurrent-engines case);
`AuditLog` for append-only history. v1 ships in-memory implementations of
both; v2 swaps in SQLite-backed impls. No call sites change.

```
// Mutable state snapshot — needs CAS for concurrent engines.
interface ExecutionStore {
  putIfVersion(workflow_id, expected_version, state, identity?): Promise<'ok' | 'conflict'>
  get(workflow_id, identity?): Promise<WorkflowState | null>
  list(filter, identity?): AsyncIterable<WorkflowState>

  // Atomic lifecycle transitions — not raw CRUD.
  park(workflow_id, state, checkpoint, identity?): Promise<void>
  resume(workflow_id, checkpoint_id, payload, identity?): Promise<WorkflowState | 'conflict'>
  cancel(workflow_id, reason, identity?): Promise<void>
}

// Append-only run history — observability, not recovery.
interface AuditLog {
  append(workflow_id, entry, identity?): Promise<void>
  tail(workflow_id, after_seq?, identity?): AsyncIterable<LogEntry>
}
```

The split is deliberate: mutable snapshot and append-only history have
different consistency requirements (CAS vs eventual), different storage
strategies (row-locked vs append-only WAL), and different lifecycles
(snapshot is GC-able once a workflow completes, log may be retained for
audit). Conflating them in one interface invites callers to do `get →
mutate → put` sequences that are racy by construction. Atomic `park` /
`resume` / `cancel` operations encode the transitions v2 actually needs;
exposing them at the interface prevents callers from rolling racy
sequences themselves.

That is the only architectural commitment. Everything else on the v1 list
is local discipline — naming, branching, return shapes — and costs nothing
to enforce.

### What v2 adds (purely additive)

- A non-in-memory `WorkflowStore` impl.
- `engine.resume(workflow_id, payload)` MCP tool.
- Disconnect detection in `handleAwait` → call `WorkflowStore.park` instead
  of returning an error to the caller.
- A "list pending workflows" tool, and optionally an ext-apps UI for it
  (natural fit — pattern (3) on a smaller surface).
- TTL / expiry policy on parked workflows.
- Conflict handling when multiple sessions try to resume the same workflow.

None of these touch the DSL, the blueprint format, the agent contract, or
existing UI-MCPs.

## Why isn't this just LangGraph?

The resemblance is real and worth naming directly. LangGraph solves a closely
related problem — stateful multi-step LLM workflows with durable state and
human-in-the-loop interrupts — and the vocabulary lines up almost exactly:

| LangGraph | Here |
| --- | --- |
| `thread_id` | `workflow_id` |
| Checkpointer (`MemorySaver`, `SqliteSaver`, ...) | `WorkflowStore` |
| `Interrupt` + `Command(resume=...)` | `await_user` annotation + blocking RPC |
| State dict flowing through nodes | Variable bindings flowing through steps |
| Streaming node outputs | PostMessage notifications to ext-apps iframe |

The interrupt-and-resume semantics, the checkpointer interface shape, the
thread-id model — all well-trodden, all worth borrowing. This document is
not claiming to invent any of those.

What is genuinely different is the **substrate** (where the engine sits in
the larger system) and the **artifact** (what a workflow IS, as a stored
thing). The runtime question is a wash — both designs need to execute
somewhere, and "TypeScript not Python" is a language preference, not an
architectural argument. Drop that.

### Side-by-side gap comparison

| Dimension | LangGraph | Blueprint engine |
| --- | --- | --- |
| Workflow as artifact | Python source — graph topology implicit in graph-builder code | JSON document over fixed DSL |
| Author needs language toolchain | Yes (Python env + LangGraph deps) | No — writes JSON; only the engine needs a runtime |
| LLM-authorable | Difficult — must emit valid Python + correct SDK usage + topology | Tractable — emit a JSON tree over 5 primitives |
| Static analysis on the artifact | AST inspection or runtime introspection | Direct JSON traversal |
| Diff / version control on the artifact | Source diffs; topology hides in lambda bodies and conditional edges | Structural diff over the graph itself |
| Indexable for retrieval (`plan_search`, `plan_match`) | No — code, not data | Yes — JSON, embeds and tags cleanly |
| Cross-language execution | No (Python only) | Yes — any engine implementing the DSL |
| Engine and workflow logic share a process | Yes — node functions run in-process | No — every step is an out-of-process MCP call |
| Failure isolation between engine and step | Limited — exceptions, imports, globals share fate | Strong — step failure is a remote-call failure |
| Tool calls to MCP | Custom node code instantiates an MCP client per node | Native — every step IS an MCP call |
| Engine exposed as MCP tool | Requires a custom MCP server wrapper | Engine IS an MCP server natively |
| Control inversion | LangGraph owns the orchestration loop; app is built around it | Agent owns the loop; workflow is one tool among many |
| Mid-execution branching on LLM judgement | First-class via LLM nodes and conditional edges | Out of scope by design — re-engages design phase |
| Conditional logic | Arbitrary Python on edges | Limited to DSL primitives |
| LLM-aware nodes | First-class | Not supported (deliberate — execution is deterministic) |
| Built-in checkpointer backends | Multiple (memory, SQLite, Postgres, async variants) | Just an interface — implementations to write |
| Built-in streaming | Yes (`stream`, `astream`) | Via PostMessage notifications |
| UI surface | Build your own frontend | Free via ext-apps in any MCP-compliant host |
| Maturity | Production, ecosystem, docs, debugging tools | Reference impl (~1,250 LoC TS in the paper) |

### Honest gaps — where LangGraph wins outright

- **Maturity.** Production deployments, multi-backend checkpointers, docs,
  community, debugging tools (LangSmith, time travel, thread inspection).
  The blueprint engine is a reference impl.
- **Mid-execution flexibility.** Conditional edges driven by Python lambdas
  or LLM judgement let a graph make runtime decisions the original author
  did not enumerate. A blueprint engine forced into the same situation has
  to either re-engage the design phase or fail.
- **LLM-aware nodes.** If your workflow needs LLM judgement *inside*
  execution (not just at the boundary), LangGraph supports that directly.
  The blueprint engine's whole premise is that you have already crystallised
  the reasoning — workflows that genuinely cannot be crystallised are
  outside its scope.
- **Built-in primitives.** Checkpointers, retry policies, streaming,
  thread inspection — all there, all working, none yet in the blueprint
  engine.

### Where the blueprint approach genuinely differs

- **The artifact is data, not code.** This is the load-bearing
  difference. A workflow IS a JSON document. It can be searched, ranked,
  diffed structurally, embedded into retrieval, version-controlled at the
  topology level, and emitted by an LLM as the output of a design phase.
  None of that is true of a Python graph.
- **The engine is natively MCP.** Workflows are tools to any agent in the
  ecosystem without wrapping. The Mediator pattern (server upward, client
  downward) is the architecture, not a layer on top.
- **Control inversion.** The agent orchestrates and calls workflows; the
  workflow does not orchestrate the agent. This is what makes "compile a
  loop's stable reasoning into a workflow tool" possible.
- **Process isolation between engine and step.** Step failures are remote
  call failures, not in-process exceptions. Steps can be implemented in any
  language, by any team, deployed independently.

### The asymmetry that justifies the approach

Most of the LangGraph wins are *features*. Most of the blueprint wins are
*consequences of one constraint*: that a workflow is a JSON document.

That constraint is what makes:

- The design-once / execute-many split work, because an LLM can reliably
  emit a JSON tree over 5 primitives but cannot reliably emit correct
  Python over a flexible API.
- The retrieval lifecycle (`plan_search`, `plan_match`, `plan_promote`)
  work, because plans are stored data, not code.
- The MCP-native framing work, because exposing a JSON document as a tool
  is straightforward, while exposing a Python graph requires wrapping.
- Cross-language and cross-host portability work.

If you do not need any of those properties — if your workflows are not
LLM-authored, not retrieved by similarity, not exposed to external agents,
not portable across runtimes — then LangGraph is the better choice and
this design has no advantage.

### "Couldn't the engine use LangGraph internally?"

Yes, and that's a defensible implementation choice. The blueprint layer and
the MCP-native server framing are the load-bearing pieces; what executes the
graph underneath is replaceable, and LangGraph's checkpointer + interrupt
implementation is good. Practical reasons not to:

- Most of LangGraph's value — LLM-aware nodes, conditional edges driven by
  LLM judgement, mid-execution branching — is unused in a deterministic
  blueprint executor. You'd be paying for abstractions you don't exercise.
- Mapping `await_user` semantics onto `Interrupt` + `Command(resume=...)`
  is workable but adds a translation layer for no functional gain.
- Two state machines in one process — MCP session state plus LangGraph's
  checkpointer — invites edge cases at the boundary.
- A five-primitive interpreter over a JSON tree is small enough (~1,250
  LoC in the reference) that the dependency cost rarely pays back.

The takeaway: borrow the abstraction *shapes* (checkpointer interface,
thread ids, interrupt-and-resume semantics). The substrate and the
artifact are where the design earns its keep.

## Recommended architecture (TS-engine starting point)

Given a working TypeScript first-cut of the interpreter, and assuming
**no cross-machine resume requirement**, the architecture simplifies to
the standard MCP server shape: the host (Claude Code, Cowork, ...)
spawns the engine as a subprocess, and everything — frontend, engine,
storage — lives in that one process. No daemon. No network. No REST
gateway in the default.

The LangGraph hybrid drops out for the same reason it dropped out
earlier: adopting it would force a Python detour (LangGraph.js trails
Python meaningfully) for a runtime that's already lean and TS-native.

### Default deployment: client-hosted, in-process

```
┌────────────────────────────────────────────────────────┐
│ MCP host (Claude Code, Cowork, ...)                    │
│   spawns engine via standard MCP server config         │
└────────────────────────────────────────────────────────┘
              ↓ stdio (MCP)
┌────────────────────────────────────────────────────────┐
│ Engine subprocess — single binary                      │
│   • MCP server (upward, to host)                       │
│   • Blueprint interpreter (the working TS code)        │
│   • JMESPath template resolution                       │
│   • Execution state (in-memory + SQLite for v2)        │
│   • BlueprintStore  (~/.local/share/<engine>/plans.db) │
│   • WorkflowStore   (~/.local/share/<engine>/runs.db)  │
│   • MCP client (downward, to tool servers)             │
└────────────────────────────────────────────────────────┘
              ↓ stdio (MCP)
┌────────────────────────────────────────────────────────┐
│ Downstream tool MCPs (k8s, db, fs, ...)                │
│   spawned by the engine, lifetime tied to it           │
└────────────────────────────────────────────────────────┘
```

The engine is just another MCP server. The host launches it via the
same configuration mechanism it uses for any other server. The engine
itself acts as an MCP client downward to the tool servers it
orchestrates — that's the Mediator pattern, contained in one process.

### Where each concern lives

| Concern | Lives in |
| --- | --- |
| Transport to host | stdio, via the MCP SDK |
| Blueprint registry | SQLite file in user data dir (or nexus T2/T3 if integrated) |
| Workflow execution state | Same process; persisted to SQLite for v2 |
| Audit log | Append-only, same SQLite database |
| ext-apps `ui://` resources | Emitted by the engine on host request |
| Identity | Inherited from the host's user context — same user, same machine |

No tier separation in deployment. The "frontend / engine / storage"
split is internal organization, not a process boundary.

### Cross-session survival without a daemon

The engine subprocess lifetime matches the host session. Workflow
durability comes from the SQLite file, not from a long-lived process:

1. User starts a workflow in host session A. It runs, hits
   `await_user`, and the engine writes parked state to SQLite.
2. Engine returns `{status: "parked", workflow_id, ...}` to the host.
   Host can close. Engine can exit cleanly — its job is done.
3. Later, user opens host session B (same machine, possibly different
   host — Claude Code one day, Cowork the next).
4. New engine subprocess starts, reads SQLite, lists pending workflows
   on demand via `engine.list_pending`.
5. User selects one; host calls `engine.resume(workflow_id, payload)`;
   engine loads state, continues execution.

The "engine subprocess dies" event is recoverable in the same way
"daemon disconnects" was recoverable — durable state in SQLite.

### Concurrency between simultaneous host clients

If the user has Claude Code and Cowork open at the same time, each
host spawns its own engine subprocess. Both engines read and write the
same SQLite files. SQLite handles this with WAL mode + standard file
locking; concurrent reads are free, concurrent writes serialize at the
DB level.

What this means in practice:

- Listing pending workflows is consistent across hosts.
- Resuming a workflow from one host while another host has it
  displayed needs a soft check (the engine reads current status before
  applying the resume) but no distributed coordination.
- Each engine process owns its own running workflows; there's no
  "steal" semantics. A workflow started in Claude Code cannot be
  *actively executing* in Cowork's engine. Parked workflows are
  freely picked up by whichever engine resumes them.

### ext-apps integration

Native, since the engine is itself an MCP server. Brief recap; full
detail in [ext-apps integration in detail](#ext-apps-integration-in-detail)
below.

- Blueprints can declare a `ui://` resource alongside the tool
  definition. The engine emits it on host request.
- `await_user` steps push current state to the iframe via PostMessage
  notifications. Iframe calls back through the host into the engine's
  `engine.resume` tool with `{workflow_id, payload}`.
- All of this is in-session and in-process; no daemon needed.

### v1 → v2 progression (client-hosted)

v1: the existing TS engine, exposed as an MCP server, with in-memory
state and ephemeral execution. Spawned per host session, exits when
done.

v2 adds, additively:

- Persistent SQLite-backed `BlueprintStore` and `WorkflowStore` in the
  user data dir. ~300 LoC of SQL + a small migration shim.
- `engine.list_pending`, `engine.resume`, `engine.cancel` MCP tools.
- Disconnect detection on `await_user` → write parked state and
  return a deferred handle, instead of holding the call open
  indefinitely.
- Optionally: an ext-apps UI for browsing pending workflows.
- TTL / expiry policy on parked workflows (a nightly cleanup the next
  spawned engine performs on startup is sufficient — no daemon).

None of this changes the DSL, the blueprint format, the agent
contract, or existing UI-MCPs.

### Variant: hosted service (only when remote access is required)

The client-hosted model covers everything that runs locally —
Claude Code, Cowork, locally-hosted browser apps that the user
explicitly launches alongside the engine, anything where "the engine
and the user are on the same machine."

It does *not* cover:

- **Remote agent hosts** (OpenAI Custom GPT Actions, Gemini hosted
  agents, anything that lives on someone else's infrastructure and
  needs to call into your workflows over the network).
- **Browsers that haven't been told to launch a local helper.**
- **Distributed teams** sharing a workflow registry.
- **Scheduled / autonomous execution** without a host session
  (e.g., cron triggering a workflow with no user present).

For those, the same engine binary runs in a different deployment shape:

```
[Remote host]                  [Browser app]
      ↓ Streamable HTTP (MCP)               ↓ HTTPS (REST)
┌───────────────────────────────────────────────┐
│ Engine daemon                                 │
│   • Same TS engine, same code                 │
│   • Streamable HTTP MCP transport binding (with resumable SSE via Last-Event-ID)            │
│   • REST gateway shim (~200-300 LoC)          │
│   • Auth: API key / OAuth                     │
│   • Storage: SQLite (single tenant) or RDBMS  │
└───────────────────────────────────────────────┘
              ↓ stdio (or HTTP) MCP
[Downstream tool MCPs]
```

This is the Mode B variant. It exists when needed; it is not the
default. Same engine, same DSL, same blueprints — different
transport binding, different process model, different auth surface.

The MCP TS SDK supports both stdio and Streamable HTTP against the same
server implementation, so the engine code is unchanged between the
two modes. The variant adds:

- A daemon process model (system service or container).
- Auth middleware on the Streamable HTTP and REST surfaces.
- The REST gateway shim, with OpenAPI auto-generated from blueprint
  input schemas.
- Multi-user identity scoping in `WorkflowStore` rows.

If the project never grows into the remote scenario, this variant is
never built. The architecture does not pay for it preemptively.

### What you don't build

Stated explicitly to keep the surface honest:

- **No daemon, no network listener, no REST gateway in the default.**
  Those exist only in the hosted-service variant, only when remote
  access is a requirement.
- **No LangGraph dependency.** The TS engine is the runtime; durable
  state and retry semantics are small enough to build in-tree.
- **No per-host protocol adapter.** Local hosts speak MCP over stdio;
  remote hosts (in the variant) speak MCP over Streamable HTTP or use the
  REST gateway. No host-specific code in the engine.
- **No Python in the critical path.** Everything is TS.
- **No separate frontend codebase per host.** ext-apps gives you UI
  in any MCP-compliant host for free.

### What you might add later, but not now

- **Multi-tenancy** in the hosted variant. Defer until you have
  tenants.
- **Distributed execution** (multiple engine workers behind a queue).
  Defer until single-engine throughput becomes a measured limit.
- **Time-travel debugging.** Possible against the audit log; build
  when the operational pain justifies it.
- **A TS-native workflow runtime backend** (Temporal, Inngest) under
  the engine if you outgrow SQLite. The `WorkflowStore` interface
  from v1 makes this a swap, not a rewrite.

## ext-apps integration in detail

The earlier mentions of ext-apps in the architecture section are
deliberately brief. This section unpacks what the integration actually
involves — protocol shape, lifecycle, blueprint declarations, host
variation.

### Where the iframe sits

```
[Host: Claude Code / Cowork / ChatGPT / VS Code / Goose ...]
   renders iframe inline in the conversation
   web hosts: WRAPS the View in a Sandbox proxy iframe at a
   different origin (REQUIRED by SEP-1865); desktop/native hosts MAY omit
        ↑                                 ↓
   postMessage                       postMessage
   (JSON-RPC 2.0,                    (JSON-RPC 2.0,
    ui/notifications/* etc.)          tools/call etc.)
        ↑                                 ↓
   ─────────────── HOST'S MCP CLIENT ───────────────
        ↑                                 ↓
   server-side                      tools/call from View,
   tool execution                   routed through host
   + ui/ notifications              (engine.resume etc.)
        ↑                                 ↓
[Engine: MCP server emitting ui:// resources]
```

The critical property: **the engine never talks directly to the
iframe.** Every iframe-side interaction routes through the host's MCP
client. This is the Mediator pattern preserved at the UI boundary.
The host's auth and policy layer applies to iframe-originated actions
just like any other tool call.

**Sandbox-proxy hop (web hosts).** SEP-1865 § Security mandates that
"If the Host is a web page, it MUST wrap the View and communicate
with it through an intermediate Sandbox proxy. The Host and the
Sandbox MUST have different origins." The View talks to the Sandbox
proxy via postMessage; the Sandbox proxy forwards to the host. The
actual hop count for web hosts is **four** (View → Sandbox → Host →
MCP server), not three. Desktop hosts (Claude Code, VS Code) may
omit the proxy and present a three-hop path. Latency math should
assume four.

### Lifecycle of a UI-attached workflow invocation

The spec-compliant lifecycle, per SEP-1865:

1. **Connection setup**: host fetches `ui://` resources at MCP
   session establishment via `resources/list` + `resources/read`.
   Resources MUST be served with `mimeType: text/html;profile=mcp-app`.
   Hosts prefetch templates before any tool execution.
2. Agent calls `tools/call` for a workflow whose tool definition
   includes `_meta.ui.resourceUri` pointing at a prefetched `ui://`
   resource.
3. Host renders the View iframe (web hosts: inside a Sandbox proxy).
4. View → Host handshake:
   - View sends `ui/initialize` request with capabilities, clientInfo,
     protocolVersion, appCapabilities.
   - Host returns `McpUiInitializeResult { hostContext, capabilities }`.
   - View sends `ui/notifications/initialized` to complete the
     handshake.
5. Host MUST send `ui/notifications/tool-input` to the View after
   `initialized`. MAY send `ui/notifications/tool-input-partial` while
   arguments stream.
6. Engine executes. **There is no spec-defined "mid-execution state
   push" channel** — `ui/notifications/tool-result` fires once per
   tool execution. For step-by-step progress the engine has three
   options: (a) emit `notifications/resources/updated` so the host
   re-`resources/read`s; (b) the View polls via `tools/call
   engine.get_state`; (c) propose a new SEP for a
   `ui/notifications/server-update` notification. **v1 uses (b)**:
   View polls; a small `pollInterval` is part of `hostContext`.
7. On `await_user`, engine returns control (yield model) or holds
   the call (blocking model). View renders the prompt. View
   `tools/call engine.resume(workflow_id, payload)`.
8. Engine continues. On completion, host MUST send
   `ui/notifications/tool-result` to the View. On cancellation, host
   MUST send `ui/notifications/tool-cancelled`.
9. On teardown, host sends `ui/resource-teardown` request to the
   View; View SHOULD respond before the iframe is destroyed.

### PostMessage protocol shape (per SEP-1865)

SEP-1865 standardises a JSON-RPC 2.0 protocol over postMessage between
View iframe and host. The previous draft of this document invented an
engine-defined notification taxonomy; that was incorrect. The View is
an MCP client; the host is the MCP server-adjacent endpoint. The
engine does not invent its own notification taxonomy.

**Lifecycle handshake** (View → Host):
- `ui/initialize` request — capabilities + clientInfo +
  protocolVersion + appCapabilities → returns
  `McpUiInitializeResult { hostContext, capabilities }`.
- `ui/notifications/initialized` — completes handshake.

**Tool-execution notifications** (Host → View):
- `ui/notifications/tool-input` — MUST send after initialized
  completes; carries the tool-call arguments the agent invoked with.
- `ui/notifications/tool-input-partial` — MAY send while arguments
  stream from the agent.
- `ui/notifications/tool-result` — MUST send on completion if the
  View is displayed.
- `ui/notifications/tool-cancelled` — MUST send on cancellation.
- `ui/notifications/host-context-changed` — partial HostContext
  updates (theme, displayMode, orientation, locale).

**Teardown** (Host → View request):
- `ui/resource-teardown` — host SHOULD wait for response before
  destroying the iframe.

**View-initiated actions, standard MCP** (View → Host, forwarded to
the MCP server):
- `tools/call` — the channel for `engine.resume`, `engine.cancel`,
  `engine.get_state`.
- `resources/read`, `prompts/get`, `ping`, `notifications/message`.

**View-initiated actions, MCP Apps extensions** (View → Host, do not
reach the MCP server):
- `ui/open-link` — host opens an external URL.
- `ui/message` — send text content to the host's chat surface,
  preserving `role`. Spec-blessed fallback channel when the View
  wants to escalate to the agent.
- `ui/request-display-mode` — request `inline | fullscreen | pip`.
- `ui/update-model-context` — inject content / structuredContent
  into the next agent turn without a tool round-trip.

**The "mid-workflow state push" gap.** The notifications the
previous draft invented (`step_completed`, `checkpoint`, etc.) have
no spec analog. v1 falls back to polling: the View calls `tools/call
engine.get_state` on a host-recommended interval. Best-effort
delivery semantics still apply — JSON-RPC 2.0 notifications are
fire-and-forget — so the View's drift-detection logic remains
relevant.

### What blueprints declare

```json
{
  "name": "approve_deploy",
  "_meta": {
    "ui": {
      "resourceUri": "ui://approve_deploy/main.html",
      "csp": {
        "connectDomains": ["https://api.k8s.example.com"],
        "resourceDomains": ["https://cdn.example.com"]
      }
    }
  },
  "ui_checkpoints": {
    "review_changes": "ui://approve_deploy/review.html"
  },
  "steps": [
    {"call": "k8s.diff", "args": {...}},
    {"await_user": "review_changes", "expose_state": ["diff", "blast_radius"]},
    {"call": "k8s.apply", "args": {...}}
  ]
}
```

- `_meta.ui.resourceUri`: the per-tool UI binding per SEP-1865. The
  host prefetches and renders this iframe.
- `_meta.ui.csp`: per SEP-1865, only four fields are accepted:
  `connectDomains`, `resourceDomains`, `frameDomains`,
  `baseUriDomains`. Anything else the blueprint declares here is
  ignored by the host.
- `ui_checkpoints`: engine-level extension mapping `await_user` step
  names to per-checkpoint `ui://` overrides. The engine surfaces the
  appropriate resource via `_meta.ui.resourceUri` on the
  checkpoint's response (or via a separate tool the View calls to
  resolve the URI). Not part of SEP-1865; engine-internal.
- `expose_state` on a step: an allowlist of variables the iframe sees.
  Prevents accidental leakage of intermediate secrets to the View.

A blueprint with no `_meta.ui` section runs headless. Same engine,
same execution path; result is plain JSON.

Resource content **MUST** be served with
`mimeType: text/html;profile=mcp-app`. Other types are reserved for
future extensions.

### What the engine ships

Two pieces:

1. **A small library of generic widgets** under `ui://_builtin/...`:
   - `progress.html` — DAG view, step list, live status.
   - `form.html` — generated from JSON Schema, default `await_user` UI
     when no custom UI is declared.
   - `pending.html` — list of parked workflows with resume/cancel
     controls (v2).
   - These cover most routine flows. Blueprint authors don't write
     HTML for them.

2. **A way to bundle custom UIs** for blueprints that need them:
   - HTML+JS files alongside the blueprint definition, served from
     disk.
   - Inline strings in the blueprint JSON (small UIs).
   - A `ui-asset://` blob store for larger React apps. Engine serves
     blobs via `resources/read`.

### What the engine has to implement

- `resources/list` and `resources/read` for `ui://` URLs; serve
  with `mimeType: text/html;profile=mcp-app`.
- Tool definitions carrying `_meta.ui.resourceUri` and optional
  `_meta.ui.csp` per SEP-1865.
- Server-side handling of `engine.resume`, `engine.cancel`,
  `engine.get_state` tools (invoked by the View via `tools/call`
  through the host).
- A **host-side** session mapping: `iframe-session-id → (workflow_id,
  branch_id)`. SEP-1865 does NOT carry workflow or branch
  discriminators in the wire protocol; routing is established at
  `ui/initialize` time and addressed via each iframe's
  `contentWindow.postMessage` Window. The engine must establish the
  binding (e.g., by encoding `workflow_id` / `branch_id` in the
  initial `tool-input` notification's arguments, then having the
  View carry them on every subsequent `engine.resume` /
  `engine.get_state` call).
- A View-driven polling loop for mid-execution state (since SEP-1865
  has no server-push channel for that). Engine sets a recommended
  `pollInterval` via the `_meta.ui` block or `HostContext`.
- Strict adherence to "iframe is a stateless re-renderer" — engine
  state is canonical; iframe pulls via `engine.get_state` on init
  and never assumes continuity across renders. `localStorage` (the
  spec-blessed recoverable-view-state channel) is acceptable for UI
  preferences (collapsed panels, sort order) but NOT for workflow
  state.

### Limits and pitfalls

- **Four-hop latency on web hosts.** View → Sandbox proxy → Host →
  MCP server → engine. Three on native hosts. Fine for discrete
  actions (button clicks, form submits). Bad for fine-grained
  interactions (mouse tracking, real-time collaborative editing).
  Don't design for those.
- **Sandbox attributes (web hosts only) per SEP-1865.** The
  Sandbox proxy iframe MUST be set with `allow-scripts
  allow-same-origin`. Hosts that deviate violate the spec.
- **CSP defaults per SEP-1865.** If `_meta.ui.csp` is omitted on a
  tool, the host MUST apply:
  ```
  default-src 'none';
  script-src 'self' 'unsafe-inline';
  style-src 'self' 'unsafe-inline';
  img-src 'self' data:;
  media-src 'self' data:;
  connect-src 'none';
  ```
  No `unsafe-eval` is permitted in any mode. To make outbound HTTP
  calls (analytics, REST APIs), declare
  `_meta.ui.csp.connectDomains`. To load fonts/scripts/images from
  a CDN, declare `resourceDomains`. To nest iframes, declare
  `frameDomains`. To use `<base href>`, declare `baseUriDomains`.
  Anything beyond these four fields is ignored. **Bundle assets**
  is still the right default; declare a CDN only when bundling is
  impractical.
- **Iframe size caps.** Hosts cap iframe dimensions for chat
  layout. Assume ~600px tall, fluid width, design progressive
  disclosure for richer content. SEP-1865 lets the View request
  `inline | fullscreen | pip` via `ui/request-display-mode`; hosts
  MAY honor or refuse.
- **postMessage size and ordering.** Spec is silent — semantics
  inherit from the browser's structured-clone algorithm (browsers
  typically cap at a few MB) and from JSON-RPC 2.0 (notifications
  are fire-and-forget). For large state, the spec's recommended
  pattern is to push the bulk into model context via
  `ui/update-model-context` first, then send a small follow-up
  message; the engine's equivalent is to keep large state
  server-side and have the View pull via `engine.get_state` as a
  `tools/call` response rather than push via postMessage payload.
- **No persistent iframe→engine connection.** View cannot open a
  WebSocket to the engine. All comms route through host postMessage
  + host's MCP client.
- **Multi-iframe coordination is a host-implementation gap, not a
  spec limitation.** SEP-1865 does NOT cap the number of active
  iframes per workflow; each iframe is an independent MCP UI client
  session bound to its own postMessage Window. Routing is by
  `MessageEvent.source` and `contentWindow.postMessage`. However,
  **current host implementations render one iframe per
  `tools/call` response.** So spawning N branch iframes for
  `parallel { await_user; await_user }` requires either (a) N
  independent `tools/call` invocations (one per branch), which
  costs N agent-visible tool calls and complicates the workflow
  contract, or (b) host extension work that does not exist today.
  The doc commits to the **parent-iframe strategy** as v1 default:
  one iframe per workflow, sub-views per `branch_id` rendered
  client-side. The engine maintains the `branch_id →
  iframe-session` mapping host-side, because the wire protocol
  carries no branch discriminator. The per-branch-iframe strategy
  is preserved as a future option pending host support; workflows
  that genuinely need cross-organisation independent approval
  surfaces should decompose into separate workflows joined by an
  outer orchestrator.

### Progressive enhancement — host capability varies

ext-apps is host-supported, not universal:

| Host | ext-apps (SEP-1865) support |
| --- | --- |
| Claude (Code, Cowork, web) | Full support |
| ChatGPT | Full support |
| VS Code | Joining (per ecosystem roadmap) |
| Goose | Joining (per ecosystem roadmap) |
| OpenAI Custom GPTs (Actions) | No — different widget system; falls back to text/JSON |
| Gemini | Not at present |
| Third-party MCP CLI clients | Typically ignore `ui://` resources |
| Browser apps via REST gateway | N/A — they own their own UI |
| Java MCP SDK | No ext-apps support at the time of writing |

The engine emits `ui://` resources unconditionally; the host decides
what to do. **Workflows that have UIs work with rich UIs in
supporting hosts, and fall back to plain text in non-supporting
hosts.** No engine-side branching needed.

**Spec-blessed fallback channel: `ui/message`.** For hosts that DO
support ext-apps but where the View wants to escalate text to the
agent (rather than handle interaction in-iframe), the View can call
`ui/message` to post text content into the chat surface, preserving
`role`. This is the natural fallback for "we want to surface a
prompt to the user via the agent rather than via the iframe form"
without requiring the engine to emit a structured tool result for
the agent to render.

For hosts that DON'T support ext-apps at all, workflows that
*require* user input (a hard `await_user` with no fallback) need
either a text-only fallback path (engine emits a structured prompt
the agent can render and forward) or an explicit declaration that
the workflow refuses to run in non-UI-capable hosts. Per-blueprint:

```json
"checkpoints": {
  "review_changes": {
    "ui": "ui://approve_deploy/review.html",
    "fallback": "agent_prompt"   // or "require_ui"
  }
}
```

## Async surface and where it bites

The architecture and ext-apps sections describe what the system does;
this section names the async issues we keep tripping on across both,
sorts them by when they bite, and is honest about where we've been
hand-wavy.

### Parallel branches are v1

`parallel` is one of the original five DSL primitives, and it is
structurally just `Promise.all` (or `Promise.allSettled`) over MCP
tool calls. The interpreter spawns N concurrent calls, awaits
collectively, merges results into the parent state keyed by step_id.
~50 LoC of interpreter logic. **Nothing about parallel branches
requires durable state, a daemon, or v2 machinery.**

What v1 must ship alongside `parallel`:

- **Error policy declaration:** `parallel: {branches: [...], on_error:
  "fail_fast" | "collect"}`. Default fail-fast (cancel siblings on
  first error); `collect` for "run everything, report all errors at
  join."
- **Per-step timeout** so a hung downstream tool doesn't stall the
  join indefinitely. Blueprint declares; engine wraps each call in a
  timeout race.
- **Cancellation propagation.** Engine sends MCP `CancelledNotification`
  to in-flight branch calls on fail-fast / user cancel / timeout.
  Some downstream tools won't honor it; engine discards the result.
- **Optional concurrency cap** (`max_concurrent: 4`) per parallel
  block. Don't enforce by default.
- **Distinct step_ids per branch.** Compiler/validator catches
  duplicates at blueprint load.
- **`branch_id` is a first-class schema field.** Each direct child of a
  `parallel` block carries a `branch_id` assigned by the
  compiler/validator at blueprint load (or declared explicitly by the
  author). Notifications, audit-log entries, and iframe-routing all
  key off `(workflow_id, branch_id)`. Without this, multi-iframe
  routing for `parallel { await_user; await_user }` is conceptually
  fine but specification-incomplete. Example:
  ```json
  {"parallel": {
    "on_error": "fail_fast",
    "branches": [
      {"branch_id": "infra_review",
       "steps": [{"await_user": "review_infra", "expose_state": [...]}]},
      {"branch_id": "security_review",
       "steps": [{"await_user": "review_security", "expose_state": [...]}]}
    ]
  }}
  ```

### v1 issues — real and unavoidable

These are not deferrable. They show up the first time a workflow
runs.

1. **Three-hop iframe latency** (iframe → host → MCP call → engine).
   Fine for button clicks; unsuitable for anything pretending to be
   real-time. Design rule: PostMessage carries discrete events, not
   continuous streams. UIs needing real-time interactivity should be
   REST-gateway-backed in Mode B, not ext-apps-backed.

2. **PostMessage delivery is best-effort, not queued.** Notifications
   can be lost on host reconnect, focus loss, or dropped frames.
   Mitigation is in v1: monotonic version numbers + iframe-side drift
   detection + `engine.get_state` snapshot fetch on skip. An iframe
   missing a checkpoint notification is silent breakage otherwise.

3. **Per-step timeouts.** A hung downstream MCP tool stalls the
   workflow forever in v1 (no daemon to kill anything). Blueprint-
   declared timeouts with a hard ceiling are required at v1, not
   optional.

4. **Cancellation propagation.** When the engine wants to abort
   (fail-fast, user cancel, timeout), it sends `CancelledNotification`.
   Most downstream tools ignore it; engine discards results. The
   engine itself must handle the cancel cleanly — release the slot,
   mark the step errored, propagate up. ~30 LoC, easy to forget.

5. **Promise leaks.** A branch whose MCP call never resolves AND whose
   timeout doesn't fire holds the engine open forever. Belt-and-
   suspenders: timeouts + a process-level inactivity watchdog with an
   absolute upper bound.

### v1 issues — manageable but worth flagging

6. **Multi-iframe coordination from `parallel` branches — v1
   spec item, not a runtime concern.** The ext-apps spec doesn't
   define multi-iframe routing for one tool call. v1 picks the
   parent-iframe strategy (single host-level iframe, internal
   per-branch sub-views keyed by `branch_id`). The runtime
   concern that *remains* under this strategy is the resource cap
   on concurrent `await_user` branches inside one `parallel`
   block: ship a soft cap (`max_concurrent_checkpoints: 3`) with
   a clear error if exceeded. The deeper "two host-level iframes
   for one workflow" pattern is deferred to a future ext-apps
   spec extension; do not rely on it.

7. **Backpressure on notifications.** Engine emits faster than host
   delivers to iframe. Mitigation: coalesce — replace pending
   notifications of the same type with the latest, send periodic
   snapshots rather than per-step deltas for high-frequency steps.
   Most workflows won't hit this; design it in from the start.

### v2 issues — arrive with durability

8. **Disconnect detection vs slow-host.** When the engine emits a
   notification and the host doesn't ack, is the host gone or slow?
   v1 doesn't decide — it just blocks. v2 must, because parking is
   now an option. Detection comes from the transport (stdio: pipe
   broken; Streamable HTTP: connection closed); policy is a tunable: "wait
   5s before parking on `await_user`, never park during regular
   execution."

9. **Resume race conditions.** Two clients try to
   `engine.resume(workflow_id, ...)` simultaneously. SQLite serializes;
   one wins. Loser sees `409 Conflict` or equivalent. Policy:
   first-wins; the loser's UI shows "this workflow already moved
   past this checkpoint, reload."

10. **Engine restart while a workflow is running.** v1: workflow
    dies, agent gets an error. v2: engine traps SIGTERM, runs a 1-2s
    graceful drain (write parked state for in-progress workflows,
    abort in-flight calls, close DB), exits. If killed harder
    (SIGKILL), workflows in progress are recovered on next engine
    start by reading `WorkflowStore` and either resuming from last
    checkpoint or marking errored with a recoverable flag. **This is
    the v2 work item nobody wants to do, but it determines whether v2
    is actually durable or just durable-on-the-happy-path.**

11. **Cross-session iframe re-rendering.** v1: iframe lives as long
    as the host session. v2: iframe disappears when host closes,
    comes back fresh later. The "stateless re-renderer" v1 shape
    constraint pays off here — iframe pulls full state via
    `engine.get_state` on init, no continuity assumptions.

12. **Concurrent engines hitting the same SQLite.** Two host clients
    spawn two engines on the same machine; both write to the workflow
    store. SQLite + WAL handles atomic writes, but the application
    must reason about: who owns the actively-executing workflow lock,
    what does "list pending" return when another engine is mid-resume,
    can two engines try to advance the same workflow simultaneously.
    Lock at the `workflow_id` level with a heartbeat (engine writes
    "I'm processing W123, last-tick T") and other engines respect the
    lock until heartbeat staleness threshold expires. Standard
    pattern, but real work.

### Genuinely tricky — not solved by v1 or v2 mechanically

13. **Resource conflicts between parallel branches.** Two branches
    both write to `db.users.id=42`. The engine has no idea this is a
    conflict — it's at the tool layer. Blueprints must be authored
    with side-effect awareness. Engine can mitigate by logging
    step-level concurrency for debugging; cannot prevent. **Blueprint
    authoring concern, not an engine concern.**

14. **Long-running workflows that span engine versions.** v2
    workflow parked Tuesday, engine upgraded Wednesday, schema or DSL
    semantics changed. State migration on resume. Real workflow-system
    territory; defer until you have actual upgrade scenarios, then
    build versioned blueprint storage with explicit migration steps.

### Where we've been most hand-wavy

Three areas where the architecture has been talking around problems
rather than solving them. Concrete answers:

- **Notification ordering and delivery guarantees.** "PostMessage is
  best-effort" is honest, but we haven't said what the iframe *does*
  on detected drift other than `engine.get_state`. Concrete answer:
  iframe shows a "Reconnecting..." spinner during the snapshot fetch,
  then renders fresh. If `get_state` itself fails, show an explicit
  error with a retry. Don't try to be clever about partial recovery.

- **Engine-shutdown semantics.** What does v2 do when the host kills
  the engine subprocess mid-workflow? Engine traps SIGTERM (or
  equivalent), runs a 1-2 second graceful drain (write parked state
  for everything in progress, abort in-flight calls, close DB), then
  exits. If killed harder (SIGKILL), workflows in progress are
  recovered on next engine start by reading `WorkflowStore` state and
  either resuming from last checkpoint or marking errored with a
  recoverable flag.

- **`await_user` timeout semantics.** What if the user never
  responds? v1 has no answer (engine just blocks until the host
  kills it). v2 needs a per-checkpoint timeout
  (`timeout: "1h", on_timeout: "fail" | "auto_reject" | "auto_approve"`)
  and an expiry sweep on engine startup that processes any
  checkpoints whose deadline has passed.

## What to borrow from LangGraph (and what not to)

The "Why isn't this just LangGraph?" section earlier in this document
argues against adopting LangGraph as the engine substrate. That answer
stands. But LangGraph has solved several overlapping problems and its
abstractions are well-designed. The right move is to borrow the
*patterns* without taking the *dependency*. This section names which
patterns are worth stealing, which are not, and what tier of LangGraph
adoption (if any) makes sense.

### Where LangGraph helps the async surface

Mapping the async issues from the previous section to LangGraph
features. *Yes* means LangGraph has built-in machinery that reduces
your work; *No* means it's outside the runtime's scope or solved at a
different layer.

| # | Async issue | LangGraph helps? |
| --- | --- | --- |
| 1 | Three-hop iframe latency | No — architectural, outside runtime |
| 2 | PostMessage best-effort delivery | No — UI transport, outside scope |
| 3 | Per-step timeouts | Partial — retry policy handles attempts; timeout race is yours |
| 4 | Cancellation propagation | **Yes** — `asyncio.CancelledError` / `AbortSignal` propagation |
| 5 | Promise leaks / watchdog | Partial — recursion limit guards loops; hung calls still hang |
| 6 | Multi-iframe from parallel | Partial — Send API isolates branch state; iframe coordination is yours |
| 7 | Backpressure on notifications | **Yes** — stream-mode async generators |
| 8 | Disconnect vs slow-host | No — transport concern |
| 9 | Resume race conditions | Partial — versioned checkpoints; single-resume policy is yours |
| 10 | Engine restart while running | **Yes** — checkpointer + super-step resume |
| 11 | Cross-session iframe re-rendering | **Yes** indirectly — state in checkpointer |
| 12 | Concurrent engines on shared SQLite | No — workflow ownership across engines is application logic |
| 13 | Resource conflicts in parallel | No — out of scope |
| 14 | Workflow versioning across upgrades | Partial — checkpoint format versioned; DSL semantics are yours |

The genuine wins concentrate in **#10 (engine restart durability)**,
**#7 (backpressure)**, **#4 (cancellation)**, and indirectly
**#11 (cross-session)**. The iframe-side, transport-layer, and
multi-engine coordination issues — most of what bites in practice —
are outside LangGraph's scope.

### Three tiers of "use LangGraph"

**Tier A — use LangGraph as the engine** (the hybrid). Argued
against earlier in this document: forces a Python or Python-flavored
TS runtime into a working TS engine, retains LangGraph's graph model
when you want a 5-primitive JSON DSL, dual state machines invite edge
cases. Not recommended.

**Tier B — use LangGraph's checkpointer as a library, ignore the
rest.** Import `@langchain/langgraph-checkpoint-sqlite` (or
`-postgres`) and adapt your `ExecutionStore` over its
`BaseCheckpointSaver` interface. Inherit their durable-state work
without inheriting the graph runtime.

The dominant reason to reject this is **dependency weight, not
channel impedance**:

- `@langchain/langgraph-checkpoint-sqlite` transitively pulls
  LangChain core. LangChain core carries LLM-provider abstractions,
  prompt templates, message-history machinery, output parsers,
  callback systems — all of which are unused by a deterministic
  blueprint executor that never makes an LLM call. You pay
  install-size, audit-surface, and version-churn costs for a toolbox
  you don't open.
- Secondarily, the channel/reducer state model creates impedance:
  channel values with typed slots and reducers, channel versions,
  pending writes (writes from a node not yet applied), parent
  checkpoint IDs for branching/time-travel, configs with optional
  subgraph namespaces. Your state is a flat variable-binding dict +
  frame stack + execution log. Mapping into their model means
  putting everything in one synthetic channel (using their store
  as a blob store, losing their semantics) or mapping each
  blueprint variable to a channel (schema drift). The interface
  *shape* is borrowable; the *implementation* assumes a state
  model you don't have.
- ~300 LoC of SQLite plus an append-only run log is small enough
  to own in-tree. The implementation cost recovers in maintenance
  predictability and dependency surface within the first major
  upgrade cycle.

**Tier C — steal the patterns, don't import the dependency.**
Replicate the well-designed parts in your own code, keyed to your own
state shape. Recommended.

### Patterns worth stealing (Tier C)

The design wins, ported to TS without the dep:

- **Save-after-every-primitive checkpoint boundary.** LangGraph saves
  state after every node execution; resumption granularity equals
  node granularity. Your equivalent: every primitive execution is a
  checkpoint. Don't try to checkpoint inside a `call`; only between
  primitives.

- **Interrupt-and-resume as a single exception type, caught at one
  site.** LangGraph throws a known `Interrupt` type; the runtime
  catches at the node boundary and converts to a deferred handle.
  Your `await_user` should follow the same pattern: a known
  exception (`AwaitUserInterrupt`) thrown by the await primitive,
  caught only by the interpreter loop, converted to a parked-state
  record. Keeps await logic out of every step and concentrated in
  one place.

- **Stream-mode taxonomy.** LangGraph's stream modes — `values`
  (full state), `updates` (delta per node), `events` (debug events)
  — map cleanly to your notification types: `snapshot`,
  `step_completed`, plus a debug-only event stream. Steal the
  taxonomy; the names hint at the right semantics.

- **`BaseCheckpointSaver` interface shape — adapted, with two
  deliberate departures.** Their methods (`put`, `get`, `getTuple`,
  `list`, `putWrites`) are well-thought-out. The doc's
  `ExecutionStore` interface (in the v1 shape constraints section)
  borrows the *shape* (`get`, `list`, write-path methods returning
  Promises) while departing on two points:
  - **Split mutable snapshot from append-only audit log.** Their
    `BaseCheckpointSaver` mixes both; the doc separates them
    (`ExecutionStore` and `AuditLog`), since they have different
    consistency and storage requirements.
  - **Atomic lifecycle transitions, not raw CRUD.** `park`,
    `resume`, `cancel` are first-class methods rather than
    sequences of `get → mutate → put`. v2's concurrent-engines
    case requires atomic transitions; encoding them in the
    interface prevents callers from rolling racy sequences.

  An adapter to LangGraph's checkpointer would still be possible —
  squash both the snapshot and the most recent log slice into a
  single checkpoint blob, fake the channel protocol — but the
  dependency-weight argument above already rules out taking the
  dep, so the adapter ergonomics are moot.

- **Retry-policy DSL.** `RetryPolicy(max_attempts=3,
  backoff="exponential", on_exceptions=[...])` — declarative, per
  node. Adopt at the `call` level in the blueprint:

  ```json
  {"call": "k8s.apply", "args": {...},
   "retry": {"max_attempts": 3, "backoff": "exponential",
             "on": ["timeout", "5xx"]}}
  ```

- **Cancellation propagation contract.** LangGraph documents what
  happens when a graph is cancelled mid-node. Document the same for
  your engine: `AbortSignal` propagates from interpreter → MCP
  client → in-flight call. Steps mid-execution get cancelled and
  marked errored. Document explicitly that some downstream tools
  won't honor cancellation; document that the engine doesn't wait
  for them past the timeout.

- **Thread / workflow_id model.** Already aligned.

### Patterns worth NOT stealing

- **Graph topology vocabulary.** Nodes + edges + conditional edges
  is their model. Your DSL is 5 primitives. Don't import their
  topology language; it'll leak into your blueprint format and
  fight against the JSON-tree shape.

- **State-reducer system.** Channels + reducers is their state-merge
  model. You have JMESPath. Different mechanism, same intent. Don't
  try to port reducers; use JMESPath consistently.

- **Subgraph machinery.** Their subgraphs nest graphs with isolated
  state and cross-boundary streaming. You have `pipe` and `parallel`
  as compositional primitives in the same DSL. Same expressive
  power, simpler.

### TS-native alternatives if you outgrow Tier C

Worth knowing about in case SQLite + run-log becomes insufficient:

- **Temporal TypeScript SDK.** Real workflow orchestration. Heavy
  (requires Temporal server). Provides durable execution, retries,
  signals (= your `await_user`), timers, child workflows. Overkill
  for the client-hosted default; appropriate if you reach the Mode B
  hosted variant at scale.

- **Inngest.** Lighter than Temporal, TS-native, supports durable
  functions. Hosted offering available. Reasonable middle ground if
  you outgrow SQLite but don't want Temporal.

- **XState.** State-machine library with persistable actors. Not
  really for long-running workflows but the durable-actor pattern is
  similar. Listed for completeness; probably not the right fit for
  the DSL shape.

### The Tier C recommendation

Steal the patterns above. Don't take the LangGraph dependency. The
design wins LangGraph offers are mostly *interface and discipline*
wins, not *implementation* wins. The implementation wins (production-
tested SQLite checkpointer) come bundled with impedance mismatch
(channel-based state) that costs more than reimplementing.

Concretely for v2: ~300 LoC of SQLite + an append-only run log,
written against the split `ExecutionStore` + `AuditLog` interfaces
defined in the v1 shape constraints section, with `AwaitUserInterrupt`
as a single exception type caught in the interpreter loop. This
achieves most of LangGraph's #10 win (engine restart durability) and
#11 win (cross-session resume) without any of the deps.

## Adjacent workflow systems — hard-won lessons beyond LangGraph

LangGraph is the closest neighbour in the LLM-orchestration space, and
the previous section treats it in detail. But the broader workflow-
orchestration landscape — Temporal, Restate, Inngest, AWS Step
Functions, Argo Workflows, Tekton, Camunda BPMN, the Serverless
Workflow Spec — has accumulated decades of operational scars. This
section distills the lessons most relevant to a data-first MCP-native
engine, grouped by what every mature system either solved or learned
the hard way.

### Landscape map

The engine occupies a sparse quadrant: **declarative/data-first AND
natively MCP-aware**. The four populated quadrants around it:

- **Code-first, durable execution** (Temporal, Restate, Inngest, Azure
  Durable Functions): workflows ARE code. Polyglot, production-grade,
  rich human-in-the-loop primitives. None are MCP-aware; all require
  a custom MCP wrapper to participate in agent orchestration.
- **Code-first, stateful LLM orchestration** (LangGraph, CrewAI):
  workflows are Python graphs. First-class LLM nodes; the workflow is
  not a searchable / diffable artifact.
- **Data-first, infrastructure-centric** (AWS Step Functions/ASL,
  Argo Workflows, Tekton, Nextflow, Serverless Workflow Spec):
  workflows are YAML/JSON definitions. Not designed for LLM
  authorship; expression languages bolted on after.
- **Data-first, no-code** (n8n, Zapier, Prefect flow decorators):
  visual or near-visual. Good for business users; poor for LLM
  authorship.

The engine's claim to genuine novelty: LLM-authored JSON workflow as
first-class retrievable artifact, executed by an engine that is
simultaneously an MCP server (upward, to agents) and an MCP client
(downward, to tool servers) — the Mediator pattern applied to MCP.
The Parmar 2026 paper appears to be the first indexable work framing
it this way. Community response is nascent as of May 2026.

### What every "workflow as data" system learned the hard way

AWS Step Functions / ASL is the most mature data-first workflow system
and the most instructive prior art. The lessons are not unique to it:

- **In-flight versioning is painful.** Running executions are bound to
  the definition they started with. AWS shipped immutable versions +
  aliases in 2023 specifically so teams could deploy without breaking
  in-flight workflows. **The blueprint engine inherits this problem:
  what happens to a parked workflow when the blueprint it was
  executing is updated?** Required policy: immutable blueprint at
  park time? Copy-on-park? Version field on `WorkflowState` with
  refusal to resume against a different version? The doc does not
  address this yet; it must before v2 ships.
- **Conditional logic eventually escapes the DSL.** ASL added
  `States.Format`, `States.JsonMerge`, `States.ArrayGetItem` and
  similar intrinsic functions as escape hatches. The Serverless
  Workflow Spec community debated swapping JSONPath for jq for the
  same reason (Issue #216). Every data-first workflow system
  eventually needs to express "choose path A or B based on step N's
  output" and either limits the language or opens a trap door into
  code. **Parmar's deliberate position — "conditionals require agent
  reasoning; re-engage the design phase" — is principled but will
  break when the condition is genuinely data-driven** (e.g., "if the
  Kubernetes cluster has >100 pods, use the batched path"). The
  doc should state this as a boundary with explicit policy: when
  re-engagement is appropriate, when an intrinsic-style escape hatch
  is appropriate, and when the workflow simply isn't a good fit.
- **Sub-workflow composition is table-stakes.** Step Functions, Argo,
  Temporal, BPMN engines — all support nested workflow invocation.
  The 5-primitive DSL has no mechanism for one blueprint calling
  another. See the "Design space kept open" section's "cross-blueprint
  composition" angle.
- **Saga / compensating transactions are absent.** No rollback, no
  compensating actions. Step Functions Express Workflows and Argo
  both ship retry + catch + fallback paths. If step 5 of 10 fails
  after steps 1-4 had side effects, the 5-primitive DSL has no way
  to undo them. The error strategy is "continue or abort." This is
  a known limit; it surfaces as the first hard constraint when
  blueprints touch stateful external systems.

What the 5-primitive DSL covers well: sequential coordination of
independent MCP tool calls, fan-out/fan-in via `parallel`, chained
data transformation via `pipe`, collection iteration via `loop`,
batch aggregation via `collect`. The 80% case the paper claims is
real. The 20% that falls outside is also real and not small in
production.

### Human-in-the-loop — the full taxonomy

The doc's blocking-vs-yield comparison covers two of five mechanically
distinct shapes the industry has converged on. The full set:

| Shape | Example | How it works | Connection held during wait? |
| --- | --- | --- | --- |
| **Blocking RPC** | Camunda BPMN sync user task; this doc's v1 | Engine holds connection open while human acts | Yes |
| **Task token / callback** | AWS Step Functions `.waitForTaskToken` | Engine embeds opaque token in outgoing message; resumes via `SendTaskSuccess(token, result)` | No |
| **Durable promise / awakeable** | Restate `ctx.awakeable()` | Engine emits ID, suspends, releases compute; external HTTP callback resolves | No |
| **waitForEvent** | Inngest, Cloudflare Workflows | Function pauses until matching event arrives on event bus | No (but bus-bound) |
| **Yield / parking** | This doc's v2 default | Engine serialises state, returns `pending`, closes connection; resume via separate tool | No |

The task-token pattern and the durable-promise pattern are the same
shape at different infrastructure layers. The doc's
`engine.resume(workflow_id, checkpoint_id, payload)` is structurally
the task-token pattern on top of SQLite. **MCP SEP-1686 Tasks** has
been **accepted by Core maintainers in iterating-to-final state**
(per @dsp-ant on the PR thread, May 2026) — not draft, not
experimental in the casual sense. Method shape: `tasks/create` →
state machine `working` / `input_required` / `completed` / `failed`
/ `cancelled` (the deprecated `submitted` and `unknown` statuses
were removed during iteration); `tasks/get` polls; `tasks/result`
blocks until terminal. The `input_required` status is exactly the
field `await_user` parking would lean on.

**Known gaps** (open issues at time of writing):
- **Idempotency** (issues 2452, 2451): explicitly deferred to a
  separate SEP. Receiver-generated task IDs mean a lost
  `CreateTaskResult` leaves the requestor unable to deterministically
  retry without duplicate-task risk. Mitigations (`tasks/list`
  discovery, server-side dedup, Streamable HTTP reliability) are
  partial.
- **Deadlines** (issue 1956): community-debated; currently treated
  as a server-implementation concern, not a protocol field.
- **TTL retention windows** for hours-long workflows: unaddressed at
  the protocol layer; receivers MAY override but no normative
  guarantee.
- **`input_required` loosened**: from "request-specific" to "any
  information the server needs from the client" — slightly weaker
  than ideal for a structured await-user contract.

**Practical implication for the engine.** Building `await_user`
parking on SEP-1686 today (May 2026) is structurally feasible:
`tasks/create` returns `taskId`, the task enters `input_required`,
the requestor sends input, the task moves to `completed`. But
durability across server restart is delegated to the server
implementation. The engine still owns the parking layer; SEP-1686
just makes the upstream protocol observable and standard. The
significant simplification arrives when the idempotency and TTL
SEPs land. See "Substrate dependencies" below.

### Where the design aligns with convergent industry practice

- **Snapshot + audit log is the right storage split.** LangGraph's
  SQLite checkpointer (checkpoints table + writes table), Temporal's
  history, every mature workflow system. The doc's `ExecutionStore`
  (CAS snapshot) + `AuditLog` (append-only) is correct and matches
  production systems.
- **Timeout on human-approval states is mandatory.** Step Functions
  docs, Temporal tutorials, this doc — all say the same thing.
- **~300 LoC SQLite checkpointer estimate** is consistent with
  LangGraph's ~400 LoC Python implementation. Temporal's equivalent
  is orders of magnitude larger because it handles distributed
  execution and replay-at-scale, which is out of scope here.

### Where the design has gaps prior art treats as table-stakes

- Blueprint versioning policy for in-flight (parked) workflows when
  the blueprint changes
- Sub-workflow composition (cross-blueprint `call`)
- Conditional branching on runtime data (or an explicit boundary
  policy for re-engagement)
- Error compensation / rollback / saga semantics
- Explicit measurement of host tool-call timeouts before relying on
  blocking `await_user`

## Substrate dependencies and pre-v1 experiments

Several v1 commitments depend on factors outside the engine. Each
deserves an empirical or research answer before locking in the
corresponding piece of the architecture.

### Substrate questions

- **MCP Tasks (SEP-1686).** Accepted by Core maintainers in
  iterating-to-final state. State machine usable today
  (`working` → `input_required` → `completed`/`failed`/`cancelled`).
  Outstanding work: idempotency (deferred to separate SEP),
  deadlines (still server-implementation concern), TTL retention
  for long-running parked workflows. If those gaps close, the v2
  SQLite parking layer becomes thinner — the engine still owns
  durable state but delegates the protocol surface. Track all
  associated issues (2451, 2452, 1955, 1956); revisit at every MCP
  minor-version release.
- **nx plan library as `BlueprintStore`.** If the engine ships
  *inside* nexus, the plan library (`plan_save` / `plan_match` /
  `plan_promote`) is a strict superset of `BlueprintStore`
  functionality — semantic search, tiered promotion (T1 scratch →
  T2 project → T3 cross-project), retrieval lifecycle. If the
  engine ships *standalone*, separate SQLite is the right call.
  Decide explicitly; document the chosen path in the deployment
  section.
- **Per-tool effect annotations in the MCP ecosystem.** Effect-
  shadow static analysis (Design Space § 1) and capability-aware
  blueprints (Design Space § 2) both depend on tools declaring what
  they read / write / mutate. No such annotation standard exists in
  MCP today. The engine could ship a recommended annotation schema
  and become the demand-side pressure for it — or wait for an
  ecosystem standard. Track; do not block v1 on it.
- **ext-apps maturity for parallel branches.** The current ext-apps
  spec (2026-01-26) provisions one iframe per `ui://` resource per
  tool call. Multi-iframe routing for `parallel { await_user;
  await_user }` is not specified. The doc commits to the parent-
  iframe strategy (single iframe with sub-views keyed by
  `branch_id`) as v1 default; per-branch-iframe is deferred to
  either a spec extension or per-host implementation.

### Pre-v1 experiments worth running

Five concrete experiments that resolve currently-unknown facts the
v1 design is making bets on. Each is bounded; each produces a
yes/no or a number that changes a design decision.

1. **Measure each target host's stdio tool-call timeout.** Build a
   tool that sleeps and instrument when the host kills it. Cover
   Claude Code, Cowork, ChatGPT (where applicable), VS Code. One
   afternoon of work. Determines whether v1 blocking `await_user`
   is viable at all without Streamable HTTP. If timeouts are under
   ~2 minutes, the blocking model has no path to human-approval
   use cases; force Streamable HTTP for any await-enabled workflow.

2. **Verify the existing TS interpreter's execution model.** Read
   the engine code. If it uses recursive descent over the JSON
   tree with execution position living in JS call frames,
   v1 shape constraint #1 ("state is data") is a real refactor,
   not a discipline. If it uses a flat iterating loop with
   explicit frame objects (matching Parmar's Algorithm 1),
   serialization is a writer addition. One hour.

3. **Find the JMESPath wall in the first 10 real blueprints.**
   Build representative blueprints for 3-5 actual workflows (not
   the CMDB example). Locate the first blueprint that needs to
   *construct* a new object from parts of multiple prior step
   outputs (not just extract). That is the JMESPath limit. Decide
   from evidence: add JSONata (query + construction), add
   intrinsic functions (States.Format style), or enforce
   re-engagement. Without this experiment the choice is anticipation.

4. **Spike `await_user` on top of MCP Tasks (SEP-1686).** Implement
   parking via the Tasks primitive (accepted by Core maintainers,
   iterating to final) as a parallel v2 path. State machine is
   usable today; idempotency / TTL gaps are server-side concerns
   the spike will surface. If the idempotency + TTL SEPs land, the
   spike's adapter becomes the v2 implementation and the SQLite
   parking layer is thinner.

5. **Prototype multi-iframe routing for `parallel { await_user;
   await_user }`.** Build a workflow with two concurrent
   `await_user` branches and observe whether host (Claude Code,
   Cowork) actually routes notifications correctly to two
   simultaneously-active iframes — and if not, whether the
   parent-iframe strategy works in practice. The doc commits to
   parent-iframe as v1 default specifically because per-branch
   routing is unverified; verify before relying on either.

## Design space kept open

The previous critique rounds and brainstorm pass surfaced eight angles
the engine could expand into but has not committed on. Each is sketched
here so the decision to defer (or accept) is explicit, not silent.

### 1. Effect-shadow static analysis

From a blueprint plus per-tool effect annotations (read / write,
target resource, idempotency), compute the *effect shadow* of the
entire workflow before execution: which tables it touches, which
side effects it commits, whether two parallel branches conflict,
whether the workflow is idempotent end-to-end. **Buys:** CI-style
"this blueprint touches production" warnings; blueprint-level
idempotency proofs justifying safe re-execution; feeds capability-
aware permissions (next). **Costs:** depends on a tool-annotation
ecosystem that doesn't yet exist; partial coverage may mislead.
**Status:** deferred pending MCP effect-annotation standard;
prototype on internal MCP servers possible now.

### 2. Capability-aware blueprints (workflow-as-privilege-bracket)

MCP tool permissions are per-tool. A workflow is one tool that fans
out to N tool calls. An agent granted permission to call
`engine.run_workflow` has transitively been granted permission to
every tool that workflow invokes. Two design moves: (a) blueprints
declare their *required* tool capabilities up front, the host gates
`run_workflow` against the union; (b) the engine does subject-
shifting — tool calls from within a workflow run under the
blueprint's identity, not the agent's, with a separately-granted
capability bundle (like `sudo` rules). **Buys:** meaningful permission
decisions on workflows; grant agents access to *outcomes* without
granting underlying tools. **Costs:** identity threading already
present (constraint #7) gets richer; capability-negotiation UX is new.
**Status:** deferred; would significantly change the security model
of MCP-mediated workflows if adopted ecosystem-wide.

### 3. Cross-blueprint composition (sub-workflows)

Does `engine.run_workflow` register itself as a tool that the engine
can `call`? If yes, blueprints become composable units. If no, agents
inline-copy logic between blueprints. Hard part is recursion: a
blueprint that `call`s `run_workflow` with its own ID is a fixed
point. Required machinery: call-stack depth limit; cycle detection
in `BlueprintStore` (treat blueprints as a directed graph; reject
cycles at save time, or detect at runtime); naming/visibility (does
workflow A in project P see workflow B in project Q?). **Buys:**
real composition; library-of-blueprints culture. **Costs:** debugging
across nested workflow boundaries needs the trace-tree from § 4 to
be readable. **Status:** v1.x roadmap item; table-stakes in every
mature workflow system, only deferred because the v1 surface is
already large.

### 4. OpenTelemetry trace-tree by construction + cost roll-up

`parent_step_id` and `branch_id` are already in the audit log shape.
Emit OTel spans natively from the interpreter and a workflow becomes
a single trace tree by construction — no downstream instrumentation
needed. Cost attribution rolls up free (per-tool-call cost from
response metadata → per-step → per-workflow → per-blueprint-version).
**Buys:** drop-in compatibility with every observability stack;
cross-system tracing if downstream tools also emit OTel. **Costs:**
OTel dependency in the engine's hot path; sampling becomes an engine
concern. **Open question:** is the audit log redundant with OTel
spans? Probably yes — they are the same artifact at different
abstraction levels. **Decision needed:** does the engine maintain
two artifacts (audit log + OTel spans) or unify them (audit log =
OTel span sink)? The unification is preferable; the audit-log
schema in v1 should be chosen as a subset of OTel span attributes.

### 5. Workflow-as-test-fixture (record/replay)

A deterministic JSON-driven engine over MCP tool calls has the shape
of a record/replay substrate. Capture each `(tool, params) → result`
pair during a real run (the audit log already does this); replay
the blueprint with all `call` steps shadowed by recorded responses.
**Buys:** regression tests for the engine; validate blueprint changes
without re-spending tool budgets; reproducible bug reports
("audit log + blueprint, replay it"); test substrate for v1→v2
migration. **Costs:** result-fidelity is fragile (timestamps,
UUIDs, generated IDs); needs scrubbing/canonicalisation. **Status:**
near-free if the audit log is designed with this in mind; principal
cost is making the audit log schema deterministic.

### 6. Semantic blueprint diff

Two blueprints can be structurally identical with parameter swaps
inside `pipe`, or textually divergent but semantically equivalent
(renamed step IDs). Semantic diff normalises step IDs by topological
position, computes graph isomorphism on the data-flow DAG, surfaces
"this adds a side-effecting `call` between X and Y" rather than
"8 lines changed." **Buys:** meaningful PR review on blueprint
changes; safe auto-merge of equivalent blueprints. **Costs:**
graph-isomorphism is non-trivial; partial implementations may
mislead. **Status:** deferred until blueprint culture exists.

### 7. Blueprint mining from observed agent traces

The inverse of authoring. Given a corpus of agent traces (audit
logs from prior, blueprint-less runs), mine recurring tool-call
sequences and propose blueprints automatically. **Buys:** solves
cold-start — watch agents solve the same problem 5 times, then
offer "want me to encode this as a workflow?"; quantifies blueprint
value via shadow execution ("the last 50 times you did this
manually cost X tokens; the workflow would have cost Y"); aligns
with `plan_match` philosophy. **Costs:** trace privacy
(cross-project mining); false-positive blueprints from coincidentally
similar traces. **Status:** stated future direction; the audit-log
schema in v1 must be designed to be mineable (this is a constraint
on the schema, not a v1 deliverable).

### 8. UI surfaces beyond ext-apps

Voice (Twilio bidirectional), Slack thread, mobile push, plain
email — all fit the "deliver prompt → external system → return
input → resume" shape. Voice can hold 30s; Slack-thread cannot
hold 4h. **Forces yield over blocking** for any substrate beyond
iframes. **Buys:** workflows reach humans wherever they are;
engine becomes interaction substrate, not just iframe substrate.
**Costs:** the v1 blocking model breaks for these; multi-channel
routing logic. **Status:** indirectly informs the v2 yield-default
decision (above); explicit UI-surface adapters are a v2.x roadmap
item.

### Alternative considered: "no v2 durability, ever" (the steelman)

The doc frames v2 durability as "purely additive" — but does not
argue that v2 is *needed*. The steelman alternative:

- Every tool the engine calls must be idempotent (or carry a dedup
  key). This is already required for retry; raising it to a
  blueprint-level invariant costs little.
- Workflows that need long human time use the yield model with the
  agent re-issuing on resume.
- Engine crash loses in-flight workflows but blueprints are
  durable, so recovery is "the agent re-runs the workflow."

**Buys:** massive simplification — no `ExecutionStore` SQLite, no
checkpoint serialization, no resume race conditions, no dual-
database locking, no version-skew on in-flight workflows. The engine
is genuinely stateless; horizontal scaling is trivial.

**Costs:** places hard constraints on tool design (idempotency or
it doesn't compose); workflows with non-idempotent side effects
(charging a card, sending email) need explicit dedup keys threaded
through; loses the "park a workflow for 3 days" use case entirely
or pushes it to the agent's calendar.

**Why we still plan v2 durability:** the "park for days" use case
is real and increasingly important as workflows extend to
substrates beyond iframes (§ 8 above). Idempotency-as-invariant
is a tax on the ecosystem the engine cannot unilaterally impose.
But naming this alternative explicitly forces v2 to defend itself
on merits — if the v2 work-item list becomes unwieldy, this
steelman is the fallback.

## Decision posture

The decisions accumulated through this document, consolidated. Each
section names the choice, the alternative considered (where there was
one), and the load-bearing reason.

### Substrate

- **Engine is an MCP server natively, not a library wrapped in one.**
  Simultaneously an MCP server (upward, to agents) and an MCP client
  (downward, to tool servers) — the Mediator pattern.
  *Alternative:* Python library (LangGraph) wrapped in a custom MCP
  server. *Reason:* workflows become tools to any MCP-compliant agent
  without wrapping; cross-host compatibility falls out of the
  architecture; the Mediator is the architecture, not a layer on top.

- **Artifact is a JSON document, not code.** Blueprints are stored as
  JSON over the 5-primitive DSL.
  *Alternative:* Python graphs (LangGraph). *Reason:* LLM
  authorability (an LLM can reliably emit JSON over a fixed DSL but
  not correct Python over a flexible API), retrieval lifecycle works
  (`plan_search`, `plan_match`, `plan_promote` operate on data),
  structural diff and version-control work, cross-language portability
  at the artifact level.

- **Agent owns the orchestration loop; workflow is a tool.** The
  agent host (Claude, ChatGPT, Gemini, ...) is the top-level
  orchestrator and calls workflows as tools.
  *Alternative:* workflow runtime owning the loop. *Reason:* enables
  "compile a stable reasoning pattern into a workflow tool" without
  forcing the agent into the workflow's framework.

### DSL

- **Five primitives: `call`, `pipe`, `parallel`, `loop`, `collect`.**
  Four (`call`, `pipe`, `parallel`, `loop`) adopted unchanged from
  Parmar 2026. `collect` added by this design as an explicit batch
  aggregation primitive (capturing a pattern that Parmar's blueprints
  encode via `loop` + result-list manipulation, which we believe
  reads more clearly as its own primitive). Cite `collect` as a
  doc-level extension, not "per Parmar."

- **JMESPath for cross-step parameter passing — with a documented
  dot-path fallback.** Parmar's actual mechanism is `{{expression}}`
  templates with two strategies: JMESPath for structured extraction,
  and a simpler `steps.<id>.<field>` dot-path for direct references.
  The dot-path is not JMESPath, it's a separate (simpler) syntax.
  Adopted unchanged from Parmar including the fallback.

- **`await_user` is a semantic annotation on `call`, not a sixth
  primitive.** Mechanically a slow blocking RPC to a UI-emitting MCP
  server in v1. **v2 default is yield semantics** (return
  `pending_user_input` immediately, resume via separate
  `engine.resume` call) — driven by industry-converged HITL
  taxonomy (task-token / awakeable / waitForEvent patterns all
  release the connection), multi-channel UI surface requirements
  (voice / Slack / mobile / email all force yield), and the
  "always-fresh" steelman that yield naturally subsumes. The
  blocking-with-park alternative remains available as a v2 path
  but is not the assumed one. The annotation matters regardless of
  execution shape — for static analysis, policy, observability,
  and audit. The annotation matters for static analysis (warn about
  timeout exposure), policy (human-in-loop steps may need different
  auth), observability (separate user thinking time from tool
  latency), and audit (compliance often requires explicit human-
  approval markers).
  *Alternative:* a separate primitive with its own engine machinery.
  *Reason:* blocking RPC is what we already have; a new primitive
  would just rename it.

- **JMESPath for cross-step parameter passing.** Adopted unchanged
  from Parmar.

- **Parallel branches are v1.** `Promise.all` over MCP calls.
  Required to ship alongside `parallel`: error policy declaration
  (`fail_fast` vs `collect`), per-step timeout, cancellation
  propagation, optional concurrency cap, distinct step_ids per
  branch.

### Deployment

- **Default: client-hosted, in-process.** The MCP host (Claude Code,
  Cowork, ...) spawns the engine as a subprocess via standard MCP
  config. Engine, frontend, storage all in one process. Storage in
  user data dir.
  *Alternative:* daemon-by-default. *Reason:* cross-machine resume
  is out of scope; the client-hosted model gives session durability
  via SQLite without operational footprint.

- **Mode B variant: hosted service, only when remote access is
  required.** Same engine binary, different process model: Streamable HTTP
  MCP transport, REST gateway, auth middleware, multi-user identity
  scoping. Built only when targeting remote agent hosts (OpenAI
  Custom GPTs, hosted Gemini), browsers without local helper,
  distributed teams, or scheduled execution without a host session.

- **REST gateway exists only in Mode B, only for non-MCP clients.**
  Browsers using `fetch`, OpenAI Custom GPT Actions consuming
  OpenAPI, Gemini bare function-calling, curl/cron/webhooks, mobile
  apps, integration tests, microservice service-to-service. Local
  MCP-speaking hosts use stdio MCP and never go through the gateway.
  Blueprint authoring is not in the gateway's surface.

- **No per-host protocol adapter.** Hosts pick a transport (stdio or
  Streamable HTTP); the MCP TS SDK serves both against the same engine
  code.

### State and durability

- **v1: in-memory state, ephemeral execution.** Engine spawned per
  host session, exits when done. No durable storage required.

- **v2: persistent SQLite-backed `BlueprintStore` and
  `WorkflowStore` in user data dir.** ~300 LoC of SQL. Survives
  engine subprocess exits; cross-session resume via SQLite, not via
  long-lived process.

- **The 7 v1 shape constraints** (kept cheap in v1 so v2 is
  additive, not a rewrite):
  1. State is data, not code (pure-data JSON state with explicit
     frame stack — grounded in Parmar's iterative executor).
  2. Workflow IDs exist from day one (logging-only at v1).
  3. Execution log carries `parent_step_id` / `branch_id` /
     `loop_iteration` for nested-primitive observability;
     snapshot is the recovery source of truth.
  4. Engine code path branches on the await annotation.
  5. UI-MCPs are stateless re-renderers.
  6. Workflow tool's return schema is a discriminated union over
     `completed` / `parked` / `pending_user_input` from v1.
  7. All store calls and tool handlers thread an optional
     identity context (`null` at v1; populated by Mode B auth).

- **Single architectural commitment at v1: the split `ExecutionStore` +
  `AuditLog` interfaces**, with in-memory implementations. The shape
  borrows from LangGraph's `BaseCheckpointSaver` but deliberately
  separates mutable snapshot from append-only history (different
  consistency, storage, and lifecycle), and adds atomic lifecycle
  transitions (`park`, `resume`, `cancel`) so callers don't roll their
  own racy `get → mutate → put` sequences. v2 swaps in
  SQLite-backed implementations; call sites unchanged. Identity is
  threaded through every method (constraint #7) but defaults to
  `null` at v1.

- **`BlueprintStore` and `ExecutionStore`/`AuditLog` never share a
  backing store.** Blueprints have indefinite lifetime, are
  user-authored content, are version-controllable, and are
  read-mostly. Workflow runs are operational state with TTL,
  GC policy, and write-heavy access patterns. Mixing them in one
  SQLite file would couple backup/restore, schema migration, and
  vacuum cycles across two unrelated lifecycles. *Alternative
  considered:* one SQLite file with two table groups. *Reason
  rejected:* operational coupling outweighs the convenience of
  one file path.

- **Concurrent engines on the same machine** read/write the same
  SQLite via WAL mode + workflow_id-keyed heartbeat lock. Standard
  pattern, real work. v2.

### UI

- **ext-apps integration is native.** The engine is itself an MCP
  server emitting `ui://` resources. No separate frontend codebase
  per host.

- **Progressive enhancement, not a requirement.** Workflows that
  declare a `ui://` resource get rich UIs in supporting hosts
  (Claude family, ChatGPT) and fall back to plain text/JSON in
  non-supporting hosts. Per-blueprint declarations: `ui.default`
  for the whole workflow, `ui.checkpoints` for specific
  `await_user` UIs, `expose_state` allowlist to prevent leaking
  intermediate secrets to iframes.

- **Iframe is a stateless re-renderer.** State of truth lives in the
  engine; iframe pulls via `engine.get_state` on init. Drift
  detection via monotonic version + snapshot fetch on skip.
  Constraint enforced at v1 even though it is strictly unnecessary
  there, so v2 doesn't break every UI-MCP.

- **PostMessage protocol is engine-defined.** Engine emits
  `snapshot`, `step_started`, `step_completed`, `checkpoint`,
  `completed`, `error`, `cancelled`. Iframe calls back via
  `engine.resume`, `engine.cancel`, `engine.get_state` tools through
  the host's MCP client.

- **Engine never talks directly to iframe.** All iframe-side actions
  route through the host's tool-calling layer. Mediator pattern
  preserved at the UI boundary.

- **A small library of generic widgets** ships with the engine:
  `progress.html`, `form.html` (JSON-Schema-driven default for
  `await_user`), `pending.html` (v2 list view). Most blueprints
  don't write HTML.

- **Iframe interactions are discrete events, not continuous
  streams.** Three-hop latency makes real-time interactivity
  unsuitable for ext-apps; UIs needing real-time should be
  REST-gateway-backed in Mode B.

### Async surface

- **Per-step timeouts mandatory at v1.** Without a daemon, hung
  downstream calls stall workflows forever. Blueprint-declared
  timeouts with a hard ceiling.

- **Cancellation propagation contract documented.** `AbortSignal`
  from interpreter → MCP client → in-flight call. Some downstream
  tools won't honor; engine discards their results past timeout.

- **Backpressure via coalescing.** Engine collapses pending
  notifications of the same type and emits periodic snapshots for
  high-frequency steps.

- **Soft caps on concurrent human-in-the-loop branches.**
  `max_concurrent_checkpoints: 3` default per parallel block;
  exceeding raises a clear error rather than racing iframes.

- **Notification ordering and delivery guarantees:** PostMessage is
  best-effort. Engine emits monotonic version numbers; iframe
  detects gaps and shows a "Reconnecting..." spinner during
  `engine.get_state` snapshot fetch, then renders fresh.

- **Engine-shutdown semantics (v2):** trap SIGTERM, run a 1-2s
  graceful drain (write parked state, abort in-flight calls, close
  DB), exit. SIGKILL recovery on next start: read `WorkflowStore`,
  resume from last checkpoint or mark errored with a recoverable
  flag.

- **`await_user` timeout semantics (v2):** per-checkpoint `timeout`
  and `on_timeout` policy (`fail` | `auto_reject` | `auto_approve`).
  Expiry sweep on engine startup processes any checkpoints whose
  deadline has passed.

### LangGraph

- **No LangGraph dependency.** Tier C (steal patterns, don't import)
  is the chosen position.

- **Patterns to steal:** save-after-every-primitive checkpoint
  boundary, interrupt-and-resume as a single exception type,
  stream-mode taxonomy (`snapshot` / `step_completed` / debug
  events), `BaseCheckpointSaver` interface shape, retry-policy DSL,
  cancellation propagation contract, thread/workflow_id model.

- **Patterns NOT to steal:** graph topology vocabulary (nodes +
  edges + conditional edges), state-reducer system (use JMESPath
  consistently), subgraph machinery (use composition primitives in
  the DSL).

- **TS-native alternatives if you outgrow Tier C:** Temporal,
  Inngest. Reach for them only if measured limits exceed SQLite +
  run-log capacity.

### Deferred to v2 (additive, not rewrite)

- Persistent SQLite-backed `BlueprintStore` and split
  `ExecutionStore` + `AuditLog`.
- `engine.list_pending`, `engine.resume`, `engine.cancel`,
  `engine.get_state` MCP tools.
- `await_user` switches to yield semantics by default (see Decision
  Posture DSL section); blocking-with-park retained as alternative.
- "List my pending workflows" tool + ext-apps UI for it.
- TTL / expiry policy on parked workflows.
- Resume race-condition handling (first-wins with conflict response).
- Engine restart graceful drain + recovery on next start.
- Cross-engine concurrency lock with heartbeat.
- Mode B daemon mode (only if remote access becomes a requirement).
- **In-flight blueprint versioning policy** when a parked
  workflow's blueprint changes (immutable-at-park, or version-
  field-with-refusal, or copy-on-park). Must ship with v2;
  picking the policy is a v2 design item.
- **Cross-blueprint composition (`engine.run_workflow` as a tool).**
  v1.x roadmap item. Requires call-stack depth limit + cycle
  detection in `BlueprintStore` + naming/visibility rules.

### Substrate dependencies — track, decide before locking in

- **MCP Tasks primitive (SEP-1686).** If matures with retry +
  expiry + idempotency, v2 SQLite parking layer becomes redundant.
  Revisit at every MCP minor-version release.
- **nx plan library as `BlueprintStore`.** Decide explicitly
  based on whether the engine ships inside nexus or standalone.
  Document the chosen path.
- **Per-tool effect annotations in MCP.** Enables effect-shadow
  static analysis and capability-aware blueprints. No ecosystem
  standard yet; engine could be demand-side pressure.
- **ext-apps multi-iframe spec.** Current spec is single-iframe;
  per-branch routing for `parallel { await_user; await_user }` is
  unspecified. Parent-iframe strategy is v1 default; revisit if
  spec extends.

### Pre-v1 experiments (gate v1 commitments on these)

- **Measure each target host's stdio tool-call timeout.** Gates
  whether v1 blocking `await_user` is viable without Streamable HTTP.
- **Verify the existing TS interpreter is flat-loop or recursive
  descent.** Determines whether v1 shape constraint #1 is a
  discipline or a refactor.
- **Find the JMESPath wall** in 10 real blueprints. Decide
  JSONata / intrinsics / re-engagement from evidence.
- **Spike `await_user` on MCP Tasks (SEP-1686)** as alternative
  v2 path.
- **Prototype multi-iframe routing** for `parallel { await_user;
  await_user }` to verify host behavior.

### Genuinely deferred (no v2 commitment)

- Multi-tenancy in the hosted variant.
- Distributed execution / multiple engine workers behind a queue.
- Time-travel debugging (build against the audit log when
  operational pain justifies).
- Workflow versioning across engine upgrades (real workflow-system
  territory; build when actual upgrade scenarios arrive).
- Resource conflict detection between parallel branches (blueprint
  authoring concern, not engine concern).
- **Effect-shadow static analysis** and **capability-aware
  blueprints** — deferred pending MCP effect-annotation standard.
- **Semantic blueprint diff** — deferred until blueprint culture
  exists.
- **Blueprint mining from observed agent traces** — stated future
  direction; constrains v1 audit-log schema (must be mineable) but
  is not itself a v1 deliverable.
- **UI surfaces beyond ext-apps** (voice / Slack / mobile / email)
  — v2.x roadmap; informs the v2 yield-default decision.

### Open architectural questions

- **Audit log vs OTel spans — unify or maintain both?** The
  audit log shape (`step_id`, `parent_step_id`, `branch_id`,
  `loop_iteration`, `input`, `output`, `status`, `ts`) is a
  subset of OpenTelemetry span attributes. Maintaining two
  artifacts is redundant. Recommended position: the audit log
  IS an OTel span sink; the v1 schema is chosen as a subset of
  OTel span attributes. Decide before v1 ships.
