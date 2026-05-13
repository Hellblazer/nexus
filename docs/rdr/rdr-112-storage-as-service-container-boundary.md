---
title: "Storage-as-Service: Every Persistent Shared-State Store Behind a Daemon, T1 Stays In-Container"
id: RDR-112
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-12
accepted_date:
related_issues: []
related_rdrs: [RDR-004, RDR-041, RDR-105, RDR-108, RDR-110, RDR-111]
related_tests: []
implementation_notes: ""
---

# RDR-112: Storage-as-Service: Every Persistent Shared-State Store Behind a Daemon, T1 Stays In-Container

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus's three storage tiers are accessed today as **library-mode
file handles** — `sqlite3.connect(path)` for T2, `chromadb.PersistentClient(path)`
for T3, `EphemeralClient()` or per-session HTTP for T1. That worked when
the only consumer was a single `nx` CLI process plus its in-tree MCP
server. It breaks the moment work runs **inside a sandbox container**
(Claude Desktop's bundled MCP runtime, ext-app sandboxes, `claude -p`
sub-processes with their own filesystem view, future agentic co-work
peers running on the same host but in distinct containers).

In a container, "open the file at `~/.nexus/t2.sqlite`" resolves to a
*copy or an empty path*, not the host's database. The container gets
its own sqlite, its own chroma collections, its own memory tier — and
silently fragments knowledge across silos. T1 is fine in that world
(per-session by design). T2 and T3 are not — they are the *cross-session,
cross-instance* tiers, and their entire value proposition is that
co-workers see the same state.

### Enumerated gaps to close

#### Gap 1: T2 and T3 leak the storage substrate as a file path

`T2Database` and the T3 chroma client are opened by direct filesystem
path. Any process with read access to the path is a peer writer. Inside
a sandbox container that path either does not exist (silent fork to an
empty DB) or resolves to a bind-mounted copy (silent fork to a stale
DB). There is no enforced single-writer boundary, no liveness check,
no version negotiation — just `open(path)` and hope.

#### Gap 2: Multi-instance co-work fragments knowledge into silos

When Claude Desktop, a `claude -p` sub-process, and a separate IDE
agent all run on the same host, each opens its own T2/T3 handles. T2
writes from one instance are invisible to the others until/unless they
re-open the file (and even then, WAL behavior across containers is not
guaranteed). Memory put in one cockpit is unreachable from another.
This is the same fragmentation problem RDR-105 solved for T1 with env
passdown — but T1 fragmentation is *desired* (per-process working
memory), while T2/T3 fragmentation is *broken-by-design*.

#### Gap 3: No lifecycle, liveness, or event hooks at the storage edge

Because the storage layer is a file handle, there is nowhere to hang
the things RDR-110 (tuple-space `in`/`out`/`rd`) and RDR-111 (ORB hook
projection) actually need: a process owns the data; that process
publishes events on write; clients subscribe; leases time out when
holders die; barriers wake when N participants arrive. None of that
is expressible against `sqlite3.connect(path)` — it needs a process
that *owns* the data and mediates access.

#### Gap 4: T1's existing service shape is the proof-of-concept, but it's not generalised

RDR-105's `NX_T1_HOST`/`NX_T1_PORT` env passdown already establishes
that nexus can run a tier as a host-process service and have container
clients connect to it. That pattern exists for T1 but was deliberately
*not* extended to T2/T3 because T1 had the unique problem (per-session
isolation with sibling visibility). T2 and T3 have a *different* but
equally clear service-shape problem: single writer, many readers,
cross-container shared truth. The substrate is the same; only the
sharing discipline differs.

## Context

### Background

Three recent RDRs make this RDR's question unavoidable:

- **RDR-105** moved T1 from on-disk session records to env-passdown of
  a host-local chroma. Settled the "T1 is per-process working memory"
  question and proved the host-service-plus-env-discovery pattern.
- **RDR-110** named the unified abstraction over plan-match / T1 / T2
  as a semantic tuple space and added Linda's atomic `in` operation.
  Atomic destructive read requires a *single arbiter* — which a shared
  file handle is not.
