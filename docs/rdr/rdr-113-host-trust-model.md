---
title: "Host-Trust Model for nexus Daemons: UDS Permissions, Peer Credentials, Single-User v1"
id: RDR-113
type: Architecture
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-13
accepted_date: 2026-05-13
related_issues: []
related_rdrs: [RDR-105, RDR-110, RDR-111, RDR-112]
related_tests: []
implementation_notes: ""
---

# RDR-113: Host-Trust Model for nexus Daemons: UDS Permissions, Peer Credentials, Single-User v1

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

RDR-112 puts T2, T3, and CatalogDB behind daemons that accept
connections over a unix-domain socket (UDS-primary) or loopback
TCP (fallback). The v1 framing in RDR-112 §Cross-Cutting Concerns
says "no auth in v1 — local daemons, UDS uses unix file
permissions, TCP listeners are loopback-only." That sentence is
load-bearing for cross-RDR safety claims but no concrete trust
model is committed: the UDS `chmod` value is unspecified, the
TCP listener's bind address is not pinned, and there is no
explicit answer to "who can attach to my daemon?"

RDR-111's ORB cockpit raises the stakes. Its panels project
*every host agent's state* — running tools, pending bindings,
hook events. RDR-110's tuple space carries `mailbox/<agent>`
subspaces (agent-to-agent direct messaging). If a second user
on the same host can attach to the daemon, that user can read
every memory entry, every in-flight tuple, every hook event,
and every mailbox — and can post messages impersonating any
agent.

For nexus's actual deployment model — single-user developer
laptops, occasionally a shared dev VM — the threat is small but
real. For the daemon model to be defensible at all, the trust
boundary needs to be named and enforced, not assumed.

### Enumerated gaps to close

#### Gap 1: UDS socket file mode is unspecified

RDR-112 references "unix file permissions" but never commits to
a value. The default umask on most systems is `0022`, which
would create the socket as `0755` (world-readable, owner-write).
Any local user could `connect()` to the daemon. The fix is a
specific `chmod` value, applied by the daemon at bind time
before announcing the address.

#### Gap 2: TCP fallback bind address is not pinned

RDR-112 says "TCP listeners are loopback-only" in prose but
does not commit to `127.0.0.1` (vs `0.0.0.0`) in the design
artefact. Future planning could read the prose as advisory and
bind a routable interface. A binding-time commitment prevents
the silent regression.

#### Gap 3: No peer-credential check on connect

UDS supports `SO_PEERCRED` (Linux) and `LOCAL_PEERCRED` (macOS),
which return the connecting peer's UID. The daemon currently
performs no check. On a multi-user host (rare but real for dev
VMs), file permissions alone may be circumventable — for
instance, if the socket path traverses a directory the second
user controls. A peer-UID check at accept time is cheap and
closes the residual gap.

#### Gap 4: No commitment on what v1 *does not* protect against

"Single-user v1" needs a written boundary. Threats RDR-113
explicitly does *not* address: a malicious local process
running as the same user (it can read the SQLite file directly
if the daemon is down, and can connect to the UDS regardless);
a compromised parent process injecting `NX_T2_ADDR` env vars;
cross-host attacks (out of scope per RDR-112). Naming the
boundary prevents over-claiming.

## Context

### Background

This RDR was spun out of the RDR-110/111/112 triad rework
(2026-05-13) as the closure for gap G6. The triad analysis
flagged that RDR-112's auth deferral was vague enough that
"no auth in v1" could be read as "we'll get to it later," when
the actual stance — for the daemon model to compose safely with
RDR-110's mailbox tuples and RDR-111's cockpit projection —
needs to be "v1 enforces single-user host trust via UDS
permissions and peer credentials." That stance is small enough
to land in a mini-RDR, large enough to deserve its own gate.

### Technical Environment

- **UDS sockets**: stdlib `socketserver` / `multiprocessing.connection`.
  `socket.bind()` creates the path with current umask; daemon
  must `os.chmod()` after bind to set the desired mode.
- **TCP fallback**: stdlib `socketserver` with `server_address =
  ('127.0.0.1', 0)` for explicit loopback bind.
