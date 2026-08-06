# `nexus.daemon` — AGENTS.md

The shared daemon-lifecycle substrate. **One** leased/fenced/atomic service-registry primitive backs the discover / single-writer / self-heal / version-skew lifecycle for every tier it supervises. The interesting policy is the standing gate below: there is no longer a per-tier lifecycle copy, and there must never be one again.

The tier roster has SHRUNK since RDR-149 collapsed it — the T2 and T3 daemons are both retired — but the gate is unchanged, and applies to whatever tiers `TIERS` currently names. Historical references to "all three storage tiers (T1, T2, T3)" below are heritage, not current state.

## The standing gate (RDR-149, the load-bearing rule)

**Any future lifecycle fix lands in the shared primitive (`service_registry.py`) plus the cross-tier conformance suite (`tests/daemon/test_rdr149_lifecycle_conformance.py`) — NEVER in one tier's copy.**

This is the stop-the-bleeding gate. RDR-149 was created because the same lifecycle bug class (discovery loss, single-writer races, missing self-heal, stale-after-upgrade) was the target of ~10 RDRs and 156 commits in 90 days — each incident teaching exactly one tier a lesson the other two never received, because T1 (`session.py`), T2 (`t2_daemon.py`), and T3 (`t3_daemon.py`) each had a bespoke copy with no shared code. The fix collapsed all three onto one primitive. The gate keeps them collapsed.

Concretely, when you touch lifecycle behaviour (how an owner is published, discovered, reaped, fenced, restarted, self-healed, or version-cycled):

1. The change goes in `service_registry.py` (`ServiceRegistry` / `ServiceSupervisor`) — **not** in a single tier's daemon/consumer. (The shared-helper module `discovery.py` was the other home for this; it is deleted.)
2. Add or extend a property in the conformance suite's `EXPECTATIONS` matrix so the new behaviour is asserted for **every** tier in its `TIERS` tuple — currently `t1`, `t3`, `storage_service`, `aspect_worker` — not just the one you were debugging. **Read `TIERS` rather than trusting this list**; `t2` was removed from it in sub-stage B, and a stale cell is rejected by the matrix's own non-vacuity meta-test. A tier that legitimately cannot satisfy a property records a documented `GAP`/`SPEC` cell with a reason, never a silent omission.
3. If a property is genuinely tier-specific (e.g. T1's transient-key → session-id re-key, CA-3), it still lives in the conformance file alongside the shared battery, with a non-vacuity meta-test guarding it.

A reviewer seeing a lifecycle change that edits one tier's file without a corresponding `service_registry.py` + conformance change should treat it as a defect: it is the exact pattern that produced the recurring bug class.

## Modules

| File | Purpose |
|---|---|
| `service_registry.py` | **The primitive.** `LeaseRecord`, `ServiceRegistry` (publish / heartbeat / discover / mark_shutting_down / relinquish, per-scope election flock, generation fencing), `ServiceSupervisor` (heartbeat cadence + version-skew cycle), `mint_owner_token`. Tier-parameterized by `tier=` + per-call `scope_key`, and **directory-scoped first**: leases live at `<dir>/<tier>_addr.<scope_key>` where `dir=nexus_config_dir()` — a `NEXUS_CONFIG_DIR` sandbox gets an independent lease from `~/.config/nexus` for the same uid (the nexus-tmsnz confusion). |
| `t1_lease.py` | T1 consumer. `T1LeasePublisher` (MCP-lifespan-owned, NOT a supervised daemon) + `discover_t1_lease`. Publishes under a transient `server_pid` key and re-keys to the session-id (RF-2 / CA-3). `scope_key` is the session-id; directory-scoped per the primitive above. |
| `storage_service_daemon.py` | Storage-service supervisor. Consumes `ServiceRegistry(tier="storage_service")`; supervises the native engine binary + Postgres. `scope_key=str(os.getuid())`; directory-scoped per the primitive above. |
| `aspect_worker_daemon.py` | Aspect-worker consumer. Leased, per-tenant host for the aspect queue. |
| `binary_install.py` / `binary_lifecycle.py` | Engine-binary download, pin verification, and version-cycle wiring. |
| `mineru_lifecycle.py` | MinerU sidecar lifecycle. |
| `installer.py` | Daemon install / autostart wiring. INSTALL is service-tier only; UNINSTALL still knows the legacy `t2` unit, because removal machinery outlives what it removes. |

**Deleted, and deliberately absent from this table:** `t2_daemon.py`, `t2_client.py`,
`discovery.py`, `spin_guard.py`, `catalog_write_shim.py` (nexus-i711w Stage 2 sub-stage B)
and `t3_daemon.py`, `t3_client.py` (RDR-155 P4b). `discovery.py`'s helpers had no surviving
consumer once the T3 arm went; the module died whole rather than being folded into the
primitive. `CATALOG_WRITE_OPS` is still load-bearing (`catalog/factory.py`,
`catalog/catalog_protocol.py`) but relocated there directly (commit `004fafa4`) — it does
not live in this package (nexus-2tdkx).

## The two flocks (do NOT conflate) — HISTORICAL as of sub-stage B

The **spawn-lock** was the RDR-128 single-writer guarantee: exactly one daemon opens
the SQLite WAL, and it WAS T2/T3's election. Both spawn paths are gone (T3 at RDR-155
P4b, T2 at nexus-i711w Stage 2 sub-stage B), so no surviving tier has a spawn-lock.

