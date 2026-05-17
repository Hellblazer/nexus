# Agentic cockpit: tuple space common model + semantic events as integration

> **Implementation status (2026-05-17)**: the substrate described in
> this document shipped via RDR-111 (accepted 2026-05-13).  See
> [`docs/rdr/rdr-111-orb-agentic-cockpit-substrate.md`](rdr/rdr-111-orb-agentic-cockpit-substrate.md)
> for the accepted spec, `src/nexus/cockpit/` for the production
> code, and `nx cockpit --help` for the operator surface.

A design exploration. **Not a workflow engine spec.** The workflow engine
is handed off to the work-instance under a separate document set; here
it is a free variable. We assume *some* workflow capability exists; we
do not commit to which, at what level, or under whose execution model.
What this doc commits to is the *substrate* the harness needs, and
specifically two architectural moves.

## The two moves

**Move 1 — A semantic tuple space (RDR-110) is the common
*coordination* model.**

Nexus already has three retrieval surfaces that are structurally the
same abstraction with different names (plans, T1 scratch, T2 memory).
RDR-110 unifies them, adds Linda's atomic destructive read (`in`), and
makes the registry of types/dimensions first-class. The proposal is
accepted; this doc takes it as load-bearing.

The move beyond RDR-110's stated scope: **everything that wants to
coordinate uses the tuple space — but the tuple space is the
switchboard, not the bandwidth.** Coordination, discovery,
declaration, claims, bindings, layout state, profile state — these
live as tuples. Actual high-bandwidth or low-latency dataflow does
*not*. The bus is the store; the store is the switchboard. Writes are
events *about* state; events are coordination handles. The actual
data behind those handles flows over connections the tuple space
helps establish but doesn't carry.

**Move 2 — Semantic events are the local integration point.**

The harness (Claude Code today, Cowork / VS Code / web variants
ahead) has an existing event bus: hooks (`PreToolUse`, `PostToolUse`,
`Stop`, `SubagentStop`, `SessionStart`, `SessionEnd`,
`UserPromptSubmit`, `PreCompact`, `Notification`). These fire at
well-defined points but are *code-only* surfaces today — a hook
either runs a shell script or doesn't. The integration point we
need is to *project* these events into the tuple space as
semantically-typed tuples, so they become composable, queryable, and
addressable by intent.

Critically, semantic events are **milestones, not byte streams**.
The PreToolUse hook firing produces *one* tuple per tool call, not
a tuple per stdout line the tool emits. The workflow engine emitting
"step N completed" produces one tuple per step, not one per stream
chunk. Granularity matters. High-frequency dataflow stays on its
own transport.

Once both moves are made, most of what we want — composable C2
surfaces, multi-agent coordination, cross-instance visibility,
user-authored bindings — falls out structurally rather than needing
to be invented.

## The substrate is log-structured, per tier

Before going further: the tuple space is not a key-value store with
notifications bolted on. It is an **append-only event log per tier,
with a materialised "current tuples" view as a fold over the log**.
The four Linda-shaped primitives are operations on the log:

- `out(t)` appends an `out` record at LSN N.
- `in(template)` appends a `claim` record at LSN M, succeeding iff
  the materialised view shows a matching unclaimed tuple in the same
  transaction. The CAS is one SQLite tx; the log preserves who took
  what when.
- `rd(template)` reads the materialised view at the current LSN. Not
  logged — reads don't change state.
- Subscriptions ("notify") are **offset-tracking consumers**, Kafka /
  Redis-Streams shaped, not callback registrations. A subscriber
  holds an LSN cursor and advances it as it processes; restart =
  resume from last committed offset; "at-least-once" is just
  "subscriber hasn't advanced past LSN N yet." No callback to lose.

On one machine this is essentially free: SQLite WAL already
serialises writers through a single write lock, so the commit order
**is** the global total order — no consensus, no extra coordination.
The 1986-Linda and 1999-JavaSpaces literature paid total-ordering
cost as if it were a distributed problem; for the local substrate it
isn't.

### Logs (plural), not log

The three tiers have asymmetric lifetimes and scopes, and the
log-structured discipline has to respect that.

- **T2 is the canonical event log of the cockpit substrate.**
  Everything coordination-shaped lives here: hook projections,
  bindings, claims, manifests, liveness, mailboxes, layouts. One LSN
  sequence per project (one SQLite file). Total order within the
  project, across all processes touching it. Subscribers hold offset
  cursors; resume-from-offset is the recovery story. Compaction =
  snapshot at LSN N + prune below `min(consumer_offsets)`. When the
  rest of this document says "the tuple space," T2 is the tier it is
  load-bearing on.
