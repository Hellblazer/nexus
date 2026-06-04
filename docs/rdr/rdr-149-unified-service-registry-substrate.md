---
title: "Unified Leased Service-Registry Substrate for T1/T2/T3 Daemon Lifecycle: End the Recurring Discovery / Single-Writer / Self-Heal Bug Class"
id: RDR-149
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-04
accepted_date: 2026-06-04
related_issues: [nexus-4fw0z, gh-1112, gh-1114, gh-956, nexus-9eaz]
related_rdrs: [RDR-010, RDR-041, RDR-063, RDR-094, RDR-105, RDR-128, RDR-129, RDR-140, RDR-141, RDR-146]
---

# RDR-149: Unified Leased Service-Registry Substrate for T1/T2/T3 Daemon Lifecycle

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

nexus runs three local "ephemeral service" lifecycles — the per-session **T1**
chroma, the per-user **T2** daemon, and the per-user **T3** (managed `chroma
run`) daemon. Each independently re-solves the *same* concern: **spawn → publish
identity→endpoint → discover → liveness → reap-orphans → restart-republish →
self-heal → single-writer/election → version-skew**. They share no code. The
result is a chronic, recurring bug class.

**This is not a hypothesis; it is measured.**

- **~10 RDRs** target this one cluster: RDR-010, 041, 063, 094, 105, 128, 129,
  140, 141, 146 (and 037/038 adjacent).
- **71 distinct GH-issue / bead references** appear as scar-tissue comments in
  just six files (`session.py`, `db/t1.py`, `daemon/*.py`, `mcp/core.py`).
- **156 commits in 90 days** touch `session.py` / `db/t1.py` / `daemon/` /
  `mcp/core.py`.
- A standing flaky-test family (`nexus-9eaz`) spans `test_t2`,
  `test_migrations`, `test_aspect_drain_protocol`,
  `test_t2_daemon_startup_invariant`.

**The mechanism of recurrence: hard-won fixes land in one tier and never
propagate to the parallel implementations.** Measured robustness asymmetry:

| Hard-won fix | T2 | T3 | T1 |
|---|---|---|---|
| Self-healing re-assert (RDR-140) | 17 refs | 0 | 0 |
| Version-skew cycle on upgrade (RDR-141) | yes (`_cycle_daemon_to_current` → `nx daemon t2 stop`) | no | n/a |
| Single-writer / election (RDR-128/129/140) | 59 refs | 5 | 0 |

The two issues filed 2026-06-04 are not new bugs — they are **already-solved T2
bugs reappearing in the tiers that never received the fix**:

- **#1114** (T1 chroma runs unpublished; `t1_addr.<claude_pid>` missing for the
  live session → sibling `nx scratch` raises `T1ServerNotFoundError`) = T1
  lacks RDR-140's self-heal. Empirically: live session `c76c1995`, T1 server on
  port 54847 alive, no addr file references it; reproduces non-sandboxed; PPID
  walk and session-id resolution are both fine — the addr file was published
  then lost across an MCP restart with no surviving re-assert.
- **#1112** (T3 daemon not auto-restarted on CLI upgrade; stale version
  persists) = T3 lacks RDR-141's version-skew upgrade-cycle (`upgrade.py`
  cycles only T2).

Three implementations means every production incident teaches exactly one tier a
lesson the other two never receive. We do not keep finding new bugs; we keep
rediscovering the same bug in a copy that was not patched. The specific gaps:

#### Gap 1: Three duplicated lifecycle implementations, no shared code

The same concern (spawn / publish / discover / liveness / reap / restart /
self-heal / election / version-skew) is implemented three times — T1 in
`session.py`, T2 and T3 in `daemon/`, with no cross-imports (verified). A fix in
one tier cannot reach the others, so every hard-won correctness lesson must be
re-learned per tier via a production incident.

#### Gap 2: PID is the wrong identity / liveness primitive

Addr files are keyed by and reaped on PID (`t1_addr.<claude_pid>`;
`_is_pid_alive`). PIDs are reused; "process alive" ≠ "endpoint healthy"; and the
writer (MCP server) and reader (sibling shell) can resolve different PIDs via the
`find_immediate_claude_pid` walk. T1 can instead key on **session-id** — both
sides already compute `resolve_active_session_id()` identically from
`~/.config/nexus/current_session` (verified live: both return `c76c1995`).

