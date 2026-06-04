---
title: "Unified Leased Service-Registry Substrate for T1/T2/T3 Daemon Lifecycle: End the Recurring Discovery / Single-Writer / Self-Heal Bug Class"
id: RDR-149
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-04
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
rediscovering the same bug in a copy that was not patched.

### Two compounding design errors underneath

1. **PID is the wrong identity / liveness primitive.** Addr files are keyed by
   and reaped on PID. PIDs are reused; "process alive" ≠ "endpoint healthy"; and
   the writer (MCP server) and reader (sibling shell) can resolve different PIDs
   via the `find_immediate_claude_pid` walk. T1 specifically can key on
   **session-id** — both the writer and reader already compute
   `resolve_active_session_id()` identically from
   `~/.config/nexus/current_session` (verified live: both return `c76c1995`).
   Session-id keying deletes the pid-reuse class, the writer/reader divergence,
   and the entire PPID-walk machinery.
2. **No cross-tier invariant test.** Each tier's tests assert only its own
   behaviour, so a property proven for T2 (survive restart with a discoverable
   endpoint; survive concurrent siblings; survive version skew) is never
   asserted for T1/T3. Missing features and regressions in T1/T3 stay invisible
   until a human hits them in production.

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
- **Fencing token.** A monotonic generation per scope; a stale restart cannot
  clobber a newer owner's record (compare-and-set on generation). This makes
  restart-republish atomic and immune to the "old server's shutdown unlinks the
  new server's file" race (#1114).
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

## Approach

1. **Conformance suite first, against the current three implementations [MEDIUM].** Write one parameterized lifecycle property battery (publish→discover roundtrip; survive ungraceful kill→reap; survive restart→republish-with-new-generation; concurrent siblings converge to one owner; version-skew cycle; pid-reuse immunity) and run it against T1, T2, T3 as they exist now. The red cells are the exact, evidence-based scope; #1112 and #1114 appear here as failing tests, not speculative scope.
2. **Extract the leased/fenced/atomic registry + supervisor primitive [LARGE].** A single module (lease record schema, atomic publish via rename, heartbeat/TTL liveness, monotonic generation fencing, scope-keyed flock election, self-heal re-assert loop). Pure, deterministic, fixed-clock-testable; no tier-specific code.
3. **Migrate T2 onto the primitive first (the reference) [MEDIUM].** T2 has the most complete behaviour, so its migration is behaviour-preserving and the conformance suite proves no regression. Replaces the bespoke `daemon/discovery.py` + election + RDR-140 re-assert with calls into the primitive.
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
inherited env. **Design constraint:** the substrate must (a) publish the lease
lazily / re-key it in the heartbeat loop once the session resolves, not eagerly
at spawn, and (b) when the key is still `unknown`, **refuse to publish a
session-scoped record** (or fall back to a guaranteed-unique key) so N unknown
sessions never collapse into one record. This is the one correctness subtlety
of the T1 migration (Approach item 5).

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

- Should T1 become a supervised process under the same supervisor as T2/T3
  (session-scoped), or stay an MCP-lifespan-owned chroma that merely *consumes*
  the registry primitive? (Lean: consume the primitive, do not add a daemon —
  RDR-146 cutover-not-rebuild.)
- Does the version-skew cycle belong in the supervisor (so upgrade cycles all
  tiers uniformly) or remain in `upgrade.py` per tier? (Lean: supervisor, so
  #1112's class cannot recur in a future fourth service.)
- Is there a fourth lifecycle instance already (e.g. the aspect-worker poll, or
  a future service) that should be in scope now to avoid a fourth copy?

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
