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
related_rdrs: [RDR-004, RDR-041, RDR-105, RDR-108, RDR-110, RDR-111, RDR-113]
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
3. **CatalogDB collapses into T2**: `CatalogDB`
   (`src/nexus/catalog/catalog_db.py:250-255`) is structurally
   identical to a T2 domain store — own `sqlite3.connect`, own file,
   same threading discipline. Its schema-evolution cadence aligns
   with T2's (RDR-101 / RDR-103 / RDR-104 all touched both surfaces
   in lockstep), and `catalog_taxonomy` already lives in T2 as one
   of the seven domain stores. **Decision**: CatalogDB becomes the
   eighth T2 domain store, served from `nx daemon t2`. One daemon,
   one introspection surface, one discovery file. **Escape hatch**:
   if planning surfaces a concrete crash-isolation or hot-path
   contention reason to separate, the decision flips to a third
   daemon (`nx daemon catalog`) — but the burden of proof is on
   the separation, not on the collapse. The transport, discovery,
   and introspection design is the same either way.
4. **T1 stays put**: RDR-105's env passdown is already correct for T1.
   No change.
5. **Any future persistent store** is added as a domain inside an
   existing daemon (preferred) or as a new daemon (only if a
   first-class operational separation is needed). Direct
   `sqlite3.connect()` / `chromadb.PersistentClient()` outside
   `src/nexus/db/` daemon-internal code becomes a lint-checked
   anti-pattern. **Enforcement (committed)**:
   - **Tool**: `nx doctor --check-storage-boundary` — an AST-based
     scan modelled on the existing
     `tests/test_no_direct_catalog_writes_outside_projector.py`
     ε-lint (RDR-101 Phase 3 ε). The implementation lives at
     `src/nexus/commands/doctor.py` (a new check function) and
     scans `src/nexus/` for `sqlite3.connect(...)` and
     `chromadb.PersistentClient(...)` call sites outside the
     allowlisted prefix `src/nexus/db/`.
   - **Trigger**: `nx doctor` runs the check as part of its
     default audit pass; CI invokes `nx doctor
     --check-storage-boundary --fail-on-violation` as a
     dedicated step in the existing pytest workflow.
   - **Surfacing**: violations are listed by `file:line` with the
     offending call shape, mirroring the ε-lint output format.
     Exit code 1 from CI fails the build.
   - **Escape hatch**: a `# storage-boundary-allow: <reason>`
     line marker, ≥8-char reason, parallels the
     `# epsilon-allow:` mechanism already in the catalog lint.
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
7. **Event stream (wire contract)**: each daemon exposes a
   streaming RPC `EventStream(subspace_prefix, since_cursor) →
   Stream<Event>` over the same UDS/TCP transport as the regular
   RPCs. Event records carry `{cursor: rowid, subspace, op,
   payload_summary, ts}`. Cursor is monotonic `rowid > N` within
   a subspace; consumers persist their last-acked cursor and
   reconnect from there after a disconnect (at-least-once
   delivery). Subspace-prefix matching is glob-style (e.g.
   `tuples/*`, `daemon/*/lifecycle`). The stream is the **only**
   way clients observe change — direct SQL polling on the
   underlying SQLite is daemon-internal and forbidden to clients.

   **Reserved subspaces**:
   - `tuples/<subspace>` — tuple-space change events (RDR-110
     consumers; the projection RDR-111 §Step 6 watcher consumes).
   - `daemon/<name>/lifecycle` — substrate operational events
     (started, stopping, migration-applied, client-connected,
     client-disconnected, sweep-fired). First-class so a cockpit
     binding "show me when the daemon restarts" is expressible
     on the same bus as any other observation.

   **Failure-category demux**: events carry an optional
   `category ∈ {data, schema, substrate}` field. Consumers
   distinguish substrate failures (transport/daemon faults) from
   action failures (their own dispatch errors) — RDR-111
   §Watcher failure isolation depends on this demux to avoid
   auto-disabling user bindings when the daemon hiccups.

8. **Subspace registry — daemon validates**: tuple-space
   subspace schemas (RDR-110 §Subspace registry) ship with each
   daemon and are validated daemon-side at startup, not in the
   MCP client. The daemon-client version handshake carries a
   subspace-schema digest; client refuses to connect on
   mismatch. Rationale: the daemon owns the data and must
   enforce its own invariants regardless of which client
   connects. RDR-111's plugin-supplied subspaces (RDR-111 CA-9)
   land via the daemon's `nx daemon t2 subspace add <yaml>`
   admin RPC, not by the client registering at runtime.