- **Peer credentials**: `socket.SO_PEERCRED` (Linux), via
  `getsockopt(SOL_SOCKET, SO_PEERCRED, ...)`; `LOCAL_PEERCRED`
  (macOS), via `getsockopt(SOL_LOCAL, LOCAL_PEERCRED, ...)`.
  Both available without third-party deps.

## Research Findings

### Investigation

- **Inherited pattern from RDR-105**: The T1 chroma server
  spawned at `session.py:494-504` is a similar host-local
  daemon. Inspecting its current bind-time behaviour: TCP only,
  port 0 (dynamic), bound to whatever interface chromadb's HTTP
  server defaults to (loopback in practice, but not enforced).
  RDR-113 tightens this pattern rather than inventing one from
  scratch.
- **Docker socket precedent**: `/var/run/docker.sock` ships
  `0660` with `root:docker` ownership; users in the `docker`
  group connect. nexus's analogue: `0600` with the running
  user's UID; no group ACL in v1.
- **PostgreSQL UDS precedent**: peer authentication via
  `pg_hba.conf` checks `SO_PEERCRED` UID against database role.
  nexus v1 is simpler: peer UID must equal the daemon's UID, no
  multi-user mapping.

### Key Discoveries

- **Verified**: `os.chmod(socket_path, 0o600)` after
  `socket.bind()` is the standard way to restrict UDS access to
  the owner. Verified against stdlib docs and a quick spike.
- **Documented**: `SO_PEERCRED` returns `(pid, uid, gid)` on
  Linux; `LOCAL_PEERCRED` returns `xucred` on macOS. Both work
  without setting any flag on the listening socket beforehand.
- **Assumed**: TCP loopback peers cannot be reliably identified.
  `SO_PEERCRED` does not apply to TCP. The v1 stance accepts
  this — TCP fallback is for orchestrator-injected access
  (containers, VMs), where the orchestrator is the trust
  boundary, not the daemon.

### Critical Assumptions

- [x] **A1**: `os.chmod(0o600)` on the UDS path is honored
  before any client `connect()` attempt — i.e., there is no
  race window between `bind()` and `chmod()` where a fast peer
  could attach with default-umask permissions.
  — **Status**: **Verified** (2026-05-13 spike at
  `/tmp/rdr113_a1_spike.py`; T2 entry
  `nexus_rdr/113-research-A1-spike`) — **Method**: Spike.
  **Finding**: `bind()` alone does not enable connections —
  `connect()` to a bound-but-not-listening UDS returns
  `ConnectionRefusedError`. The actual gate is `listen()`, not
  `bind()`. Ordering `bind() → chmod(0o600) → listen()` closes
  the race window to zero: during the bind→chmod gap (~13 µs in
  the spike), no peer can connect because no backlog exists.
  Defense-in-depth via parent-dir `0o700` is preserved as
  belt-and-braces. The §Risks "temp-path + atomic-rename"
  mitigation is unnecessary given this finding and is retired
  below.
- [ ] **A2**: `SO_PEERCRED` / `LOCAL_PEERCRED` reliably return
  the *originator* of the connection, not a forwarding proxy
  (relevant if the user later puts the daemon behind a
  socket-relay).
  — **Status**: Unverified — **Method**: Docs Only is
  sufficient for v1; revisit if proxying is introduced.

## Proposed Solution

### Approach

**v1 trust model: single-user host, daemon owner == client owner.**

1. **UDS bind discipline** (T2, T3, CatalogDB daemons) —
   ordering matters, A1-verified:
   - `socket.bind(path)` → `os.chmod(path, 0o600)` →
     `sock.listen(backlog)`, in that order. Per A1, `connect()`
     against a bound-but-not-listening UDS returns
     `ConnectionRefusedError`, so the bind→chmod window is
     closed by `listen()` not having been called yet. The mode
     is restricted before any peer can establish a connection.
   - Daemon refuses to start if `os.geteuid() != os.stat(path).st_uid`
     after chmod (sanity check).
   - Socket directory (`~/.config/nexus/sockets/` or similar)
     created with `0o700` if not present. Defense-in-depth: even
     if the socket inode somehow ends up at default mode (e.g.
     chmod silently failed on an exotic filesystem), the parent
     dir gates path traversal.