- **RDR-111** projects hooks onto the tuple space as observable state
  and adds a user-authored composition layer. Projection requires a
  publishing point — which a file handle does not have.

Both RDR-110 and RDR-111 land cleanly *if* there is a process that
owns T2 and a process that owns T3 and they expose typed RPCs. They
land messily otherwise: every reader has to poll, every writer has to
trust filesystem locking, and `in` has to be implemented as advisory
SQLite locks across containers — which does not work.

Independently, Claude Desktop's container model and the proliferation
of `claude -p` sub-processes and ext-app sandboxes have made the
container-vs-host filesystem split a daily operational fact, not an
edge case. Every "I can't see my memory" report from co-work
configurations traces back to this gap.

### Technical Environment

**Scope is universal**: every persistent shared-state store in
nexus moves behind a daemon — not just T2 and T3. The container/silo
problem is structural to *any* file-backed store opened by direct
path; cherry-picking only T2/T3 would leave the same class of bug
sitting under another name (today: `catalog.db`; tomorrow: any new
domain store added without thinking about the boundary).

Inventory of in-scope stores (every direct file open under
`src/nexus/`):

- **T2 (seven domain stores)** behind `T2Database` facade:
  `memory_store`, `plan_library`, `chash_index`, `catalog_taxonomy`,
  `aspect_extraction_queue`, `document_aspects`, `telemetry`. SQLite
  + FTS5, WAL enabled. Each opens its own `sqlite3.Connection`.
- **T3 local mode**: `chromadb.PersistentClient(path)` + local ONNX
  embedder.
- **T3 cloud mode**: `chromadb.CloudClient` — already service-shaped;
  no daemon needed (it's a remote service), but its access goes
  through the same `T3Client` abstraction for symmetry.
- **CatalogDB** (`src/nexus/catalog/catalog_db.py:250-255`): own
  SQLite file, own `sqlite3.connect()`. Structurally identical to a
  T2 domain store and equally vulnerable to the container silo
  problem. **In scope.**
- **Any future shared-state store** — the architecture must make the
  daemon boundary the *default* way to add persistent state, not an
  option developers have to remember to use.
- **T1**: `chromadb.EphemeralClient` in-process or per-session HTTP
  via RDR-105 env passdown. Per-process isolation is *correct* for
  T1; **explicitly out of scope** and stays as-is.

Direct-file-open call sites under `src/nexus/` total roughly 80
files (grepping `sqlite3.connect` + `PersistentClient`). The
impact-analysis inventory (T2 entry `nexus_rdr/112-research-impact-inventory`)
breaks these down by surface and refactor cost.

**MCP boundary**: every nexus tool consumer talks to storage through
`nexus.mcp.*` handlers today; those handlers open T2/T3/CatalogDB
directly. A service split puts a thin client there instead. The MCP
server becomes a *client* of the daemons even when co-located on
the same host.

## Research Findings

### Investigation

Conducted 2026-05-12 via deep-research-synthesizer (T2: 112-research-A1..A4).
Primary sources: installed chromadb package, sqlite.org docs, nexus T1
implementation (`src/nexus/db/t1.py`, `src/nexus/db/session.py`),
T2 facade and stores (`src/nexus/db/t2/`, `src/nexus/db/memory_store.py`,
`src/nexus/db/plan_library.py`, `src/nexus/db/chash_index.py`).

### Key Discoveries

- **Verified**: T2 daemon is *structurally required* for the
  container case — Docker overlayfs blocks the `mmap` semantics SQLite
  WAL requires, so a bind-mounted host path opened from inside a
  container produces a separate WAL per process (or outright failure).
  This is the silent silo failure mode the RDR set out to fix.
- **Verified**: T1's existing service shape (chroma HTTP over loopback,
  spawned at `session.py:494-504`, discovered via `~/.config/nexus/
  t1_addr.<pid>` + PPID walk at `session.py:634-703`) is a live
  production precedent for the T2/T3 daemon pattern.
