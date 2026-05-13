---
title: "The ORB: Observable Relay Bus — Hook Projection, Bindings, and C2 Cockpit Substrate"
id: RDR-111
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-11
related_issues: []
related_rdrs: [RDR-105, RDR-110, RDR-112, RDR-113]
related_tests: []
implementation_notes: ""
---

# RDR-111: The ORB: Observable Relay Bus — Hook Projection, Bindings, and C2 Cockpit Substrate

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The agentic harness ships a remarkably complete coordination
substrate — hooks at every lifecycle point (`PreToolUse`,
`PostToolUse`, `Stop`, `SubagentStop`, `SessionStart`, `SessionEnd`,
`UserPromptSubmit`, `PreCompact`, `Notification`), background
supervision, inter-agent messaging, scheduled wakeups, worktree
isolation, permission modes, and MCP server composition. But these
capabilities are **opaque by default**. An agent runs; things happen;
the user has no situational awareness unless they inspect tool output
line by line. The hook bus fires but its events vanish. Coordination
state — which agent holds what resource, what tasks are in flight,
what just completed — is invisible unless each agent individually
surfaces it, in ad-hoc ways, with no shared model.

Three concrete gaps:

### Gap 1: Hook events are fire-and-forget, not observable state

`PreToolUse` and its siblings fire a shell script or nothing.
There is no mechanism to observe these events as data — to query
them, subscribe to them, aggregate them, or react to them
declaratively. The hook bus is the right shape for an event
backbone; it is wired to exactly one consumer (the script) and
exposes nothing to other actors or surfaces.

### Gap 2: No user-authored composition layer

There is no way for a user — or an LLM acting on the user's behalf
— to say "when event X happens, do Y and show Z." Reactions to
events are hard-coded in hooks or in agent prompts. The composition
is in code, not in data. Adding a new reaction requires a code
change. Removing one requires finding it. There is no management
surface, no toggle, no profile.

### Gap 3: No situational awareness surface

There is no panel, status line, or ambient display that answers
"what is running right now, what does it hold, what just happened."
The user context-switches between terminal windows, reads raw
output, and loses track of what each background agent is doing.
The cockpit metaphor is exactly right: this is a cockpit with no
instruments.

The ORB addresses all three gaps with minimal new mechanism,
leveraging the semantic tuple space (RDR-110) as the backbone.

## Context

### The two architectural moves (from `docs/agentic-cockpit.md`)

The design exploration in `docs/agentic-cockpit.md` identifies two
load-bearing moves that turn the latent substrate into a C2 cockpit:

**Move 1** — The RDR-110 semantic tuple space is the common
coordination model. Everything that wants to coordinate uses it as a
switchboard, not a bandwidth medium. Tuples describe state, declare
connections, claim resources, and post milestones. Actual payload
flows on direct peer-to-peer connections brokered by manifest tuples.

**Move 2** — Hook events are projected into the tuple space as
semantically-typed tuples (semantic events), making them composable,
queryable, and addressable by intent. These are milestones —
`PreToolUse` fires once per tool call, producing one tuple, not one
per stdout byte. Granularity is the discipline.

The ORB is the implementation of Move 2, built on the foundation of
Move 1 (RDR-110). Three independently shippable pieces:

1. **Hook → tuple bridge.** A hook adapter that projects each harness
   lifecycle event into the tuple space as a typed semantic event.
2. **Bindings primitive.** A tuple type that encodes a declarative
   trigger/action/surface triple, plus CRUD management.
3. **C2 cockpit surfaces.** Three minimal panels that make the
   substrate visible: active claims, recent events, active bindings.

### Prior art grounding

- **Linda tuple spaces** (Gelernter & Carriero, 1986): `out`/`in`/`rd`
  as the coordination primitives. RDR-110 provides these (as `out`/
  `take`/`read`); the ORB uses `out` for event projection and `read`
  for subscription.
- **Blackboard architectures** (Hayes-Roth et al., Hearsay-II, 1970s):
  specialists post hypotheses to a shared knowledge base; others
  refine. Each actor in the system — LLM, user, surface, background
  worker — is a specialist on the blackboard.
- **WeakAuras** (WoW addons, 2004–): the richest consumer-grade example
  of cooperating active surfaces over shared world-state. A
  declarative trigger/display/action triple, user-authored without
  code. The existence proof that this pattern is learnable.
- **Bakke, Karger & Miller** (InfoVis 2013): automatic layout of
  structured hierarchical reports via schema-driven hybrid layouts.
  The binding-type registry is the stylesheet; layout decisions are
  per schema node, applied uniformly to every instance.
- **SCADA / DCS / mission control** (1960s–): tag-based data models,
  alarm systems, runbook integration. The dashboards that don't suck.
- **SIP/WebRTC signaling**: the connection manifest pattern —
  signaling brokers connections without carrying the payload. Manifest
  tuples declare direct peer-to-peer streams; nexus tracks the
  lifecycle without seeing the data.

### Relationship to RDR-110

RDR-110 ships the tuple space primitive: `out`/`read`/`take`/`ack`/
`nack`, the claim ledger, the subspace registry, five canonical
subspaces, and the four-consumer landing surface. The ORB extends
the subspace registry with seven new subspace types (semantic event
types from the hook bus), adds the `bindings`, `layout_state`, and
`connection_manifest` subspaces. The ORB's surfaces consume the tuple
space via `read` queries; they do not require new storage.

RDR-111 is **blocked on RDR-110 Phase 1 Step 4** (core API + MCP
tools). The hook bridge needs `out`; the cockpit panels need `read`.

**API name mapping** (Linda academic terms → RDR-110 concrete API):

| Linda | RDR-110 | Notes |
|-------|---------|-------|
| `out` | `out` | identical |
| `rd` / read-without-consuming | `read` | pattern-match query; tuple stays |
| `in` / read-and-consume | `take` | atomic claim + consume |

Linda terminology (`rd`, `in`) is used in prior-art and conceptual
sections of this RDR. Implementation-facing sections use the RDR-110
names (`read`, `take`).

**RDR-110 coordination required (CA-9)**: The ORB's seven hook-event
subspaces and `bindings/<profile>` subspace must be registered in
RDR-110's subspace registry (the YAML subspace registry defined in
RDR-110 Phase 1 Step 6). This RDR adds entries to that registry; the
RDR-110 implementation must expose a registration mechanism for
third-party subspaces (i.e., subspaces not defined in RDR-110 itself).
This is a **definitive prerequisite** (CA-9), not a conditional: the
RDR-110 implementor must be notified before Phase 1 Step 6 ships so
that open registration is included in scope. Without this, the ORB's
Phase 1 Step 1 (hook-event subspace schema registration) cannot proceed.

### Relationship to RDR-112

RDR-112 ships the storage-as-service daemons that own `tuples.db`,
`memory.db`, the T3 chroma store, and CatalogDB. Under RDR-112 the
MCP server is a **client** of the daemons, not the daemon itself —
the ORB's `_BindingWatcher` and cockpit surface code all run in the
client process and reach storage exclusively through daemon RPCs.

Specific RDR-112 surfaces RDR-111 consumes:

- **`EventStream(subspace_prefix, since_cursor)`** streaming RPC
  (RDR-112 Approach §7) — `_BindingWatcher` consumes
  `tuples/<subspace>` events instead of the direct-SQL `rowid > N`
  query the pre-rework draft used. The `rowid` cursor protocol is
  preserved as the wire-level cursor; only the transport changes.
- **Failure-category demux** on the event stream (`category ∈
  {data, schema, substrate}`, per RDR-112 §7) — `_BindingWatcher`'s
  failure-isolation logic depends on this to avoid auto-disabling
  user bindings when the daemon hiccups (see §Step 6 Watcher
  failure isolation).
- **Subspace registry validation is daemon-side** (RDR-112 Approach
  §8) — the ORB-supplied subspaces (per CA-9) land via
  `nx daemon t2 subspace add <yaml>` rather than client-side
  runtime registration.