#### Gap 3: No self-heal re-assert in T1/T3 (only T2 has it)

RDR-140's self-healing re-assert exists only in T2 (mentions: T2=17, T3=0,
T1=0). A transient loss of a discovery/addr record therefore strands clients
permanently in T1/T3. **#1114** is exactly this: the T1 chroma runs but its
`t1_addr` was lost across a restart with nothing to re-assert it.

#### Gap 4: No version-skew upgrade-cycle for T3 (only T2 has it)

RDR-141's upgrade-time daemon cycle (`_cycle_daemon_to_current` →
`nx daemon t2 stop`) covers only T2; T3 is never cycled. **#1112** is exactly
this: after `nx upgrade`, the T3 daemon keeps running the stale binary until a
manual restart.

#### Gap 5: No cross-tier invariant test

Each tier's tests assert only its own behaviour, so a property proven for T2
(survive restart with a discoverable endpoint; survive concurrent siblings;
survive version skew) is never asserted for T1/T3. Missing features and
regressions in T1/T3 stay invisible until a human hits them in production; the
standing `nexus-9eaz` flaky-test family is the symptom of per-tier bespoke
concurrency harnesses rather than one model.

## Decision

The three tiers are **one problem at three scopes** (T1 = session-id, T2 = uid,
T3 = uid). The divergences are *parameters*, not blockers. Build **one leased,
fenced, atomic service-registry + supervisor primitive** and migrate all three
tiers onto it, behaviour-preserving, proven by **one parameterized lifecycle
conformance suite**. Stop shipping per-tier lifecycle patches.

Core primitive semantics:

- **Lease, not PID.** The owner heartbeats (re-writes / touches the record)
  every interval; a record is live iff `lease_age < TTL`. Reaping is
  lease-expiry, not pid-death. Heartbeat *is* the RDR-140 self-heal.