- **Documented**: `chroma run` is FastAPI + uvicorn with a configurable
  thread pool (`chroma_server_thread_pool_size`, default 40). chromadb's
  own "not for production" disclaimer attaches to `EphemeralClient` /
  `PersistentClient`; `HttpClient` is explicitly the "recommended
  production configuration" (`chromadb/__init__.py:326`).
- **Refuted (partially)**: The RDR-105 PPID-walk + discovery-file
  pattern *does not work* in PID-namespace-isolated containers — the
  walk terminates at container init (PID 1) and `~/.config` is not
  bind-mounted in typical sandbox configurations. The `NX_T2_ADDR` /
  `NX_T3_ADDR` env-var override must be the *primary* path for
  sandboxed consumers, not a fallback. Discovery protocol needs a
  dual-primary design.
- **Inconclusive**: RPC overhead is directionally acceptable —
  T3 store ops are dominated by embedding latency (ONNX 10-50 ms,
  Voyage 100-300 ms) and the hop disappears in noise; T2 `memory_get`
  is the tightest case (0.5-2 ms direct, +10-25% over loopback) and
  needs a spike with `timeit` against a real fixture to lock the
  number. UDS will be faster than the loopback TCP T1 already uses.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| chromadb (`chroma run`, FastAPI app) | Yes | uvicorn-backed; 40-thread pool; HttpClient is recommended prod config. T1 already uses this pattern. |
| sqlite3 WAL | Yes (sqlite.org/wal.html + T2 stores) | WAL cannot use mmap across overlayfs bind mounts → container case broken. Same-host WAL fine. |
| RDR-105 discovery (`session.py`) | Yes | PPID walk + `~/.config/nexus/t1_addr.<pid>` file. Works for host-native and `claude -p`; fails in PID-namespaced containers. |

### Critical Assumptions

- [x] **A1**: chromadb's bundled HTTP server (`chroma run`) is
  production-quality enough to be our T3 service, not just a dev
  convenience. — **Status**: Documented (High confidence) —
  **Method**: Source Search — see T2 `112-research-A1`.
- [x] **A2** (qualified): SQLite cross-process write concurrency under
  WAL is *not* an acceptable substitute for a single-writer daemon —
  **Verified for the container case** (overlayfs breaks WAL mmap);
  **Inconclusive for same-host multi-process**, where WAL +
  `busy_timeout` is the existing T2 design and works correctly. The
  daemon is structurally required for containers, architecturally
  necessary for RDR-110/111, and a discipline win same-host. —
  **Method**: Source Search + sqlite.org/wal.html — see T2
  `112-research-A2`.
- [x] **A3**: A daemon-per-tier architecture imposes acceptable
  latency overhead on the common-case read path. — **Status**:
  **Verified** (High confidence) — **Method**: Spike
  (`/tmp/rdr112_a3_spike.py`, 2000 iters on real `MemoryStore`).
  Results in µs: direct p50=85 / p99=286; UDS p50=100 / p99=300
  (+15 µs); TCP loopback p50=114 / p99=620 (+29 µs, doubled tail).
  All sub-millisecond. **Design implication**: T2 daemon binds both
  UDS and loopback TCP. Clients prefer UDS (faster, bypasses TCP
  stack, free unix file permissions) but fall back to TCP whenever
  the socket file is unreachable — Windows VM-based co-work,
  containerised Claude Desktop, port-allow-listing sandboxes. The
  ~14 µs gap between transports is well under budget so the fallback
  is essentially free. T3 transport hop is below noise (embedding
  dominates). See T2 `112-research-A3-spike`.
- [x] **A4** (refuted): Discovery via per-host UID file (RDR-105
  pattern) does **not** work universally — it fails in
  PID-namespace-isolated containers where `/proc` and `ps` show only
  container processes and `~/.config` is not bind-mounted. Mitigation:
  promote `NX_T2_ADDR`/`NX_T3_ADDR` env-var to co-primary discovery
  path, with daemon emitting its address on stdout for orchestrator
  capture-and-inject. Proposed Solution will be revised to reflect
  dual-primary discovery. — **Method**: Source Search of
  `session.py:634-703` + container semantics — see T2 `112-research-A4`.

## Proposed Solution

### Approach (sketch — to be refined)

