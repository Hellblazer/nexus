# T2 Daemon — RDR-120 P3a.A archive port (WIP)

This file documents the preemptive port of the T2 daemon from
`archive/develop-2026-05-19` into `feature/nexus-7aayk-rdr-120-p3a-t2-daemon`.
**Do not merge as-is.** The strip + wiring pending in follow-up commits.

## What landed in commit 1 (verbatim port)

- `src/nexus/daemon/t2_daemon.py` (1862 lines, archive 2026-05-19)
- `src/nexus/daemon/t2_client.py` (1530 lines, archive 2026-05-19)
- `src/nexus/daemon/introspection.py` (676 lines, archive 2026-05-19)
- `nx/daemon/com.nexus.t2.plist` (launchd template)
- `nx/daemon/nexus-t2.service` (systemd user unit)

Transport is already correct per RDR-120 S2 lock: length-prefixed JSON
frames via `t2_json_dumps` / `t2_json_loads` at `t2_daemon.py:399-461`.
NOT pickle. NOT multiprocessing.connection. The S2 fix-in-place locked
this at RDR-120 P0; the archive predates the lock but happens to
satisfy it.

## What follow-up commits MUST strip (RDR-120 §Out of scope)

The archive carries imports and methods for surfaces explicitly banned
by the RDR-120 moratorium. They must be removed before the PR leaves
draft state.

### Module-level bans (delete entire imports + call sites)

- `nexus.daemon.peer` (PeerCredentials) — **host-trust** (banned)
- `nexus.daemon.tuplespace_service` — **tuplespace** (banned)
- `nexus.daemon.event_stream` — **event-stream RPC** (banned)
- `nexus.daemon.subspace_registry` — **subspace registry** (banned)
- `nexus.cockpit.bindings` — **cockpit panels** (banned, plus does not
  exist on main as of 4.33.x)
- `nexus.tuplespace.api` / `.registry` / `.store` — **tuplespace** (banned)

### t2_daemon.py methods to strip

- `_handle_event_stream` (line ~1434) — event-stream RPC
- `_subspace_add_handler` (closure ~635) and the subspace dispatch
  branches around it
- `_start_binding_watcher` (~1473) — cockpit hook-bridge wiring
- Any tuplespace dispatch lines (the `from nexus.tuplespace.*` imports
  at 813 / 1356 / 1596 sit inside conditional branches; check each)
- The `_cockpit_bindings_disabled()` helper (~104) becomes vestigial
  once binding-watcher is gone

### t2_client.py methods to strip

- Tuplespace API methods (around line 403-406)
- Any subspace-related client methods

### introspection.py

Substrate-internal RPCs (schema / quotas / store_info). Probably keeps
as-is, but audit each RPC for tuplespace / event-stream coupling.

## Wiring pending

- `src/nexus/commands/daemon.py` extends with `nx daemon t2
  {start,stop,status,install,uninstall}` verbs. Port from archive
  `commands/daemon.py` lines 53-1075 (T2 portion); strip subspace
  subcommands.
- `src/nexus/daemon/__init__.py` documentation updated to mention
  `t2_client`, `t2_daemon`, `discovery.find_t2_daemon`.

## Tests pending

- `tests/daemon/test_t2_daemon_lifecycle.py` (parallel to
  `test_t3_daemon_lifecycle.py`).
- `tests/daemon/test_t2_client.py` (parallel to `test_t3_client.py`).
- `tests/daemon/test_t2_install.py` (parallel to `test_t3_install.py`).
- Migration-ownership tests (RDR-120 §Approach Phase 3a: "Daemon owns
  migration on its own path"; clients carry expected-schema-version,
  handshake fails loud on mismatch).

## Soak invariant

P3a.A is "transport only". T2 call sites do NOT flip in this phase;
`NX_STORAGE_MODE=direct` remains the default. P3a soaks ≥3 days before
P3b may land; combined P3 soak from P3a ship to P4 open is ≥7 days
end-to-end.

## P3 MVV

Two `claude -p` subprocesses in different working dirs construct
`T2Client` against the live daemon and share `memory_put` / `memory_get`
(cross-process daemon-mediated state). Validates client-traffic
against the daemon; the global call-site flip is deferred to P4.
Companion script: `scripts/rdr120_p3_mvv.py` (deferred; mirror
`scripts/rdr120_p2_mvv.py`).

## Why preemptive?

P3a.A is structurally the largest implementation bead in RDR-120 (T2
daemon owns 7 domain-store SQLite handles, dispatch table for ~50+
RPC methods, migration ownership transfer, UDS + TCP transport,
discovery file, autostart). Landing the archive port verbatim now —
with the strip + wiring pending — establishes the branch surface so
follow-up commits can review the strip diff in isolation rather than
mixing it with the import. Independent of the P2 soak (which gates
when `nexus-7aayk` may *open* as a bead, not when this WIP branch
may *exist*).

## Strip strategy decision (pending)

The archive carries ~60 banlist references spread across imports,
admin-op tables, constructor parameters, dispatch-table builders,
closure handlers, and inline-imported helpers. Two paths:

**Surgical strip** of the archive port (in place):
- Remove banned imports
- Delete methods that reference them (e.g. `_handle_event_stream`,
  `_subspace_add_handler`, `_start_binding_watcher`)
- Trim constructor parameters (`event_stream_handler`,
  `tuplespace_service`, etc.)
- Risk: cross-cutting concerns leave orphaned references, comments,
  helpers that are now dead. Strip diff is large and unreviewable.

**Fresh rewrite** of substrate-only core:
- Keep the archive's frame protocol (`t2_json_dumps`, `t2_json_loads`,
  `write_frame`, `read_frame`) and the type-tagged encoder verbatim.
- Write a minimal `T2Daemon` class: `__init__`, `start`, `stop`,
  `run_until_signal`, UDS + TCP bind, connection handler, dispatch
  table built only from `_T2_STORE_ATTRS` + `_T2_DATABASE_METHODS`.
- Same shape for `T2Client`: connect, send-frame, receive-frame,
  method-proxy for each of the 8 stores' methods.
- Target ~600 LOC per module; archive lives as reference material.
- Cleaner diff but more original code to land + test.

Decision deferred to the next session. The verbatim archive port
exists in commit 1 of this branch as reference. The strip / rewrite
work is a clean follow-up.

## Branch state

- Commit 1 (this commit): verbatim archive port + templates + WIP doc.
- Branch will NOT import on its own (banned modules are referenced
  but absent from main).
- No tests, no CLI wiring.
- DRAFT PR #916 for tracking; not for merge as-is.

Bead: nexus-7aayk (still OPEN; this branch is preemptive structure,
not a closing commit set).