9. **Schema migration ownership**: the daemon is the **sole
   migration runner**. On startup the daemon checks its own
   schema version and applies pending migrations against its
   single SQLite handle before accepting client connections.
   Clients carry an expected-schema-version constant; on
   connect, the handshake compares and fails loud if the daemon
   is behind (instruct user to `nx daemon t2 migrate`) or ahead
   (instruct user to upgrade the client). The MCP server stops
   driving migrations entirely (per RDR-110 Phase 1 Step 2 needs
   updating).

10. **Original "Lifecycle hooks" promise**: superseded by items
    7–9 above, which spell out the event-stream protocol,
    failure demux, registry validation, and migration ownership
    explicitly. T2, CatalogDB, and T3 all participate in the
    EventStream RPC; same wire contract per daemon.

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

### Sequencing constraint vs RDR-110 — `block=True` gates on the daemon

RDR-110's blocking `take` semantics (`block=True`) depend on
`PRAGMA data_version` polling against the `tuples.db` SQLite file
(RDR-110 §RF-9, §CA #6 Verified). The cross-process wake claim in
CA #6 is correct for same-host shared filesystem; it is **not**
correct across container overlayfs bind mounts — the same `mmap`
constraint RDR-112 §A2 verified for WAL writes also blocks
`data_version`'s cross-process visibility. Naively shipping
RDR-110 against direct-file T2 first (the obvious "least
disruptive" sequencing) would silently break blocking `take` for
containerised consumers — Claude Desktop bundled MCP,
`claude -p`, ext-app sandboxes — with no diagnostic, because each
container would observe its own forked `data_version`.

**The corrected sequencing**:

1. RDR-110 Phase 1 ships with `block=False` only (polling-based
   take). Same-host same-process correctness, no container
   exposure to the `data_version` failure mode.
2. RDR-110's `block=True` is gated behind
   `NX_STORAGE_MODE=daemon` (see Cross-Cutting Concerns §
   Incremental adoption) and ships only after the T2 daemon's
   blocking-take RPC is in place. The daemon implements
   `data_version` polling internally against its single-process
   SQLite handle, then delivers the wake to clients over UDS/TCP.
3. Non-blocking `take` (`block=False`) is safe under both modes
   and ships as part of RDR-110 Phase 1 unchanged.

This constraint is **load-bearing for RDR-110's correctness** in
the containerised case and must be communicated to RDR-110's
planning chain before Phase 1 Step 4 (the blocking-take
implementation) ships. The constraint costs RDR-110 nothing
operationally — `block=False` is the v1 default already; this RDR
merely defers the optional `block=True` path to when the
substrate supports it.

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

**How the commitment is kept**: `nx daemon t2 exec --raw <SQL>`
forwards an arbitrary read-only SQL string through to the daemon's
SQLite handle and streams results back as JSON or tabular output.
The `--raw` flag is the audit-trail marker that opts out of the
parameterised-query path. Read-only is enforced by the daemon
opening a second connection in `mode=ro` for `--raw` execution,
not by SQL pattern matching (which is brittle). This closes the
long tail — recursive CTEs, `EXPLAIN QUERY PLAN`, anything the
underlying SQLite supports — at the cost of one extra read-only
connection per `--raw` invocation. `ATTACH DATABASE` for cross-DB
joins is **out of scope** for v1 (would require the daemon to
expose multiple file paths to clients, which conflicts with the
"daemon owns the substrate" discipline); follow-on RDR if needed.

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
- [x] RDR-110 planning chain notified of the `block=True` sequencing
  constraint — recorded 2026-05-13 in RDR-110 §Revision History
  ("Post-acceptance cross-reference 2026-05-12" subsection,
  "Revised preference (2026-05-13, RDR-112 gate round 2)" block).
  RDR-110 Phase 1 Step 4 ships `block=False` unconditionally and
  `block=True` feature-flagged off until `NX_STORAGE_MODE=daemon`.
- [x] All Critical Assumptions verified (A1 Documented, A2 Verified
  for containers, A3 Verified via spike, A4 Refuted-then-redesigned).

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

The RPC overhead on `memory_get` / `search` / `store_get` was the
load-bearing performance question; the A3 spike closed it. Results
(2000 iters over real `MemoryStore` on a tmp SQLite, 500 rows):
direct in-process p50 = 85 µs; UDS p50 = 100 µs (+15 µs); TCP
loopback p50 = 114 µs (+29 µs). All sub-millisecond. T3 store ops
are dominated by embedding latency (ONNX 10–50 ms, Voyage
100–300 ms); the transport hop is below noise. Full data in T2
`nexus_rdr/112-research-A3-spike`.

## Finalization Gate

> Complete each item with a written response before marking this RDR
> as **Accepted**.

### Contradiction Check

**One tension surfaced and resolved.** RDR-110 §CA #6 (Verified)
states that SQLite `PRAGMA data_version` is "visible across
processes" — true for same-host shared filesystem. RDR-112 §A2
(Verified) establishes that container overlayfs bind mounts break
the `mmap` semantics WAL relies on, which equally breaks
`data_version`'s cross-process visibility. The two claims are
both correct within their respective scopes; the apparent
contradiction is a scope-tightening, not a refutation. **Resolved
by sequencing constraint** in §Decision Rationale: RDR-110's
`block=True` (the only consumer of `data_version` polling) gates
on `NX_STORAGE_MODE=daemon` and ships only after the T2 daemon's
blocking-take RPC. `block=False` and the rest of RDR-110 are
unaffected. No other contradictions found between Research
Findings, Proposed Solution, and the cross-referenced RDRs
(110, 111, 105, 108).

### Assumption Verification

All four Critical Assumptions are closed:

- **A1** (`chroma run` production quality): **Documented** (High
  confidence) — Source Search of `chromadb/app.py` (FastAPI +
  uvicorn, 40-thread pool) and live precedent in nexus T1
  (`session.py:494-504`). T2 entry `112-research-A1`.
- **A2** (WAL not a substitute): **Verified** for containers
  (High confidence) — overlayfs blocks WAL `mmap`; same-host
  remains correct. T2 entry `112-research-A2`.
- **A3** (RPC overhead acceptable): **Verified** (High
  confidence) — spike (`/tmp/rdr112_a3_spike.py`, 2000 iters):
  UDS +15 µs p50, TCP loopback +29 µs p50. Both sub-ms. T2
  entries `112-research-A3` and `112-research-A3-spike`.
- **A4** (per-host UID discovery file works universally):
  **Partially Refuted** (High confidence) — fails in
  PID-namespaced containers. Design revised to dual-primary
  (file + env var) co-equal paths. T2 entry `112-research-A4`.

No load-bearing assumption remains Unverified.

### Scope Verification

Minimum Viable Validation (two `claude -p` sub-processes on the
same host share memory via `memory_put` / `memory_get`) is in
scope, not deferred. The cross-process T2 visibility test is the
single end-to-end proof and ships with the daemon.

### Cross-Cutting Concerns

- **Versioning**: daemon-client handshake required; encoded in
  the discovery file's `version` field. Mismatch fails loud.
- **Build tool compatibility**: no new build-time deps
  anticipated; stdlib `socketserver` / `multiprocessing.connection`
  cover the T2 transport.
- **Licensing**: no new third-party libs anticipated.
- **Deployment model**: daemons run on the host; clients run
  anywhere with a route to the host's UDS path or loopback TCP.
- **IDE compatibility**: N/A.
- **Incremental adoption**: rollout gates on
  `NX_STORAGE_MODE=daemon|direct`. Semantics:
  `direct` retains pre-112 file-handle behaviour (debug /
  migration only); `daemon` requires a running daemon (fail-loud
  if absent — **no auto-spawn in daemon mode** to avoid silent
  ambiguity about which mode the client is in). Default is
  `direct` until MVV passes, then flips to `daemon`. The flip is
  the cutover marker; a `direct` fallback remains available
  indefinitely for debugging but is not the recommended path
  post-cutover.
- **Secret/credential lifecycle**: N/A in v1 — local daemons,
  UDS uses unix file permissions (`chmod 0600` + peer-credential
  check on accept; see **RDR-113** for the v1 host-trust model),
  TCP listeners are loopback-only. Future cross-host RDR will
  introduce token-based auth.
- **Memory management**: daemon long-lived; needs a memory
  budget (to be set during planning based on expected concurrent
  client count and result-set sizes).

### Proportionality

Document covers a structural shift in how every persistent
shared-state store is accessed; size is justified. Research
findings, assumption ledger, impact inventory, and gate
finalisation are all evidence-backed. The remaining "to be
expanded during `/nx:create-plan`" markers in §Implementation
Plan Phase 1 / Phase 2 are appropriate — phase decomposition is
the planner's job, not this RDR's.

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
- 2026-05-13 — **Gate round 1 BLOCKED** (substantive-critic
  `a67d3d730415cb2c8`): 2 critical, 3 significant, 3 observations.
  Round-1 closeout this revision:
  - C1 (RDR-110 `block=True` sequencing creates container soak-bug
    via `data_version` overlayfs failure): **resolved** by new
    §Decision Rationale "Sequencing constraint vs RDR-110"
    subsection and Implementation Plan Prerequisites — `block=True`
    gates on `NX_STORAGE_MODE=daemon`, Phase 1 ships `block=False`
    only. RDR-110 planning chain to be notified.
  - C2 (Finalization Gate boilerplate contradicted body):
    **resolved** by rewriting Contradiction Check, Assumption
    Verification, Scope Verification, Cross-Cutting Concerns, and
    Proportionality with current-state evidence.
  - S1 (CatalogDB punt lacked criteria): **resolved** by §Proposed
    Solution §3 committing to collapse into T2 as eighth domain
    store; planning-time flip allowed only on concrete crash/
    contention evidence.
  - S2 (introspection commitment under-backed): **resolved** by
    `nx daemon t2 exec --raw` commitment plus read-only-connection
    enforcement; `ATTACH DATABASE` explicitly out of v1. Open
    Question retired.
  - S3 (`NX_STORAGE_MODE` underspecified): **resolved** in
    Cross-Cutting Concerns — `direct` retains pre-112 behaviour,
    `daemon` fails loud (no auto-spawn), default flips post-MVV.
  - O1, O2, O3 (RDR-111 consistency, read-only side socket loopback
    constraint, stale "Spike A3 pending" forward reference):
    Performance Expectations rewritten with measured A3 data;
    other two folded into existing prose. No structural changes
    needed.
- 2026-05-13 — **Gate round 2 BLOCKED** (substantive-critic
  `a737a8c724835c148`): 1 critical, 1 significant, 3 observations
  (closeout verifications). Round-2 closeout this revision:
  - C1' (round-2): RDR-112's "ship `block=True` only behind
    daemon" claim contradicted RDR-110's existing revision-history
    preference for Option 1 (ship direct-file first); the C1
    closeout had written one side of the constraint without
    updating the other. **Resolved** by editing RDR-110's
    "Post-acceptance cross-reference" subsection to record the
    revised hybrid ordering (`block=False` ships in Phase 1
    unconditionally; `block=True` lands as feature-flagged code
    gated behind `NX_STORAGE_MODE=daemon`). The CAS primitive is
    unchanged in either path. Both documents now agree.
  - S1' (round-2): Implementation Plan prerequisite bullet
    "RDR-110 planning chain notified" was unchecked.
    **Resolved** — RDR-110 revision history updated 2026-05-13;
    bullet checked with a forward reference to the specific
    subsection.
  - All round-1 closeouts (C2/S1/S2/S3/O1/O2/O3) verified clean
    by round-2 critic; no regressions introduced by round-1
    edits.
- 2026-05-13 — **Triad rework (110/111/112 composition gaps)**:
  deep-analyst pass `a56172936d63fd480` surfaced 10 gaps between
  the three RDRs that local gates couldn't see. User chose deep
  rework (option D). Three load-bearing decisions:
  - **D1**: MCP server is a **client** of the daemon, not the
    daemon itself. Recorded here; closes G10. RDR-110/111
    watcher placement updates follow.
  - **D2**: Event bus is the `EventStream(subspace_prefix,
    since_cursor)` streaming RPC on the daemon (new Approach
    §7 above). Closes G2 (RDR-111 watcher subscription protocol)
    and G8 (daemon lifecycle events first-class on same bus).
  - **D3**: Under `NX_STORAGE_MODE=daemon`, **no client ever
    touches the SQLite file**. Direct-file mode is single-host
    single-filesystem only. Recorded in RDR-110 revision-history
    "Further-revised scope" block. Closes G1 (overlayfs
    correctness extends past `data_version` to the entire CAS
    primitive, sweeps, audit).
  This revision also adds Approach §8 (subspace registry —
  daemon validates, closing G3), Approach §9 (daemon is sole
  migration runner, closing G4), failure-category demux on the
  event stream (closing G5 with the matching consumer-side
  change in RDR-111), and forward-references RDR-113 for host
  trust / UDS auth (closing G6). G7 closes via a one-line
  forward-reference in `docs/workflow-engine-synthesis.md`. G9
  (cross-triad integration test) becomes a planning-time bead.
- 2026-05-13 — **Light re-gate post-triad-rework PASSED**
  (substantive-critic `abbc3adfbe8d94ca2`, 0C/1S/1O). Critic
  confirmed §7-§10 integrate cleanly with §1-§6; cross-RDR
  consistency with RDR-111 (EventStream signature, CA-9, CA-10)
  solid; R3 PASSED items intact. Closeouts in this revision:
  - S1 (Approach §5 lint-rule enforcement was an unresolved
    disjunction predating the rework): committed to
    `nx doctor --check-storage-boundary` as the concrete tool,
    AST-scan modelled on the existing RDR-101 Phase 3 ε-lint at
    `tests/test_no_direct_catalog_writes_outside_projector.py`.
    Trigger (CI step + `nx doctor` default audit), surfacing
    (file:line + exit 1), and escape hatch
    (`# storage-boundary-allow: <reason>`, ≥8-char) all named.
  - O1 (RDR-113 forward-reference lived only in revision
    history + frontmatter): added a body-prose cite in
    Cross-Cutting Concerns §Secret/credential lifecycle so the
    cross-reference is navigable without a revision-history
    dig.