1. **T2 service**: introduce `nx daemon t2` — a long-lived process
   that owns the SQLite handle and exposes the seven domain-store
   operations over **both UDS and loopback TCP** listeners. The
   daemon always binds both. The **client** picks: UDS first (faster,
   bypasses the TCP stack, gets unix file permissions for free), TCP
   fallback when UDS is unreachable. UDS-unreachable is the expected
   case for (a) Windows VM-based co-work, (b) containerised Claude
   Desktop variants, (c) any orchestrator that allow-lists ports but
   not socket files, and (d) ext-app sandboxes that can do network
   policy but not bind mounts. A3 spike: UDS p50=100 µs, TCP p50=114
   µs — both well under budget, so making TCP the fallback (not the
   exclusion) costs us nothing and buys cross-environment reach.
   Clients link `T2Client` instead of `T2Database`; the facade API
   is unchanged.
2. **T3 service**: run `chroma run` as a managed subprocess under
   `nx daemon t3`. **Note**: chromadb 1.4.4's HTTP server is loopback
   TCP, not UDS — we accept the TCP hop for T3 because (a) it's an
   upstream constraint we don't control, and (b) T3 reads are
   dominated by embedding latency (ONNX 10–50 ms, Voyage 100–300 ms)
   so the transport hop is below noise. Local-mode T3 stops being
   "open the directory directly" and becomes "connect to the managed
   local server."
3. **CatalogDB service**: `CatalogDB` (`src/nexus/catalog/catalog_db.py`)
   is a separate SQLite file structurally identical to a T2 domain
   store. **Two viable shapes** — to be decided during planning:
   (a) collapse CatalogDB into T2 as an eighth domain store and serve
   from `nx daemon t2` (preferred for operational simplicity — one
   daemon, one introspection surface, one discovery file); or
   (b) ship `nx daemon catalog` as a third daemon, mirroring T2's
   transport and discovery pattern (preferred only if catalog's
   schema-evolution cadence or crash-isolation profile differs enough
   from T2 to justify the second process). The container-silo
   problem and the introspection-surface requirement are the same
   either way.
4. **T1 stays put**: RDR-105's env passdown is already correct for T1.
   No change.
5. **Any future persistent store** is added as a domain inside an
   existing daemon (preferred) or as a new daemon (only if a
   first-class operational separation is needed). Direct
   `sqlite3.connect()` / `chromadb.PersistentClient()` outside a
   daemon process becomes a lint-checked anti-pattern, enforced by
   `nx doctor` or a CI rule.
6. **Discovery (dual-primary)**: each daemon writes **both** its
   UDS socket path and its TCP `host:port` to
   `~/.config/nexus/<tier>_addr.<host_uid>` (single file, both
   addresses) for host-native consumers with shared filesystem. For
   sandboxed/containerised consumers where the discovery file is
   unreachable (PID-namespaced containers per the A4 finding,
   Windows-VM co-work, or containerised Claude Desktop), the
   orchestrator injects `NX_T2_SOCK` and/or `NX_T2_ADDR` (and the T3
   / catalog equivalents) into the container's environment. Daemon
   emits both addresses on stdout at startup for orchestrator
   capture. **Client transport selection**: if `NX_T2_SOCK` is set
   and the socket is reachable → UDS. Else if `NX_T2_ADDR` is set →
   TCP. Else read the discovery file and try UDS first, TCP second.
   Fail loud if neither reaches a live daemon.
7. **Lifecycle hooks**: each daemon publishes change events to a
   local pub-sub (RDR-110 tuple space surface), enabling RDR-111's
   ORB projection without each client having to instrument writes.
   T2, CatalogDB, and T3 all participate.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `nx daemon t2` | `nexus.db.t2.T2Database` (seven domain stores) | Extend — wrap existing facade in a socket server; do not rewrite store logic. |
