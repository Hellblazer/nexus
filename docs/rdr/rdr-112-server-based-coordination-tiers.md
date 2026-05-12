---
title: "Server-Based Coordination Tiers for Cross-Host Nexus Deployment"
id: RDR-112
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-12
related_issues: []
related_rdrs: [RDR-094, RDR-105, RDR-110, RDR-111]
related_tests: []
implementation_notes: ""
---

# RDR-112: Server-Based Coordination Tiers for Cross-Host Nexus Deployment

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus's coordination tiers are file-backed and host-local. T2 lives at
`~/.config/nexus/memory.db` (plus six adjacent SQLite domain stores).
T3 in local mode lives at `~/.config/nexus/chroma/` as a ChromaDB
`PersistentClient` directory. Both tiers assume every process that
needs to read or write shares the same filesystem on the same host.
SQLite WAL is multi-writer safe only under that assumption — the
SQLite team's own documentation says WAL "does not work over a
network filesystem" and that processes accessing the same database
must be on the same host computer.

Cowork — the Claude Code feature that runs a second agent inside an
isolated Ubuntu 22.04 VM via Apple's Virtualization framework — is
the proximate driver. A Cowork session's `nx-mcp` lives inside a VM
with its own filesystem at `/sessions`. It cannot reach the host's
`~/.config/nexus/`. The result today: every Cowork session starts with
empty state, cannot query the host's indexed corpus, cannot share T2
memory or beads with the host, and cannot coordinate hook events with
the host agent. This blocks Cowork from being a useful deployment
target for nexus-driven workflows.

The same constraint generalises beyond Cowork: any container
deployment, any second workstation, any sandboxed subprocess that has
its own filesystem view is locked out of host nexus state. Sharing
storage through a network filesystem (NFS, SMB, virtio-fs read-write)
is not a fix — the SQLite team explicitly warns against it because
file-locking semantics across the boundary are unreliable and WAL
breaks. This is the canonical "must use client/server" situation.

### Enumerated gaps to close

#### Gap 1: T2 is unreachable from a different filesystem

The seven T2 domain stores (`memory_store`, `telemetry`,
`catalog_taxonomy`, `plan_library`, `chash_index`, `document_aspects`,
`aspect_extraction_queue`) each open a local SQLite connection via
`sqlite3.connect` against paths under `~/.config/nexus/`. A process
running in a Cowork VM resolves `~/.config/nexus/` to a VM-local
directory; there is no transport for the VM-side `nx-mcp` to read or
write the host's T2 state. RDR-105 designated T2 as "the shared bus
across all processes" — that designation breaks at the VM boundary.

#### Gap 2: T3 local mode is unreachable from a different filesystem

T3 in local mode is a ChromaDB `PersistentClient` rooted at the host's
local chromadb directory. Same shape as Gap 1: the VM cannot open a
file handle to the host's chroma store. T3 cloud mode (ChromaDB Cloud
+ Voyage) is already network-reachable from anywhere, so it sidesteps
this gap — but at the cost of an external dependency and per-call API
spend. Local-mode T3 is the canonical path for users who run nexus
without a Voyage subscription, and it must work for Cowork.

#### Gap 3: No discovery channel for VM-side processes to find host services