What remains for every tier is the primitive's **per-scope election flock**
(`<tier>_elect.<scope>.lock`, taken briefly inside publish/heartbeat), which serializes
the generation read-increment-write so the fencing token is monotonic. It never
replaced the spawn-lock; it now has nothing to be distinguished from.

Kept as heritage because the distinction explains why the primitive's flock is scoped
the way it is — do not re-derive a spawn-lock from it.

## Adding a lifecycle fix (the checklist)

1. Reproduce the behaviour as a **red** conformance property first (cross-tier where applicable). Red-first against current code is the CA-1 discipline.
2. Implement the fix in `service_registry.py` (the shared substrate), not a tier copy.
3. Flip the conformance cell(s) to `pass`; update the matching non-vacuity meta-test from "reproduces the bug" to "fix landed".
4. Run `tests/daemon/` (the full lifecycle + supervisor + contention + version-skew + fairness suites) green.
5. Run BOTH stacked reviewers at the boundary (see root `CLAUDE.md` § Review Discipline) — the substantive-critic specifically catches the surface this gate exists to protect (a fix that quietly edits one tier).

## Hot rules

- **No per-tier lifecycle copy.** Discovery, liveness, reap, election, self-heal, version-skew all live in the primitive. If you find yourself writing a pid-keyed sweep or a bespoke discovery walk in a tier file, stop — it belongs in the substrate. This is mechanically enforced by `tests/daemon/test_lifecycle_gate.py` (the deleted bespoke addr-file functions stay deleted; `LeaseRecord` + the election flock live only in the primitive). Reintroducing one fails CI.
- **Liveness is lease freshness, not pid.** A dead owner's lease ages out via TTL; never add a new `os.kill(pid, 0)` **orphan sweep** in a tier-consumer file (pid reuse is the bug that causes). This bans the orphan-sweep *shape*, not all pid use. **The exemption list that used to sit here named `discovery.py`, `t2_daemon.py` and `t3_daemon.py` — all three now deleted.** An exemption naming a deleted file never matches, so it silently grants permission to whatever lands there next; that is the same silent-degradation class sub-stage B fixed in four code allowlists, and it is why this list is now empty rather than trimmed. `ServiceRegistry` reaches liveness through `nexus.session._is_pid_alive`.
  **Documented exception (nexus-oyo2g):** `service_registry.sweep_matching_processes` (+ `all_process_rows`/`process_command`/`terminate_pids`/`storage_service_stack_matcher`) IS a process-table, pid-based mechanism — but it does not change `discover()`'s liveness contract above. It answers a narrower, different question: on a lease *MISS*, is the miss a genuinely stopped service, or a discovery gap (a TTL-expired lease on a stalled-but-alive owner — the lz3f2/f9y78 stall signature)? `discover()` still never consults a pid; this fallback fires only inside the idempotent `stop` verb, only after a lease miss, purely to avoid a false "already stopped" and to make `stop` signal the whole discovered process tree (a supervisor's engine child can survive the supervisor's own SIGTERM). It generalizes the mechanism `upgrade_finish.py`'s convergence path already carried for this exact gap (`service_stack_pids` / `_sweep_surviving_stack`, nexus-cfgo9) into the primitive so no tier grows a second copy. Any future use of this exception outside `stop`-shaped idempotent-cleanup semantics needs its own justification here.
- **Generation is the fencing token.** `publish` only ever increments; `heartbeat` raises `StaleOwnerError` when superseded. A delayed predecessor must never clobber a higher-generation successor (CA-4).
- **Stop before relinquish.** Cancel a tier's heartbeat/reassert task BEFORE relinquishing its lease so it cannot resurrect a mid-shutdown record (RDR-129 early-stop ordering).
- **No daemon child is ever silent (nexus-ovbr7).** Never spawn a long-lived daemon or daemon child with `stdout/stderr -> DEVNULL`. Supervisor entry points call `configure_logging(<mode>, config_dir=...)` (rotating file at `<config>/logs/<mode>.log`); child processes (jar, chroma) get `nexus.logging_setup.open_child_log(...)` as stdout/stderr; detached spawns route to `<mode>.crash.log` so pre-configure failures are captured; child exits are logged WITH returncode. Four storage-service supervisor deaths (2026-06) were undiagnosable because all of this went to DEVNULL. Pinned by `tests/daemon/test_storage_service_observability.py`. (Short-lived utility subprocesses handled via returncode may still DEVNULL.)