| `nx daemon t3` (local mode) | `nexus.db.t3.T3Database` (PersistentClient) | Replace — direct PersistentClient becomes managed `chroma run` subprocess. |
| CatalogDB serving (in scope) | `nexus.catalog.catalog_db.CatalogDB` (`sqlite3.connect` at `:255`) | Decide during planning: collapse into T2 as eighth domain store (preferred), or ship as separate `nx daemon catalog`. Either way, direct file access goes away. |
| Client-side `T2Client` | `T2Database` facade | New — thin RPC client mirroring the facade signature, so call sites do not change. |
| Client-side `CatalogClient` | `CatalogDB` public API | New — same pattern; whether it shares the `T2Client` connection depends on the daemon-collapse decision. |
| Discovery file | `~/.config/nexus/t1_addr.<claude_pid>` (RDR-105) | Extend pattern — same mechanism, one address file per daemon. |
| **Lint rule**: ban direct `sqlite3.connect` / `PersistentClient` outside `src/nexus/db/` daemon-internal code | (new) | Prevents regression: future stores must go through a daemon by default. |

### Decision Rationale

The discipline this RDR imposes — *no direct DB access outside the
owning process* — is what unlocks RDR-110's atomic-take and RDR-111's
event projection. It also closes a class of co-work bugs that today
present as "memory I saved isn't there" and are diagnosed individually
each time. The cost is a daemon to keep alive; the discovery+auto-spawn
pattern from RDR-105 already shows that cost is tolerable.

## Alternatives Considered

### Alternative 1: Status quo — direct file access, hope WAL holds

**Description**: Keep opening T2 and T3 by path; rely on SQLite WAL
and chroma's filesystem locking to mediate cross-process access.

**Pros**:

- No new daemon to operate.
- No discovery problem.

**Cons**:

- Does not work across container boundaries (the central problem).
- No place to hang RDR-110 atomic-take or RDR-111 event projection.
- Silent fork-into-empty-DB failure mode persists.

**Reason for rejection**: Does not solve the stated problem.

### Alternative 2: Single mega-daemon owning all three tiers

**Description**: One `nx daemon` process owns T1+T2+T3.

**Pros**:

- One process to manage, one discovery file.

**Cons**:

- T1 must stay per-session — collapsing it into a shared daemon
  breaks RDR-105's sibling-visibility model.
- T2 and T3 have very different scaling and crash-isolation profiles;
  a chroma OOM should not take down memory.

**Reason for rejection**: Conflates tiers that RDR-105 deliberately
separated.

### Briefly Rejected

- **Database-as-MCP-server only**: making the daemon a full MCP server
  rather than a tier-internal RPC would conflate the storage boundary
  with the tool boundary. The tool layer already exists; we want a
  layer *below* it.
- **Cloud-only T3**: would solve the boundary problem but reintroduces
  the local-mode-required-for-air-gapped-work constraint.

## Trade-offs

### Consequences

- (+) Closes the cross-container silo class of bugs structurally.
- (+) Gives RDR-110 and RDR-111 a real publishing/arbitrating point.
- (+) Forces discipline that future tiers (T4? cross-host?) will need
  anyway.
- (+) Multi-user host isolation is free (UDS filesystem permissions);
  no in-process auth code in v1.
- (−) Two new long-lived processes to keep alive on every nexus install.
- (−) Adds an RPC hop to every T2/T3 call — measured at +15 µs p50
  (UDS) / +29 µs p50 (TCP loopback) in the A3 spike; both sub-ms.
- (−) Daemon ↔ client version handshake is a new operational concern.
- (−) Containerised consumers require an orchestration step (UDS
  bind-mount or TCP port allow-list + env-var injection) at
  container launch. Documented in Day 2 Ops.
- (−) Cross-host co-work is explicitly *not* addressed; a future RDR
  layers federation on top of the local daemon.
- (−) **Ad-hoc DB access disappears when nexus runs inside its own
  container** — see "Nexus-in-its-own-container" subsection below.

### Nexus-in-its-own-container — interaction model shift

One deployment mode RDR-112 enables (and which the agentic-cockpit
roadmap anticipates) is **packaging nexus itself as a container** —
daemons, MCP server, CLI, and config inside a single OCI image that
consumers attach to via UDS bind-mount or TCP. In that configuration
**no out-of-band process on the host can reach the underlying SQLite
file or chroma directory**: `sqlite3 ~/.nexus/t2.db`, `chroma utils
info`, DBeaver, Datasette, ad-hoc `SELECT ... FROM memory WHERE ...`
are all unavailable without `docker exec`-ing into the nexus
container and even then constrained by what the image ships.