- **Migration ownership is daemon-side** (RDR-112 Approach §9) —
  the `watcher_state` table and the `action_idempotency` table are
  applied by the daemon at startup, not by the MCP client (CA-10
  language adjusted accordingly in the gate revision).
- **Cockpit panel direct-SQL queries** (Steps 8 + 9 + the
  active-claims and recent-events surfaces) — under RDR-112 these
  re-route through daemon read RPCs (or the introspection surface
  `nx daemon t2 exec --raw` for ad-hoc analytical queries; see
  RDR-112 §Nexus-in-its-own-container). The query shapes are
  unchanged; only the substrate boundary moves.
- **Host-trust model** is inherited from RDR-113 (UDS chmod, peer
  credentials, single-user v1). Cockpit projections respect the
  same trust boundary as every other daemon client — only the
  daemon-owner UID sees other-agent tuples and mailbox traffic.

RDR-111 is **blocked on RDR-112 Phase 1** (T2 daemon with
EventStream RPC) for the binding-watcher pieces. The cockpit
surfaces that only query (not subscribe) can ship earlier against
the daemon's read RPCs.

## Research Findings

### Investigation

Structural analysis of the harness hook contract, the RDR-110 API
surface, and the `docs/agentic-cockpit.md` design exploration. The
findings below are verified against those sources.

### Key Discoveries

- **Verified** — The harness hook contract is stable and machine-
  readable. Each hook fires with a well-defined JSON payload; the
  mapping to a semantic event tuple is mechanical and ~100 LoC.
  The hook adapter is additive — no existing hook handler changes.
- **Verified** — A binding is a tuple type: trigger template (a
  subspace query + optional structural filters), action descriptor
  (tool call, tuple post, `SendMessage`, or workflow ref), surface
  descriptor (level + payload shape + optional stream endpoint).
  Bindings are managed via `out`/`take` on a `bindings/<profile>`
  subspace. No new storage required.
- **Verified** — The three minimum cockpit panels (active claims,
  recent events, active bindings) are each a single `read` query over
  the tuple space with a light formatting layer. No new primitives.
  A tmux pane or ext-apps iframe rendering the query result is the
  fulfillment layer.
- **Verified** — Control plane / data plane separation is the load-
  bearing discipline. Tuples describe and broker connections; they do
  not carry payload. A `connection_manifest` tuple declares a direct
  peer-to-peer stream (SSE, WebSocket, pipe, file-fd); the actual
  bandwidth flows between peers without touching nexus. This keeps
  nexus as a switchboard, never a bottleneck.
- **Verified** — The auto-layout engine (Bakke's Measure /
  Auto-Style / Layout pipeline, adapted for live event streams) sits
  on the binding-type registry. Layout disposition is a property of
  the registered event type, not of individual event tuples. Online
  re-layout, temporal decay, priority preemption, and the
  horizontal-budget demotion cascade are the four extensions the
  static-report algorithm needs for the dynamic case.