- **Fencing token.** A **flock-serialized monotonic generation** per scope (read
  -increment-write while holding the election flock — NOT an optimistic
  lock-free CAS; the flock is the mutex). A stale restart with a lower
  generation cannot clobber a newer owner's record. This makes restart-republish
  atomic and immune to the "old server's shutdown unlinks the new server's file"
  race (#1114).
- **Atomic publish.** Write-temp + rename, always; the record is never absent
  while a live owner exists (re-assert covers transient gaps).
- **Scope-keyed election.** Exactly-one-owner-per-scope via a per-scope flock —
  uid for T2/T3, session-id for T1 (one T1 server per session, intentionally
  N-per-user). One election implementation, three scope keys.
- **PID-reuse immunity.** No PID in the identity or liveness path.

This explicitly does **not** introduce a new daemon, rewrite storage, or change
T1's session-scoping / T2/T3 singleton semantics. It is a cutover of the
lifecycle plumbing, not a rebuild (the RDR-146 lesson; and the anti-pattern of
the scrapped RDR-110→113 big-bang substrate chain, 9 RDRs / 67 stranded beads,
is the cautionary precedent).

Two prior open questions are resolved and locked here:

- **The version-skew upgrade-cycle moves to the supervisor**, not per-tier
  `upgrade.py` code. Reason: keeping it per-tier is exactly how #1112 happened
  (T2 got cycled, T3 did not). A supervisor-owned `cycle_to_current(scope)`
  means a future fourth service inherits the behaviour for free. The primitive's
  API therefore includes a cycle entry point.
- **T1 stays MCP-lifespan-owned and merely *consumes* the primitive** (it does
  not become a supervised daemon). Reason: T1 is session-scoped working memory
  whose lifecycle is already bound to the MCP server's lifespan (RDR-105 P4);
  adding a supervised T1 daemon would be the "build a fourth thing" anti-pattern
  RDR-146 warns against. The primitive slots into the existing four-branch
  lifespan dispatch (`mcp/core.py:109-345`) at the publish branch only; the
  env-inherited / isolated branches are untouched.

## Approach

1. **Conformance suite first, against the current three implementations [MEDIUM].** Write one parameterized lifecycle property battery (publish→discover roundtrip; survive ungraceful kill→reap; survive restart→republish-with-new-generation; concurrent siblings converge to one owner; version-skew cycle; pid-reuse immunity) and run it against T1, T2, T3 as they exist now. The red cells are the exact, evidence-based scope; #1112 and #1114 appear here as failing tests, not speculative scope.
2. **Extract the leased/fenced/atomic registry + supervisor primitive [LARGE].** A single module (lease record schema, atomic publish via rename, heartbeat/TTL liveness, monotonic generation fencing, scope-keyed flock election, self-heal re-assert loop). Pure, deterministic, fixed-clock-testable; no tier-specific code.
3. **Migrate T2 onto the primitive first (the reference) [MEDIUM].** T2 has the most complete behaviour, so its migration is behaviour-preserving and the conformance suite proves no regression. Replaces the bespoke `daemon/discovery.py` + election + RDR-140 re-assert with calls into the primitive. **Preserve the `_t2_ensure_running_inner` interface** consumed by `mcp_infra._reassert_t2_daemon()` (`mcp_infra.py:214`, the RDR-141 version-skew arm); the migration refactors its internals to delegate to the primitive but must not change its external contract. Regression gate: the RDR-140/129/141 suites stay green (see §Test Plan).
4. **Migrate T3 onto the primitive [MEDIUM].** T3's previously-red conformance tests (self-heal, version-skew cycle) go green = #1112 fixed structurally. Wire the upgrade-cycle to the shared supervisor so it covers T3, not just T2.
5. **Migrate T1 onto the primitive, re-keyed on session-id [MEDIUM].** Replace `t1_addr.<claude_pid>` + `find_immediate_claude_pid` PPID-walk with a session-id-scoped lease record; T1's red conformance tests (self-heal, atomic restart-republish) go green = #1114 fixed structurally. Preserve session-scoped N-per-user semantics.
6. **Delete the dead bespoke lifecycle code and prove zero copies remain [SMALL].** Remove the per-tier pid-sweep, PPID-walk, and per-tier election once all three route through the primitive; an inverse-grep / lint audit to zero confirms no bespoke copy survives (the RDR-146 boundary-lint-to-0 discipline).
7. **Standing process gate [SMALL].** Document the rule: any future lifecycle fix lands in the shared primitive + the conformance suite, never in one tier's copy. This is the stop-the-bleeding gate that prevents patch N+1.

## Research Findings

Investigated 2026-06-04 against the codebase (file:line evidence).

**RF-1 (RESOLVED): the re-assert pattern already exists in T2 and is cheap;
generalize it.** `T2Daemon._reassert_discovery_loop` (`t2_daemon.py:842`) runs
every `_REASSERT_INTERVAL = 1.0s` (`:124`) and, when the record is intact, is a
**stat + read with no write** — it rewrites only when the file is missing or
names a different pid. The locked invariant `_LOSER_POLL_TIMEOUT (3.0) >=
_REASSERT_INTERVAL (1.0) + write latency` (`:120-125`) gives sub-second
self-heal with a discoverer's poll window that cannot straddle a mid-re-assert
gap. The loop is cancelled at the *start* of `stop()` so it can never resurrect
a file the shutdown unlink just removed. This is exactly the primitive's
heartbeat; lift it verbatim, parameterized by scope. **Constraint for the
substrate:** keep TTL ≥ heartbeat + worst-case write latency; reuse these two
constants as the substrate defaults.

**RF-2 (RESOLVED, with a constraint): session-id is the right T1 key but is
written by the SessionStart hook, not the MCP lifespan, so the lease must be
published/re-keyed lazily.** `current_session` is written by
`hooks.py:session_start` → `write_claude_session_id` (`hooks.py:57+`); nested
subprocesses inherit `NX_SESSION_ID` and deliberately leave the parent pointer
alone. The MCP lifespan owns chroma but **not** the session pointer (RDR-105
P4). `resolve_active_session_id` precedence is `NX_SESSION_ID` env →
`current_session` file → `None` (`session.py:82+`). So at lifespan
`__aenter__` the session-id may not yet be resolvable on a cold top-level
session (hook race), while `claude -p` subprocesses have it immediately via the
inherited env.

**LOCKED re-key design (resolves the gate's RF-2 ambiguity).** A single
protocol, not "(a) or (b)":

1. At lifespan publish, the owner writes a lease record under a **transient
   server-unique key** = the chroma `server_pid` (guaranteed unique among live
   owners; never `unknown`; never collides across sessions). The record carries
   the resolved-or-`None` session-id as a *field*.
2. The heartbeat loop calls `resolve_active_session_id()` each tick. The instant
   it resolves non-`None` and the record is still transient-keyed, the loop
   **atomically re-keys**: under the scope election flock, write the
   session-id-keyed record (with the incremented generation), then unlink the
   transient record, then update the in-process discovery pointer.
3. Readers resolve by session-id; until the re-key completes, a sibling that
   needs T1 falls back to the transient `server_pid` key advertised via
   `_t1_state` / env (the existing env-passdown path, RDR-105 Path A), so there
   is no undiscoverable window.

This eliminates the N-unknown-sessions collapse (no record is ever keyed
`unknown`) and the undiscoverable window (transient key covers the gap), and the
flock + generation make the re-key safe against a concurrent sibling. This is
the one load-bearing correctness subtlety of the T1 migration (Approach item 5),
tracked as **CA-3**.

**RF-3 (RESOLVED): no generation primitive exists today; add a per-scope
counter incremented under the existing election flock.** T2 elects via a
`.spawn_lock` `fcntl.flock` (`_acquire_spawn_lock`, `t2_daemon.py:1335`;
`_spawn_lock_path_for_db`, `:279`) but persists **no** monotonic
generation/epoch. The fencing token is therefore new work: persist a counter
*inside the lease record*, read-increment-write it while holding the
per-scope election flock at publish time. Flock provides the mutual exclusion;
the counter survives restarts because it lives in the record, not in process
memory or a clock (satisfies the scripts' no-`Date.now` determinism rule).

**RF-4 (RESOLVED): T3's chroma is an external subprocess, so its heartbeat must
be a supervisor-side proxy.** T3 is a managed `chroma run` (`t3_daemon.py:4`,
`start_t3_daemon` at `:226`); nexus writes the discovery file atomically
(`_write_discovery_atomic`, `:101`) but there is **no re-assert loop**
(self-heal mentions = 0). chroma cannot heartbeat a nexus lease itself, so the
nexus supervisor that manages the subprocess heartbeats on its behalf, with
liveness = (lease fresh) ∧ (chroma port reachable). T3 already routes its
discovery path through the shared `daemon.discovery.discovery_path(tier='t3')`
(`t3_daemon.py:79`), so the discovery *record* is already half-shared; the
missing half is the heartbeat/lease semantics.

**RF-5 (RESOLVED): the tiers are already code-isolated, so per-tier migration is
naturally independent.** T1 discovery lives in `session.py` (pid-keyed); T2/T3
in `daemon/discovery.py` (uid-keyed); they import none of each other's
lifecycle code (verified: `t1.py`/`session.py` do not import
`daemon.discovery`, and `daemon/` does not import the `t1_addr` helpers). The
substrate can be introduced and adopted one tier at a time (T2 → T3 → T1) with
the conformance suite (Approach item 1) green at each step; a partial rollout
cannot strand a tier because each tier's consumers resolve through their own
(old or new) path until cut over. This validates the behaviour-preserving,
incremental sequencing and rules out a big-bang rewrite (the RDR-110→113
failure mode).

## Open Questions

- The two prior open questions (T1-as-daemon vs consumer; version-cycle location)
  are now **resolved and folded into §Decision**.
- **MinerU is a confirmed fourth lifecycle instance** (`mineru.pid`,
  `config.py:197-249`, with its own broken endpoint discovery — see RDR-148,
  filed one day before this RDR). It is **fenced out of RDR-149** (see §Out of
  Scope) to hold scope, with the convergence question deferred to a tracked
  follow-on rather than left implicit.
- Residual: should the substrate, once proven on T1/T2/T3, absorb MinerU and the
  aspect-worker poll in a follow-on RDR? (Lean: yes, but only after the
  three-tier conformance suite is green — adopt-by-evidence, not speculatively.)

## Out of Scope

- Storage-engine changes (no Postgres, no tuplespace — RDR-110→113 are
  scrapped; do not revive).
- T1/T2/T3 *data* semantics, schema, or migration content (this RDR is the
  lifecycle plumbing only).
- Host-trust / multi-user / peer-credential models (RDR-113 scrapped;
  single-user assumption unchanged).
- The #956 stacked-review hook itself: it stays on a discovery-free
  `current_session`-keyed marker file and is unblocked by this RDR
  (`nexus-4fw0z`); it is listed as related, not a child.
- **MinerU server lifecycle** (`mineru.pid` / `_restart_mineru_server` /
  `get_mineru_server_url`, `config.py:197-249`): a real fourth instance of this
  pattern, but its immediate fix is **RDR-148** (endpoint discovery +
  subprocess-fallback resilience). Bringing it into RDR-149 now would re-create
  the over-scope that sank RDR-110→113. It is explicitly out of scope here;
  whether it converges onto this substrate is a deferred follow-on (see §Open
  Questions). Decoupling it keeps RDR-149 to the three tiers whose conformance
  failures are already evidenced (#1112, #1114).

## Alternatives Considered

1. **Patch each tier independently (the status quo).** Add self-heal to T1, a
   version-cycle to T3, etc. Rejected: this *is* the recurring pattern — it has
   produced ~10 RDRs and is what generated #1112/#1114. It does not stop the
   class; it adds the next copy to maintain.
2. **Make all three tiers full daemons under one supervisor.** Rejected for T1:
   T1 is intentionally session-scoped (N per user); promoting it to a supervised
   daemon is the "build a fourth thing" anti-pattern (RDR-146) and changes user-
   visible semantics. The chosen design keeps T1 lifespan-owned and only shares
   the *registry primitive*.
3. **Big-bang rewrite of the storage/coordination substrate.** Rejected
   explicitly: this is the scrapped RDR-110→113 chain (9 RDRs, 67 stranded
   beads). The chosen design is a behaviour-preserving, tier-by-tier cutover
   gated by a conformance suite.
4. **A real service-discovery dependency (consul/etcd/zeroconf).** Rejected:
   massive operational and packaging weight for a single-user, single-host,
   localhost-only problem. A leased file record under `~/.config/nexus/` with
   flock election is sufficient and matches the existing model.

## Trade-offs

- **Up-front cost vs. recurring cost.** One substrate + conformance suite is more
  work than patching #1112/#1114 directly, but it amortizes against a class that
  has cost ~10 RDRs and counting. Accepted.
- **A shared primitive is a shared blast radius.** A bug in the primitive can
  affect all three tiers at once, where today a bug is contained to one. Mitigated
  by: the conformance suite (one bug surfaces as a uniform red across tiers,
  caught pre-merge) and behaviour-preserving migration proven tier-by-tier. Net
  positive because the *current* containment is illusory — the same bug already
  recurs across tiers, just discovered serially in production.
- **Lease/heartbeat adds a periodic tick** (~1 stat+read/s/owner when healthy).
  Negligible; already paid by T2 today (RF-1).
- **Generation fencing adds a counter to every publish.** One extra
  read-increment-write under a flock already held for election; negligible.

## Critical Assumptions

- **CA-1: the conformance suite exercises the right failure modes for all three
  tiers.** Verifiable red-first: run the suite against the current
  implementations and confirm it reproduces #1112 (T3 stale-after-upgrade) and
  #1114 (T1 lost-addr-no-self-heal) as failures, and that T2 passes the
  properties T1/T3 fail. If the suite goes green against today's code, it is
  vacuous. (Approach item 1.)
- **CA-2: T2 migration is behaviour-preserving.** Verifiable by code inspection
  + the existing RDR-140/129/141 suites staying green, specifically: exactly-one
  -daemon, never-zero-daemon, loser-quiet-attach, wait-then-force reap, the
  `stop()`-cancels-reassert-before-unlink ordering (`t2_daemon.py:917` before
  `:935`), and the preserved `_t2_ensure_running_inner` interface consumed by
  `mcp_infra._reassert_t2_daemon()`. (Approach item 3.)
- **CA-3: the RF-2 transient-key → session-id re-key protocol has no
  undiscoverable window and no N-unknown collapse.** Verifiable by a spike
  harness against the cold-start lifespan race: assert (i) no record is ever keyed
  `unknown`; (ii) a sibling started during the transient window discovers T1 via
  the `server_pid`/env path; (iii) the re-key is atomic under flock with a
  concurrent sibling racing. (Approach item 5.)
- **CA-4: the flock-serialized generation prevents stale-restart clobber without
  cross-process synchronization beyond the election flock.** Verifiable by a
  restart-race harness: an old owner's delayed shutdown must not unlink or
  overwrite a newer (higher-generation) owner's record. (Approach item 2.)

Every CA above is **Verifiable** (none "Assumed"); each names its verification
method and the Approach item that discharges it.

## Test Plan

- **Cross-tier conformance suite (the load-bearing artifact, Approach item 1).**
  One parameterized property battery run against T1, T2, T3: publish→discover
  roundtrip; survive ungraceful kill→reap; survive restart→republish with a
  higher generation; concurrent siblings converge to exactly one owner;
  version-skew cycle replaces the running owner; pid-reuse immunity; (T1)
  transient-key→session-id re-key has no undiscoverable window.
  - **Acceptance:** 100% of properties pass for a tier before that tier's
    migration bead closes. Red-first against current code is required (CA-1).
  - **Flakiness control:** the multi-process harness uses the RDR-140
    convention (in-process / monkeypatched stack, shrunk interval+timeout
    constants, `port=0`, fixed clocks) so it does not join the `nexus-9eaz`
    flake family. Live-process variants run only under the integration marker.
- **Regression scope (must stay green at every migration step):**
  `tests/daemon/test_t2_*`, the RDR-140 supervisor suite, the RDR-129
  contention suite, the RDR-141 version-skew suite, `tests/daemon/test_rdr146_
  fairness.py`, and the T1 scratch suite.
- **Determinism:** seeded randomness, fixed/injected clocks, `port=0`; no
  wall-clock sleeps in unit tests (the substrate clock is injectable, mirroring
  `T2Daemon._monotonic`).

## Validation

- #1112 and #1114 are validated as **fixed structurally** when their first-red
  conformance properties (T3 version-cycle; T1 self-heal + atomic re-key) flip
  green after the respective tier migration — not by a tier-local patch.
- Post-migration, an inverse-grep audit shows **zero** surviving bespoke copies:
  no `find_immediate_claude_pid` publish path, no per-tier orphan sweep, no
  per-tier election outside the primitive (Approach item 6), mirroring the
  RDR-146 boundary-lint-to-0 discipline.
- A live multi-session shakeout: in a real session, a sibling shell `nx scratch
  list` succeeds whenever the MCP T1 server is up (the #1114 reproducer), and a
  `nx upgrade` cycles T3 as well as T2 (the #1112 reproducer).

## Finalization Gate

- **Pre-accept:** CA-1..CA-4 each have a named verification method and Approach
  owner (above); Layer-1 gap structure present; research RF-1..RF-5 resolved.
- **Phase ordering gate:** CA-1 (conformance suite red-first) and CA-4
  (generation fencing) must be verified **before** Approach item 2 (primitive
  extraction) begins; CA-2 before Approach item 3 closes; CA-3 before Approach
  item 5 closes. Approach item 6 (delete bespoke code) cannot close until the
  inverse-grep audit is zero.
- **Per-phase review:** each migration phase (items 3/4/5) runs
  `/conexus:phase-review-gate 149 --phase N` cross-walking these Approach items,
  plus the stacked code-review-expert + substantive-critic pass.
- **Close condition:** all seven Approach items implemented/traceable, the
  conformance suite green for all three tiers, #1112/#1114 validated fixed, and
  the bespoke-code inverse-grep at zero.

## References

- Recurrence evidence + full root-cause analysis: T2 memory
  `nexus_rdr/daemon-lifecycle-recurring-class-root-cause-2026-06-04`.
- Prior tier-scoped fixes this RDR consolidates: RDR-128 (T2 single-writer),
  RDR-129 (T2 contention), RDR-140 (T2 supervisor / self-heal), RDR-141 (T2
  version-skew), RDR-094 / RDR-105 (T1 chroma lifecycle / env-passdown),
  RDR-063 (T2 domain split), RDR-010 / RDR-041 (T1 scratch origins).
- Triggering issues: GH #1114 (T1 unpublished addr), GH #1112 (T3 stale on
  upgrade); GH #956 / `nexus-4fw0z` (related, discovery-free).
- Anti-pattern precedent: scrapped RDR-110/111/112/113/118/119 big-bang
  substrate chain (`docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md`).