Today these out-of-band paths are heavily used. Debugging a stuck
indexer, sampling raw memory rows, inspecting a chroma collection,
running one-off analytical SQL across the seven domain stores — all
happen against the file directly. Containerising nexus closes those
paths by design. The daemon-as-arbiter discipline is the whole
point, but it means the daemon must **expose first-class
introspection surfaces** to replace what ad-hoc DB access provides
today.

**Anticipated surfaces** (to be specified during planning, not
locked here):

- `nx daemon t2 exec <SQL>` — execute a parameterised read-only
  query via RPC, output as JSON or table. Read-only by default;
  explicit `--write` flag for destructive ops gated on a
  confirmation prompt. Replaces `sqlite3 <file>` for the 90% case.
- `nx daemon t3 peek <collection>` — paged inspection of a
  collection's documents / metadata / embeddings. Replaces
  `chromadb` CLI inspection.
- `nx daemon t2 schema` / `nx daemon t3 schema` — print live schema
  (T2 tables, T3 collection metadata) so external tooling can be
  code-generated against the current shape.
- `nx daemon t2 export [--table N] [--format jsonl|csv|sqlite]` —
  snapshot dump to a path the daemon writes (mounted volume in
  container mode). Replaces sqlite3's `.dump`.
- `nx daemon t2 import` — reverse operation; required for backup
  restore parity.
- **Streaming event log** — RDR-111's ORB projection of T2 change
  events is the principled answer to "tail -f the database":
  subscribe to `T2.changed` events filtered by table/project,
  rendered in the cockpit UI or via `nx daemon t2 events --follow`.
- **Read-only side-channel socket** — optional second UDS bound to
  a `readonly` group, exposing only the read surface. Lets external
  tooling (Grafana, DataDog SQL exporter, custom dashboards) attach
  without write capability or in-daemon auth code.

**What this RDR commits to**: the introspection surface must be
**at least as expressive as `sqlite3 <file>` is today for read
paths** before nexus-in-a-container ships as a supported deployment
mode. Without that commitment, containerising nexus regresses
operational visibility — the opposite of what this RDR exists to
do.

**What this RDR explicitly defers**: an *interactive REPL* surface
(`nx daemon t2 repl` for a sqlite3-like prompt) is nice-to-have for
power users but not required for correctness. Planning picks it up
if cheap; otherwise it falls to a follow-on RDR. The pipe-friendly
`exec` subcommand is sufficient for scripted workflows.

### Risks and Mitigations

- **Risk**: Daemon crashes leave clients hanging.
  **Mitigation**: Health-check + auto-respawn; clients fail loud with
  recovery instructions rather than silent fallback to direct file.
- **Risk**: Auto-spawn races between concurrent first-use clients.
  **Mitigation**: Filesystem-lock-mediated spawn (the spawner takes
  a lock on the discovery file before forking).
- **Risk**: Sandbox containers with no host filesystem visibility at
  all cannot read the discovery file.
  **Mitigation**: `NX_T2_SOCK` / `NX_T2_ADDR` / `NX_T3_ADDR` env-var
  injection is a co-primary discovery path per the A4 finding —
  orchestrator captures the daemon's stdout-announced addresses and
  passes them into the container at launch. T2 daemon binds both UDS
  and loopback TCP so the orchestrator can pick whichever the
  container can actually reach: bind-mount the UDS path for OCI
  containers that allow mounts; rely on TCP for Windows-VM co-work,
  containerised Claude Desktop, or sandboxes that allow-list ports
  but not socket files. Cross-host co-work is still out of scope
  (TCP listener is loopback-only by default; binding non-loopback
  would require auth and is deferred to a future RDR).

### Failure Modes

- **Daemon down, client connects**: fail loud, suggest `nx daemon
  start`. Do not silently degrade.
