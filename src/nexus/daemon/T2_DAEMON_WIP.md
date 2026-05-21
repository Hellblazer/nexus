# T2 Daemon: RDR-120 P3a.A status

Branch: `feature/nexus-7aayk-rdr-120-p3a-t2-daemon` / PR #916.

## Status: substrate-only scaffold shipped (2026-05-21)

Path 2 (fresh rewrite from archive reference) chosen and executed.
Archive port deleted; clean substrate-only modules in place.

## What ships

- `src/nexus/daemon/t2_daemon.py` (~560 LOC)
  - `T2Daemon` class with full lifecycle: start / run_until_signal /
    stop. UDS bind + loopback TCP bind via separate sockets; both
    served by the same asyncio dispatch handler.
  - `t2_json_dumps` / `t2_json_loads` / `write_frame` / `read_frame`
    plus the type-tagged encoder, ported verbatim from archive.
  - `_build_dispatch_table` for the seven domain stores plus the
    `database.*` pseudo-store. NO admin ops, NO UDS-only gate (per
    moratorium; daemon will not accept admin RPCs until those land
    in a future RDR).
  - Spawn lock via `fcntl.flock` on `~/.config/nexus/t2_spawn.lock`.
  - Discovery file `~/.config/nexus/t2_addr.<uid>` with both
    `uds_path` and `tcp_host` + `tcp_port`.
  - `run_t2_daemon()` sync entrypoint for the CLI verb.

- `src/nexus/daemon/t2_client.py` (~250 LOC)
  - `T2Client` class with `__enter__` / `__exit__` / `close`.
  - `_StoreProxy` attribute-driven RPC dispatch:
    `client.<store>.<method>(*args, **kwargs)` builds the op string
    and ships a framed request.
  - C2 precedence: env-first (`NX_T2_SOCK` then `NX_T2_ADDR`),
    file-fallback, UDS preferred when both available, fail-loud on
    unreachable target.
  - `make_t2_client()` factory.
  - `T2DaemonNotReachableError` (transport) and `T2ClientError`
    (daemon-side exception round-trip).

- `src/nexus/commands/daemon.py`
  - `nx daemon t2 {start,stop,status,install,uninstall}` verbs
    wired through the new `T2Daemon` API. `start` is always
    foreground (the daemon IS this Python process; no detached
    mode); supervisors (launchd / systemd) treat it as their
    supervised foreground process.

- `nx/daemon/com.nexus.t2.plist` + `nx/daemon/nexus-t2.service`
  - Templates updated to remove the `--foreground` arg (no such
    flag exists on `nx daemon t2 start` because the start command
    always foregrounds).

- Tests
  - `tests/daemon/test_t2_daemon_lifecycle.py` (15 tests): discovery
    file shape, frame protocol round-trip, dispatch-table build,
    start/stop happy path with real UDS + TCP sockets, spawn-lock
    invariant, public-surface assertions.
  - `tests/daemon/test_t2_client.py` (7 tests): construction, store
    proxies, fail-loud no-daemon, end-to-end memory.put / search
    round-trip, unknown-op surfaces T2ClientError.

## What is intentionally NOT here (RDR-120 §Out of scope moratorium)

- NO peer-credentials module (host-trust)
- NO event_stream subscription RPC
- NO subspace registry
- NO tuplespace service
- NO cockpit binding watcher
- NO introspection RPCs (the archive's introspection.py is gone)
- NO admin-ops UDS gate (no admin ops exist; gate will be designed
  fresh if/when one is added post-moratorium)
- NO eighth domain store yet (`catalog` joins at P5)

## What still ships in a follow-up bead (not P3a.A)

- **P3a soak validation script**: `scripts/rdr120_p3_mvv.py` mirroring
  `scripts/rdr120_p2_mvv.py`. The P3 MVV per §MVV table: two
  `claude -p` subprocesses in different working dirs share
  `memory_put` / `memory_get` via the T2 daemon. Tracked under the
  P3a soak marker bead, not under P3a.A.

- **P3b migration ownership transfer** (`nexus-e9x4l`): remove
  `apply_pending` from `T2Database.__init__` so the daemon is the
  sole `apply_pending` caller. Currently the daemon's
  `T2Database(self._db_path)` still triggers `apply_pending` per
  RDR-120 §A6 P3 transition mitigation. P3b lifts that.

- **P3a.C P3 MVV bead** (`nexus-uai7p`): the two-subprocess MVV
  itself; runs during the P3a soak window.

- **Call-site cutover (P4)**: `nexus-2ngox` flips T2 call sites
  through `T2Client`. Not part of P3a.

## Local verification

```
uv run pytest tests/daemon/
  -> 78 passed
uv run nx doctor --check-storage-boundary --phase 3a
  -> violations 16; catalog-allowlist 2; unchanged from P2 baseline.
uv run nx daemon t2 --help
  -> shows start / stop / status / install / uninstall
uv run nx daemon t2 start &        # foreground daemon
uv run nx daemon t2 status         # reads discovery file
uv run nx daemon t2 stop           # SIGTERM
```

## Soak posture

Branch was preemptively staged ahead of the P2 soak. P3a.A may
formally OPEN as an active bead only after the P2 soak completes
(≥7 days under `NX_STORAGE_MODE=daemon` on main following #914's
merge). This PR's merge does not move the soak clock; it just
ensures the implementation is ready when the soak does clear.

Bead: nexus-7aayk