- **Revised** — Cross-instance liveness must use **T2 (SQLite),
  not the tuple space**. The tuple space is scoped to a single
  `nx-mcp` process (T1 or the process's local SQLite shard); a
  tuple posted by one process is not visible to another process's
  `read` query unless the tuple space itself has a cross-process
  backend (which RDR-110 does not provide in Phase 1). Per RDR-105,
  T2 (SQLite WAL mode) is the defined cross-process shared bus.
  The correct implementation: a `liveness` table in the T2 SQLite
  database with `(pid, machine, user, session, project,
  focus, activity_summary, last_seen)`, upserted every 30s by
  each `nx-mcp` process and swept by a background thread for rows
  where `last_seen < now - 60s`. `nx instances` queries this
  T2 table directly. This adds ~80 LoC (T2 schema migration +
  upsert + sweep + CLI command), slightly more than the 50 LoC
  originally estimated for the tuple-space approach.

### Research Finding RF-1: Hook payload shapes — verified inventory

Sourced from `nx/hooks/scripts/`, `tests/hooks/`, and the LangSmith Claude Agent SDK
integration (`_hooks.py`). All fields below are verified against production code or
test fixtures. Fields marked `[inferred]` are from community documentation and GitHub
issues; no production hook in this repo reads them yet.

**Common base fields** (present in all or most hook types):
`session_id`, `hook_event_name`, `transcript_path` (path to JSONL), `cwd`

| Hook type | Additional stdin fields | Output contract |
|---|---|---|
| `PreToolUse` | `tool_name`, `tool_input` (object with tool args, e.g. `command`, `file_path`) | **Required**: `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"\|"block", "additionalContext": "..."}}` — malformed output blocks tool calls |
| `PostToolUse` | `tool_name`, `tool_input`, `tool_response` (tool output, any JSON type) | `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "permissionDecision": "allow", "additionalContext": "..."}}` — advisory, never blocks |
| `PermissionRequest` | `tool_name` | **Required**: `{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}` — malformed output blocks tool |
| `Stop` | `stop_hook_active` (bool) | `{"decision": "approve", "reason": "..."}` — top-level `decision`, NOT `hookSpecificOutput` |
| `StopFailure` | `error` (type string: rate_limit \| authentication_failed \| billing_error \| invalid_request \| server_error \| max_output_tokens \| unknown), `error_details` (string), `last_assistant_message` | Output **ignored** by harness; side-effects only |
| `SubagentStart` | `task` (description), `prompt` (agent prompt text) | `{"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": "..."}}` — injected into subagent context |
| `SubagentStop` | `stop_hook_active` (bool), `permission_mode` [inferred] | `{"hookSpecificOutput": {"decision": {"behavior": "allow"\|"stop"}}}` [inferred] |
| `SessionStart` | `claude_session_id` (UUID string) | Raw text/markdown — injected into Claude's context window |
| `SessionEnd` | (no type-specific fields beyond base) | Output ignored; side-effects only |
| `PostCompact` | `trigger` ("manual"\|"auto"), `compact_summary` (text), `permission_mode` | Raw text/markdown — injected into Claude's context window |
| `PreCompact` | `trigger` ("manual"\|"auto"), `compact_summary`, `estimated_tokens`, `token_limit` [inferred] | Output ignored (`hooks.json` has empty `[]` for this type — not currently used) |
| `UserPromptSubmit` | `prompt` (the user's message text) [inferred] | `{"hookSpecificOutput": {"decision": {"behavior": "allow"\|"stop"}}}` [inferred] |
| `Notification` | `message`, `notification_type` [inferred] | Output ignored (informational only) |

**Critical output divergence** (`Stop` vs `PreToolUse`): `Stop` uses top-level `{"decision":
"approve"}` while `PreToolUse` uses `{"hookSpecificOutput": {"permissionDecision":
"allow"}}`. These are **not interchangeable**. Each per-hook-type bridge script must
emit exactly the contract for its hook type.

**Gap note**: `SubagentStop`, `UserPromptSubmit`, `PreCompact`, and `Notification` payload
shapes are partially inferred from community docs and GitHub issues (no production script
in this repo reads them). CA-6 below gates on empirical verification before the bridge
scripts for these types are written.

**Implication for CA-2**: The hook payload shape is clear and already depended upon by
multiple production hook scripts for the 9 verified types. CA-2 is substantially
de-risked for those types; the bridge mapping table can be written today for them.

### Research Finding RF-2: Hook output semantics vary by type — bridge must be transparent

This is a critical design constraint not surfaced in the original draft.

Hook scripts that respond with `hookSpecificOutput` JSON **affect Claude's behaviour**:

- `PreToolUse` response: `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"|"block", "additionalContext": "..."}}`
  — if the bridge outputs malformed JSON here, tool calls are blocked.
- `PermissionRequest` response: same shape with `permissionDecision`.
- `SubagentStart` response: `{"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": "..."}}` — injects content into the subagent's initial context.
- `Stop` / `StopFailure`: output and exit codes are **ignored** by the harness.
- `SessionStart` / `SessionEnd`: output is injected as context; exit code matters.

**Design correction**: The bridge cannot simply write to stdout for all hook types.
For hooks with decision semantics (`PreToolUse`, `PermissionRequest`), the bridge must
emit a permissive `hookSpecificOutput` JSON (pass-through allow) while posting the
tuple as a side effect. For `SubagentStart`, it must either emit no stdout (to avoid
interfering with the context-injection chain) or be registered *before* the existing
hook so the existing hook's `additionalContext` arrives last. The safest shape:

```python
# For PreToolUse / PermissionRequest:
import json, sys
payload = json.loads(sys.stdin.read())
out_to_tuple_space(payload)   # side effect only; errors caught and logged
# emit transparent allow — never block on bridge failure
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": payload.get("hookEventName", "PreToolUse"),
    "permissionDecision": "allow"
}}))
sys.exit(0)

# For Stop / StopFailure / SessionEnd:
out_to_tuple_space(payload)
sys.exit(0)  # no stdout needed
```

This constraint affects the bridge architecture: it is **not** a single handler
for all hook types, but a **family of thin per-hook-type scripts**, each with the
correct output contract, all sharing the `out` call.

### Research Finding RF-3: Hook chaining is already in use — bridge is additive

`nx/hooks/hooks.json` shows multiple hooks registered per event type (e.g.,
`SessionStart` chains 6 commands). The bridge adds one more entry per type.
Hook chaining has no ordering guarantee across entries at the same level, but
output from all entries is collected. **The bridge must never be the last
`PreToolUse` handler that determines the allow/block decision** — register it
first in the chain, emit `allow`, and let the existing decision hooks follow.

### Research Finding RF-4: No existing tmux surface integration — genuinely new territory

Searching the codebase (`find + grep tmux`) shows tmux references only in:
- E2E test runner scripts (`tests/e2e/*.sh`) — use tmux to drive test scenarios
- Documentation (`docs/agentic-cockpit.md`, `rdr-111`) — design only

There is no existing `status-right` update path, no pane management code, no
tmux IPC in any Python source. The tmux fulfillment adapter is net-new code with
no prior art to reuse within the repo. CA-4 (tmux refresh cadence) remains
spike-gated and unverified.

### Research Finding RF-5: `CLAUDECODE=1` env var gates production side-effects

Existing hooks (`stop_failure_hook.py`) skip side-effects unless `CLAUDECODE=1`
is set. The bridge should follow the same pattern: if `CLAUDECODE` is not set,
skip the `out` call and exit 0 silently. This makes the bridge safe to invoke
in test harnesses, linting, and local dev without polluting the tuple space.

### Critical Assumptions

| # | Assumption | Status | How to verify | Risk if wrong | Phase gate |
|---|---|---|---|---|---|
| CA-1 | RDR-110 Phase 1 Step 4 ships before this RDR begins implementation | Open | RDR-110 status check | Blocked; cannot proceed | P1 |
| CA-2 | Hook payload JSON for the 9 verified hook types is stable across harness versions | **De-risked (RF-1)** | Schema validated against production scripts; 4 types (`SubagentStop`, `UserPromptSubmit`, `PreCompact`, `Notification`) still require empirical verification (CA-6) | Low for verified types; medium for inferred types | P1 |
| CA-3 | `read` query latency over T2 is < 50ms at 10k tuples | Open | Benchmark after RDR-110 ships | Panel refresh sluggish; may need materialised view | **P2 gate** |
| CA-4 | tmux `status-right` update at 1-2s cadence has no perceptible lag | Open | Manual test on target hardware | Increase to 5s cadence + stale indicator | **P3 gate** |
| CA-5 | Binding reaction end-to-end < 200ms at 10 concurrent active bindings | Open | Benchmark after bridge + watcher ship | Move watcher to dedicated process | **P2 gate** |
| CA-6 | `SubagentStop`, `UserPromptSubmit`, `PreCompact`, `Notification` payload shapes match inferred schemas | Open — **spike required before bridge scripts for these types are written** | Register a minimal logging hook for each type; capture real payload from a live session; compare against RF-1 table. Spike ≤ 2h. | Bridge scripts emit wrong field names → tuples have missing/null dimensions | P1 (bridge for these 4 types) |
| CA-7 | The Bakke Measure/Auto-Style/Layout adaptation is ≤ 300 LoC and can be implemented by a single developer in < 1 week | Open — **novel work, no prior art in repo** | Prototype the three pipeline phases on a static binding set before committing to Phase 3 | Layout engine becomes the long pole; delays Phase 3 by 2–4 weeks | **P2 gate** |
| CA-8 | Multi-`PreToolUse`-hook ordering: when two hooks are registered, the first-registered "allow" does not preempt subsequent hooks' ability to "block" | Open — **empirically unverified** | Run `tests/cc-validation/scenarios/` harness with two PreToolUse hooks: hook-A returns `allow`, hook-B returns `block`; observe which wins. See scenario 07 for PermissionRequest precedent. **Fallback design if first-wins confirmed**: the bridge cannot be registered first as an unconditional `allow` emitter without neutralising legitimate block hooks. Fallback: bridge registers as a *last* entry using a separate `PostToolUse`-adjacent hook that fires after the decision hooks have run, or bridge emits no `permissionDecision` key at all (only `additionalContext`) and relies on the harness's default allow. Fallback design must be documented before Phase 2. | Bridge registered first could silently neutralise a legitimate block hook; or a block hook registered first could stall the bridge chain | P1 (bridge registration ordering) |
| CA-9 | RDR-110's subspace registry exposes a registration mechanism for third-party subspaces not defined in the canonical five | **Definitive prerequisite — must be flagged before RDR-110 Phase 1 Step 6 ships** | Coordinate with RDR-110 implementor: confirm that Step 6 ships with open registration (additional YAML dirs discoverable at startup), not a closed allow-list. If closed, an extension mechanism must be added to RDR-110 before the ORB's Phase 1 Step 1 can proceed. | Without this, the seven hook-event subspaces and `bindings/<profile>` cannot be registered; the tuple space bridge is blocked | P1 |
| CA-10 | RDR-110's `tuples.db` migration includes the `watcher_state` table (see schema in Phase 2 Step 6) | **Coordination required before Phase 2 Step 6** | Notify RDR-110 implementor before the `tuples.db` migration is finalized; confirm `watcher_state` is included. Gate criterion: confirmed presence of `watcher_state` CREATE TABLE in the tuples.db migration file. If RDR-110 ships without it, the ORB must ship a tuples.db upgrade migration (with schema-version guard) before Phase 2. | Phase 2 Step 6 fails at first run with "no such table: watcher_state" | P2 |

## Proposed Solution

### The seven semantic event subspaces

Seven new subspace YAML files in `nx/tuplespace/builtin/hooks/`:

```
hook_events/tool_call_intent      ← PreToolUse
hook_events/tool_call_completed   ← PostToolUse
hook_events/agent_completed       ← SubagentStop
hook_events/assistant_turn_ended  ← Stop, StopFailure
hook_events/user_prompt           ← UserPromptSubmit
hook_events/session_lifecycle     ← SessionStart, SessionEnd
hook_events/notification          ← Notification
```

Note: `PostCompact` is intentionally excluded — it fires after context is
rebuilt and injecting a tuple at that point would be redundant (the compact
summary is already injected into Claude's context window by the hook output).
`PreCompact` is also excluded — its output is currently unused (empty `[]` in
`hooks.json`). If either is needed in future, a new subspace can be added.
`StopFailure` is merged into `assistant_turn_ended` (error variant) rather
than given its own subspace, since both represent a session turn boundary.

Each subspace schema carries:
- Required dimensions: `actor`, `session`, `project`, `timestamp`
- Optional dimensions: `tool`, `workflow`, `intent`, `priority`
- `match_text` field: a short semantic description of the event
  (e.g. "deployed frontend via k8s.apply") for associative query
- Layout disposition: default surface level, decay profile,
  priority class, `preempts` list

### The hook → tuple bridge

**Revised per RF-2**: not a single handler but a family of thin per-hook-type
scripts in `nx/hooks/scripts/orb_bridge_*.py`, all sharing a common
`src/nexus/cockpit/hook_bridge.py` library for the `out` call and
mapping table.

Each script:
1. Reads hook payload JSON from stdin
2. Maps to the appropriate subspace + dimensions + `match_text`
3. Calls `out` on the tuple space (skipped if `CLAUDECODE` not set — RF-5)
4. Emits the correct stdout for that hook type (RF-2):
   - `PreToolUse` / `PermissionRequest`: emit transparent `allow` JSON
   - `SubagentStart`: emit no stdout (bridge registered first; existing hook follows)
   - `Stop` / `StopFailure` / `SessionEnd`: emit nothing
5. Exits 0 unconditionally — bridge errors are logged to stderr, never propagated

Registered in `nx/hooks/hooks.json` as the **first** entry for each hook type
(before existing decision hooks) — **provisional pending CA-8 spike**. If the
CA-8 spike (now gated before Step 2 — see Step 7a below) confirms first-wins
semantics (i.e., the first hook's `allow` prevents subsequent hooks from
blocking), registration must change to **last-in-chain** or the bridge must emit
no `permissionDecision` key (only `additionalContext`). Step 2 implementation
must use a feature flag or configuration entry for registration order so the
CA-8 outcome can be applied without a code change. Default before CA-8 result:
register first.

Estimated: ~150 LoC in the shared library + ~20 LoC per hook-type script (7 types
= ~140 LoC scripts). Total ~290 LoC. Larger than originally estimated due to
per-type output contract requirements.

### The bindings primitive

A new subspace `bindings/<profile>` with schema:

```yaml
bindings:
  dimensions:
    - name: profile        # mission context name
    - name: enabled        # true/false; default false during async creation
    - name: status         # pending | active | failed (lifecycle for async binding_create)
    - name: task_id        # UUID issued at binding_create time; used to poll creation progress
    - name: priority       # integer, for demotion cascade ordering
    - name: min_surface    # floor for demotion cascade
  match_text_field: description
  content_fields:
    - trigger_subspace     # which subspace(s) to watch
    - trigger_filters      # structural filter on the event tuple
    - trigger_similarity   # optional semantic query for associative match
    - action_type          # tool_call | tuple_out | send_message | workflow
    - action_descriptor    # action-type-specific payload (must include idempotency_key)
    - surface_level        # status-line | notification | panel | full-screen
    - surface_payload      # how to render the event
    - surface_stream       # optional: declare a connection_manifest instead
```

`task_id` is a registered dimension (not an MCP-layer side channel) so that callers can filter `binding_list(where={"task_id": "<uuid>"})` to check creation progress. `status` transitions: `pending` (background task running) → `active` (binding live, `_BindingWatcher` will match it) → `failed` (NLP parse or `out` call failed; error in `action_descriptor`). `enabled` starts `false` during async creation and is set to `true` atomically with the `pending → active` transition.

CRUD operations: `out` to create/update, `take` to delete, `read` to list.
A binding is enabled by setting `enabled=true`; disabled bindings are
kept (tombstoned per RDR-106 pattern) rather than deleted.

### The connection manifest subspace

A new subspace `connection_manifest` for declaring direct peer-to-peer
streams:

```yaml
connection_manifest:
  dimensions:
    - name: producer       # actor-id
    - name: consumer       # actor-id or "any-subscriber"
    - name: conn_type      # stream | pipe | websocket | sse | file-fd
    - name: payload_type   # semantic type of the stream content
    - name: ttl_seconds    # heartbeat interval; expired = teardown
  content_fields:
    - endpoint             # transport-specific address
```

Producer posts manifest after endpoint is ready. Consumer reads and
connects directly. Heartbeats refresh the tuple's TTL via `out` with
the same ID (idempotent). `take` or TTL expiry signals teardown.

### The layout_state subspace

A new subspace `layout_state/<profile>` for the auto-layout engine's
stylesheet output. One tuple per binding-type (keyed by `event_type`
and `profile`), written by the layout engine and read by all cockpit
surfaces to know which slot and level to use for each event type.

```yaml
layout_state:
  dimensions:
    - name: profile        # mission context name
    - name: event_type     # subspace path, e.g. hook_events/tool_call_intent
  content_fields:
    - surface_level        # status-line | notification | panel | full-screen
    - demotion_level       # current effective level after cascade
    - ttl_seconds          # on-screen display TTL for events of this type
    - pinned               # bool; true = user override, skip auto-layout
```

The layout engine writes one `out` per event_type per profile per debounce
window (250ms). Cockpit surfaces call `read(subspace="layout_state/<profile>")`
to get the current stylesheet before rendering. Schema ships in Phase 1
Step 1 alongside the hook-event subspace schemas (even though Step 11
is Phase 3) so producers can register and write to it without waiting for
the layout engine implementation.

### The three minimum cockpit surfaces

**Active-claims panel** — direct SQL query on `~/.config/nexus/tuples.db`
(RDR-110's tuple-space database):

```sql
SELECT * FROM tuples
WHERE claim_state = 'claimed'
  AND claim_expires_at > unixepoch()
ORDER BY claim_expires_at ASC;
```

Formatted as a table: actor (extracted via `json_extract(t.dimensions_json,
'$.actor')` — `actor` is a registered dimension stored inside
`dimensions_json`, not a top-level column on `tuples`), subspace, tuple
summary, claimed_at (from `tuple_claim_log` via `JOIN tuple_claim_log tcl
ON t.id = tcl.tuple_id WHERE tcl.transition = 'claim'`; `tuples` has no
`claimed_at` column directly), TTL remaining
(= `claim_expires_at - unixepoch()`). The expiry filter
(`claim_expires_at > unixepoch()`) excludes expired leases not yet swept
— without it, crashed-agent claims would appear as active until the next
sweep. `claim_state` is an internal column on the `tuples` table (per
RDR-110 claim ledger schema), not a registered dimension; it is not
accessible via `read()` and must be queried directly. The cockpit surface
is an internal component in the same process, so direct access to
`tuples.db` is appropriate. Rendered as a tmux pane or ext-apps iframe.

**Recent-events panel** — direct SQL query on `~/.config/nexus/tuples.db`
for time-ordered display (RDR-110's `read()` has no `sort_by` or
`since_cursor` parameter — time ordering requires direct SQL):

```sql
SELECT * FROM tuples
WHERE subspace LIKE 'hook_events/%'
  AND created_at > unixepoch() - ?   -- decay_window from subspace registry
ORDER BY created_at DESC
LIMIT 50;
```

With temporal decay: events older than their subspace `decay_profile`
window are excluded by the `created_at` filter. Supports structural
filtering by `actor`, `session`, `project` dimensions (additional WHERE
clauses). For semantic filtering by intent, call `read()` on the
subspace with a query string, then display results sorted client-side by
the `timestamp` dimension (required on all hook-event subspaces — see
schema above; `timestamp` is a registered dimension accessible in the
`read()` response, unlike `created_at` which is an internal `tuples`
table column not exposed by the API). This trades time-ordering guarantee
for semantic relevance. The default display is SQL (time-ordered); semantic
filter is an opt-in mode.

**Active-bindings panel** — `read(subspace="bindings/<current_profile>",
where={"enabled": "true"})`. Shows trigger, action, surface, enabled state.
Toggle via `take` (remove) + `out` (re-post with `enabled` flipped). Edit
via a form rendered in the panel itself.

### The auto-layout engine

A `src/nexus/cockpit/layout.py` module implementing the three phases:

**Measure** — traverse the active binding set, read layout disposition
from the subspace registry for each event type (surface level, decay
profile, priority class, typical event rate).

**Auto-Style** — produce a stylesheet tuple (one `out` to
`layout_state/<profile>`): for each binding, which surface slot,
which on-screen TTL, which demotion level. Respects the display budget
(terminal width, active pane count) and mission-context profile.

**Layout** — apply the stylesheet to actual events: render status line,
populate panels, fire notifications, raise full-screen for checkpoints.

The demotion cascade, applied when the display budget shrinks:
```
full-screen → panel → status-line → notification-only → suppressed
```
Per-binding `min_surface` prevents critical bindings from falling
below their floor. Manual pin overrides auto-layout for that binding.

**Write-storm guard**: the auto-layout engine must debounce its
`layout_state/<profile>` tuple writes. Hook events may arrive in
bursts (e.g., 10 `PostToolUse` events in < 100ms). A naive re-layout
on every event would produce a write-per-event flood. The engine must
batch events over a 250ms debounce window before re-running the
Measure/Auto-Style/Layout pipeline. Only one `layout_state` write
per debounce window is allowed.

### Cross-instance liveness

**Implementation: T2 table, not the tuple space.** Although project-tier
T2 subspaces in RDR-110 are cross-process (SQLite WAL mode is multi-writer
safe), raw T2 is the correct choice for liveness for three reasons: (a) no
semantic matching is needed — liveness is a pure key-value keep-alive
requiring only primary-key lookup by `(pid, machine)`; (b) no subspace
schema registration overhead (liveness has no `match_text` or vector
embedding); (c) no tombstone semantics — rows are hard-deleted on expiry,
not soft-deleted with `data_version` ticks as required by the tuple space
claim ledger. T2 (SQLite WAL, via `src/nexus/db/`) is the correct tier per
RDR-105.

T2 `liveness` table added via a new migration entry in
`src/nexus/db/migrations.py` (actual migration number assigned at
implementation time — add as the next integer after the current
highest-numbered migration):

```sql
CREATE TABLE liveness (
    pid          INTEGER NOT NULL,
    machine      TEXT    NOT NULL,
    user_id      TEXT    NOT NULL,
    session      TEXT,
    project      TEXT,
    focus        TEXT,
    activity     TEXT,
    last_seen    REAL    NOT NULL,  -- epoch seconds
    PRIMARY KEY (pid, machine)
);
```

Each `nx-mcp` process upserts its row at startup and every 30s via the
FastMCP lifespan background task (per RDR-094 pattern). A sweep thread
deletes rows where `last_seen < epoch() - 60`. `nx instances` queries
the T2 table directly and formats the result as a table.

The `~/.config/nexus/t1_addr.<pid>` file mechanism (RDR-105) is
retained for T1 bootstrap; `liveness` replaces it only for the
cross-instance visibility use-case.

## Alternatives Considered

### A: Separate event store (not the tuple space)

A dedicated SQLite table for hook events, separate from the tuple
space. Rejected: the whole point is one query model for coordination
metadata. A separate store means two mental models, two
calibration stories, two debug surfaces. The tuple space is
already the right shape.

### B: Push notifications via the existing Notification hook

Use `Notification` to push events to the user rather than projecting
into the tuple space. Rejected: `Notification` is a one-way push to
the current session. It has no queryability, no persistence, no
subscription model. It is not composable. The ORB adds a composable
layer; `Notification` remains a valid *sink* for a binding's action.

### C: Separate binding engine process

A dedicated process that watches the tuple space and dispatches
binding reactions. Rejected for Phase 1: the hook bridge can dispatch
binding reactions synchronously (or via a lightweight background
thread) without a separate process. A separate process adds
deployment complexity that isn't justified until binding volume or
latency requirements force it.

### D: Visual wiring UX (LabVIEW-style)

A drag-and-drop wiring interface for bindings. Rejected as the entry
point: form-based binding authoring is closer to what's been proven
learnable (WeakAuras, Streamdeck plugins). Wiring may be useful for
advanced users in a later phase but is not the starting point.

## Trade-offs

| Decision | Trade-off |
|---|---|
| Semantic events as tuples (not a separate event log) | One query model, one persistence story — but tuple space volume grows with hook frequency. Mitigated by per-subspace TTL/decay. |
| Bindings dispatched in-process by the bridge | Simple, no extra process — but binding reaction latency is bounded by bridge execution time. A slow binding can delay the next hook. Mitigated by timeout on action dispatch. |
| Auto-layout drives from the binding set, not from events | Layout is stable and predictable — but a binding with no recent events still occupies a layout slot. Mitigated by `decay_profile` on the registry entry. |
| Connection manifests declared in the tuple space | Every direct connection is visible and debuggable — but manifest heartbeating adds a small overhead per active stream. Acceptable at expected connection counts. |
| tmux as the first fulfillment target | Terminal users get the full cockpit immediately — but GUI (ext-apps iframe) and headless adapters are deferred. Each is ~200 LoC and independently shippable. |

## Implementation Plan

### Prerequisites

- [ ] RDR-110 Phase 1 Step 4 shipped (core API: `out`/`read`/`take`/
      `ack`/`nack` as MCP tools, `data_version` watcher, `block`
      support on `take`).
- [ ] RDR-110 Phase 1 Step 5 shipped (periodic sweeps for TTL/tombstone
      cleanup — required for liveness TTL and event decay to work).
- [ ] RDR-110 Phase 1 Step 6 shipped (five canonical subspaces — the
      `events/<topic>` and `locks/<resource>` patterns are prior art
      for the new hook-event and bindings subspaces).

### Phase 1: Hook bridge + seven event subspaces

#### Step 1: Seven hook-event subspace schemas

Add `nx/tuplespace/builtin/hooks/*.yml` with the seven hook-event subspace
definitions, plus `nx/tuplespace/builtin/connection_manifest.yml` and
`nx/tuplespace/builtin/layout_state.yml` (schema defined in Proposed
Solution). Register layout disposition fields on each hook-event subspace
(surface level, decay profile, priority class, `preempts` list).
No code changes — schema-only. `layout_state` ships now so the layout
engine (Step 11, Phase 3) can write to a registered subspace without a
schema-migration step later.

#### Step 1b: CA-8 spike (PreToolUse ordering — required before Step 2)

Run the CA-8 multi-hook ordering experiment **before writing any bridge
code**. Use the cc-validation harness pattern from
`scenarios/07_perms_preempt_hook.sh`: register two PreToolUse hooks —
bridge first (emits `allow`), then an existing decision hook (may emit
`block`). Determine whether first-registered `allow` preempts the second
hook's `block`, or all hooks run and the last `block` wins. Record the
result in T2 (`111-ca8-spike-result`) and update Step 2's registration
order accordingly before any bridge code is written. (≤ 1h)

#### Step 2: Hook → tuple bridge

`src/nexus/cockpit/hook_bridge.py`: reads hook payload from stdin,
maps to subspace + dimensions + match_text, calls `out` via the
MCP tool (or directly against the store if in-process), exits 0.
Register in `nx/hooks/hooks.json` (the canonical hook registration
location per RF-3) for the **seven projected hook types**: `PreToolUse`,
`PostToolUse`, `Stop`/`StopFailure` (→ `assistant_turn_ended`),
`SubagentStop`, `UserPromptSubmit`, `SessionStart`/`SessionEnd`
(→ `session_lifecycle`), `Notification`. **Not registered**:
`SubagentStart` (carries no semantic event worth projecting — the
subagent's task description is already in the prompt),
`PermissionRequest` (ephemeral permission handshake, no durable
coordination state), `PreCompact` (output unused), and `PostCompact`
(redundant with the compact summary already injected into context — see
Proposed Solution, "The seven semantic event subspaces").

Unit tests: mock `out`; assert correct subspace, dimensions, and
match_text for each hook type. One test per hook type + one for
malformed payload (must exit 0).

#### Step 3: T2 liveness table + heartbeat

Add `liveness` table via `src/nexus/db/migrations.py` migration (manages
`~/.config/nexus/memory.db` — see schema above). Wire upsert into the
`nx-mcp` FastMCP lifespan background task (startup upsert + 30s periodic
upsert via `asyncio.create_task`, matching the pattern in RDR-094). Add
sweep logic to delete stale rows (`last_seen < epoch() - 60`) in the
same background task. Add `nx instances` CLI command: queries the
`liveness` table in `memory.db` directly and formats as a table. No
tuple-space subspace YAML required for liveness.

Note: `watcher_state` (cursor table for `_BindingWatcher`) is added in
`tuples.db` as part of the RDR-110 implementation (not here), so that the
cursor and tuple reads are in the same database for genuine atomicity. See
Phase 2 Step 6.

### Phase 2: Bindings primitive

#### Step 4: Bindings subspace schema

Add `nx/tuplespace/builtin/bindings.yml`. Include all fields from
the proposed solution above. Add `profile` as a first-class
dimension so `read(subspace="bindings/<profile>")` is the query
for the active profile's bindings.

#### Step 5: Binding CRUD path + MCP tools

`src/nexus/cockpit/bindings.py`: `binding_create`, `binding_list`,
`binding_toggle`, `binding_delete`. Wire as four new MCP tools.
`binding_create` **returns a `task_id` immediately** (async task
pattern): it enqueues the binding creation work (NLP parse + `out` call)
and returns `{"task_id": "<uuid>", "status": "pending"}` to the caller
without waiting for completion. A background `asyncio.Task` does the
actual work and updates the binding's `status` dimension from `pending`
to `active` when done. This keeps `binding_create` non-blocking from
the MCP caller's perspective. The LLM-assisted authoring path (calling
`nx_answer` to parse a natural-language binding description into a
structured tuple) runs inside this background task and is `await`-ed
there. If `nx_answer` is currently synchronous, wrap it in
`asyncio.run_in_executor` with a dedicated executor — do not block the
MCP event loop. Callers poll status via `binding_list` filtering on
`task_id` or register a binding-completion notification.

#### Step 6: Binding reaction loop

A `_BindingWatcher` background task (integrated with the FastMCP
lifespan async task group per RDR-094; **must be `asyncio.create_task`,
not `threading.Thread`** — the MCP server is async and thread-pool
dispatch creates reentrancy risk) that consumes new events via the
**RDR-112 daemon `EventStream` RPC** on the `tuples/<subspace>`
namespace.

**Retrieval mechanism — RDR-112 EventStream RPC**: under RDR-112 the
T2 daemon owns the SQLite handle for `tuples.db`; the MCP server is a
*client* of the daemon (RDR-112 §Proposed Solution §1 + Approach §7).
Direct SQL access from inside `nx-mcp` is daemon-internal-only and
forbidden to clients. The watcher subscribes via:

```python
# Pseudocode — verify signature during implementation.
async for event in t2_client.event_stream(
    subspace_prefix="tuples/<subspace>",
    since_cursor=last_rowid,   # 0 on first connect; persisted across restarts
):
    # event = {cursor: rowid, subspace, op, payload_summary, category, ts}
    ...
```

The daemon's EventStream RPC carries the same monotonic `rowid` cursor
the original direct-SQL pattern used, so the at-least-once semantics
(below) survive verbatim — only the transport changes. On disconnect,
the watcher reconnects from its last-acked cursor. `EventStream` is the
*only* way clients observe tuple changes; the RDR-110 `read()` API
remains the surface for associative / structural queries by external
callers.

A `watcher_state` table records the per-subspace cursor (last-seen
`rowid`). **Database placement**: `watcher_state` lives in `tuples.db`
alongside the tuple data. Under RDR-112, this table is owned by the T2
daemon (single SQLite handle); the client-side watcher reads and writes
its cursor via a dedicated daemon RPC pair (`watcher_state.get`,
`watcher_state.set`) rather than direct SQL. The schema (owned by
RDR-110's tuple-space migration, applied daemon-side per RDR-112
Approach §9):

```sql
-- in tuples.db migration (RDR-110 implementation, daemon-applied)
CREATE TABLE IF NOT EXISTS watcher_state (
    subspace   TEXT NOT NULL,
    profile    TEXT NOT NULL,
    last_rowid INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (subspace, profile)
);
```

**Cursor persistence and restart semantics** (required for correctness):

- The watcher's cursor position (`rowid` of the last processed tuple)
  is persisted to `watcher_state` in `tuples.db` after every
  successfully dispatched batch. The correct sequence is:

  ```python
  # 1. Fetch events (SELECT ... WHERE rowid > cursor ORDER BY rowid)
  events = fetch_events(conn_tuples, subspace, cursor)

  # 2. Dispatch action — OUTSIDE any SQLite transaction.
  #    dispatch is an async network call (tool_call / out / send_message);
  #    Python sqlite3 is synchronous — you cannot await inside an open
  #    sqlite3 transaction without blocking the event loop.
  await dispatch_action(event, idempotency_key=...)

  # 3. Advance cursor — separate, committed after dispatch succeeds.
  with conn_tuples:  # sqlite3 context manager = BEGIN/COMMIT
      conn_tuples.execute(
          "UPDATE watcher_state SET last_rowid=?, updated_at=?"
          " WHERE subspace=? AND profile=?",
          (event.rowid, time.time(), subspace, profile)
      )
  ```

  Steps 2 and 3 are **not** in the same SQL transaction — they cannot
  be: the dispatch is an async network call. The delivery guarantee is
  **at-least-once**: if the process crashes between step 2 and step 3,
  the event is replayed on restart. The `idempotency_key` mechanism in
  the action descriptor handles duplicate deliveries.

  `watcher_state` and `tuples` are in the same database (`tuples.db`),
  which means only one database connection is needed; it does not imply
  that dispatch and cursor-advance are in the same ACID transaction.
- **Delivery guarantee: at-least-once.** The cursor advances only
  after the action is dispatched (not before). Duplicate deliveries
  are possible on crash-restart. Actions must therefore be idempotent:
  the `action_descriptor` schema must include an `idempotency_key`
  field (e.g. `sha256(binding_id + event_tuple_id)`); action handlers
  must be no-ops if the key has been seen in a T2 dedup table within
  the action's TTL window (default 5 minutes).
- **At-most-once is not the guarantee.** Do not design actions that
  must fire exactly once without a separate dedup mechanism.

**Idempotency dedup table** (home: `memory.db`, added via
`src/nexus/db/migrations.py`, same migration entry as `liveness`):

```sql
CREATE TABLE IF NOT EXISTS action_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    expires_at      REAL NOT NULL   -- epoch seconds; TTL default 300s
);
```

Action handlers check `SELECT 1 FROM action_idempotency WHERE
idempotency_key = ?` before executing; if found, skip and return
success. On first execution, insert the key. Sweep during the liveness
sweep task: `DELETE FROM action_idempotency WHERE expires_at <
unixepoch()`.

Action dispatch: `tool_call` type dispatches via the MCP tool API
(must be `await` — no `asyncio.run_in_executor` for tool calls);
`tuple_out` calls `out` directly; `send_message` uses the harness
`SendMessage` tool. All dispatch is bounded by a `action_timeout_s`
field on the action descriptor (default 10s); timeout fires the next
cursor advance regardless.

**Watcher failure isolation** — must distinguish failure categories
to compose correctly with the RDR-112 daemon:

- **Action failure** (the dispatched tool call / `out` / send_message
  raised). Caught, logged via `structlog.get_logger(__name__)`, cursor
  advances past the failed event to prevent livelock. Consecutive-
  failure counter (kept in T2 via `memory_put`) increments; after 5
  consecutive failures on the same binding the binding is
  auto-disabled with an error annotation. This is "the user's binding
  is broken" — the original failure-isolation path, unchanged.
- **Substrate failure** (RDR-112 daemon down, EventStream RPC
  disconnect, transient transport error). Logged at WARN; cursor does
  **not** advance; consecutive-failure counter does **not** increment
  toward auto-disable. The watcher reconnects with exponential
  back-off (capped at 30s) and resumes from the last-acked cursor.
  Substrate hiccups must not silently auto-disable user bindings.
- **Schema failure** (event payload doesn't match the binding's
  expected shape — e.g. a registry version mismatch). Logged at ERROR
  with the binding ID, cursor advances, counter increments. Treated
  as an action failure: the binding is wrong relative to the current
  schema and the user needs to fix it.

The EventStream event record carries an optional `category` field
(per RDR-112 Approach §7); for transport-level errors the watcher
classifies directly from the exception type without needing daemon
cooperation.

#### Step 7: Phase 1 CA spikes (gate Phase 2)

These spikes must complete before Phase 2 begins:

- **CA-6**: Empirically capture real payload for `SubagentStop`,
  `UserPromptSubmit`, `PreCompact`, `Notification` hooks by running
  a logging hook in a live session. Update RF-1 table with verified
  schemas. Write bridge scripts for these 4 types only after payloads
  are confirmed. (≤ 2h)

Note: **CA-8** (PreToolUse multi-hook ordering) was completed in Step 1b,
before Step 2. Its result has already been applied to bridge registration
order. It is not repeated here.

#### Step 7b: Phase 2 CA spikes (gate Phase 3)

- **CA-3**: Benchmark `read` query latency at 10k, 50k, 100k tuples.
  If > 50ms, add a materialised view (SQLite view or summary table).
- **CA-5**: Benchmark binding reaction end-to-end latency (hook fire →
  action dispatch) at 10 concurrent active bindings. If > 200ms,
  move binding watcher to a dedicated process.
- **CA-7**: Prototype Bakke Measure/Auto-Style/Layout on a static
  binding set. Estimate LoC and developer-days. If > 300 LoC or
  > 1 week, scope Phase 3 accordingly or defer layout engine to a
  follow-on RDR.

#### Step 7c: Phase 3 CA spike (gate finalization)

- **CA-4**: Measure tmux `status-right` update cadence on target
  hardware. If noticeable lag, increase refresh interval to 5s and
  add a stale indicator.

### Phase 3: C2 cockpit surfaces

#### Step 8: Active-claims panel

`src/nexus/cockpit/surfaces/claims.py`: direct SQL on
`~/.config/nexus/tuples.db` WHERE `claim_state = 'claimed' AND
claim_expires_at > unixepoch()` (expiry filter required to exclude
expired leases not yet swept — see Proposed Solution for full query and
rationale). Formats as a table: actor, subspace, tuple summary,
claimed_at, TTL remaining. tmux fulfillment: update a named pane on a
2s cadence. ext-apps fulfillment: render as an iframe via `ui://claims`.

#### Step 9: Recent-events panel

`src/nexus/cockpit/surfaces/events.py`: direct SQL on
`~/.config/nexus/tuples.db` for default time-ordered display (see
Proposed Solution for full query). For semantic filtering, falls back
to `read()` with client-side sort by the `timestamp` dimension (registered
on all hook-event subspaces, present in the `read()` response — unlike
`created_at`, which is an internal `tuples` table column not in the DTO).
Supports structural params (`actor`, `session`, `project`) as additional
WHERE clauses.
tmux + ext-apps fulfillment as above.

#### Step 10: Active-bindings management panel

`src/nexus/cockpit/surfaces/bindings.py`: `read(subspace=
"bindings/<profile>", where={"enabled": "true"})`. Toggle via
`take` + `out` (remove then re-post with `enabled` flipped). Edit
in-panel; the edit form is itself a binding-create form rendered inline.

#### Step 11: Auto-layout engine

`src/nexus/cockpit/layout.py`: Measure / Auto-Style / Layout
pipeline. Reads the subspace registry for layout dispositions.
Writes a `layout_state/<profile>` tuple. Triggers on:
binding-set change, profile switch, display-budget change
(terminal resize signal, pane count change). Implements the
demotion cascade and `min_surface` floor.

#### Step 12: First end-to-end mission profile

Pick a real workflow (RDR creation, build watching, incident
response). Author the bindings, the layout, the permission mode.
Run it. Measure what's awkward. Iterate.

### Connection manifest (Phase 4 — deferred)

The `connection_manifest` subspace schema ships in Phase 1 (Step 1)
so producers can use it. The fulfillment adapters that *consume*
manifests to open direct streaming connections are deferred to
Phase 4 (ext-apps iframe work, covered in the workflow-engine doc
handed off separately).

## Test Plan

### Unit tests

- Hook bridge: one test per hook type for correct subspace/dimensions
  mapping. One test for malformed payload (must exit 0 and log).
- Bindings CRUD: create / list / toggle / delete round-trip.
- Binding reaction: mock event tuple → assert correct action dispatch.
- Layout engine: given a binding set and display budget, assert
  correct stylesheet tuple. Test demotion cascade at N bindings
  exceeding budget.
- Liveness: heartbeat emitted at startup; `nx instances` returns
  the expected row; TTL expiry removes the row.

### Integration tests

- Bridge → tuple space end-to-end: fire a real hook, assert the
  tuple appears in the `hook_events/*` subspace with correct fields.
- Binding reaction end-to-end: register a binding; fire a hook;
  assert the action was dispatched within 200ms.
- Cockpit panel SQL queries (active-claims, recent-events) return
  correct results at 1k, 10k event volumes against `tuples.db`.
  Active-bindings panel `read()` returns correct results at 1k bindings.

## Validation

The ORB is validated when:

1. A `PreToolUse` hook fires and the corresponding tuple appears in
   `hook_events/tool_call_intent` within 100ms.
2. A user-authored binding ("when a tool call completes, update the
   status line with the tool name and duration") fires correctly
   for 10 consecutive tool calls with no missed events.
3. The active-claims panel accurately reflects all currently-claimed
   tuples, updating within the refresh cadence.
4. `nx instances` returns the correct set of live processes on a
   machine running two `claude` sessions simultaneously.
5. A mission profile (chosen in Step 12) runs end-to-end with all
   three cockpit panels providing correct situational awareness,
   and the user can toggle a binding on/off from the panel without
   restarting any process.

## Finalization Gate

Phase-gated CA clearance:
- [ ] **Before Phase 1 begins**: CA-9 cleared = RDR-110 Phase 1 Step 6 confirmed shipped with open-registry registration mechanism (or RDR-110 amended to add one), verified by checking the Step 6 implementation. Notification of the RDR-110 implementor is the action to take; a confirmed open-registry implementation is the gate criterion.
- [ ] **Before Phase 1 Step 2**: CA-8 (PreToolUse multi-hook ordering determined; Step 2 registration order updated per result; fallback design implemented as feature flag).
- [ ] **Before Phase 2**: CA-6 (4 inferred hook payload shapes verified empirically).
- [ ] **Before Phase 2 Step 6**: CA-10 (`watcher_state` table confirmed in RDR-110 tuples.db migration).
- [ ] **Before Phase 3**: CA-3 (`read` latency benchmark) + CA-5 (binding reaction latency benchmark) + CA-7 (Bakke layout engine LoC estimate).
- [ ] **Before finalisation**: CA-4 (tmux cadence verified on target hardware) + CA-1 (RDR-110 prerequisite steps shipped).

Other gates:
- [ ] Unit + integration test suite passes.
- [ ] First end-to-end mission profile runs cleanly (Step 12).
- [ ] `docs/agentic-cockpit.md` updated to note implementation
      status of each section.
- [ ] `docs/cli-reference.md` updated with `nx instances` and
      `nx bindings` commands.
- [ ] RDR-110 implementor notified of subspace registry extension
      requirement (per Relationship to RDR-110 section).

## References

- `docs/agentic-cockpit.md` — Design exploration; source for all
  architectural decisions in this RDR.
- `docs/rdr/rdr-110-semantic-tuple-space.md` — Foundation primitive.
- `docs/rdr/rdr-105-t1-chroma-architecture-env-passdown.md` — T1/T2
  tier discipline and process lifecycle patterns.
- Gelernter & Carriero, "Linda in Context" (1992).
- Bakke, Karger & Miller, "Automatic Layout of Structured Hierarchical
  Reports" (InfoVis 2013).
- WeakAuras project documentation (wago.io).

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-05-11 | Hal Hildebrand | Initial draft |
| 2026-05-11 | Hal Hildebrand | Post-gate revision: expanded RF-1 to complete verified payload inventory for all 13 hook types; revised liveness from tuple-space to T2 table (cross-process visibility requirement); added _BindingWatcher restart semantics, cursor persistence, at-least-once delivery + idempotency_key requirement; added CA-6 (inferred payload empirical spike), CA-7 (Bakke LoC estimate), CA-8 (PreToolUse ordering empirical spike); restructured CA phase gates to P1/P2/P3; added debounce to auto-layout engine; added binding_create async requirement; added RDR-110 subspace registry extension coordination note |
| 2026-05-11 | Hal Hildebrand | Post-gate-2 revision (all gate-2 BLOCKED issues addressed): (C-NEW-1) _BindingWatcher retrieval mechanism changed from `rd()` with offset cursor to direct T2 SQL on `tuples` table by `rowid` — intentional internal bypass, RDR-110 `read()` has no ordered-retrieval parameter; (C-NEW-2) Active-claims panel changed from `rd(claim_state=claimed)` to direct T2 SQL — `claim_state` is an internal column not a registered dimension; (S1) T2 migration added to `src/nexus/db/migrations.py`, `watcher_state` table added to same migration as `liveness`; (S2) corrected false liveness rationale — project-tier T2 subspaces ARE cross-process, liveness uses raw T2 for semantic/schema/tombstone reasons; (S3) registry extension CA-9 added as definitive prerequisite, registry section changed from conditional "if" to CA-9; (S4) CA-8 fallback design documented (no-permissionDecision or last-in-chain if first-wins confirmed); (S5) `binding_create` changed to return `task_id` immediately with async task pattern |
| 2026-05-11 | Hal Hildebrand | Post-gate-4 revision (all gate-4 BLOCKED issues addressed): (C-G4-1) corrected cursor-advance pseudocode — dispatch is now outside SQLite transaction (async dispatch cannot be wrapped in synchronous sqlite3 BEGIN/COMMIT); removed "genuinely atomic" claim; clarified same-database placement enables one connection, not transactional atomicity between dispatch and commit; (S-G4-1) switched recent-events semantic fallback sort from `tuple.created_at` (internal column not in read() DTO) to `timestamp` dimension (registered, accessible in read() response); (S-G4-2) removed CA-8 from Step 7 spike list — now only in Step 1b where it belongs; added cross-reference note; (S-G4-3) added CA-10 for `watcher_state` table coordination with RDR-110 implementor; added to Finalization Gate; (O-G4-2) fixed Step 2 registration path from `.claude/hooks/` to `nx/hooks/hooks.json` |
| 2026-05-11 | Hal Hildebrand | Post-gate-5 revision (no criticals; 3 significant fixed pre-accept): (SIG-1) removed residual "enabling a genuine single-transaction cursor advance" claim from watcher_state rationale — replaced with accurate "one connection" language; (SIG-2) corrected Step 9 semantic fallback sort from `created_at` (internal column, not in read() DTO) to `timestamp` dimension; (SIG-3) added `action_idempotency` table schema, migration reference (memory.db), and sweep hook to Phase 2 Step 6 |
| 2026-05-11 | Hal Hildebrand | Post-gate-6 revision (no criticals; all significant and observation issues addressed pre-accept): (SIG-A) replaced `rd`/`in` Linda aliases with `read`/`take` throughout all implementation-facing sections — Bindings CRUD, active-bindings panel, Step 10, Relationship to RDR-110 surfaces sentence, Key Discoveries (bindings, surfaces, cross-process), CA-3 across all references (assumption table, Step 7b spike, Finalization Gate), and the Linda bullet's "ORB uses" clause; added Linda→RDR-110 API name mapping table and usage rule to Relationship to RDR-110 section; (SIG-B) added `layout_state/<profile>` subspace schema block (dimensions: profile, event_type; content: surface_level, demotion_level, ttl_seconds, pinned) to Proposed Solution; added `layout_state.yml` and `connection_manifest.yml` to Step 1 file list; (OBS-1) added `tuple_claim_log` join note for `claimed_at` display field — `tuples` has no `claimed_at` column; (OBS-2) `connection_manifest.yml` now explicit in Step 1; (OBS-3) integration test 3 corrected to distinguish SQL-based panels (active-claims, recent-events) from `read()`-based panel (active-bindings); (OBS-4) Step 2 explicitly documents which hook types are projected (7) and which are excluded (SubagentStart, PermissionRequest) with rationale |
| 2026-05-11 | Hal Hildebrand | Post-gate-3 revision (all gate-3 BLOCKED issues addressed): (C-G3-1) Added `task_id` (string) and `status` (enum: pending/active/failed) dimensions to bindings subspace schema YAML; documented enabled/status/task_id lifecycle; (S-G3-1) moved `watcher_state` table to `tuples.db` (not `memory.db`) for genuine single-transaction cursor atomicity; corrected "atomic" language; (S-G3-2) added `claim_expires_at > unixepoch()` filter to active-claims panel query in Proposed Solution and Step 8 to exclude expired leases; (S-G3-3) flagged Step 2 bridge registration order as provisional pending CA-8; moved CA-8 spike to new Step 1b (before Step 2); added feature-flag requirement for registration order; (S-G3-4) changed recent-events panel from `rd()` (no time ordering) to direct SQL on `tuples.db` ordered by `created_at DESC`; semantic filter noted as opt-in mode; (S-G3-5) replaced "direct T2 SQL via src/nexus/db/" with explicit `tuples.db` database references throughout; (O1) corrected "six" to "seven" subspaces; (O2) documented PostCompact/PreCompact exclusion with rationale; (O3) updated CA-9 gate condition to outcome-based (confirmed open-registry shipping) not action-based (notification sent) |
| 2026-05-11 | Hal Hildebrand | Post-gate-7 revision (1 critical + 2 significant + 2 observations all addressed pre-accept): (C-G7-1) removed `PostCompact` from Step 2 bridge registration list — was contradicted by Proposed Solution's explicit exclusion; added `PreCompact` and `PostCompact` to the not-registered list with rationale cross-reference; (SIG-G7-1) replaced three residual `rd()` aliases with `read()` — Step 4 bindings dimension example, Step 6 `_BindingWatcher` rationale paragraph (header + two body references); (SIG-G7-2) corrected "six" → "seven" hook-event subspaces at four remaining sites (Relationship to RDR-110 summary, CA-9 description, CA-9 risk-if-wrong, Phase 1 section header); added `layout_state` to the Relationship-section subspace enumeration; (OBS-G7-1) active-claims display now explicitly extracts `actor` via `json_extract(t.dimensions_json, '$.actor')` — `actor` lives inside `dimensions_json`, not a top-level column; (OBS-G7-2) watcher failure isolation now logs via `structlog` (not "T2 scratch" — category error, scratch is T1); failure counter kept in T2 as before |
| 2026-05-13 | Hal Hildebrand | **Triad rework (RDR-110/111/112 composition gaps, deep-analyst `a56172936d63fd480`)**: added §Relationship to RDR-112; rewrote §Step 6 binding-watcher to consume RDR-112 `EventStream(subspace_prefix, since_cursor)` RPC instead of direct SQL on `tuples.db` (cursor protocol and at-least-once semantics preserved verbatim; only transport changes); rewrote §Step 6 Watcher failure isolation to distinguish action / substrate / schema failure categories so daemon hiccups do not silently auto-disable user bindings (closes G5); updated `watcher_state` placement to record that the table is daemon-owned and read/written via dedicated RPCs (closes G4 wrt RDR-111 dependency); `related_rdrs` frontmatter extended to include RDR-112 and RDR-113. RDR-111 is now blocked on RDR-112 Phase 1 for the binding-watcher; query-only cockpit surfaces unaffected. |