- **Version mismatch**: client refuses to connect; report both versions.
- **Daemon healthy, data corrupt**: same as today (SQLite/chroma
  surface their own errors); the daemon does not mask them.

## Implementation Plan

### Prerequisites

- [ ] RDR-110 accepted (defines the atomic-take primitive the T2
  daemon must implement).
- [ ] All Critical Assumptions verified.

### Minimum Viable Validation

A single end-to-end demo: two `claude -p` sub-processes on the same
host, started in different working directories. One writes a memory
via `memory_put`; the other reads it via `memory_get`. Today this
fails (each gets its own T2). With this RDR, it succeeds.

### Phase 1: Code Implementation

To be expanded during `/nx:create-plan`.

### Phase 2: Operational Activation

To be expanded.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| T2 daemon | `nx daemon list` | `nx daemon info t2` | `nx daemon stop t2` | health-check RPC | snapshot of underlying SQLite |
| T3 daemon | `nx daemon list` | `nx daemon info t3` | `nx daemon stop t3` | health-check RPC | snapshot of underlying chroma dir |
| Discovery files | `ls ~/.config/nexus/*_addr.*` | `cat <file>` | rm on daemon stop | daemon-startup writes | n/a (ephemeral) |

### New Dependencies

Probably none — stdlib `socketserver` / `multiprocessing.connection`
for T2; chromadb itself for T3 (already a dep). To be confirmed.

## Test Plan

- **Scenario**: Two processes, distinct working dirs, same host —
  memory_put in one, memory_get in the other. — **Verify**: value
  is read back.
- **Scenario**: Daemon killed mid-operation. — **Verify**: client
  reports a clear error and does not silently fall back to direct
  file.
- **Scenario**: Two clients race to auto-spawn the daemon. —
  **Verify**: exactly one daemon runs; the loser connects to the
  winner.
- **Scenario**: Client built against daemon version N+1 connects to
  daemon version N. — **Verify**: handshake refuses with a clear
  version message.
- **Scenario**: RDR-110 atomic-take from two clients on the same
  template. — **Verify**: exactly one wins; the other blocks or
  fails per spec.

## Validation

### Testing Strategy

1. **Scenario**: cross-process memory visibility (MVV).
   **Expected**: visible.
2. **Scenario**: cross-*container* memory visibility (Claude Desktop
   ↔ host `nx` CLI).
   **Expected**: visible.
3. **Scenario**: daemon restart preserves data and notifies clients.
   **Expected**: clients reconnect transparently or fail-loud per
   policy.

### Performance Expectations

The RPC overhead on memory_get / search / store_get is the load-bearing
performance question. Will measure empirically during Spike A3.

## Finalization Gate

> Complete each item with a written response before marking this RDR
> as **Accepted**.

### Contradiction Check

To be filled at gate time.

### Assumption Verification

A1–A4 above; all currently Unverified.

### Scope Verification

Minimum Viable Validation (cross-process memory visibility) is in
scope, not deferred.

### Cross-Cutting Concerns

- **Versioning**: daemon-client handshake required; encoded in discovery file.
- **Build tool compatibility**: no new build-time deps anticipated.
- **Licensing**: no new third-party libs anticipated.
- **Deployment model**: daemons run on the host; clients run anywhere
  with a route to the host's socket/loopback.
- **IDE compatibility**: N/A.
- **Incremental adoption**: rollout gates on a `NX_STORAGE_MODE=daemon|direct`
  flag; default flips after MVV ships.
- **Secret/credential lifecycle**: N/A (local daemons, no auth in v1;
  see Open Questions below).
- **Memory management**: daemon long-lived; needs a memory budget.

### Proportionality

Document is intentionally a thinking-through-decision draft, not a
locked design. Right-size before gate.

## Open Questions

- **Authentication**: v1 assumes localhost-only and trusts every
  local user. Is that the right call, or do we need a per-user UID
  check on connect?
- **Cross-host**: every assumption here is single-host. What's the
  story when a co-worker runs nexus on a different machine? Out of
  scope for this RDR but worth naming.
- **Daemon-as-MCP**: should the T2 daemon eventually *be* the MCP
  server, collapsing two layers? Or are they intentionally distinct?