- **T1 is per-process working memory; its log is local to the
  RDR-105 cohort.** "Local" here is wider than one agent: it is the
  parent Claude process plus everything that resolves through its
  `t1_addr.<pid>` discovery file — in-process `Agent`-tool subagents
  (share the parent's MCP scratch directly), `Bash`-invoked
  `nx scratch` calls in the same process (env passdown), and
  `claude -p` subprocesses dispatched with `share_t1=True`. Sealed
  off: default-`owned` `claude -p` workers, ephemeral one-shot
  operators, and other Claude instances on the same machine. Total
  order within a T1 instance is trivial (one chroma client, one
  writer) and is used for in-session replay/debug — not for
  cross-process coordination. If the process dies, the log dies;
  anything that needs to survive belongs in T2.
- **T3 is content-addressed knowledge — not a coordination log.**
  Writes are curated and deliberate (`store_put`, `nx_tidy`,
  `nx index`). Chashes are content-addressed; position is preserved
  by the catalog manifest; consumers query by content/tumbler, not
  by time. There is no "T3 event log" in the cockpit sense. When
  something in T3 changes that the cockpit *should* react to (new
  RDR indexed, knowledge consolidated, collection drifted), the
  change is **mirrored as a T2 event** referencing the T3 artifact
  (chash, tumbler). Subscribers watch T2; T3 just *is*.

### Cross-tier ordering

There is no global LSN across tiers, and we do not want one. Each
tier serves a different lifetime/scope; pretending they share a
clock would force coordination we do not need.

- Within T2: total order, free, load-bearing.
- T1 → T2 promotions (`--persist`, `memory_put`): logged as a T2
  event. The promotion is an LSN'd row in T2's log.
- T2 → T3 promotions (`nx_tidy`, knowledge consolidation): T3
  catalog write **plus** a T2 mirror event referencing the resulting
  chash/tumbler, so cockpit consumers can react without polling T3.
- Cross-tier "what happened when" is wall-clock timestamps,
  best-effort. Used for audit and debugging, not for correctness.

The discipline this gives us: **subscribers reason about ordering
within the tier they consume.** A binding watching for "hook fired
AND claim acquired" reads both off T2's single sequence, so the
causal relationship is preserved by construction. A binding that
needs to know "T3 got a new RDR" reads a T2 mirror event, not T3
itself — and the mirror event has an LSN like everything else on
the bus.

## Control plane / data plane — the discipline

Nexus is the **control plane** and the **switchboard**. Real
high-speed, specialized, point-to-point connections are the **data
plane**. The discipline that keeps this clean:

- **Tuples describe and broker connections; they do not carry the
  payload.** A tuple may say "agent X is streaming tokens to surface
  Y via endpoint Z, format F, lifetime L." The actual tokens flow
  directly from X to Y via Z. Nexus knows the connection exists,
  what type it is, who owns it, and when it should end. Nexus does
  not see (and does not need to see) every token.
- **Connections are first-class artifacts with explicit lifecycle.**
  Manifest (declare the connection as a tuple), establish (peers
  open the direct transport), use (data flows peer-to-peer),
  heartbeat (the manifest tuple is refreshed; expired manifests
  trigger teardown), tear down (release the tuple, close the
  transport). This is the same shape as SIP signaling for an RTP
  call, WebRTC signaling for a P2P data channel, Kubernetes service
  discovery for a pod-to-pod connection. The pattern is well-trodden;
  apply it.
- **The transport is whatever fits.** Direct stdio pipe between
  spawned processes. Unix socket. Named pipe. WebSocket from an
  ext-apps iframe to its data-producing MCP server. SSE stream from
  a workflow engine to a subscribed surface. File descriptor
  passing. Shared memory. Audio device. The cockpit substrate does
  not prescribe; the manifest declares the type and the peers
  negotiate the rest.
- **The line between control and data is bandwidth × frequency, not
  semantics.** A user-click event going through the tuple space —
  fine, low frequency, discrete. LLM tokens streaming at 50 t/s
  through the tuple space — never; each token would be a tuple,
  every consumer would index every token, the substrate would
  collapse. Tool stdout flowing through the tuple space at line
  granularity — also wrong; the workflow engine consumes the stream
  and emits step milestones to the tuple space. Rule of thumb: if
  it's a discrete observable event the user or another actor would
  want to *react to as a unit*, it belongs as a tuple. If it's
  fine-grained continuous data, it belongs on a direct connection
  with a manifest.

### What this means for the actors

- **LLM agent**: the LLM API call is direct from harness to provider.
  Tuples in the cockpit substrate carry agent *intent* (a tool call
  was issued), *milestones* (an agent step completed), and *claims*
  (agent holds a resource). Token streaming is not tupled.
- **Workflow engine**: runs as a separate MCP server process. The
  engine's *step events* are tupled (milestones); the engine's
  *intra-step dataflow* (e.g., reading a 10MB blob from a downstream
  MCP server and passing it to the next step) flows directly between
  the engine and its peers. If a surface wants live progress of a
  long-running step, the engine exposes a direct streaming endpoint
  declared in a manifest tuple; the surface connects to it.
- **Surfaces (ext-apps iframe, tmux pane, etc.)**: a surface's
  *binding* is tupled — "this surface is wired to event stream E."
  The actual data the surface displays may come from the tuple space
  (low-frequency events match directly into the surface) or from a
  direct connection declared in a manifest tuple (the iframe opens a
  WebSocket to a server endpoint listed in the manifest). SEP-1865's
  ext-apps model already supports this — the iframe is itself an MCP
  client and can hold connections.
- **Background workers**: their stdout streams to a log file or
  named pipe; their *milestones* (started, completed, failed, claim
  acquired) are tuples. The harness's `Monitor` tool streams the
  pipe to the foreground agent when asked; it doesn't route every
  byte through tuples.
- **Inter-agent messaging**: discrete messages are tuples in a
  mailbox. Streaming data between cooperating agents (e.g., agent
  A producing JSON that agent B consumes step-by-step) opens a
  direct pipe declared in a manifest tuple.

### Connection manifest — concretely

A manifest is a tuple type. Roughly:

```
connection_manifest {
  id: <uuid>,
  type: "stream" | "pipe" | "websocket" | "sse" | "file-fd" | ...
  producer: <actor-id>,
  consumer: <actor-id> | "any-subscriber",
  endpoint: <transport-specific address>,
  payload_type: <semantic type>,
  lifetime: <TTL or "until-released">,
  established_at: <ts>,
  last_heartbeat: <ts>,
}
```

The producer posts the manifest after the endpoint is ready. The
consumer (or any subscriber matching the type) reads the manifest
and connects directly to the endpoint. Heartbeats refresh the
manifest. Release of the manifest tuple (via `in` or TTL expiry)
signals teardown; the endpoint is closed.

This is *one tuple type*. The cost in the substrate is small. The
benefit is large: every direct connection in the system is visible
in the control plane, debuggable, claim-able, revoke-able, without
the substrate carrying any of the payload itself.

## What the harness already provides (and why we mostly leverage)

The agentic harness ships a remarkably complete coordination
substrate. Surveyed concretely:

| Capability | Mechanism | What it gives us |
|---|---|---|
| Hook bus | `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`, `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreCompact`, `Notification` | Process-level event channel at well-defined points |
| Background supervision | `Bash(run_in_background)`, `Agent(run_in_background)` | Lifecycle for long-running tasks |
| Push notifications | `Monitor` (stream-per-line), task completion notifications | Async push from background → main agent |
| Inter-agent messaging | `SendMessage(to: agentId)` | Direct mailbox between agents |
| Scheduled re-entry | `ScheduleWakeup`, `CronCreate` | Time-based triggers |
| Isolation | `Agent(isolation: "worktree")` | Filesystem isolation for parallel agents |
| Permission modes | `acceptEdits`, `bypassPermissions`, `plan`, `auto`, `default` | Lockdown discipline |
| MCP server set | per-session, hot-reloadable | Pluggable capability inventory |
| Plugins | commands + agents + skills + hooks + MCP servers bundled | Distribution unit |
| Persistent tiers | T1 scratch, T2 memory, T3 store | Multi-scope state |
| Issue tracking | beads + Dolt | Cross-session work queue |
| Skills | invocable verbs with arguments | User- or agent-issued commands |

That is — measured against any compositional dashboard / portal /
addon framework — a near-complete starter kit *already in the
runtime*. The two moves above turn the latent substrate into an
explicit one.

## Prior art that doesn't need reinventing

Brief, anchored. Each is decades-old and well-validated; the question
is which lessons port and which don't.

- **Linda + tuple spaces** (Gelernter & Carriero, 1986). Coordination
  as a separate concern from computation. `out` / `in` / `rd` / `eval`
  as the four primitives. RDR-110 directly. Forty years of
  distributed-systems theory says this is the right shape for
  cooperating-process coordination.
- **Blackboard architectures** (Hayes-Roth et al., Hearsay-II, 1970s).
  Specialists post hypotheses to a shared knowledge base; other
  specialists refine. The LLM is a specialist; the user is a
  specialist; surfaces are specialists. Same pattern.
- **Portal frameworks** (JSR-168/286, Plumtree, Liferay). Pluggable
  components in a container with shared session, IPC events, user
  customisation, saved layouts. The corporate-IT decade's worth of
  evidence on what works (composability, themes) and what fails
  (degeneration into siloed panels when IPC is too loose).
- **Bakke, Karger & Miller — Automatic Layout of Structured
  Hierarchical Reports** (InfoVis 2013). Schema-driven hybrid
  layouts (nested table / multi-column form / outline) selected per
  schema node and applied uniformly to every instance, with a
  three-phase Measure / Auto-Style / Layout pipeline driven by a
  schema-only stylesheet. Direct prior art for the cockpit's
  automatic-layout discipline; the layout-engine section below
  adapts it from static reports to the dynamic event-stream case.
- **WoW addons + WeakAuras** (2004–). The richest consumer-grade
  example of cooperating active surfaces over a shared world-state
  API. WeakAuras specifically: a *scriptable display engine* with a
  declarative trigger/display/action triple, user-authored without
  code, importable as strings. Millions of users author auras
  without being programmers. The existence proof that this pattern
  is *learnable*, not just buildable.
- **SCADA / DCS / mission control** (1960s–). Industrial command-and-
  control. The lineage of HUDs, tag-based data models, alarm
  systems, runbook integration. The dashboards that don't suck.
- **Streamdeck / Loupedeck / TouchPortal**. Consumer C2: buttons,
  pages, profiles, plugins. Per-app context auto-switching.
- **LabVIEW / Pure Data / Node-RED**. Data-flow visual programming.
  Architecture validates; UX (raw wiring) doesn't always.

The lesson from this set, distilled: **the abstraction is the
blackboard (typed shared state) plus the event bus (subscriptions
on the blackboard) plus a binding language (declarative
trigger→action), with the harness providing the rendering substrate.
Don't reinvent the abstraction. Provide a runtime where the
abstraction can actually be used.**

## What "semantic event" means concretely

A semantic event is a tuple posted to the tuple space that:

1. **Carries a typed payload.** Not an opaque string. The type is
   registered in the tuple-space dimension registry (RDR-110's
   registry). Consumers can query by type.
2. **Carries embeddable content.** The payload has a semantic-search
   dimension — a description, a target, a domain, an intent. This is
   what makes it *semantic* rather than just structured. A consumer
   can `rd` by associative match ("any event indicating a build
   failure in the catalog module") rather than only by exact type.
3. **Carries structural metadata.** Timestamp, source actor
   (`agent:abc123`, `user`, `tool:k8s.apply`), session, project,
   correlation ID. The retrieval surface filters cheaply on these.
4. **Is addressable for action.** If a consumer wants to *consume*
   the event (claim it for a one-shot reaction), `in` takes it
   atomically. If multiple consumers want to react in parallel, `rd`
   reads non-destructively.
5. **Is persistent at the tier appropriate to its lifecycle.** Some
   events are session-scoped (T1); some are project-scoped (T2);
   some are durable knowledge (T3). The tuple space tiers handle
   this without callers caring.

Examples of semantic events:

- `tool_called(tool="k8s.apply", args={...}, actor="agent:abc",
  session=S, intent="deploy frontend")` — emitted on PreToolUse hook.
- `step_completed(workflow=W, step=N, output={...}, actor="engine",
  duration_ms=124)` — emitted by the workflow engine.
- `user_decision(checkpoint=C, choice="approve", workflow=W)` —
  emitted when a user submits an iframe form.
- `claim_acquired(resource="memory:plan_state", holder="agent:xyz",
  ttl=300s)` — emitted on `in` of a coordination tuple.
- `attention_request(reason="long-running tool may timeout",
  surface="status-line", priority=low)` — emitted by a workflow that
  wants to surface awareness without interrupting.

The integration point is: **hooks fire → harness projects to tuple
space as semantic events → bindings subscribe → actions trigger →
surfaces render.** The hook bus is the producer; the tuple space is
the substrate; bindings are the composition language; surfaces are
the consumers.

## What the tuple space carries beyond the three existing surfaces

RDR-110's stated scope is unifying plans / scratch / memory. The
substrate move extends this — but always within the control-plane
discipline. The tuple space carries:

- **Milestone event log.** Every hook firing, every workflow step
  *completion*, every user action becomes a tuple. The audit log
  *is* the tuple space, queried by time and actor. (Note: the
  *stream* of stdout from a tool, the *stream* of LLM tokens, the
  *stream* of step-internal IO — these do NOT become tuples. The
  tool's *completion* with a result summary does. One tuple per
  observable milestone, not one per byte.)
- **Claims / leases.** Linda mutexes for shared resources. Active
  claims are visible as tuples; the active-claims panel is a
  workflow-as-app over `rd` of claim tuples.
- **Bindings.** User-authored triggers ("when event X, do Y"). Each
  binding is a tuple with trigger template + action descriptor +
  surface descriptor. Bindings are queryable, editable, toggle-able.
- **Connection manifests.** First-class declarations of direct peer-
  to-peer streams as described above. Each open high-bandwidth
  channel in the system has a manifest tuple; teardown is a tuple
  release.
- **Liveness / discovery.** Each active agent or instance posts a
  heartbeat tuple. `nx instances` is `rd` of liveness tuples within
  TTL. RDR-105's address-file discovery becomes tuple-space-native.
- **Layout / profile state.** Saved layouts, active mission
  contexts, per-context profile bindings — all tuples scoped by
  user, project, time-window.
- **Inter-agent mailboxes (small messages only).** `SendMessage`-
  style discrete messages become tuples keyed by recipient. The
  existing `SendMessage` tool remains the user-facing surface; the
  transport is the tuple space *only when the messages are
  small and discrete*. For agents that need to pipe data
  continuously to each other, a connection manifest declares the
  pipe and the agents communicate directly.
- **Selection / focus.** Shared cross-surface state. When a user
  clicks an item in one surface, the selection tuple updates; other
  surfaces that `rd` selection update their view. (A user click is
  one tuple, not 100Hz mouse-position.)

In each case, the tuple space gives us *one query model, one
persistence story, one debug surface, one access-control story* for
the **coordination metadata**. Payload-heavy dataflow stays in its
own appropriate transport, made visible to the substrate via
manifest tuples.

## The integration surface

Three small pieces of glue, each independently shippable.

**1. Hook → tuple bridge.** A single hook adapter that, on every
configured hook event, emits a corresponding semantic event tuple.
The mapping is mechanical:

```
PreToolUse{tool, args}     → tool_call_intent(...)
PostToolUse{tool, result}  → tool_call_completed(...)
SubagentStop{id, summary}  → agent_completed(...)
Notification{...}          → harness_notification(...)
UserPromptSubmit{prompt}   → user_prompt(...)
SessionStart{}             → session_started(...)
SessionEnd{}               → session_ended(...)
PreCompact{...}            → compaction_pending(...)
Stop{}                     → assistant_turn_ended(...)
```

Every hook becomes a tuple. The dimension registry registers the
event types; consumers query by type, by actor, by content (semantic
match on the payload), or by structural metadata.

Estimated cost: a single hook handler that's a thin adapter over
RDR-110's `out`. Maybe 100 LoC including the registry entries.

**2. Tuple → surface fulfillment, with optional direct streaming.**
When a binding fires and wants to render to a surface, the surface
descriptor (`level: {notification, status-line, pane, iframe,
full-screen}`, payload shape, optional CSS hints, optional direct
stream endpoint) is fulfilled by whatever rendering substrate the
harness has natively:

- Terminal harness with tmux → spawn or update a pane (descriptor
  becomes a tmux command).
- GUI harness with ext-apps support → emit `ui://` resource, host
  renders iframe.
- Headless / autonomous → write to log file, optionally fire OS
  notification.
- Voice-first harness → text-to-speech of a short summary.

**Static surfaces** render directly from the binding's tuple
payload. One tuple → one render. Cheap, low-latency for discrete
events.

**Streaming surfaces** render from a direct connection declared
via a manifest tuple. The binding's tuple says "this surface
subscribes to event stream E with endpoint Z." The surface opens
the direct connection to Z and consumes the stream at full
bandwidth. The tuple space tracks the manifest's lifecycle but does
not carry the stream. This is how a live-progress panel watches
a workflow execute: not by polling the tuple space, not by
receiving tuples at step-event granularity, but by holding a direct
SSE / WebSocket / pipe connection to the workflow engine's emitter
endpoint that the binding registered.

The engine doesn't care about the host; the host doesn't care about
the engine. The contract is the surface descriptor and (where
needed) a connection manifest. This is what makes the substrate
harness-agnostic without sacrificing the per-harness native
experience or sacrificing bandwidth.

Estimated cost: per-harness adapter. tmux adapter is small (~200
LoC). ext-apps adapter is the work already covered in the handed-off
workflow-engine doc. Manifest lifecycle is shared across adapters
and is small once the tuple type is defined.

**3. Bindings primitive + management panel.** Bindings are tuples.
The trigger is a tuple template (or compound query); the action is
a workflow invocation (or a tuple post, or a `SendMessage`); the
surface is a descriptor. A binding is one row in the bindings table;
the table is itself a tuple type in the tuple space.

Authoring: an ext-apps form or tmux TUI lets the user fill in
trigger/action/surface. The form is itself a workflow rendered as a
surface. Bootstraps eats its own dog food.

Management: a single panel lists all active bindings, lets the user
toggle/edit/delete. Disabled bindings are kept (in case the user
wants to reactivate). Bindings are profile-scoped — different
mission contexts surface different bindings.

Estimated cost: the primitive is a tuple type + a small CRUD path.
The management panel is itself authored as a workflow-as-app. The
authoring UX is a form; the form generator can be largely
LLM-assisted ("user describes the trigger; LLM emits the tuple
template").

## The LLM and other actors on the bus

The agent (LLM) is one specialist on the blackboard. So is the user.
So is each surface. So is each background worker. So is each
scheduled job.

A critical discipline that has to be visible: **who acted, when,
why.** The active-claims panel surfaces this. A claim is `in`-style
acquisition of a coordination tuple — "I, agent xyz, am working on
this; here's my TTL." The panel shows currently-active claims, what
each actor is doing, and how long until TTL expiry. The user can
see when an agent is about to time out; the agent can see what
peers are working on adjacent things; the user can revoke a claim
manually.

Turn-taking happens at two levels:

1. **Permission modes** as the existing lockdown discipline. The
   harness already enforces this. `plan` mode = agent proposes,
   user disposes. `acceptEdits` = agent acts freely. `auto` =
   agent runs continuously. These map directly to the WoW
   combat-lockdown analog: a posture the user picks for the
   current mission context.
2. **Claim-based coordination** as the fine-grained turn-taking. An
   agent claims a resource (or a binding's action slot, or a
   surface's render slot) before taking action; releases on
   completion. Other agents see the claim and back off. The user
   can break a claim manually. The atomic `in` makes this safe
   under concurrent agents.

Visualisation matters here. The substrate without visualisation is
just plumbing. The active-claims panel + the recent-events stream
+ the active-bindings list are the *minimum* visible surfaces that
turn the substrate into a comprehensible C2 cockpit. Without these
three panels, the user has no situational awareness.

## Cross-instance coordination

Multiple Claude Code processes on the same machine, or multiple
users in a shared project, or one user with foreground + N
background agents — all the same problem. The tuple space's T2 tier
(SQLite WAL) is multi-process safe, so the cross-process bus is
free.

Each instance posts a `liveness` tuple periodically with its PID,
project, current focus, recent activity summary. A cross-instance
panel `rd`s liveness tuples within TTL. The user sees what every
instance is doing without context-switching.

This is also where the existing `~/.config/nexus/t1_addr.<pid>`
discovery mechanism (RDR-105) gets cleaned up: it remains for T1
bootstrap, but everything else moves to T2 tuples. Cross-instance
state becomes one query.

Inter-instance messaging: post a tuple addressed to the recipient
PID or by intent ("any instance handling project X"). The recipient
`in`s it from its mailbox. The `SendMessage` user-facing tool can
remain unchanged; its transport unifies to the tuple-space mailbox.

## Workflows as a free variable

Workflows in some form will exist. The form may be Parmar-style
(JSON DSL, MCP Mediator, deterministic execution) — that's what the
handed-off doc explores. Or it may be lighter (chained tool calls
the agent re-issues), or richer (a Temporal-style code-first
runtime). The substrate this doc proposes is *agnostic* to which.
What it commits to is that:

- A binding's action slot can hold a workflow reference, a single
  tool call, a tuple post, or a `SendMessage`. The action is whatever
  the harness can dispatch.
- The workflow engine, if present, is a **peer specialist** on the
  blackboard, running in its own process and connected to nexus via
  MCP. It `in`s "run this workflow" tuples, emits step-completion
  tuples back to the space, posts the final result tuple, and
  optionally declares connection manifests for live step-progress
  streams that subscribed surfaces consume directly.
- Workflow execution does not happen in nexus. The workflow engine
  is a separate MCP server process. Nexus coordinates; the workflow
  engine executes. Intra-step dataflow (engine pulling a 10MB blob
  from a downstream MCP server and feeding it into the next step's
  input) is engine-internal, never tuple-routed.

That last point matters: **the workflow engine is just another
specialist** under this framing — and the *separation* is what
keeps nexus as a switchboard rather than a bottleneck. If a project
uses Parmar's engine for stateful workflows and direct tool calls
for everything else, the substrate accommodates both with no extra
work and no payload routing through nexus.

## Surfaces — the C2 cockpit observation

The harness already has rendering surfaces. The substrate doesn't
need a new component model; it needs a *layout layer* and a few
standard surface types.

Surface levels (from least intrusive to most):

- **Status line** — one-line ambient indicator. Maps to tmux
  status-right, terminal status bar, or a small header element in
  GUI hosts.
- **Notification** — transient, dismissible. Maps to system notify,
  terminal bell + log line, or in-chat toast.
- **Panel** — persistent surface, takes a slot. Maps to tmux pane,
  ext-apps iframe, VS Code webview, or artifact panel.
- **Full-screen** — temporary takeover for a checkpoint or wizard.
  Maps to tmux full-window mode, ext-apps fullscreen mode, or
  modal in GUI.

A *layout* is a named arrangement of slots, each slot bound to a
surface descriptor produced by a binding. Layouts are tuples,
profile-scoped. Switching layouts is `out`ting a "current_layout"
tuple; subscribed surfaces react.

Mission contexts are profiles in WoW's sense: a saved bundle of
(active layout, active bindings, permission mode, recent-events
filter). The user switches mission contexts by gesture; the LLM
can suggest a switch. The harness handles the actual surface
re-arrangement.

### Layout is automatic — the binding-type registry is the stylesheet

Hand-arranged dashboards rot. Every productivity tool that survived
(Word, IDEs, Streamdeck profiles) defaults to automatic layout with
user override as the escape hatch, not the entry point. The cockpit
follows the same discipline.

Bakke, Karger, and Miller's *Automatic Layout of Structured
Hierarchical Reports* (InfoVis 2013) is the load-bearing prior art.
Their algorithm composes three layout idioms (nested table,
multi-column form, outline-style indented list) into hybrid layouts
that fit a horizontally-constrained viewport by making **layout
decisions per schema node, applied uniformly to every instance of
that node**. Gestalt across siblings is what makes their reports
legible at a glance. Our surface-level palette (`status-line`,
`notification`, `panel`, `full-screen`) is the direct analog of
their idiom palette; the same discipline applies — **pick a surface
level per binding type, not per event instance**. Every
`tool_call_completed` event renders the same way; every
`claim_acquired` renders the same way; the user learns the cockpit
*once*.

The stylesheet sits on the schema, not on the data. In our terms
that is **the RDR-110 dimension registry**, extended to carry layout
disposition for each registered semantic-event type. When a new
event type is registered, its default layout properties go on the
registry entry; per-binding overrides ride on the binding tuple.
Individual event tuples never carry layout properties — they carry
payload, and the registry tells the layout engine how to render
that payload-shape.

### Measure → Auto-Style → Layout, online

Bakke's three-phase algorithm maps cleanly, then needs three
extensions for the dynamic case:

- **Measure.** Traverse the active binding set. For each binding,
  read off the registry: event type, typical event rate, payload
  size profile, priority class, time-decay profile. For surfaces
  already on-screen, measure observed render width / history depth.
- **Auto-Style.** Heuristics over the binding set produce a
  stylesheet — for each binding, which surface level it gets, which
  slot allocation, which iconography, which on-screen TTL. The
  heuristic respects the display budget (see below) and the active
  mission-context profile. The stylesheet is a tuple, materialised
  per mission context, regenerated whenever the binding set or the
  display budget changes.
- **Layout.** Apply the stylesheet to actual events: render the
  status line from recent low-priority events, populate panels from
  their bound queries, fire notifications from priority-tagged
  events, raise full-screen modes for checkpoint/wizard events.

The static-report assumptions in Bakke do not survive contact with a
live event stream. Three extensions are mandatory:

- **Online re-layout.** The Auto-Style phase is incremental, not
  one-shot. Binding registrations, mission-context switches, and
  display-budget changes (window resize, pane split, secondary
  display attached) trigger a stylesheet regeneration. The
  regeneration runs on the same totally-ordered T2 log as everything
  else — it is one or two tuple operations, not a re-render of the
  world.
- **Temporal decay.** Bakke renders all data; the cockpit cannot.
  Each event type carries a decay profile on its registry entry
  (immediate-fade, last-N-on-status, persistent-until-acked,
  persistent-until-superseded). The status line shows the last N
  decayed events; the recent-events panel shows the last M
  un-acked events; old tuples release their slot back to the layout
  engine.
- **Priority preemption.** A `priority=critical` event displaces a
  lower-priority event already occupying its target surface level.
  Bakke's algorithm has no notion of "this content must displace
  lower-priority content"; the cockpit does. Preemption is a
  property on the registry entry (`preempts: [...]`), enforced by
  the layout engine when it allocates slots.

### Horizontal-budget cascade — the demotion rule

Bakke constrains horizontal width and refuses to scroll
horizontally; everything cascades from there. Cockpit equivalent:
the display budget (terminal pane width, monitor width, mobile
width, voice-only audio "bandwidth") is fixed; vertical history
scrolls; horizontal does not.

When the budget shrinks (window resize, pane split, mission-context
switch into a denser layout), the layout engine applies a
**surface-level demotion cascade** to each binding, in priority
order:

```
full-screen  →  panel  →  status-line  →  notification-only  →  suppressed
```

A binding with a `panel` disposition that no longer fits gets
demoted to `status-line`; if the status line is full, it falls
through to `notification-only` (events fire as transient
notifications without occupying a slot); if even notifications would
overwhelm, the binding is suppressed and a single
`suppressed-bindings` indicator lights up on the status line.
Per-binding minimums on the registry entry (`min-surface:
status-line`) prevent critical bindings from being demoted past a
floor.

The user's manual override path stays available: any binding can be
pinned to a specific surface level, in which case the cascade
respects the pin and demotes other bindings around it. Manual
layout authoring is the escape hatch; auto-layout from the binding
set is the default.

The cockpit metaphor is exact: cockpits have instruments (surfaces
reading state), controls (surfaces issuing commands), modes (lockdown
discipline), and checklists (workflows). The substrate provides each;
the layout engine arranges them from the active bindings and the
display budget; the user adjusts when the automatic arrangement
needs nudging for the mission being flown.

## What we build vs what we leverage

The lever distinction matters because the cost picture changes
radically when we count properly.

**Build (small, novel):**
- Hook → tuple bridge (~100 LoC).
- Bindings primitive (~150 LoC: tuple type + CRUD path).
- Auto-layout engine — Measure / Auto-Style / Layout phases with
  online re-layout, temporal decay, priority preemption, and the
  horizontal-budget demotion cascade. Built on top of the dimension
  registry; per-harness adapters render the resulting slot
  assignments. Layout disposition fields on registry entries are
  ~50 LoC of registry plumbing; the engine itself is ~300 LoC of
  heuristics plus per-harness adapter (~200 LoC tmux, more for GUI
  hosts).
- Active-claims panel (workflow-as-app on top of the substrate).
- Active-bindings management panel (workflow-as-app).
- Recent-events panel (workflow-as-app).
- tmux fulfillment adapter (~200 LoC).
- Cross-instance liveness tuple + `nx instances` (~50 LoC).
- LLM-as-specialist turn-taking discipline (mostly documentation +
  surfacing existing permission modes correctly).

**Leverage (already in nexus):**
- RDR-110 tuple space (in flight, accepted, load-bearing).
- T1/T2/T3 tier discipline.
- Plan library + plan_match.
- nx CLI + skill ecosystem.
- Beads for cross-session work tracking.
- Catalog tumblers for content-addressed identity.

**Leverage (already in the harness):**
- Hook bus (every hook event type listed above).
- Background process supervision.
- Inter-agent SendMessage.
- Scheduled wakeups and cron.
- Worktrees for isolation.
- Permission modes for lockdown.
- MCP server set as capability inventory.
- Plugin distribution.
- tmux (terminal harness) — entire layout and multiplexing substrate.

**Leverage (external prior art — patterns, not code):**
- Linda's `out`/`in`/`rd`/`eval` semantics.
- Blackboard architecture's specialist composition.
- WeakAuras' trigger/display/action triple as the binding DSL shape.
- Portal frameworks' lessons on IPC-versus-shared-state.
- SCADA's tag-based data model.
- LabVIEW's data-flow composition (architecture only; not the wiring UX).

**Not building, intentionally:**
- A new component / widget framework. The harness's native surfaces
  are fine.
- A new identity / auth layer. The harness owns identity.
- A new persistence engine. RDR-110 tier model is sufficient.
- A new IPC mechanism. SendMessage + tuple mailboxes are sufficient.
- A wiring-style visual programming UX. Form-based binding authoring
  is closer to what's been proven learnable.

## The open questions (intentionally)

These are *kept open*, not deferred:

- **Where workflows fit.** Some bindings will want a multi-step
  workflow as their action; others will want a one-shot tool call.
  The substrate accepts both. Whether a project ships a workflow
  engine as a peer MCP server, or inlines workflow-like logic in
  the binding itself, or delegates to an external runtime — all
  three are substrate-compatible. Don't commit yet.
- **Binding authoring UX.** Form-based is the default. LLM-assisted
  binding emission is natural. Visual wiring may eventually be
  useful for advanced users but is not the starting point.
- **Surface descriptor format.** Need to settle on a small, stable
  shape (level + payload + optional layout hints). Earliest version
  can be a JSON Schema; mature version maps to A2UI or similar
  declarative component intent.
- **Multi-user / collaborative shape.** Shared projects in Claude
  Code already exist; the tuple-space-shared scope is the obvious
  bridge, but the access-control and conflict-resolution stories
  for multi-user editing of bindings / layouts / claims need
  spec work.
- **The voice-first / autonomous-headless cases.** The substrate
  works without a display, but the binding/management UX needs
  alternatives. Probably out of scope for early iterations.

## Sketched ordering — what to do first

Dependencies aside, the minimum-viable cockpit substrate ships in
roughly this order:

1. **RDR-110 implementation lands.** Tuple space common model with
   `in` / `out` / `rd` / `eval` and the dimension registry. This is
   the foundational primitive everything else hangs from.
2. **Hook → tuple bridge.** Trivial to implement. Makes the
   harness's existing event bus observable as data. Without this,
   nothing in the substrate is composable.
3. **Bindings primitive + management panel.** One tuple type, one
   CRUD path, one workflow-as-app for management. Enables
   user-authored composition.
4. **Active-claims and recent-events panels.** Visualisation of the
   substrate itself. Turns plumbing into a comprehensible cockpit.
5. **tmux fulfillment adapter.** Lights up multi-pane situational
   awareness for terminal users. Leverages the existing terminal
   composition substrate.
6. **Cross-instance liveness tuple.** Solves "what's running"
   across multiple Claude Code processes. Cheap.
7. **First end-to-end mission profile.** Pick a real workflow Hal
   does often (RDR creation, incident response, build watching).
   Author the bindings, the layout, the lockdown mode. Run it.
   Measure what's awkward; iterate.

Each step is bounded, builds on the prior, and leans on existing
substrate rather than inventing.

## Reframe

The previous design conversation centered on a workflow engine
extended with ext-apps. That doc is now shipped and handed off.

This document centers on a different thing: **nexus as the
coordinator and switchboard for an agentic cockpit. Tuple space as
the control plane; direct peer-to-peer connections, brokered by
manifest tuples, as the data plane. The harness's existing hook bus
projected into the tuple space as semantic events. Workflows as one
of many possible specialists, running in their own processes.**

Nexus does not sit in the data path. It brokers, declares, claims,
and tears down. The actual bandwidth flows where it needs to —
between agents, between agent and surface, between engine and
downstream MCP servers — over whatever direct transport fits.
Manifests make every direct connection visible to the control plane
without making the control plane responsible for the payload.

The substrate is small. The leverage is enormous. Most of the work
is naming what already exists and wiring it together; the
genuinely-new code budget is in the low hundreds of LoC. The hard
part is the discipline — making sure bindings, layouts, claims,
manifests, and turn-taking are visible and comprehensible to both
the user and the LLM, and that the line between control and data is
respected so nexus never becomes a bottleneck.

The metaphor that fits is the WoW addon ecosystem combined with
SIP-style signaling: blackboard coordination at the substrate, real
data paths between peers, both visible to a runtime that brokers but
does not interpose.

Workflow is a free variable in this design. Tuple space (as
switchboard) and semantic events (as integration projection) are
the load-bearing commitments. Connection manifests are the
discipline that keeps the switchboard a switchboard.