Even with a server-mode T2 and a server-mode T3 running on the host,
the VM-side `nx-mcp` needs to know where to connect. RDR-105
established env-passdown (`NX_T1_HOST` / `NX_T1_PORT`) and PID-file
(`~/.config/nexus/t1_addr.<pid>`) as the discovery mechanisms for T1,
but both assume host-local processes. Cross-VM discovery needs an
explicit channel: either coworkd's CLI-plugin socket relaying
endpoint information at session start, or a stable host-bridged
address (the VM's gateway IP, 172.16.10.1, is a known fixed value)
plus a token-exchange protocol.

#### Gap 4: No authentication model for cross-trust-boundary access

Host-local nexus has no authentication — every process on the host is
implicitly trusted because filesystem permissions gate access. Once
T2 and T3 are reachable over a network port, that implicit trust
model fails. A Cowork VM is a trust boundary (sandboxed subprocess
with its own identity); a future remote workstation is a stronger one.
RDR-112 must define how clients authenticate to the host's tier
servers without bolting on a heavy identity system.

## Context

### Background

The gap was surfaced concretely during work on RDR-110 (the semantic
tuple space) and RDR-111 (the ORB cockpit substrate). Both depend on
cross-process coordination at the project tier, which RDR-105
designated as T2 (SQLite WAL). Mid-RDR-111-review, the user noted
that Cowork's VM boundary defeats SQLite WAL's multi-writer
guarantees — WAL requires shared memory across processes and shared
memory does not exist across VM boundaries. The architectural fix
applies not only to RDR-110 and RDR-111 but to nexus's entire
coordination story: any time we want a second nexus-aware process to
participate, and that process lives in a different filesystem, the
current design fails.

This RDR pre-empts a class of architectural failures rather than
patching one specific feature. It stands alone — even if RDR-110 and
RDR-111 never ship, RDR-112's value is "Cowork can use nexus."

### Technical Environment

- Python 3.12+, `chromadb` (pinned), `sqlite3` from the Python stdlib
- T2 storage: `~/.config/nexus/memory.db` + six sibling `.db` files,
  all SQLite WAL mode
- T3 storage: `~/.config/nexus/chroma/` (local mode, `PersistentClient`)
  or ChromaDB Cloud (`CloudClient`)
- T1 already runs as a per-session ChromaDB HTTP server (per RDR-094
  Phase 4 and RDR-105 P4); discovery via env-passdown and PID files
- Cowork: Ubuntu 22.04 arm64 VM on Apple Virtualization framework,
  hostname `claude`, per-session ext4 disk at `/sessions`, static IP
  `172.16.10.3/24` with gateway `172.16.10.1`, network egress via a
  MITM proxy with ephemeral CA in the system trust store, host RPC
  over vsock CID=2 port=51234, host filesystem read-only at
  `/mnt/.virtiofs-root`

## Research Findings

### Investigation

Three sources informed the design:

- The SQLite team's official guidance on network use (`sqlite.org/
  useovernet.html`): "if your data is separated from the application
  by a network, use a client/server database." Three acceptable
  patterns: (1) real client/server, (2) WAL mode with a proxy relay
  on the database machine, (3) rollback mode with reader-or-writer
  semantics.
- A 2026-state-of-the-art survey of distributed-SQLite projects:
  libSQL/sqld (Turso), rqlite, dqlite, LiteFS, cr-sqlite, Cloudflare
  D1. Maturity, deployment model, and operational footprint each
  vary. See "Decision Rationale" for the comparison.
- The cowork VM environment inspection (boot log, allowed-ops
  manifest, mount inventory) to verify network paths, trust
  boundaries, and the available host-VM channels.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| libSQL / sqld | No — docs only | Hrana protocol (HTTP+WS), Ed25519 JWT auth, BETA-but-production. Source verification deferred to Phase 1 spike. |
| ChromaDB server mode | Partial — we already operate it via RDR-094/105 for T1 | HTTP server, no built-in auth (deployment-time concern), `chromadb.HttpClient` is the canonical client. |
| Apple Virtualization framework vsock + MITM proxy | Inspected via coworkd boot log | vsock CID=2 reachable at host RPC port 51234; HTTPS traffic from VM intercepted by `/var/run/mitm-proxy.sock` with auto-trusted ephemeral CA. |
| SQLite WAL semantics | Source-confirmed in sqlite.org docs | WAL multi-writer safety requires shared memory; explicitly does not cross VM/network boundaries. |

#### T2 Research Index

Seven structured research findings recorded in T2 (project
`nexus_rdr`, retrievable via `nx memory get --project nexus_rdr
--title 112-research-<topic>`). Each maps to one or more of the
Critical Assumptions below and is classified by evidence basis.

| T2 Title | Classification | Maps to CA | Spike-required? |
| --- | --- | --- | --- |
| `112-research-sqlite-network-guidance` | Verified | foundational (no CA) | No — authoritative source |
| `112-research-libsql-sqld-transport` | Documented | CA-1, CA-2, CA-6, CA-7 | Yes |
| `112-research-cowork-vm-environment` | Verified | CA-4, CA-5 (Gap 3) | Partial — source-search for CA-5 |
| `112-research-nexus-t2-facade-structure` | Verified | Existing-infra-audit support | No |
| `112-research-2026-distributed-sqlite-ecosystem` | Documented | Decision-rationale support | No |
| `112-research-chromadb-server-mode-performance` | Documented | CA-3 | Yes |
| `112-research-sqld-concurrency-limits` | Documented | CA-7 | Yes |

The seven findings collectively cover every Critical Assumption.
Verified items rest on direct source inspection (SQLite docs, the
nexus codebase, the coworkd boot log). Documented items rest on
official-but-secondary sources (Turso engineering posts, ChromaDB
performance docs) and require spike-verification before
implementation commits — see each CA's Method field.

### Key Discoveries

- **Documented** — SQLite team's prescription matches our situation
  exactly. "Many client programs sending SQL to the same database
  over a network" → use client/server. There is no fix that keeps
  the file-shared-WAL model.
- **Documented** — libSQL/sqld is a Turso-led fork of SQLite with a
  server mode using the Hrana protocol over HTTP+WebSocket. It
  preserves SQLite on-disk format and SQL semantics; transactions
  and WAL continue to operate on the server side. Single binary,
  Docker-deployable, JWT (Ed25519) auth in-box, embedded-replica
  support as a future optimization. BETA but in commercial production
  at Turso.
- **Documented** — ChromaDB ships a server mode we already run for T1
  per RDR-094/105. The same pattern transfers to T3 local-mode:
  spawn `chroma run` on the host, clients use `chromadb.HttpClient`.
- **Verified** — The Cowork VM has direct IP-level connectivity to
  the host's bridged gateway (`172.16.10.1`). The MITM proxy
  auto-trusts its ephemeral CA in the VM's system store, so HTTPS
  traffic crosses the boundary without certificate issues. No new
  network plumbing required.
- **Verified** — Existing nexus T2 access patterns use the
  `T2Database` facade in `src/nexus/db/t2/`. Each domain store opens
  its own `sqlite3.Connection`. A single point of substitution at
  connection-construction time is sufficient to swap to libSQL's HTTP
  client without changing any consumer's API surface. This is a
  contained refactor, not a rewrite.
- **Assumed** — libSQL's Hrana protocol preserves the exact
  transactional semantics nexus relies on (BEGIN IMMEDIATE,
  `ON CONFLICT DO NOTHING RETURNING`, FTS5 queries, `json_extract`,
  recursive CTEs). Needs verification spike (CA-1).
- **Assumed** — Hrana over localhost has latency within budget for
  nexus's tightest read paths (`scratch` get, `memory_get` exact-
  title lookup, telemetry write). Budget target: 95th percentile
  < 5ms for single-row gets, < 20ms for typical FTS5 queries. Needs
  benchmark spike (CA-2).
- **Assumed** — ChromaDB HTTP server-mode performance is acceptable
  for T3 read patterns (`coll.query`, `coll.get`, `coll.add` in
  batches up to 300). We have existing operational evidence from T1
  but not at T3 corpus sizes (~290k chunks per `T3 metadata audit
  2026-05-08`). Needs benchmark spike (CA-3).

### Critical Assumptions

- [ ] **CA-1**: libSQL/sqld Hrana protocol preserves the
  transactional and SQL-feature surface area nexus's seven T2 domain
  stores require. — **Status**: Unverified — **Method**: Spike
  (run nexus T2 test suite against a local sqld instance; fix any
  divergences; document gaps). Gates Phase 1.
- [ ] **CA-2**: Hrana over localhost latency is within budget for
  nexus's tightest read paths (target P95 < 5ms single-row, < 20ms
  FTS5). — **Status**: Unverified — **Method**: Spike (microbenchmark
  on representative workload; measure overhead vs direct sqlite3).
  Gates Phase 1.
- [ ] **CA-3**: ChromaDB HTTP server-mode performance at our T3
  corpus size (~290k chunks across ~95 collections) is acceptable
  for nexus query patterns. — **Status**: Unverified — **Method**:
  Spike (load representative subset into chroma server, exercise
  `coll.query` / `coll.get` / `coll.add` patterns, compare against
  PersistentClient baseline). Gates Phase 3.
- [ ] **CA-4**: JWT (Ed25519) auth between Cowork VM and host sqld
  is operationally workable without a key-rotation story in v1. Single
  static key per VM, issued at session start by the host, valid for
  the session lifetime. — **Status**: Unverified — **Method**: Docs
  Only (libSQL docs confirm Ed25519 PKCS#8 or URL-safe base64); spike
  to confirm token validation behaviour and revocation semantics.
  Gates Phase 4.
- [ ] **CA-5**: The host-VM bridged IP (`172.16.10.1`) is stable
  across Cowork session restarts within the same nexus host
  installation. — **Status**: Unverified — **Method**: Source Search
  on coworkd networking config; if not stable, discovery must use
  the coworkd CLI-plugin socket relay instead. Gates Phase 4.
- [ ] **CA-6**: sqld can persist to the existing
  `~/.config/nexus/memory.db` + sibling `.db` files without a format
  migration. (libSQL claims 100% SQLite compatibility on-disk.) —
  **Status**: Unverified — **Method**: Spike (point sqld at an
  existing nexus memory.db, run read + write workload, verify no
  corruption). Gates Phase 1.
- [ ] **CA-7**: A single host-wide sqld daemon handling all T2 traffic
  for all `nx-mcp` processes (host + VMs + future containers) does
  not exceed sqld's documented concurrency / connection limits in
  realistic nexus workloads (10s of concurrent connections, hundreds
  of writes/sec at peak). — **Status**: Unverified — **Method**: Spike
  load test. Gates Phase 1.

## Proposed Solution

### Approach

Move T2 and local-mode T3 from file-backed clients to server-mode
daemons running on the host. Host-side and VM-side `nx-mcp`
processes both become network clients of those daemons. T1 retains
its existing per-session-Chroma model (it is already process-local by
design; cross-process T1 visibility is explicitly out of scope per
RDR-094).

Three daemons on the host:

- **sqld** (libSQL daemon) — owns `~/.config/nexus/memory.db` plus the
  six sibling SQLite files. Hrana protocol over HTTP+WebSocket on
  `localhost:<port>` (host-side clients) and `172.16.10.1:<port>`
  (VM-side clients). Single binary, supervised by the first
  `nx-mcp` process on the host (existing T1 chroma supervision
  pattern from RDR-094 Phase 4, generalised to host-wide rather than
  per-session).
- **chromadb HTTP server** — owns the T3 local-mode persistent
  directory. Spawned by the same first `nx-mcp` if T3 is in local
  mode. Cloud-mode T3 needs no daemon (it already talks to ChromaDB
  Cloud over the network).
- **T1 chromadb HTTP server** — unchanged from RDR-094/105. Per-session,
  per-`nx-mcp`-process. Continues to use env-passdown and PID-file
  discovery within a single host. VMs run their own T1 (no cross-VM
  T1 visibility — that is correct, T1 is session-scoped).

Daemon lifecycle is **host-wide single-instance**, not per-session.
The first `nx-mcp` on the host wins a singleton race (`flock` on
`~/.config/nexus/sqld.lock` and `chroma.lock`); subsequent `nx-mcp`s
discover the running daemon via a PID file
(`~/.config/nexus/sqld_addr.json` containing `{pid, host, port, token,
created_at}`). When the winner exits, an elected successor takes
over supervision (the next `nx-mcp` that finds a stale PID file).
This avoids per-session restart churn and gives Cowork VMs a stable
endpoint to connect to.

### Technical Design

**Layered architecture (after RDR-112):**

```text
┌─────────────────────────────────────────────────────────────────┐
│ nx-mcp (host)                  nx-mcp (Cowork VM)               │
│   T2Database facade              T2Database facade              │
│   T3Database facade              T3Database facade              │
│       │   │                          │   │                      │
│       │   │ libsql.Client            │   │ libsql.Client        │
│       │   │ chromadb.HttpClient      │   │ chromadb.HttpClient  │
│       ▼   ▼                          ▼   ▼                      │
│ ╔════════════════════╗         ╔════════════════════╗           │
│ ║   localhost:NN     ║         ║  172.16.10.1:NN    ║           │
│ ║   localhost:MM     ║         ║  172.16.10.1:MM    ║           │
│ ╚════════════════════╝         ╚════════════════════╝           │
│        host network                 vsock-bridged network       │
│                                                                 │
│   sqld (port NN, host-wide singleton, supervises memory.db)     │
│   chromadb (port MM, host-wide singleton, supervises T3 local)  │
└─────────────────────────────────────────────────────────────────┘
```

**Client substitution points:**

| Facade | Today | After RDR-112 |
|---|---|---|
| `T2Database` (`src/nexus/db/t2/__init__.py`) | `sqlite3.connect(path)` | `libsql.connect(url, auth_token=...)` |
| `T3Database` local mode (`src/nexus/db/t3.py`) | `chromadb.PersistentClient(path=...)` | `chromadb.HttpClient(host=..., port=..., headers={"Authorization": f"Bearer {token}"})` |
| `T3Database` cloud mode | `chromadb.CloudClient(...)` | unchanged |
| `T1Database` (`src/nexus/db/t1.py`) | `chromadb.HttpClient` (per-session) | unchanged |

All four facades expose the same public method surface to nexus
consumers as today. The substitution lives entirely inside the
facade constructors; no caller changes.

**Discovery surfaces:**

- **Host process →** reads `~/.config/nexus/sqld_addr.json` (and
  `chroma_addr.json` for T3 local mode). If absent or stale (PID
  doesn't exist), claim the singleton lock and spawn the daemon,
  then write the PID file. Subsequent processes read the file.
- **Cowork VM process →** receives `NX_T2_URL`, `NX_T2_TOKEN`,
  `NX_T3_URL`, `NX_T3_TOKEN` via env-passdown from the cowork
  session-start hook. Falls back to `172.16.10.1:<well-known-port>`
  with a session-issued JWT if env vars are missing (token retrieved
  via coworkd's CLI-plugin socket).
- **Generic remote process →** explicit configuration (env or
  `~/.config/nexus/server.toml`). No magic discovery.

**Authentication model:**

- v1 ships single-tenant JWT (Ed25519) with two roles: `host`
  (full access, no expiry) and `cowork` (scoped to a session,
  TTL = session duration, default 8h). Keys live in
  `~/.config/nexus/auth.key` (Ed25519 PKCS#8 PEM, 0600).
- The host's first-started `nx-mcp` generates the auth key if
  absent, issues itself a `host` token at startup, and grants
  `cowork` tokens on demand via the coworkd CLI-plugin channel.
- No multi-user, no fine-grained ACLs, no key rotation in v1.
- Reasoning: nexus is a single-user tool. The trust model is "this
  user's processes, possibly across machines this user controls."
  RBAC and rotation can land in a follow-on RDR if/when a
  multi-user deployment ships.

**Code guidance** (interfaces only, not implementations):

```text
# src/nexus/db/_servers.py — new module
class ServerHandle(Protocol):
    @property
    def url(self) -> str: ...
    @property
    def token(self) -> str: ...
    def health(self) -> bool: ...

def discover_or_spawn_t2_server(*, config_dir: Path) -> ServerHandle: ...
def discover_or_spawn_t3_server(*, config_dir: Path) -> ServerHandle | None: ...
# returns None when T3 cloud mode is configured

# Facade construction (replaces direct sqlite3.connect):
class T2Database:
    def __init__(self, *, server: ServerHandle | None = None) -> None: ...
    # When server is None, falls back to direct sqlite3.connect for
    # tests / migration. Production paths always pass a ServerHandle.
```

The fallback to direct `sqlite3.connect` exists only to keep the
existing unit test suite (which uses `tmp_path` SQLite files) working
without spinning up a daemon. Production runtime always goes through
the server.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| sqld supervisor | `src/nexus/mcp_infra.py` (FastMCP lifespan + T1 chroma supervisor) | Extend: add a host-wide-singleton variant alongside the per-session pattern; share the spawn/wait/healthcheck helpers. |
| sqld discovery / PID file | `src/nexus/session.py` (T1 hybrid discovery) | Extend: same pattern (PID-file + env-var fallback) for sqld. |
| Chroma HTTP server supervisor | RDR-094 Phase 4 implementation in `src/nexus/mcp_infra.py` | Reuse: the existing supervisor already speaks `chroma run`; generalise to also supervise the T3-local-mode singleton. |
| `libsql` Python client | not present | Add: new dep, pinned. (Pure Python wheel from `pypi.org/project/libsql/`.) |
| `T2Database` client connection | `src/nexus/db/t2/*.py` (seven domain stores, each calls `sqlite3.connect`) | Extend: route through a single connection factory that returns either `libsql.Client` or `sqlite3.Connection` based on `ServerHandle` presence. Minimal blast radius. |
| `T3Database` local-mode client | `src/nexus/db/t3.py` `PersistentClient` branch | Replace: swap the `PersistentClient` constructor for `HttpClient` against the supervised chroma server. |
| Auth key generation | not present | Add: tiny wrapper around `cryptography.hazmat.primitives.asymmetric.ed25519` to generate / read PKCS#8 keys. Stable, well-known API. |
| Cowork VM endpoint discovery | not present | Add: coworkd CLI-plugin client (talks `/run/coworkd/cli-plugin.sock` inside the VM) + env-passdown receiver. New module under `src/nexus/cowork/`. |

### Decision Rationale

The transport choice for T2 is the load-bearing decision. State of
the art in May 2026 puts five projects on the shortlist:

| Project | Model | Pros | Cons | Fit |
|---|---|---|---|---|
| **libSQL / sqld** | SQLite fork + Hrana HTTP/WS server | SQLite-compat; single binary; JWT in-box; embedded-replica future-proofing; commercially production-deployed | BETA pre-1.0; ecosystem still maturing | **Chosen.** |
| rqlite | Multi-primary via Raft consensus | Production-mature; HTTP API | Raft cluster overhead; no single-node-as-default mode; we don't need consensus | Overkill |
| dqlite | C-library distributed SQLite (Canonical, LXD, MicroK8s) | Production-mature in Canonical's stack | Heavier C-binding integration; designed for cluster use cases | Overkill |
| LiteFS | FUSE-based WAL replication | Transparent to apps | Pre-1.0; Fly.io deprioritized active dev; LiteFS Cloud sunset Oct 2024; FUSE does not cross VM boundaries | Avoid |
| cr-sqlite | CRDT extension for multi-master | Partition-tolerant | Not our shape — single-host single-user, conflicts rare | Wrong fit |

The single-node sqld answer is what the SQLite team's own
documentation prescribes (option 1: client/server). It preserves
SQLite semantics, gives us cross-host reachability, and adds no
operational complexity we don't already shoulder for T1 chroma.
Raft-class clustering remains available later via rqlite as a
sidecar if/when single-host stops being enough.

T3 local-mode chromadb server is the obvious answer because we
already operate the same daemon shape for T1. There is no
distributed-chromadb decision to make; we use the same client/server
split chromadb itself supports.

## Alternatives Considered

### Alternative 1: Roll our own HTTP shim over local sqlite3

**Description**: Build a small FastAPI service in nexus that exposes
the T2 facade methods over HTTP, with the existing `sqlite3`
connections on the server side. ~200 LoC; no new external dep.

**Pros**:
- Zero new dependencies
- Full control over the API surface
- Could expose exactly the methods nexus actually uses, no SQL pass-through needed

**Cons**:
- Reinvents libSQL/sqld badly
- No protocol stability story; every nexus client must match the server version
- Auth, connection pooling, transaction lifecycle, error mapping all become our problem
- Maintenance forever

**Reason for rejection**: We'd be a multi-year ride from feature
parity with what sqld already gives us. The dependency cost of
adopting libSQL (one pinned Python package, one daemon binary) is
much lower than the cost of building and maintaining a transport.

### Alternative 2: PostgreSQL replacement

**Description**: Migrate T2 from SQLite to a host-resident
PostgreSQL instance. Multi-host clients connect via standard
PostgreSQL wire protocol.

**Pros**:
- Battle-tested cross-host story
- Mature client libraries everywhere
- Strong concurrency story

**Cons**:
- Loses everything we built on SQLite-specific features (FTS5,
  `json_extract`, recursive CTEs targeting SQLite, RDR-105's WAL-CAS
  patterns)
- Heavy install footprint for end users
- Migration cost across seven domain stores is substantial
- Defeats the SQLite-first design philosophy

**Reason for rejection**: Disproportionate cost. nexus is designed
around SQLite's specific strengths. The transport problem is much
smaller than a database migration.

### Alternative 3: Direct virtio-fs read-write share of `~/.config/nexus/`

**Description**: Mount the host's `~/.config/nexus/` into the Cowork
VM read-write via virtio-fs. Both host and VM share the SQLite file
directly.

**Pros**:
- Zero new daemons
- Zero new dependencies
- "Just works" if you ignore the warnings

**Cons**:
- SQLite team explicitly warns against this — WAL doesn't cross
  the boundary; file locking is unreliable
- Real risk of database corruption
- Works only for Cowork; doesn't generalise to other host setups

**Reason for rejection**: SQLite's own documentation calls this
out as the failure mode. Accepting data-corruption risk to avoid
running a daemon is a bad trade.

### Briefly Rejected

- **rqlite as primary T2 transport**: Raft consensus is unneeded;
  single-node is the actual deployment shape.
- **LiteFS**: pre-1.0, deprioritized by Fly.io, doesn't cross VM
  boundaries anyway (FUSE is host-local).
- **cr-sqlite**: CRDT is the wrong primitive for our concurrency
  profile (no partition tolerance scenario, conflicts are rare).
- **Cloudflare D1**: managed-only, not self-hostable, not relevant
  for desktop nexus.
- **Mounting host's chroma directory into VM via virtio-fs**:
  ChromaDB local mode also uses file locks; same corruption risk.

## Trade-offs

### Consequences

- **+** Cowork VMs become first-class nexus participants. Host T2,
  T3, beads all visible from inside the VM.
- **+** Any second host (second workstation, container, sandbox)
  works via the same path — Cowork is just the first user.
- **+** T2 access becomes formally typed (Hrana protocol) rather than
  raw `sqlite3.Connection` passed around. Future ACL or quota work
  has a natural insertion point.
- **+** A bug in one consumer can no longer corrupt the T2 file
  directly — all writes go through sqld's server-side validation.
- **−** Latency floor rises. localhost HTTP is ~0.1ms RTT vs direct
  sqlite3's ~10µs. For hot paths this is real; for almost everything
  else it's noise. The CA-2 spike must confirm the budget.
- **−** A new daemon to supervise. Operational complexity grows.
  Mitigated by reusing the RDR-094 supervision pattern.
- **−** New external dependency (`libsql` Python client) pinned in
  `pyproject.toml`. One more thing that can break on upgrade. sqld
  itself is BETA pre-1.0; we accept the risk and pin tightly.
- **−** Authentication becomes a runtime concern. Misconfigured
  tokens silently fail to authenticate. Mitigated by `nx doctor`
  checks for token presence and reachability of each tier.

### Risks and Mitigations

- **Risk**: sqld BETA status hides a transactional bug that affects
  nexus.
  **Mitigation**: CA-1 spike runs the full T2 test suite against
  sqld before commitment. Fallback: pin to a known-working sqld
  version; document the version in `pyproject.toml`. Worst case:
  abandon sqld for the roll-our-own shim (Alternative 1) — the
  facade abstraction makes the swap localised.

- **Risk**: Hrana latency over localhost exceeds budget for hot read
  paths.
  **Mitigation**: CA-2 spike microbenchmarks before commitment.
  Hrana over Unix domain socket (not TCP) is a documented
  optimization; v1 falls back to that if loopback TCP is too slow.

- **Risk**: ChromaDB HTTP server performance degrades unacceptably
  at our T3 corpus scale.
  **Mitigation**: CA-3 spike against representative load. Fallback:
  T3 stays in PersistentClient mode on the host, and Cowork-side
  T3 reads go through a thin proxy that batches `coll.get` calls
  to a host-side helper script via coworkd CLI-plugin. Less clean
  but bounds the blast radius.

- **Risk**: Cowork VM token leaks (e.g., committed to a repo by
  accident).
  **Mitigation**: Tokens are session-scoped (TTL = session). Worst
  case is replay until session ends. Long-lived tokens (host role)
  live in `~/.config/nexus/auth.key` with mode 0600 and are never
  written to env vars or logs.

- **Risk**: Host bridged IP isn't stable across cowork session
  restarts, breaking VM discovery.
  **Mitigation**: CA-5 verifies stability. If unstable, fall back
  to coworkd CLI-plugin socket relay (the VM asks coworkd for the
  current endpoint at each session start).

### Failure Modes

- **sqld daemon crashes**: every T2 operation in every `nx-mcp`
  fails with `ConnectionError`. The supervisor (first-started
  `nx-mcp`) auto-restarts sqld within ~5s. During the gap, clients
  see retryable errors and back-off. Diagnostic: `nx doctor`
  reports the dead daemon and the supervising PID.

- **Auth token mismatch**: requests succeed with the network but
  return 401. `nx doctor` checks token validity for each tier
  and surfaces specific guidance ("re-issue cowork token via
  coworkd").

- **VM cannot reach host port** (firewall, MITM proxy rejecting
  unknown ports): `nx-mcp` startup fails loud. Cowork-side `nx
  doctor` reports the unreachable endpoint and the bridged-network
  config.

- **PID file is stale (host crashed without cleanup)**: the next
  `nx-mcp` to start finds a PID that no longer exists, claims the
  singleton lock, and starts a fresh daemon. The old PID file is
  overwritten atomically.

- **Two `nx-mcp` processes race to be supervisor**: `flock`
  serialises them. The loser reads the winner's PID file. No double
  daemon.

## Implementation Plan

### Prerequisites

- [ ] CA-1 spike completed (libSQL transactional-semantics parity)
- [ ] CA-2 spike completed (Hrana latency budget)
- [ ] CA-6 spike completed (sqld can persist to existing memory.db
      without format migration)
- [ ] CA-7 spike completed (sqld concurrency at realistic load)

### Phase 1: T2 server-mode foundation

#### Step 1: Add `libsql` Python client dependency

Pin `libsql` (or `libsql-experimental`, whichever name the Turso
project publishes at implementation time) in `pyproject.toml`. Run
the CA-1 transactional-parity spike before merging.

#### Step 2: Server-handle abstraction

Add `src/nexus/db/_servers.py` with the `ServerHandle` protocol and
the `discover_or_spawn_t2_server` / `_t3_server` factories. Pure
data layer; no actual spawning yet.

#### Step 3: sqld lifecycle supervisor

Generalise `src/nexus/mcp_infra.py`'s T1 chroma supervisor to support
a host-wide-singleton mode in addition to the existing per-session
mode. New module: `src/nexus/sqld_supervisor.py` (parallel to
T1 supervision). Singleton lock at `~/.config/nexus/sqld.lock` via
`fcntl.flock`. PID file at `~/.config/nexus/sqld_addr.json`.
Spawn `sqld --http-listen-addr 127.0.0.1:<port> --auth-jwt-key
<path>` with database path argument pointing at `~/.config/nexus/`.

#### Step 4: T2 facade migration

Update the seven domain stores under `src/nexus/db/t2/` to accept a
`ServerHandle` and route through `libsql.connect(handle.url,
auth_token=handle.token)` when one is provided. Fall back to direct
`sqlite3.connect(path)` when `ServerHandle is None` (test path).
Connection factory lives in a new `src/nexus/db/t2/_connection.py`
module; each domain store imports from there.

#### Step 5: Auth key generation + host token

New module `src/nexus/auth.py`. Generates an Ed25519 PKCS#8 key at
`~/.config/nexus/auth.key` (mode 0600) on first use. Issues a `host`
JWT with `role: host, exp: never` (per libsql JWT validation, "never"
= the far-future expiry sentinel). Loaded at `nx-mcp` startup and
passed via `ServerHandle.token`.

#### Step 6: `nx doctor` checks for T2 server

Add T2 reachability + auth checks to `src/nexus/commands/doctor.py`.
Output: `T2 server: running (PID 12345, port 7900)` or `T2 server:
unreachable (last seen: ...)`.

### Phase 2: Bind-mount-free Cowork-VM client path

#### Step 7: Cowork environment detection

Detect Cowork VM at `nx-mcp` startup via `/etc/cowork-vm` marker
file (or equivalent — verify during CA-5 spike). When detected,
skip the supervisor-spawning code path and force discovery-only
client mode.

#### Step 8: env-passdown receiver

`NX_T2_URL`, `NX_T2_TOKEN`, `NX_T3_URL`, `NX_T3_TOKEN`. If present,
`ServerHandle` is constructed from env. If absent and Cowork is
detected, fall through to Step 9.

#### Step 9: coworkd CLI-plugin client

New module `src/nexus/cowork/coworkd_client.py`. Talks to
`/run/coworkd/cli-plugin.sock` to retrieve the host's current
endpoint addresses and session-scoped tokens. Used when env-passdown
is missing (e.g. cold cowork-VM start).

#### Step 10: Cowork session-start hook

Add a hook that runs on cowork VM session start. Resolves host
endpoints via coworkd, validates connectivity to T2 and T3, writes
env vars to a session-scoped file the `nx-mcp` reads at startup.
Fails loud if either tier is unreachable.

### Phase 3: T3 server-mode for local mode

#### Step 11: ChromaDB HTTP server supervisor (host-wide)

Same singleton pattern as Phase 1 Step 3, applied to a `chroma run`
process. PID file at `~/.config/nexus/chroma_addr.json`. Only
spawned when nexus is in local T3 mode; cloud-mode T3 skips this.

#### Step 12: T3 facade migration

Update `src/nexus/db/t3.py` to route local-mode T3 through
`chromadb.HttpClient` against the supervised server. Cloud-mode T3
unchanged.

#### Step 13: T3 reachability check in `nx doctor`

Same shape as Step 6 but for T3.

### Phase 4: Cowork productionisation

#### Step 14: Token-issuance API on the host

Endpoint exposed by the host-side `nx-mcp` (HTTP on
`172.16.10.1:<dedicated-port>`) that accepts a coworkd-validated
session identifier and returns a `cowork` JWT. Issuance is gated on
coworkd having vouched for the session.

#### Step 15: VM-side documentation + smoke test

`docs/cowork-nexus.md` describes the install and verification steps.
A test suite under `tests/cowork/` exercises the end-to-end flow:
fresh VM start → endpoint discovery → token issuance → T2 read →
T2 write → T3 query.

#### Step 16: Migration of host installs

Existing host nexus installs (4.32.x and earlier) currently access
`~/.config/nexus/memory.db` directly. On first 4.33+ startup, the
new code detects the file's presence and starts sqld pointed at the
existing file — no data migration. The on-disk format is byte-
identical (libSQL is SQLite-compatible).

### Phase 5: Hardening + follow-on

#### Step 17: Failure-mode test suite

Tests that exercise: sqld crash + auto-restart, token expiry, VM
network partition, stale PID file, race-to-supervise. Each must
recover without manual intervention.

#### Step 18: Doc updates

`docs/architecture.md` — add the server-mode tier diagram. README
mentions the daemon supervision model. `docs/cli-reference.md`
documents the new `nx doctor` output and any new
`nx daemon`-prefixed subcommands.

#### Step 19: Deferred items (recorded, not implemented)

- Embedded replicas (Turso's sync model) for sub-millisecond reads
- Multi-user / multi-tenant support
- Key rotation
- HA (multi-host clustering via Raft sidecar)

These are explicitly out of scope for RDR-112 and are noted here
so they're not lost.

## Test Plan

### Unit tests

- `ServerHandle` discovery: existing PID file is found; missing PID
  file triggers spawn; stale PID file (dead process) triggers
  respawn; concurrent spawn races are serialised by `flock`.
- T2 connection factory: returns `libsql.Client` when handle is
  present; returns `sqlite3.Connection` when handle is None;
  raises a clear error on `ServerHandle` failure.
- Auth key generation: idempotent (re-running creates same key);
  file permissions are 0600.
- env-passdown receiver: parses all four `NX_T*_URL` / `NX_T*_TOKEN`
  vars; rejects partial / malformed values.

### Integration tests

- T2 full round-trip via sqld: spawn sqld, write to all seven
  domain stores, read back, shut down. Compare results against a
  direct-sqlite3 control run.
- T3 local-mode round-trip via chromadb server: add a chunk,
  query, get, delete.
- Cowork-VM smoke (gated behind a `cowork` pytest marker so CI
  doesn't try to run it): full discovery → token → read → write.

### Failure-mode tests

- sqld crash: kill the daemon mid-test, assert client errors
  surface, supervisor auto-restarts within 5s, subsequent operations
  succeed.
- Token expiry: issue a 1-second-TTL token, wait, assert 401.
- Stale PID file: write a PID file pointing at a non-existent PID,
  assert next supervisor claim succeeds and overwrites.

## Validation

RDR-112 is validated when:

1. A Cowork VM, freshly started, can `nx memory put` and have the
   value visible from the host's `nx memory get` (and vice versa).
2. A Cowork VM can run `nx search` against the host's indexed T3
   corpus and return results identical to a host-side query.
3. The host's `~/.config/nexus/memory.db` continues to be readable
   by the legacy direct-sqlite3 code path (compatibility check),
   ensuring no on-disk format divergence.
4. `nx doctor` cleanly reports both daemon states (running,
   responsive, authenticated) on both the host and a Cowork VM.
5. Killing the host's sqld daemon causes T2 operations to fail
   loudly, the supervisor restarts sqld within 5s, and operations
   resume — no manual intervention required.
6. P95 latency for `memory_get` is within 5ms (CA-2 target) on a
   warm system; 20ms for typical `memory_search` FTS5 queries.

## Finalization Gate

- [ ] All seven Critical Assumptions cleared by spike or source
      search before Phase 1 implementation begins.
- [ ] CA-1, CA-2, CA-6, CA-7 specifically gate Phase 1 (they govern
      whether sqld is a viable T2 transport at all).
- [ ] CA-3 gates Phase 3 (T3 server-mode).
- [ ] CA-4, CA-5 gate Phase 4 (Cowork productionisation).
- [ ] Unit + integration test suite passes; failure-mode suite
      passes.
- [ ] `nx doctor` cleanly reports server states on both host and
      Cowork VM.
- [ ] `docs/architecture.md`, `README.md`, `docs/cli-reference.md`
      updated.
- [ ] A migration smoke test on a pre-RDR-112 install confirms
      no data loss and no format divergence.
- [ ] RDR-110 and RDR-111 cross-references updated to note that
      cross-host coordination is provided by RDR-112's server tiers
      (not direct file access).

## References

- SQLite official: [Use Over Network](https://sqlite.org/useovernet.html)
- SQLite official: [When to Use SQLite](https://sqlite.org/whentouse.html)
- [GitHub — libsql/sqld](https://github.com/libsql/sqld)
- [GitHub — tursodatabase/libsql](https://github.com/tursodatabase/libsql)
- [Self-hosting Turso libSQL (Hubert Lin, 2024-11)](https://hubertlin.me/posts/2024/11/self-hosting-turso-libsql/)
- [LiteFS vs Litestream vs rqlite vs dqlite on VPS (Onidel, 2025)](https://onidel.com/blog/sqlite-replication-vps-2025)
- [Litestream — Alternatives](https://litestream.io/alternatives/)
- `docs/rdr/rdr-094-mcp-owned-t1-chroma-lifecycle.md` — supervision
  pattern reused for the T2/T3 daemons
- `docs/rdr/rdr-105-t1-chroma-architecture-env-passdown.md` —
  discovery + env-passdown patterns extended to T2/T3
- `docs/rdr/rdr-110-semantic-tuple-space.md` — downstream
  consumer; tuple-space tiers become network-reachable via RDR-112
- `docs/rdr/rdr-111-orb-agentic-cockpit-substrate.md` — downstream
  consumer; cockpit cross-process visibility relies on RDR-112

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-05-12 | Hal Hildebrand | Initial draft. Scoped as a foundational RDR independent of RDR-110/111: server-based T2 + T3 local-mode for cross-host coordination, motivated by the Cowork-VM use case but generalising to any second-filesystem deployment. sqld (libSQL) for T2, ChromaDB HTTP server for T3 local mode, single-node single-tenant JWT auth, host-wide-singleton supervision (extending the RDR-094 per-session pattern). Seven CAs gate implementation; libSQL transactional-parity + latency-budget spikes are the load-bearing checks. |