2. **TCP bind discipline**:
   - `server_address = ('127.0.0.1', port)`. Hard-coded
     loopback; never `0.0.0.0`, never the host's external IP,
     never a `--bind-host` flag in v1.
   - `port=0` for dynamic allocation; actual port goes into
     discovery file and stdout announcement.

3. **Peer-UID check on accept**:
   - For UDS connections: read `SO_PEERCRED` / `LOCAL_PEERCRED`;
     reject with a clear error if peer UID ≠ daemon UID.
   - For TCP connections: no UID check (loopback only; trust
     boundary is the orchestrator).
   - Rejection is logged at INFO with peer PID + UID for audit.

4. **No in-protocol auth in v1**:
   - No tokens, no challenge/response, no TLS. The UDS/TCP
     bind discipline plus peer-cred check is the entire trust
     mechanism.
   - Future RDR will add token-based auth for cross-host /
     multi-user / federated deployments.

5. **Explicit non-goals (named in §Threat Model below)**:
   - Same-user malicious processes (they own the socket and the
     SQLite file directly; defence against this is outside
     nexus's trust boundary).
   - Compromised parent processes injecting `NX_T2_ADDR`.
   - Cross-host attacks (RDR-112 out of scope).

### Threat Model

| Threat | Mitigation | Residual risk |
| --- | --- | --- |
| Second user on host attaches to my UDS | `chmod 0600` + peer-UID check | None (v1) |
| Second user crafts TCP connection to my daemon | Loopback bind + peer cannot reach loopback as another user | None (single-host) |
| Same-user malicious process | **Not addressed in v1** | Accepted — user owns the data |
| Daemon impersonation by same-user rogue process | **Not addressed in v1** (subsumed by "same-user malicious process" non-goal — a process running as the daemon's UID can bind any path the daemon could) | Accepted — user owns the host |
| Cross-host attacker | **Not addressed in v1** | RDR-112 out of scope |
| TCP listener reaches the network | Hard-coded `127.0.0.1` bind, no flag to override | None (v1) |

### Decision Rationale

Minimum-viable trust model that closes the visible gap (G6)
without inventing auth code we don't yet need. The threat model
matches nexus's actual deployment (single-user developer
laptop), names the cases it explicitly doesn't protect against,
and leaves a clean seam for a future auth RDR if/when the
threat model expands.

## Alternatives Considered

### Alternative 1: Token-based auth in v1

**Description**: Daemon generates a token at startup, writes it
to the discovery file alongside the socket address; clients
must present the token on every RPC.

**Pros**:
- Closes the same-user-malicious-process threat (process needs
  read access to the discovery file *and* the socket).
- Forward-compatible with cross-host auth.

**Cons**:
- The discovery file is in the same place the malicious process
  would already have read access to. Net protection is small.
- Adds RPC overhead and token-rotation lifecycle for marginal
  benefit at v1 scale.

**Reason for rejection**: Cost-benefit doesn't justify v1.
Token auth lands when the threat model justifies it (multi-user
or cross-host).

### Briefly Rejected

- **mTLS**: Heavy; appropriate for cross-host, not single-host.
- **Group-based UDS ACLs** (`chmod 0660` + group): unnecessary
  generalisation for single-user v1; can land later if shared
  dev VMs become a supported deployment.

## Trade-offs

### Consequences

- (+) Closes G6 (multi-user host exposure) at full v1 scope.
- (+) Threat model written down — over-claiming prevented.
- (−) Same-user malicious process is explicitly out of scope;
  if a user's threat model includes that, RDR-113 doesn't help.
- (−) Peer-cred check adds one `getsockopt()` per accept;
  negligible.

### Risks and Mitigations

- **Risk** (retired): Race window between `bind()` and
  `chmod()`. **Disposition**: A1 spike verified the window is
  effectively zero given `bind() → chmod() → listen()`
  ordering, because `connect()` to a bound-but-not-listening
  UDS returns `ConnectionRefusedError`. No temp-path-plus-
  atomic-rename mitigation required. Defense-in-depth via
  parent-dir `0o700` is retained.
- **Risk**: macOS `LOCAL_PEERCRED` quirks (different struct
  shape from Linux `SO_PEERCRED`).
  **Mitigation**: platform-specific accessor in `nexus.daemon.peer`;
  unit tests on both OS.

### Failure Modes

- **UDS chmod fails** (e.g., filesystem doesn't support it):
  daemon fails to start with a clear error. No silent fallback.
- **Peer-cred rejection**: client sees a `PermissionDenied`
  RPC error with a message naming the daemon's expected UID and
  the peer's actual UID. Logged daemon-side.
- **`getsockopt(SO_PEERCRED / LOCAL_PEERCRED)` itself raises**
  (unexpected platform, sandbox restriction, kernel version
  gap): daemon closes the connection with a clear error and
  logs at ERROR. **No fallback to "accept with warning"** —
  consistent with the chmod-failure handling above; the trust
  model is fail-loud.

## Implementation Plan

### Prerequisites

- [ ] RDR-112 accepted (this RDR layers onto its daemon model).
- [ ] A1 verified (no `bind()`-to-`chmod()` race).

### Minimum Viable Validation

Two users on the same host. User A starts `nx daemon t2`. User
B tries to `nx memory get` against User A's socket path. The
attempt must fail loud with a peer-UID rejection message — not
hang, not silently fall back, not return a stale read.

### Phase 1: Daemon-side enforcement

To be expanded during `/nx:create-plan`.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| UDS socket file | `ls -la <path>` | `stat <path>` | daemon stop | `stat` mode == `0600` | n/a |
| TCP bind address | `ss -tlnp | grep nx-daemon` | discovery file | daemon stop | bind == `127.0.0.1:N` | n/a |

### New Dependencies

None. Stdlib `socket`, `os`, `getsockopt`.

## Test Plan

- **Scenario**: Daemon binds UDS; check `os.stat(path).st_mode & 0o777 == 0o600`. — **Verify**: passes.
- **Scenario**: User B (different UID) connects to User A's UDS. — **Verify**: connect fails or accept rejects with clear message.
- **Scenario**: Daemon TCP bind. — **Verify**: `ss -tln` shows `127.0.0.1:N`, not `0.0.0.0:N`.
- **Scenario**: `LOCAL_PEERCRED` path on macOS. — **Verify**: peer UID parsed correctly.
- **Scenario**: After `bind() → chmod() → listen()` ordering, assert `os.stat(socket_path).st_mode & 0o777 == 0o600` is observable before the listening backlog opens — i.e. between `chmod()` and `listen()` calls. (A1 spike already proved that `connect()` against a bound-but-not-listening UDS refuses, so no peer-side race test can fire; this scenario instead asserts the mode invariant the spike relies on.)

## Validation

### Testing Strategy

1. Unit tests for the peer-cred accessor on Linux + macOS.
2. Integration test: two-user MVV scenario (CI uses sudo or
   `unshare -U` to fake a second UID).
3. Negative test: TCP bind never exposes a non-loopback
   interface.

### Performance Expectations

`getsockopt(SO_PEERCRED)` is a single syscall on accept;
negligible. No measurable overhead expected.

## Finalization Gate

> Complete each item with a written response before marking
> this RDR as **Accepted**.

### Contradiction Check

Reviewed against the active RDR-110 / RDR-111 / RDR-112
contracts. No contradictions surfaced.

- **Same-host claims from RDR-110/111 are honored.** RDR-110
  (cockpit) and RDR-111 (binding watcher / reaction loop) both
  assume a single-user local workstation: the cockpit reads and
  writes via the same daemon the user already owns, and the
  watcher dispatches reactions inside the daemon process. This
  RDR narrows the trust model for the daemon's *transport
  surface*, it does not redefine the same-host coordination
  surface those RDRs depend on. UDS bind discipline (bind,
  chmod 0o600, listen) plus the `SO_PEERCRED` peer check keep
  the same-uid invariant that RDR-110 and RDR-111 already
  assume, so their claims pass through unchanged.
- **TCP-fallback trust is delegated to the orchestrator.** v1
  ships UDS-only and explicitly defers any TCP listener to a
  later RDR. If a future revision introduces a TCP fallback or
  a socket-relay, peer identity can no longer be derived from
  `SO_PEERCRED` (see §A2): the originating uid is hidden behind
  the relay or the TCP stack. In that world the trust decision
  moves up to the orchestrator (mTLS, signed tokens, or a
  brokered handshake), and this RDR's same-host guarantees no
  longer apply transitively. v1 is consistent because it does
  not open that surface; the contradiction is bounded to a
  future RDR that introduces the relay.
- **No other contradictions found.** RDR-112 (daemon
  lifecycle) is layered cleanly: the peer check runs inside the
  daemon's accept loop, which RDR-112 already owns. RDR-111
  CA-9 / CA-10 (coordination subspaces, migration gate) do not
  cross the transport boundary and are unaffected.

### Assumption Verification

- **A1**: **Verified** (2026-05-13). Spike at
  `/tmp/rdr113_a1_spike.py` confirmed that `connect()` against a
  bound-but-not-listening UDS returns `ConnectionRefusedError`.
  The required ordering — `bind() → chmod(0o600) → listen()` —
  closes the race window to zero. Bind→chmod gap measured at
  ~13 µs; irrelevant because no peer can connect before
  `listen()`. T2 entry: `nexus_rdr/113-research-A1-spike`.
- **A2**: **Documented** (Docs Only is sufficient per the
  Critical Assumptions table). Revisit only if a socket-relay
  is introduced.

### Scope Verification

MVV (cross-user rejection) is in scope, not deferred. Single
end-to-end test using two host UIDs.

### Cross-Cutting Concerns

- **Versioning**: trust model is invariant across daemon
  versions; no handshake field needed.
- **Build tool compatibility**: stdlib only.
- **Licensing**: N/A.
- **Deployment model**: single-user host (named explicitly).
- **IDE compatibility**: N/A.
- **Incremental adoption**: enforced from daemon v1; no flag.
- **Secret/credential lifecycle**: N/A (no secrets in v1).
- **Memory management**: N/A.

### Proportionality

Mini-RDR; right-sized for a single closure of G6.

## Open Questions

- **Future auth surface**: when the threat model expands
  (cross-host, multi-user, federated), token-based auth lands
  in a follow-on RDR. RDR-113's discipline survives that change
  as the host-local v1 path.
- **`docker:docker` group analogue**: if shared dev VMs become
  supported, RDR-113 v2 may add group-based ACLs (`chmod 0660`).
  Deferred.

## References

- RDR-105: T1 Chroma Architecture — Env Passdown (the
  precedent for host-local daemon spawning).
- RDR-110: Semantic Tuple Space (mailbox subspaces inherit the
  trust boundary).
- RDR-111: ORB Observable Relay Bus (cockpit projection
  inherits the trust boundary).
- RDR-112: Storage-as-Service (this RDR closes its G6 gap).
- POSIX `socket(7)`, `unix(7)`, `SO_PEERCRED` Linux man page.

## Revision History

- 2026-05-13 — Initial draft. Spun out of RDR-110/111/112 triad
  rework as the closure for gap G6.
- 2026-05-13 — A1 spike executed. Verified that `bind() →
  chmod(0o600) → listen()` ordering closes the race window
  because `connect()` against a bound-but-not-listening UDS
  returns `ConnectionRefusedError`. §Proposed Solution §1 UDS
  bind discipline updated to record the required ordering;
  §Risks "Race window between bind() and chmod()" retired;
  §Finalization Gate Assumption Verification updated. T2:
  `nexus_rdr/113-research-A1-spike`.
- 2026-05-13 — **Gate round 1 PASSED** (substantive-critic
  `ad665ee8c98897352`, 0C/2S/2O). Significant closeouts in this
  revision:
  - S1 (T2 spike record cited but critic couldn't find it):
    record exists at `nexus_rdr/113-research-A1-spike` (critic
    searched the wrong project `nexus`); confirmed present.
  - S2 (daemon-impersonation row committed to unspiked
    mitigation): row reworded to "not addressed in v1" —
    subsumed by the same-user-malicious-process non-goal, since
    a same-UID rogue can bind any path the legitimate daemon
    could. No new spike or assumption needed.
  Observations:
  - Obs1: §Failure Modes extended — `getsockopt` itself raising
    is now explicitly fail-loud, no silent fallback.
  - Obs2: Test Plan scenario 5 reframed — A1 already proved
    no peer-side race test can fire, so the scenario instead
    asserts the mode-invariant the spike relies on.
  - Obs3 (macOS `LOCAL_PEERCRED` quirks): kept as-is, critic
    confirmed proportional for mini-RDR.
