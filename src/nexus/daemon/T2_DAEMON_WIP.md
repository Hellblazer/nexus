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

- `conexus/daemon/com.nexus.t2.plist` + `conexus/daemon/nexus-t2.service`
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

## What still ships in a follow-up bead (not P3a)

- **P3b migration ownership transfer** (`nexus-e9x4l` — DONE 2026-05-22):
  `apply_pending` removed from `T2Database.__init__` direct-open path.
  The daemon explicitly opts in via `T2Database(path, run_migrations=True)`;
  all other direct-open call sites get the production default
  (`_DEFAULT_RUN_MIGRATIONS=False`) and run against whatever schema
  the daemon (or `nx upgrade`) last left in place. **Operator contract
  during the P3b→P4 window**: keep the T2 daemon running OR run
  `nx upgrade` after every conexus version bump before invoking
  direct-open CLI commands. P4 (`nexus-2ngox`) flips these call sites
  to `T2Client` and the contract goes away.

- **Spawn-lock scope** (RDR-120 P3b code-review item 2): two locks
  held for the daemon's lifetime — the legacy `<config_dir>/t2_spawn.lock`
  (preserves the "one daemon per config_dir" invariant operators
  rely on) and a new `<db_path>.spawn_lock` sibling of the data file
  (prevents two daemons against the same data file from different
  `config_dir`s both running `apply_pending`). Either lock failing
  causes the second daemon to fail loud with a message naming the
  contended lock file.

- **Call-site cutover (P4)**: `nexus-2ngox` flips T2 call sites
  through `T2Client`. Not part of P3a.

## What shipped alongside P3a.A (this branch, follow-up commits)

- **T2 stress harness** (`tests/stress/test_t2_daemon_stress.py`,
  12 scenarios): lifecycle stress, spawn-lock contention, kill -9
  recovery, discovery file corruption, 30-parallel-client storm,
  100-cycle connection churn, SIGSTOP/SIGCONT suspend/resume,
  fail-loud after daemon death, frame protocol robustness (oversized
  frame + garbage bytes), mixed UDS + TCP traffic, repeated SIGTERM
  idempotency. Closes the validation requirement for the P3a
  phase-review-gate (per RDR-120 amendment: harness pass is the
  sole runtime-bug gate; no calendar component).

- **P3 MVV script** (`scripts/rdr120_p3_mvv.py`): two `python -c`
  subprocesses share T2 memory state via the daemon. Mirrors the
  P1 and P2 MVV scripts; supports `--auto-start` for one-shot
  ad-hoc daemon spawning. Closes `nexus-uai7p`.

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
