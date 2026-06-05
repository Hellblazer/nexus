# `nexus.daemon` — AGENTS.md

The shared daemon-lifecycle substrate. **One** leased/fenced/atomic service-registry primitive backs the discover / single-writer / self-heal / version-skew lifecycle for all three storage tiers (T1, T2, T3). The interesting policy is the standing gate below: there is no longer a per-tier lifecycle copy, and there must never be one again.

## The standing gate (RDR-149, the load-bearing rule)

**Any future lifecycle fix lands in the shared primitive (`service_registry.py`) plus the cross-tier conformance suite (`tests/daemon/test_rdr149_lifecycle_conformance.py`) — NEVER in one tier's copy.**

This is the stop-the-bleeding gate. RDR-149 was created because the same lifecycle bug class (discovery loss, single-writer races, missing self-heal, stale-after-upgrade) was the target of ~10 RDRs and 156 commits in 90 days — each incident teaching exactly one tier a lesson the other two never received, because T1 (`session.py`), T2 (`t2_daemon.py`), and T3 (`t3_daemon.py`) each had a bespoke copy with no shared code. The fix collapsed all three onto one primitive. The gate keeps them collapsed.

Concretely, when you touch lifecycle behaviour (how an owner is published, discovered, reaped, fenced, restarted, self-healed, or version-cycled):

1. The change goes in `service_registry.py` (`ServiceRegistry` / `ServiceSupervisor`), or in a shared helper in `discovery.py` — **not** in a single tier's daemon/consumer.
2. Add or extend a property in the conformance suite's `EXPECTATIONS` matrix so the new behaviour is asserted for **every** tier (`t1`, `t2`, `t3`), not just the one you were debugging. A tier that legitimately cannot satisfy a property records a documented `GAP`/`SPEC` cell with a reason, never a silent omission.
3. If a property is genuinely tier-specific (e.g. T1's transient-key → session-id re-key, CA-3), it still lives in the conformance file alongside the shared battery, with a non-vacuity meta-test guarding it.

A reviewer seeing a lifecycle change that edits one tier's file without a corresponding `service_registry.py` + conformance change should treat it as a defect: it is the exact pattern that produced the recurring bug class.

## Modules

| File | Purpose |
|---|---|
| `service_registry.py` | **The primitive.** `LeaseRecord`, `ServiceRegistry` (publish / heartbeat / discover / mark_shutting_down / relinquish, per-scope election flock, generation fencing), `ServiceSupervisor` (heartbeat cadence + version-skew cycle), `mint_owner_token`. Tier-parameterized by `tier=` + per-call `scope_key`. |
| `discovery.py` | Shared discovery helpers reused by every tier's read path: `_resolve_lease_record(raw, path, *, tier=...)`, `is_lease_record`, `normalize_discovery_view`. Liveness is lease freshness (TTL), not pid. |
| `t1_lease.py` | T1 consumer. `T1LeasePublisher` (MCP-lifespan-owned, NOT a supervised daemon) + `discover_t1_lease`. Publishes under a transient `server_pid` key and re-keys to the session-id (RF-2 / CA-3). Session-scoped (N owners per uid). |
| `t2_daemon.py` | T2 daemon. Owns the SQLite+FTS5 WAL writer; consumes `ServiceRegistry(tier="t2")` + a spawn-lock (RDR-128 single-writer). uid-scoped. |
| `t2_client.py` | T2 client transport (UDS + loopback TCP). |
| `t3_daemon.py` | T3 supervisor + daemon. `T3Supervisor` consumes `ServiceRegistry(tier="t3")`; long-lived, version-cycled via `cycle_to_current`. uid-scoped. |
| `t3_client.py` | T3 client factory (RDR-120). Returns a `T3Database` backed by an HTTP client pointed at the running T3 daemon; local mode only. |
| `catalog_write_shim.py` | Catalog write dispatch hosted in the T2 daemon (RDR-146). |
| `installer.py` | Daemon install / autostart wiring. |

## The two flocks (do NOT conflate)

For the uid-scoped tiers (T2/T3) there are **two** locks with **two** concerns:

- The **spawn-lock** is the RDR-128 single-writer guarantee — exactly one daemon opens the WAL. It IS T2/T3's election.
- The primitive's **per-scope election flock** (`<tier>_elect.<scope>.lock`, taken briefly inside publish/heartbeat) only serializes the generation read-increment-write so the fencing token is monotonic. It does NOT replace the spawn-lock.

Both are required for T2/T3. For T1 (no spawn-lock, MCP-lifespan-owned, session-scoped) the primitive's flock IS the whole election.

## Adding a lifecycle fix (the checklist)

1. Reproduce the behaviour as a **red** conformance property first (cross-tier where applicable). Red-first against current code is the CA-1 discipline.
2. Implement the fix in `service_registry.py` / `discovery.py` (the shared substrate), not a tier copy.
3. Flip the conformance cell(s) to `pass`; update the matching non-vacuity meta-test from "reproduces the bug" to "fix landed".
4. Run `tests/daemon/` (the full lifecycle + supervisor + contention + version-skew + fairness suites) green.
5. Run BOTH stacked reviewers at the boundary (see root `CLAUDE.md` § Review Discipline) — the substantive-critic specifically catches the surface this gate exists to protect (a fix that quietly edits one tier).

## Hot rules

- **No per-tier lifecycle copy.** Discovery, liveness, reap, election, self-heal, version-skew all live in the primitive. If you find yourself writing a pid-keyed sweep or a bespoke discovery walk in a tier file, stop — it belongs in the substrate. This is mechanically enforced by `tests/daemon/test_lifecycle_gate.py` (the deleted bespoke addr-file functions stay deleted; `LeaseRecord` + the election flock live only in the primitive). Reintroducing one fails CI.
- **Liveness is lease freshness, not pid.** A dead owner's lease ages out via TTL; never add a new `os.kill(pid, 0)` **orphan sweep** in a tier-consumer file (pid reuse is the bug that causes). This bans the orphan-sweep *shape*, not all pid use: the retained probes in `discovery.py` (legacy-format upgrade-window interop), `t2_daemon.py` (spawn-lock guard against killing a recycled pid), and `t3_daemon.py` (SIGTERM/SIGKILL shutdown signaling) are documented exceptions and must not be expanded into liveness sweeps.
- **Generation is the fencing token.** `publish` only ever increments; `heartbeat` raises `StaleOwnerError` when superseded. A delayed predecessor must never clobber a higher-generation successor (CA-4).
- **Stop before relinquish.** Cancel a tier's heartbeat/reassert task BEFORE relinquishing its lease so it cannot resurrect a mid-shutdown record (RDR-129 early-stop ordering).