- **Migration**: how do existing on-disk T2/T3 stores get picked up
  by the new daemons? Presumably "the daemon opens the same path the
  old direct client did" — but worth being explicit.
- **Introspection-surface completeness**: the "at least as expressive
  as `sqlite3 <file>` for reads" bar is correct for the 90% case;
  the long tail (recursive CTEs against arbitrary user-supplied SQL,
  EXPLAIN QUERY PLAN, ATTACH DATABASE for cross-DB joins) needs an
  explicit decision during planning — accept the regression, ship
  an escape hatch (`nx daemon t2 exec --raw`), or keep direct-file
  access as a separately gated "debug" mode.
- **Container image surface**: if nexus ships as a container, which
  external paths must remain available — `/tmp` for downloads, the
  user's git repos for indexing, an output volume for `export`? The
  container's filesystem contract is part of the introspection story.

## References

- RDR-004: Four-Store Architecture (the original tier definitions)
- RDR-041: T1 Scratch Inter-Agent Context
- RDR-105: T1 Chroma Architecture — Env Passdown (the proof-of-concept
  for host-service-plus-discovery)
- RDR-108: Graph Identity Normalization (relies on a single catalog
  source-of-truth that this RDR's T2 daemon would arbitrate)
- RDR-110: Semantic Tuple Space (needs an arbiter to implement `in`)
- RDR-111: ORB — Observable Relay Bus (needs a publishing point at the
  storage edge)

## Revision History

- 2026-05-12 — Initial draft. Hal.
- 2026-05-12 — Research phase: A1 Documented (High), A2 Verified for
  containers / Inconclusive same-host (High/Med), A3 Inconclusive
  pending spike (Med), A4 Partially Refuted (High) — discovery needs
  dual-primary design. Findings in T2 `nexus_rdr/112-research-A{1..4}`.
- 2026-05-12 — A3 spike executed: UDS overhead +15 µs p50 on
  `memory_get`, TCP loopback +29 µs p50 with doubled p99 tail.
  A3 promoted to Verified.
- 2026-05-12 — Design: T2 daemon binds **both UDS and loopback TCP**.
  Client preference order is UDS → TCP. Reversed an earlier UDS-only
  lock after considering Windows-VM co-work, containerised Claude
  Desktop, and port-allow-listing sandboxes where UDS bind-mounts
  aren't an option. The A3 spike's ~14 µs gap between transports
  makes the fallback essentially free. T3 keeps loopback TCP
  (upstream chromadb constraint). TCP listeners are loopback-only;
  cross-host co-work is still a future RDR.
- 2026-05-12 — Added **Nexus-in-its-own-container** trade-off
  subsection anticipating the deployment mode where nexus itself is
  the container. Ad-hoc DB access (`sqlite3 <file>`, DBeaver, etc.)
  becomes unavailable; the daemon must therefore expose first-class
  introspection surfaces (`exec`, `peek`, `schema`, `export`,
  streaming events, optional read-only side socket) that are at
  least as expressive as direct-file reads. Cross-references RDR-110
  (atomic-take CAS unchanged — same SQLite handle, single process,
  daemon hosts it) and RDR-111 (event-streaming surface).
- 2026-05-12 — **Scope generalised**: RDR-112 covers *every*
  persistent shared-state store, not just T2 and T3. Initial draft
  scoped narrowly out of caution; an impact analysis (T2:
  `nexus_rdr/112-research-impact-inventory`) revealed `CatalogDB`
  (`src/nexus/catalog/catalog_db.py:250-255`) is structurally
  identical to a T2 domain store and equally vulnerable. Cherry-picking
  only T2/T3 would leave the same silo bug under another name and
  invite future stores to repeat the pattern. Title updated;
  Technical Environment lists in-scope stores explicitly;
  Existing Infrastructure Audit adds CatalogDB and a lint rule
  banning direct `sqlite3.connect` / `PersistentClient` outside
  daemon-internal code. Approach §3 names two planning-time options
  for CatalogDB (collapse into T2, or ship as third daemon).
  Discovery and lifecycle hooks (§6, §7) renumbered to reflect.
