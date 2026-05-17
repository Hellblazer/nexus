# Changelog

All notable changes to Nexus are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Tests (RDR-110, nexus-r6u5: 10-worker work-stealing MVV harness)

New spike test under
`tests/tuplespace/spikes/test_r6u5_work_stealing_mvv.py`. Pre-populates
a shared `tasks/r6u5` subspace and races 10 worker threads to drain it,
asserting four invariants against the SQLite + claim-log audit trail:

- Exactly N tuples consumed (no duplicate claims, no missed tuples).
- Zero rows still in `claim_state='claimed'` after drain.
- `tuple_claim_log` records exactly 2 * N rows (one `claim` + one `ack`
  per tuple).
- No tuple_id appears as the target of two distinct active claims
  (CAS race guard).

Two parametrised modes:

- **Direct**: workers open their own SQLite WAL connections and call
  `api.take`/`ack` synchronously. Exercises the CAS UPDATE ... RETURNING
  contention path under WAL.
- **Daemon**: workers route every take/ack through `T2Client` over a UDS
  socket to a `T2Daemon` with a wired `TuplespaceService`. Exercises
  RPC serialisation through the service's single-writer `self._lock`
  plus the single-writer SQLite guarantee.

Default `N=200` runs in ~50s per mode (~2 minutes for the pair); the
full 1000-tuple variant required by the RDR-110 acceptance criterion
is gated behind `@pytest.mark.slow`.

Observed baseline (Apple M-series, EphemeralClient, ONNX MiniLM, N=200):

| mode | elapsed | p50 | p99 |
|---|---|---|---|
| direct | 43.3s | 269.5ms | 597.3ms |
| daemon | 54.8s | 261.9ms | 398.7ms |

Two real-substrate issues surfaced and were resolved during
authoring:

- Semantic-mode `take()` with `default_n=5` starves the worker pool
  after the first 5 consumed tuples because chroma keeps returning the
  same top-K (it has no consumption signal). Test YAML bumps
  `default_n` to 100 and the workers vary the query text per
  attempt so chroma's top-K rotates.
- `TuplespaceService.take` serialises through `self._lock`, so 10
  concurrent take RPCs queue. The RDR-114 default 5s per-RPC timeout
  is too aggressive under sustained contention; the spike's daemon
  client uses `rpc_timeout_seconds=30.0`.

Both findings are characterisation observations, not regressions; the
behaviour is well-defined and the spike documents it for future
substrate work.

### Tests (RDR-111, nexus-2oa6: CA-3 read-latency spike at 10k/50k/100k)

New spike test under `tests/tuplespace/spikes/test_ca_3_read_latency.py`
characterising `ts_api.read()` latency at four scales (1k smoke +
10k/50k/100k slow). Populates each scale via direct Chroma batch
upsert + SQL `executemany` to bypass per-record `api.out` overhead so
even the 100k setup completes in ~13 minutes (vs hours through
`api.out`).

Baseline captured on Apple M-series with ChromaDB EphemeralClient and
the bundled ONNX MiniLM embedder:

| N | p50 | p95 | p99 | max |
|---|---|---|---|---|
| 1,000 | 37.7ms | 39.5ms | 41.0ms | 42.0ms |
| 10,000 | 50.2ms | 52.1ms | 53.7ms | 55.0ms |
| 50,000 | 111.8ms | 116.5ms | 126.3ms | 126.5ms |
| 100,000 | 225.6ms | 234.6ms | 243.2ms | 275.6ms |

Scaling pattern is roughly sub-linear (Chroma HNSW gives log-N-ish
growth). 100x more tuples (1k → 100k) yields only ~6x p99 growth.
The 1k smoke runs in the default suite; the 10k/50k/100k cases are
gated behind `@pytest.mark.slow` (deselected by `pyproject.toml`).
Conservative p99 ceilings (~10x observed baseline) catch a
full-scan regression without flaking on hardware variance.

### Added (RDR-111, nexus-7lb9: Bindings CRUD MCP tools)

Four new MCP tools and a backing helper module make cockpit
bindings runtime-managed without a daemon restart.

- **New MCP tools** (`nexus.mcp.core`): `binding_create`,
  `binding_list`, `binding_toggle`, `binding_delete`. All operate
  on user-owned profile YAMLs under
  `~/.config/nexus/bindings/profiles/`; the shipped builtin profiles
  under `nx/tuplespace/builtin/bindings/profiles/` are read-only
  via this surface (operators edit those by hand if needed).
- **`Binding.enabled` field** (`nexus.cockpit.bindings`). New
  optional field on the `Binding` dataclass, default `True`,
  parsed from the YAML `enabled:` key. `_BindingWatcher._dispatch_event`
  skips bindings with `enabled=False` so `binding_toggle` has a real
  flag to flip without removing the binding row.
- **Watcher hot-reload** (`_BindingWatcher._reload_if_changed`).
  When constructed with `profiles_dirs=[...]`, the watcher fingerprints
  per-file mtimes across those dirs and rebuilds `self._profiles`
  whenever a source file changes. Called once per `_tick`; a stat
  call per `*.yml` per tick is negligible against the existing 50ms
  cadence. CRUD writes take effect on the next tick.
- **`bindings_crud` helper module**
  (`nexus.cockpit.bindings_crud`). Synchronous filesystem helpers
  underneath the MCP tools: `create_binding`, `list_bindings`,
  `toggle_binding`, `delete_binding`. Profile YAML is created on
  demand for the first binding and removed when the last binding in
  it is deleted (an empty `bindings: []` would fail validation on
  the next load).
- **Daemon wiring** (`T2Daemon._start_binding_watcher`). The watcher
  now loads from both the builtin and user dirs and passes both as
  `profiles_dirs` to `_BindingWatcher` so hot reload sees CRUD
  writes immediately.

Tests: `tests/cockpit/test_bindings_crud.py` with 20 cases
(`TestBindingEnabledField` + `TestBindingsCrudHelpers` +
`TestBindingWatcherReload` + `TestMcpToolWrappers`).

### Added: RDR-110 Semantic Tuple Space landed (2026-05-14)

ORB tuplespace ships as the substrate the rest of the agentic
substrate stands on. Subspaces declare a schema (dimensions, embed
source, take semantics, retention) in YAML; `out`/`read`/`take`/
`ack`/`nack` are the lifecycle primitives. `tuples.db` lives next to
`memory.db` under the T2 daemon (RDR-112 §9) so subspaces share the
single-writer guarantee.

- **CLI** (`nx tuplespace`): `out`, `read`, `take`, `ack`, `nack`,
  `list-subspaces`, `show-schema`, `stats`. Mutating subcommands
  refuse to open a competing SQLite handle under
  `NX_STORAGE_MODE=daemon`.
- **MCP tools**: `tuplespace_out`, `tuplespace_read`,
  `tuplespace_take`, `tuplespace_ack`, `tuplespace_nack`,
  `tuplespace_list_subspaces`, `tuplespace_subspace_schema`,
  `tuplespace_subspace_stats`. Routed through the daemon RPCs when
  `NX_STORAGE_MODE=daemon`.
- **Idempotent retake** (CA-1/CA-2): same-claimant `take` during the
  active lease returns the existing claim, no extra `tuple_claim_log`
  row, no rotation of `claim_id`.
- **Two-store atomicity** (nexus-qmrr, PR #800): `api.out` writes
  Chroma first, commits SQLite second. SQLite presence implies Chroma
  presence; the reverse window leaves an orphan Chroma record that
  idempotent refire reclaims.
- **Tuple refresh on refire** (nexus-i4kd, PR #806): content-
  identical refire refreshes `created_at`/`expires_at` so the
  retention sweeper does not expire rows based on the first fire's
  clock. Claim and tombstone state survive untouched.
- **Builtin subspaces** under `nx/tuplespace/builtin/`:
  `tasks/<project>`, `locks/<resource>`, `mailbox/<topic>`, `events`,
  `barriers`, `layout_state`, `connection_manifest`, plus the seven
  `hook_events/*` schemas under `hooks/`. Reserved prefixes
  (`tuples/`, `daemon/`, builtins) cannot be minted by third-party
  `subspace_add`.
- **Coordination landing surface** (nexus-90pe, PR #791): four
  consumer-facing subspaces and seven cockpit skills wired up so the
  tuplespace has real Day-1 traffic, not just primitives waiting for
  callers.

### Added: RDR-111 ORB Hook Bridge and Cockpit landed (2026-05-15)

Claude Code hook events now project onto the tuplespace, user-authored
binding profiles react to matching events, and operators see the
resulting state through cockpit panels. The four-RDR cockpit substrate
(110/111/112/113) is now structurally complete on `develop`.

- **Hook bridge** (nexus-y0nb, PR #784): seven `orb_bridge_*.py`
  scripts under `nx/hooks/scripts/` drain Claude Code hooks into the
  seven `hook_events/*` subspaces. `emit()` gates on `CLAUDECODE`
  (RF-5: prevents contamination of non-Claude shells) plus
  `NX_BRIDGE_DISABLE` (operator opt-out; nexus-7zvp, PR #822). The
  `output_for_hook()` pure helper stays unconditional so disabling
  emission cannot break the hook protocol (PermissionRequest
  transparent-allow still fires).
- **Daemon-mode routing** (nexus-6s8v, PR #789): bridge prefers the
  T2 daemon's `tuplespace.out` RPC, falls back to direct mode on
  discovery or RPC failure. Daemon-mode is now the default
  (`_ROUTING_TBA = "daemon"`).
- **SQLite write retry** (nexus-wf07, PR #807): `_sqlite_with_retry`
  wraps the bridge's SQLite writes with bounded exponential backoff on
  `OperationalError: database is locked` / `busy`.
- **Plugin / wheel version-compat guard** (nexus-yeu8, PR #809):
  bridge scripts embed `EXPECTED_BRIDGE_API_VERSION` and skip cleanly
  when the installed wheel exposes a different `BRIDGE_API_VERSION`.
  Stale scripts on a fresh wheel exit 0 instead of corrupting tuples.
- **Cockpit `_BindingWatcher`** (nexus-0xaq, PR #787): async polling
  reaction loop over the events table. User-authored binding profiles
  match events by subspace/op/category and fire `python:<module:func>`
  or `log:<marker>` actions in cursor order. Error containment: one
  bad binding does not crash the loop or starve siblings.
- **Cockpit panels** (nexus-ut5r, PR #802): `nx cockpit
  {status|show|dashboard}` reads `tuples.db` and surfaces recent
  events, active claims, and active bindings. Read-only by
  construction; no panel writes tuples.
- **Retention sweeper** (nexus-kk9h, PR #788): recurring 6-hour sweep
  prunes expired tuples and claim-log rows. Best-effort SQLite-only
  sweep in Phase 1; Chroma vector cleanup deferred.
- **embed_from fail-loud** (nexus-zm2n, PR #800): subspace schemas
  that reference a missing embed source raise `EmbedFromError` with a
  precise error instead of silently writing zero-vector embeddings.
- **Substrate hardening** (PR #803): six coordination YAML schemas
  shipped in the wheel, `nx doctor --check-bridge` for installability
  diagnostics, plugin/wheel packaging verified.

### Added: RDR-113 Host-Trust v1 (2026-05-13)

Single-user host trust boundary for the T2 daemon transports.

- **UDS**: socket mode `0600`, `SO_PEERCRED` peer-credential check at
  accept time. `bind()` then `chmod(0o600)` then `listen()` ordering
  (A1 spike verified) closes the bind-to-chmod window: `connect()` to
  a bound-but-not-listening UDS returns `ConnectionRefusedError`.
- **TCP fallback**: hard-bound to `127.0.0.1`. No peer-cred check on
  loopback (orchestrator trust per single-user host model).
- **Admin gate**: `_ADMIN_OPS` + `_KNOWN_ADMIN_NAMES` frozensets plus
  startup integrity check force admin RPCs to UDS only and catch any
  drift between dispatch-table registration and the gate. Verbs:
  `subspace_add`, `exec_raw`, `export`, plus the `admin_ping` test
  scaffold (production never registers it).
- **Contradiction check filled** (nexus-ycf5, PR #792): RDR-113
  Finalization Gate Contradiction Check section completed with the
  bind-to-chmod analysis and the loopback-TCP justification.

### Added — RDR-112 T2 Storage-as-Service Phase 1 complete (2026-05-14)

T2 SQLite stores now live behind a single-writer asyncio daemon that
clients reach via JSON-RPC over UDS (primary) or loopback TCP (fallback).
All Phase 1 surfaces shipped and gate-reviewed by four parallel auditors;
nexus-52lb GATE PASSED, T2 entry `nexus/rdr-112-phase1-gate-passed.md`.

- **nexus-61x6** (PR #766) — T2 daemon process scaffold + dual-bind
  UDS+TCP transport. UDS at chmod 0600 with peer-cred enforced;
  TCP hardcoded to 127.0.0.1. A3 spike: UDS p50 = 100µs, TCP p50 = 114µs.
- **nexus-qy0u** (PR #768) — 150 domain-store RPCs across 8 namespaces
  (memory, plans, chash_index, taxonomy, telemetry, document_aspects,
  aspect_queue, database) + `T2Client` facade with type-tagged JSON
  (datetime, bytes, Path, dataclass) and pooled connections.
- **nexus-m4gm** (PR #771) — EventStream RPC with rowid-cursored backfill
  capped at 1000 rows/burst, live mode via `PRAGMA data_version` polling,
  and failure-category demux via server-side SQL filter.
- **nexus-w0et** (PR #770) — Daemon-startup migration runner. Daemon is
  sole migration runner per RDR-112 §9; includes the RDR-111 watcher_state
  table per CA-10. Schema version handshake with directional errors.
- **nexus-x98k** (PR #774) — `subspace_add` admin RPC with reserved-prefix
  rejection (`tuples/`, `daemon/`), digest advertisement in hello_ack, and
  fixture-tested registration of all seven RDR-111 hook-event subspaces.
- **nexus-08i1** (PR #775) — Introspection RPCs: `exec_raw` (mode=ro URI,
  row-capped at 50k, audit hash post-execution), `schema`, `peek`
  (clamped to 300), `stats`, `export` (streaming for jsonl/csv via
  fetchmany(256), Backup API for sqlite; path-traversal defense).
- **nexus-pce1.1** (PR #773) — Admin-RPC UDS-only gate. `_ADMIN_OPS` +
  `_KNOWN_ADMIN_NAMES` frozensets + startup integrity check catch any
  drift between dispatch-table registration and gate membership.
- **nexus-pce1.4 + pce1.5** (PR #772) — Denormalize subspace into
  `tuple_claim_log` (eliminates COALESCE-on-deleted-tuple silent drop in
  the EventStream trigger); validate `subspace_prefix` to prevent GLOB
  metacharacter injection.
- **nexus-52lb** (PR #777) — Phase 1 gate cleanup. Duplicate CLI removed;
  `subspace_add` rewrites discovery file digest; new test column
  assertions; public `T2Database.path`; documentation polish.

CLI: `nx daemon t2 {start|stop|info|exec|schema|peek|stats|export|subspace add}`.
`nx daemon t2 start` constructs T2Database + RegistryStore + T2Daemon fully
wired so domain-store RPCs work from day one. Live smoke: memory.put +
memory.get round-trip ≈ 9ms over UDS on M-series Mac.

Out of scope here, tracked for Phase 2/3:
- `tuplespace_*` MCP tool routing under `NX_STORAGE_MODE=daemon`
  (nexus-pce1.6, blocked on Phase 2 CatalogDB collapse).
- Default `NX_STORAGE_MODE` flip from direct to daemon (nexus-507q P6.3).

### Added: RDR-112 Phase 2 in flight (2026-05-15)

- **CatalogDB collapse** (nexus-7ejx, PR #785): eight catalog tables
  (`owners`, `documents`, `documents_fts`, `links`, `collections`,
  `_meta`, `document_chunks`, plus supporting indexes/triggers) move
  into `memory.db` alongside the seven domain stores. Schema
  invariants from RDR-108 preserved: `documents` is tumbler-
  addressable, `document_chunks` is authoritative for the chunk-to-
  doc manifest.
- **Daemon registry seed** (nexus-me9y, PR #801): daemon seeds the
  subspace registry from `nx/tuplespace/builtin/` on every start
  before sockets bind. Builtin schemas become the single source of
  truth for reserved-prefix namespaces; YAML bumps land as UPDATEs,
  unchanged YAMLs are no-ops.
- **Daemon auto-start** (nexus-6w0c, PR #808): `nx daemon t2
  install --autostart` writes the OS autostart unit (launchd plist
  on macOS, systemd user unit on Linux). `KeepAlive: Crashed` plus
  `SuccessExitStatus=143` cooperate so `nx daemon t2 stop` is a
  clean SIGTERM that does not flap-respawn.

### Added: RDR-114 daemon unavailability policy (2026-05-16)

Implements RDR-114 (epic nexus-homw). Unifies daemon-unavailability
behaviour across both client surfaces (subscribers and emitters):
in daemon-routing mode, daemon unavailability is loud and
recoverable, not silent.

- **EventStream reconnect contract** (nexus-wfko, RDR-114 Step 1).
  `T2Client.event_stream` now wraps subscribe in a reconnect loop:
  capped exponential backoff (initial 0.25 s, cap 8 s, max 10
  attempts) with ±25 % uniform jitter, ~48 s nominal budget (range
  36-60 s with jitter), cursor-driven resumption via
  `since_cursor=last`. Delivery is
  **at-least-once**: callers requiring exactly-once must dedup
  via the `action_idempotency` table (RDR-111 / nexus-8wvs) keyed
  on `tuple_id`. New typed exception `EventStreamUnavailable`
  (with `last_cursor` attribute) raised on budget exhaustion;
  `reconnect=False` preserves the legacy single-subscribe
  semantics.
- **T2Client RPC timeout** (nexus-wcs9, RDR-114 Step 4).
  `T2Client(rpc_timeout_seconds=5.0)` applies a socket timeout to
  every recv. A hung daemon (UDS accepts but never replies) now
  surfaces as the new typed exception `RpcTimeoutError`,
  deliberately NOT a subclass of `ConnectionRefusedError` or
  `OSError` so the reconnect wrapper can distinguish hung-daemon
  from gone-daemon. Cloud-mode operators with high Voyage RTT
  can override per-RPC.
- **Bridge fail-closed default** (nexus-jokh, RDR-114 Step 2).
  Under the shipped routing default (`_ROUTING_TBA == "daemon"`),
  the bridge no longer silently falls back to a direct SQLite
  open when the daemon RPC fails. A drop is logged as the
  structlog event `hook_bridge_emit_drop_rpc_failed` with the
  hook type, subspace, and underlying error class. The hook's
  RF-2 transparent-allow stdout is unchanged so user-facing tools
  never see the drop. Operators who knowingly accept the WAL-
  contention risk during planned daemon downtime can opt in via
  the env `NX_BRIDGE_ALLOW_DIRECT_FALLBACK=1`. The gate is keyed
  off `_ROUTING_TBA`, NOT `NX_STORAGE_MODE`, so the rule fires
  uniformly across operator workflows whether or not the storage
  mode env is exported.
- **`nx doctor --check-bridge` operator surfacing** (nexus-6bad,
  RDR-114 Step 3). Two new fields: (a) bridge fail-closed policy
  state (default vs operator override; warns when both
  `NX_BRIDGE_DISABLE` and `NX_BRIDGE_ALLOW_DIRECT_FALLBACK` are
  set since the former exits first and the latter has no effect),
  (b) recent `hook_bridge_emit_drop_rpc_failed` events from
  `~/.config/nexus/logs/daemon.log` within the last 24 hours.

RDR file: `docs/rdr/rdr-114-daemon-unavailability-policy.md`. Spike
script preserved at
`scripts/spikes/spike_rdr114_tuplespace_out_latency.py`
(p99=48 ms local-mode, N=1000).

### Style (em-dash sweep, nexus-oyho Bundles F.1 + F.2)

Mechanical replacement of em-dashes (U+2014) with commas or comma-space
sequences per the project rule (no em-dashes in prose under Hal's
name). `sed -e 's/ — /, /g' -e 's/—/,/g'` applied across two scoped
sets; the third leaf (`nexus-ibdl`, 747-instance CHANGELOG sweep) is
deferred to a separate decision because it touches historical
release-note text where the mechanical rewrite would lose git-blame
granularity for limited reader benefit.

- **`nexus-d7a3` docs sweep**: 49 instances cleared from
  `docs/cli-reference.md` (37) and `docs/architecture.md` (12).
  RDR files under `docs/rdr/` are left untouched (historical record;
  RDR files are never deleted or rewritten per project rule).
- **`nexus-upes` src sweep**: 77 instances cleared from
  `src/nexus/cockpit/` (9), `src/nexus/tuplespace/` (26), and
  `src/nexus/daemon/` (42). All in docstrings and comments; no
  behaviour change. Daemon+cockpit+tuplespace suite 606/606 PASS
  after the sweep.

### Docs (UX + first-run, nexus-m0bw Bundle G)

Two UX-doc follow-ups from the documentation critic, both
discoverability fixes.

- **Cockpit overview in README.md** (nexus-g5qe). The Security
  section name-dropped RDR-113 (host-trust model) but never
  introduced the cockpit substrate the trust model exists to
  protect. New `## Cockpit substrate (RDR-110/111/112)` section
  links each RDR with a paragraph summary: RDR-110 covers the ORB
  tuplespace; RDR-111 the hook bridge + cockpit panels; RDR-112 the
  T2 storage-as-service daemon plus the autostart install path.
- **`tests/commands/test_help_completeness.py`** (nexus-fnhe). New
  test file that walks the Click hierarchy under `nexus.cli.main`
  and asserts every reachable (non-hidden) command has non-empty
  `help` text AND its `--help` invocation exits 0 with a Usage
  block. 388 parametrised cases run in <1 s. A third test scans
  for placeholder tokens (`TODO`, `FIXME`, `TBD`) in any help
  string. Hidden commands (the internal `hook` group) are skipped
  by design.

### Changed (line-level review polish, nexus-z7hf Bundle E)

Seven small follow-ups from the code-review critic. None changes
behaviour for callers using the public API; the watcher rename is
backed by a backwards-compat alias so external imports keep working.

- **`route_payload` documented as a public extension point**
  (nexus-kkp9). The helper at `nexus.cockpit.hook_bridge.route_payload`
  has only one in-tree caller (`emit()`) but is intentionally exported
  under its bare name (no leading underscore) so external bridge
  scripts can decompose a Claude Code hook payload without
  re-implementing the dispatch table. Docstring now spells this out
  and pins the return-shape stability contract.
- **`prune_expired_tuples` docstring corrected** (nexus-qu6t). The
  prior text said `idx_tuples_expires` covers the full sweep; in fact
  the partial index only covers `consumed_at IS NULL` rows. The SELECT
  fetches all expired rows regardless of `consumed_at` (Chroma cleanup
  is this sweeper's job), so consumed-but-expired rows fall through to
  a full scan. Acceptable given typical TTLs and the six-hour sweep
  cadence; docstring now states this explicitly.
- **`TuplespaceService.close()` logs on failure** (nexus-dxap). The
  prior `except Exception: pass` left operators with no signal when
  shutdown didn't actually release the underlying SQLite handle. Now
  emits a `tuplespace_service_close_failed` warning with the error
  message and exception type, then returns. Clean-path callers see
  no log line.
- **`match_text=match_text or None` simplified** (nexus-4ivq).
  `TuplespaceService.out()` passed `match_text or None` through to
  `ts_api.out()`. The empty-string case (`'' or None -> None`) was
  semantically identical to passing `''` through under the existing
  `Optional[str]` contract, so the redundancy was type-misleading.
  Dropped to `match_text=match_text`.
- **`_TupleSpaceWatcher` -> `_DataVersionWatcher` rename** (nexus-zrk4).
  Two unrelated classes used the "watcher" label
  (`_TupleSpaceWatcher` for `PRAGMA data_version` polling,
  `_BindingWatcher` for cockpit binding dispatch over the `events`
  table). The new name makes the role self-evident. Existing imports
  via `_TupleSpaceWatcher` keep working through a backwards-compat
  alias (removed in the next major bump).
- **`_DataVersionWatcher.start()` idempotency pinned** (nexus-fvww).
  The guard `if self._thread is not None and self._thread.is_alive()`
  was already present; this bundle adds two tests
  (`TestDataVersionWatcherStartIdempotent`) that pin the behaviour as
  a contract so a future refactor cannot silently regress to
  double-spawning the poll thread.
- **Adaptive idle backoff in `_DataVersionWatcher`** (nexus-o5tc).
  Polling cadence stays at `_POLL_INTERVAL_BASELINE_SECONDS` (1 ms,
  preserves the RDR-110 CA-5 reactive-take latency contract of p50
  <= 5 ms). After `_POLL_IDLE_RAMP_THRESHOLD` (100) consecutive idle
  polls (~100 ms of dead air), the interval doubles each tick up to
  `_POLL_INTERVAL_MAX_SECONDS` (1 s); activity (any data_version
  increment) resets the cadence to baseline. Steady-state CPU on a
  quiet system drops by ~1000x relative to the always-on 1 ms loop.
  `_next_poll_interval(*, idle_polls, current)` is the pure helper
  tests exercise to pin the cadence shape.

### Tests (RDR-112 coverage gaps, nexus-hwy7 Bundle D)

Two test-only additions closing coverage gaps surfaced by the
360-critique. Pure additions; no behaviour change.

- **`_BindingWatcher._tick` sqlite3.Error handlers** (nexus-a79y).
  Both ``except sqlite3.Error`` blocks (around ``_fetch_event_batch``
  and ``_save_cursor``) were 0% covered. Two new tests in
  ``tests/daemon/test_coverage_bundle_d.py`` patch each call to raise
  ``sqlite3.OperationalError`` mid-tick and assert the watcher
  continues, the cursors stay sane, and no propagation reaches the
  caller.
- **`T2Daemon._retention_loop` body** (nexus-qhxf). The body past
  ``await asyncio.sleep(_RETENTION_SWEEP_INTERVAL_SECONDS)`` was 0%
  covered. Two new tests monkey-patch the interval to 50 ms, spin
  ``_retention_loop`` through 2+ iterations, and assert
  ``_run_retention_sweep_sync`` fires the expected number of times;
  the second test flips ``_stopping`` mid-loop and asserts the loop
  exits without exception via the ``if self._stopping: return``
  mid-loop check.

### Fixed (RDR-112 A2 cockpit boundary, nexus-wlkf Bundle C)

Two cockpit-boundary findings from the 360-critique sweep. Both
were silent-degradation issues that would have surfaced as
performance regressions or security warnings as the daemon
accumulated load over time.

- **`_fetch_event_batch` GLOB rewrite** (nexus-anjo). The cockpit
  binding watcher polled the tuplespace `events` table with
  `WHERE subspace GLOB ? AND rowid > ?` and the default
  `subspace_glob='*'`. SQLite cannot use a B-tree prefix scan with a
  leading wildcard, so `idx_events_subspace_rowid` went unused and
  every poll did a full-table scan (negligible at current volume,
  linear with growth). The query now dispatches on glob shape:
  `'*'` drops the subspace predicate entirely (rowid range walk),
  `'prefix*'` rewrites to a half-open `subspace >= 'prefix' AND
  subspace < 'prefix\\uffff'` range scan that hits the index, and
  anything else (`[abc]*`, `foo?bar`) falls back to GLOB.
  `_build_fetch_event_batch_sql` is the new public-ish helper that
  tests use for `EXPLAIN QUERY PLAN` assertions.
- **`events` table retention** (nexus-anjo, secondary fix). The
  `events` table had no retention sweep, so it grew linearly with
  write volume forever. New `prune_old_events(conn, *,
  retention_seconds=7*86400, now=None)` in
  `nexus.tuplespace.store`; wired into
  `_run_retention_sweep_sync` so the daemon's 6-hour retention loop
  drops events older than 7 days. Failure on the events sweep is
  logged but does not abort the tuples-prune count.
- **`_announce_stdout` gated behind explicit flag** (nexus-l712).
  `T2Daemon` previously wrote the discovery JSON (UDS path, PID,
  TCP port, registry digest) to stdout unconditionally at startup.
  Under a shared stdout sink (containerised orchestrators that pipe
  stdout to a multi-tenant log), the PID + socket-path + install
  fingerprint became visible to anyone with read access to the sink.
  Now opt-in via `T2Daemon(..., announce_stdout=True)` and the
  matching `nx daemon t2 start --announce-stdout` CLI flag; the
  discovery file at `~/.config/nexus/t2_addr.<uid>` remains the
  primary channel and is unaffected.

### Fixed (RDR-112 daemon hardening, nexus-gdb3 Bundle B)

Four operational findings from the 360-critique sweep that affect
daemon install / start / shutdown reliability. All in
`src/nexus/daemon/t2_daemon.py` or `src/nexus/commands/daemon.py`
(plus a `nx doctor --check-bridge` surfacing line for the autostart
drift case).

- **Discovery-file shutdown marker and unlink retry** (nexus-12gb).
  `_unlink_discovery` now writes a `status: "shutting_down"` marker
  (with `shutdown_at` timestamp) into the discovery JSON before
  attempting unlink, and retries the unlink once on `OSError`.
  Together with the PID-liveness probe in `find_t2_daemon()`
  (nexus-j6dj), this closes the stale-discovery race: a reader that
  arrives during shutdown sees the marker and skips the file even
  when transient filesystem errors (NFS hiccups, EROFS, EPERM)
  prevent the unlink from completing.
- **Native Windows refused on `nx daemon t2 start`** (nexus-dl3g).
  `_acquire_spawn_lock` previously logged a warning on
  `sys.platform == "win32"` and returned without holding the lock,
  permitting two daemons to start concurrently. Native Windows is
  out of v1 scope (RDR-112 §Host-Trust Model); the lock acquire
  now raises `RuntimeError` with a message naming the supported
  alternative (run the daemon on Linux or macOS and connect
  Windows-VM clients via the TCP fallback).
- **Install overwrite guard** (nexus-31cr).
  `nx daemon t2 install --autostart` previously called
  `dest.write_text(rendered)` unconditionally, silently clobbering
  operator customisations (extra `EnvironmentVariables`, log-dir
  changes, etc.). The command now compares existing-vs-rendered
  content: identical files are a no-op ("already up to date"),
  divergent files refuse with a diagnostic naming the `--force`
  override, and `--force` overwrites as before. The `--force` flag's
  documentation expands accordingly.
- **Autostart-binary drift detection in `nx doctor`** (nexus-2wvl).
  `_resolve_nx_bin` is captured at install time; a later
  `pip install --upgrade conexus` (or `uv tool` relocation) can
  move the `nx` executable, leaving the launchd plist /
  systemd unit pointing at a vanished path. `nx doctor
  --check-bridge` now parses the installed autostart file (using
  `plistlib` on macOS, `ExecStart=` shlex-split on Linux) and
  surfaces a stale-path warning that names `nx daemon t2 install
  --autostart --force` as the remediation.

### Security (RDR-113 defence-in-depth, nexus-qh9k Bundle A)

Three same-UID surface tightenings on the T2 daemon's wire path. None
exploitable today (admin ops are UDS-only and the daemon's threat
model already excludes cross-UID peers), all defensive against a
buggy or compromised same-UID peer.

- **Dataclass-tag allowlist** (nexus-ac2l). `_t2_decode` previously
  unpacked any `__dataclass__`-tagged dict to a plain dict and
  discarded the qualname. A same-UID client could feed an unexpected
  tag to bypass downstream "is this a dataclass-shaped payload?"
  checks. Decode now consults `_ALLOWED_DATACLASS_TYPES` (a frozenset
  of known wire-traversing dataclass qualnames: `QueueRow`,
  `AspectRecord`, `Tumbler`, `OwnerRecord`, `DocumentRecord`,
  `LinkRecord`, `CatalogEntry`, `CatalogLink`, `ManifestRow`,
  `OrphanPlan`, `DedupePlan`) and raises `ValueError` on unknown
  tags. Encode stays permissive; the allowlist is the strict gate on
  inbound payloads.
- **Frame-size cap lowered to 1 MiB** (nexus-ex4r). Both
  `t2_daemon._MAX_FRAME_BYTES` and `t2_client._MAX_FRAME_BYTES`
  drop from 16 MiB to 1 MiB. Typical T2 RPC payloads (single rows,
  small batches, search params) sit well under 64 KiB; the prior
  cap let a misbehaving same-UID peer announce a 16 MiB header and
  force the daemon to allocate that buffer per connection in
  `readexactly`. Bulk ops that legitimately need a larger frame
  (introspection export, schema dumps) must page their results or
  lift the cap on a per-RPC basis after explicit review.
- **macOS LOCAL_PEERCRED TOCTOU documented** (nexus-qyff).
  `peer.py` module docstring now spells out that
  Linux `SO_PEERCRED` re-derives per `getsockopt` call while macOS
  `LOCAL_PEERCRED` snapshots at peer socket-creation time. Safe
  under the same-UID threat model (the trusted UID is constant), but
  the gap matters if a future trust model widens to "same UID and
  same process binary". Inline note added to `_read_darwin`.

## [4.32.12] - 2026-05-13

Patch on 4.32.11. Two CI-correctness fixes plus substantial RDR
work landed during the gate cycle for RDR-111/112/113. No
user-facing behavior change; main-branch CI is green again.

### Fixed (nexus-rkc0, P2)

- `commands/collection.py:215` delete-cascade routed the non-
  event-sourced `DELETE FROM collections` through
  `cat._db.execute(...)` directly, violating the RDR-101 Phase 3
  ε lint gate
  (`tests/test_no_direct_catalog_writes_outside_projector.py`).
  Introduced by PR #722 and red on CI since 2026-05-12. Fix
  adds a public `Catalog.delete_collection_projection(name, *,
  reason)` method modeled on `register_collection` (lock,
  short-circuit, ES vs legacy branch); cascade calls the verb
  instead of reaching into `_db`. Lint gate 17/17 PASS, catalog
  suites 229/229 PASS. (PR #733.)

### Fixed (test hardening)

- `tests/test_phase5_doc_cite.py` JSON-parsing assertions made
  robust against leading structlog WARNING lines on stdout.
  CliRunner under CI merges stderr → stdout; one-shot WARNINGs
  (`_chash_fallback_warned`,
  `migrate_document_aspects_pk_skip_no_catalog`, and whatever
  comes next) can prefix the JSON payload. Helper
  `_parse_json_payload(stdout)` finds the first `{` and parses
  from there — covers current and future one-shot leaks without
  chasing them individually. (PR #734.)

### Architecture (RDR work)

- **RDR-111** (ORB: Observable Relay Bus) drafted; gated R1–R9,
  final R9 PASSED. Hook-event projection onto the tuple space;
  user-authored bindings; cockpit substrate. (PRs #726, #731.)
- **RDR-112** (Storage-as-Service) drafted; gated R1/R2/R3, then
  triad rework, then light re-gate PASSED. **Accepted
  2026-05-13.** Every persistent shared-state store (T2 seven
  stores, T3 chroma, CatalogDB, future) moves behind per-tier
  daemons. UDS-primary, TCP-fallback. EventStream RPC for
  change events; daemon owns migrations; subspace-registry
  validates daemon-side. `nx doctor --check-storage-boundary`
  AST lint bans direct `sqlite3.connect` / `PersistentClient`
  outside `src/nexus/db/`. Planning chain: epic `nexus-pce1` +
  33 children + 4 cross-RDR beads filed.
  (PRs #726, #728, #729, #730, #735, #737.)
- **RDR-113** (Host-Trust Model) new mini-RDR; R1 PASSED. UDS
  `chmod 0600` + peer-credential check; loopback-only TCP;
  single-user host trust v1. A1 race-window verification:
  `bind() → chmod(0o600) → listen()` ordering closes the window
  to zero because `connect()` to a bound-but-not-listening UDS
  returns `ConnectionRefusedError`. (PR #732.)
- **RDR-110** (Semantic Tuple Space, already Accepted) — 5x5
  alignment pass against post-triad work. §Technical Design and
  §Phase 1 Step 4 watcher descriptions now carry "Mode split —
  daemon vs direct"; mailbox-subspace trust boundary cites
  RDR-113. No design surface change.
  (PRs #727, #730, #736.)

## [4.32.11] - 2026-05-12

Patch on 4.32.10. Post-release sandbox shakeout (pristine
`NEXUS_CONFIG_DIR` + `NX_LOCAL=1` + fresh chroma + index of a
real repository) surfaced three latent bugs in code paths that
the audit umbrella did not exercise directly. All three close in
this release so a fresh-index → delete-collection cycle leaves
every doctor check PASSing — the right pre-flight before a clean
cloud-database cutover.

### Fixed (nexus-qj1q, P1)

- `indexer._prune_misclassified_in_collection` raised
  `chromadb.errors.DuplicateIDError` when two indexed docs shared
  a chunk (common file content vendored to two paths, shared
  boilerplate header, etc.). The post-processing prune step
  accumulated `chash[:32]` from per-doc manifest entries without
  deduping; Chroma rejected the batch and the prune silently
  no-oped, leaving misclassified chunks in T3 indefinitely.
  Fixed by collecting into a `set()` before batching.

### Fixed (nexus-jm3z, P1)

- `nx collection delete` cascaded to T3 + taxonomy +
  `chash_index` + pipeline state but NOT to the catalog's
  `documents` rows pointing at the gone collection, nor to the
  `collections` projection row. Operators saw doctor FAILs
  (`t3-vs-catalog` + `collections-drift`) after every delete and
  remediated per-tumbler. The cascade now runs through
  `Catalog.delete_document` for each orphan (emits
  `DocumentDeleted` events) and emits a new `CollectionDeleted`
  event so the projection row drops via the event log on apply.

### Fixed (nexus-vxz3, P2)

- `nx catalog doctor --replay-equality` reported two false-
  positive divergence classes:
  - `documents.chunk_count` is populated by
    `manifest_write_batch_hook` (post-store side-effect), not
    by the event log. The in-memory replay projection has no
    `document_chunks` table to derive from, so its re-derive
    sees zero rows and keeps register-time `chunk_count=0`
    while live SQLite carries the hook-driven value. Excluded
    from the documents comparison (consistent with the existing
    `LINKS_EXCLUDE` pattern for the `links.id` autoincrement).
  - Admin SQL DELETE on the collections projection bypassed
    the event log. Replay therefore projected more collections
    than live SQLite carried. Closed by the new
    `CollectionDeleted` event type (see `nexus-jm3z` above) so
    the round-trip is symmetric.

### Added

- `CollectionDeleted` event type +
  `_v0_collection_deleted` projector handler. Complements
  `CollectionSuperseded` (which points at a replacement target):
  use `Deleted` when there is no replacement (obsolete embedding-
  model collection, test detritus).

### Tests

- 320 tests pass across `test_indexer`, `test_collection_cmd`,
  `test_catalog`, `test_catalog_event_log`,
  `test_catalog_event_sourced_mutators`,
  `test_catalog_doctor_replay_equality`,
  `test_catalog_shadow_emit`.

### Sandbox verdict

| Stage | All 6 doctor checks |
|---|---|
| Fresh sandbox | PASS |
| Index `~/git/ext-apps` | PASS |
| Delete a collection | PASS |

## [4.32.10] - 2026-05-12

Post-4.32.4 deep-audit umbrella `nexus-58ui` closes. Twelve PRs
consolidate the P0/P1/P2 findings (RDR-108 Phase-3 fallout,
inequality-assertion sweep, hook-drift coverage, deferment-ref
hygiene) plus two follow-on features.

### Fixed (nexus-dxly, P0)

- `aspect_readers._gather_chroma_chunks_by_field`: post-Phase-3
  chunks reassembled in chroma insertion order (chunk_text_hash
  driven, not document order) when no `manifest_lookup` was wired
  and chunks lacked `chunk_index`. Guard detects the corruption
  fingerprint (identity_field == "doc_id" AND chash_position empty
  AND >1 chunks AND every ci == 0) and returns
  `ReadFail("unreachable")` so callers see the structural problem
  instead of silently extracting scrambled text. `_read_chroma_uri`
  propagates `unreachable` from the doc_id gather path rather
  than falling through to the legacy probe.
- `scoring.apply_hybrid_scoring`: file-size penalty for `code__`
  results read `chunk_count` from chunk metadata and defaulted to
  1 for every Phase-3 chunk, silently disabling the penalty. New
  optional `catalog` kwarg batch-resolves `chunk_count` via
  `documents` SQL. `commands/search_cmd` wires the catalog.

### Fixed (nexus-w5zv, P1)

- `manifest_backfill`: post-Phase-3 multi-chunk docs lacking
  `chunk_index` metadata previously collapsed every chunk to
  position 0 and got rejected by the manifest PK
  `(doc_id, position)`. New `Phase3ChunkIndexMissingError` raised
  per-doc; orchestrator catches, increments
  `docs_skipped_phase3_no_index` counter, emits structured
  WARNING. Single-chunk Phase-3 docs unaffected.
- `orphan_backfill`: four manifest-write paths
  (`register_dt_linked`, `register_synthetic`, `apply_csv`,
  `link_by_title`, `link_by_content_hash`) now stable-sort chunks
  by `chunk_index` before assigning position via `enumerate`.
  Pre-Phase-3 chunks land in document order; Phase-3 chunks
  preserve chroma insertion order.

### Fixed (nexus-lrhg, P1 — 6 of 8 sub-findings; lf8f and gaa3 cover the rest)

- `manifest_write_batch_hook`: torn-state window between purge,
  upsert, and `chunk_count` UPDATE. New
  `Catalog.atomic_manifest_replace(doc_id, chunks)` bundles
  DELETE + INSERT + `chunk_count` UPDATE under one
  `CatalogDB.transaction`. Hook routes to it on first-batch
  (position 0 present) so shrink-reindex is now all-or-nothing.
- `CatalogDB.rebuild`: with `PRAGMA foreign_keys=OFF`, `DELETE
  FROM documents` did not cascade to `document_chunks`. Manifest
  rows referencing tombstoned docs survived as silent orphans
  (PRAGMA foreign_keys=ON only enforces new writes). Post
  `_rebuild_inner`, before `foreign_keys=ON`:
  `DELETE FROM document_chunks WHERE doc_id NOT IN
  (SELECT tumbler FROM documents)`. INFO log records purge count.
- `indexer_utils.build_staleness_cache`: bare `except: pass`
  swallowed `_paginated_get` failures and returned an empty cache
  on Phase-3 corpora, forcing a whole-collection re-embed via
  the per-file fallback. Structured WARNING surfaces the failure.
- `EventLog.append` / `append_many`: documented re-entrancy
  invariant; added `append_unlocked` / `append_many_unlocked`
  variants for callers that already hold the catalog directory
  flock (e.g. Catalog mutators bundling SQLite UPDATE + JSONL
  append + event log append).

### Fixed (nexus-gaa3, P2 — finishes lrhg #4)

- `Catalog.write_manifest` and `append_manifest_chunks` normalize
  `chash` to 32-char at INSERT time. Pre-fix
  `manifest_write_batch_hook` stored 64-char while
  `orphan_backfill` stored 32-char; downstream joins
  (`chashes_for_collection`, `docs_for_chashes`) carried a
  `substr(chash, 1, 32)` workaround. Now uniform.
  `atomic_manifest_replace` (above) also truncates.

### Verified shipped (nexus-lf8f, P0 — all 5 sub-bugs already in prior PRs)

- `fire_store_chains catalog_doc_id` plumbed at all 4 sites
  (commands/memory.py, commands/store.py, mcp/core.py,
  exporter.py `_fire_store_chains_grouped_by_doc`).
- Projector post-replay bulk re-derives `documents.chunk_count`
  from `document_chunks` (`projector.apply_many`).
- `catalog_spans.py` `list_failed` flag preserves chash index
  on T3 transient.
- `indexer.py` prune `get_manifest` failure logged + tracked via
  `skipped_doc_ids`.
- `delete_document` cascade-deletes `document_chunks` in both
  event-sourced and shadow paths.

### Tests (nexus-8g79.23 batch 2, .26, .27, .28, .31; .34 already shipped)

- `tests/test_t2_concurrency.py`: 3 warm-up sleeps replaced with
  `threading.Barrier` rendezvous (`8g79.26`). Loaded CI runners
  no longer race past the under-load measurement.
- `tests/test_indexer_modules.py`: 8 `_chroma_with_retry`
  patches replaced with real `chromadb.EphemeralClient`
  collections (`8g79.28`). Real ChromaDB return shapes
  exercised instead of synthetic dicts.
- New `tests/test_operator_pipelines_dispatch.py`: 10 smoke
  tests covering the §D.4 operator dispatch boundary
  (`operator_groupby`, `operator_aggregate`,
  `operator_filter`, `operator_extract`, `operator_rank`,
  `operator_check`, `operator_verify`, `operator_compare`,
  `operator_summarize`, `operator_generate`). Fills the
  CI gap on the integration suites excluded via
  `-m 'not integration'` (`8g79.27`).
- Inequality-assertion sweep batch 2: ~33 `assert X >= N`
  flipped to exact `== N` across 7 files (`test_md_chunker`,
  `test_doc_indexer`, `test_ast_languages`,
  `test_md_preservation`, `test_minified_chunking`,
  `test_local_mode`, `test_catalog_e2e`). Remaining ~144
  inequalities are legitimate (contract-minimums, timings,
  non-deterministic clustering) (`8g79.23`).
- New `tests/test_phase5_doc_cite.py` flake guard:
  pre-consume the chash-fallback one-shot warning so the JSON
  test is order-independent.

### Added (nexus-bw65)

- `nx collection re-embed NAME --to MODEL`: in-place re-embed
  for non-CCE Voyage models (`voyage-3`, `voyage-code-3`).
  Preserves chunk ids, document text, and metadata; only the
  vector changes. `metadata.embedding_model` stamped to the
  target model so subsequent `check_staleness` reads correctly.
  Default `--dry-run`; `--no-dry-run --yes` to apply. CCE
  (`voyage-context-3`) intentionally rejected at parse time.
  Use case: embedding-model upgrade on a sourceless collection
  (store_put-only / MCP-promoted notes) where `nx collection
  reindex` refuses (correctly) because there is nothing to
  re-index from.

### Docs (nexus-8g79.31)

- Anchored 3 orphan deferment refs to beads: `nexus-7bwe`
  (plans.origin column, P3 deferred); `nexus-bw65` (in-place
  re-embed, shipped this release); `nexus-ocu9.11` deferment
  comment in migrations.py updated to reflect closed state
  (shipped in 4.31.0).

## [4.32.9] - 2026-05-12

Patch on 4.32.8. Audit follow-up: RDR-096 P5.1 aspect_worker
batch path migration (nexus-8g79.34). Closes the RDR-096 P5
deprecation cycle.

### Refactored (nexus-8g79.34)

- **``extract_aspects_batch`` return type widened** from
  ``list[AspectRecord | None]`` to
  ``list[AspectRecord | ExtractFail | None]``. Mirrors the
  single-doc ``extract_aspects`` contract introduced in RDR-096
  P1.2. Pre-fix, batch rows with un-sourceable content landed as
  null-fields ``AspectRecord`` (polluting operator SQL fast paths);
  post-fix they land as typed ``ExtractFail`` and the worker
  ``mark_done``s without writing a row.
- **Per-row URI-based content sourcing moved INTO
  ``extract_aspects_batch``**. Empty-content rows now route through
  :func:`nexus.aspect_readers.read_source` with a
  ``chroma://<collection>/<source_path>`` URI — the same path
  single-doc takes. Per-row ``doc_id_lookup`` built from the
  queue-captured ``doc_id`` (nexus-tdgc) so chunk attribution stays
  correct. New ``manifest_lookup`` kwarg passes through to the
  chroma reader for canonical position ordering (4.32.5's
  nexus-8g79.2 plumbing).
- **Items tuple extended** to a 4-tuple
  ``(collection, source_path, content, doc_id)``. Back-compat
  preserved: 3-tuple callers continue to work (``doc_id`` defaults
  to ``""``, falling back to source_path identity probe).
- **``aspect_worker._process_batch`` rewritten**: deleted the
  pre-fetch block (``_source_content_from_t3`` + disk fallback
  dance, ~30 lines). Worker now passes raw queue rows in 4-tuple
  form; ``extract_aspects_batch`` owns sourcing. ``ExtractFail``
  rows handled with ``mark_done`` (mirrors ``_process_row``).
- **``_source_content_from_t3`` shim deleted** from
  ``aspect_extractor.py``. The ``warnings.warn(DeprecationWarning,
  "Slated for removal in RDR-096 Phase 5")`` is gone. The
  ``_T3_CONTENT_CAP_BYTES`` constant deleted with it.

### Tests

New regression test
``test_batch_empty_content_uri_read_fail_yields_extract_fail``
locks the new contract: empty-content row with ``read_source``
returning ``ReadFail`` produces ``ExtractFail`` in the
corresponding slot (not ``_empty_record``). Existing 3 batch
tests' ``fake_batch`` shims extended to accept kwargs +
``*_args``; existing 3-tuple input form preserved via the
normalisation step.

### Result

The single-doc and batch aspect extraction paths now share the
same content-sourcing contract. Behavioural divergence between
the two is eliminated; deprecation cycle closes.

## [4.32.8] - 2026-05-12

Patch on 4.32.7. Audit follow-up: layering violations
(nexus-8g79.10). Closes all 7 inversions identified by the audit
where library modules reached up into the CLI presentation layer.
No behavioural change — pure code movement with re-exports
preserving back-compat for CLI callers.

### Refactored (nexus-8g79.10)

- **V1 — ``_catalog_store_hook`` extracted from
  ``commands/store.py`` to ``nexus.catalog.store_hook
  .catalog_store_hook``**: the audit-flagged
  ``mcp/core.py:1029`` reach-up is gone. ``commands/memory.py``
  (``nx memory promote``), ``commands/store.py``
  (``nx store put``), and ``mcp/core.py`` (MCP store_put) all
  consume from the canonical lower-layer location. Legacy private
  name re-exported from ``commands/store.py`` for back-compat.
- **V2 — sentinels + git helpers extracted from
  ``commands/hooks.py`` to ``nexus._git_hooks_meta``**: library
  callers (``nexus.health._check_git_hooks``) no longer reach up
  into commands/. The CLI module wraps ``git_common_dir``'s
  ``RuntimeError`` as ``ClickException`` at the boundary.
- **V3 — ``default_db_path`` promoted from
  ``commands/_helpers.py`` to ``nexus.config``** (biggest pattern
  — 8+ leak sites): ``mcp_infra``, ``health``,
  ``collection_health``, ``collection_audit``, ``context``,
  ``operators/aspect_sql``, ``merge_candidates``,
  ``console/routes/health``, and ``_session_end_launcher`` now
  import from ``nexus.config`` directly. CLI command modules
  continue to import from ``commands._helpers`` (now a thin
  call-time delegator so test monkeypatches on
  ``nexus.config.default_db_path`` reach the live binding).
- **V4 — MinerU PID-file primitives extracted from
  ``commands/mineru.py`` to ``nexus._mineru_pid``**: ``config.py``
  and ``pdf_extractor.py`` no longer reach up into commands/ for
  ``_is_process_alive`` / ``_read_pid_file``. CLI module
  re-exports under legacy private names.
- **V5 — ``rename_collection_data_plane`` extracted from
  ``commands/collection.py`` to ``nexus.collection_rename``**:
  the indexer's RDR-103 Phase 5 conformant-shape migrator no
  longer reaches up into commands/. The library version requires
  explicit ``t3_db``; the CLI wrapper preserves the ``t3_db=_t3()``
  default for CLI rename + orphan-cleanup paths.
- **V6 — ``Catalog.lookup_doc_id_by_collection_and_path`` public
  helper**: ``db/t2/document_aspects.py:_resolve_doc_id`` no
  longer cracks open raw catalog SQL — calls the new public probe
  method. Failure-tolerant contract preserved (returns ``""`` on
  any error).
- **V7 — ``commands/doc.py:_phase5_search`` reach-up to
  ``mcp/core.search``**: reviewed and **closed as not-a-violation**.
  The function is a documented composable indirection seam (test
  injectability for ``test_phase5_doc_cite``). Replicating MCP
  ``search``'s full pipeline (~150 lines: corpus resolution,
  filter parsing, where-clause handling, cluster_by + topic) in
  a CLI-layer library wrapper is substantial refactor for a P2
  item where the existing code is structurally sound.

### Test patches updated

8 test files updated to patch the new canonical locations:
``test_catalog_cli.py``, ``test_rdr052_verification.py``,
``test_upgrade_e2e.py``, ``test_collection_audit.py``,
``test_doctor_cmd.py``, ``test_collection_rename.py``,
``test_phase5_integration.py``, ``test_git_hooks.py``,
``test_silent_error_logging.py``, ``test_index_reminder.py``.

## [4.32.7] - 2026-05-12

Patch on 4.32.6. Five dependency upgrades, all unblocked by the
``mineru[all]`` → ``mineru[pipeline]`` switch in 4.32.6 (the old
extras pulled vllm / nvidia-cudnn-frontend which had no macOS
wheels and pinned the resolver against ``llama-index-core 0.14``).

### Deps

- **``llama-index-core`` ``0.12.7`` → ``0.14.21``** (nexus-8g79.16):
  two major versions behind, CVE-response blocker per the audit.
  CodeSplitter API surface (the only call site in ``chunker.py``)
  unchanged between 0.12 and 0.14. Verified locally: chunker imports
  + a synthetic Python file produces the expected node count.
- **``mineru`` ``3.0.5`` → ``3.1.11``** (nexus-8g79.19): six weeks
  stale, 11 patch releases since. ``do_parse`` signature unchanged;
  pipeline backend imports clean.
- **``tree-sitter-language-pack`` ``0.7.1`` → ``0.13.0``**
  (nexus-8g79.20): pin relaxed from ``==0.7.1`` to ``>=0.7.1,<1.0``.
  Local resolver picks 0.13.0 (newest 0.x). 1.x is a complete API
  rewrite — the C-binding ``Parser`` exposes no ``parse()``
  method, replaced by a top-level ``process()`` + ``ProcessResult``
  flow. Nexus's ``_extract_context`` (``code_indexer.py:262``)
  calls ``parser.parse(source)`` directly, so 1.x raises
  ``AttributeError`` on every code-indexing run. Migration scoped
  as a follow-up (``nexus-8g79.35``); this patch unblocks 4.32.7
  without touching the chunker.
- **``chromadb`` ``1.5.1`` → ``1.5.9``** (nexus-8g79.21): eight
  patch releases. The ``t3.py`` internal-object timeout patch
  (which overrides chromadb's hardcoded
  ``httpx.Client(timeout=None)``) still applies cleanly — the
  ``client._server._session`` shape is preserved; verified
  ``timeout=None`` and ``httpx.Client`` patterns still present in
  ``chromadb.api.fastapi``.
- **``mcp`` ``1.26.0`` → ``1.27.1``** (nexus-8g79.22): minor
  upstream catch-up. No source changes.

### Cosmetic (nexus-8g79.22)

- **``Settings()`` kwargs migration** in
  ``commands/_provision.py``: replace the chromadb-0.4-era
  attribute-set form (``settings.attr = value``) with constructor
  kwargs. The attribute-set form was deprecated upstream and its
  deprecation window keeps quietly advancing.
- **``urllib.request.urlopen`` → ``httpx``** in
  ``commands/_provision.py``: one outbound HTTP call in nexus was
  on ``urllib``; every other call uses httpx. Consistency +
  explicit-timeout argument shape. Tests updated to patch
  ``httpx.get`` instead of ``urllib.request.urlopen``.
- **``voyage-3`` annotated as retired** in ``commands/doctor.py``:
  Voyage AI retired the base ``voyage-3`` model in early 2025;
  doctor now reports ``"status": "retired"`` rather than a
  healthy-looking line. New code paths use ``voyage-code-3`` /
  ``voyage-context-3`` exclusively.

## [4.32.6] - 2026-05-12

Patch on 4.32.5. Audit follow-ups: mineru install-surface reduction +
RDR-108 Phase 4 retrospective close.

### Fixed

- **``mineru[all]`` → ``mineru[pipeline]``** (nexus-8g79.18): the
  audit flagged the install surface — 356 packages pulled, including
  ``gradio 6.x`` (stale XSS advisory GHSA-2wxf-49m7-6x5q), ``boto3``,
  ``openai``, ``vllm``, ``lmdeploy``, ``cupy``, ``mlx-vlm``. Nexus
  has a single MinerU call site (``src/nexus/pdf_extractor.py``)
  using ``backend="pipeline"``; the VLM-inference backends and the
  gradio UI are never invoked. Pinning to ``mineru[pipeline]``
  retains the exact stack ``do_parse`` needs (torch + torchvision +
  transformers + onnxruntime + OCR / formula / table models) and
  drops 77 packages from the lockfile — including gradio. Verified
  locally: ``do_parse`` and the pipeline backend import + work
  clean with ``[pipeline]`` only.

### Verified shipped (nexus-8g79.15)

- **RDR-108 Phase 4 already complete**: the audit flagged Phase 4
  as pending based on source-code comments, but the work shipped
  2026-05-10 in PR #624 (manifest-based GC + content-derived chunk
  IDs + retrieval rewrites) and PR #635 (Phase 4+5 stack — bundled
  nexus-xy3b + qlm2 + 1ljk + o9an + 6l9p + 2exh + e5aw + 9p0c +
  w9vq + v7mn). The closed child beads ``nexus-dyxe``,
  ``nexus-kosc``, and ``nexus-z1mu`` correspond to the planner's
  P4a/b/c ordering. Stale ``# pending Phase 4`` comment in
  ``doc_indexer.py:114`` refreshed to point at the shipped PR.

## [4.32.5] - 2026-05-12

Patch on 4.32.4. Consolidated audit-driven follow-ups from the
post-4.32.4 multi-agent audit (6 specialised agents covering
architecture, error handling, test quality, external deps, design
contracts, deferment inventory). Closes Tiers 0-1 fully and the
short-payoff items in Tiers 2-6. The audit-epic tracking is
``nexus-8g79``; 22/35 audit children closed by this release.

### Tier 0 — same-class regressions as nexus-zq79 / 4.32.4

- **``fire_store_chains`` missing ``catalog_doc_id``** (nexus-lf8f):
  the consolidated hook-firing helper used by MCP ``store_put``,
  ``nx store put``, ``nx memory promote``, ``nx store import``
  didn't pass ``catalog_doc_id`` through. Post-Phase-3 chunks have
  no ``doc_id`` fallback so the manifest hook short-circuited and
  the catalog row shipped with ``chunk_count=0`` and an empty
  manifest. Same regression class as nexus-zq79; different code
  path. Kwarg added; ``nx store put``, ``nx memory promote``, and
  ``nx store import`` (with per-doc grouping) all wired.
- **``Projector.apply_all`` re-derives ``chunk_count`` post-replay**
  (nexus-lf8f): the ``resync_chunk_count_cache`` hook writes
  direct SQL (the cache isn't event-sourced). Without a re-derive
  in replay, ``nx catalog rebuild`` from ``events.jsonl`` projected
  ``chunk_count=0`` forever — the ``DocumentRegistered`` events
  carry the register-time snapshot only. Single set-based
  ``UPDATE`` post-replay; guarded by ``WHERE EXISTS`` so
  caller-supplied values via ``Catalog.update(chunk_count=N)``
  still survive.
- **Search-result manifest stamping** (nexus-dxly partial):
  ``_attach_doc_ids_from_catalog`` batches catalog manifest fetches
  and stamps ``chunk_count`` + ``chunk_index`` onto every result
  resolving to a catalog doc. Closes the ``scoring.py:125``
  file-size penalty regression (was defaulting ``chunk_count=1``,
  silently disabling the penalty).
- **``nx memory promote`` + ``nx store import`` catalog plumbing**
  (nexus-8g79.1): the T2→T3 promotion path pre-registers via the
  shared ``_catalog_store_hook`` so chunks land with the catalog
  tumbler as ``doc_id`` at write-time. The import path groups by
  ``meta["doc_id"]`` and fires ``fire_store_chains`` per group
  with the group key as ``catalog_doc_id``.
- **aspect_readers + aspect_extractor manifest-ordered reassembly**
  (nexus-8g79.2): ``_gather_chroma_chunks_by_field`` accepts
  ``manifest_lookup(doc_id) -> list[ManifestRow]`` and, when
  ``identity_field == 'doc_id'``, orders chunks by canonical
  manifest position keyed on ``chunk_text_hash``. Post-Phase-3
  chunks have no ``chunk_index`` so the legacy
  ``md.get("chunk_index", 0)`` ordering collapsed to insertion
  sequence — wrong for multi-chunk docs. Threaded through
  ``read_source`` → ``_read_chroma_uri`` + 3 callers (``enrich.py``
  aspects/dry-run paths, ``aspect_worker.py``,
  ``aspect_extractor.extract_aspects``).

### Tier 1 — silent-fail discipline pass

- **``catalog_spans.py:290`` no longer wipes the chash index on T3
  transient** (nexus-8g79.3): pre-fix ``except: live = set()``
  made every row look stale and the self-heal loop DELETEd them all.
  Now logs at WARNING and skips self-heal — every row stays a
  provisional survivor.
- **``Catalog.delete_document`` cascades to ``document_chunks``**
  (nexus-8g79.7): pre-fix only ``DELETE FROM documents`` ran,
  leaving orphan manifest rows that survived even after
  event-sourced replay (schema has no FK cascade). Both projector
  ``_v0_document_deleted`` and the legacy write path now cascade.
- **``indexer.py:1547`` prune-misclassified** (nexus-8g79.4): bare
  ``continue`` on ``get_manifest`` / ``col.get`` failures during
  prune left T3 orphan chunks accumulating indefinitely. WARNING
  with doc_id / batch size + ``exc_info``.
- **``catalog.py:_needs_compaction``** (nexus-8g79.5): pre-fix
  silent ``except: pass`` disabled JSONL compaction forever on
  persistent error. WARNING + exc_info.
- **``catalog_sync.py:_should_use_event_sourced_rebuild``**
  (nexus-8g79.6): silent return-False downgraded every startup to
  legacy rebuild on persistent error. WARNING + exc_info.
- **``pipeline_buffer.update_progress``** (nexus-8g79.9):
  defense-in-depth ``"col" = ?`` identifier quoting against future
  allowlist entries colliding with SQL keywords.
- **8 silent-fail patterns** (nexus-8g79.8): ``indexer_utils.py:317``
  (WARNING — docs_for_chashes), ``context.py:71/78`` (DEBUG inner +
  WARNING outer), ``operators/dispatch.py:128`` (DEBUG pipe-read),
  ``plans/matcher.py:298`` (DEBUG plan-cache eviction + plan_id),
  ``doc_indexer.py:1434`` (DEBUG frontmatter-parse + path),
  ``collection_audit.py:186`` (WARNING + failed-count for partial
  query failures producing sparse-looking histograms).
- **manifest hook log severity** bumped DEBUG → WARNING since
  post-Phase-3 the hook is load-bearing.

### Tier 2 — architectural cleanups

- **``catalog/consolidation.py:116``** routes T3 writes through
  ``T3Database.upsert_chunks_with_embeddings`` (nexus-8g79.11):
  pre-fix the raw ``target_col.upsert`` bypassed quota validation,
  manifest hook, taxonomy hook, and chash dual-write.
- **``fire_post_document_hooks`` signature-classified at register
  time** (nexus-8g79.12): mirrors the pattern used by
  ``register_post_store_batch_hook``. Pre-fix the dispatcher caught
  ``TypeError`` from inside the hook body and silently retried with
  legacy shape, misclassifying unrelated type bugs.

### Tier 3 — dependency hygiene

- **``cryptography`` ``46.0.5 → 48.0.0``** (nexus-8g79.17): two-minor
  catch-up on a high-CVE-frequency package. Transitive bump only —
  no source changes. The ``llama-index-core`` upgrade (nexus-8g79.16)
  is BLOCKED by the ``mineru[all]`` GPU-stack vendoring
  (``nvidia-cudnn-frontend`` has no macOS wheels) — bead links the
  blocker.

### Backfill on upgrade

The fix corrects the *write path*; documents indexed before this
release retain stale caches. A migration step is filed for a
follow-up patch; in the interim apply directly:

```sql
-- catalog SQLite (.catalog.db)
UPDATE documents SET chunk_count = (
  SELECT COUNT(*) FROM document_chunks dc WHERE dc.doc_id = documents.tumbler
) WHERE chunk_count = 0 AND EXISTS (
  SELECT 1 FROM document_chunks dc WHERE dc.doc_id = documents.tumbler
);

-- memory.db (taxonomy)
UPDATE topics SET doc_count = (
  SELECT COUNT(*) FROM topic_assignments WHERE topic_id = topics.id
);
```

### Tier 4 — test discipline

- **``manifest_write_batch_hook`` exception path coverage**
  (nexus-8g79.24): regression test induces a manifest write
  failure and asserts no-propagate + WARNING structlog event.
- **``_collections_cache`` TTL expiry coverage** (nexus-8g79.25):
  test rewinds the cached timestamp past TTL, verifies exactly-one
  re-fetch.
- **Inequality-assertion sweep — 4 densest violators**
  (nexus-8g79.23 partial): tightened ~25 ``assert x >= 1`` /
  ``is not None`` to exact ``== N`` / identity checks across
  ``test_chunker.py``, ``test_indexer_modules.py``,
  ``test_catalog_e2e.py``, ``test_catalog.py``. Per Hal's rule:
  inequalities are how silent-corruption tests pass. ~480 patterns
  remain across the wider suite; bead stays open for incremental
  continuation.

### Tier 5 — hygiene

- **Stale xfail / skip markers** (nexus-8g79.29):
  ``test_builtin_plans.py`` skip reasons updated (the 15 YAMLs
  shipped); ``test_dispatch_router.py`` sentinel-driven test for
  the permanently-skipped ``CLAUDE_OPERATORS_PINNED`` routing
  branch; ``test_exporter.py`` ``xfail(strict=True)`` replaced
  with 4 deterministic ``_apply_remap`` cases (the legacy test
  asserted ``source_path`` on chunks RDR-102 Phase B dropped from
  the schema).

### Tier 6 — cosmetic

- **Retry backoff jitter ±20%** (nexus-8g79.32): both
  ``_chroma_with_retry`` and ``_voyage_with_retry`` apply
  ``delay * (1 + (random() - 0.5) * 0.4)`` before sleep. Pre-fix
  deterministic doubling caused thundering-herd retries under
  sustained rate-limit.
- **API-key truncation in provision error log** (nexus-8g79.33):
  ChromaDB error bodies occasionally echo the offending token;
  truncate to 120 chars matching ``retry.py``'s safety bound.
- **CI ONNX cache key bound to ``uv.lock`` hash** (nexus-8g79.33):
  pre-fix the runner-OS-only key reused stale ONNX models across
  chromadb upgrades.
- **``sn`` plugin manifest** filled in ``author``, ``repository``,
  ``license`` for marketplace-schema parity with ``nx``
  (nexus-8g79.33).
- **Defensive async-context assertion** in
  ``commands/taxonomy_cmd.py:_label_batch`` (nexus-8g79.33):
  raises a clear ``RuntimeError`` if a future async caller invokes
  it (pre-fix ``asyncio.run()`` would raise an opaque "cannot be
  called from a running event loop").

### Verified no-action

- **nexus-8g79.13** (chunker ``chunk_index`` emission): cargo-filter
  ordering is test-enforced; removing the emit requires substantial
  chunker-API refactor. Deferred to a focused bead.
- **nexus-8g79.14** (dead code): audit findings were wrong — the
  "demoted" wrappers are part of the public MCP surface via
  ``mcp_server.py`` imports; ``DEFINITION_TYPES`` is used by
  ``tests/test_languages.py``. Per "Unused != useless" rule, closed
  as no-action.

### Known follow-ups (audit epic ``nexus-8g79`` open children)

- **nexus-8g79.10** (P1) — 7 layering violations (refactor).
- **nexus-8g79.15** (P0) — RDR-108 Phase 4 (manifest-driven prune
  + reads retargeting + audit; own multi-bead arc).
- **nexus-8g79.18** (P1) — ``mineru[all]`` 356-package surface
  reduction; blocks llama-index upgrade.
- **nexus-8g79.16/.19/.20/.21** — dep upgrades stacked on .18.
- **nexus-8g79.22** — misc dep hygiene bundle.
- **nexus-8g79.23** — inequality assertion sweep continuation.
- **nexus-8g79.26** — ``time.sleep`` → ``threading.Event`` in 8
  test files.
- **nexus-8g79.27** — integration operator pipelines CI coverage.
- **nexus-8g79.28** — indexer tests ``EphemeralClient`` migration.
- **nexus-8g79.31** — orphan deferment refs audit.
- **nexus-8g79.34** — RDR-096 P5.1 batch-path migration.

## [4.32.4] - 2026-05-12

Patch on 4.32.3. Stops the silent data-correctness regression from
RDR-108 Phase 3 (PR #618, fd09a7f4): fresh indexes were shipping
with empty catalog manifests and ``chunk_count=0`` despite chunks
correctly landing in T3. Catalog-aware retrieval (tumbler→chunks
resolution) was silently broken for every doc indexed since 4.32.0.

### Fixed

- **``documents.chunk_count`` cache never re-derived** (nexus-zq79):
  Post-Phase-3, ``chunk_count`` is a denormalised cache of
  ``COUNT(*) FROM document_chunks``. The catalog-register hook
  seeds it to 0 (runs BEFORE per-file indexing) and nothing
  re-derived it for code/prose indexers. New Catalog public APIs
  ``resync_chunk_count_cache(doc_id)`` and
  ``purge_manifest_for_doc(doc_id)`` keep the cache and manifest
  consistent through ``manifest_write_batch_hook``.
- **Shrink-reindex orphan rows** (nexus-zq79 F3):
  ``append_manifest_chunks`` UPSERTs by ``(doc_id, position)``;
  re-indexing with fewer chunks left orphan rows at higher
  positions, inflating ``chunk_count``. The hook now purges the
  doc's prior manifest rows when a batch contains position 0.
- **First-time PDF/markdown batch ingest silently never wrote
  manifest** (nexus-zq79 F2): the doc-indexer batch paths used
  ``_lookup_existing_doc_id`` (read-only), which returned ``""``
  for a fresh document; post-Phase-3 chunks have no ``doc_id``
  fallback so the manifest hook short-circuited. Three call
  sites in ``doc_indexer.py`` switched to
  ``_register_or_lookup_doc_id``.
- **``Catalog.update()`` emitted stale ``chunk_count``** (nexus-zq79
  F4): ``DocumentRegistered`` event payload used the resolve-time
  snapshot; event-replay projected stale zero. ``update()`` now
  re-derives ``chunk_count`` when the caller omits it;
  caller-supplied values still win. Previously-silent
  ``except Exception: pass`` now logs at debug (F1).
- **``documents.indexed_at`` never refreshed on re-index**
  (nexus-zq79 F7): ``Catalog.update(head_hash=...)`` now
  refreshes ``indexed_at`` so ``nx catalog show`` last_indexed
  and ``collection_health`` advance.
- **``topics.doc_count`` cache stale until next discover rebuild**
  (nexus-n41p): ``CatalogTaxonomy.assign_topic`` re-derives
  ``doc_count`` from ``topic_assignments`` after every assign.
- **``topic_links.link_count`` stale on incremental indexing**
  (nexus-zq79 F5): ``assign_topic`` resyncs all ``topic_links``
  rows touching the assigned topic, materialising new pairs.
  ``link_count`` re-derived atomically via correlated subquery.
- **``manifest_write_batch_hook`` failure logged at debug** —
  bumped to WARNING since post-Phase-3 the hook is load-bearing.

### Backfill on upgrade

The fix corrects the *write path*; documents indexed before this
release retain stale caches. A migration step is filed for a
follow-up patch; in the interim apply directly:

```sql
-- catalog SQLite (.catalog.db)
UPDATE documents SET chunk_count = (
  SELECT COUNT(*) FROM document_chunks dc WHERE dc.doc_id = documents.tumbler
) WHERE chunk_count = 0 AND EXISTS (
  SELECT 1 FROM document_chunks dc WHERE dc.doc_id = documents.tumbler
);

-- memory.db (taxonomy)
UPDATE topics SET doc_count = (
  SELECT COUNT(*) FROM topic_assignments WHERE topic_id = topics.id
);
```

### Known follow-ups

Deep audit surfaced additional silent regressions filed for the
next patch:

- ``nexus-dxly`` (P0) — ``scoring.py``, ``aspect_readers.py``,
  ``aspect_extractor.py`` still read dropped chunk metadata
  fields with bad defaults (ranking degrades, multi-chunk
  aspect ordering wrong).
- ``nexus-w5zv`` (P1) — ``manifest_backfill.py`` /
  ``orphan_backfill.py`` write Phase-3 chunks at position 0.
- ``nexus-lrhg`` (P1) — atomicity wrap of manifest hook, chash
  32/64-char normalisation, ``DocumentDeleted`` replay orphan
  cleanup, ``event_log.py`` flock re-entrancy, staleness-cache
  silent-fail.

## [4.32.3] - 2026-05-11

Patch on 4.32.2. Stops the auto-restart-writes-ephemeral-port-to-
persistent-config drift that 4.32.2 made visible (nexus-oa7r). The
PID file at ``~/.config/nexus/mineru.pid`` is now the canonical
source of truth for the live MinerU server port; the config write
that previously stamped ephemeral ports into ``config.yml`` is
gone from both ``nx mineru start`` and
``PDFExtractor._restart_mineru_server``.

### Fixed

- **``mineru_server_url`` config drift** (nexus-oa7r):
  ``get_mineru_server_url`` now resolves via PID file when a live
  server is found (``_is_process_alive``-validated), falling back
  to configured ``pdf.mineru_server_url`` and the built-in
  ``http://127.0.0.1:8010`` default in that order. Neither startup
  path writes to persistent config; drift becomes structurally
  impossible. Static config retains value for out-of-band server
  management (launchctl etc).

## [4.32.2] - 2026-05-11

Patch on 4.32.1. Surfaces MinerU server reachability state in the
default ``nx doctor`` flow and adds a warn-on-fallback in
``PDFExtractor._mineru_server_available`` so operators see when math-
PDF indexing silently degrades to the in-process subprocess path
(where Grossberg-class math papers OOM-kill the worker at ~p23).

Caught during the 4.32.1 live shakeout: a stale
``mineru_server_url`` in ``~/.config/nexus/config.yml`` (written by a
prior ``_restart_mineru_server`` cycle to a now-dead port) silently
redirected every PDF index to the OOM-prone fallback. Three Grossberg
papers (cohen-1997 18p / grossberg-1975 34p / GroSchmajuk1987 46p)
that failed on 2026-05-08 indexed cleanly (129 + 445 + 351 = 925
chunks) once the config was corrected and the server was running.

### Fixed

- **MinerU server unreachable now surfaces in ``nx doctor``**
  (nexus-h1jk). New ``_check_mineru_server`` in ``nexus/health.py``
  wired into the default health-check flow. Reports ``✓ MinerU
  server: reachable at <url>`` on the happy path; ``✗ MinerU server:
  <url> unreachable`` with remediation hints (``nx mineru start`` /
  inspect config.yml) on miss.
- **``PDFExtractor._mineru_server_available`` warns on fallback** —
  structured ``mineru_server_unreachable`` / ``mineru_server_unhealthy``
  log events plus an inline ``_progress`` line naming the URL and
  recommending ``nx mineru start``. The silent fallback to the
  in-process subprocess (slower, OOM-risk on math PDFs) was the
  proximate cause of operator confusion.

### Known follow-up

- The auto-restart-writes-ephemeral-port-to-persistent-config drift
  itself is not fixed by this release — visibility only. Tracked as
  ``nexus-oa7r``.

## [4.32.1] - 2026-05-11

Patch on 4.32.0. Fixes a 4.32.0 release bug (nexus-m3dp): the
RDR-109 Phase 5 ``salient_sentences`` migration was registered at
version 4.31.7. ``apply_pending`` requires ``m_ver > last_seen_t``,
so users upgrading from 4.31.7 to 4.32.0 had the migration skipped —
the column was never added. Fresh installs got the column via the
base CREATE TABLE; only upgraders were affected.

Re-registering the migration at 4.32.1 forces re-evaluation against
every install. The migration body is idempotent
(``PRAGMA table_info`` guards the ALTER), so installs that already
have the column from a fresh-install path no-op cleanly.

Live-confirmed by 4.32.0 shakeout: local memory.db at stored
version 4.32.0 was missing ``salient_sentences``.

### Fixed

- **``salient_sentences`` migration unreachable for upgraders**
  (nexus-m3dp). Migration registration bumped from 4.31.7 to 4.32.1.

## [4.32.0] - 2026-05-11

Minor release. **Headline**: RDR-109 ships in five phases — local
mode is now the test-suite default, local-mode collection names tell
the truth about the embedder that produced them, the cross-encoder
substrate lands without pulling PyTorch, and the
``attention-guided-v1`` salience boost is wired into search behind a
feature flag (default OFF per Phase 4b measurements). RDR-108
Phase 1c PK migration relands; je0b backfill closes the residual
empty-doc_id rows; one chunker bug and two CI flakes fixed.

### RDR-109: Honest Local-Mode Naming and Cross-Encoder Salience

- **Phase 1** (#681) — Test-suite mode default. Local mode is now
  the default; cloud-mode tests opt in via a new ``cloud_mode``
  fixture. New lint ``test_mode_declarations_are_explicit`` blocks
  unmarked voyage-token references at CI.
- **Phase 2** (#682) — Honest local-mode naming. Adds
  ``LOCAL_EMBEDDING_MODELS`` (``minilm-l6-v2-384`` /
  ``bge-base-en-v15-768``) alongside the canonical voyage set. New
  ``effective_embedding_model_for_writes`` chooses the right token
  per mode; ``CollectionName.parse`` widens to accept both.
  ``T3Database._build_embedding_fn`` implements bidirectional
  name-aware dispatch: cloud + voyage-token uses Voyage, cloud +
  local-token uses ``LocalEmbeddingFunction`` (the legacy 59vl /
  GH #667 path), local + local-token uses the local EF, and local
  + voyage-token raises the new ``IncompatibleCollectionError``
  rather than producing dim-mismatched silent noise. Existing
  local-mode collections keep their voyage-* names; only new
  writes pick up the honest token. ``nx doctor`` reports the
  active embedder honestly. **Closes nexus-59vl + GH #667.**
- **Phase 3** (#683) — Local cross-encoder substrate. New
  ``nexus.cross_encoder`` module ships a lazy onnxruntime-backed
  ``LocalCrossEncoder`` (default model
  ``cross-encoder/ms-marco-MiniLM-L-6-v2``, ~80MB HF-hub download
  on first call). ``rerank_results`` becomes mode-aware: cloud
  uses Voyage rerank-2.5, local uses the new substrate. No new
  optional extra; reuses already-present core deps
  (onnxruntime / tokenizers / huggingface_hub via chromadb's
  bundled ONNX path), so RDR-038 F-03 "no PyTorch" stays intact.
- **Phase 4 + 4b** (#684 + #686) — Calibration infrastructure +
  measurements. New ``scripts/rdr-109-calibrate.py`` sweep
  harness, ``scripts/rdr-109-generate-qa.py`` deterministic Q&A
  generator, ``scripts/rdr_109_salience.py`` prototype. Measured
  140 Q&A items across four content_types: code + docs Pareto-
  clean at w=0.025; rdr neutral; knowledge regresses two
  baseline-hits at every non-zero weight. Default-on gate
  **NOT** met for knowledge.
- **Phase 5** (#687) — Salience boost substrate. New
  ``salient_sentences TEXT`` column in ``document_aspects``
  (migration at 4.31.7 — backwards-compatible ALTER TABLE).
  New ``nexus.salience`` module ships the production extractor
  + token-overlap boost. ``search_cross_corpus`` gains a
  ``_apply_salience_boost`` pass gated on
  ``.nexus.yml``'s ``attention_guided_v1.enabled``
  (default ``False``, recommended weight ``0.025`` when on,
  applies only to ``knowledge__*`` / ``docs__*`` results).

### Other

- **RDR-108 Phase 1c reland** (#675 / nexus-4s2o) — PK switch
  ``document_aspects`` and ``aspect_extraction_queue`` to
  ``doc_id``. Defers when the catalog is absent (idempotent
  retry) or when an MCP worker holds the aspect_worker lock.
- **RDR-108 Phase 1c source_path drop** (#676 / nexus-6xp2 /
  ocu9.11) — drops ``document_aspects.source_path`` column once
  je0b has run.
- **RDR-108 Phase 5 verification** (#685 / nexus-b5mh) — new
  ``scripts/rdr-108-verify.py`` replays the 2026-05-08
  prod-shakeout probes. 4/4 probes pass on develop tip after
  the je0b backfill (#688) cleared the empty-doc_id residue.
- **je0b backfill** (#688 / nexus-f8u8) — new
  ``scripts/rdr-108-je0b-backfill.py`` resolves doc_id for the
  329 ``document_aspects`` rows that the je0b PK migration left
  empty. 320 resolved via catalog lookup (youngest-indexed_at
  tie-breaker among multiple candidates from re-index dup
  accumulation); 9 unresolvable orphans optionally deleted via
  ``--delete-unresolvable``.
- **Orphan-backfill substrate** (#674 / nexus-h2pm + 4fw8 +
  oa9k) — ``nx catalog orphan-backfill`` for legacy collections
  whose chunks lack catalog Documents.

### Known issues

- ``tests/test_indexer_e2e.py::test_smart_index_staleness_check`` and
  ``test_migration_moves_prose_from_code_to_docs`` fail post-Phase-2.
  The shared ``_index`` helper mocks ``get_credential`` to return
  ``"test-key"`` for all keys, which makes ``is_local_mode()`` return
  False, so the indexer writes voyage-named collections. The
  ``local_t3`` fixture then trips the new bidirectional EF dispatch
  (Phase 2's IncompatibleCollectionError boundary) for the re-index
  / prune paths these two tests exercise. Production behavior is
  correct; the test mock pattern is what needs updating. Tracked in
  ``nexus-7kf7``.

### Fixed

- **chromadb EphemeralClient shared-state flake** (#678) —
  ``test_collection_audit::test_json_flag_emits_parseable_payload``
  cleared collections before use.
- **WAL race in ``synthesize-log --force``** (#680 /
  nexus-fmhv) — extends the ``.db-shm`` ignore-pattern to also
  cover ``.db-wal``; SQLite checkpoints can vanish either file
  between scandir and per-file copy.

## [4.31.7] - 2026-05-10

Patch on 4.31.6. Fixes a Linux-only race condition in
``nx catalog synthesize-log --force``: the snapshot ``shutil.copytree``
crashed when the SQLite WAL ``.db-shm`` file disappeared between
the directory listing and the per-file copy. The fix skips
``*.db-shm`` files entirely (they are transient WAL helpers and
SQLite regenerates them on next open).

### Fixed

- **``nx catalog synthesize-log --force`` SHM race** (CI-Linux only):
  ``shutil.copytree`` now passes ``ignore=shutil.ignore_patterns(
  "*.db-shm")``. The WAL itself is preserved for forensics; only
  the kernel-shared-memory helper is excluded. Caught by the
  4.31.6 Python 3.12 CI run; passes locally on macOS where the
  race window is wider/non-existent.

## [4.31.6] - 2026-05-10

Patch on 4.31.5. Updates the
``test_migrations_rdr108_phase1c::TestMigrationsListRegistration``
assertions that 4.31.5 missed: with je0b deferred from MIGRATIONS,
the previous "je0b is registered" assertions invert to "je0b is
deferred". Mirrors the same flip already applied to
``test_drop_source_path_appears_in_migrations_list`` in 4.31.3.

### Tests

- **``test_document_aspects_migration_registered`` →
  ``test_document_aspects_migration_deferred``**: asserts the
  PK migration is NOT in MIGRATIONS until nexus-4s2o lands.
- **``test_aspect_queue_migration_registered`` →
  ``test_aspect_queue_migration_deferred``**: same flip for the
  queue PK migration.
- **``test_both_at_version_4_30_0`` →
  ``test_both_functions_still_defined``**: now asserts the function
  definitions stay in place (so reland is one-line registry change)
  rather than testing their version stamp.

## [4.31.5] - 2026-05-10

Re-defers the RDR-108 Phase 1c PK migrations (``nexus-je0b``: PK
switch on ``document_aspects`` and ``aspect_extraction_queue``)
that 4.31.4 attempted to reland. The 4.31.4 attempt surfaced
cascading test surgery (collection-rename / aspect-worker direct
INSERTs need doc_id threading) plus a contract violation: the
empty-catalog fast-path broke the K11/CG2 "no-catalog must not
cache in _upgrade_done" invariant. Reverts to the 4.31.3 baseline
behavior on those paths.

The ``_resolve_doc_id`` substrate added to
``DocumentAspects.upsert`` ships, so the eventual je0b reland is a
one-line registry change plus targeted test updates rather than a
runtime-contract change.

### Added

- **``DocumentAspects._resolve_doc_id`` substrate** (preparation
  for je0b reland): module-scope helper that derives a doc_id when
  caller passes empty against a post-migration table. Resolution
  order: catalog lookup on ``(physical_collection,
  file_path|title)`` → tumbler; ``record.source_uri``; deterministic
  ``legacy:{collection}:{source_path}`` synthetic. The upsert post-
  migration path uses it. Inactive in 4.31.5 because je0b is not
  registered, but the substrate is in place for the next reland
  attempt.

### Changed

- **Reverted the empty-catalog je0b fast path** (4.31.4 attempt
  only): both ``_migrate_document_aspects_pk_via_apply_pending`` and
  ``_migrate_aspect_queue_pk_via_apply_pending`` go back to
  ``MigrationRetry`` on missing catalog. The K11 / CG2 contract
  ("no catalog → not cached → retry on next open") is preserved.
- **Reverted the high-volume-orphan ``MigrationRetry``** (4.31.4
  attempt only): back to ``MigrationError`` per the test contract.
  The reland will need to coordinate this change with the test
  fixtures rather than landing it standalone.

## [4.31.4] - 2026-05-10

Re-lands the RDR-108 Phase 1c PK migrations (``nexus-je0b``:
document_aspects + aspect_extraction_queue PK switch to ``doc_id``)
that 4.31.3 deferred. The companion fix lives in
``DocumentAspects.upsert``: a ``_resolve_doc_id`` helper auto-derives
the doc_id when the caller passes it empty, removing the latent gap
that surfaced when the migration completed in test environments.

``nexus-ocu9.11`` (drop ``document_aspects.source_path``) stays
deferred. Multiple read/write methods in DocumentAspects still
reference ``source_path`` via SQL; dropping the column needs a
``_has_source_path_column`` schema flag and branch on every reference.
Tracked as a follow-up.

### Added

- **``DocumentAspects.upsert`` doc_id resolver** (companion to je0b):
  a new ``_resolve_doc_id`` helper at module scope. When caller
  passes empty ``doc_id`` against a post-migration table, derives
  via (1) catalog lookup on ``(physical_collection, file_path|title)``
  → tumbler, (2) ``record.source_uri`` (RDR-096 canonical identity),
  (3) ``legacy:{collection}:{source_path}`` deterministic synthetic.
  Logs ``document_aspects_upsert_synthesized_doc_id`` when synthesis
  kicks in so operators see the wiring gap. The hard
  ``ValueError("doc_id must not be empty")`` only fires when
  collection AND source_path AND source_uri are all empty (true
  programming error).

### Changed

- **``_migrate_document_aspects_pk_via_apply_pending`` empty-table
  fast path**: when catalog is absent AND the table is empty (fresh
  install), spins up a temp stub catalog with the minimal
  ``documents`` + ``collections`` schema and runs je0b against it.
  The cross-DB JOIN finds zero matches, the rebuild proceeds.
  Replaces the previous unconditional ``MigrationRetry`` which left
  35 migrations unbumped on every fresh install.
- **``_migrate_aspect_queue_pk_via_apply_pending`` same shape**:
  empty queue + absent catalog now uses a stub catalog instead of
  deferring.

### Re-enabled

- **``nexus-je0b`` (RDR-108 Phase 1c)**: document_aspects PK switch
  to doc_id. Re-registered in MIGRATIONS at 4.30.0.
- **``nexus-je0b`` (RDR-108 Phase 1c)**: aspect_extraction_queue PK
  switch to doc_id. Re-registered in MIGRATIONS at 4.30.0.

### Tests

- **``test_doctor_aspect_queue._enqueue``**: derives a synthetic
  doc_id via ``f"legacy:{collection}:{source_path}"`` when the
  table has been migrated to PK=doc_id, satisfying the NOT NULL
  constraint without changing test semantics.
- **``test_empty_source_uri_not_classified_as_orphan``**: same
  pattern for the direct-SQL legacy-row INSERT.
- **``test_drop_source_path_appears_in_migrations_list``**: renamed
  to ``test_drop_source_path_deferred_pending_callers_refactor``;
  asserts that ``ocu9.11`` is intentionally NOT in MIGRATIONS until
  the upsert/get refactor lands.

## [4.31.3] - 2026-05-10

Patch release. Defers the RDR-108 Phase 1c PK migrations
(``nexus-je0b``: document_aspects and aspect_extraction_queue PK
switch to ``doc_id``) and ``nexus-ocu9.11`` (drop
``document_aspects.source_path``) from the migration registry. The
schema migrations themselves are correct, but the runtime upsert
path (``DocumentAspects.upsert``) and the ``nx enrich aspects`` CLI
still write rows with empty ``doc_id`` against the post-migration
table. The strict ``doc_id must not be empty`` guard added in #610
then fires for those callers. Re-enables once the enrich pipeline
+ upsert path are wired to populate ``doc_id`` from the catalog
lookup (``_build_catalog_doc_id_lookup`` in
``commands/enrich.py``).

Everything else from the 4.31.x release series ships as planned:
RDR-108 Phase 4 read-path remediation, operator dispatch
qwen-routing promotion, 3 new catalog doctor checks, prose + PDF
auto-link generators, lazy T3 collection creation, aspects
write-side confidence floor, default-exclude implements-heuristic
from graph traversal, classifier skips minified bundles, and the
Windows winget hint block in ``nx doctor``. The RDR-108 Phase 4a
``chash_index.chunk_chroma_id`` column drop (``nexus-mmf5``) ships
as planned (no caller dependency).

### Changed

- **Defer ``nexus-je0b`` and ``nexus-ocu9.11``**: removed from the
  registered migration list. Function definitions stay in place.
  The column-presence guards added to
  ``migrate_document_aspects_source_uri`` (4.16.0) and
  ``migrate_document_aspects_source_uri_backfill_empty`` (4.26.2)
  are kept as defensive code (they document the column-drop
  contract and prevent re-run failures when these migrations
  re-enable later).

## [4.31.2] - 2026-05-10

Patch release. The 4.31.0 tag-push uncovered a migration ordering
bug, and the 4.31.1 attempt to fix it via a table-rebuild diverged
from the runtime upsert path (which still writes ``source_path`` as
a denorm cache when the table is in the post-je0b shape). 4.31.2
takes the simpler approach: ``ocu9.11`` defers when ``je0b`` hasn't
run yet, mirroring je0b's own ``MigrationRetry`` pattern. Neither
4.31.0 nor 4.31.1 reached PyPI (test gate held both times).

### Fixed

- **``migrate_drop_source_path_column`` defers until je0b ran**
  (nexus-ocu9.11): the migration now raises ``MigrationRetry`` when
  ``source_path`` is still in the PRIMARY KEY (which means ``je0b``
  was skipped because the catalog is absent). ``apply_pending``
  re-runs all skipped migrations on the next DB open; once the
  catalog exists and je0b succeeds, source_path is no longer in the
  PK and the simple ``ALTER TABLE ... DROP COLUMN`` path applies.
  Replaces the 4.31.1 table-rebuild approach which diverged from
  the runtime upsert path that writes source_path as a denorm cache.
- **``migrate_document_aspects_source_uri`` source_path guard**
  (4.16.0): early-return when source_path column is absent. Defends
  against ``apply_pending`` re-running this migration on a DB where
  ``ocu9.11`` has already dropped source_path (e.g. after a later
  migration raised ``MigrationRetry`` and apply_pending is replaying
  from last_seen).
- **``migrate_document_aspects_source_uri_backfill_empty`` same
  guard** (4.26.2): same shape; same defensive early-return.

## [4.31.0] - 2026-05-10

Major release rolling up RDR-108 Phase 4 read-path remediation,
RDR-096 Phase 5.2 source_path retirement, the operator dispatch
qwen-routing promotion (#623, #626), and a wave of catalog-doctor
+ indexer correctness work surfaced during the 4.29.x prod
shakeouts.

### Added

- **Operator dispatch qwen routing** (RDR-110 substrate): per-operator
  routing via ``NEXUS_DISPATCH_BACKEND`` and
  ``NEXUS_DISPATCH_{QWEN,CLAUDE}_OPERATORS``. ``extract`` promoted to
  qwen-default; remaining operators stay on claude-dispatch. Feature
  is opt-in via env; default behavior unchanged. (#623, #626)
- **3 new catalog doctor checks** (nexus-6dan): ``--chunk-size`` flags
  T3 chunks above ``MAX_DOCUMENT_BYTES``; ``--chunk-text-dedup``
  reports duplicate chunk_text_hash collisions per collection;
  ``--t3-vs-catalog`` cross-checks T3 collections against catalog
  projection rows. (#665)
- **Prose + PDF auto-link generators** (nexus-sob9): bulk ingest now
  emits prose-to-filepath and pdf-to-corpus links during
  ``nx index repo`` / ``nx index pdf`` instead of leaving links to
  manual ``nx catalog link`` calls. Default-on; disable via
  ``--no-auto-links``. (#669)
- **Lazy T3 collection creation** (nexus-27u7): bulk ingest defers
  creating ``code__*`` / ``docs__*`` Chroma collections until the
  first chunk write. Eliminates zombie empty collections from
  content-type-mismatched runs. (#672)
- **Aspects confidence floor on writes** (nexus-17wf): the
  document_aspects upsert path drops rows with ``confidence < 0.3``
  or NULL/empty source_uri before insert, matching the existing
  read-side filter. (#663)
- **Default-exclude implements-heuristic from graph traversal**
  (nexus-6ppk): ``catalog graph`` BFS no longer follows
  heuristic-only edges by default; pass ``--include-heuristic`` to
  opt in. (#671)
- **Skip minified bundles by default** (nexus-haet): classifier
  rejects ``*.min.{js,mjs,cjs,css}`` and ``*.bundle.{js,mjs}`` before
  code-extension routing. Saves Voyage tokens; prevents oversize
  failures on bundled web assets. (#670)
- **Honest local-mode test-suite mode default + RDR-109 draft**:
  documents the architectural plan to fix local-mode collection
  naming + add cross-encoder salience scoring as a paired RDR.
  Implementation deferred.
- **Windows winget hints in install Fix-line block** (nexus-njmg):
  ``nx doctor`` install instructions include Windows-native winget
  recipes alongside macOS/Linux. (#656)
- **Plugin SessionStart preflight degraded-mode marker**
  (nexus-hwbj): plugin emits a marker when ``nx`` is unreachable so
  downstream skills can route to fallback behavior. (#657)

### Changed

- **Drop document_aspects.source_path column** (RDR-096 Phase 5.2,
  nexus-ocu9.11): ``apply_pending`` migrates the projection table to
  remove the column. ``source_uri`` is the sole identity. Caught +
  cleared 50 leftover NULL/empty source_uri rows during the
  2026-05-10 triage. (#666)
- **Drop chash_index.chunk_chroma_id column** (RDR-108 Phase 4a,
  nexus-mmf5): retired as part of the Phase-3 metadata reduction.
  ``chunk_text_hash`` is now the sole T3 chash anchor.
- **Migrate document_aspects PK to doc_id** (RDR-108 Phase 1c,
  nexus-je0b): aspects now key by tumbler-style ``doc_id`` instead
  of the legacy compound key.
- **Migrate aspect_extraction_queue PK to doc_id** (RDR-108
  Phase 1c, nexus-je0b): same rationale; queue keys align with the
  projection table.

### Fixed

- **t3-doc-id-coverage falls back to manifest for Phase-3 chunks**
  (nexus-esrl): doctor now resolves chunk doc_id via the catalog
  ``document_chunks`` manifest when chunk metadata lacks doc_id.
  (#650)
- **Reindex resolves Phase-3 chunks via catalog manifest**
  (nexus-vn48): the indexer's "is this chunk already known?" check
  uses the manifest as a fallback so Phase-3 chunks aren't
  re-embedded unnecessarily. (#651)
- **Staleness cache resolves Phase-3 chunks via manifest**
  (nexus-0ocy): the file-level staleness cache no longer considers
  Phase-3 chunks "missing" when their doc_id is only available
  through the manifest. (#652)
- **doctor ``t3-doc-id-coverage`` skips bypass-schema collections**
  (nexus-wszt). (#653)
- **Auto-bootstrap leaves created_at empty for replay-equality**
  (nexus-33xm): synthesized ``CollectionCreated`` events stamp
  ``created_at=""`` (matching the post-Phase-4 contract) instead of
  NOW(), so replay-equality holds across rebuilds. (#654)
- **CLI force UTF-8 stdout/stderr on Windows** (nexus-vwu1):
  prevents crashes when a cp1252 console can't encode non-ASCII
  output. (#655)
- **Pipeline pass-2 fetch with prior-batch upsert in reidentify**
  (nexus-zpnq): ``nx t3 reidentify`` eagerly upserts each batch so
  a mid-run failure preserves earlier work. (#658)
- **DEVONthink URI collapse** (nexus-n3md): catalog deduplicates
  variant DEVONthink URIs to a single canonical form; exposes
  empty-sentinel for documents that lost their DEVONthink anchor.
  (#662)
- **docs_for_chashes accepts both 32-char and 64-char chashes**
  (nexus-f8c3): mixed Phase-2/Phase-3 chash tables no longer
  partial-miss. (#645)
- **search ``--max-file-chunks`` via post-query manifest filter**
  (nexus-oo4f D-M2): Phase-3 chunks lack file_path metadata; filter
  now resolves file_path through the manifest. (#648)
- **MCP derives chunk_count from catalog manifest** (nexus-voy5):
  ``store_get`` and adjacent verbs no longer try to read
  ``chunk_count`` from removed chunk metadata. (#646)
- **ThreadPoolExecutor lifecycle + collection-gc clean error
  handling** (nexus-uv06 + nexus-pz24). (#644)
- **collection_audit uses get_collection to avoid zombie creation**
  (nexus-8lbe). (#643)
- **chash-reconcile guards isinstance(c, str) in list_collections**
  (nexus-l1yt): defensive type-check against the rare non-str row
  that crashed the reconciler. (#642)

### Tests

- **Papers-curator isolation invariant for knowledge__knowledge**
  (nexus-frai): regression test pinning that catalog operations
  never cross-contaminate between papers-curator-owned
  ``knowledge__knowledge`` and other curator collections. (#660)
- **Fail session when fixture cache files leak into
  ``~/.config/nexus/``** (nexus-nifd): conftest guard catching a
  whole class of test-isolation regressions. (#661)
- **Tighten 5 inequality weaknesses + lock manifest-authoritative
  contract** (nexus-oe2i): converts ``>=N`` assertions to ``==N`` in
  fixture regressions and locks the post-RDR-108 contract that the
  manifest is authoritative for chunk-doc mapping. (#649)
- **Phase-3 position ordering regression tests** (nexus-ivra):
  guards the ``(doc_id, position)`` ordering the catalog-aware
  retrieval path relies on. (#647)

### Documentation

- **Chunk vs document metadata semantics + T3 health &
  audit-membership runbooks** (nexus-gndj): fills a long-standing
  doc gap on what lives where post-Phase-3. (#664)
- **Removed stale T3 expire-guard SQL rule** (nexus-ejoq): AGENTS.md
  no longer references the two-clause guard pattern; the
  three-clause rule (``ttl_days > 0 AND expires_at != "" AND
  expires_at < now``) is the only documented form. (#659)
- **RDR-110 (Semantic Tuple Space)** drafted: unified coordination
  primitive over ChromaDB + SQLite. Status: Accepted; implementation
  deferred.

## [4.29.2] - 2026-05-09

Patch release. Restores Windows compatibility — every CLI invocation
on Windows was failing with ``ModuleNotFoundError: No module named
'fcntl'`` because ``catalog.py``, ``event_log.py``, and ``indexer.py``
each did an unconditional top-level ``import fcntl`` (Unix-only stdlib
module). The import error fired at module load before click ever got
a chance to dispatch, so ``nx --version`` was as broken as anything
else. Caught by a fresh Windows install of 4.29.1 from PyPI; the
package was effectively unusable on the platform.

### Fixed

- **Windows ``ModuleNotFoundError: fcntl`` on every CLI invocation**
  (#620): adds a small ``nexus._locking`` shim wrapping
  ``fcntl.flock`` (POSIX) and ``msvcrt.locking`` (Windows) behind a
  uniform API. Two patterns: ``acquire_directory_lock`` /
  ``release_lock`` for serializing catalog writers, and
  ``lock_file`` / ``unlock_file`` for the per-repo PID lock in the
  indexer. Windows can't ``flock`` directory handles, so the
  directory lock locks a sentinel file ``<dir>/.lock`` instead;
  caller contract (opaque token in/out) is preserved. Non-blocking
  failures raise ``BlockingIOError`` on both platforms so callers
  handle them with one except clause. ``msvcrt.locking(LK_LOCK)``
  only retries 10 times before raising; the helper loops it for
  true blocking semantics matching ``fcntl.LOCK_EX``.

### Docs

- **RDR-108 graph identity normalization** (#606): drafts the design
  for graph identity normalization across catalog stores, supersedes
  RDR-107.

## [4.29.1] - 2026-05-08

Patch release. Hardens the catalog's destructive-verb surface
caught during the 4.29.0 prod shakeout: ``nx catalog prune-stale``
narrowly misclassified 11,766 valid relative-path entries as
stale because of a cwd-dependent ``Path.exists()`` check
(nexus-6ims). Plus two adjacent safety regressions
(``catalog gc`` deletes-by-default, ``link-bulk-delete`` no
``--confirm``) and a backup-before-delete safety net
(RDR-106 Option A).

### Fixed

- **``nx catalog prune-stale`` cwd-dependent classification**
  (nexus-6ims P0): pre-fix logic used ``Path(fp).exists()`` to
  classify entries as stale, which only worked for absolute
  paths. Relative paths (most catalog entries store paths
  relative to their owner's repo_root) resolved against the
  running process's cwd; running the verb from any cwd other
  than the entry's owning repo over-classified valid entries.
  Caught 2026-05-08 during prod shakeout: a live catalog with
  23,370 documents reported 11,837 "stale" entries when only
  71 were actually stale. Post-fix: relative paths resolve
  against ``owner.repo_root`` (RDR-060); owners without a
  ``repo_root`` skip with a structured warning rather than
  auto-classifying as stale.
- **``nx t3 prune-stale`` cwd bug** (same root cause): chunk
  ``source_path`` resolution now joins through the catalog to
  pick up the owning document's ``owner.repo_root``. Owners
  with no ``repo_root`` skip with the same fail-safe.
  Critical because T3 chunk deletion is unrecoverable without
  re-embedding (Voyage cost) and there's no event-log audit
  trail like the catalog has.
- **``nx catalog gc`` deletes-by-default** (nexus-tnz3 P1):
  ``--dry-run`` was opt-in (default deletes). A typo or
  forgotten flag silently dropped orphan entries. Inverted to
  match ``prune-stale``: dry-run by default, ``--no-dry-run
  --confirm`` required to actually delete.
- **``nx catalog link-bulk-delete`` lacks ``--confirm``**
  (nexus-9nim P2): hidden verb with the same delete-by-default
  pattern. Hidden so casual usage was unlikely, but still on
  the destructive-verb surface. Now matches the
  ``prune-stale`` / ``gc`` shape.

### Added

- **Backup-before-delete safety net** (RDR-106 Option A):
  every destructive catalog verb (``delete``, ``gc``,
  ``prune-stale``, ``link-bulk-delete``) writes a JSONL
  snapshot of the rows about to be deleted to
  ``$NEXUS_CONFIG_DIR/catalog/.deleted-backups/`` BEFORE the
  actual delete. The snapshot captures the full document row
  + inbound and outbound links so an undelete can fully
  reconstruct the document AND its position in the link
  graph.
- **``nx catalog undelete <backup-file>``**: restores the
  documents and links from a backup snapshot via event-sourced
  ``DocumentRegistered`` / ``LinkCreated`` events. Documents
  are restored with their ORIGINAL tumblers; the tumbler
  minting path is bypassed.
- **``nx catalog list-backups``**: enumerates available
  backup snapshots newest-first with verb, timestamp, row
  count, and reason.
- **``nx catalog vacuum-backups``**: drops backup files past
  the retention window (default 30 days). Defaults to
  dry-run; ``--no-dry-run`` to actually remove.

### Filed for follow-up

- **RDR-106** (draft): proper soft-delete via tombstone
  columns on the catalog projection. The 4.29.1 backup
  pattern is a recovery affordance; RDR-106 is the full
  architectural answer with grace-window semantics, in-tree
  state, and read-path filtering.

### Closed

- nexus-6ims (P0): catalog + t3 prune-stale cwd-dependent
  classification
- nexus-tnz3 (P1): catalog gc deletes-by-default
- nexus-9nim (P2): link-bulk-delete missing --confirm

## [4.29.0] - 2026-05-08

Minor release. Decomposes the 4434-LOC ``catalog.py`` god object into a
1683-LOC facade plus six focused modules — a -62% LOC reduction with
the public API preserved verbatim.  Plus the supporting test isolation
fix (#601), nexus-wehp regression guard (#599), RDR-104 / RDR-105
close artefacts (#599 / #600), and two stale shakedown-script bugs
surfaced and fixed by the live verification of this refactor.

This release ships behaviour-equivalent code with a substantially
clearer module structure.  No public API changes; downstream callers
keep working with no source edits beyond the ``catalog/`` package
itself.  Every existing catalog test passes; 12 new contract tests
pin the previously-undocumented decomposition invariants
(composition order, ``_cat_mod`` patching propagation, graph cap
scenarios, ``bulk_unlink`` event-sourced vs legacy paths,
shadow-emit ordering).

### Changed

- **Catalog decomposition** (#602 nexus-mbm, #603 nexus-p01o):
  ``src/nexus/catalog/catalog.py`` 4434 LOC → 1683 LOC (-62%).  Six
  new focused modules, each composed onto ``Catalog`` as a
  ``self._<ops>`` facade following the T2Database domain-store pattern
  (RDR-063):

  - ``catalog_git.py`` (171 LOC): git subprocess plumbing for
    ``Catalog.init`` / ``sync`` / ``pull`` (clone, init, ensure
    identity, commit-and-push, pull-if-remote).
  - ``catalog_spans.py`` (417 LOC): chash span resolution and
    RDR-086 fallback machinery (``resolve_span_in_t3`` /
    ``resolve_chash_globally`` / ``fallback_chash_scan`` /
    ``negate_iso``).
  - ``catalog_links.py`` (959 LOC, ``_LinkOps``): the link-table SQL
    surface plus BFS traversal — ``link`` / ``unlink`` / ``links_from``
    / ``links_to`` / ``link_query`` / ``bulk_unlink`` /
    ``validate_link`` / ``link_audit`` / ``graph`` / ``graph_many``.
  - ``catalog_docs.py`` (688 LOC, ``_DocumentOps``): read-only
    document / owner / collection lookups (``resolve`` / ``find`` /
    ``by_*`` / ``descendants`` / ``list_collections`` /
    ``collection_for*`` / ``resolve_alias`` / ``resolve_path`` /
    ``resolve_chunk``).
  - ``catalog_sync.py`` (787 LOC, ``_SyncOps``): RDR-104 incremental
    rebuild machinery, the consistency / offset / header-hash markers,
    and the JSONL defrag/compact maintenance verbs.
  - ``catalog_writes.py`` (714 LOC, ``_WriteOps``): non-registration
    mutations (``update`` / ``delete_document`` / ``rename_collection``
    / ``supersede_collection`` / ``set_alias`` /
    ``update_document_collection`` /
    ``update_documents_collection_batch``).  Registration writes
    (``register_owner`` / ``register`` / ``register_collection``) stay
    on the ``Catalog`` facade because they directly drive the
    event-sourcing dual-write machinery.

  The ``_cat_mod`` reference pattern in three of the new modules
  preserves the test-monkeypatch contract:
  ``monkeypatch.setattr("nexus.catalog.catalog._FOO", ...)`` propagates
  into the extracted modules without needing a re-import dance.
  ``Catalog._MAX_GRAPH_DEPTH`` / ``_MAX_GRAPH_NODES`` keep their
  class-attribute aliases so tests reading them at the class level
  continue to work.

### Fixed

- **Test config-dir isolation** (#601 nexus-mrmq): autouse fixture in
  ``tests/conftest.py`` redirects ``NEXUS_CONFIG_DIR`` to
  ``tmp_path/.config/nexus`` for every test, propagating to spawned
  subprocesses (``claude_dispatch -p``, plan-runner, nx_answer
  equivalence) via ``os.environ`` inheritance.  Closes a leak where
  ``uv run pytest -m integration`` rewrote the live MCP's
  ``~/.config/nexus/current_session`` from a transient subprocess.
- **nexus-wehp catalog writer-lock regression guard** (#599): adds a
  multiprocessing test that pins the post-fix invariant — two
  concurrent ``Catalog.register_collection`` calls succeed without
  ``database is locked``.  The original 4-second exclusive rebuild
  window that occasionally exceeded ``busy_timeout=5s`` was eliminated
  by the 4.23.1 marker persistence + RDR-104 incremental rebuild;
  this is the regression test the ``nexus-wehp`` acceptance criteria
  required.
- **release-sandbox.sh step-5** integration-marker selection: was
  invoking pytest without ``-m integration`` and reading the
  exit-5 (no-tests-collected) as FAIL.  Caught during the
  catalog-decomposition shakeout.
- **release-sandbox.sh step-8** post-RDR-105 T1 contract drift: the
  comment + invocation assumed pre-4.27 silent EphemeralClient
  fallback for ``nx scratch put`` outside a Claude Code session.
  Post-RDR-105 T1 fails loud with ``T1ServerNotFoundError``; the
  fix sets ``NX_T1_ISOLATED=1`` for the shakedown invocation.

### Closed

- nexus-wehp (catalog Catalog() construction triggers DELETE FROM
  links rebuild that races MCP-held SQLite connection)
- nexus-mrmq (integration tests leak into live ~/.config/nexus/)
- nexus-mbm (720 review: extract catalog.py god object into focused
  modules)
- nexus-p01o (catalog.py write surface: extract update / delete /
  rename / supersede to catalog_writes.py)
- RDR-104 (incremental catalog projection rebuild)
- RDR-105 (T1 chroma architecture: env-passdown)

## [4.28.0] - 2026-05-08

Minor release. Adds ``nx catalog synthesize-log`` (lossless
in-place recovery for catalogs in bootstrap-fallback mode) and
closes four bugs surfaced during the 4.27.1 follow-up review:
``NX_T1_ISOLATED=1`` precedence (#593), session_id-chain drift
across T1 / tier-write / launcher (#594), the synthesize-log
data-loss regression (#591), and chronic POSIX-semaphore
exhaustion from orphan multiprocessing trackers (nexus-9h1s).

### Fixed

- **POSIX semaphore exhaustion from orphan multiprocessing trackers**
  (nexus-9h1s): each ungraceful MCP shutdown (SIGKILL/OOM) leaves
  chroma's multiprocessing-worker ``resource_tracker`` /
  ``spawn ... --multiprocessing-fork`` subprocesses re-parented to
  init (PPID=1). The trackers continue holding their POSIX named
  semaphores until killed; ``stop_t1_server``'s ``safe_killpg`` only
  signals the CURRENT chroma's process group, so workers from PRIOR
  sessions live in different (now-empty) PGIDs and cannot be
  reached. ``sweep_orphan_tmpdirs`` reaped the directories but left
  the kernel-level resources, so the namespace
  (``kern.posix.sem.max=10000`` on macOS) accumulated to exhaustion
  ("Errno 28") system-wide.

  Live shakeout 2026-05-08 03:30 PT on the dev machine: 25 orphan
  ``nx_t1_*`` tmpdirs, 3,314 orphan multiprocessing trackers, 8,359
  POSIX semaphores held (83% of cap). Oldest tracker 11 days old.
  After fix: 0 orphan trackers, 74 semaphores held.

  Fix: introduce ``nexus.session.sweep_orphan_resource_trackers``
  and wire it into the MCP top-level startup sweep alongside
  ``sweep_orphan_t1_addr_files`` and ``sweep_orphan_tmpdirs``. Pure
  parser ``_parse_orphan_tracker_candidates`` (PPID=1 + command
  contains ``"multiprocessing"`` + age >= 60 s + not in
  ``protected_pids``) feeds ``_kill_orphan_tracker_pids`` which
  SIGTERMs each candidate, escalates to SIGKILL on survivors after
  3 s. Both helpers are individually testable.

  ``nx doctor --check-resources`` extended to surface the orphan
  count: ``[✓]`` below 100, ``[!]`` advisory between 100 and
  999, ``[!]`` URGENT at 1000+ with reap-inline instructions and
  the bead reference. The known-sources warning text adds the
  nexus-9h1s case alongside nexus-dc57 / nexus-ze2a.

  Regression coverage in ``tests/test_session_sweep_orphan_trackers.py``
  (9 tests): six parser unit tests pin the discrimination logic
  (orphan vs live parent, multiprocessing match, etime parsing of
  ``MM:SS`` / ``H:MM:SS`` / ``DD-HH:MM:SS`` / very-old, protected
  PID exclusion) and three kill-helper tests verify SIGTERM
  delivery to live subprocesses, ProcessLookupError handling for
  already-dead PIDs, and SIGKILL escalation when SIGTERM is
  trapped.

### Changed

- **Single source of truth for the Claude session_id chain** (issue
  #594, nexus-9e9a): introduce ``nexus.session.resolve_active_session_id``
  and route the three open-coded callsites
  (``T1Database._resolve_session_id``, ``mcp/core._record_tier_write``,
  ``_session_end_launcher._print_tier_status_summary``) through it.
  Pre-PR each site implemented ``NX_SESSION_ID env >
  read_claude_session_id() > <fallback>`` independently with three
  divergent fallbacks: ``uuid4()`` (T1), ``"unknown"``
  (``_record_tier_write``), and no fallback (launcher). The drift
  between the first two was the exact bug class that produced PR #590
  / nexus-h8ge -- the audit log and the T1 chunk store disagreed on
  attribution because each site mutated its chain independently.

  ``T1Database`` and ``_record_tier_write`` now both fall back to
  ``"unknown"`` so the audit log and the T1 chunk store agree on
  attribution: rows under ``"unknown"`` are exactly the rows from
  processes that did not bind to a Claude session, grep-able for
  forensics. Behaviour change: T1 no longer mints a per-process
  ``uuid4()`` for an anonymous CLI run -- the existing
  ``test_uuid_fallback_when_nothing_set`` regression test (now
  ``test_unknown_fallback_when_nothing_set``) was locking in the
  drift-prone behaviour. The launcher still short-circuits when the
  resolver returns ``None`` (no useful per-session summary without a
  bound session; querying ``WHERE session_id = "unknown"`` would leak
  rows from unrelated invocations into the user-facing summary).

  Regression coverage in ``tests/test_session_resolver.py`` (12 tests):
  six chain-semantics tests pin the resolver's behaviour, six callsite
  tests assert each of the three sites routes through the helper and
  applies its documented fallback. Future changes to the chain happen
  in one place or surface here.

### Fixed

- **`NX_T1_ISOLATED=1` precedence inside Claude sessions** (issue #593,
  nexus-svpq): pre-fix, ``T1Database._init_new_discovery``'s four-branch
  gate ran in order Path A (env-pair) -> Path B (addr file) -> Path C
  (isolation flag) -> raise. Inside an active Claude session, Path B
  always resolved a sibling MCP T1 via ``find_immediate_claude_pid()`` +
  ``read_t1_addr_for(pid)`` and short-circuited before the
  ``_t1_isolated_env()`` check fired. Operators who ran
  ``NX_T1_ISOLATED=1 nx scratch ...`` from a sibling shell expected an
  in-process ``EphemeralClient`` sealed from the live MCP T1; they got
  an ``HttpClient`` against the live T1 instead. The 4.27.0 CHANGELOG
  line "Operators who want ephemeral semantics opt in via
  ``NX_T1_ISOLATED=1``" was true only outside a Claude session.

  Fix: hoist Path C above Paths A and B so an explicit operator opt-in
  outranks env-pair and addr-file auto-discovery. ``NX_T1_HOST`` /
  ``NX_T1_PORT`` and the ``~/.config/nexus/t1_addr.<pid>`` file are
  inheritance / discovery signals; ``NX_T1_ISOLATED=1`` is a deliberate
  operator action, and opt-ins outrank discovery (consistent with the
  pre-4.27 ``NEXUS_SKIP_T1=1`` semantics that the env-rename preserved).
  Regression coverage in
  ``tests/test_t1_discovery.py::TestT1DatabaseIsolatedOverridesDiscovery``
  asserts isolated wins over env-pair, over addr-file, and over both
  simultaneously, plus that the deprecated ``NEXUS_SKIP_T1=1`` alias
  retains the same override semantics through the deprecation cycle.

### Added

- **`nx catalog synthesize-log` CLI verb** (issue #591, nexus-hh1b):
  in-place lossless recovery for catalogs in bootstrap-fallback mode.
  The `synthesize_from_jsonl` synthesizer code has always been present
  in `src/nexus/catalog/synthesizer.py`; only the CLI handler was
  retired post Phase 5b (nexus-iftc). The previous `nx catalog doctor`
  warning told operators to delete the catalog directory and re-run
  `nx catalog setup`, which is destructive: setup bootstraps owners
  and documents from current T3 state but cannot reconstruct user-
  authored typed links (`relates`, `cites`, `implements`,
  `supersedes`) because T3 stores chunks, not the catalog graph. On a
  real-world catalog (15,853 docs / 533 links / 41 owners) this
  produced 99% link loss in field reports.

  Re-exposing the synthesizer behind a CLI handler restores the
  zero-loss recovery path. Flags: `--check` (detect fallback, exit 1
  if active), `--dry-run` (print event counts, write nothing),
  `--no-verify` (skip post-write replay-equality check), `--force`
  (synthesize on a healthy catalog, harvesting and preserving the
  existing event-log `tumbler->doc_id` map so T3 chunk metadata
  stays valid). Default invocation snapshots the entire catalog
  directory to a sibling `<catalog>.synth-snapshot-<ts>/` before
  writing, performs an atomic write of `events.jsonl`, runs the
  doctor's `--replay-equality` check, and on FAIL rotates the
  failed live state aside and restores the catalog from the snapshot
  via `copytree` so all three artifacts (pristine snapshot, failed
  state, restored catalog) are retained for forensics. `nx catalog
  doctor` warning text updated to direct operators at the new verb
  and label the destructive `setup` path as a lossy fallback.

## [4.27.1] - 2026-05-08

Patch release. Closes a critical T1 regression introduced by RDR-105
P4 in 4.27.0 (PR #585) that broke cross-process T1 visibility for
every nx-plugin hook reading scratch from the shell. Surfaced during
the live 4.27.0 shakeout (bead nexus-h8ge) within ~30 minutes of the
PyPI publish.

### Fixed

- **T1 cross-process session_id propagation** (PR #590, nexus-h8ge):
  RDR-105 P4 deletion of the legacy session-record resolver removed
  the ``read_claude_session_id()`` (~/.config/nexus/current_session)
  fallback from ``T1Database._init_new_discovery``'s session_id
  resolution chain. Every ``T1Database()`` call without an explicit
  session_id arg or ``NX_SESSION_ID`` env minted a fresh ``uuid4()``
  per process, so two processes in the same Claude session (the MCP
  server and a Bash-tool sibling, or two shell ``nx scratch``
  invocations) could not see each other's entries via the per-entry
  session_id metadata filter. Live repro from any Bash tool, no env
  overrides:
  ```
  $ nx scratch put hello && nx scratch list
  Stored: <uuid>
  No scratch entries.            <-- BUG
  ```
  ``nx tier-status`` continued to use the correct chain
  (``mcp/core.py:_record_tier_write`` resolves ``NX_SESSION_ID`` env
  > ``read_claude_session_id()`` > ``"unknown"``) so telemetry
  attributed writes to the canonical Claude session while the
  actual T1 chunks sat under fresh per-process UUIDs: the audit log
  and the T1 chunk store disagreed on attribution. Production hooks
  reading T1 from the shell silently saw "No scratch entries."
  regardless of T1 contents:
  - ``nx/hooks/scripts/subagent-start.sh:252`` ("Inject current T1
    scratch entries") never injected anything; sub-agents got no T1
    context.
  - ``nx/hooks/scripts/post_compact_hook.sh:23`` saw no scratch
    post-compact.
  - ``nx/hooks/scripts/pre_close_verification_hook.sh:82,215``
    verification gates were blind.
  - ``nx/hooks/scripts/divergence-language-guard.sh:75`` writes
    were lost into per-process ephemeral namespaces.

  Fix: factor the resolution chain into
  ``T1Database._resolve_session_id`` and call it from all four
  construction branches (Path A env, Path B addr file, Path C
  isolation, client-injection) so the chain is a single source of
  truth. ``read_claude_session_id`` / ``write_claude_session_id``
  now resolve ``NEXUS_CONFIG_DIR`` per call instead of freezing the
  path at module import (consistency with every other path helper
  in ``session.py``); the import-time ``CLAUDE_SESSION_FILE``
  constant remains for backward compat but is no longer
  load-bearing.

- **``test_plan_miss_returns_clear_message`` over-narrow markers**
  (PR #592): release-gate integration test failed 2/3 in isolation
  on a freshly-seeded plan library. The LLM produced a perfect
  graceful-degrade response that the substring matcher did not
  recognise: ``"I can't retrieve real-time weather for Tokyo from
  this knowledge base..."``. The accepted-marker list assumed the
  LLM would use the formal "cannot" rather than the contraction
  "can't" and did not include the natural-language shapes the model
  consistently produces ("static indexed", "knowledge base", "no
  tool"). Broadened the markers; assertion still requires SOMETHING
  from a curated list of degrade-shape phrases (not weakened to
  "any non-empty string"). Unrelated to the T1 fix above; surfaced
  by the same shakeout pass.

### Added

- **Regression coverage for the T1 invariant 4.27.0 lacked** (PR
  #590): ``TestT1DatabaseSessionIdResolution`` (16 unit tests)
  parametrizes the four resolution scenarios (explicit-arg-wins,
  env-wins, file-wins, uuid-fallback) across all four entry points;
  ``TestE2ESessionIdSharedAcrossProcesses`` (1 integration test)
  spawns two real subprocesses with no ``NX_SESSION_ID``, asserts
  both converge on the on-disk session_id and round-trip a T1
  entry. Pre-fix the integration test fails because each subprocess
  mints its own UUID; post-fix it passes. **This is the invariant
  test the 4.27.0 ship lacked.** ``TestE2EParallelStress`` (the
  pre-existing "10-parallel" RF-3 verification) explicitly sets
  ``NX_SESSION_ID`` per worker so each subprocess gets its own
  scoped view by design; the missing case was "two processes with
  no ``NX_SESSION_ID``, expect to share Claude session via the
  on-disk pointer".

### Known issues (deferred to follow-up)

The 4.27.0 shakeout also surfaced two adjacent T1 issues left for
a follow-up PR (notes on bead nexus-h8ge):

- ``NX_T1_ISOLATED=1`` is silently ignored when Path B can fire.
  Four-branch order is A -> B -> C -> raise; any shell sibling of a
  live Claude has a discoverable addr file -> Path B wins -> HTTP
  chroma. Operators cannot opt into ephemeral inside a Claude
  session. The CHANGELOG line in 4.27.0 ("Operators who want
  ephemeral semantics opt in via ``NX_T1_ISOLATED=1``") is true
  only outside an active Claude session.
- ``nx tier-status`` and the T1 writer used different session_id
  resolution surfaces pre-fix (audit log got it right, T1 chunks
  did not). Post this release the chains are aligned via
  ``_resolve_session_id``; the underlying surface asymmetry is
  worth its own audit.

## [4.27.0] - 2026-05-07

Minor release. RDR-105 retires the multi-writer T1 coordination layer
that produced six consecutive bug iterations
(GH #567 / #572 / #574 / #575 / #576 / #579) in seven days. The new
hybrid-discovery architecture has a single writer (the top-level MCP
lifespan), one trivial discovery file per Claude session
(`~/.config/nexus/t1_addr.<claude_pid>` containing `host:port\n`), and
a four-branch fail-loud constructor. The class of bugs is structurally
eliminated because the surface it lived on is gone.

### Changed (RDR-105)

- **T1 discovery is now hybrid.** MCP-dispatched subprocesses
  (`claude -p` shared, plan-runner) inherit `NX_T1_HOST` /
  `NX_T1_PORT` from the parent's env. Claude-Code-spawned siblings
  (Bash tools, hooks, shell `nx scratch`) walk the PPID chain to the
  FIRST `claude*` ancestor (NOT the topmost; RF-6 fix) and read
  `~/.config/nexus/t1_addr.<claude_pid>`.
- **`T1Database.__init__` is fail-loud.** Four-branch gate: explicit
  client injection, env-HTTP, file-HTTP, explicit isolation, else
  raise `T1ServerNotFoundError`. No silent fallback to
  `EphemeralClient` (closes GH #567 by structural elimination).
- **`claude -p` dispatch grows three modes.** `share_t1=True` for
  parent-T1 visibility, `ephemeral=True` for stateless one-shots
  (the historical default), and `owned` (default) for sealed-from-parent
  subsessions. `share_t1` and `ephemeral` are mutually exclusive.
- **`NEXUS_SKIP_T1` deprecated** in favour of `NX_T1_ISOLATED`. Both
  honoured for the 4.27 -> 4.28 cycle; a one-shot deprecation
  warning fires when only the legacy name is set. Removed in 5.0.
- **`nx doctor --check-t1`** new flag: diagnoses missing addr file,
  unreachable chroma, and the `exec -a` / wrapper-rename residual
  where the PPID walk's `claude*` prefix check misses.

### Removed (RDR-105)

- Multi-writer session-record machinery: per-session `<uuid>.session`
  JSON files, `sweep_stale_sessions`, `write_session_record_*`,
  `find_session_by_id`, `find_ancestor_session`, `find_claude_root_pid`
  (the topmost-walk that broke owned isolation),
  `_resolve_session_record_with_retry`.
- `src/nexus/t1_watchdog.py` entire file.
- `_t1_chroma_init_if_owner`, `reconcile_owned_chroma`,
  `_resolve_top_level_session_id` from `mcp/core.py`.
- ~5000 LOC across 30 files.

### Fixed

- **GH #579 closed by structural elimination.** All six bug iterations
  in the class (#567 / #572 / #574 / #575 / #576 / #579) shared the
  shape "on-disk session record discovery races with another writer."
  The new architecture has neither on-disk session records nor a
  second writer.

### Behavioural changes (user-visible)

- `nx scratch` from a fresh shell with no Claude Code session running
  now raises `T1ServerNotFoundError` instead of silently landing in a
  per-shell ephemeral. Operators who want ephemeral semantics opt in
  via `NX_T1_ISOLATED=1`.
- `claude -p` plan-runner / non-operator subprocesses default to
  `owned` mode (their own session-scoped T1, sealed from the parent).
  Today these implicitly shared with parent via on-disk-record
  discovery; the new default is sealed-from-parent. Callers needing
  parent-T1 visibility opt in via `share_t1=True`.
- Per-session record files (`~/.config/nexus/sessions/<uuid>.session`)
  are no longer written. The `sessions/` directory will be empty after
  migration; the one-time post-upgrade sweep at MCP startup removes
  any leftovers.
- MCP server crash mid-session loses T1. The pre-RDR-105 architecture
  nominally preserved it (orphan chroma survived MCP crash), but in
  practice operators always restart Claude Code, which restarts MCP.

### Migration

Six PRs landed RDR-105 in develop before this release: #581 (P1
spike), #582 (P2 productionize), #583 (P3 default flip), #584 (P3.1
shakeout playbook), #585 (P4 deletion), #586 (P5 doctor diagnostic +
sub-agent contract docs). The shakeout playbook lives at
`docs/rdr/rdr-105-shakeout.md`.

## [4.26.8] - 2026-05-07

### Fixed

- **`sn` SubagentStart hook delivery** — `sn/hooks/scripts/mcp-inject.sh` migrated from plain stdout to the documented Claude Code JSON envelope (`hookSpecificOutput.additionalContext`). The `nx` plugin made this migration on 2026-05-05 (commit `68854ca`); `sn` was missed. Symptom: spawned subagents did not reliably receive the Serena + Context7 setup ritual (ToolSearch loading recipe, backend pair handling, task→tool table). Fix mirrors the FD-redirect + EXIT-trap pattern in `nx/hooks/scripts/subagent-start.sh`. Body code unchanged. (nexus-t5q2, PR #578)
- **Sequential Thinking imperative restored** — the April 25 trim (`e2fc2408` / PR #320) collapsed the `using-nx-skills/SKILL.md` Essential MCP Tools entry from an active imperative to a noun-phrase listing; the May 5 restore (`aa0076a1` / PR #519) targeted `nx_answer` and didn't bring this back. Subagents were unaffected because the SEQTHINK section in `subagent-start.sh` was unchanged across the trim, but main-session use dropped. Fix: restore the imperative ("use for any non-trivial decision"), the four use-case list (adds risk assessment back), the workflow recipe (hypothesis → evidence → evaluate → branch or proceed), and the `branchFromThought` param hint. Single line, deliberately tighter than pre-trim. (PR #578)

### Tests

- `tests/test_sn_plugin.py` — new `test_envelope_is_valid_json`; existing substring assertions retargeted at the unwrapped `additionalContext`. 26 passed.
- `tests/cc-validation/scenarios/18_real_sn_subagent.sh` — new sandbox scenario, mirrors scenario 12 (real_nx_subagent): installs sn from the working tree, dispatches a real subagent, probes for Serena + Context7 anchor phrases.

## [4.26.7] - 2026-05-06

Patch release. Closes the T1 data-loss class that escaped three rounds of patches in 4.26.4–4.26.6. Six-phase unified fix plus invariant test scaffolding.

### Fixed

- **T1 silent data loss when subprocess SessionStart sweeps parent's session record** (PR #577, GH #576): two-part root cause shipped together since they compose in the failure mode. (1) `reconcile_owned_chroma` renamed `sessions/<lifespan>.session` to the canonical name but did NOT rewrite the JSON content's `session_id` field; the stale field then triggered `sweep_stale_sessions.uuid_stale` on every subsequent SessionStart fire (including subprocess SessionStart from `claude_dispatch`'s plan-runner `claude -p` for `nx_answer` with `verb=research`). The sweep unlinked the canonical record. (2) `T1Database._reconnect` then fell through to a silent `EphemeralClient` fallback (the same defect class PR #569 fixed in the constructor — reconnect was missed) AND used the legacy PID-keyed `find_ancestor_session` resolver instead of the constructor's UUID-keyed chain, so reconnect could not find any post-v4.13 record even when one existed. Fix: reconcile rewrites JSON content atomically; sweep compares filename-stem instead of JSON content (filename is canonical post-rename, JSON is incidental metadata); subprocess `_t1_chroma_init_if_owner` becomes hard read-only when `NX_SESSION_ID` or `NEXUS_SKIP_T1=1` is set (closes the deep-analyst's "fifth bug" — silent spawn-and-overwrite under inherited UUID when the parent's TCP probe races); subprocess SessionStart skips both `sweep_stale_sessions` and `sweep_orphan_tmpdirs` when inherited; reconnect uses the same UUID-keyed resolver as the constructor and raises `T1ServerNotFoundError` on miss instead of EphemeralClient. Live sandbox shakeout exercised reconcile + JSON rewrite + subprocess SessionStart preservation end-to-end.

- **T1 watchdog leak after `reconcile_owned_chroma` rename** (PR #577, GH #575): companion regression to PR #574's sticky-flag fix. The watchdog was spawned with `--session-file=sessions/<lifespan>.session` (CLI-arg snapshot at lifespan time); reconcile renamed the file 88 ms before the watchdog's first poll, so `session_file_has_existed` evaluated False at startup AND on every later poll (the path at `--session-file` was never observed to exist). The `session_file_removed` exit branch PR #574 set out to enable was unreachable for the rest of the watchdog's life — orphan watchdog leak per session boundary. Fix: per-poll lookup via `_find_record_by_chroma_pid` matches the JSON's `server_pid` field (invariant across reconcile renames, `/clear`, `/resume`); cleanup-time also resolves canonical record by `chroma_pid` so the watchdog unlinks the right path on cleanup-fires exits.

### Added

- **Invariant regression scaffolding** (`tests/test_t1_invariants.py`, PR #577): five tests that encode the invariants violated by the data-loss class, so any future patch that re-introduces the same shape fails CI before merge. Includes an `_EPHEMERAL_ALLOWLIST` grep test (any new `chromadb.EphemeralClient(` outside the two opt-in paths fails CI), AST-style audits for resolver symmetry / JSON rewrite / subprocess sweep gate, and an end-to-end #576 chain reproduction that walks lifespan-then-SessionStart drift through subprocess SessionStart and asserts the canonical record survives. The fix-loop pattern (`#567 → #569 → #572 → #573 → #574 → #575 + #576`) terminated with this commit because the codebase finally has the underlying invariants encoded as test assertions rather than patching witnessed instances.

## [4.26.6] - 2026-05-06

Patch release. Two T1-discovery follow-ups to the 4.26.5 silent-write-loss fixes (#567 + race fix #571), both surfaced during the 4.26.5 sandbox shakeout.

### Fixed

- **T1 record key drifts from `current_session` after lifespan-vs-SessionStart race** (PR #573, GH #572): the lifespan can spawn chroma under a stale pointer and SessionStart can write the canonical conversation UUID afterwards, leaving `current_session != sessions/<key>.session`. Pre-fix every later `T1Database()` reads the canonical pointer, looks for `sessions/<canonical>.session`, fails -- entire conversation's T1 broken until manual `NX_SESSION_ID=` recovery. Fix: `reconcile_owned_chroma` re-reads the pointer and renames `sessions/<old>.session` -> `sessions/<canonical>.session` when they've drifted. Wired post-spawn in `_t1_chroma_init_if_owner` (closest race window) AND at first-tool-call via `get_t1` (wider window: SessionStart fired AFTER spawn but BEFORE first tool call). Subagent + reused/nested chroma paths opt out (not their record to rename).

- **T1 watchdog never exited on session-file removal** (PR #574): companion to PR #569's session-file-removed exit. The startup-snapshot flag (`session_file_existed_at_start`) was always False because `_t1_chroma_init_if_owner` spawns the watchdog BEFORE writing the record (so the watchdog can be PID-watched even if the record write fails). Sticky in-loop flag instead: once we observe the file in any poll, the flag stays True; later disappearance triggers exit. Direct watchdog test confirms exit within `POLL_INTERVAL` of file removal.

## [4.26.5] - 2026-05-06

Patch release. Three fixes from the post-4.26.4 day-2 issue triage. Two close long-deferred GH issues (#371 OOM, #436 progress halt) that share a single root cause; one closes a critical T1 silent-write-loss bug surfaced during the 4.26.4 shake-out (#567); one mirrors PR #533's MCP fix into the matching CLI surface (#568).

### Fixed

- **`nx index repo` parent OOM + progress halt on huge files** (PR #566, GH #371 + GH #436): both issues shared a single root cause -- no file-size check anywhere in classification or per-file index paths. A repo with one multi-megabyte text file (vendored minified bundle, generated payload, large JSON config) loaded its full content into memory in `read_text(encoding='utf-8')` before any decode-error check, allocating 2 GB+ RSS. Symptoms: parent process OOM-killed at 3 GB+ on one repo (#371); progress bar stalled at file N with CPU at 0% on another (#436). Fix: `stat()` each candidate file before classification + read; files larger than `indexing.max_file_bytes` (default 5 MiB, configurable per repo) get skipped with a structured warning naming the largest offender.

- **T1 scratch CLI silent write-loss when session file missing** (PR #569 + PR #571, GH #567): two-part fix. PR #569 closed the silent-loss surface (constructor now RAISES `T1ServerNotFoundError` instead of falling through to `EphemeralClient`); PR #571 closed the underlying lifespan-vs-SessionStart race that caused the missing `.session` file in the first place. The race: FastMCP lifespan calls `_t1_chroma_init_if_owner` BEFORE Claude Code's SessionStart hook writes `current_session`. Pre-fix, `_resolve_top_level_session_id` returned None, the init function returned silently, no chroma spawned. Post-fix: `mcp_infra.get_t1` calls `_t1_chroma_init_if_owner` BEFORE constructing T1Database (lazy init by first tool call); the function self-mints a UUID and writes `current_session` when none is set so the MCP server is self-sufficient. Sandbox-validated end-to-end: empty sandbox → MCP boot → mint UUID + write pointer + spawn chroma + write `.session` → separate-process CLI `nx scratch list` reads the entry MCP wrote. Cross-process T1 sharing restored. Opt-in paths preserved -- `client=...` injection (MCP server lifespan, tests) or `NEXUS_SKIP_T1=1` (operator subprocess). `t1_watchdog` self-exits when its session file disappears.

- **`nx catalog list --type` returned empty on small-cardinality types** (PR #570, GH #568): mirror of #538 (4.26.1, PR #533) which fixed the MCP `catalog_list` surface but missed the matching CLI path. Pre-fix the CLI fetched `LIMIT + OFFSET + 1` rows then Python-filtered, so `nx catalog list --type rdr -n 3` on a 15K-entry catalog with 2 rdr rows returned empty (the first 51 rows fetched were all code/docs). Fix: `Catalog.all_documents` accepts a `content_type=` kwarg that pushes `WHERE content_type = ?` into the SQL. CLI no-owner branch routes through it. Owner-path keeps the Python-side filter (small cardinality per owner).

## [4.26.4] - 2026-05-06

Patch release. Four fixes from the post-4.26.3 P3-deferral cleanup round, plus a self-correcting follow-up to one of them. All small, isolated; no schema or migration changes.

### Fixed

- **`store_list` bare prefix `code`/`docs`/`rdr` still misrouted on multi-collection installs** (PR #561 + PR #565, GH #563): the 4.26.3 fix in PR #550 only handled the unique-match case. On installs with multiple `{prefix}__*` collections, the resolver fell through to the existing promotion branch and silently produced `knowledge__code__voyage-context-3__v1` (wrong namespace). PR #561 picks deterministically when 2+ matches: prefer the conformant `{prefix}__{prefix}__<canonical_model>__v1`, then the legacy 2-segment `{prefix}__{prefix}`, then alphabetical first. Logs a `t3_collection_name_bare_prefix_ambiguous` warning so the operator sees the choice. PR #565 carve-out: the deterministic-pick path skips bare `knowledge` so the historical `knowledge__knowledge` legacy fallback (#536) still fires (test_store_put + test_store_get_round_trip locked this contract).

- **Builtin-plan matcher missed common-shape questions even with verb-dimension hint** (PR #562, nexus-qi8t): live repro 2026-05-06 -- `nx_answer` with question "Find papers by Grossberg about ART resonance" and `dimensions={"verb":"research"}` did NOT match the builtin `find-by-author` plan. Routed to inline-planner instead and ran 80s. Root cause: `matcher.py:_superset` enforced strict equality on every dimension. The inline planner classified the question as `verb=query`, the plan declared `verb=research`, so the filter rejected the cosine hit before scoring even when confidence was high. Fix: carve out the verb dimension specifically. Equivalence classes `{query, research, lookup}` (retrieval intents) and `{analyze, review, compare}` (critique intents) are interchangeable for filter purposes. Other verbs (`debug`, `document`, `plan-*`) stay isolated. Other dimensions (`scope`, `strategy`, `taxonomy_domain`) keep strict equality.

- **Smoke-test plan accumulating use_count from production traffic** (no PR, manual fence-off, nexus-w7sg): `weather-tokyo-right-now` (plan id 80, project=nexus) accumulated 10+ uses against real `nx_answer` calls. Disabled via `nx plan disable 80 --reason "smoke-test plan getting matched against production traffic (nexus-w7sg)"`. The `disabled_at` filter in `list_active_plans` keeps it out of `plan_match` candidates going forward. No code change.

### Added

- **`nx catalog link-generate` deprecation alias** (PR #564, nexus-2297 partial): `link-generate` and `generate-links` were near-equivalent verbs that confused operators with no semantic distinction. `link-generate` becomes a hidden, deprecated alias that emits a stderr warning and delegates to `generate_links_cmd` with `citations=False, filepath=True` to preserve historical behaviour. The verb-noun order matches sibling commands (`link`, `unlink`, `links`). Scope B from nexus-2297 (folding diagnostic verbs into `nx doctor`) is deferred -- requires a deprecation cycle on user-facing commands documented in `cli-reference.md`.

## [4.26.3] - 2026-05-06

Patch release. Twelve fixes from the post-4.26.2 day-2 issue triage and shake-out, plus a CI test-order-dependency fix that was masking real failures on the runner. Most user-facing impact: `nx_answer` had been silently disabled session-side on every call (chromadb 1.5.9 regression in the text-side query path), and a generate-terminal plan returned a double-JSON-wrapped `final_text` that broke every prose-rendering skill. Both fixed.

### Fixed

- **`PlanSessionCache.query` crashed inside chromadb 1.5.9** (PR #558, GH #554): every `nx_answer` call logged `plan_session_cache_query_failed` and silently fell back to the un-cached path. The crash was inside chromadb 1.5.9's `convert_np_embeddings_to_list`, which iterates and calls `.tolist()` on each item, failing when the embedding function returned plain Python lists. `LocalEmbeddingFunction` returns `list[list[float]]`, so every conexus install hit it on every invocation. Fix: pre-embed the intent via `LocalEmbeddingFunction` directly and pass `query_embeddings=` to chromadb. Bypasses the text-side adapter and the `convert_np_embeddings_to_list` landmine entirely. Session-level plan caching is live again.

- **`nx_answer` final_text was double-JSON-wrapped on generate-terminal plans** (PR #559, GH #555): when the plan's terminal step was a `generate` operator (matched or dynamic), `final_text` came back as `'{"output": "{\\\"output\\\": \\\"actual prose...\\\"}"}'` with two layers of `{output: ...}` wrapping and the prose at the third level. The same broken value landed in `nx_answer_runs.final_text` and in the `structured=True` envelope's `final_text` field, breaking every skill that prints results as prose (research, query, debug, document, analyze). Fix: add `output` and `comparison` to the final-step text-key search so the canonical generate-terminal field is surfaced directly instead of via `json.dumps` fallback, plus a strict-shape recursive unwrap (`_maybe_unwrap_output_envelope`) that handles the double-wrap case the bundle prompt confused the model into producing.

- **`OperatorError` graceful-fallback test class-identity drift on CI** (PR #560): the runner's three operator-failure catches (`except _OperatorError`) bound the class at module import time. Under test-order-dependent state (mock.patch on the dispatch module's attribute table during `test_nx_answer.py`'s `with patch.object(_dispatch_mod, 'claude_dispatch', ...)` chains), the exception's class identity drifted from the runner's bound reference and the catch silently failed, breaking every CI run since #548. Fix: replace `except _OperatorError` with `except Exception as exc: if not _is_operator_error(exc): raise` where `_is_operator_error` uses live module attribute + name+module fingerprint match. Identity-drift-proof.

- **`store_list` bare prefix `code`/`docs`/`rdr` returned wrong namespace** (PR #550, GH #545): the 4.26.2 fix in #536 only covered the special case where the legacy 2-segment `knowledge__knowledge` happened to exist. For `code`, `docs`, `rdr` there's no `<x>__<x>` convention, so bare `nx store list -c code` resolved to `knowledge__code__voyage-context-3__v1` (wrong namespace) and the operator got "No entries" while the data sat under `code__myrepo__voyage-code-3__v1`. Fix: when t3 is supplied, user_arg is in CONTENT_TYPES, and there's no `__`, probe live T3 for collections starting with `{user_arg}__`. Unique match returns it; zero or multiple falls through to existing promotion.

- **`nx index repo` slug derivation accepted unsanitized basenames** (PR #553, GH #551): `nx index repo /git/com.conductor.sys.monitoring` persisted the basename verbatim into the registry, including the dots. The resulting collection name (`code__com.conductor.sys.monitoring-b25083f0`) failed `validate_collection_name` on every subsequent index attempt, looping forever in the index log on every git-hook fire. Fix: extracted `_sanitise_owner_segment` helper that replaces every non-alnum char with a hyphen, collapses adjacent hyphens, and strips leading/trailing hyphens.

- **`nx collection reindex` skipped post-processing** (PR #556, GH #369): reindex re-embedded chunks but did NOT run any of the post-index pipeline (catalog updates, taxonomy discovery, Claude auto-labeling, cross-collection projection, cooccurrence links, topic-link compute, L1 context refresh). After a bulk reindex (e.g. an embedding-model upgrade), the catalog/taxonomy/links went stale and the operator had to know to re-run `nx index repo` per repo to recover. Fix: extracted `run_collection_postprocessing(collections, *, repo_path, quiet)` to module level. Both `index_repo_cmd` and `reindex_cmd` now call the same chain.

- **Bib enricher 1-token title coincidence over-acceptance** (PR #547 + PR #552, nexus-5cez): `_titles_compatible` auto-accepted single-token coincidences. The short-source relaxation (designed for filename-derived 2-token titles like `Pbeegees` matching `pBeeGees: A Prudent Approach to ...`) was firing on 1-token sources (`Survey`, `Methods`, `Notes`) where any OpenAlex paper that mentions the word would over-accept and stamp the wrong bib. Fix: denylist of common single-word title coincidences. Rare invented words like `Pbeegees` are not in the denylist and continue to accept under the existing short-set relaxation.

- **`OperatorError` propagated unhandled through plan_run** (PR #548, nexus-l0yh): when an operator step raised `OperatorError`, the runner died with an unhandled exception instead of returning partial results with a failure note. Three call sites patched: bundle dispatch, bundle-fallback per-step, isolated step. Each now substitutes a sentinel `{error, status: failed, tool, step_index, text: '', summary: '', aggregates: []}` into step_outputs and continues with the next step. Mirrors the retrieval-tool short-circuit shape.

- **`claude_dispatch` discarded partial output on timeout** (PR #548, nexus-1at5): when the subprocess timeout fired, the partial stdout/stderr in the pipe buffers was discarded. After SIGKILL+reap, the writer is dead and `read()` returns EOF immediately, so `_drain_pipe` collects whatever was produced and persists it to `~/.config/nexus/logs/operator-timeout-<ts>.log` for post-mortem.

- **T3 query crashed on `None` metadata rows** (PR #547, GH #373): `t3.py:942` and `t3.py:1051` unpacked Chroma metadata via `**meta` without a None check. ChromaDB returns `None` metadata for corrupted or upsert-without-meta rows; the bare `**` then raised `TypeError: 'NoneType' object is not a mapping` and killed the entire query, even though the other rows in the result set were valid. Fix: coerce None (and non-dict) to `{}` with a structlog warning so the row still surfaces with empty metadata fields and the failure leaves a footprint instead of an opaque crash.

- **CI Python 3.12 sqlite multi-conn flake** (PR #546, nexus-ybvl): `test_console_health_aspect_queue::test_mixed_statuses_aggregate_correctly` flaked on the GH Actions Linux Python 3.12 runner (system sqlite 3.37 + ext4) when the cross-connection fsync visibility window between commit and the next open's read was wide enough to occasionally make `_collect_aspect_queue_data()` miss a just-committed row. Fix: WAL + synchronous=FULL on the writer, plus an explicit `wal_checkpoint(FULL)` after each commit.

### Added

- **`nx index repo --corpus { docs | knowledge }`** (PR #557, GH #451): default `docs` keeps current behaviour. `--corpus knowledge` mutates the registry's `docs_collection` field after `reg.add()` to use the `knowledge__` prefix. Every downstream layer reads the field directly, so a single edit reaches every prose / PDF routing site. Code routing is unaffected. Sticky: subsequent runs without the flag preserve the operator's choice.

- **Scratch `get` / `delete` accept unique 8-char prefix** (PR #549, nexus-zpw6): pasting back the prefix `scratch list` displays now resolves cleanly. `T1Database._resolve_id` does exact-then-prefix resolution scoped to `session_id` (mirrors `MemoryStore.resolve_title`'s nexus-e59o shape). MCP `scratch_get` / `scratch_delete` surfaces a clean disambiguation message instead of silently picking when the prefix is ambiguous.

## [4.26.2] - 2026-05-06

Patch release. Seven bug fixes plus one prep migration for 4.27.0. All surface from the post-4.26.1 day-2 issue triage: silent failures in CLI surfaces (search, store_list, catalog list), runtime hygiene (line-buffered stdout, legacy session.lock cleanup, WAL growth bounds), and the source_uri backfill required before RDR-096 Phase 5 can drop `source_path`.

### Fixed

- **`store_list` bare-prefix resolver bridges to legacy 2-segment collection** (PR #536, GH #535, nexus-6mr0): when a user passes `store_list(corpus="code__nexus-1-1")` and the conformant 4-segment collection (`code__nexus-1-1__voyage-code-3__v1`) does not yet exist, the resolver now falls back to the bare 2-segment legacy form (`code__nexus-1-1`) instead of returning empty. Bridges installs that have not yet run RDR-103 collection rename; production paths that worked in 4.25.x continue to work in 4.26.x.

- **`nx catalog list --owner` accepts owner name (not just tumbler)** (PR #539, GH #537, nexus-1lx7): `--owner project-foo` previously crashed because the resolver only understood dotted tumbler form (`1.2`). Now resolves the name through `Catalog.owner_tumblers_by_name(name)` and surfaces a clean Click error listing candidate tumblers when the name is ambiguous (UNIQUE constraint is `(name, owner_type)`, so duplicates exist legitimately).

- **`nx search --corpus` accepts comma-separated values** (PR #541, GH #538, nexus-v8cj): the documented form `--corpus a,b,c` worked for some commands but `nx search` only accepted repeated `--corpus` flags. Now the value is CSV-expanded before resolve_corpus runs, restoring parity across the CLI.

- **CLI line-buffers stdout/stderr to flush progress in non-TTY** (PR #542, GH #370): when `nx index repo` was piped or run under a hook, progress messages buffered for minutes at a time, making long indexes appear hung. Calls `sys.stdout.reconfigure(line_buffering=True)` (and stderr) at CLI entry; safe-guarded with try/except for environments where the streams do not support reconfigure.

- **`session_start` cleans up pre-v4.13.0 legacy `session.lock` relic** (PR #543, GH #435): pre-v4.13.0 installs left a sentinel `session.lock` file in `~/.config/nexus/sessions/` that no current code reads or removes. New `_cleanup_legacy_session_lock(sessions_dir)` runs after `sweep_stale_sessions` with a PID liveness probe (`os.kill(pid, 0)`) so a live old install is not disturbed.

- **Catalog DB caps WAL growth via `journal_size_limit`** (PR #544, GH #437): under long-lived MCP-server reader connections, SQLite auto-checkpoint runs only as PASSIVE; PASSIVE folds frames into the main DB but cannot truncate the WAL file. Reporter observed 12 MB WAL after a few hours. Adds `PRAGMA journal_size_limit=67108864` (64 MiB) on the `CatalogDB` connection so the WAL caps at a bounded steady-state size.

### Added

- **`document_aspects.source_uri` backfill migration** (PR #540, nexus-pnje): a Migration entry at version 4.26.2 that fills `source_uri` for any row where it is NULL or empty by deriving it from `source_path` (`file://` prefix). Live audit: 61 of 579 rows had empty `source_uri`. Idempotent. Prerequisite for RDR-096 Phase 5 (nexus-ocu9.11) which drops the redundant `source_path` column.

- **`nx catalog list` resolves `--owner` by name OR tumbler** (PR #539): the new resolver path uses `Catalog.owner_tumblers_by_name(name) -> list[Tumbler]`. Empty list raises a clean ClickException; one match uses it; multiple matches list candidates for the operator to disambiguate.

## [4.26.1] - 2026-05-06

Patch release. Three bug fixes from the post-4.26.0 round, all surfacing the same theme: silent-failure paths in MCP / runner code that returned plausible-looking results but lost data.

### Fixed

- **catalog_search ignored `query` when `content_type` was also set** (PR #532, nexus-a414 Part 1): the SQL filter path triggered on `content_type` alone and never incorporated `query` into the WHERE clause, so `catalog_search(query='RDR-104', content_type='rdr')` returned the first N rdr rows regardless of query content. Live consequence: agents following the AUTO-LINK recipe got back the wrong tumblers (or fell back to T3 chunk doc-IDs that don't parse as Tumbler), the auto-linker silently skipped them, and the catalog graph stayed empty. Routing fixed: when `query` is non-empty, fall through to the FTS5 path which already supports `content_type` via `cat.find(query, content_type=...)`. Together with the auto-linker observability fix already in 4.25.3, this closes both halves of nexus-a414's silent-zero-link path.

- **catalog_list ignored `content_type` at the SQL layer** (PR #533, nexus-blk2 Part 1): the docstring promised the filter; the SQL had no `WHERE` clause for it. Filtering happened in Python AFTER the SQL `LIMIT/OFFSET`, so `catalog_list(content_type='rdr', limit=5)` returned `[]` whenever the first 5 rows were any other type. Production catalog had 2,270 rdr docs and the matching list call returned nothing. Pushed `content_type` into the SQL `WHERE` so pagination is correct regardless of filter ratio. Both no-owner and by-owner branches updated; `limit+1`/`has_more` pattern brings the `_pagination` footer in line with `catalog_search`.

- **catalog_resolve leaked `Tumbler.parse` `ValueError`** (PR #533, nexus-blk2 Part 2): the dashed format produced by `nx doctor` (e.g. `1-2188`, `Luciferase-f2d57dbc`) is the physical-collection prefix, not a tumbler. `Tumbler.parse` called `int()` on the dashed string and leaked the raw `invalid literal for int() with base 10` error. Tutorials point at `catalog_resolve` as the entry for semantic-identity → physical-collection routing; the bare `ValueError` silently broke downstream skills. Wraps `Tumbler.parse` with a typed diagnostic that explains the dotted-tumbler requirement and explicitly notes that `nx doctor` output is NOT a tumbler.

### Added

- **`plan_run` emits per-segment progress events** (PR #534, nexus-0qi9): paired `nx_answer_step_start` / `nx_answer_step_complete` structured logs at INFO, with `kind`, `step_indices`, `tools`, `total_steps`, and `elapsed_ms` (on completion). Closes the visibility gap that made multi-step `nx_answer` runs feel like a hang. Empirical from 100 production runs (memory: tier-discipline-audit-2026-05-06): 32% under 5s, 5% in 5–30s, 40% in 30s–2min, 23% in 2–5min. With zero per-step signal, anything past 5s looked wedged. Events flow to `~/.config/nexus/logs/mcp.log` for tail-style consumers; the `nx_answer` docstring now documents the empirical latency so callers do not assume sub-second.

## [4.26.0] - 2026-05-06

Minor release. Ships the tier-discipline observability subsystem: per-call telemetry of T1/T2/T3/plan writes, a CLI to audit them, agent attribution kwargs, and hook + agent-file guidance that teaches subagents to tag their writes. Closes the empirical-loop gap that PR #519 had to recreate from external transcript mining: the next time discipline regresses, the data to spot it lives in the same database the writes do.

### Added

- **``tier_writes`` T2 telemetry table** (PR #525, nexus-kren). One row per call to a tier-write MCP tool. Columns: ``id``, ``session_id``, ``ts``, ``tool``, ``tier``, ``agent``, ``project``, ``target_title``. Three indexes (session_id, ts, tool). Migration registered at 4.25.5; created lazily by the recorder so installs without writes never see it.
- **``_record_tier_write`` helper in ``mcp/core.py``** (PR #525). Best-effort: any failure swallowed so telemetry can never break the hot path of the calling tool. Wired into ``memory_put`` (T2), ``store_put`` (T3), ``scratch put`` (T1), ``plan_save`` (plan).
- **``nx tier-status`` CLI** (PR #526, nexus-a52i). Default reports the current session via ``NX_SESSION_ID`` env or ``read_claude_session_id()``; ``--session``, ``--last N``, ``--since <iso>`` override (mutually exclusive); ``--json`` for downstream tooling.
- **``nx doctor --check-tier-discipline``** (PR #526). Audits current session: prints the tier-write summary and emits a soft warning when zero writes are recorded for a substantive session. Heuristic only; never exits non-zero.
- **``memory_put`` accepts ``agent`` and ``session`` kwargs** (PR #527, nexus-9clx). Schema columns existed since project Phase 1 but were never wired through the MCP layer; 1012 of 1012 production rows had both NULL. Empty-string defaults translate to None so the existing ``MemoryStore.put`` fall-back chain runs (``NX_AGENT`` env, then NULL). Session resolution now happens at the MCP layer so ``NX_SESSION_ID`` env wins over the legacy getsid file.
- **``scratch`` (T1) accepts ``agent`` kwarg** (PR #531). T1 metadata model differs from T2 (chroma metadata dict, not SQL columns) so it shipped separately. ``T1Database.put`` accepts ``agent`` (default ``""``); empty falls back to ``NX_AGENT`` env. Persisted in chroma metadata; ``nx tier-status`` slices T1 writes by agent via the ``tier_writes`` mirror.
- **SubagentStart hook AGENT TAG injection** (PR #528). One line added to the NX_AUTOLINK heredoc reaches every dispatched subagent: ``AGENT TAG: pass agent="<your-role>" to memory_put so nx tier-status slices writes by agent``. Single point of injection beats updating 10+ agent files individually. Heredoc stays under the 500-byte bash 5.3 deadlock guard.
- **Per-role agent kwarg in 10 agent files** (PR #530). The canonical Post-flight ``memory_put`` example in each producing-findings agent now includes ``agent="<role>"`` matching the agent's filename stem, plus a one-sentence pointer at ``nx tier-status``. Belt-and-braces with the hook injection. Stub agents (knowledge-tidier, plan-auditor, plan-enricher) excluded; already MCP-delegated.
- **SessionEnd tier-write summary** (PR #529). The launcher now prints a one-line stderr summary BEFORE the daemonization fork so the operator sees their own session's contribution count without invoking ``nx tier-status``. Suppressed when no session resolvable, no DB, no table, or zero writes (transactional sessions stay quiet).

### Verified end-to-end

- 4 PRs of empirical mining preceded the implementation: 2274 pre-trim sessions surveyed, 2622 transcripts parsed, top-discipline sessions analysed for the synthesis-flywheel and artifact-driven patterns. Findings persisted in T2 (tier-discipline-audit-2026-05-06, past-conversation-mining-2026-05-06, past-working-patterns-2026-05-06).
- Live smoke against production T2 confirms ``nx tier-status`` returns realistic per-tier summaries and ``nx doctor --check-tier-discipline`` audits the current session.
- Unit + integration suite green.

### Why

Past mining showed only **5.8% of pre-trim sessions used any tier tool, and 1.7% wrote back**. PR #519's surgical hook restoration was only possible because the prior trim left a measurable footprint via search_telemetry + nx_answer_runs. Today's restorations had no equivalent measurement rung for tier-discipline specifically. This release builds it: every write is now visible, attributable, and aggregable per session and per agent.

The discipline still lives in a small high-value tail (synthesis flywheels, RDR-driven workflows). The next iteration's job is to make it easier for those sessions to start and harder to abandon, but first you need the data to know which interventions move the needle. This release delivers the data.

## [4.25.4] - 2026-05-06

Patch release. Fixes two aspect-extraction worker bugs surfaced by the 4.25.3 live shakeout. Both bugs caused silent worker failure modes that left rows wedged in the queue.

### Fixed

- **Aspect-extraction worker batches mixed-collection rows together, marking the whole batch failed** (nexus-nncy, P2): ``claim_batch`` grabs FIFO across collection boundaries, so a ``knowledge__*`` row enqueued before an ``rdr__*`` row lands in the same claim. ``extract_aspects_batch`` enforces single-``ExtractorConfig`` homogeneity per its docstring contract and raises ``ValueError``, marking every row in the batch failed even though each row would have succeeded individually. The worker's ``_process_batch`` now detects heterogeneity at the top of the function (``configs = {select_config(row.collection) for row in rows}``), logs ``aspect_worker_batch_heterogeneous_fallback`` with row count and observed config names, and falls back to per-row processing via the existing single-row path. Homogeneous batches keep the cost-amortised path. Verified live during the shakeout: a 13-row queue (8 reset-pending plus 5 reclaimed orphans) drained cleanly with the fallback firing twice on mixed batches.

- **`reclaim_stale` SQL never matches production timestamps, dead-worker orphans persist forever** (nexus-7yoz, P2): production writes ``last_attempt_at`` via ``datetime.now(UTC).isoformat()`` (``2026-05-06T03:01:51.332866+00:00``, T separator, ``+00:00`` suffix, microseconds). The reclaim SQL compared against ``datetime('now', '-N seconds')``, which returns ``2026-05-06 10:01:54`` (space separator, no timezone, no fractional seconds). String comparison fails because ``'T' (0x54) > ' ' (0x20)``, so production-formatted ``in_progress`` rows always sort after the cutoff and never match the WHERE clause. Effect: a worker that died after claiming rows orphaned them forever, regardless of the 300-second reclaim timeout. The shakeout exposed this with 5 rows stuck ``in_progress`` for 6.85 hours. Existing tests masked the bug because they injected ``last_attempt_at`` via SQL ``datetime('now', '-N minutes')``, which already matches the cutoff format. Fix wraps ``last_attempt_at`` in SQLite's ``datetime()`` so both formats normalise before the compare; new test injects via Python ``datetime.now(UTC).isoformat()`` to mirror production writes.

### Verified end-to-end

- Live shakeout drain: 13-row queue cleared in one worker session, ``aspect_worker_batch_heterogeneous_fallback`` event fired twice, ``document_aspects`` grew by 4 net new rows plus 6 upserts of existing aspects.
- Unit suite: 6549 pass, 33 skip, 3 xfail, 0 fail.
- Targeted aspect tests: ``test_aspect_worker.py`` 14/14 pass; ``test_aspect_extraction_queue.py`` 27/27 pass; ``test_aspect_extractor.py`` clean.

## [4.25.3] - 2026-05-05

Patch release. Fixes two observability bugs surfaced by the live shakeout of 4.25.2's hooks/agents reinforcement (PRs #519 + #520) and tightens the agent / skill prompts the shakeout exposed as escapable.

### Fixed

- **AUTO-LINK silent failure** (nexus-a414, P1): recipe-compliant agents calling ``store_put`` after the ``catalog_search → scratch put with link-context tag → store_put`` recipe could land zero links and get no error signal when targets failed ``Tumbler.parse`` (e.g. T3 chash hex landed in scratch instead of tumbler strings, the canonical bug-causing pattern surfaced by the live shakeout). ``auto_link()`` now returns an ``AutoLinkResult`` dataclass with separated counts (``created``, ``skipped_invalid_tumbler``, ``skipped_missing_endpoint``); invalid-tumbler skips upgrade from DEBUG to WARNING with an actionable hint pointing at the correct ``catalog_search`` field; ``catalog_auto_link`` emits an ``auto_link_summary`` log per non-trivial outcome (WARNING when contexts present + zero created + invalid-tumbler skips > 0); the bare-except wrapper in ``mcp/core.py:948-955`` is replaced with named-exception capture that logs unexpected failures at WARNING via ``store_put_auto_link_failed``. End-to-end verified live: a recipe-compliant call against RDR-104 tumblers materialised two catalog edges (``relates``, ``cites``) on the spot.

- **nx_answer planner transient JSON parse failures** (nexus-wr5o, P2): ``_nx_answer_plan_miss`` now retries once on ``OperatorOutputError`` (transient model-output drift, partial stream, null ``structured_output`` on first attempt) with a halved 150 s timeout so a single hang doesn't double total wall time. ``OperatorError`` (subprocess non-zero) and ``OperatorTimeoutError`` do NOT retry — those failure modes are not transient. WARN log on first failure with attempt number; the second exception (most actionable diagnostic) propagates on exhaustion.

### Changed

- **10 agent files** + **8 producing skill files**: tightened ``## Pre-flight`` lead to explicitly name the rationalization the using-nx-skills Red Flags table warns about (skipping pre-flight on grounds "the code is the answer / tiers won't help"). Reframed ``## Post-flight`` write-back from "what a future session would benefit from" to audience-aware language, with T1 ``scratch_put`` added as a first-class write target for sibling agents downstream THIS session (the original wording omitted T1 entirely from the write-back menu, framing all three tiers as "future sessions"; the live shakeout's sibling-sharing probe confirmed T1 IS the bus for in-session promotion).

### Verified end-to-end

- ``tests/cc-validation/scenarios/12_real_nx_subagent.sh`` (hook injection): pass.
- ``tests/cc-validation/scenarios/13_disambiguate_subagent_inject.sh`` (3 sub-scenarios: project bash multi-line, plugin bash multi-line, JSON envelope): all pass.
- AUTO-LINK happy path via live MCP toolchain: two catalog edges materialised against RDR-104 tumblers ``1.2188.863`` and ``1.2189.146``.
- AUTO-LINK error path via direct Python: WARNING emitted with actionable hint for both T3 chash strings (``aca95577feec25d1``, ``ddbff7f16e4454e2``) used in the original shakeout failure.
- ``auto_link()`` and ``_nx_answer_plan_miss`` retry: 16 + 5 new unit tests, all pass; 1714 affected-suite tests pass post-change.

### Honest finding

The agent / skill prompt changes make pre-flight rationalizations VISIBLE in the agent's self-report ("this is a rationalization the prompt warns about, I should have run it") but do NOT prevent them at the prompt-strength tried so far (including the existing ``<HARD-GATE>`` block with "MUST", "STOP", "Do NOT return without persisting"). Behaviorally enforcing the discipline requires harness-level mechanisms (Stop hook inspecting tool-call history, etc.) rather than further prompt wordsmithing — left as follow-up. The transparency improvement remains a real win: silent-skip is now acknowledged-skip, which is the precondition for any future enforcement work.

## [4.25.2] - 2026-05-05

Patch release. Reinforces the hook-side composed-retrieval guidance shipped in 4.25.1 — same signal landed in the agents' and skills' own role descriptions, so the guidance reaches both ambient subagent context (SubagentStart hook) AND the agent-file surface that subagents reason about as their role definition.

The 4.25.1 SubagentStart hook restoration was already verified driving behavior end-to-end (probe A: subagent on a verb-shape question called ``nx_answer`` and ``memory_put``, citing verbatim hook lines; probe B: subagent on a research-and-link task followed the full 3-step AUTO-LINK recipe ``catalog_search → scratch put → store_put``, citing each step). 4.25.2 reinforces in the agent's own voice — redundant by design, not the primary lever.

### Added

- **10 non-stub agent files** get a ``## Pre-flight (plan reuse + tier check)`` + ``## Post-flight (write-back — mandatory before returning)`` block (~17 lines each, inserted before ``## Relay Reception``):

  - architect-planner, code-review-expert, codebase-deep-analyzer, debugger, deep-analyst, deep-research-synthesizer, developer, strategic-planner, substantive-critic, test-validator.

  Pre-flight: ``plan_search`` → ``memory_search`` (T2) → ``nx_answer`` for verb-shape questions → ``scratch search`` for sibling work-in-progress. Post-flight: ``store_put`` (T3 cross-project), ``memory_put`` (T2 project), ``plan_save`` for pipeline outcomes. *"Findings not stored are findings lost."*

- **8 producing skill files** get a ``**Tier-aware discipline**`` block (~12 lines each, inserted right after frontmatter):

  - research, research-synthesis, deep-analysis, analyze, query, document, knowledge-tidying, debug.

  Same shape as the agent block, more compact: read widest → narrowest, reuse plans before dispatch, write back at end.

### Out of scope (intentionally untouched)

- Stub agents (knowledge-tidier, plan-auditor, plan-enricher) — already MCP-delegated; agent file is a thin redirect.
- Reference skills (serena-code-nav, nexus, cli-controller) — describe tooling, don't produce findings.
- Discipline gates (brainstorming-gate, finishing-branch, git-worktrees, plan-first, plan-validation) — process gates, not retrieval/synthesis.

### Token cost

~150 tokens per agent invocation when *that* agent is dispatched. Subagent dispatch cost (SubagentStart hook content) is unchanged — agent files only weigh in when the agent is selected. (nexus-t4ke)

## [4.25.1] - 2026-05-05

Patch release. Two fixes: a pre-RDR-104 catalog-rebuild perf cap, and an over-correction in the April 25 startup-hook trim that quietly broke the agent guidance for composed retrieval (``nx_answer``, ``plan_search``, ``catalog_search``, ``store_put``).

The catalog change removes a ``_event_log_covers_legacy()`` O(N) scan from the rebuild dispatch path on catalogs whose event log is already steady-state. On a 460K-event production catalog the post-write rebuild path drops from ~850 ms to ~1 ms; the MCP server's per-tool-call latency floor immediately after any catalog write moves with it. RDR-104's advertised <100 ms incremental target is now achievable at production scale.

The hook change restores the behavior-driving content (analytical-question routing examples, tier "when to check" cues, AUTO-LINK recipe, WRITE-BACK exhortation) that was over-trimmed in commit e2fc2408 (PR #320). Telemetry from 795 session transcripts (10-day pre/post-trim windows) showed `nx_answer` use dropped 78%, `plan_search` 90%, `catalog_search` 100%, `store_put` 100% — exactly the tools whose recipes/examples/reasoning had been most condensed. Bare tool listings drove T1 and raw-search use just fine; composed retrieval needed trigger phrasings and a "why".

### Fixed

- **``Catalog._ensure_consistent`` skips ``_event_log_covers_legacy()`` once the offset marker is established.** The bootstrap guardrail's job is to refuse the event-sourced rebuild while bootstrap is still in progress (sparse event log against a populated legacy JSONL). ``_write_offset_marker`` is only reached from rebuild branches that ran after the guardrail accepted the event log, so the existence of ``_meta.last_applied_event_offset`` proves the guardrail has already passed at least once. The O(N) line+JSON scan of both ``events.jsonl`` and ``documents.jsonl`` (~838 ms on a 460K-event log) was running on every post-write rebuild dispatch and capping the RDR-104 incremental fast path well above its <100 ms target. The marker check is a single ``SELECT key, value FROM _meta`` and short-circuits before the scan, so the steady-state path is microseconds. Bootstrap and marker-loss-recovery semantics are unchanged: when the marker is absent, the guardrail still fires and ``bootstrap_fallback_active`` still flips on sparse logs. (nexus-1sy5)

- **``nx/skills/using-nx-skills/SKILL.md``: behavior-driving signal restored.** Brought back the "ALL analytical questions go through ``nx_answer``" header with verb-shape paragraph and "composed > raw chunks" reasoning; the tier "when to check" cues ("check before researching" / "before project work" / "before duplicating sibling work"); three specific ``search``→``nx_answer`` phrasings in Common Mistakes ("how does X work", "tradeoffs in Y", "compare X across projects"); the "Findings not stored are findings lost" exhortation; and a five-row tool-skipping Red Flags table (down from twelve — the seven meta/skill-priority rows that weren't behavior-driving stay cut). Net 5803 chars vs 8681 pre-trim — 33% smaller while restoring the load-bearing content. (nexus-xxsj)

- **``nx/hooks/scripts/subagent-start.sh``: same restoration on the SubagentStart side, now wrapped in the documented JSON envelope.** ``NX_TIERS`` heredoc carries the tier "when to check" cues inline; ``NX_T3`` heredoc adds the verb-shape routing line and WRITE-BACK exhortation; new ``NX_AUTOLINK`` heredoc restores the ``catalog_search → scratch put → store_put`` 3-step recipe as a discoverable workflow. The script now emits its content via the documented ``{"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": "..."}}`` envelope (plain stdout works today per ``tests/cc-validation/scenarios/13_disambiguate_subagent_inject.sh`` 13a/13b, but the JSON envelope is the explicit contract — future-proof against parser tightening). FD-redirection wrapper at the top of the script captures body stdout into a tempfile; an EXIT trap restores stdout and emits the envelope. All heredoc bodies remain under the 500-byte bash 5.3 deadlock guard. (nexus-xxsj)

### Tests

- ``tests/test_catalog_bootstrap_guardrail.py``: two new tests pin the optimization invariant and the bootstrap invariant. ``test_covers_legacy_skipped_when_marker_established`` patches ``_event_log_covers_legacy`` to record invocations and asserts it is not called once the marker exists; ``test_covers_legacy_runs_when_marker_absent`` builds a sparse-log fixture, asserts the guardrail still fires, and pins ``bootstrap_fallback_active = True``. The four pre-existing bootstrap tests (C1 floor, C2 fallback flag set, fallback flag clear, doctor surface text+JSON) continue to pass unchanged.

- ``tests/cc-validation/scenarios/12_real_nx_subagent.sh``: real plugin end-to-end injection probe — passes with the JSON envelope wrapper, confirming hook content delivery to dispatched subagents.

- ``tests/cc-validation/scenarios/13_disambiguate_subagent_inject.sh``: 3 sub-scenarios (project-level plain stdout, plugin-level plain stdout, JSON envelope positive control) — all pass, definitively answering "is the SubagentStart hook silently broken" in the negative.

## [4.25.0] - 2026-05-05

Minor release. Lands RDR-104 (Incremental Catalog Projection Rebuild). Steady-state ``Catalog()`` construction after a single write becomes <100 ms instead of ~4 s on a 452K-event log because the rebuild now replays only the delta of new bytes in ``events.jsonl`` rather than the entire file. Transparent to callers; same API surface.

### Added

- **Incremental rebuild path in ``Catalog._ensure_consistent``.** Five-way dispatch over the event-sourced rebuild branch: existing mtime fast path, new empty-delta fast path (events.jsonl unchanged, advance only ``last_consistency_mtime``), bootstrap full rebuild, invalidated full rebuild (header-hash drift or window-size mismatch), incremental (replay only the byte-range delta), and corruption escalation (zero events from a non-empty range falls back to full rebuild without advancing the marker). The full-rebuild path retains the FTS5 bulk-load fence; incremental writes are bounded by delta size and bypass it. Every marker write (mtime + offset + header-hash + window) commits inside the same ``transaction()`` block as the projector writes for the 4.24.4 atomicity contract.

- **``EventLog.replay_from(offset, *, limit_offset)``.** Offset-aware streaming iterator that yields events whose start-of-line byte offset is in the half-open range ``[offset, limit_offset)`` (or to EOF when ``limit_offset is None``). Binary-mode file open + ``seek(offset)`` so byte positions are portable across platforms. The bounded form is mandatory for concurrent-appender safety: a writer landing between the orchestrator's ``stat()`` snapshot and the iterator's read window must not extend the iterator past the captured offset, or the marker drifts below the true tail and incremental never settles. Mid-line / malformed-first-line behaviour follows the existing ``replay()`` warn-and-skip pattern.

- **Three new ``_meta`` marker rows** (``last_applied_event_offset``, ``last_applied_event_header_hash``, ``last_applied_event_header_window``) plus the existing ``last_consistency_mtime``. The window is persisted alongside the hash so a future bump of ``_HEADER_HASH_BYTES`` (currently 64 KB) invalidates prior markers cleanly via the window-size check rather than silently comparing hashes computed over different windows. The reader returns ``None`` on any incomplete or unparseable state so the orchestrator falls through to full rebuild rather than acting on partial metadata.

### Fixed

- **``DELETE FROM collections`` added to both rebuild paths.** Pre-fix ``Catalog._ensure_consistent`` (event-sourced) and ``CatalogDB.rebuild`` (legacy) both DELETEd ``owners``/``documents``/``links`` before reloading but excluded ``collections``. Combined with ``_v0_collection_created``'s ``INSERT OR REPLACE`` plus its ``COALESCE`` preservation pattern for ``superseded_by``/``superseded_at``/``created_at``, the rebuild silently inherited stale supersede metadata that no replay event re-validated. The COALESCE in the projector verb is retained because it is load-bearing for the degraded-path retry case (incremental rebuild that rolls back mid-delta leaves the marker put; the next retry replays the same delta against an un-cleared table — the COALESCE preserves supersede metadata from events before the marker).

### Tests

- ``tests/test_catalog_incremental_rebuild.py`` (new): 19 tests covering all five branches plus full-rebuild-vs-incremental projection equality, 4.24.4 atomicity on the incremental path, malformed-line warn-and-skip, double-apply idempotency, collections round-trip via incremental, concurrent-appender bounded form (orchestrator-level race simulation), ``CatalogDB.commit``-not-called invariant, the documented same-size-rewrite known-cost case, the split-pair conditional-idempotency case for ``_v0_document_aliased``, and a performance budget pin.

- ``tests/test_catalog_collections_rebuild.py`` (new): event-sourced rebuild clears stale supersede metadata; round-trips ``CollectionSuperseded`` correctly; legacy ``CatalogDB.rebuild`` clears the table even with no events.

- ``tests/test_catalog_event_log.py``: 10 new ``TestReplayFrom`` scenarios covering bounded-form caps, half-open boundary semantics, EOF behaviour, ``ValueError`` on offset > file_size, mid-line offset warn-and-skip, and ``replay_from(0)`` equivalence with ``replay()``.

- ``tests/test_catalog_consistency_marker.py``: extends the 4.24.4 baseline test to append a real event before the patched-rebuild raise so it hits the ``apply_all`` path; adds 11 scenarios for header-hash helper, marker round-trip, partial-marker-returns-None, atomicity rollback under raise.

### Background

The arc was tracked under epic ``nexus-plpv`` with five sequential beads (``nexus-rhvo`` / ``nexus-v386`` / ``nexus-rpgn`` / ``nexus-3sx1`` / ``nexus-0tld``). RDR document at ``docs/rdr/rdr-104-incremental-catalog-projection-rebuild.md``; gate result PASSED on round 3 (0 Critical, 3 Significant addressed in-place). Original motivating bug ``nexus-rr0u`` closed.

## [4.24.4] - 2026-05-05

Patch release. Closes a latent silent-corruption hazard in the catalog consistency-marker write surfaced by the RDR-104 critic.

### Fixed

- **Consistency marker is now written inside the catalog rebuild transaction.** Pre-fix, ``Catalog._write_consistency_marker`` ran its own ``self._db.commit()`` after the rebuild's ``with self._db.transaction():`` block had already closed, making the marker write a separate atomic unit from the projection writes. Under the *current* call ordering (projection-then-marker) the failure direction is benign — projection commits first, marker second, crash between them re-rebuilds idempotently on the next run. But the *inverse* ordering (marker first, projection rolls back) would silently corrupt the projection by skipping events on the next run. One refactor away. The fix moves the marker write inside the rebuild transaction so the two writes commit atomically; the ordering hazard cannot exist regardless of caller code rearrangement.

  ``CatalogDB.rebuild`` gained ``consistency_mtime: float | None = None`` keyword. When supplied, the marker write is the last statement inside the same ``with self._lock, self._conn, bulk_load_documents()`` block as the projection writes. ``Catalog._ensure_consistent`` was updated for both paths (event-sourced calls ``_write_consistency_marker`` from inside its existing ``transaction()`` block; legacy passes ``consistency_mtime=current_mtime`` to ``rebuild()``).

  Regression test in ``tests/test_catalog_consistency_marker.py``: patches both ``CatalogDB.rebuild`` and ``Projector.apply_all`` to raise mid-transaction, asserts ``Catalog.degraded is True`` AND the persisted marker did NOT advance. Pins the invariant "rebuild raise = marker stays put" regardless of which path the seeded fixture exercises.

### Background

This is the atomicity-only piece of the broader incremental-projection-rebuild work tracked under bead nexus-rr0u and RDR-104. Shipping it as a standalone patch lets RDR-104 land on a clean atomicity baseline.

## [4.24.3] - 2026-05-05

Patch release. Closes the per-file ChromaDB roundtrip blowups that surfaced once 4.24.2 made the catalog rebuild fast enough to expose them, and adds a richer summary line + crash-safe finally on the catalog-rebuild heartbeat.

### Fixed

- **Catalog rebuild summary line is now informative.** ``Catalog._ensure_consistent`` previously printed a bare ``Catalog: rebuilding projection done (Ns)`` after a slow rebuild — operators had no signal of cause or scale. The summary now reports which canonical-truth file's mtime triggered the rebuild, the number of events replayed (event-sourced path) or the JSONL row counts loaded (legacy path), the resulting projection size (docs, links), and elapsed:

    ```
    Catalog: rebuild triggered by events.jsonl — replayed 441,917 events
      → 23,190 docs, 22,238 links in 3.4s
    ```

  Sub-second rebuilds (the common case on a healthy projection post-FTS5 fix) emit nothing — the gate keeps CLI commands that incidentally trigger a rebuild from scribbling progress over their stdout.

- **Catalog-rebuild heartbeat finally no longer swallows exceptions.** A bare ``return`` inside a generator-contextmanager ``finally`` block in 4.24.2's heartbeat helper discarded any in-flight exception. ``CatalogDB.rebuild`` raising ``RuntimeError`` was being silently dropped, leaving ``Catalog.degraded`` un-set. Replaced with an ``if/else`` gate so the finally falls off the end and exception propagation is preserved (the dedicated regression ``TestEnsureConsistentDegradedFlag::test_degraded_true_on_rebuild_failure`` caught it).

### Performance

- **Misclassified-chunk prune is now batched.** ``_prune_misclassified`` previously did one ``col.get(where={"doc_id": <id>})`` per file, twice (prose+pdf against the code collection, code against the docs collection). On a repo with thousands of files (ART has ~4,800) that was ~9,600 sequential ChromaDB Cloud roundtrips at 50–200 ms each — 8–30 minutes of pure latency where the actual work (chunks to delete) was almost always zero. Replaced with batched ``where={"doc_id": {"$in": batch}}`` capped at ``_CHROMA_PAGE_SIZE`` (300 ids/batch). Round-trips: ``ceil(N / 300)`` per direction — ~34 total instead of ~9,600. Roughly 280× reduction. Legacy chunks predating the doc_id backfill still fall through to per-path ``where={"source_path": …}``. Adds a tqdm progress bar with running chunk count so the phase is visible.

- **Pre-built per-collection staleness cache.** ``check_staleness`` previously did one ``col.get(where={"doc_id": <id>}, limit=1)`` per file before any indexing work fired, just to confirm "yes, current, skip." On a healthy repo (most files unchanged) every one of those calls returned "current" — pure waste. New ``StalenessCache`` and ``build_staleness_cache(col)`` in ``nexus.indexer_utils``. The orchestrator builds one cache per collection (``code_col``, ``docs_col``) AFTER catalog registration so freshly-registered doc_ids are visible to the sweep, then passes them down through ``IndexContext.staleness_cache``. Per-file ``check_staleness`` becomes an O(1) dict lookup instead of a network roundtrip. Round-trips: ``ceil(total_chunks / 300)`` per collection — independent of the file count being indexed. For an "all current, skip" run on ART, the staleness phase went from 8–30 minutes of network latency to ~1 second.

  Ghost-chunk healing preserved: ``by_doc_id`` cache miss when the caller has a non-empty doc_id returns False (stale → re-index → new chunk carries doc_id metadata). Same end-state as the per-file path's ``if doc_id and not stored.get("doc_id"): return False`` branch.

### API stability

- ``check_staleness(cache=None)`` is the back-compat default — direct callers that have not migrated stay on the per-file Chroma path with full ``_chroma_with_retry`` semantics.
- ``IndexContext.staleness_cache`` defaults to ``None``.

## [4.24.2] - 2026-05-05

Patch release. Two compounding catalog-rebuild problems surfaced during ART repo indexing once the 4.24.1 ``ignorePatterns`` fix took effect: the rebuild was both slow and silent.

### Fixed

- **Catalog projection rebuild now uses the FTS5 bulk-load idiom.** ``Catalog._ensure_consistent`` and ``CatalogDB.rebuild`` both replayed every document through ``INSERT INTO documents`` with the ``documents_ai`` FTS5 trigger active. SQLite's FTS5 cannot merge index entries incrementally during a transaction; per-row trigger inserts queue every term/column in an in-memory hash and merge into on-disk segments at COMMIT. On a project with hundreds of thousands of events the merge alone took 15-20 minutes of CPU on ``fts5IndexCrisismerge`` / ``fts5HashEntrySort`` with a 38+ MB WAL pending the entire time. ART's catalog (435,275 events in events.jsonl, 233 MB) hit this every time the indexer ran.

  New ``CatalogDB.bulk_load_documents`` context manager drops the ``documents_ai`` / ``documents_au`` / ``documents_ad`` triggers, lets the caller perform mass writes, then recreates the triggers and runs FTS5's documented bulk-load idiom (``INSERT INTO documents_fts(documents_fts) VALUES('rebuild')``) which materializes the index in source order from the content table — far cheaper than per-row hash queue plus commit-time merge. Wired into both rebuild paths.

### Added

- **Heartbeat on long catalog rebuilds.** ``Catalog._ensure_consistent`` and ``CatalogDB.rebuild`` previously emitted nothing during the rebuild — operators saw ``Catalog: housekeeping…`` from the indexer hook and then total silence for tens of minutes, indistinguishable from a hang. New ``_rebuild_heartbeat`` helper writes ``Catalog: rebuilding projection (Ns)`` to stderr every 5 s after a 5-second warmup. Operations that finish in <5 s stay completely silent; long ones produce visible elapsed-time signal.

### Operator note

- This release does NOT eliminate the rebuild from firing on every ``Catalog()`` construction when any canonical-truth file is newer than the persisted ``_last_consistency_mtime`` marker. On a hot project, a single new event still re-replays the entire event log — but the FTS5 fence makes that replay much faster, and the heartbeat tells you it is running. The deeper architectural fix (incremental projection against ``last_applied_event_id``) is filed as ``nexus-rr0u`` for the next arc.
- If your catalog has hundreds of thousands of events and you have been seeing ``nx index repo`` go silent for >5 minutes after the per-file Catalog progress messages, this is the release that explains it. Restart any in-progress indexing after upgrading.

## [4.24.1] - 2026-05-05

Patch release. Fixes a silent no-op in ``.nexus.yml`` ``server.ignorePatterns`` matching.

### Fixed

- **``server.ignorePatterns`` now honours path-style globs.** The schema documents path-style patterns like ``docs/papers/**`` and that's the form used in our own examples, but the matcher was iterating over individual path components and feeding each one to ``fnmatch.fnmatch`` against every pattern. ``fnmatch`` treats ``/`` as a literal character; a slash-containing pattern could not match any single-component string, so any rule of the form ``a/b/**`` was a silent no-op. Only single-segment patterns (``papers``, ``*.lock``, ``__pycache__``) actually excluded anything.

  This bit ART. ``ART/.nexus.yml`` shipped ``- docs/papers/**`` for months with a comment block referencing the 10,264-duplicate-chunk cleanup the rule was meant to prevent; the rule never excluded anything. ``nx index repo`` walked the 79 papers every time and re-ingested them into ``docs__ART`` alongside the dedicated ``docs__art-grossberg-papers`` collection.

  The matcher now distinguishes pattern shapes: path-style (contains ``/``) routes through a path-aware component walker where ``*`` does not cross ``/`` and ``**`` matches zero or more components; part-style (no ``/``) keeps the original ``fnmatch``-per-component behaviour, so existing ``_DEFAULT_IGNORE`` patterns and per-repo configs that used single-segment patterns continue working unchanged.

### Operator note

After upgrading, repos whose ``.nexus.yml`` carried a path-style ignore pattern will see those exclusions take effect on the next ``nx index repo`` run. If you previously worked around the bug by using a single-segment pattern (e.g. ``papers`` instead of ``docs/papers/**``), it still works — both forms are honoured. If you indexed against the old, broken matcher and want to drop the now-ignored chunks from T3, the regular maintenance path is the same as any other "files were indexed that shouldn't have been": delete the affected collections and re-index.

## [4.24.0] - 2026-05-05

Minor release. Promotes math-aware PDF extraction from optional to default, replaces silent formula loss with a loud failure, and cuts CI runtime by ~50% per push.

### Changed

- **`mineru[all]` is now a default dependency** (nexus-2fyb). Was previously an optional extra. Adds ~500 MB of Python deps (PyTorch CPU, transformers, OCR libs) to the wheel; first PDF extraction downloads ~2-3 GB of models. The trade-off: math-paper indexing is now correct by default. Users who do not want it can still pass `--extractor docling` to opt out per-PDF.
- **`PDFExtractor.extract(extractor="auto")` on a formula-bearing PDF now raises `RuntimeError` instead of silently returning Docling output** (nexus-2fyb). Pre-fix, every install without the `mineru` extra silently received formula-stripped Docling output stamped with `formula_count=0`. The error message includes the formula count, the reinstall command, and the explicit opt-out flag. `extractor="docling"` and `extractor="mineru"` paths are unchanged.

### Added

- **`nx doctor --check-mineru`** surfaces install + server state for the math extractor before users hit it via `nx index pdf`. Reports import status, `do_parse` reachability, and the configured `mineru-api` server endpoint when present.
- **Test fixtures `tests/fixtures/distributed-bloom-filter.pdf`, `bft-to-smr.pdf`, `tc-sql.pdf` are now committed.** The first is the canonical witness for the formula-preservation regression guard; the other two are referenced by the release-sandbox shakedown step 3a/3b. Whitelist additions to `.gitignore` keep developer-local PDFs ignored.
- **Release-sandbox shakedown step 3b probes the MinerU path** with `bft-to-smr.pdf`. Previously the shakedown only ran Docling against `tc-sql.pdf`, so a MinerU regression would not surface until production indexing.

### Fixed

- **DEVONthink batch indexing no longer aborts on a single failed PDF** (`commands/dt.py`, code-review R4-I2). Catches `RuntimeError` / `ImportError` per record so one math PDF that hits the new fail-loud path does not kill the rest of the smart-group run. Failures are listed at the end of the batch summary.
- **`pipeline_stages.extractor_loop` signals `extraction_done` in `finally`** (code-review C-int-1). Prevents `chunker_loop` from blocking on `extraction_done.wait(timeout=0.5)` when extraction raises.
- **Pipeline failure clears orphan WAL rows** (`pipeline_buffer.clear_orphan_wal`, code-review C-int-2). Prevents the `failed → resuming → re-fail` loop on deterministic failures (math PDF without MinerU). The pipeline row stays `failed` for audit; the orphan `pdf_pages` / `pdf_chunks` rows are dropped so the next run extracts from scratch.
- **Formula counter restructured** (`pdf_extractor._count_formula_markers`, code-review C1). Pre-fix the regex alternation undercounted: each `$$..$$` block consumed whole, and `\frac` instances inside were never separately counted. Now block-style and command-token patterns are counted independently. The fixture-regression guard `_EXPECTED_REGEX_MARKERS` was relocked from 4 to 16 (4 blocks + 12 `\frac` commands) to reflect the corrected count.
- **URL credentials redacted from error messages** (code-review R5-I2). If a user configures `pdf.mineru_server_url` with embedded `user:pass@host`, those credentials no longer leak into chained exception messages or structured logs.
- **`scripts/reinstall-tool.sh` passes the receipt path via env var** rather than shell-interpolating it into a `python -c` heredoc (code-review R5-I1). Removes a Python-injection vector if the receipt path ever contained a quote.
- **`nx index pdf` surfaces extraction `RuntimeError` as `ClickException`** (`commands/index.py`, code-review R4-I1). Users see an actionable message instead of a raw Python traceback.
- **`pdf_extractor._progress` routed through structlog** (code-review R1-I3). The interactive stderr write is preserved (gated by `NEXUS_PDF_PROGRESS_QUIET=1`) but the event also goes to the structured-logging chain, so library and MCP-server callers no longer lose progress in their log capture.

### CI

- **`tests/test_indexer_e2e.py` and `tests/test_taxonomy_e2e.py` now carry module-level `@pytest.mark.integration`.** Both files are textbook integration suites (real ChromaDB, real local embeddings, real CLI subprocesses); they were running on every PR push despite `pyproject.toml addopts` already deselecting `integration`.
- **Three pagination-boundary tests marked `@pytest.mark.slow`**: `test_t3.py::test_existing_ids_pagination_respects_300_cap`, `test_exporter.py::TestPagination::test_export_pagination`, `test_exporter.py::TestPagination::test_import_pagination`. Each seeds >300 records to cross `QUOTAS._PAGE`; the boundary they guard does not drift between releases.
- **Result: CI pytest 14m42s → ~8m, total job 17m → ~9m.** Coverage preserved by `tests/e2e/release-sandbox.sh shakedown` running every release tag.
- **Six type-system theatre tests deleted** (PR #511). They asserted `isinstance(_, frozenset/set/list/T2Database/T3Database)` against values whose type annotation already enforced exactly that. A test that would still pass if the production type became the ground truth is theatre by definition.

## [4.23.3] - 2026-05-05

Patch release. Fixes a misplaced stamp gate that wasted operator embedding budget.

### Fixed

- **`pipeline_version` now stamps on every successful `nx index repo` run, not only on `--force`** (`nexus-7yfm`). The stamp asserts "these embeddings were produced by `PIPELINE_VERSION` code." That is true regardless of whether `--force` was used — `--force` only bypasses the per-file staleness check (`code_indexer.py:329`, `indexer.py:1200`); both paths run the same chunker and embedder. The pre-fix gate (`if force:` at `indexer.py:1995`) meant incremental runs that wrote v4 embeddings produced unstamped collections, which `nx doctor` then nagged about. The suggested remediation forced operators to re-pay for full Voyage re-embedding (potentially thousands of files × API calls) to repair a state that should never have existed. Doctor's "no version stamp" message remediation now reads `(next 'nx index repo' will stamp)` instead of `(index with --force to stamp)`.

### Operator note

After upgrading, an unstamped collection from any prior nexus version will be stamped on its next `nx index repo` run — incremental, no `--force` required. Collections with stale stamps (`v<old>` < current `PIPELINE_VERSION`) still need `--force-stale` because the embeddings genuinely need to be regenerated; the new behaviour applies only to "no stamp" cases.

## [4.23.2] - 2026-05-04

Patch release. Cleans up a misleading `nx doctor` warning.

### Fixed

- **`nx doctor` no longer reports `pipeline_version` warnings against `taxonomy__*` collections** (`nexus-l6mz`). The taxonomy centroid collection (RDR-070) is a BERTopic c-TF-IDF aggregate computed over existing chunk embeddings, not an indexer output. `PIPELINE_VERSION` semantics (voyage-* + CCE prefixes + RDR-028 language registry) do not describe it, and the indexer's stamp loop only touches `code__` / `docs__` / `rdr__` (`src/nexus/indexer.py:1995-2009`) — so the suggested remediation `nx index repo --force` would never have stamped it. Doctor now skips any collection whose name starts with `taxonomy__`.

## [4.23.1] - 2026-05-04

Patch release. Fixes a release-day operator-visible regression introduced by 4.23.0's release-gate steering operators directly into a SQLite lock contention.

### Fixed

- **`nx catalog backfill-collections --no-dry-run` and other CLI write-side catalog ops fail with `database is locked` while `nx-mcp` is running** (`nexus-wehp`). The new RDR-103 release gate (`nx catalog doctor --collections-drift`) tells operators to run `nx catalog backfill-collections` to register legacy 2-segment collections in the projection. Every v4.23.0 user with pre-RDR-103 collections (everyone) hit this on first upgrade. Root cause: `Catalog.__init__`'s `_ensure_consistent` triggered a heavy `DELETE FROM links` + replay rebuild on every CLI invocation because `_last_consistency_mtime` was per-instance and reset to 0.0 each construction; the rewrite contended with the `nx-mcp`-held SQLite connection. Fix: persist the marker to `.last_consistency_mtime` in the catalog directory; new processes read it and skip the rebuild when no canonical-source file has been written past the recorded mtime. Cross-process safe; failures fall back to pre-fix behaviour.

### Operator workaround for v4.23.0 (no upgrade required)

Stop Claude Code (or kill `nx-mcp` + `nx-mcp-catalog`), run write-side catalog verbs (e.g. `nx catalog backfill-collections --no-dry-run`), then `/reload-plugins` to bring MCP back. Upgrading to 4.23.1 eliminates the workaround.

## [4.23.0] - 2026-05-04

Minor release. Headline: **RDR-101 closed end-to-end (Phases 4-6 + irreversible flip + cleanup)** and **RDR-103 closed** (Catalog as Collection-Name Authority). The catalog is now the sole authority for collection names, the conformant 4-segment shape `<content_type>__<owner_id>__<embedding_model>__v<n>` is the only collection name reachable from new writes, and the five transitional migration verbs are retired. The chunk-metadata schema is reduced (`source_path`, `corpus`, `store_type`, `git_meta` all dropped); chunks carry `doc_id` as the canonical identity field. Six new operator verbs (catalog `doctor --collections-drift`, `rename-collection`, `supersede-collection`, `backfill-collections`, `migrate-fallback`, `nx t3 gc`) ship as the post-cleanup operator surface. Plus follow-up bug fixes surfaced in sandbox shakedown, the chromadb httpx-timeout fix, and a thorough post-arc doc/code cleanup sweep.

### Changed (potentially breaking)

- **RDR-101 Phase 5b: IRREVERSIBLE: `[catalog].event_sourced` default flipped to `true`** (`nexus-o6aa.12`). New chunks are written WITHOUT the deprecated metadata fields (`corpus`, `store_type`, `git_meta`) by default. Readers route through the catalog `doc_id` instead; Phase 4 reader migration completed (`_display_path` resolution, `doc_id`-keyed dispatch), so dropping these fields on the write side is safe. Escape hatch: `NEXUS_CATALOG_EVENT_SOURCED=0` env var still falls back to the legacy direct-write path at runtime.
- **RDR-101 Phase 5c: chunk-metadata schema reduction** (`nexus-o6aa.13`, PR #80071934). Removes `corpus`, `store_type`, and `git_meta` from `ALLOWED_TOP_LEVEL`. `title` is intentionally KEPT (load-bearing for `find_ids_by_title` + the MCP `store_get` title-fallback path). The schema goes from 31 keys to 28 keys.
- **RDR-103 Phase 5: IRREVERSIBLE: strict collection-naming default-on** (`nexus-yqnr.7`). `T3Database.get_or_create_collection` now requires conformant 4-segment names by default. The opt-in `strict_collection_naming` flag is gone; the single guard at `src/nexus/db/t3.py:547` is the only enforcement site. Operators who type the short legacy form (`knowledge__topic`) at the `--collection` boundary get auto-promoted by `t3_collection_name`; pre-existing legacy 2-segment collections remain readable.
- **Auto-migration on first index** (`nexus-yqnr.6`). The first `nx index repo` per content_type after upgrade detects legacy collections (both pre-RDR-101 2-segment and pre-strict 4-segment path-derived) and renames them to the conformant tuple-derived shape via `Catalog.rename_collection`. Idempotent: re-running after migration emits zero migration lines.
- **Five transitional catalog verbs retired** (`nexus-iftc`, PRs #496+#497). `nx catalog migrate`, `synthesize-log`, `t3-backfill-doc-id`, `repair-orphan-chunks`, and `prune-deprecated-keys` are gone. Their function (one-shot migration scaffolding) is complete after Phase 5b; the operator playbook for any remaining edge case is "delete the catalog directory and re-run `nx catalog setup`" for legacy catalogs and "re-index" for orphan recovery on collections written under post-Phase-4 contracts. Net `commands/catalog.py` reduction: ~6,100 LOC.

### Added

- **RDR-101 Phase 5a: `[catalog].event_sourced` opt-in flag** (`nexus-o6aa.11`). Predecessor to the Phase 5b irreversible flip; landed first to give operators a soak window.
- **`nx catalog doctor --collections-drift`** (`nexus-o6aa.14`). Release-gate: enforces the projection ⊇ T3 ⊇ documents.physical_collection invariant. Wired into `tests/e2e/release-sandbox.sh` step 11; any drift is a deterministic failure rather than an audit finding.
- **`nx catalog rename-collection`** (`nexus-o6aa.14`). Atomic 1:1 T3-then-catalog rename with rollback. Chunks become orphans for `nx t3 gc` to sweep; operator re-indexes the target. (No background re-embed; that's a possible follow-up.)
- **`nx catalog supersede-collection`** (`nexus-o6aa.14`). Marks a collection as superseded by another (e.g. when an embedding model changes); routes new writes to the successor.
- **`nx catalog backfill-collections`** (`nexus-o6aa.14`). One-shot projection backfill from existing T3 + documents state. Defaults to `--dry-run`; `--no-dry-run` actually writes. Filters `taxonomy__*` via the same `_BYPASS_SCHEMA_PREFIXES` set the drift check uses.
- **`nx catalog migrate-fallback`** (`nexus-o6aa.14`). Operator-driven fallback for collections that the auto-migration on first index cannot resolve.
- **`nx t3 gc`** (`nexus-r5eo`). T3 garbage collector: sweeps chunks left orphaned by catalog operations (rename, supersede, document deletion) once they exit the orphan window.
- **RDR-103 conformant collection-name shape** (`nexus-yqnr.1` through `nexus-yqnr.6`). `CollectionName` tuple type + `Catalog.collection_for_repo` convenience + indexer + plugin-layer rewrites + Phase 4 auto-migration. The catalog is the sole authority for collection names; the indexer asks rather than constructs.
- **RDR-103 OpenQ Q2 resolution** (`nexus-yqnr.9`, PR #493). Per-collection upgrade messages remain (no `--quiet` flag). The migration loop iterates over `("code", "docs", "rdr")`, so a single `nx index repo` invocation emits at most 3 `Upgraded legacy collection` lines, then 0 thereafter.

### Fixed

- **chromadb httpx timeout=None hang** (`nexus-jgjw`, commit `83daa8b4`). chromadb >=1.5 hardcodes `httpx.Client(timeout=None, ...)`; observed during 2026-05-03 orphan recovery as a 10+ minute hang at 91% on a 63K-chunk collection (CPU=0%, one TCP socket in `CLOSE_WAIT`). `T3Database.__init__` now overrides via `_apply_chroma_http_timeout(client)` to `httpx.Timeout(connect=10, read=120, write=60, pool=10)`. Stalled reads now raise `httpx.ReadTimeout` (already classified retryable) so the existing retry helper converts the hang into a bounded retry-then-fail loop. Defensive on shape: skips PersistentClient/EphemeralClient that have no `_server._session`.
- **`owners.UNIQUE(name)` schema bug: split rdr collections** (`nexus-7vuw`, PR #494). The single-column UNIQUE on `owners.name` produced split rdr collections via INSERT OR REPLACE silently obliterating the repo owner when a Phase-4 path-derived synthetic owner registered under the same name. Fixed by composite `UNIQUE(name, owner_type)` with an in-place migration that detects the legacy single-column index and rebuilds the table.
- **t3-aware grandfathering for legacy `--collection` input** (`nexus-hmxi`, PR #495). `nx store list --collection knowledge__delos` auto-promoted 2-segment input to conformant; `nx search --corpus knowledge__delos` used the input as-is. Operators got split read/write views of legacy collections. Fixed by threading `t3=` explicitly through MCP/CLI surfaces so `t3_collection_name` can grandfather only collections that actually exist in T3.
- **`_migration_source_candidates` enumerates BOTH legacy shapes** (`nexus-7vuw`). The Phase 4 migration originally only detected 2-segment legacy names. Sandbox shakedown surfaced that pre-strict runs had also produced path-derived 4-segment names; both shapes now enumerate per `(repo, content_type)` and the existing `Catalog.rename_collection` handles whichever exists.
- **RDR-102 Phase B: `source_path` retired from chunk schema** (`nexus-ejs4`). Hard-removed from `make_chunk_metadata` signature in lockstep with 7 writer call sites. `_PRUNE_DEPRECATED_KEYS ∩ ALLOWED_TOP_LEVEL == ∅` enforced by unit test.
- **RDR-102 Phase A: doc_indexer family pre-flight catalog registration** (`nexus-uusi`). PDF + markdown indexers register catalog Documents before chunk-write so chunks land in T3 with `doc_id` at write time, not via a backfill verb.
- **RDR-102 Phase C: orphan-ratio surface in doctor** (`nexus-2nls`). Per-collection + global `orphan_ratio` in JSON; new "Orphan ratio" text section emits WARN > 50%.
- **`_prune_deleted_files` doc_id-keyed** (Phase B critical regression). Previously read `meta.get("source_path", "")` and would silently no-op for post-Phase-B repos.
- **Deleted-file cleanup + curator-filter coverage gaps** (PRs #487, #488). `taxonomy__centroids` correctly skipped in `chunk_text_hash` backfill; doc_indexer + bare-name catalog/store owner lookups filter by curator type.

### Documentation

- **RDR-101 closed** (`nexus-o6aa`, PR #498). Post-mortem at `docs/rdr/post-mortem/101-event-sourced-catalog-migration.md`. 14/14 child beads complete; phases 0-6 shipped.
- **RDR-103 closed** (`nexus-yqnr`, PR #499). Post-mortem at `docs/rdr/post-mortem/103-catalog-collection-name-authority.md`. 9/9 child beads complete; single-day arc on top of RDR-101's irreversibility.
- **Post-RDR-101/103 doc/code drift cleanup** (PRs #500-504, 5 passes):
  - PR #500: 4 surfaces (cli-reference, repo-indexing, metadata-consistency-matrix, catalog source messages).
  - PR #501: 8 surfaces (top-level AGENTS.md identity column, catalog AGENTS.md invariants, source comments + docstrings + ReadFail/ClickException strings).
  - PR #502: 2 dead validation scripts (`scripts/validate/rdr-101-migration-e2e*.sh`).
  - PR #503: 2 operator-playbook docs deleted (`docs/migration/rdr-101.md`, `rdr-101-phase4-orphan-recovery.md`); `docs/migration/README.md` flags surviving 5 audit artifacts as historical record; 2 source-side broken-link fixes (`_migration_prompt.py`, `commands/catalog.py`).
  - PR #504: `tests/test_abstract_themes_plan_integration.py` refactored to drop hardcoded `dominant_themes` fixtures; coverage gate now corpus-grounded against runtime BERTopic labels (top-K by doc_count). Author-curated theme lists moved to `bench/queries/abstract-themes.yml` as evaluation harness. Closes `nexus-igzg`.
- **`docs/migration/README.md`** (new). Flags the directory as historical forensic record paralleling `docs/rdr/post-mortem/`.
- Zero "Run nx catalog [retired-verb]" guidance remains anywhere in `src/`.

## [4.22.0] - 2026-05-03

Minor release. Headline: **RDR-101 — Event-sourced catalog with immutable document identity**, landed across Phases 0 → 4 (~70 PRs). The catalog is now backed by an append-only `events.jsonl` event log; SQLite is a derived projection; chunks carry `doc_id` (UUID7-stamped at chunk-write time) as the canonical identity field; the legacy `source_path` keying is on the deprecation glide path. Plus a bug-fix tail covering recovery-time hangs, owner-name collision silent failures, and catalog UX progress visibility.

This release rolls up untagged 4.21.3 + 4.21.4 work (the version bumps in main never made it to a tag) into a single 4.22.0 cut.

### RDR-101 Phase 0 — Foundation (PRs #404–#413)

- **Event log infrastructure** (#414, `events.py` + `event_log.py`). Defines the canonical event types (`OwnerRegistered`, `DocumentRegistered`, `DocumentDeleted`, `LinkCreated`, `LinkDeleted`, `ChunkIndexed`, `DocumentAliased`, `OwnerDeleted`) and the append-only writer with fcntl-flocked atomic appends.
- **Projector + JSONL synthesizer + replay-equality test** (#416). Projector replays events into SQLite; synthesizer rebuilds `events.jsonl` from legacy JSONL. The replay-equality test asserts `live_db == projected_from_events` for every catalog state — the load-bearing invariant of event-sourcing.
- **`nx catalog doctor --replay-equality`** (#417). Operator surface for the replay-equality invariant.
- **Frecency projection + Document bib columns** (#419). New T2 table `frecency_projection` decouples frecency scoring from the catalog row; bib-enrichment columns split out of `documents.metadata` JSON into typed columns.
- **chash_index column rename** (#418). `chash_index.doc_id` → `chunk_chroma_id` to free `doc_id` for its real role; resolves naming collision noted in `docs/rdr/post-mortem/rdr-101-rdr086-collision.md`.
- **T2Database self-recording fix** (#413). `_upgrade_done` now records its own `path_key`, fixing version-tracking on parallel-database setups (nexus-avwe).
- **Phase 0 deliverables** — field-by-field disposition audit (#405), bib_semantic_scholar_id migration plan (#406), chash_index doc_id naming collision (#407), chunk_id generation rule (#408), downstream caller survey (#409), direct T3 metadata access survey (#410), nexus-3e4s post-mortem (#411), index (#412).

### RDR-101 Phase 2 — Synthesis + backfill verbs (PRs #421–#426)

- **`nx catalog synthesize-log`** (#421, #423). Synthesizes `events.jsonl` from legacy JSONL state and (with `--chunks`) extends to T3 chunk synthesis with `ChunkIndexed` events. The `--prefer-live-catalog` flag (added in #480 family below) prefers the live catalog's `doc_id` over the chunk metadata's `doc_id` when reconciling.
- **`nx catalog t3-backfill-doc-id`** (#424). Walks T3 chunks lacking `doc_id`, looks up their owning Document via the event log, and writes `doc_id` into the chunk metadata. Idempotent.
- **`nx catalog doctor --t3-doc-id-coverage`** (#425). Operator-runnable per-collection orphan/coverage report.
- **`nx catalog repair-orphan-chunks`** (#426). Manual identity assignment for chunks the synthesize-log path couldn't recover — for cases where the source has been deleted or the catalog Document never existed.

### RDR-101 Phase 1 — Shadow-emit + opt-in event sourcing (PRs #414, #420)

- **Shadow-emit events alongside JSONL writes (gated)** (#420). `NEXUS_EVENT_SOURCED=1` enables side-by-side emission so events.jsonl can be validated against the legacy JSONL canonical state before the canonical flip in Phase 3.

### RDR-101 Phase 3 — Event-sourced read/write paths (PRs #427–#457)

- **Event-sourced register path (gated, opt-in)** (#430). With `NEXUS_EVENT_SOURCED=1`, `Catalog.register` writes through the projector instead of directly to JSONL+SQLite.
- **Event-sourced update / delete / set_alias / rename_collection** (#431). Mutating ops also funnel through the projector under the gate.
- **Round-3 + 4 correctness fixes** (#432, #433). link/unlink event-source, atomic rebuild, bootstrap guard, doctor, alias, projector cache. Failure-mode coverage (mtime races, owner crash window, alias_of threading, doctor schema).
- **Phase 3 PR γ — link/unlink merge semantics deep-clean** (#434). Replay-equality hardening for the relational case.
- **Phase 3 PR δ — schema gate + chunk-write doc_id wiring** (#438–#445). Stage A: schema gate forbids chunk writes without `doc_id`. Stages B.1–B.6: wire `prose_indexer` (#439), `code_indexer` (#441), PDF path + register (#442), store put (#443, #444), MCP `store_put` (#444), doctor end-to-end coverage (#445).
- **Phase 3 PR ε — lint gate forbidding direct catalog writes outside projector module** (#446). Prevents architectural regression: only `nexus.catalog.projector` may write to the SQLite catalog DB.
- **Phase 3 — dedupe through projector** (#447). Emits `LinkDeleted` / `DocumentDeleted` / `OwnerDeleted` from the dedupe path under the gate.
- **Phase 3 PR ζ — flip `NEXUS_EVENT_SOURCED` default to ON** (#448). Irreversibility window opens — the canonical state of new catalogs is the event log.
- **Atomic dedupe** (#449). flock + batched events + transactional projection.
- **Bootstrap guardrail floor + operator-visible signal** (#453). Prevents silent corruption when running event-sourced reads against a sparse / mid-migration catalog state.
- **Phase 3 cleanup — lint gate hardening + cascade + ES coverage gaps** (#454).
- **Phase 3 sandbox e2e migration validation harness** (#455). Operator-runnable migration smoke against a temporary catalog + T3.
- **`nx catalog migrate` + TTY upgrade prompt + migration guide** (#456). One-verb operator path through the full Phase 0 → 4 migration sequence.
- **Phase 3 extended e2e — scaled soak + partial-failure recovery** (#457).

### RDR-101 Phase 4 ramp — UX + resilience (PRs #458–#487)

- **Clean migration UX — sync live SQLite + import vector-only collections** (#458, nexus-o6aa.9.14, .9.15).
- **t3-backfill per-chunk retry + deferred-class quota differentiation** (#459, nexus-o6aa.9.18). On batch-update failure falls back to per-chunk retry; over-cap chunks land in a separate `chunks_deferred` class so they don't poison the success metric.
- **Per-verb progress output** (#460, nexus-o6aa.9.17). `nx catalog migrate`, `synthesize-log`, `t3-backfill-doc-id` now emit per-collection progress to stderr — required for cloud T3 ops that take tens of minutes.
- **t3-backfill batch-bisect O(log N) recovery** (#461, nexus-o6aa.9.19). On a persistent batch-quota failure the verb halves and recurses, isolating failing chunks in O(log N) update calls instead of O(N) per-chunk retry.
- **Catalog `Catalog.update()` None-coercion fix** (#463, nexus-ga48). `cat.update(doc, fields=None)` no longer crashes — coerces to `{}`.
- **MCP smoke test under bootstrap-fallback** (#465, nexus-o6aa.9.13). MCP server stays serviceable when the catalog is in the bootstrap-fallback state.
- **T3 import — bypass canonical schema for taxonomy collections** (#464, nexus-o6aa.9.16). Taxonomy collections carry centroid embeddings without underlying chunk text — they don't fit the canonical chunk-metadata schema and now bypass it.
- **doc_id-keyed dispatch / lookups / safety checks across the codebase**:
  - Aspects: queue + hook + 9 fire sites (#478, nexus-tdgc); chroma reader dispatch (#471, nexus-o6aa.10.1); transitional fallback when `doc_id` query empty (#476).
  - Search: catalog prefilter narrows on `doc_id` not `source_path` (#467, nexus-ufyl); catalog-resolved `_display_path` for formatters (#472, nexus-1qed); link boost + display_path priority (#473).
  - Indexer: doc_id-keyed frecency-only update (#479, nexus-f4z9); doc_id-aware reindex safety check (#477, nexus-7b5n).
  - Document indexer: doc_id-keyed chunk lookups in PDF/MD indexing (#470, nexus-dcym PR-B).
  - T3: doc_id-keyed chunk lookups for incremental sync (#469, nexus-dcym PR-A).
  - Catalog: doc_id-aware walks for import-from-t3 + auto-link (#474, nexus-7b5n).
- **`nx catalog prune-deprecated-keys`** (#480, nexus-o6aa.10.3). Operator verb that strips the 5 legacy chunk-metadata keys (`source_path`, `git_branch`, `git_commit_hash`, `git_project_name`, `git_remote_url`) — the post-Phase-4 reader-migration cleanup.
- **Show progress on prune verb's coverage gate + dry run** (#481).
- **Fold Phase 4 into `nx catalog migrate` + doctor next-verb hint** (#482). One-verb path now includes the Phase 4 prune; doctor's failure messages name the next verb to run.
- **Catalog hook + staleness escape route for ghost chunks** (#484). Indexer can re-stamp chunks whose source file moved without producing ghost catalog entries.
- **`nx catalog migrate` runs Phase 4 finisher when Phase 3 already done** (#483). Idempotent forward-progress.
- **Indexer progress UX**: cumulative chunks + skipped count on progress bar (#485, nexus-6xqk); ETA ticker stops when post-pass phases begin (#486).
- **Catalog process cache + read-side helper migration** (#487). `Catalog.open_cached(path)` returns a process-shared instance to prevent per-call SQLite write-lock storms on bursty operations.

### Fixed (cherry-pick tail — independent of the schema arc)

- **Catalog owner lookups now filter by `owner_type='curator'`** (commits `45758118`, `bbc46ed0`). Repo and curator owners can share names (e.g. `scheme-evolution-research` exists as a REPO owner from `nx index repo` and as a target for `nx index pdf --corpus scheme-evolution-research`). The bare `WHERE name = ?` lookup used by `_lookup_existing_doc_id`, `_catalog_markdown_hook`, and the `_catalog_pdf_hook` in pipeline_stages picked up whichever was registered first — typically the repo owner. When the file then lived outside that repo's tree (e.g. a DEVONthink-sourced PDF), `Catalog.register`'s cross-project guard raised `ValueError`, the lookup caught broadly and returned `""`, and chunks orphaned silently. All 5 sites now filter by `owner_type='curator'`. Discovered during the RDR-102 post-merge live shakeout.
- **`nx collection backfill-hash` skips `taxonomy__centroids`** (commit `035f30b0`, supersedes #488). Centroid embeddings have no underlying text — `chunk_text_hash` is computed from `documents`, which is empty for synthetic centroids. Pre-fix the backfill walked the centroids collection, computed empty hashes, and emitted misleading "0 backfilled" lines that obscured actual progress on the real-content collections.
- **`tests/e2e/sandbox.sh` and `release-sandbox.sh` no longer hang on heredocs** (commit `4ed26272`). Bash here-docs blocked indefinitely in non-interactive shell contexts (Claude Code harness, some CI runners) where parent stdin was wired to a pipe the here-doc machinery never closed. Symptom: scripts returned `rc=124` (timeout) with a 0-byte target file. Replaced both here-docs with `printf` chains. `release-sandbox.sh smoke --skip-install` now completes cleanly in ~30s.
- **Chroma cloud operations now bound by per-request timeout (nexus-jgjw)** (commit `83daa8b4`). chromadb >=1.5 hardcodes `httpx.Client(timeout=None, ...)` at `chromadb/api/fastapi.py:86,91` — Chroma ops block indefinitely on any read where the server has closed the connection. Observed during the 2026-05-03 orphan recovery: `nx catalog t3-backfill-doc-id` hung for 10+ minutes at 91% on a 63K-chunk collection (CPU=0%, one TCP socket in `CLOSE_WAIT`, no recovery without SIGTERM). `T3Database.__init__` now overrides the timeout via `_apply_chroma_http_timeout(client)` to `httpx.Timeout(connect=10, read=120, write=60, pool=10)`. After the override, a stalled read raises `httpx.ReadTimeout` — already classified retryable by `_is_retryable_chroma_error` — so the existing retry helper converts the hang into a bounded retry-then-fail loop. Defensive on shape: skips PersistentClient/EphemeralClient that have no `_server._session`. Removable once chromadb exposes a settings knob for httpx timeout (track upstream).

### Other

- **t3 import path creates taxonomy collections with cosine, not L2** (#468, nexus-18wz). Taxonomy similarity uses cosine; L2 was a writer-side bug.
- **Skip integration tests by default; dodge bash 5.3 heredoc deadlock in subagent hook** (#440).
- **Test coverage stub** (#415).
- **Documentation** — fire_store_chains consumer audit (#475, nexus-buv0); RDR-101 live-migration post-mortem (#462); Phase 4 reader audit (#466).

### Holding on develop (RDR-102 Phase 4 closeout)

The schema-changing tail of RDR-102 Phase 4 stays on `integration/rdr-102-phase4` + `develop` until RDR-101 Phase 5 catches up:

- Phase A — pre-flight catalog registration writes `doc_id` at chunk-write time
- Phase B — drop `source_path` from `ALLOWED_TOP_LEVEL` (cleans up 5 deprecated keys for new chunks)
- Phase C — doctor surfaces orphan ratio with WARN threshold
- Phase D — operator-runnable e2e gate
- Synthesizer title-prefix orphan recovery (nexus-olhr)
- Greenfield acceptance pytest + shakedown step (Phase B-coupled)

Phase 5a opt-in flag (`nexus-o6aa.11`) → 5b default flip (`nexus-o6aa.12`) → 5c final schema removal (`nexus-o6aa.13`) → Phase 6 enforcement (`nexus-o6aa.14`). Bundled with Phase 4 closeout in a future release.

## [4.21.2] - 2026-04-30

Hotfix release. Refines the v4.21.1 title-validation heuristic after live shakeout against `knowledge__delos` showed pure Jaccard over-rejected legitimate matches with short filename-derived source titles (continuation of nexus-yy1m).

### Fixed

- **OpenAlex title validation accepts short-source matches** (PR #403). v4.21.1's Jaccard-with-threshold rule rejected `Pbeegees` (1 substantive token) against the genuine OpenAlex hit `pBeeGees: A Prudent Approach to Certificate-Decoupled BFT Consensus` (6 tokens; Jaccard 1/6 = 0.167 < 0.20) and `Hex Bloom` against `HEX-BLOOM: An Efficient Method for Authenticity ...` for the same reason. Both are legitimate matches the operator wanted. The threshold rule is replaced with an asymmetric one: 2+ substantive token matches accept (multi-token coincidence is rare); exactly 1 match accepts only when the smaller token set has at most 2 tokens (one side is essentially the intersection); 0 matches reject. Live shakeout on `knowledge__delos` (15 docs) went from 3 correct enrichments under 4.21.1 to 6 correct under 4.21.2, with 1 known false positive (`Zanzibar` single-word source matched a 2007 medical study via the place-name token, filed as `nexus-5cez` for richer year-sanity / content-keyword validation in a follow-up).

## [4.21.1] - 2026-04-30

Hotfix release. Closes the citation-DOI poisoning class surfaced in the v4.21.0 live shakeout (nexus-yy1m).

### Fixed

- **OpenAlex bib enrichment now validates returned title against the source title** (PR #402, nexus-yy1m). The DOI / arXiv-aware lookup added in 4.21.0 trusted whatever identifier showed up in the document body, but academic papers have full reference lists with DOIs that belong to OTHER papers. v4.21.0 shakeout caught this on a CacheRAG preprint: a citation DOI (`10.1145/3742872`) was extracted from the references section, looked up against OpenAlex, and stamped a foreign embedded-systems proceedings paper's metadata across all 174 chunks. The OpenAlex `/works?search=` title-search path had the same failure mode (returns SOMETHING for almost every query, ranked by relevance, so an irrelevant paper wins when the real one is not indexed). Both lookup paths now post-validate the returned `display_name` against the source title via Jaccard token similarity (threshold 0.20 over substantive 4+-character non-stopword tokens). Low-similarity matches return `{}` and emit a structured `openalex_title_mismatch_rejected` / `openalex_title_search_rejected` warning so the operator can audit. The Semantic Scholar backend is unaffected by the citation-DOI path (does not do direct-by-DOI lookup) and unchanged here. New helper `nexus.bib_enricher_openalex._titles_compatible(source, returned)` is exposed for callers that want to apply the same gate elsewhere.

## [4.21.0] - 2026-04-30

Minor release. Bib enrichment gains an OpenAlex backend with DOI / arXiv-aware lookup; aspects acquire first-class read verbs; the PDF indexer surfaces silent zero-chunk failures with actionable error messages instead of reporting success on zero records.

### Added

- **OpenAlex bib backend** (PR #390, nexus-tv22). New `nx enrich bib --source openalex` (alongside `--source semantic-scholar`) with `enrich_by_doi` and `enrich_by_arxiv_id` direct-lookup paths. arXiv IDs use the canonical `10.48550/arXiv.<id>` DOI form for the OpenAlex `/works/doi:` endpoint. Companion changes: `link_generator.py` indexes both `bib_semantic_scholar_id` and `bib_openalex_id` so cross-source citation links resolve regardless of which backend enriched the row.
- **DOI / arXiv-aware bib lookup** (PR #394, PR #395, PR #396, nexus-liir). `bib_extractor.py` extracts DOIs (with labeled-form preference) and arXiv IDs (with version-suffix disambiguation) from chunk body text across all chunks of a document, not just the first 5. Lookup tries DOI / arXiv direct hit first, falls back to fuzzy title only on miss. The "labeled preference" rule prefers `DOI: 10.x/y` over a bare `10.x/y` match elsewhere in the chunk to avoid cross-citation contamination.
- **`nx enrich aspects-show <TUMBLER>`** and **`nx enrich aspects-list <COLLECTION>`** (PR #398, nexus-bkvk). First-class read interface for the structured aspects extracted by `nx enrich aspects`. `aspects-show` resolves a tumbler (or title) via the catalog, looks up the aspect row by `(physical_collection, file_path)`, and renders all fields (`problem_formulation`, `proposed_method`, `experimental_datasets`, `experimental_baselines`, `experimental_results`, `extras`, `confidence`). `aspects-list` is the collection-level companion (preview / audit shape) with `--missing` to invert into gap detection. `--json` and `--field <name>` for scripting and projection. Pre-this verb the only way to inspect aspects was raw SQL against `~/.config/nexus/memory.db`.
- **`nx dt index` defaults PDFs to `knowledge__<collection>`** (PR #397, nexus-cvaw). DEVONthink-sourced PDFs now route to `knowledge__*` collections by default, matching the convention that external reference PDFs land in `knowledge__` and not `docs__`. Override with `--collection docs__<name>` if you specifically want the docs prefix. Markdown records still default to `docs__`.
- **`nx catalog backfill --from-t3` per-file recovery** (PR #388, nexus-p03z Issue 2). Per-file targeted recovery path for catalog rows whose chroma side is healthy but whose catalog entry is missing or inconsistent. Reads the source-of-truth metadata from T3 and re-registers the catalog row idempotently.
- **One-off ART migration script** (PR #391, `scripts/migrate_art_papers.py`). 78-PDF migration from `docs__ART` to `knowledge__art-papers` using `cat.update(tumbler, physical_collection=...)` so the move is atomic across T3 chunks, T2 aspects, and the catalog. Documented as the canonical pattern for future cross-collection migrations.

### Fixed

- **Silent zero-chunk PDF indexing now raises with actionable error** (PR #400, nexus-aold). A 71MB PDF (DEVONthink 4.2.2 user manual) caused `nx index pdf --extractor docling` to exit silently with 0 chunks indexed, with a multiprocessing leaked-semaphore warning at process shutdown as the only signal. Three load-bearing guards were added: `_extract_normalized` (PyMuPDF fallback) raises on empty text, `_pdf_chunks` (batch path) raises when extracted text is non-empty but the chunker returned zero chunks, and `chunker_loop` (streaming path, the actual hit path since `_STREAMING_THRESHOLD=0`) raises on the same mismatch. All errors include actionable mitigation: rerun with `--extractor mineru` or file a bug with the source PDF. The CLI surfaces these via the existing `try/except` in `commands/index.py:616`.
- **`nx catalog update --source-uri` emits clean error on unknown scheme** (PR #399, nexus-fb6x). Previously the command leaked a `ValueError` traceback when given a URI whose scheme was not in the allow-list. Now wraps the validation in a `try/except ValueError` and prints a one-line diagnostic.
- **`nx enrich aspects` catalog hook matches by `source_path`** (PR #392, nexus-tv22). Pre-fix the hook used a `LIMIT 1` fallback against the catalog's first row, which mis-attributed enrichment results when multiple aspect rows shared a tumbler. Now matches strictly by `(physical_collection, source_path)` so future enrich runs propagate cleanly without manual remediation.
- **`nx enrich aspects` chroma lookup uses absolute path** (PR #389). Aspect extraction for `docs__*` collections was failing because the chroma lookup used a relative path while the indexed `source_path` was absolute. The lookup now derives an absolute path from `source_uri` so the lookup succeeds.
- **`nx catalog backfill` skips `None` documents** (PR #386). Pre-fix the command crashed with an unhandled `AttributeError` when the catalog held a `None` document. Now skips with a structured warning.
- **`docs__*` collections re-excluded from aspect extractor `_REGISTRY`** (PR #393, revert of #377). The pre-#377 behavior is restored: `_REGISTRY` aliases `knowledge__` only. The `docs__` aspect rows produced under #377 were inconsistent with the structured-aspect contract, so the v4.19.2 expansion is rolled back. RDR-089 will revisit `docs__` aspect extraction with a separate config.

### Documentation

- **RDR-100 (Plan-Cache Improvements from CacheRAG)** drafted, researched, and closed as `disposition: deferred` (PR #401). Empirical findings (T2: `nexus_rdr/100-research-1`) showed none of the four CacheRAG-inspired phases solve a problem the system is currently hitting at the live plan library's scale. Closure note documents revisit triggers (200 plans, 500 unredacted runs, 300 per-bucket). The narrow operator-error case that surfaced during research is filed separately as bead `nexus-l0yh`.

## [4.20.0] - 2026-04-29

Minor release. Headline: closes the cross-project `source_uri` contamination class that produced ~6,500 mis-attributed catalog rows in the wild (nexus-3e4s), shipped alongside an audit-membership sweep tool and DEVONthink in-app install scripts.

The contamination root cause was `_normalize_source_uri()` resolving relative `file_path` values against the process CWD instead of the owner's `repo_root`, so any `nx index <repo>` invoked from a foreign CWD wrote `source_uri` rows pointing at the foreign tree but attributed to the indexed repo's owner. The release-sandbox shakeout flow was the production trigger. The fix anchors relative paths on the owner's `repo_root`; a register-time guard catches anything that still slips through, including the disaster-recovery backfill path. `Catalog.update()` runs the same guard on every call (not just when `source_uri` is in fields), so the catalog hook's hot-path re-index calls now exercise the guard too. `nx catalog audit-membership --all-collections` provides a single-shot post-release health check; the sweep is owner-aware so single-home wrong-home collections cannot silently pass as "clean".

### Added

- **`nx dt install-scripts`** (PR #380). Installs the bundled in-DEVONthink toolbar / menu AppleScript wrappers for `nx dt index` (selection, current group, knowledge selection) under `~/Library/Application Scripts/com.devon-technologies.think/Menu/`. Idempotent; skips files that already exist unless `--force` is passed.
- **`nx catalog audit-membership <COLLECTION>`** (PR #381, nexus-ow9f). Detects cross-project `source_uri` contamination in a single physical_collection by grouping entries by their source_uri "home" (the first 4 path segments for `file://` URIs, `<scheme>://<netloc>` otherwise). Per-home counts surface multi-root collections; `--canonical-home SUBSTR` overrides the dominant-home heuristic when contamination outnumbers legitimate entries; `--purge-non-canonical` deletes the offending rows after `--dry-run` review and a confirmation prompt (suppressible with `--yes`). `--json` for structured output.
- **`nx catalog audit-membership --all-collections`** (PR #383, nexus-3e4s Phase 3). Sweep mode: runs the audit across every physical_collection in a single pass and emits one summary report. Sorted contaminated-first. Read-only by design; `--purge-non-canonical` and `--canonical-home` are per-collection contexts and raise `UsageError` when combined with `--all-collections`. Use as a daily / post-release health check.
- **`NEXUS_CATALOG_ALLOW_CROSS_PROJECT=1` env var** (PR #382, nexus-3e4s). Emergency-only escape hatch that bypasses the new register-time cross-project guard. Use only for known-good recovery scripts that legitimately need to register rows across project boundaries.

### Fixed

- **Catalog `update()` now runs the cross-project guard on every call** (nexus-3e4s critique-followup C1). Pre-fix the guard was gated on the caller passing `source_uri` explicitly. The production hot path (the catalog hook in `indexer.py`) calls `update()` with `head_hash`, `physical_collection`, `meta`, and `source_mtime` but no `source_uri`, so the guard was effectively unreachable from the most-traveled code path. Now the guard runs on every `update()` against the carried-through source_uri, so any in-place row whose URI drifted out of the owner's tree cannot be silently re-anointed. The env override `NEXUS_CATALOG_ALLOW_CROSS_PROJECT=1` still bypasses for emergency recovery scripts that need to touch contaminated rows.
- **`nx catalog audit-membership --all-collections` now detects single-home wrong-home collections** (nexus-3e4s critique-followup C2). Pre-fix the sweep used dominant-home as its only signal, so a collection where every row pointed at the wrong project (single home, no internal disagreement) silently passed as "clean". This was the failure mode that masked ~4,200 wrong-home rows in `code__ART-...`. The sweep now cross-references each collection's owning `repo` owner; a single-home collection whose home does not match the owner's `repo_root` is flagged as 100% contaminated with a `wrong_home: true` field (JSON) or `[wrong-home]` tag (text). Curator-owned collections still skip the owner check (they legitimately span sources).
- **`nx catalog backfill` for `rdr__*` collections now uses the repo owner** (nexus-3e4s critique-followup S1). Pre-fix `_backfill_rdrs` unconditionally created a curator owner for every `rdr__*` collection, which made the register-time guard skip (curator owners legitimately span sources). The disaster-recovery path could therefore re-introduce the contamination class it was supposed to clean up. Now backfill looks up the registered repo whose hash matches the collection suffix and registers RDR rows under that repo owner; curator is the legitimate fallback only when no matching repo is found. Repo lookup failures now log a warning instead of being silently swallowed.
- **`Catalog._owner_repo_root` defensively returns an absolute path** (nexus-3e4s critique-followup S4). A pre-RDR-060 owner row with a relative `repo_root` would otherwise let `os.path.abspath` inside `_normalize_source_uri` fall back to CWD-anchoring, silently re-introducing the bug. The lookup now applies `os.path.abspath` before returning so the relative-storage edge case cannot defeat the fix.

- **Catalog `register()` no longer writes cross-project `source_uri` rows** (PR #382, nexus-3e4s). `_normalize_source_uri()` was deriving `source_uri` from a relative `file_path` via `os.path.abspath()`, which resolves against the process CWD rather than the owner's `repo_root`. The catalog hook always passes a relative `file_path`, so any `nx index` run from a CWD outside the indexed repo wrote `source_uri` rows pointing into CWD's tree but attributed to the indexed repo's owner. That was the contamination signature for ~6,500 rows in the live catalog. Fix: `_normalize_source_uri(repo_root=...)` anchors relative paths on `owner.repo_root` when provided. Companion register-time guard `Catalog._check_source_uri_in_repo_root` raises `ValueError` on any `file://` URI that still resolves outside the owner's `repo_root`, so the bug class cannot recur even if a future code path bypasses the normalization. Owner-type-aware (curator and pre-RDR-060 owners with empty `repo_root` skip), scheme-aware (`chroma://`, `https://`, `x-devonthink-item://` pass through). `update()` runs the same guard so it cannot back-door register.

## [4.19.2] - 2026-04-29

Patch release bundling six findings from a post-v4.19.1 audit (PR #378) plus the headline #377 fix. The audit ran two parallel deep-analyzer agents (one over the RDR-099 / `nx dt` surface, one over prefix-keyed config registries) and surfaced two critical bugs, three significant issues, and one feature gap.

### Added

- **`nx enrich aspects` now supports `docs__*` collections** (Closes #377). `docs__*` collections produced by `nx index repo` (markdown / ADR / design-doc holders) hold the same kind of substantive prose as `knowledge__*` but were silently excluded from the aspect-extraction registry. `_REGISTRY` now aliases `docs__` to the same `scholarly-paper-v1` config, so `problem_formulation`, `proposed_method`, `experimental_datasets`, `experimental_baselines`, and `experimental_results` extraction applies uniformly to both prefixes. Companion edits: error message in `nx enrich aspects` lists `docs__*` in supported prefixes; stale "Phase 1 = `knowledge__*` only" docstring in `aspect_worker.py` updated to current registry state.
- **`nx catalog update --source-uri` flag**. `Catalog.update()` already accepted `source_uri` via `**fields` but the CLI exposed no flag. Adds the recovery path for entries whose DT-URI stamp failed during `nx dt index` (the entry would carry `source_uri=file://…` instead of `x-devonthink-item://<UUID>`). URI is validated against the same scheme allowlist as register-time.

### Fixed

- **`nx dt index` now forwards `--collection` for `.md` files**. `_index_record` passed `collection_name=collection` to `index_pdf` but called `index_markdown` without it, silently dropping the operator's `--collection` flag for every Markdown record. PDFs landed in the requested collection; Markdowns landed in `docs__default` regardless of intent. New `test_index_record_md_forwards_collection` exercises the real `_index_record` body to lock the parity.
- **`nx dt open <tumbler>` checks platform before catalog I/O**. UUID-form gated correctly but the tumbler-form ran catalog resolution before checking `sys.platform`, leaking catalog errors (`tumbler not found`, `Catalog not initialized`) instead of the documented `macOS-only` diagnostic on non-darwin. Hoisted `_is_darwin()` to the top of `open_cmd`.
- **`nx dt index` summary surfaces stamp failures**. `_stamp_dt_uri_on_entry` failures were logged-and-swallowed; the summary still reported `Indexed N record(s) (M skipped).` as if every entry got the DT identity. `_index_record` and `_stamp_dt_uri_on_entry` now return `bool`; the summary adds `<N> DT-URI stamp-failed` plus a recovery hint pointing at `nx catalog update --source-uri`.

### Documentation

- **`docs/devonthink-smart-rules.md` drift**: removed the last reference to the nonexistent `nx catalog list --source-uri-prefix` flag (PR #374 fixed the same drift in `tests/e2e/devonthink-manual.md` but missed this second file). Replaced with the canonical `nx catalog list --json | jq …` form.
- **`docs/devonthink-smart-rules.md` install-path table**: `/usr/local/bin/nx` is the wrong default for Apple Silicon Homebrew (`/opt/homebrew/bin/nx`) and `uv tool install` (`~/.local/bin/nx`). Replaced the bare "replace this" note with a four-row table covering common install methods plus `which nx` instructions.

## [4.19.1] - 2026-04-29

Two bug fixes caught during v4.19.0 post-release live shakeout. RDR-099 AC-1's central round-trip promise (`nx dt index` ↔ `nx dt open`) was silently broken; the doctor taxonomy check was hanging past 30s on real-size catalogs.

### Fixed

- **`nx dt index` now stamps the DT identity on the catalog entry** (PR #376, RDR-099 AC-1). Indexer-registered entries previously carried `source_uri = file://...` (the resolved local path) and empty `meta`. After the indexer call, `_stamp_dt_uri_on_entry` now updates the entry to `source_uri = x-devonthink-item://<UUID>` and `meta.devonthink_uri = x-devonthink-item://<UUID>`, restoring the round-trip via `nx dt open <tumbler>` and making the entry stable across DT relocations inside `Files.noindex/`. Stamp failures are logged and swallowed: a miss leaves a recoverable `file://` entry rather than aborting the whole batch. Verified end-to-end on a live DT installation: indexed PRDTs PDF (UUID 5321AD83), catalog now reports `URI: x-devonthink-item://5321AD83-...`, and `nx dt open 1.2163.1` opens the record in DT.
- **`nx doctor --check-taxonomy` no longer hangs on real-size catalogs** (PR #375). The drift-detection query used `NOT EXISTS` with an `OR` clause (`tl.from_topic_id = ta.topic_id OR tl.to_topic_id = ta.topic_id`) that defeated SQLite's index planner: each outer row triggered a covering scan of `topic_links`. On one production database (526k `topic_assignments` × 13k `topic_links`) that's 6.93 BILLION row comparisons, timing out past 30s with no output. Restructured as `NOT IN (SELECT from_topic_id UNION SELECT to_topic_id)` so each half uses the primary-key index and the union materialises into a hash set. Same catalog: 30s+ → 0.42s (>71x improvement), drift output unchanged. All 6 `TestDoctorCheckTaxonomy` tests still pass.

### Documentation

- `tests/e2e/devonthink-manual.md`: replaced four `nx catalog list --source-uri-prefix x-devonthink-item://` examples with `nx catalog list --json | jq ...` (PR #374). The flag never existed; the JSON-pipe form is the canonical query path.

## [4.19.0] - 2026-04-29

Feature release. RDR-099 ships first-class DEVONthink integration on macOS: operators can now ingest DT records into Nexus by selection, tag, group, smart group, or UUID, and round-trip catalog entries back to DT. Cross-platform CI is unaffected; the integration is gated to `sys.platform == "darwin"` with friendly error messages elsewhere.

### Added

- **`nx dt` Click command group** (RDR-099, PR #363). Two subcommands cover the v1 surface:
  - `nx dt index`: ingest DT records into Nexus. Mutually-exclusive selectors `--selection`, `--tag <name>`, `--group <path>`, `--smart-group <name>`, and `--uuid <UUID>` (repeatable for batch). Per-record dispatch by extension routes `.pdf` to `nx index pdf` and `.md` to `nx index md`; other extensions are skipped with a structured WARN. Passthrough `--database` (default: every open library, with UUID dedupe), `--collection`, `--corpus`, and `--dry-run`.
  - `nx dt open <tumbler|UUID>`: round-trip a catalog entry back to DT via `open(1)`. UUID-shaped arguments build the URI directly (no catalog hit); tumblers resolve through the catalog, preferring `meta.devonthink_uri` and falling back to `source_uri`.
- **`src/nexus/devonthink.py`**: selector helpers exposing 5 sdef-canonical AppleScript surfaces (`selected records`, `lookup records with tags`, `parents whose record type is smart group`, `search predicates` PLURAL + `search group` + `exclude subgroups`) over a centralised `_run_osascript` spawn. `DTNotAvailableError` translates DT's `Application isn't running` into an operator-friendly message; non-darwin invocations refuse with a clear `macOS-only` error rather than silently no-op.
- **`docs/devonthink-smart-rules.md`**: operator recipe for DT smart rules + macOS folder actions calling `nx dt index --uuid`. Covers AppleScript stanza, save location (`~/Library/Application Scripts/com.devon-technologies.think/Smart Rules/`), error-handling pattern, and the concurrency caveat for bulk imports.
- **`tests/e2e/devonthink-manual.md`**: fixture-creation runbook + per-AC manual smoke for the live-DT path (one-off setup, then `nx dt index --selection|--tag|--group|--smart-group|--uuid` + `nx dt open` repros).
- **`tests/test_devonthink_live.py`**: gated live-DT integration suite (`sys.platform == "darwin"` + `NEXUS_DT_LIVE=1`) verifying multi-database tag invariant, recursive group walk, and smart-group `search group` scope preservation against real DT state.
- Substrate from 4.17.0 is reused: `x-devonthink-item://` URI scheme (registered in `_KNOWN_URI_SCHEMES`), `meta.devonthink_uri` reverse-lookup, and the single-UUID resolver in `aspect_readers._devonthink_resolver_default`.

### Fixed

- **`tests/test_catalog_prune_stale.py` fixtures collided with real RDR-099** (RDR-099 P-Review). The fixtures used `rdr-099-*` as a deliberately-fake slug for missing-file entries; once RDR-099 shipped as a real RDR file, `nx catalog prune-stale`'s CWD-walk for same-prefix replacements found the real file and skipped the test entry instead of pruning. Fixtures renamed to `rdr-999-test-*` to remove the collision. (The underlying default-`--source-dir` behaviour is a separate concern; this fix takes the smallest-scope route.)

## [4.18.2] - 2026-04-29

Data-loss fix. Closes #367. Plus two CI test-isolation fixes that surfaced during the PR's verification.

### Fixed

- **`nx collection reindex` no longer silently destroys store_put-only collections** (PR #368, Closes #367). The previous logic refused with a `--force` gate when SOME entries were sourceless and some were source-backed, but when EVERY entry was sourceless `--force` took the user past the check and the command collapsed to a destructive delete with nothing to re-index from. The reporter lost 28 entries across three knowledge collections plus a `taxonomy__centroids` collection while migrating embedding models. The fix detects the all-sourceless case before any delete and refuses unconditionally; `--force` does NOT bypass. The error points users at `nx collection delete` for the explicit-delete path. In-place re-embedding (preserve content, swap embedding model) is the user's underlying need and is tracked as nexus-bw65 — out of scope for this hotfix; the goal here is "don't destroy data when the user types `reindex` on a file-less collection."
- **CI test-isolation fixes** (PR #368). Two latent fragilities surfaced during the verification CI run:
  - `tests/test_console_health_aspect_queue.py::test_aspect_queue_card_dash_when_table_absent` relied on filesystem-isolation that broke when the `/health/refresh` pipeline pre-created the T2 db at `NEXUS_CONFIG_DIR/memory.db` (Ubuntu CI side-effect; macOS local didn't trip it). Now mocks `_collect_aspect_queue_data` directly so the template-branch assertion is independent of T2 side-effects.
  - `tests/conftest.py::_isolate_t1_sessions` autouse fixture's `monkeypatch.setattr("nexus.db.t1.SESSIONS_DIR", ...)` failed on Python 3.13 with `AttributeError: module 'nexus.db' has no attribute 't1'` when no prior test had imported `nexus.db.t1`. Python 3.13 tightened package-attribute lazy access; `getattr(nexus.db, 't1')` no longer auto-loads the submodule the way 3.12 did. Force-importing both `nexus.db.t1` and `nexus.hooks` at the top of the fixture body removes the test-ordering luck. Same class of strictness as the multiprocessing fork-with-threads flake fixed in v4.18.1.

## [4.18.1] - 2026-04-29

Internal hardening release. No new user-visible features. Fixes the multiprocessing flake that hung the v4.18.0 release job, removes a per-tool-call observability hook, tightens a Bash hook timeout footgun, and adds a workflow timeout ceiling.

### Fixed

- **Python 3.13 multiprocessing fork-with-threads flake destochasticised** (PR #365). `tests/test_mcp_concurrency.py` now pins `multiprocessing.get_context("spawn")` for every `Process` and `Manager`. `fork` was unsafe in this test's parent because pytest had imported chromadb + nexus.db.{t1,t3}, all of which spin up background threads; forking a multi-threaded parent can copy held locks into the child in an acquired state and deadlock the child. This is the root cause that hung Python 3.13's pytest run for 36+ min during the v4.18.0 tag-push, blocking PyPI publish until manual cancellation. `spawn` always creates a fresh interpreter so no parent state crosses the process boundary. Also avoids the 3.13 `resource_tracker` shutdown waitpid hang (cpython#146313).
- **`nx hook session-start` no longer hangs on TTY stdin** — see v4.18.0 notes (this entry is for completeness; the fix shipped in 4.18.0).

### Changed

- **`nx/hooks/hooks.json` PreToolUse Bash timeout dropped from 300s to 5s** (PR #364). The advisory `pre_close_verification_hook.sh` is fast by construction (read stdin, JSON out, exit 0; <100 ms on the slow path). The 300s ceiling was a footgun: a future bug or filesystem stall would block every Bash tool call for five minutes with no operator visibility. 5s matches the SessionStart fast-path hooks and is comfortably above the actual budget. Two test pins (`tests/test_upgrade_e2e.py::TestSC8HooksJson::test_pretooluse_bash_timeout_is_short` + `tests/hooks/test_verification_integration.py::TestHooksJsonStructure::test_hooks_json_pretooluse_timeout`) enforce a `<=10 s` ceiling so any future drift toward "minutes" trips CI.
- **Release workflow gets `timeout-minutes: 15`** (PR #365), matching `ci.yml`. Defense-in-depth: a future hang fails fast instead of stalling the publish step indefinitely. Hit during the v4.18.0 tag-push when pytest 3.13 ran 36 min before manual cancel.

### Removed

- **`hook_telemetry` PostToolUse hook + `nx doctor --check-hooks` flag** (PR #366). The hook fired on every tool call, paid ~100-300 ms Python cold-start, and the data was consumed only by an ad-hoc `nx doctor --check-hooks` inspection that was never gated on or read by code. Pure observability tax. Removed: `nx/hooks/scripts/hook_telemetry.py`, the empty-matcher `PostToolUse` block in `nx/hooks/hooks.json`, the `--check-hooks` flag and `_run_check_hooks` impl in `src/nexus/commands/doctor.py`, the `hook_telemetry` table from `_TELEMETRY_SCHEMA_SQL`, the `log_hook_event` / `query_slow_hooks` / `trim_hook_telemetry` methods on `Telemetry`, the `migrate_hook_telemetry` function and its MIGRATIONS entry (count drops 29 → 28), `tests/test_hook_telemetry.py`, and `--check-hooks` references in `tests/e2e/release-sandbox.{sh,md}`. Net diff: 13 insertions / 565 deletions. Existing installs that already migrated through 4.14.0 keep their `hook_telemetry` table as a dormant artifact (nothing reads or writes it; a future cleanup migration could `DROP TABLE` explicitly, but the table is tiny and not worth a migration entry just for that).

## [4.18.0] - 2026-04-29

Three new builtin plan templates (RDR-097 hybrid factual lookup + companion, RDR-098 abstract-themes), CLI parity hardening for T3-write hook chains, observability across `nx doctor` and the console UI, plan-authoring affordances, and the RDR-090 retrieval-bench scaffold. Closes RDR-089, RDR-093, RDR-097, RDR-098.

### Added

- **`hybrid-factual-lookup` plan template** (`verb=lookup, strategy=hybrid-factual-lookup`, RDR-097, `nexus-vej3`). Five-step shape: vector search over-fetch, graph traversal on factual-evidence purpose, per-stream `limit_per_source` budgets, operator_rank merge, operator_generate. Routes narrow-target factual queries where graph context disambiguates. Integration harness ships 5 fixtures over `knowledge__hybridrag` (RDR-097 P1.5).
- **`traverse-then-generate` plan template** (`verb=lookup, strategy=traverse-then-generate`, RDR-097, `nexus-ddm0`). Explicit-seeds companion: walk `factual-evidence` from caller-supplied tumblers, hydrate, generate. Use when seed tumblers are inputs (not derived from a question).
- **`abstract-themes` plan template** (`verb=query, strategy=abstract-themes`, RDR-098, `nexus-ldnp`). CheapRAG community-summary pipeline: broad over-fetch (`mode: broad`), groupby by BERTopic centroid label, per-group aggregate, coalesce summarize. Routes "main themes / overview / summary of findings" question shapes. v1 is single-collection abstract QA; cross-collection routes through RDR-075 projection layer (deferred). Integration harness covers 10 fixtures over `docs__art-grossberg-papers` and `knowledge__delos` plus 3 match-text hygiene fixtures (RDR-098 P1.4 + P1.5, `nexus-17yg`).
- **`nx plan disable` / `nx plan enable`** subcommands (`nexus-mrzp`). Soft-disable a plan without deleting it: drops out of `plan_match` but preserves the row id, telemetry counters, and T1 cache embedding. Useful for triaging mis-routing without committing to a delete + reseed cycle. Backed by a new `plans.disabled_at` column (migration #29).
- **`nx plan list / show / delete / reseed`** admin CLI. Day-2 plan-library maintenance counterparts to `nx plan repair`.
- **`nx doctor --check-post-store-hooks`** (`nexus-b0ka`). Enumerates every hook the MCP runtime has registered against the document-grain and batch-grain post-store chains in fire order. Surfaces the side-effect surface that a `store_put` triggers without grepping `mcp_infra.py`.
- **`nx doctor --check-aspect-queue`** (`nexus-1pfq`). Reports `aspect_extraction_queue` row count, per-status breakdown (pending / processing / failed / completed), oldest non-completed `enqueued_at` as a lag indicator, and top failed rows with `last_error`. Pre-RDR-089 databases report cleanly as "table not present" rather than erroring.
- **`nx catalog link-density --by-collection`** (RDR-097 P1.4, `nexus-8el5`). Per-collection report of outgoing-link counts at the depth-N BFS frontier (default depth 2). Output: `frontier_p50`, `frontier_p90`, and `link_types` present per collection. Observability for hybrid plan rollout: collections with median frontier `<3` are poor candidates and the operator should fall back to a vector-only plan.
- **`nx catalog prune-stale`** + **`nx catalog remediate-paths --rdr-prefix-mode`** (`nexus-zg4c`). Catalog hygiene CLIs.
- **`nx t3 prune-stale`** subcommand (RDR-090 P1.4). T3 staleness sweep counterpart to the catalog command.
- **Console `Aspect Queue` card on `/health`** (`nexus-qf48`). Polls `aspect_extraction_queue` total + per-status breakdown on the existing console refresh interval. Mirrors `nx doctor --check-aspect-queue`; warn-paints on failed rows or absent T2.
- **`mode: broad` plan-step affordance** (`nexus-h3e2`). Disables the per-corpus default search threshold (e.g., 0.65 for prose) for broad / abstract phrasings; the runner translates to threshold=2.0 on the search step. Drop-in for plan templates that do over-fetch and rely on downstream re-ranking.
- **`limit_per_source` kwarg on `store_get_many`** (RDR-097 P1.0, `nexus-uwkw`). Per-stream input cap, symmetrical with the existing list-shaped `ids` / `collections` contract. Hard prerequisite for RDR-097's per-stream token-budget mechanism.
- **`factual-evidence` purpose alias** (RDR-097 P1.3, `nexus-wp9s`). Bundles `cites`, `implements`, `relates` link types under one stable name in `nx/plans/purposes.yml`.
- **RDR-090 retrieval-bench harness scaffold** (`nexus-q5yt`). 5-query × 3-path benchmark spike infrastructure under `bench/` and `scripts/bench/`: query and result schemas, runner, metrics module. Phase 2 query authoring + Phase 3 weekly CI workflow are tracked separately.
- **AST drift guard** for T3-write CLI parity (`nexus-jgzl`). New test catches a future regression where a CLI ingest path silently drops back to bypassing the post-store hook chain.
- **`force_dynamic` kwarg on `nx_answer`** (RDR-090 P1.1). Skips `plan_match` and forces inline-planner dispatch. Used by the RDR-090 bench harness to compare grown-plan vs inline-planner trajectories.

### Changed

- **`abstract-themes` plan filters `section_type=references`** (`nexus-j5ka`). Drops bibliography clusters from the groupby surface; they cluster together regardless of subject and consume a groupby slot on every query. RDR-055 chunkers populate `section_type`; this filter is a no-op on corpora that pre-date RDR-055 metadata.
- **Scope-fit weighting tightened** in `plan_match` (RDR-090 P1.2). Bare prefix matches no longer admit specific-scoped plans, so `arcaneum/*` doesn't accidentally pick up `arcaneum/web` plans.
- **Cosine floor for grown plans raised** in `plan_match` (RDR-090 P1.3). Reduces false-confident matches from auto-saved plans whose match-text overlaps trivially with a different intent.
- **`nx_answer` auto-aliases the question text** into any unsupplied `required_bindings`. Removes the `$intent`-only constraint for builtin templates; new plans can use semantic binding names (`$concept`, `$topic`, `$query`) without losing dispatch from a bare question.
- **`nx catalog setup` seeds 15 templates** (was 14): abstract-themes added.
- **AGENTS.md restructured** per the Augment Code AGENTS.md guidance (#357). Per-module `AGENTS.md` files now live alongside `src/nexus/catalog/`, `src/nexus/db/`, and `src/nexus/mcp/`. Root `CLAUDE.md` is a symlink to root `AGENTS.md`.

### Fixed

- **T3-write CLI paths now fire post-store hook chains** (`nexus-9099`). `nx store put`, `nx index repo`, `nx index pdf`, `nx index md` previously committed to T3 without invoking the document-grain post-store hooks (taxonomy assignment, link generation, aspect queueing). The MCP path always fired hooks; CLI parity was missing. The fix routes every CLI ingest through the same hook chain, with an AST drift guard test to catch future regressions.
- **`nx hook session-start` no longer hangs on TTY stdin** (`nexus-rv2x`). The hook used to read stdin unconditionally, which blocked on interactive shells where the parent kept the TTY open.
- **MCP plan-cache mtime-guarded refresh on T2 mutation** (`nexus-qgjr`). The `plans__session` cache rebuilt only on session start; re-seeded plan rows were invisible until the MCP server bounced. The fix mirrors `Catalog._last_consistency_mtime`: cache compares its populate-time mtime against the current PlanLibrary file mtime and refreshes when the underlying T2 file advances.
- **Integration flake destochasticised** in `test_search_filter_groupby_aggregate_end_to_end` (`nexus-uf9f` / `nexus-16he`). The `>=2 aggregates` floor was empirically flaky (PASS/FAIL/PASS on identical code) due to LLM stochasticity in the Byzantine-vs-crash partition decision. Relaxed to `>=1` for E2E plumbing; the deterministic regression-catch moved to a new mocked unit test (`test_bundled_pipeline_preserves_all_aggregates`) plus the existing operator-scope unit (`test_returns_aggregates_with_key_value_and_summary`).
- **`memory_search` discover_topics perf gate stabilised** (`nexus-9lzx`).
- **Concurrent T2 writes test** pre-initialises schema to kill a DDL race that surfaced under threaded concurrency.

### Documentation

- **`docs/architecture.md`**: new `### Builtin plan templates` subsection (between `## Module Map` and `## Design Decisions`) enumerates all 15 shipped templates grouped by verb dimension.
- **`docs/cli-reference.md`**: added sections for `nx catalog link-density`, `nx doctor --check-post-store-hooks` / `--check-aspect-queue`, `nx plan disable / enable`.
- **`docs/plan-centric-retrieval.md`**: builtin-template table now includes `abstract-themes`; surrounding paragraph explains the v1 single-collection scope plus the RDR-070 / RDR-075 follow-up path.
- **`docs/rdr/rdr-070-incremental-taxonomy-clustered-search.md`**: new `## Downstream consumers` section establishing that BERTopic centroid labels are RDR-098's community-partition substrate.
- **`docs/rdr/README.md`**: index synced with frontmatter for RDR-089 / 093 / 097 / 098.

### RDRs closed in this release

- **RDR-089** Structured Aspect Extraction at Ingest (closed 2026-04-26)
- **RDR-093** GroupBy and Aggregate Operators (closed)
- **RDR-097** Hybrid Retrieval Plan Template (closed 2026-04-29)
- **RDR-098** Abstract-Question Plan Template (closed 2026-04-29)

## [4.17.0] - 2026-04-28

DEVONthink integration for the catalog: PDFs managed by DEVONthink (DT3 / Pro / Server) carry a stable identity URL that survives DT-internal relocations, and Nexus now treats it as a first-class catalog source.

### Added

- **`x-devonthink-item://<UUID>` source-URI scheme** (`nexus-bqda`). Added to `_KNOWN_URI_SCHEMES` so `nx catalog register --source-uri x-devonthink-item://<UUID>` accepts it. A new reader in `nexus.aspect_readers` resolves the UUID via DEVONthink's AppleScript bridge (osascript → application id `DNtp`), reads the file at the resolved path, and returns `ReadOk` with `metadata.scheme = "x-devonthink-item"`. macOS-only; on other platforms returns `ReadFail("unreachable", "DEVONthink integration is macOS-only")` rather than attempting subprocess.
- **`nx catalog remediate-paths` consults `meta.devonthink_uri`** before basename scanning (`nexus-srck`). DT-managed PDFs live inside `Files.noindex` trees that aren't part of any papers archive, so basename scanning can't find them. When DT reports an existing path, that wins (and the report shows `of which N via DEVONthink`); when DT returns nothing or a stale path, falls through to the legacy basename scan so we never persist a path the resolver lied about.

### Behavior

- Both paths share `_devonthink_resolver_default(uuid) -> (path|None, error_detail)` so production gets one osascript implementation. Tests inject a stub via `dt_resolver` kwarg (reader) or `monkeypatch.setattr` (remediate-paths), so the suite passes on non-macOS CI without invoking osascript.
- `_KNOWN_URI_SCHEMES` lock test updated to include `x-devonthink-item`.

## [4.16.1] - 2026-04-27

Hotfix release for `nx index md` / `nx index pdf` silently returning 0 chunks when Voyage/Chroma credentials were unset (#336). The CLI looked like it succeeded — exit code 0, "Indexed 0 chunk(s)" — while indexing nothing. The fix has two layers:

### Fixed

- **`nx index md` / `nx index pdf` now work without credentials in local mode** (`#338`). When `is_local_mode()` returns True (NX_LOCAL=1, or either `voyage_api_key` / `chroma_api_key` unset), ingestion falls back to the same `LocalEmbeddingFunction` (ONNX MiniLM / fastembed) that `store_put` and local-mode `nx search` already use. This finally honours the claim from `nx doctor`: "T3 mode: local (no API keys needed)" — the doctor was right about reads, but ingestion was lying. Chunk metadata records the local model name (not `voyage-context-3`), so re-indexes against unchanged content are no-ops, and a later upgrade to cloud correctly triggers re-embed.
- **Cloud mode without credentials raises `CredentialsMissingError`** (`#337`). When the user has explicitly opted into cloud mode (`NX_LOCAL=0`) but `voyage_api_key`/`chroma_api_key` are missing, `nx index md` / `nx index pdf` exit non-zero with a 4-line operator-readable message naming the missing key(s) and explaining how to set them or fall back to local mode. Replaces the silent `return 0` that reported "Indexed 0 chunk(s)" with exit code 0 — indistinguishable from a no-op success.

### Behavior matrix

|  | `voyage_api_key` set | `voyage_api_key` unset |
|---|---|---|
| `NX_LOCAL=1` | local fallback | local fallback |
| `NX_LOCAL` unset | cloud (existing) | local fallback |
| `NX_LOCAL=0` | cloud (existing) | `CredentialsMissingError` |

`NX_LOCAL=0` is the operator's explicit commitment to Voyage; honouring it means a credential gap surfaces rather than silently degrading.

### Internal

- New `_make_local_embed_fn` helper in `doc_indexer.py` returns `(embed_fn, model_name)`. The caller overrides `target_model` with the returned `model_name` so the staleness check + chunk metadata are aligned. Defensive embedding normalisation: chromadb's upsert validator accepts `list[list[float]]` or `list[np.ndarray]` but rejects `list[list[np.float32]]`; the helper converts all the way down to native floats regardless of whether TIER0 (ONNXMiniLM_L6_V2) or TIER1 (fastembed) ran underneath.
- Three pre-existing tests that monkeypatched `_has_credentials → True` without env vars now also patch `is_local_mode → False` to preserve their cloud-path test intent.

## [4.15.1] - 2026-04-26

Documentation-only point release. README leads with the new Tensegrity blog posts: Post 0 (*How I actually use Nexus*) as the conceptual overview and Post 00 (*Installing Nexus*) as the install walkthrough, with Post 1 (*Nexus by Example*) following as the practice tour. Header image swapped to the establishing shot for the series.

### Changed

- **README.md**: "New to Nexus?" callout now opens with [Post 0](https://tensegrity.blog/2026/04/26/how-i-actually-use-nexus/) and [Post 00](https://tensegrity.blog/2026/04/26/installing-nexus/); header image is `a-stately-pleasure-dome.png`; Documentation table gains rows for both new posts.

No source, schema, or plugin behaviour changes. Tag exists so the manifests, marketplace, and PyPI carry the documentation update without users needing to navigate to the blog from a stale README.

## [4.15.0] - 2026-04-26

Metadata schema rationalisation arc plus a new catalog repair tool. Three landed PRs (#324, #325, #326) merged in sequence: the chunk-metadata factory + section_type-on-PDFs refactor, the `nx catalog remediate-paths` command for fixing basename and ghost file_paths in production catalogs, and a follow-up cargo cleanup that restored `bib_semantic_scholar_id` to the schema after discovering normalize() had been silently dropping the marker that drives `nx enrich`'s skip-already-enriched logic.

The aspect extractor (RDR-089, shipped in 4.14.2) was broken end-to-end on every paper after release because the section-type filter dropped chunks with empty `section_type`, and PDFs had `section_type` hardcoded to `""` since the original PDF chunker was written. SAGE (arxiv 2604.15583) was the canary: indexed cleanly, extracted nothing. This release fixes the root cause (PDFChunker now detects markdown / numbered academic / bare-word headings and tags every chunk) and the downstream enrich idempotency hole.

### Added

- **`nx catalog remediate-paths <SOURCE_DIR>`** (`src/nexus/commands/catalog.py`). Repairs catalog entries whose `file_path` is a basename or points to a no-longer-existing path. Walks SOURCE_DIR for candidates by basename, builds an index, updates each broken entry to the matching absolute path. Idempotent. Options: `--dry-run`, `--collection`, `--owner`, `--prefer-deepest`, `--mark-missing`, `--extensions`. Workflow: user moves loose ingested papers from `~/Downloads` into a git-backed papers archive, points the command at the archive root, gets every catalog entry's `file_path` repaired in one pass.
- **`make_chunk_metadata` factory** (`src/nexus/metadata_schema.py`). Single chunk-metadata builder every chunked-write indexer routes through: every `ALLOWED_TOP_LEVEL` key gets a documented default; bib_* placeholders drop together when all-empty; git_* short and long key shapes both pack to `git_meta` JSON. Indexers used to build metadata dicts by hand and each accumulated their own subset of fields; new fields had to be added in seven separate places. Now one edit in the factory.
- **PDF section detection** (`src/nexus/pdf_chunker.py`). PDFChunker grows heading detection (markdown / numbered academic / bare-word) and tags every chunk with `section_title` (hierarchical `Outer > Inner` path matching `SemanticMarkdownChunker` convention) and `section_type` via `classify_section_type`. Subsections inherit `section_type` from the dotted-numeral parent (`3.1 Approach` inherits from `3 METHODOLOGY`).
- **Section pattern coverage** (`src/nexus/md_chunker.py`). `SECTION_PATTERNS` recognises evaluation / experiments / approach / algorithm / related work / future work / summary alongside the pre-existing methods / results / discussion / conclusion. New `related_work` section_type so subsection inheritance does not collapse it into `other`. SAGE's `4 EVALUATION` subsections now classify as `results`.
- **`is_expired(metadata, now_iso)` helper** (`src/nexus/metadata_schema.py`). Replaces the dropped `expires_at` field; computes expiry from `indexed_at + ttl_days` Python-side. `ttl_days == 0` is the permanent sentinel.
- **Cross-indexer drift guard test** (`tests/test_metadata_consistency.py`). Pins the contract that every chunked-write indexer routes through the factory and emits the full `ALLOWED_TOP_LEVEL` keyset.
- **Production-data remediation plan** documented in `docs/metadata-consistency-matrix.md`.

### Changed

- **Schema rationalisation:**
  - `source_title` collapsed into `title` (consumers used the chain `r.metadata.get("source_title") or r.metadata.get("title")` everywhere; one canonical field replaces the fallback).
  - `expires_at` removed; expiry derived from `indexed_at + ttl_days` via `is_expired`. `indexed_at` promoted from silently-dropped to allow-listed.
  - `bib_semantic_scholar_id` re-added to `ALLOWED_TOP_LEVEL` (had been mistakenly classified as cargo; it is the load-bearing "this title was enriched" marker for `commands/enrich.py:89` and `catalog/link_generator.py:38`).
  - Net: `ALLOWED_TOP_LEVEL` ends at 31 keys with one slot of safety margin under Chroma's 32-key cap.
- **All seven indexer paths** (`code_indexer.py`, `prose_indexer.py` markdown branch + line-fallback, `doc_indexer.py:_pdf_chunks` + `_markdown_chunks`, `pipeline_stages.py:_build_chunk_metadata` + `_enrich_metadata_from_extraction`, `db/t3.py:put` for MCP `store_put`) retrofitted through `make_chunk_metadata`.
- **MCP `store_put` (`db/t3.py:put`)** now produces full chunk identity including `content_hash` + `chunk_text_hash` + `chunk_index=0` + `chunk_count=1` + `corpus`. Closes the RDR-086 chash coverage hole for MCP-stored docs (previously they had no chash row, so `chash:<hex>` link spans could not resolve them).
- **PDF catalog hook** (`pipeline_stages._catalog_pdf_hook`): file_path is now stored as an absolute path (was basename, blocked aspect extractor's disk fallback when reading process cwd differed from the original ingest cwd).
- **Aspect extractor** (`src/nexus/aspect_extractor.py`):
  - New `_source_content_from_t3` helper reassembles document text from T3 chunks, filters to scholarly sections, caps at 80 KB. Sources from T3 first, falls back to disk on T3 miss.
  - `_invoke_once` and `_invoke_once_batch` now feed the prompt via stdin instead of argv. Removes ARG_MAX as a runtime crash class for long papers.
  - `aspect_worker` batch path uses the same T3-then-disk precedence.
- **Read-side migrations:** every `r.metadata.get("source_title") or r.metadata.get("title")` chain in `mcp/core.py`, `commands/store.py`, `commands/catalog.py`, `commands/enrich.py`, `commands/index.py`, `indexer.py` collapsed to direct `title` reads. T3 expire-guard moved from `where=expires_at < now` to `is_expired` Python-side check.
- **Cargo cleanup** (post-factory follow-up):
  - `chunker.py` stops emitting `filename` / `file_extension` / `ast_chunked` per code chunk (the factory ignored them; normalize() would have dropped them anyway).
  - `mcp/core.py` and `commands/store.py` stop reading `extraction_method` / `page_count` / `has_formulas` from search-result display paths (those fields are not in `ALLOWED_TOP_LEVEL`, so the reads always returned empty since the factory landed).

### Fixed

- **Aspect extraction was broken end-to-end** for every PDF since 4.14.2. Two compounding causes: PDFs had `section_type=""` hardcoded, so the extractor's section filter dropped every chunk; the catalog stored basename `file_path` for PDFs, so the extractor's disk-fallback resolved against cwd and failed. Both root causes fixed; SAGE paper (arxiv 2604.15583) extracts cleanly with confidence 0.78 in end-to-end verification.
- **`nx enrich` was non-idempotent** since the `bib_semantic_scholar_id` field stopped surviving normalize(). Every enrich run re-enriched every title because the marker write at `commands/enrich.py:145` was silently dropped at storage. Restoring the field to `ALLOWED_TOP_LEVEL` re-enables the skip-already-enriched check.
- **MCP-stored docs were invisible to chash-based features** (RDR-086 spans, `chash_index` coverage audits) because `db/t3.py:put` was producing only 10 metadata keys, missing `content_hash` and `chunk_text_hash`.

### Removed

- `source_title` from `ALLOWED_TOP_LEVEL` (collapsed into `title`).
- `expires_at` from `ALLOWED_TOP_LEVEL` (derived via `is_expired`).
- `filename` / `file_extension` / `ast_chunked` from the chunker's per-chunk metadata dict.

### Migration

Production catalogs and T3 collections indexed before this release carry the old schema (`source_title` instead of `title`, `expires_at` instead of derived expiry, empty `section_type` on PDFs, basename `file_path` for PDFs). Remediation:

1. **Path repair**: `nx catalog remediate-paths <SOURCE_DIR>` against your papers archive resolves basename and ghost `file_path` values.
2. **T3 metadata backfill**: not yet automated; the matrix doc (`docs/metadata-consistency-matrix.md`) sketches a `backfill-metadata` command that pages through every chunk and re-derives `section_type` from chunk text + collapses `source_title` into `title` + drops the dropped fields. Until that ships, old chunks read with empty `title` if they only had `source_title` (consumer fallbacks were removed in this release for simplicity).

## [4.14.2] - 2026-04-26

Closes RDR-089 (Structured Aspect Extraction at Ingest). Ships ingest-time per-document structured aspect extraction for `knowledge__*` collections via a new third post-store hook chain (`fire_post_document_hooks`), a synchronous extractor calling the Claude CLI, and an async-queue worker that decouples extraction from the ingest path. Also retires the RDR-037 four-database migration probe (transitional safety net from 2026-03-14 — its job is done).

The original RDR design specified synchronous-inline extraction at the post-document hook fire site. The P1.3 spike on `knowledge__delos` (10 papers × 3 runs) measured median 26.5 s and p95 38.1 s per document — 11–17× over the <3 s threshold from Critical Assumption #2. Synchronous-inline would block the ingest path for ~25 s per document. The redirect: hook enqueues to T2 `aspect_extraction_queue` (microsecond-scale INSERT), a daemon worker thread drains the queue and invokes the same synchronous extractor. Ingest stays fast; aspects populate within seconds-to-minutes of ingest depending on queue depth.

### Added

- **Document-grain post-store hook chain** (`src/nexus/mcp_infra.py`). Third chain alongside the single-doc (RDR-070) and batch (RDR-095) chains. Primitives: `register_post_document_hook(fn)` and `fire_post_document_hooks(source_path, collection, content)`. Same per-hook failure-isolation pattern (capture, persist to T2 `hook_failures`, never propagate). Synchronous all the way down — zero asyncio in the dispatcher; F1 audit caught and pinned this contract via the `test_mcp_store_put_calls_document_hook_synchronously` AST assertion. Fires from every CLI ingest entry point (8 fire sites in 6 modules; `doc_indexer.py` carries 3 sites for its three pdf/markdown/repo entry points) and from MCP `store_put`.
- **`hook_failures.chain` enum column** (T2 4.14.2 migration, RDR-089 P0.1). Replaces the previous `is_batch` boolean encoding with a forward-compatible enum: `'single'` | `'batch'` | `'document'`. Existing batch rows backfill via `UPDATE chain='batch' WHERE is_batch=1`. The legacy `is_batch` column is retained for back-compat with pre-4.14.2 readers; existing write paths dual-write both columns. Future migration may drop `is_batch` once all readers consume `chain`.
- **T2 `document_aspects` store** (`src/nexus/db/t2/document_aspects.py`, T2 4.14.2 migration, RDR-089 P1.1). Per-document structured aspects keyed by `(collection, source_path)` (NOT per-chunk `doc_id` — multiple chunks of the same source map to one aspect row). Idempotent overwrite on re-extract. JSON columns for `experimental_datasets`, `experimental_baselines`, and an `extras` extensibility anchor. `list_by_extractor_version(name, max_version)` filter (lexicographic STRICT-less-than) drives re-extraction triage when a new model version ships.
- **T2 `aspect_extraction_queue` store** (`src/nexus/db/t2/aspect_extraction_queue.py`, T2 4.14.2 migration, RDR-089 follow-up `nexus-qeo8`). Durable WAL buffer feeding the async worker. PRIMARY KEY mirrors `document_aspects`. State machine: `pending` → `in_progress` → DELETE on success | `failed` after final retry. `claim_next` uses a SQL compare-and-swap (`UPDATE ... WHERE status='pending'` with `cursor.rowcount` check) for cross-process atomicity — concurrent MCP server + CLI ingest workers race on `claim_next` and the loser falls through to the next pending row. `reclaim_stale(timeout_seconds=300)` resets `in_progress` rows older than the timeout to `pending`, recovering from worker process death without losing rows. The `content` column captures document text at enqueue time when in scope (MCP `store_put` boundary); CLI rows where content is not in scope pass `content=""` and rely on the worker's source-path-read fallback.
- **Synchronous `aspect_extractor`** (`src/nexus/aspect_extractor.py`). Single public entrypoint `extract_aspects(content, source_path, collection) -> AspectRecord | None`. Phase 1 ships exactly one extractor config (`scholarly-paper-v1` keyed on the `knowledge__*` prefix). Subprocess invocation: `claude -p PROMPT --output-format json` with timeout 180 s. Outer-wrapper parse (`{"result": "...", "session_id": ..., "usage": ...}`) plus markdown code-fence stripping for the inner JSON. Retry policy: TimeoutExpired / transient stderr / JSON parse failure → retry (max 3 attempts, exponential backoff with ±25% jitter — 2 s / 5 s / 12 s); schema validation / hard subprocess error → null-fields record, no retry. Defensive `content.replace("\x00", "")` strip catches PDF-extracted text with embedded null bytes (4/10 papers in the P1.3 spike — `subprocess.run` rejects argv entries with null bytes per POSIX C-string contract).
- **Async aspect-extraction worker** (`src/nexus/aspect_worker.py`). `AspectExtractionWorker` daemon thread with lazy-singleton lifecycle (`get_worker` / `ensure_worker_started` / `stop_worker` / `reset_worker_for_tests`). Polls every 2 s; sleeps when queue empty. `reclaim_stale` runs every 15 polls (~30 s) to bound the O(N) UPDATE cost on large stuck queues. Result handling: populated record → upsert + DELETE; null-fields record → upsert + DELETE without worker-level retry (the extractor already retried 3× internally); unsupported collection → DELETE silently; uncaught exception → `mark_failed` for triage. `aspect_extraction_enqueue_hook` is the registered consumer of the document-grain chain; it persists `(collection, source_path, content)` to the queue (microsecond-scale T2 INSERT) and lazy-spawns the worker.
- **`nx enrich aspects <collection>` CLI subcommand** (`src/nexus/commands/enrich.py`). Click group restructure: existing `nx enrich <coll>` becomes `nx enrich bib <coll>`; new `nx enrich aspects <coll>` ships RDR-089's batch enrichment surface. Bypasses the post-document hook chain — iterates the catalog (one entry per source document, NOT per chunk via the new `Catalog.list_by_collection` method) and calls `extract_aspects` directly. Flags: `--dry-run` (cost estimate, no subprocess); `--validate-sample N` (default 5%; runs `operator_verify(claim, evidence)` on N% of newly-extracted aspects, disagreements append to `./validation_failures.jsonl`); `--re-extract --extractor-version v` (filters via `document_aspects.list_by_extractor_version`).
- **Drift guards** (`tests/test_hook_drift_guard.py`). New `DOCUMENT_HOOK_GUARDED_NAMES` frozenset (analogue of the batch-chain `GUARDED_NAMES`), allowlist `aspect_worker.py` (definition) + `mcp/core.py` (registration). New `test_every_cli_ingest_site_fires_document_hook` AST count test. New `test_mcp_store_put_calls_document_hook_synchronously` walks AST parents to assert the doc-hook call has no `await` or `asyncio.to_thread` ancestor (audit F1 regression guard). New runtime test `test_index_pdf_fires_document_hook_exactly_once` (in `tests/test_doc_indexer.py`) drives a sample PDF through `index_pdf` with a counting probe hook and asserts exactly one fire — the AST count guard alone cannot detect a regression that moves a fire site inside a per-chunk loop.
- **P1.3 spike harness** (`scripts/spikes/spike_rdr089_delos.py`). Re-runnable measurement tool with two modes (latency + fire-once) and proper `status=ok` vs `all_attempts` aggregation; written-once committed evidence in `spike_rdr089_results.jsonl` and `spike_rdr089_summary.json`.

### Changed

- **`mcp/core.py:store_put` registers `aspect_extraction_enqueue_hook` via `register_post_document_hook`**. Plain synchronous call from the existing batch-chain registration block; `store_put` is `def`, not `async def`, and FastMCP wraps sync `@mcp.tool()` bodies in worker threads at the framework level — the RDR-089 audit F1 contract.
- **`indexer.py:_index_pdf_file` gains a fire site** (P0.review caught the omission; the bead's original 7-site map missed it). Total wiring: 8 fire-statement instances across 6 modules (`doc_indexer.py` accounts for three of the eight sites).
- **`nx enrich <collection>` invocation shape** moves to `nx enrich bib <collection>`. Tests, `docs/cli-reference.md`, `docs/catalog.md`, and the SessionStart hook output all updated. The bib subcommand carries the existing Semantic Scholar enrichment behavior verbatim.
- **`Catalog.list_by_collection(physical_collection, limit=None)`** new public method (`src/nexus/catalog/catalog.py`). One entry per source document for a given physical collection, ordered `tumbler ASC`. Used by `nx enrich aspects` to drive per-document iteration.

### Removed

- **RDR-037 four-database migration probe** (`src/nexus/db/t3.py:OldLayoutDetected` and probe block in `__init__`; `src/nexus/health.py` parallel probe). Transitional safety net from the 2026-03-14 single-database consolidation. The migration window has been closed for over six weeks; new installs cannot have legacy data, and migrated users had to manually delete the old cloud databases anyway. Removing the probe saves one cloud roundtrip on every cold T3 connect and eliminates a false-positive failure mode for fresh `$HOME` environments (sandboxes, new machines).

### Fixed

- **Cross-process race in `claim_next`** (`src/nexus/db/t2/aspect_extraction_queue.py`). The original implementation issued SELECT-then-UPDATE under a Python `threading.Lock` that does not span processes. Two concurrent processes (MCP server + CLI ingest) could double-claim the same row. CAS pattern fix: UPDATE WHERE clause now includes `AND status='pending'`; loser sees `cursor.rowcount == 0` and loops back to SELECT. Bounded retry budget `_MAX_CAS_RETRIES = 8`.
- **MCP-path content lost in queue** (`src/nexus/aspect_worker.py:aspect_extraction_enqueue_hook`). The hook previously ignored the `content` argument and the worker tried to read `Path(source_path).read_text()` — but at the MCP boundary `source_path` is a 16-char content-hash `doc_id`, not a real filesystem path. Result: every MCP-path extraction silently produced a null-fields record. The hook now persists `content` to the queue row when non-empty; the worker uses `row.content` as the primary input. CLI rows still pass `content=""` and rely on the source-path-read fallback as before.

### Full-Scope Phases A–F (post-substantive-critique deliverable)

The substantive critic flagged that Phase 0–2 shipped the writer surface (extractor + queue + worker + CLI) but deferred everything that delivered user-visible value. The user's "EVERYTHING we were to originally deliver, no half-measures" directive drove this branch to close every Open Question on RDR-089 plus the RDR-089 §Day 2 Operations gap. Six discrete phases, each with its own commit + test surface:

- **Phase A** — `nx enrich aspects` Day 2 Ops: `list <collection>` / `info <collection> <source_path>` / `delete <collection> <source_path>`. RDR §Day 2 Operations explicitly listed these; the bead recipe marked them "optional in this bead but cheap to add" and they were dropped in P2.2. 8 new tests.
- **Phase B** — SQL fast path for `operator_filter`, `operator_groupby`, `operator_aggregate`. New `nexus.operators.aspect_sql` substrate. Each operator gains `source="auto"|"aspects"|"llm"` and `aspect_field=<column>` parameters. `auto` mode tries SQL first and falls back to LLM on prerequisite failure; `aspects` forces SQL; `llm` skips SQL. Heuristic `_infer_aspect_field` maps natural-language criteria to the seven aspect columns + `extras.<key>` form. 36 contract tests.
- **Phase C** — Benchmark proving the O(1) reads claim. `scripts/spikes/bench_rdr089_sql_fast_path.py` runs each operator 10 times in SQL fast path mode vs LLM mode (mocked at 1.5 s constant latency) on a synthetic 100-paper corpus. Result: 500x speedup on filter/groupby, 47000x on aggregate. Committed evidence in `scripts/spikes/bench_rdr089_results.json`; `verdict_pass: true`.
- **Phase D** — Batch Haiku per call. New `extract_aspects_batch(items)` extracts N papers in ONE Claude subprocess invocation; the model returns a `{"papers": [{source_path, ...}, ...]}` array which the extractor demuxes by `source_path`. Worker config knob `batch_size=5` (default): when queue depth permits a batch, the worker drains it in one call; when only one row pending, falls back to the single-paper path (preserves low-latency-to-first-result for small queues). At the measured 26.5 s single-paper median this collapses a 1000-paper corpus drain from ~7 hours to ~40-80 minutes. Per-paper failure isolation: a malformed entry in the batch response yields null-fields for that paper without affecting siblings. 9 batch-extractor tests + 2 worker integration tests.
- **Phase E** — `extras` → fixed-column promotion mechanic. `nexus.aspect_promotion.promote_extras_field(db, name, sql_type, prune)` runs three phases: ALTER TABLE ADD COLUMN (idempotent), backfill from `extras` via `json_extract`, optional `json_remove` of the extras key. Promotion is logged to T2 `aspect_promotion_log` (registry-managed in `src/nexus/db/migrations.py` after the round-2 substantive critique moved it out of the original lazy-create path; the lazy `_ensure_audit_table` continues to exist as a defensive guard for older databases). New `nx enrich aspects-promote-field <name> [--type TYPE] [--prune] [--history]` CLI. 16 tests covering happy path, idempotency, partial-extras backfill, reserved-name rejection (the 12 RDR-locked column names), unsafe-identifier rejection (digit prefix, hyphen, quote, semicolon, SQL-injection attempt, empty), audit log.
- **Phase F** — `rdr__*` deterministic extractor. New `rdr-frontmatter-v1` config keyed on the `rdr__` prefix. `ExtractorConfig` gains an optional `parser_fn` shortcut: when set, `extract_aspects` bypasses the Claude subprocess entirely and calls the parser directly. The RDR parser splits YAML frontmatter from the markdown body, walks h2 sections, and maps to the same five-field aspect schema (Problem Statement → problem_formulation, Proposed Solution → proposed_method, Validation → experimental_results, Alternatives Considered → experimental_baselines as the list of alternative titles). Section-name aliases tolerate template variation. Confidence is 1.0 (deterministic). 21 tests.

The Phase B benchmark in Phase C answered the original RDR's value statement empirically: a 100-paper corpus that has had its aspects extracted answers `operator_filter("knowledge__delos", aspect="proposed_method", contains="paxos")` in milliseconds where the LLM path takes seconds-to-minutes per query.

## [4.14.1] - 2026-04-25

Closes RDR-095 (Post-Store Hook Framework: Batch Contract). Adds a parallel batch-shape post-store hook chain alongside the existing single-document chain, migrates the two hardcoded batch enrichments (`taxonomy_assign_batch` from RDR-070, `chash_dual_write_batch` from RDR-086) into registered hooks, and collapses seven hardcoded CLI ingest call sites into single `fire_post_store_batch_hooks` invocations. A closure-handoff follow-up makes both chains fire from every storage event so future per-document consumers cover MCP and CLI paths uniformly. RDR-089 (the originally-anticipated single-doc consumer) instead introduced a third document-grain chain in 4.14.2 — this entry's reference to "future single-doc consumers (RDR-089 aspect extraction with one Haiku call per doc)" is historically accurate to the 4.14.1 release moment but the actual landing shape is a separate chain, not a single-doc registration.

Skips 4.14.0 as a public version: the `4.14.0` migration (`migrate_hook_telemetry`, nexus-ntbg) landed in code under PR #318 but was never released; this release fires both `4.14.0` and `4.14.1` migrations under the same upgrade.

### Added

- **Batch-shape post-store hook contract** (`src/nexus/mcp_infra.py`). New primitives `register_post_store_batch_hook(fn)` and `fire_post_store_batch_hooks(doc_ids, collection, contents, embeddings, metadatas)`. Same per-hook failure-isolation semantics as the existing single-document chain: exceptions captured, persisted to T2 `hook_failures`, never propagated to the caller.
- **`hook_failures` schema migration** (T2 4.14.1, `src/nexus/db/migrations.py`). Additive `batch_doc_ids TEXT` and `is_batch INTEGER NOT NULL DEFAULT 0` columns. Existing scalar rows are untouched. Idempotent; no-op when `hook_failures` table is absent (4.9.10 migration runs first in the chain).
- **`hook_telemetry` table migration** (T2 4.14.0, nexus-ntbg, PR #318). Surfaced via `nx doctor --check-hooks` for slow-hook investigation; ships in this release alongside the RDR-095 migration.
- **`taxonomy_assign_batch_hook` embedding-fetch fallback** (`src/nexus/mcp_infra.py:_fetch_or_embed`). When the hook is invoked with `embeddings=None` (the MCP `store_put` path), it fetches embeddings from T3 inline and falls back to local MiniLM embedding when T3 is unavailable. Same total round-trip cost as the legacy single-doc shim on the MCP path.
- **AST-based drift guard** (`tests/test_hook_drift_guard.py`). Asserts no source file outside `src/nexus/mcp_infra.py` (definitions) and `src/nexus/mcp/core.py` (registration) references the batch hooks directly. Symmetric-fire invariant test asserts every CLI indexer module has matching counts of both fire calls.
- **Batch-shape failure rendering in `nx taxonomy status`** (`src/nexus/commands/taxonomy_cmd.py`). Action line now reports `<N> post-store hook failure(s) affecting <M> document(s)` when any batch row is present (M > N). Scalar-only output preserves the legacy phrasing.

### Changed

- **Symmetric post-store fire** (post-acceptance closure follow-up). Both chains now fire from every storage event: MCP `store_put` fires single-doc once and batch with a 1-element list; CLI ingest fires batch with the full payload and single-doc per doc. Drops the legacy `taxonomy_assign_hook` (its embedding-fetch fallback folded into the batch hook).
- **Seven CLI ingest call sites** (`indexer.py`, `code_indexer.py`, `prose_indexer.py`, `pipeline_stages.py`, `doc_indexer.py` x3) collapse the legacy chash + taxonomy hardcoded pair into one `fire_post_store_batch_hooks` invocation plus a per-doc `fire_post_store_hooks` loop.
- **Documentation**: new "Post-Store Hooks" sections in `CLAUDE.md` and `docs/architecture.md`. Out-of-scope decisions (catalog-registration mechanisms, auto-linker) documented inline so future RDRs do not silently re-include them.

### Fixed

- **`migrate_hook_failures` docstring** (`src/nexus/db/migrations.py`). References both `fire_post_store_hooks` AND `fire_post_store_batch_hooks` rather than the legacy single-shape only.

## [4.13.0] - 2026-04-25

Closes RDR-094 Phase F (final phase of the MCP-Owned T1 Chroma Lifecycle epic). The `NEXUS_MCP_OWNS_T1` env-var gate from 4.12.0 is removed entirely; nx-mcp's lifespan unconditionally owns chroma's lifecycle. Net source diff: **-283 lines** (deletion of dead-but-gated code paths).

This release is structurally a simplification: 4.12.0 already shipped the MCP-owned-T1 path as default-on; 4.12.1 fixed the stdin-EOF + SIGTERM signal-handler race that surfaced in the shakeout. 4.13.0 deletes the opt-out machinery now that the path is empirically validated. Same observable behaviour as 4.12.1 unless an operator was explicitly setting `NEXUS_MCP_OWNS_T1=0` to opt out: the env var is now silently ignored and the MCP-owned path runs anyway.

### Removed

- **`NEXUS_MCP_OWNS_T1` env-var gate** (`src/nexus/mcp/core.py`, `src/nexus/hooks.py`). Module-scope `_MCP_OWNS_T1` constant, `_flag_enabled` helper in `mcp/core.py`, and `_mcp_owns_t1_enabled` helper in `hooks.py` all deleted. FastMCP's `lifespan` kwarg is unconditionally `_t1_chroma_lifespan`; `main()` unconditionally registers atexit + SIGTERM/SIGINT handlers. Tests gain regression sentinels (`test_no_mcp_owns_t1_module_attr`) asserting the module attrs are gone.

- **Hook-side chroma-spawn block** (`src/nexus/hooks.py:session_start`). The session.lock acquisition, `start_t1_server` call, `write_session_record_by_id` call, and `spawn_t1_watchdog` call are all deleted. nx-mcp's lifespan owns spawn; the hook now only runs the orphan-tmpdir sweep, resolves the session UUID (env > stdin > fresh), and writes `current_session` when this is a top-level session. Imports of `fcntl`, `shutil`, `find_ancestor_session`, `find_claude_root_pid`, `spawn_t1_watchdog`, `start_t1_server`, `stop_t1_server`, `write_session_record`, `write_session_record_by_id`, `_ppid_of` are dropped.

- **Hook-side chroma-stop block** (`src/nexus/hooks.py:session_end`). `session_end` is now a thin pass-through to `session_end_flush` (`return session_end_flush()`). Chroma teardown is owned by nx-mcp's lifespan + signal handler + atexit chain (with the watchdog as the safety net), never by the hook.

### Changed

- **RDR-094 §Phase 4 marked COMPLETE** (`docs/rdr/rdr-094-mcp-owned-t1-chroma-lifecycle.md`). The Phase 4 prerequisites enumeration is replaced with a record of how each was cleared: CA-2 verified post-mitigation (Spike B 40/40 connected_to_parent, T2 id=983); Phase B+C shipped (PRs #306, #307); canary served by the v4.12.0 → v4.12.1 shakeout cycle (PR #314 fixed the stdin-EOF + SIGTERM signal-handler race that surfaced).

- **RDR-093 closed as implemented** (`docs/rdr/rdr-093-groupby-aggregate-operators.md`). `operator_groupby` and `operator_aggregate` shipped in RDR-088's wake; per-gap pointers validated against the codebase. Ride-along close in this release.

## [4.12.1] - 2026-04-25

Shakeout patch for 4.12.0. Production sessions on Hal's reference install showed `mcp_server_crashed` events in `mcp.log` on every clean shutdown after the default-on flag flip. Root cause: stdin-EOF + SIGTERM race. Claude Code closes the MCP client's stdio pipe and sends SIGTERM near-simultaneously; the lifespan finally fires (clean-exit path) and starts running `_t1_chroma_shutdown` → `stop_t1_server`'s 100ms poll loop; the SIGTERM signal handler then runs on top of the paused frame, calls `sys.exit(0)`, and the resulting `SystemExit` propagates through anyio's TaskGroup as an unhandled error. Chroma was always cleaned up correctly (the watchdog confirmed via `chroma_cleanup_complete` events; `nx doctor --check-tmpdirs` was clean), but every clean shutdown logged a misleading multi-thousand-character traceback as a "crash".

Spike A's 40-cycle SIGTERM evidence didn't surface this because the spike sent only SIGTERM (no stdin EOF); only one cleanup path fired per cycle. Production has BOTH paths firing simultaneously.

### Fixed

- **`_sigterm_handler` no longer races the lifespan finally** (`src/nexus/mcp/core.py`). Two coordinated changes:
  - New module-scope `_SHUTDOWN_IN_FLIGHT` flag — set by `_t1_chroma_shutdown` on first entry, never cleared. Re-entrant calls (signal handler firing during in-flight cleanup) short-circuit at the top of `_t1_chroma_shutdown` before touching `_OWNED_CHROMA`. Catches the case where the lifespan finally is paused inside `stop_t1_server`'s poll loop and SIGTERM arrives.
  - `_sigterm_handler` checks `_SHUTDOWN_IN_FLIGHT` first: returns immediately when the lifespan owns the exit path. When it's the first signal (SIGTERM-only path with no prior stdin EOF), it drives the shutdown then calls `os._exit(0)` instead of `sys.exit(0)`. `os._exit` terminates the process without raising `SystemExit`, so anyio doesn't see a TaskGroup error and `mcp.log` no longer records spurious crash events on clean shutdowns.

Tests: `tests/test_mcp_chroma_lifecycle.py` 15 → 19, with explicit regression sentinels (`test_in_flight_flag_blocks_reentrant_call`, `test_returns_silently_when_shutdown_in_flight`, `test_drives_shutdown_and_os_exit_when_first_signal`, `test_does_not_use_sys_exit`).

## [4.12.0] - 2026-04-25

Closes RDR-094 Phase 4 (MCP-Owned T1 Chroma Lifecycle) end-to-end and **flips the `NEXUS_MCP_OWNS_T1` flag default-on**. The chroma server's lifecycle is now owned by nx-mcp's lifespan / atexit / signal handlers by default, and the watchdog sidecar dual-watches both the MCP and Claude Code root PIDs so any failure mode (clean SIGTERM, SIGKILL, SIGSEGV, Claude crash, stdio pipe break) gets cleaned up within ~10s. Spike A (40/40), Spike B (CA-2 race verified after the retry mitigation), and Spike C (issue #40207 verified-negative for vanilla stdio) all pass at full sample size.

`NEXUS_MCP_OWNS_T1=0` (or `false` / `no` / `off`) is the emergency opt-out; absence of the env var means **on**. Full removal of the gate is left as a follow-up (Phase F / nexus-2lm0).

The T1 race retry that closes Spike B's CA-2 race is independent of the gate: `T1Database.__init__` retries `find_session_by_id` on a 100/200/400/800/1500 ms backoff (~3s max) whenever `NX_SESSION_ID` is set in env, regardless of `NEXUS_MCP_OWNS_T1`.

### Added

- **`_resolve_session_record_with_retry` in `src/nexus/db/t1.py`** (RDR-094 CA-2 / nexus-zsqf). Wraps `find_session_by_id` with an exponential 100/200/400/800/1500 ms backoff (~3s total max wait) when `NX_SESSION_ID` is set in env. Closes the silent-downgrade race where a subagent dispatched within chroma's cold-start window inherits `NX_SESSION_ID` but finds no session record yet, falling through to `chromadb.EphemeralClient()` invisibly under stdio transport. Top-level callers without a parent session pay no wait (env-var gate); `NEXUS_SKIP_T1=1` (stateless-operator path) bypasses the retry entirely. Spike B evidence: 40/40 cycles `ephemeral_downgrade` pre-mitigation → 40/40 `connected_to_parent` post-mitigation. The bead's original prescription (50/100/200ms × 3 = 350ms total) was empirically insufficient: chroma cold start is 1.1-1.7s on the reference install. Schedule deviation captured in T2 `nexus_rdr/094-spike-b-subagent-race-verified` (id=983).

- **`session_end_flush()` in `src/nexus/hooks.py`** (RDR-094 Phase B / nexus-2b9r). Storage-only path that does T1 scratch flush + T2 expire with no chroma teardown. Fork-safe (only opens fresh SQLite handles). The legacy `session_end()` becomes a thin wrapper that calls `session_end_flush()` then runs the chroma-stop block when this process owns the record AND `NEXUS_MCP_OWNS_T1` is unset (the in-place gate from PR #300 stays for the flag-off rollout window).

- **`nx hook session-end-flush` CLI subcommand** (RDR-094 Phase B / nexus-2b9r). Routes to `hooks.session_end_flush` directly. Available as a manual-debug entry point; the production SessionEnd dispatch goes through `nx-session-end-launcher` for the fork-first cold-start race fix.

- **t1_watchdog structured logging** to `~/.config/nexus/logs/watchdog.log` (RDR-094 Phase G / nexus-aqna). Emits seven lifecycle events (`watchdog_started`, `poll_tick` at DEBUG, `mcp_pid_disappeared`, `claude_pid_disappeared`, `signalling_mcp_pid`, `chroma_cleanup_started` / `_complete`, `watchdog_exiting`). Closes the attribution gap where Spike B / Phase E telemetry could not distinguish "watchdog cleaned chroma post-detection" from "watchdog never fired and chroma leaked". Hot-path import discipline preserved: only stdlib + structlog at module level.

- **`nx doctor --check-mcp-logs` CLI flag** (RDR-094 Phase H follow-up / nexus-50u5). Surfaces nx-mcp silent-death signatures from Claude Code's per-MCP-server JSONL cache at `~/Library/Caches/claude-cli-nodejs/<cwd-slug>/mcp-logs-*/`. Three signature classes: `STDIO connection dropped after Ns uptime` and `stdio transport error` surface as **WARNING**; `MCP error -32001: AbortError` as **INFO**. Two-level lookback filtering (file mtime + per-record timestamp). macOS only; Linux/Windows skip cleanly with "not present on this platform". Slug derivation: cwd with both `/` AND `.` replaced by `-` (verified empirically against 3779 cache files; the `.` rule is required for usernames containing a dot). Real-world smoke surfaces the 2026-04-25T00:20:34Z reference incident (session 0c700072, nx-mcp pid 94433 went silent for 25 minutes after 23092s of uptime).

- **`stdio_pipe_break` phase in the Spike A harness** (RDR-094 Phase I / nexus-sawb). Closes nx-mcp's stdin without sending any signal: tests the fifth failure mode observed 2026-04-25 where nx-mcp died silently with no `mcp_server_stopping` or `mcp_server_crashed` event. Three diagnostic outcomes: `mcp_owned_lifespan` (clean EOF), `watchdog_mcp` (crash event recorded), `watchdog_unknown` (the silent-death signature). Default `--phases` list grew to five.

- **Spike B race-probe harness** (`scripts/spikes/spike_rdr094_b_subagent_race.py`, RDR-094 Phase D / nexus-zsqf). 40-cycle protocol (10 runs × 4 timing variants: 0/5/50/200ms dispatch delay) with explicit probe-side classification and aggregator that requires positive evidence to verify CA-2 (an earlier classifier+aggregator pair allowed all-unknown to false-verify; both fixed before the runtime evidence pass).

### Changed

- **`NEXUS_MCP_OWNS_T1` is now default-on** (RDR-094 Phase 4 default-on rollout). `src/nexus/mcp/core.py` and `src/nexus/hooks.py` now default to ON when the env var is absent; only `NEXUS_MCP_OWNS_T1=0` / `false` / `no` / `off` opts out. New `_flag_enabled` helper in `src/nexus/mcp/core.py` and `_mcp_owns_t1_enabled` in `src/nexus/hooks.py` centralise the tri-state read. Tests gain regression sentinels for both the default-on path (`test_session_end_default_on_skips_chroma_without_env_var`) and the opt-out path (existing `test_session_end_with_session_record` updated to set `NEXUS_MCP_OWNS_T1=0` explicitly).

- **`nx/hooks/hooks.json` SessionEnd command** (RDR-094 Phase C / nexus-l828). `nx-session-end-launcher 2>/dev/null || nx hook session-end-detach || true` → `nx-session-end-launcher` (no fallback). The detach fallback hits the same 2s cold-start race the launcher exists to solve, so falling back to it on launcher failure was a footgun. Timeout reduced from 5s to 3s: storage-only flush is sub-second, so the tighter window means a wedged hook is reaped faster.

- **`nx-session-end-launcher` grandchild dispatch** (RDR-094 Phase C / nexus-l828). `_run_session_end_synchronously` now calls `hooks.session_end_flush()` instead of `hooks.session_end()`. Storage-only flush stays in the launcher; chroma teardown is owned by the watchdog (single-watch claude-only mode under flag-off, dual-watch under flag-on). The hook-side `stop_t1_server` no longer races MCP's lifespan / atexit / signal-handler cleanup. **BANNED invariant** preserved: this module imports only `os` and `sys` at top level; fork-first guarantee intact.

- **RDR-094 §Day 2 Operations** gains a "Diagnosing nx-mcp silent death" subsection (RDR-094 Phase H / nexus-3f95). Documents the three-log correlation: nexus-side `mcp.log` (nx-mcp's perspective), nexus-side `watchdog.log` (watchdog's perspective), and Claude Code's `mcp-logs-*` JSONL cache (client's perspective). The fifth failure mode lives in the gap: nexus-side silent, Claude-side records `STDIO connection dropped`. Full event-signature catalogue in T2 `nexus_rdr/094-claude-mcp-log-investigation` (id=981).

- **RDR-094 §Critical Assumptions CA-2** marked VERIFIED (RDR-094 Phase D / nexus-zsqf). Documents the full evidence trail: harness shipped (#308), classifier/aggregator bugs found and fixed (#310), race confirmed reproducible at 40/40 cycles, retry mitigation shipped (#311), final pass at 40/40 connected_to_parent. Schedule deviation from the bead's prescription (placement: T1Database vs `_resolve_top_level_session_id`; schedule: 100/200/400/800/1500ms vs 50/100/200ms × 3) captured in the assumption block.

### Fixed

- **Spike C observer historical-replay bug** (RDR-094 Phase A / nexus-5rea). Observer initialised tail position to byte 0 on startup, replaying every pre-existing `mcp.log` entry as if it had occurred during the observation window. False `+0.0s` `mcp_events` and spurious `restart_cycle` classifications contaminated the original Spike C run. Observer now filters each parsed event by comparing the structlog `timestamp=` field against the observer's start time: robust against log rotation (the alternative seek-to-EOF approach loses entries written between rotation and seek).

- **Spike B harness classifier + aggregator** (RDR-094 Phase D / nexus-zsqf). `chromadb.HttpClient(...)` and `chromadb.EphemeralClient()` are factory functions that BOTH return `chromadb.api.client.Client`: the original substring check on `type(client).__name__` matched neither and returned `unknown` for every cycle. Aggregator further required only zero downgrades (not positive evidence) to verify, so the all-unknown 40-cycle pass false-verified. Classifier now reads an explicit `outcome` field emitted by the probe (computed from the captured warnings stream); aggregator requires `connected_to_parent ≥ 1` to verify. Regression sentinels for both bugs added to the test suite.

## [4.11.1] - 2026-04-24

Shakeout patch for 4.11.0: the 4.10.3 SessionEnd detach path was still producing `Hook cancelled` at session close on the reference install. Root-caused in under an hour: `nx hook session-end-detach` forks the detach daemon only AFTER Click has parsed argv and after ~2 seconds of `nexus.*` imports on a cold cache. Claude Code's shutdown SIGTERM arrives faster than 2 seconds on this machine, so the first `os.fork()` never runs and the whole hook chain dies. The 4.11.0 `uuid_mismatch` sweep was catching the orphans at next SessionStart, so the leak was bounded — but the graceful cleanup (flush pending scratch entries to T2, expire memory entries) was being skipped and the error message appeared on every session close.

### Added

- **`nx-session-end-launcher` console script** (`src/nexus/_session_end_launcher.py`, nexus-2u7o). Dedicated entry point whose top-level imports are `os` and `sys` only. `main()` immediately double-forks + `setsid`s + redirects stdio before any `nexus.*` module is loaded; the grandchild then imports `nexus.hooks` and runs `session_end()` normally. Wall-clock time to return control to Claude Code: ~256ms on cold cache (8x faster than the 2.0s `nx hook session-end-detach` measured in the field). Tests pin the fork-first invariant (module must not import any `nexus.*` submodule at top level) and the daemonization flow.

### Changed

- **`nx/hooks/hooks.json` SessionEnd command** — `nx hook session-end-detach || true` replaced with `nx-session-end-launcher 2>/dev/null || nx hook session-end-detach || true`. The chained fallback handles mixed-version installs where the plugin upgrades ahead of the conexus CLI: if the new launcher binary isn't on PATH yet, the old detach subcommand runs as before. Once both sides are on 4.11.1, the fallback never fires.

### Fixed

- **`Hook cancelled` noise at session close** — with the fast launcher, the hook returns to Claude Code before its shutdown SIGTERM can cancel anything. Graceful cleanup (stop chroma, flush pending scratch, expire memory entries) now actually runs instead of getting skipped and deferred to the next-session sweep.

## [4.11.0] - 2026-04-24

Closes the three missing AgenticScholar paper operators (RDR-088 Phases 1+2) with full `plan_run` and operator-bundle integration, plus two ergonomics / correctness follow-ups that surfaced during the RDR-088 arc. Paper operator coverage goes from 9/13 composable tools to 11/13 (`GroupBy` and `Aggregate` from §D.4 remain explicitly deferred per the accepted RDR's scope subsection). The optional Phase 3 LLM rerank for `plan_match` was spiked against the full 50-row plan library and closed without landing — the rerank cleared the precision-delta threshold (+0.1212) but missed the recall-delta floor (-0.20 vs -0.15 allowed), so Gap 4 closes as "already addressed by RDR-092" per the pre-agreed dual-threshold gate.

### Added

- **`operator_filter(items, criterion, timeout)`** (`src/nexus/mcp/core.py:1618`, RDR-088 Phase 1). Paper §D.4 Filter operator. Returns `{items: list[dict], rationale: list[{id, reason}]}` via `claude_dispatch` — the output `items` array is a subset of the input, per-item rationale is keyed by `id`. Fills the gap where multi-step plans that narrow intermediate results previously had to synthesize through free-text `operator_rank` or `operator_extract` passes, losing precision.
- **`operator_check(items, check_instruction, timeout)`** (`src/nexus/mcp/core.py:1681`, RDR-088 Phase 2). Paper §D.2 Check operator. Returns `{ok: bool, evidence: list[{item_id, quote, role}]}` where `role` is enum-restricted to `supports` / `contradicts` / `neutral` so downstream plan steps can branch deterministically on the trichotomy. Distinct from `operator_compare` by output shape — compare returns free text for human presentation, check returns a composable boolean plus grounding evidence.
- **`operator_verify(claim, evidence, timeout)`** (`src/nexus/mcp/core.py:1735`, RDR-088 Phase 2). Paper §D.2 Verify operator. Returns `{verified: bool, reason: str, citations: list[str]}`. Single-claim variant of Check — different cardinality (1-claim vs N-items) so the two operators don't collapse into one contract.
- **Shared `_CHECK_EVIDENCE_ITEM_SCHEMA`** (`src/nexus/mcp/core.py`). Module-level constant for the `{item_id, quote, role}` evidence-item shape, referenced from both `operator_check`'s standalone schema and `src/nexus/plans/bundle.py`'s `_terminal_schema` check branch — one authoritative definition prevents drift between standalone and bundle paths.
- **Hydration + bundle wiring for all three new operators** (`src/nexus/plans/runner.py`, `src/nexus/plans/bundle.py`). `_OPERATOR_TOOL_MAP` adds `filter`, `check`, `verify` → `operator_*`. `_INPUTS_TARGET` hoisted to module scope and extended for filter and check (verify is deliberately excluded — it takes scalar `claim` + `evidence` args so a stray `inputs` should surface as an authoring bug, not be silently renamed). `BUNDLEABLE_OPERATORS` grows from 5 to 8 operators; `_describe_step` and `_terminal_schema` branches added so `search → filter → check` plans fuse into a single `claude_dispatch` call (verified live on `knowledge__delos`, ~40s end-to-end versus ~65s for an isolated-dispatch traverse-then-check).
- **Exact-then-prefix title resolution for `nx memory get`** (`src/nexus/db/t2/memory_store.py::resolve_title`, nexus-e59o). Library-level `(project, title) → (entry, candidates)`: exact match wins, falls back to unique prefix, returns ambiguous candidate list when more than one matches. CLI (`nx memory get --title`) and MCP tool (`memory_get`) both wired through it. LIKE wildcards (`%`, `_`) escaped so literal titles don't become glob patterns. Surfaced during the RDR-088 gate when a subagent couldn't retrieve `088-research-1` because the stored title was `088-research-1: RDR-092 baseline for Gap 4 spike` — the lookup required verbatim full title, and `nx memory list` + copy-paste was the only workaround.

### Fixed

- **T1 chroma leak on session-UUID rollover within a live Claude process** (`src/nexus/session.py::sweep_stale_sessions`, nexus-886w). The 4.10.3 three-layer defense (SessionEnd + watchdog + liveness sweep) covered ungraceful exits but not the `/clear` or `/resume` path: when the conversation UUID rolls but the claude process keeps running, the watchdog kept seeing `--claude-pid` alive and the sweep saw both `server_pid` and `claude_root_pid` alive. Old chroma process stayed orphaned until the 24h age threshold. Added a fourth reap trigger: `record.session_id != current_session_pointer` with `claude_root_pid` alive reaps the stale record as `reason=uuid_mismatch`. Specific-reason ordering preserved in the structured log — `age`, `server_dead`, `anchor_dead` still win when multiple triggers apply. Compact events are unaffected because compaction keeps the session UUID stable.

### Changed

- **Integration test surface** (`tests/integration/test_rdr_088_operator_pipelines.py`, new file). Two `@pytest.mark.integration` tests exercise the RDR-088 MVV (`search → filter → check`) and `traverse → check` pipelines live against `knowledge__delos`. Module-level marker auto-skips without claude auth + T3 credentials; assertions are shape-based (role enum, required keys, subset contract) so LLM output variance doesn't flake.
- **`_INPUTS_TARGET` hoisted to module scope** (`src/nexus/plans/runner.py`, nexus-4o2z). Previously defined inside `_hydrate_operator_args`, rebuilt on every operator step dispatch. Pure refactor; covered by existing `TestHydrateInputsTranslation` suite.

## [4.10.3] - 2026-04-23

Fixes a long-running leak in the per-session ChromaDB server lifecycle. On the reference install 103 orphaned chroma processes and 183 leftover tmpdirs had accumulated over 3 days because Claude Code SIGTERMs hook subprocesses at session close without waiting for them to finish. The `SessionEnd` hook was never completing its `stop_t1_server` call; the `|| true` safety valve never ran because the whole subprocess was killed, not exited non-zero. Documented upstream as [anthropics/claude-code#41577](https://github.com/anthropics/claude-code/issues/41577) (closed wont-fix). Related: [#17885](https://github.com/anthropics/claude-code/issues/17885) — SessionEnd doesn't fire at all on `/exit`. Three independent defense-in-depth layers land in this release; any one of them closes the leak on its own.

### Added

- **Self-watchdog sidecar** (`src/nexus/t1_watchdog.py`, new). Spawned detached alongside each per-session chroma server from `session_start()`. Polls the Claude Code root PID and the chroma PID every 5s via `os.kill(pid, 0)`. On `ProcessLookupError` for the claude root, triggers the existing `stop_t1_server` graceful-SIGTERM path (preserving the pgrp-wide signal that lets chroma's multiprocessing workers `sem_unlink` their POSIX named semaphores, which is the invariant from nexus-dc57 / nexus-ze2a). Independent of hooks firing at all, so it covers both the `/exit` path (#17885) and the `Hook cancelled` path (#41577). Worst-case leak window: 5s. Written using only stdlib so the hot loop has no import overhead.
- **`find_claude_root_pid`** (`src/nexus/session.py`). Walks the PPID chain looking for a command whose basename starts with `claude` (case-insensitive; covers `claude`, `claude-code`, future renames). Falls back to the immediate PPID when no match so the watchdog always watches something meaningful.
- **`spawn_t1_watchdog`** (`src/nexus/session.py`). Launches the watchdog via `python -m nexus.t1_watchdog` with `start_new_session=True` so it survives the SessionStart hook's exit. Failure is non-fatal; the other two layers pick up the slack.
- **`nx hook session-end-detach`** (`src/nexus/commands/hook.py`). Fire-and-forget SessionEnd runner using the canonical double-fork daemon pattern. First fork exits the hook in <50ms (Claude Code sees success and moves on). The child calls `os.setsid` to leave Claude Code's process group; the grandchild (second fork) redirects stdio to `/dev/null` and runs `session_end` synchronously. By the time Claude Code's shutdown SIGTERM arrives, the grandchild has been reparented to init and is no longer in the hook's pgrp, so the signal misses it. On platforms without `os.fork` (Windows), falls through to the synchronous path so behaviour degrades gracefully. This is the workaround the #41577 maintainers documented as the escape hatch.
- **Liveness-based reap in `sweep_stale_sessions`** (`src/nexus/session.py`). On SessionStart, a session record is now reaped when any of three triggers fires: legacy 24h age cutoff, `server_pid` no longer alive, or `claude_root_pid` anchor no longer alive. Previously only the age cutoff fired, which is why orphans could accumulate in the first place. With this in place, even a cold-start after days of drift cleans everything up on first session boot. The age fallback is retained for records that lack the new PID metadata.

### Changed

- **`write_session_record_by_id`** now optionally stores `claude_root_pid` and `watchdog_pid` alongside `server_pid`. Old records without these fields are still accepted — the reaper falls back to age-based sweep for them.
- **`nx/hooks/hooks.json`** SessionEnd command rewired from `nx hook session-end || true` (timeout 10s) to `nx hook session-end-detach || true` (timeout 5s). The lower timeout is correct because the hook returns fast by construction; the `|| true` is kept defensively.

### Fixed

- **Two existing `sweep_stale_sessions` tests** updated to use `os.getpid()` as the fake `server_pid` so the liveness check sees a live anchor and the tests exercise the intended age-only path.

## [4.10.2] - 2026-04-23

Shakeout patch for 4.10.1. The 4.10.1 release fixed three headline bugs; the shakeout against the reference install surfaced two more gaps in the operator-dispatch + plan-library path. Both fixed in PR #274.

### Fixed

- **Operator steps that consume pre-hydrated content via `inputs:` failed with `TypeError: missing 1 required positional argument`** (`src/nexus/plans/runner.py`, nexus-yis0). `_hydrate_operator_args` only renamed `inputs` to the operator's positional arg name (`content` for summarize, `context` for generate, `items` for rank/compare) inside the auto-hydration branch that fires when the operator step's args contain `ids`. Plans with an explicit `store_get_many` step followed by an operator that reads `$stepN.contents` (canonical repro: builtin plan 57 `find-by-author`) skipped the rename, `_default_dispatcher`'s unknown-kwarg drop stripped `inputs`, and the operator fired with no positional. Fix adds a dedicated rename pass after auto-hydration, with list-to-string normalization for summarize/generate and list-to-JSON for rank/compare. `operator_extract` keeps `inputs` unchanged (native arg). Bundle path uses a separate arg-presentation mechanism and was unaffected. Seven regression tests cover every rename direction plus the no-overwrite and extract-unchanged guards.

### Added

- **`_backfill_builtin_bindings` migration at 4.10.2** (`src/nexus/db/migrations.py`, nexus-uyc6). The 4.10.1 `seed_loader` fix merged YAML `required_bindings` / `optional_bindings` into `plan_json` at save time, but the seed loader short-circuits via `get_plan_by_dimensions` on existing rows. So every install that carried a builtin row seeded before 4.10.1 kept the old `plan_json` without binding declarations, and `_validate_bindings` still saw an empty list on upgraded installs. The migration selects `plans` rows tagged `builtin` whose `plan_json` does not contain `required_bindings`, resolves the shipping YAML directory via `importlib.resources` (wheel + editable install paths both covered, with a repo-root walk as fallback), indexes the YAMLs by `(verb, scope, strategy)` to match the stored dimensional identity, and patches the binding lists into the stored JSON. Idempotent via the `NOT LIKE '%required_bindings%'` pre-filter. Non-builtin rows (user ad-hoc plans) are untouched. Silent no-op when YAMLs are unreachable — `nx catalog setup`'s fail-loud guard is the escalation path for that case. Four regression tests cover dimensional match, idempotency, non-builtin skip, and registry presence at ≥ 4.10.2.

## [4.10.1] - 2026-04-23

Shakeout patch against 4.10.0, run live on the reference install immediately after tag. Three issues surfaced and all three fixed in one arc (PR #273): one pre-existing silent failure that 4.10.0's new telemetry surface made visible, one wiring gap the shakeout forced, and one cleanup migration that closes the gap RDR-092 Phase 0a left behind.

### Fixed

- **`nx_answer_runs` has been silently empty since the RDR-063 T2Database split** (`src/nexus/mcp/core.py`, nexus-598n). All five `_nx_answer_record_run` call sites passed `db.conn`, but the post-split facade only exposes per-domain connections (`db.memory.conn`, `db.plans.conn`, `db.taxonomy.conn`, `db.telemetry.conn`, `db.chash_index.conn`). Every insert raised `AttributeError` and was swallowed by the surrounding `except Exception: pass`, leaving the RDR-080 P1 run log permanently at zero rows despite every `nx_answer` call attempting to write. Routed through `db.telemetry.conn` — the run log is telemetry semantically, and the Telemetry store owns the table's migration. Caught during 4.10.0 shakeout when three live `nx_answer` calls populated `plans.use_count` / `success_count` correctly (4.10.0's wiring is intact) but left `nx_answer_runs` empty. Regression test `test_record_run_lands_via_t2_telemetry_conn` opens a real `T2Database`, writes via the documented path, and asserts the row lands — pinning the telemetry contract.
- **`seed_loader` dropped YAML `required_bindings` / `optional_bindings` on insert** (`src/nexus/plans/seed_loader.py`, nexus-80tk). `library.save_plan(plan_json=json.dumps(template["plan_json"]))` stored only the `steps` subtree. `Match.from_plan_row` then read `plan.get("required_bindings", [])` and got `[]` for every seeded plan, so `_validate_bindings` passed every call through regardless of caller input. Unresolved `$var` literals then slipped past `_resolve_value` (which preserves unknown vars as literals per its "required-binding validation runs upfront" contract) and ended up in operator prompts as raw `$criterion` / `$area` strings. The fix merges the top-level binding lists into the stored `plan_json` at save time: no schema change, no runner rework. Caught during shakeout when an `analyze-default` invocation reached the bundled `claude-p` subprocess with `criterion: $criterion` verbatim and the LLM responded that STEP 0 was undefined. Test `test_seed_loader_carries_required_bindings_into_plan_json` round-trips a YAML through the seeder + `Match.from_plan_row` and asserts both lists preserved.

### Added

- **`_retire_legacy_operation_shape_plans` migration at 4.10.1** (`src/nexus/db/migrations.py`, nexus-4m9b). RDR-092 Phase 0a retired the `_PLAN_TEMPLATES` seed array but did not migrate the five rows it had previously seeded. Those rows plus six older user ad-hoc plans (11 total on the reference install) carry step entries shaped like `{"step": 1, "operation": "X", "params": {...}}` rather than the RDR-078 `{"tool": "X", "args": {...}}` shape. `plan_run` cannot dispatch the legacy form; the rows pollute `plan_match` results and mask modern replacements (e.g. legacy `find-documents-author` out-ranking YAML builtin `find-by-author` during dimensional match). The migration pre-filters on the `"operation"` substring, then for each candidate requires at least one step dict has `operation` AND no step dict has `tool` before deletion. A modern plan whose args payload mentions the word `operation` (e.g. `purpose: reference-operation`) fails the second half and is preserved. Idempotent: second run finds no candidates. Generic: anyone upgrading past 4.10.0 picks up the same cleanup regardless of when their legacy rows originally landed. Four regression tests cover delete-only-legacy, idempotency, false-positive guard, and registry presence at ≥ 4.10.1.

## [4.10.0] - 2026-04-23

Ships operator bundling and the plan-use telemetry wiring. Contiguous runs of ≥2 operator steps (extract / rank / compare / summarize / generate) now collapse into a single `claude -p` subprocess instead of one spawn per step. Measured wins on real corpora: **-55%** on a 2-op `extract → summarize` chain, **-28%** on the blog-post Arcaneum tradeoffs plan (with materially better ranking quality), **-72%** on a 4-op cross-repo compositional query (192s → 54s, matched synthesis quality). Default is on (`bundle_operators=True`); the one-line escape hatch `bundle_operators=False` recovers per-step isolation for debugging. Separately, `plans.use_count` / `success_count` / `failure_count` finally populate — the `PlanLibrary.increment_run_*` methods existed since RDR-078 but had zero callers until this release. Skill wording for the five verb skills (`/nx:research`, `/nx:review`, `/nx:analyze`, `/nx:debug`, `/nx:document`) and `/nx:query` shifts from descriptive ("Routes through nx_answer") to imperative ("You MUST call nx_answer") to close the 6,537:0 audit deficit between direct `search` calls and `nx_answer` invocations.

### Added

- **Operator-bundle execution** (`src/nexus/plans/bundle.py`, new 500 LOC module). `segment_steps` walks a plan and emits `OperatorBundleSlice` markers for ≥2 contiguous operator runs; `compose_bundle_prompt` produces a single composite prompt describing all N steps with a plan-index → bundle-local-position map for cross-references; `dispatch_bundle` issues one `claude_dispatch` for the whole bundle. `BUNDLEABLE_OPERATORS` is the authoritative eligibility set with documented criteria (pure / cost-bounded / failure-meaningful-at-bundle-granularity). `MAX_BUNDLE_PROMPT_CHARS=200_000` guards against oversized composite prompts with per-step fallback. `DEFERRED_REF_KEY` is the shared sentinel for intra-bundle `$stepN.field` references.
- **`bundle_operators: bool = True` kwarg on `plan_run`** (`src/nexus/plans/runner.py`). New default collapses eligible segments via the bundle path; `False` flattens back to per-step dispatch. The runner routes through `segment_steps` as the sole bundle-boundary detector (no duplicate inline logic).
- **`supports_bundling` attribute on dispatchers** (`src/nexus/plans/runner.py::_default_dispatcher`). `plan_run` gates bundling on `getattr(dispatch, "supports_bundling", False)` instead of an identity check — decorators and wrappers either inherit or set their own marker, so timing wrappers and retry decorators don't silently disable bundling.
- **Deferred step-reference handling** (`src/nexus/plans/runner.py::_resolve_value`). Intra-bundle `$stepN.field` refs return a sentinel dict instead of raising; `compose_bundle_prompt` renders them as "STEP M output" prose. Covers the 7 of 20 bundle-eligible plans in the live library that chain between operators.
- **Parallel-branch source attribution** (`src/nexus/plans/bundle.py::OperatorBundleStep.source_collections`). Bundled operator steps whose inputs came from auto-hydration carry the pre-hydration `collections` arg into the prompt as a `source:` line, so the LLM can attribute parallel branches (e.g. Arcaneum extracts vs Nexus extracts in a cross-repo compare) to their originating corpora.
- **Plan-use telemetry wiring** (`src/nexus/mcp/core.py::_nx_answer_record_outcome`, `_plan_run` wrapping). `increment_run_started` fires before execution, `increment_run_outcome(success=bool)` fires after. Guarded on `best.plan_id > 0` to skip synthetic inline-planner matches. Errors swallowed locally so telemetry can never break the user-facing flow. Tests cover success-path, synthetic-match skip, and exception → `success=False` recording.
- **`scripts/bundle_sandbox_probe.py`** — reproducible live benchmark harness. `--runs N` (default 3) collects N samples per configuration and reports mean / stddev / min / max. `--dry-run` previews the composite prompt without spending API credit.
- **`scripts/rdr092_replay.py`** — plan-match validation harness. Clones `memory.db`, builds a fresh T1 cache, replays every plan's stored anchor + 10 synthetic noise probes; reports self-match rank distribution, confidence histogram, attractor counts, and noise-floor rejection rate.

### Changed

- **Verb skills (`research` / `review` / `analyze` / `debug` / `document`) and `/nx:query`** — wording flipped from descriptive to imperative. Each skill now has a "When direct `search` is fine" carve-out for single-corpus keyword lookups, and anti-patterns cite *composition* (not a blanket "analytical") as the deciding factor. The `using-nx-skills` common-mistakes table adds three mappings from bad `search(analytical-question)` shapes to correct `nx_answer` shapes (full `mcp__plugin_nx_nexus__` prefix throughout — no short-form syntax).
- **`_hydrate_operator_args` is now synchronous** (`src/nexus/plans/runner.py`). Previously declared `async def` with no `await` expressions. Removed the coroutine allocation per operator step and the misleading async contract.

### Fixed

- **`_DEFERRED_REF_KEY` is defined once** (`src/nexus/plans/bundle.py`), imported by `runner.py`. Previously duplicated across both modules with identical strings; a rename on one side would have silently corrupted bundle prompts.

## [4.9.13] - 2026-04-23

Ships RDR-092 (Plan Match-Text from Dimensional Identity) in full, plus the wheel-packaging deployment fix it surfaced. The plan library's T1 cosine cache and T2 FTS5 lane now both key off a hybrid `match_text` payload (`<description>. <verb> <name> scope <scope>`) instead of the raw `query` column. The R3-class state that motivated the RDR — 0/52 live plans with populated `verb` / `name` / `dimensions` — drains to 0 non-dimensional rows on `nx upgrade`. Phase 5 empirical validation confirmed both RDR targets: plan-#38 rank-1 attractor landings 4/10 → 1/10, noise probes above the 0.40 confidence floor 1/10 → 0/3. Version jumps 4.9.11 → 4.9.13 so both gated migrations (4.9.12 backfill and 4.9.13 match_text column) run on existing installs. Rolls up #266 / #267 / #268 / #269.

### Added

- **Hybrid `match_text` synthesiser** (`src/nexus/db/t2/plan_library.py::_synthesize_match_text`, `src/nexus/plans/session_cache.py`, RDR-092). Single source of truth that produces the description + dimensional-suffix string both lanes embed. `session_cache._synthesize_match_text` is a thin dict-unpacking adapter around the shared kwargs-based implementation. Shape: `"<description>. <verb> <name> scope <scope>"` when verb AND name are populated; raw description fallback when not. R10 validated the hybrid at zero verb-accuracy regression vs raw description.
- **`plans.match_text` column + `plans_fts` rebuild** (`src/nexus/db/t2/plan_library.py`, migration `_add_plan_match_text_column` at 4.9.13). Fresh installs get the column + FTS shape directly from `_PLANS_SCHEMA_SQL`; existing DBs pick it up via the migration, which drops + recreates `plans_fts` (FTS5 has no `ALTER COLUMN`) after backfilling `match_text` from existing verb/name/scope. Drop-before-backfill ordering avoids `database disk image is malformed` on external-content FTS updates. The column guard also checks `plans_fts` presence so an interrupted upgrade between `ALTER TABLE` and the FTS rebuild recovers on retry instead of silently landing empty FTS payloads.
- **Three-tier verb cascade on grown plans** (`src/nexus/mcp/core.py::_infer_grown_plan_verb`, `_infer_grown_plan_name`, RDR-092 Phase 0b). `nx_answer`'s ad-hoc grow path now populates `verb` / `name` / `dimensions` via (1) caller-supplied `dimensions["verb"]`, (2) operator-shape inference from `plan_json.steps` (compare → analyze; extract+rank → analyze; traverse+search+summarize → research), (3) `research` fallback. Name is kebab-case from the first 3-5 content tokens of the question with stop-words dropped.
- **`_backfill_plan_dimensions` migration at 4.9.12** (`src/nexus/db/migrations.py`, RDR-092 Phase 0d). Retroactively populates `verb` / `name` / `dimensions` / `scope` on every row with `dimensions IS NULL`. 29-stem verb dictionary (research / analyze / review / debug / document families) + wh-fallback catches edge cases. Within-loop collisions resolve via a deterministic row-id strategy suffix against an in-memory `claimed` set — reruns produce byte-identical identities so a rerun is a no-op. Low-confidence wh-fallback rows are tagged `backfill-low-conf` for operator review via `nx plan repair`.
- **`nx doctor --check-plan-library`** (`src/nexus/commands/doctor.py`, RDR-092 Phase 0c.2). Buckets plan rows into authored / backfilled / non-dimensional and reports the global-tier builtin count. Exits 1 when the builtin count falls below 9 (the pre-Phase-0a floor — partial installs on older plugin versions still pass). Non-dimensional rows surface a `nx plan repair` hint.
- **`nx plan repair` subcommand** (`src/nexus/commands/plan.py`, RDR-092 Phase 0d.2). Re-runs the backfill heuristic + lists `backfill-low-conf` rows with their inferred verb and original query text so operators can hand-correct edge cases. Idempotent.
- **Fail-loud loader in `nx catalog setup`** (`src/nexus/commands/catalog.py::_seed_plan_templates`, RDR-092 Phase 0c.1). Empty global tier during setup now raises `click.ClickException` instead of silently returning zero rows. Per-tier schema errors fan out to stderr via `click.echo`.
- **Three new YAML builtin plans** (`nx/plans/builtin/`, RDR-092 Phase 0a). `find-by-author.yml`, `citation-traversal.yml`, `type-scoped-search.yml` replace the three migrated shapes from the retired legacy `_PLAN_TEMPLATES` array. Each declares full `verb` / `scope` / `strategy` dimensions.
- **Per-call `min_confidence` override on `nx_answer`** (`src/nexus/mcp/core.py`, RDR-092 Phase 2 Option A). New `min_confidence: float | None` kwarg threads through both `plan_match` and the hit helper; default `None` preserves the RDR-079 P5 calibration (0.40). Verb skills that validated a stricter floor (0.50 per R9's 5+5 probe corpus) pin it per-call without moving the global knob. Bounds-checked: values outside `[0.0, 1.0]` fail loud.
- **Canary regression tests for the attractor behaviour** (`tests/test_plan_match.py::TestRdr092Canaries`). Locks in that random-token probes can't land rank-1 above the floor; positive-case companion for dimensional probes; telemetry-only concentration-ratio report.

### Changed

- **Retired the legacy `_PLAN_TEMPLATES` array** (`src/nexus/commands/catalog.py`, RDR-092 Phase 0a). The 5 hard-coded legacy templates were the upstream source of R3's 0/52 non-dimensional state. Three migrated to YAML; two (provenance, multi-corpus-compare) retired as redundant with `research-default` and `analyze-default`. Builtin count went 9 → 12 net.
- **Packaging: `nx/plans/` ships as wheel package data** (`pyproject.toml` `[tool.hatch.build.targets.wheel.force-include]`, `src/nexus/commands/catalog.py::_resolve_plugin_root`, nexus-b9f3). The wheel build now copies `nx/plans/` into `nexus/_resources/plans/`. `_resolve_plugin_root` resolves via `importlib.resources.files('nexus') / '_resources'` first (works for both wheel and editable installs via a `src/nexus/_resources/plans` symlink), with repo-root and legacy `__file__` walk as fallbacks. Before this fix, installed CLIs couldn't find the YAMLs from any cwd outside the nexus repo — the Phase 0c fail-loud guard exposed the pre-existing gap.
- **Docs: `docs/plan-authoring-guide.md`, `docs/plan-centric-retrieval.md`, `docs/cli-reference.md`** (RDR-092 Phase 4). New "match_text synthesis" section, builtin-templates table bumped 9 → 12, retrieval-trunk diagram updated for the `min_confidence` override. cli-reference documents `nx doctor --check-plan-library` and the new `nx plan` command group.
- **Test suite tightening** (`tests/test_bib_enricher.py`, `tests/test_doctor_search.py`, `tests/test_phase3_structured_chash.py`, #269). Patched `time.sleep` in the 429-backoff test (35s → 0.05s), mocked `claude_dispatch` in two `nx_answer` structured-envelope tests (84s → 0.72s combined), marked `test_flag_invokes_probe` as `@pytest.mark.slow` (122s deselected by default; opt-in via `-m slow`). Full unit suite: 12:31 → 8:02.

### Fixed

- **RDR-092 Problem Statement retrofit** (`docs/rdr/rdr-092-plan-match-text-from-dimensional-identity.md`). Added four `#### Gap N:` structured headings so the RDR-065 close gate's pointer-replay validation runs in one pass instead of blocking on malformed-new. Content unchanged; headings organise the existing prose.

## [4.9.11] - 2026-04-23

Plugin-side hardening. Shifts the `#### Gap N:` structural requirement in RDR Problem Statements from `/nx:rdr-close` (detected at close time) to `/nx:rdr-gate` (detected at gate time, before accept) so authors meet the rule before it bites. Closes the surprise-at-close pattern that bit RDR-091 on 2026-04-22: the RDR accepted cleanly without gap headings, then failed the close preamble, and the gap structure had to be retrofitted after the fact.

### Added

- **`/nx:rdr-gate` Layer 1 gap-structure check** (`nx/commands/rdr-gate.md`, `nx/skills/rdr-gate/SKILL.md`, `nexus-4qpb`). For RDRs with `id >= 65`, the preamble now validates the `## Problem Statement` section contains at least one `#### Gap N: <title>` heading. Missing headings emit a BLOCKED outcome with the same error and regex the close skill uses; the assistant stops at Layer 1 without running the assumption audit or AI critique. Legacy RDRs (`id < 65`) are grandfathered. `--skip-gaps` bypasses the check for rare RDRs where the structure does not fit; the override is recorded in the gate audit trail.

### Changed

- **`/nx:rdr-create` template and SKILL both reinforce the gap convention** (`nx/resources/rdr/TEMPLATE.md`, `nx/skills/rdr-create/SKILL.md`). Template comment now explicitly names both gate enforcement points (`/nx:rdr-gate` and `/nx:rdr-close`) so authors see "missing gaps will block the gate" at drafting time, not just at close. The Gap 1 + Gap 2 placeholders remain in place.

### Fixed

- **RDR-091 retrofitted with the gap heading it was drafted without** (`docs/rdr/rdr-091-scope-aware-plan-matching.md`). The RDR was drafted and accepted before gate-time enforcement landed, so the close skill flagged the missing structure after all the Phase 2a–2d work had already shipped. Added `#### Gap 1: plan_match silently drops scope_preference, so specialized plans lose to generic ones on scoped questions` to the Problem Statement and flipped frontmatter `status: accepted` → `status: closed` with `closed_pointers: Gap1=src/nexus/plans/matcher.py:77` for audit trail.

## [4.9.10] - 2026-04-23

Post-4.9.9 hardening — five GitHub issues filed on 2026-04-23 from the 4.9.9 shakeout (#249–#253), all observability or UX fixes to the boundaries between the catalog, taxonomy, and storage tiers. No user-facing behaviour changes on the `store_put` / `taxonomy-assign` / `split` paths; they just leave evidence now when something goes sideways. Rolls up as PR #254.

### Added

- **`nx catalog verify`** (GitHub #249, `src/nexus/commands/catalog.py`, `src/nexus/db/t3.py`). Reconciliation sweep that cross-checks every catalog tumbler against its target T3 collection to surface ghost tumblers — entries in the catalog whose `doc_id` has no matching row in ChromaDB. #244 closed the new-ghost source on the 4.9.9 put path, but latent ghosts from 4.9.7 / 4.9.8 installs still survived — discovered only when a user hit `store_get`. `verify` groups tumblers by `physical_collection` and batches `col.get(ids=[...], include=[])` at the 300-id cap for an ANN-less presence check. Missing collections (deleted, renamed) count every tumbler as a ghost. `--heal` walks ghosts interactively (`[d]rop tumbler / [p]rint put-cmd template / [s]kip / [q]uit`), `--json` emits a machine-readable map, `--collection NAME` scopes the sweep. Tumblers without `meta.doc_id` are unverifiable and skipped rather than reported as false positives. New T3 primitive `existing_ids(collection, ids)` backs the check (paginated, `include=[]`, missing collection → empty set). (nexus-23l3)
- **`nx taxonomy status` surfaces recent post-store hook failures** (GitHub #251, `src/nexus/db/migrations.py`, `src/nexus/mcp_infra.py`, `src/nexus/commands/taxonomy_cmd.py`). New `hook_failures` T2 table (`id`, `doc_id`, `collection`, `hook_name`, `error`, `occurred_at`) captures every exception `fire_post_store_hooks` catches — previously visible only in structlog output. `status` emits an `Action: N post-store hook failure(s) in the last 24h` line matching the v4.9.9 #239 / #243 pattern, so a silently-dropped `taxonomy_assign_hook` write (missing centroids, Chroma timeout) becomes actionable instead of a silent log smudge. Drop-and-warn policy preserved — the T3 row stays source of truth and the hook exception is never raised; this just makes the drops queryable. New `_record_hook_failure()` persist helper is best-effort (inner `try/except` falls through to a debug log on failure so a broken T2 never masks the primary hook exception). Read path is also best-effort — pre-4.9.10 DBs stay silent rather than blowing up. Migration registered at 4.9.10 and activates automatically with this release. (nexus-4dkf)
- **`nx doctor --check-taxonomy`** (GitHub #252, `src/nexus/commands/doctor.py`). Verifies the `topic_links ≡ projection-assignment` invariant that #240 maintained via single-caller discipline (`_persist_assignments` was the only caller of `refresh_projection_links`). Drift SQL finds topics with `assigned_by='projection'` rows but no entry in `topic_links`; exits 0 with count when the invariant holds, exits 1 with up to 10 named topics and a `Fix: nx taxonomy project --backfill --persist` hint on drift. Closes the detection gap without refactoring the domain layer — a future caller that writes projection assignments through `assign_topic` directly, or a test fixture that seeds rows, will be caught before the materialized view goes stale in production. (nexus-xhf1)
- **`nx taxonomy split` prints a next-step hint pointing at `label`** (GitHub #250, `src/nexus/commands/taxonomy_cmd.py`). `split_topic` creates children with `review_status='pending'` and n-gram labels; users previously got no signal that the documented `split → label → review` workflow still needed the labeler run. After a non-zero split, echo `Action: N new sub-topics have n-gram labels. Run nx taxonomy label -c <coll>` — collection scope resolves from `--collection` or the parent topic row so the hint stays precise even when the user resolves the topic by label alone. Matches the status-hint pattern v4.9.9 adopted. No hint on no-op splits (`child_count=0`). (nexus-ir2g)

### Fixed

- **MCP `store_put` silently swallowed `_catalog_store_hook` failures** (GitHub #253, `src/nexus/mcp/core.py`). A bare `try/except: pass` around the catalog-registration call in the MCP `store_put` wrapper hid every hook exception — schema-lock contention, corrupt catalog db, import errors — producing the inverse-of-#244 orphan shape: T3 row present, no catalog tumbler, nothing surfaces. Matches the `fire_post_store_hooks` pattern now: `catalog_store_hook_failed` warning with `doc_id`, `collection`, and `exc_info=True`. Policy stays non-fatal — T3 is source of truth — but failures are now observable. `nx catalog verify` (#249 above) can detect the inverse orphan shape from the T3 side if it becomes a recurring issue. (nexus-tsyg)

## [4.9.9] - 2026-04-22

Rolls up the kxez (GitHub #243 + #241) and gwhy (#238 / #239 / #240 + review-response) taxonomy fixes that were merged to `main` after the v4.9.8 tag was cut; they did not ship in the 4.9.8 PyPI artifact. Plus two bugs found during the 4.9.8 shakeout, plus three small `nx catalog` CLI polish items that had been sitting as open beads since 2026-04-19 (below).

### Added

- **`nx catalog links --resolve`** (`src/nexus/commands/catalog.py`, bead `nexus-i63n` rolled into `nexus-iojz`). Default output is raw tumblers (`1.17.14 → 1.1.107`), which is unreadable for external audiences and still clunky when project-hacking. `--resolve` renders each endpoint inline as `<title-or-path> (<tumbler>)` using the existing `catalog.resolve()` entry lookup: prefer `title`, fall back to `file_path`, then bare tumbler.
- **`nx catalog links --unique-targets`** (`src/nexus/commands/catalog.py`, bead `nexus-x6eu` rolled into `nexus-iojz`). Collapses edges that point at the same `file_path` via different owner tumblers (the shape re-indexing after owner-rename produces). Stable first-seen-wins; fails open to bare tumbler if the target does not resolve. Workaround for the deeper re-index dedup gap, not a replacement for it.
- **`nx catalog stats` now includes a topics block** (`src/nexus/commands/catalog.py`, bead `nexus-1n0t` rolled into `nexus-iojz`). Previously reported owners / documents / links / by_type / by_link_type; the catalog's third layer (`CatalogTaxonomy`) was silent. New section shows total assignments, distinct topics assigned, and projection breakdown by source collection. `--json` carries the same data under a top-level `taxonomy` key. Skipped quietly when T2 is absent or carries no topic rows.

### Fixed

- **`store_put` silently dropped oversized documents, leaving catalog ghosts** (GitHub #244, `src/nexus/db/t3.py`, `src/nexus/errors.py`). `_write_batch` dropped-and-warned any document over the 16384-byte ChromaDB Cloud cap and returned normally. `put()` then returned the computed deterministic `doc_id`, and the MCP `store_put` wrapper went on to register that `doc_id` in the nexus-catalog. Result: a catalog entry with `physical_collection` + `doc_id` pointing at a row that was never written (three reproduced cases in the filing session). New `PutOversizedError` is raised on the put path (`fail_on_oversized=True`); indexer batch paths keep the existing drop-and-warn behaviour because a chunker upstream was already supposed to guard against this and a pipeline-wide raise is worse than dropping one record. The caller of `put()` now sees the error and skips catalog registration, so no ghost is created. Workaround for existing ghosts: re-run `store_put` with the same `title` and in-budget content, since `doc_id` is deterministic (`sha256(collection:title)[:16]`); the live row attaches to the existing catalog tumbler. (nexus-akof)
- **Inline planner generated `query(..., author='arcaneum')` filters that returned zero** (bead `nexus-sgrg`, `src/nexus/mcp/core.py`). The LLM planner saw the `author=""` parameter on the `query` tool and emitted `author=<repo-name>` as a plausible scope filter. The catalog's `author` column is rarely populated for RDR / docs collections, so the filter almost always matches nothing, extract gets an empty input, and generate honestly reports "no synthesis produced." `_PLANNER_TOOL_REFERENCE` now documents that `author=` is rarely populated and biases callers toward `corpus=<collection>` for project scoping. (nexus-sgrg)
- **`nx taxonomy label` silently skipped split sub-topics** (GitHub #243, `src/nexus/commands/taxonomy_cmd.py`, `src/nexus/db/t2/catalog_taxonomy.py`). The pre-check and the `--all` relabel path both used `get_topics()` which returns only root topics (`parent_id IS NULL`). After a `nx taxonomy split`, `status` correctly reported the new children as pending, but `label` reported "No topics to label" and the documented `split → label → review` workflow silently stalled with cryptic n-gram labels on the sub-topics. New `get_all_topics()` helper returns roots + children; both the pre-check and the batch relabeler now use it. (nexus-kxez)
- **`nx taxonomy label` auto-accepted topics** (GitHub #241 Item 3, `src/nexus/commands/taxonomy_cmd.py`, `src/nexus/db/t2/catalog_taxonomy.py`). `rename_topic` sets `review_status='accepted'` as a side effect (correct for the interactive `review` rename path where the human is acknowledging the label). The batch LLM relabeler was also calling it, short-circuiting the documented `pending → review → accepted` flow so `status` showed topics as "accepted" with no human ever having seen them. New `update_topic_label()` helper updates the label without touching status; `relabel_topics` now uses it. After this fix, `label` leaves topics `pending`, and `review` is the step that transitions them to `accepted`. (nexus-kxez)
- **`nx taxonomy list` hid collection membership** (GitHub #241 Item 1, `src/nexus/commands/taxonomy_cmd.py`). The flat topic dump with no collection column made it impossible to tell which topic belonged to which collection on multi-collection setups. Root rows now carry a `[collection]` prefix, consistent with the format `hubs` and `links` already use. (nexus-kxez)
- **`nx taxonomy project --help` threshold resolution text was stale** (GitHub #241 Item 2, `src/nexus/commands/taxonomy_cmd.py`). Docstring said "fallback to prefix default → 0.70" but actual defaults are per-prefix (0.70 code, 0.50 knowledge, 0.55 docs / rdr). Replaced with a pointer to the `--threshold` option help for the real table. (nexus-kxez)
- **`nx taxonomy project <src>` narrowed targets vs `--backfill` for the same source** (GitHub #238, `src/nexus/commands/taxonomy_cmd.py`). Single-source tried `list_sibling_collections` (same-hash, different-prefix) first and fell back to all-collections only when siblings were empty; `--backfill` always used the full set. For multi-repo families (e.g. three `docs__<repo>-<hash>` from distinct projects), single-source targeted fewer collections than `--backfill` on the same source. Dropped the sibling-first heuristic. Default is now every collection with topics minus the source, matching `--backfill`. Use `--against` to scope explicitly when the default is too wide. (nexus-gwhy)
- **`nx taxonomy status` didn't surface zero-projection collections** (GitHub #239, `src/nexus/commands/taxonomy_cmd.py`, `src/nexus/db/t2/catalog_taxonomy.py`). A collection fresh from `discover` has own-collection topics but no cross-collection projection data; previously the status default output showed the collection as healthy and the gap was only visible via `audit -c <coll>`. Status now queries projection-assignment counts per collection, flags rows with topics but zero projection inline as `[no projection]`, and surfaces an `Action:` hint naming the concrete `project --persist` invocation. New `get_projection_counts_by_collection()` T2 helper backs the query. (nexus-gwhy)
- **`nx taxonomy rename` silently transitioned topics to `accepted`** (code-review finding M-1, `src/nexus/commands/taxonomy_cmd.py`). The standalone `rename` command called `rename_topic`, which atomically renames + accepts. Correct for the interactive `review` rename path, but surprising for a user fixing a typo on a still-pending topic. New `--no-accept` flag uses `update_topic_label` instead; default behaviour preserved (typing a new label is an acknowledgement). (nexus-gwhy)
- **`refresh_projection_links` released the taxonomy lock between its aggregate SELECT and per-pair upserts** (code-review finding C-1, `src/nexus/db/t2/catalog_taxonomy.py`). A concurrent `assign_topic` or `taxonomy_assign_hook` firing in the gap could change `topic_assignments` between the two phases, yielding stale `link_count` values. Hoisted both phases into a single `with self._lock:` block. (nexus-gwhy)
- **`nx taxonomy status` missing-projection `Action:` count could under-report under `-n`** (code-review finding C-2, `src/nexus/commands/taxonomy_cmd.py`). The count was computed from the already-truncated `rows` slice rather than the full `all_topics` universe, so `status -n 5` on a 20-collection install named at most the top-5 by `doc_count`. Count is now computed from the unfiltered universe. (nexus-gwhy)
- **`nx taxonomy links` stayed stale after `project --persist`** (GitHub #240, `src/nexus/commands/taxonomy_cmd.py`, `src/nexus/db/t2/catalog_taxonomy.py`). `project --persist` wrote per-chunk projection rows to `topic_assignments` but never updated `topic_links`, so `links` (which reads the cache) disagreed with `hubs` (which queries live). New `refresh_projection_links()` T2 method aggregates per-chunk projection rows into canonical `(from_topic_id, to_topic_id, count)` pairs and upserts them into `topic_links` with `'projection'` merged into any existing `link_types` set (so catalog-derived `cites` / `implements` survive). Called at the end of `_persist_assignments`, so single-source `project --persist` and `--backfill --persist` both leave `links` in sync with `hubs`. (nexus-gwhy)

## [4.9.8] - 2026-04-22

### Added

- **`operator_compare` gains two-sided mode** (`src/nexus/mcp/core.py`). Plans that need to compare extractions from two separate corpora (a cross-corpus DAG like "how does project A frame X vs how does project B frame X") can now pass `items_a` / `items_b` / `label_a` / `label_b` as independent args. The resolver substitutes each `$stepN.<field>` reference at top level, which the previous one-sided `items` arg could not do (the no-inline-interpolation rule means references embedded in a `focus` string stay literal). The two-sided prompt asks for shared axes, divergent decisions, side-only axes, and philosophy difference. One-sided `items` mode preserved unchanged; list / dict values in any of the `items*` args are now JSON-serialized before prompt interpolation so the LLM sees clean JSON instead of Python repr. Live cross-corpus run against Arcaneum + Nexus RDR corpora produced a clean synthesis (shared-axes table, divergent-decisions table, philosophy paragraph) with zero training-data fill-in. (nexus-km5i)

### Fixed

- **`catalog_search` rejected `content_type` as a sole filter** (`src/nexus/mcp/catalog.py`). The structured-filter trigger condition (`if owner or corpus or file_path or (author and not query)`) omitted `content_type`, so a sole `content_type` value fell through to the FTS5 path which requires a free-text query. Documented behaviour was wrong for callers asking "show me everything of type prose" without a search term. Added `content_type` to the trigger; pre-existing structured-filter SQL already handled `content_type = ?` correctly. (nexus-3o3t)
- **`store_list --docs=true` showed `?` for chunk count on every entry** (`src/nexus/mcp/core.py`). Per-doc `chunk_count` was read from chunk metadata, but `store_put` doesn't set that field — only the PDF indexer does. The dedup pass now derives the chunk count per content-hash; the page-count column is omitted entirely when no document carries one (was always `?p` for non-PDF entries). (nexus-3o3t)
- **`store_get` failed silently when given a title** (`src/nexus/mcp/core.py`). `store_list` displays titles; `store_put`/`search` return hashes; the MCP tool docstring promised hashes but the natural `list → get` copy-paste flow was broken. Now: try the input as a hash first; if not found and it doesn't look like a 16-char hex hash, fall back to `find_ids_by_title()`; on multi-match, list candidate hashes; on miss, error message names both the hash and title paths. Also surfaces the 16-char content-hash in the `--docs=true` listing so a hash is always within copy-paste reach. (nexus-3o3t)

### Changed

- **`T1Database.get` and `_reconnect` emit structured diagnostic logs** (`src/nexus/db/t1.py`). A user-reported `scratch put` → `scratch get` round-trip failure (`Not found` on the freshly-returned UUID) couldn't be reproduced in isolation. Added `t1_get_miss` (with requested_id, session_id, client_type, dead) and `t1_reconnect_to_different_server` (with prior + new session_id and host:port) so the next occurrence carries enough context to diagnose. No behaviour change. (nexus-3o3t)

## [4.9.6] - 2026-04-22

### Added

- **RDR-091: Scope-Aware Plan Matching.** `nx_answer`'s `scope` parameter was accepted and documented but silently ignored on the library-match path: when `plan_match` hit a saved plan, the plan's own corpus arg (if any) was used verbatim, so scoped calls could end up searching unrelated corpora. Phase 1 (`src/nexus/plans/runner.py`) now injects the caller's scope into retrieval step args when the plan does not pin a corpus. Phase 2 (`src/nexus/plans/matcher.py`, `src/nexus/plans/scope.py`) adds a `plans.scope_tags` column (4.8.0 migration), inference from retrieval-step `corpus` / `collection` args, and a scope-conflict filter + scope-fit boost + specificity tie-break in the matcher. `plan_save` MCP tool gains an optional `scope_tags` kwarg; `plan_search` output now surfaces the stored scope tag. Score formula is multiplicative per RDR spec: `final_score = base_confidence * (1 + scope_fit_weight * scope_fit)` with `scope_fit_weight = 0.15`. Empty `scope_preference` is a hard no-op (no boost, no filter). See `docs/plan-authoring-guide.md` §`scope_tags` and `docs/plan-centric-retrieval.md` §Scope-aware matching. (nexus-zs1d, nexus-x6pr, nexus-bgs7, nexus-svcg, nexus-jvma)

### Fixed

- **`"all"` corpus sentinel was inferred as a concrete scope tag**, filtering the seven builtin plans that use `corpus: all` out of every scoped `nx_answer` call. `_infer_scope_tags` now skips the sentinel alongside `$var` placeholders. A 4.8.1 rewash migration cleans up rows contaminated by the pre-fix backfill. (`src/nexus/plans/scope.py`, `src/nexus/db/migrations.py`, nexus-dfok)
- **`scope_tags` backfill overwrote explicit values on every process start.** The migration now guards `WHERE scope_tags = ''` so plans authored with `save_plan(scope_tags='rdr__arcaneum')` survive across MCP server / CLI restarts. (`src/nexus/db/migrations.py`, nexus-dfok)
- **Grown plans from scoped `nx_answer` calls were always agnostic.** `_infer_scope_tags` cannot see the runtime `_nx_scope` corpus injection (that lives in bindings, not `plan_json`). The grown-plan save path now passes `scope_tags=scope` explicitly so each grown plan is anchored to the retrieval space that produced it. (`src/nexus/mcp/core.py`, nexus-dfok)
- **Case-sensitive `scope_tags` prefix match surprised callers passing the ChromaDB-conventional lowercase scope** when real collections carried mixed case (`code__Delos-5af9bfe0` alongside `knowledge__delos`). `_scope_fit` now folds both sides before comparison; `_HASH_SUFFIX_RE` accepts upper-hex too. Stored values preserve original case; only the compare is case-folded. (`src/nexus/plans/matcher.py`, `src/nexus/plans/scope.py`, nexus-yi7m)

### Changed

- **`Match` dataclass gains a `scope_tags: str = ""` field** populated from the new column. All existing `Match(...)` callers keep working because the field is defaulted. (`src/nexus/plans/match.py`)
- **Scope-tag helpers (`_normalize_scope_string`, `_infer_scope_tags`, `_SCOPE_AGNOSTIC_SENTINELS`) moved out of `plan_library.py`** into a standalone `src/nexus/plans/scope.py` module, breaking a `migrations -> plan_library -> migrations` circular import path. Re-exported from `plan_library` for backward compatibility. (code-review follow-up)

### Docs

- **`docs/plan-authoring-guide.md`** gains a `scope_tags (matcher routing)` section covering inference vs explicit, normalization contract, matching semantics, multi-corpus bridging plans, interaction with grown plans, and authoring guidance. Clearly distinguishes `scope_tags` (matcher routing) from the `scope` dimension (publication tier).
- **`docs/plan-centric-retrieval.md`** gains a `Scope-aware matching` section covering filter / boost / tie-break mechanics, zero-candidate fallback to the inline planner, and prefix semantics (bidirectional `startswith`, intersect rules for multi-corpus plans). Quotes the final `scope_fit_weight=0.15` value.
- **`docs/rdr/rdr-091-scope-aware-plan-matching.md`** is the design record. `implementation_notes:` frontmatter records the picked weight, the multiplicative-formula correction history, and the critic follow-up.

## [4.9.5] - 2026-04-21

### Changed

- **`nx doctor` `Local collections` line now reports the empty-collection count** (`src/nexus/health.py`). After deleting every doc from a collection, `nx collection list` continues to show the collection at `0 chunks` because the empty collection is intentionally retained — it preserves the embedding-model binding so the next `store_put` doesn't have to re-derive it. The absence of a doctor signal made this look like a leak. Output now reads `Local collections: N collections (including K empty), <size> on disk`; the `(including K empty)` clause is omitted when `K == 0`. Pure transparency — no behavior change. (nexus-obp2)

## [4.9.4] - 2026-04-20

### Fixed

- **`nx store delete` left the catalog entry visible until the next `nx catalog gc`** (`src/nexus/commands/store.py`). The MCP `catalog_links` tool already filtered deleted-endpoint links immediately, so the eventual-consistency gap surprised users who expected delete to be atomic. After the T3 delete succeeds, look up each doc by `meta.doc_id`, tombstone the catalog row, and remove it from SQLite. Best-effort: silently skips when the catalog is uninitialised. (nexus-43pq)
- **`nx scratch get` rejected the 8-char prefix that `nx scratch list` printed** (`src/nexus/commands/scratch.py`). `delete` already accepted the prefix; `get` required the full UUID, breaking the natural `list → get <prefix>` copy-paste flow. Extracted `_resolve_entry_id()` from `delete_cmd` and reused it in `get`. Ambiguous prefixes still error with the candidate count. (nexus-43pq)
- **`nx collection info` reported the cloud Voyage model name in local mode** (`src/nexus/commands/collection.py`). `embedding_model_for_collection()` always returns the Voyage tag (its docstring even says "callers in local mode bypass this"); collection info forgot to. Now branches on `is_local_mode()` and reports `<minilm-or-bge> (local)` when local. Prevents callers from trusting the collection's self-reported model and reindexing with an incompatible embedder. (nexus-43pq)
- **`nx search` printed `:0:<content>` for results without a `source_path`** (`src/nexus/formatters.py`). The `path:line:content` format gracefully degrades to empty path + line 0 for `knowledge__*` / `docs__*` entries that aren't file-backed. `format_plain` now falls back to the MCP-style `[distance] title\n  snippet` format when no source path is present. (nexus-43pq)
- **`nx doctor` printed `Fix:` under passing (`✓`) checks** (`src/nexus/health.py`). The `Embedding model: all-MiniLM-L6-v2 (384d)` line carried `Fix: Upgrade: pip install conexus[local]…` even though nothing was broken. Renamed the prefix to `Suggest:` for `r.ok=True` results; `Fix:` is reserved for actual failures. (nexus-43pq)
- **`nx doctor` reported `T1 sessions: N session file(s), no orphans detected` even when sessions belonged to dead Claude Code instances** (`src/nexus/health.py`). The orphan check only inspects whether the chroma server PID is alive; long-lived chroma servers from prior conversations are technically "live" by that definition. Output now lists each session file with `(pid <N> alive, age <H>m/h)` so the state is transparent — and the failure message clarifies that "orphan" means the chroma pid is dead. (nexus-43pq)

### Changed

- **`nx store --help` tagline** updated from `(ChromaDB Cloud + Voyage AI)` to `(local ChromaDB or Cloud + Voyage AI)`. Local mode is the zero-config default; the prior tagline misled fresh installs. (nexus-43pq)
- **MCP `scratch` list / search** display now uses the same 8-character UUID prefix as the `nx scratch list` CLI (`src/nexus/mcp/core.py`). Previously the MCP surface showed 12 chars while the CLI showed 8, complicating copy-paste between the two. (nexus-43pq)

## [4.9.3] - 2026-04-20

### Fixed

- **Nested operator subprocesses stomped the parent's `current_session` flat file** (`src/nexus/hooks.py`, `src/nexus/operators/dispatch.py`, `src/nexus/db/t1.py`). After a parent Claude session ran any tool that fired `claude_dispatch` (operator_summarize, operator_generate, etc.), the subprocess's `SessionStart` hook unconditionally rewrote `~/.config/nexus/current_session` with its own transient UUID. The subprocess wrote no on-disk session record (skip-T1 path), so the parent's shell-side `nx scratch` / `nx memory` then resolved to a ghost UUID, found no record, and silently fell back to EphemeralClient for the rest of the conversation. Three coordinated changes resolve it: `claude_dispatch` exports `NX_SESSION_ID=<parent-uuid>` in subprocess env (populates the discriminator); `session_start` honours `NX_SESSION_ID` by preferring it as the resolved `session_id` and skipping the `write_claude_session_id()` call (preserves the parent pointer); `T1Database.__init__` short-circuits to EphemeralClient under `NEXUS_SKIP_T1` without searching for a session record (otherwise the operator would inadvertently connect to the parent's T1 server). Stateless-operator semantics preserved; cross-conversation T1 contamination eliminated.

### Added

- **Plugin↔CLI version drift detection at MCP server startup** (`src/nexus/mcp_infra.py`, `nx/.mcp.json`). The plugin and CLI ship from one `pyproject.toml` (CI enforces marketplace.json parity) but the user runs two separate update commands — `uv tool upgrade conexus` and `/plugin update nx@nexus-plugins`. After drift, the plugin's hooks may invoke flags the CLI no longer recognises. Extended `check_version_compatibility()` (already called from each MCP server's `main()` for CLI ↔ T2 schema drift) with a second case: read `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json`'s `version` field, compare against `importlib.metadata.version("conexus")`, log `plugin_cli_version_mismatch` warning on minor or major divergence with the actionable update hint for the lagging side. Patch-level drift ignored (within-minor releases are wire-compatible). Never blocks startup. The MCP server is the natural single binding point — `nx-mcp` and `nx-mcp-catalog` are conexus entry points; plugin/CLI coupling runs entirely through that surface. Modelled on JupyterLab/VSCode's runtime-recheck-on-every-load pattern. `nx/.mcp.json` gains an explicit `env: {"CLAUDE_PLUGIN_ROOT": "${CLAUDE_PLUGIN_ROOT}"}` block so the spawned MCP server sees the variable as an env var (not just a path-substitution token).

## [4.9.2] - 2026-04-20

### Fixed

- **T1 scratch was bound to the terminal session, not the Claude conversation** (`src/nexus/session.py`, `src/nexus/hooks.py`, `src/nexus/db/t1.py`). Two `claude` invocations in the same shell shared one T1 server because `session_start` walked the PPID chain looking for "the ancestor session file" and landed on the login shell. The Claude conversation UUID was already arriving via the SessionStart hook payload but was stored only *inside* each session record, never as the *filename*. Fix: drop the PPID walk; key session files on `{session_id}.session`; resolve the UUID via the existing `current_session` flat file (subagent inheritance) or `NX_SESSION_ID` env var (opt-in). Subagents within one conversation continue to share the parent's T1; two parallel conversations now correctly get distinct T1s. Migration: numeric-stem session files (legacy PID-keyed) are swept unconditionally on first new-code SessionStart.
- **T1 chroma server killed immediately after spawn (regression from 4.9.1)** (`src/nexus/session.py`). The `atexit.register(stop_t1_server, proc.pid)` added in 4.9.1 as a "defence-in-depth fallback" was wrong: it ran inside the short-lived `nx hook session-start` process, which exits within seconds of spawning chroma. atexit then killed every chroma server right after spawn — production T1 silently fell back to EphemeralClient on every Claude conversation since 4.9.1 shipped. Fix: remove the atexit registration. The chroma server is meant to outlive the hook process; cleanup belongs to the SessionEnd hook (kept). Ungraceful exits leak the server until the next SessionStart's `sweep_stale_sessions` reaps it — the pre-4.9.1 design.
- **`nx doctor` did not detect missing Node.js / `npx`** (`src/nexus/health.py`). The plugin's `sequential-thinking` and `context7` MCP servers are spawned via `npx -y …` and silently fail without `npx` on PATH. Added a non-fatal `npx (Node.js, plugin-only)` line to `_check_tools()` with `nodejs.org` install hints. CLI users without the plugin keep `exit 0`.

### Added

- **`NEXUS_SKIP_T1` env var** (`src/nexus/hooks.py`, `src/nexus/operators/dispatch.py`). Opt-out for callers that don't want a T1 chroma server spun up for short-lived `claude -p` invocations. `claude_dispatch` (every operator + the taxonomy labeler) now exports it in the subprocess env, so per-call chroma startup overhead drops to zero. T1 client falls back to EphemeralClient when no server record is found — correct semantics for stateless operator subprocesses.
- **`scripts/sandbox-t1-uuid.sh`** — E2E shell harness that exercises the full SessionStart/SessionEnd lifecycle against real chroma servers in an isolated `NEXUS_CONFIG_DIR`. 20 checks cover: distinct UUIDs → distinct servers, subagent inheritance via same UUID, `NEXUS_SKIP_T1` honoured, legacy migration sweep, SessionEnd cleanup. Caught the 4.9.1 atexit regression.

## [4.9.1] - 2026-04-20

### Fixed

- **T1 ChromaDB child process leak** (`src/nexus/session.py`). The `SessionEnd` plugin hook removed in v1.10.1 with the reasoning *"T1 server stops with process tree; hook was a no-op"* was load-bearing — chroma is intentionally spawned with `start_new_session=True` (so `safe_killpg` reaches its multiprocessing workers and avoids POSIX-named-semaphore exhaustion; beads `nexus-dc57` / `nexus-ze2a`), which also detaches it from the terminal's process group, so OS-level reaping never collects it. Symptom on the maintainer's machine: 43 leaked `chroma run …nx_t1_*` processes accreting over 2+ days, plus 51 orphan tmp dirs in `/var/folders/.../T/nx_t1_*`. The plugin-side hook re-registration ships in the `nx` plugin (see `nx/CHANGELOG.md`). The `nexus` Python package adds an `atexit.register(stop_t1_server, pid)` fallback in `start_t1_server` for cases where the hook can't fire (harness teardown cancels the hook, OOM, terminal SIGHUP swallowed by the new-session boundary). Idempotent against already-dead PIDs. Two new regression tests pin both halves of the fix.

## [4.9.0] - 2026-04-19

### Added

- **`nx index --debug-timing` — prose and PDF file paths now instrumented** (nexus-7niu extension). The scaffold PR (shipped earlier this release cycle) covered code files; this follow-up extends the same `StageTimers`-via-`IndexContext` wiring to the other two per-file paths the `nx index repo` loop exercises. `prose_indexer.index_prose_file` wraps the markdown / line-based chunker, the CCE / local embed call, and the T3 upsert + chash dual-write + taxonomy-assign block. `_index_pdf_file` (the repo-loop PDF wrapper in `indexer.py`) gets the same three-stage decomposition with a local `contextlib.nullcontext` fallback so the `stage_timers=None` fast path stays zero-overhead. The `_run_index` prose-file and PDF-file loops now build a fresh `StageTimers` per file when the CLI's `on_stage_timers` callback is installed, matching the code-file loop's pattern exactly. Three new tests in `tests/test_indexer.py` pin the callback contract for each of the three paths (code / prose / PDF); `tests/test_stage_timers.py` primitive coverage unchanged. Remaining un-instrumented sites per the bead's design doc — `doc_indexer.batch_index_markdowns` (RDR ingestion, batch-shaped rather than per-file) and `pipeline_stages.uploader_loop` (streaming PDF pipeline with decoupled extract/chunk/upload threads) — require separate design because their shapes don't map cleanly onto the per-file `StageTimers` contract.
- **`nx index --debug-timing` per-stage intra-file timing breakdown** (nexus-7niu scaffold, vatx Gap 4b). Operators investigating the 89–95 s per-file stalls surfaced by the parent bead (nexus-vatx) could see an aggregate retry summary (Gap 4a) but not a decomposition of "was voyage slow, was chromadb slow, or was chunking choked on a giant file?" This PR ships the primitive + the first instrumented call site. A new `StageTimers` dataclass in `src/nexus/stage_timers.py` accumulates four buckets (chunking / embed / upload / retry) with a `.stage(name)` context manager that snapshots `nexus.retry.get_retry_stats()` before and after to correctly attribute backoff sleep to the retry bucket rather than the embed bucket. The `code_indexer.index_code_file` hot path is now wrapped in three `ctx.stage_timers.stage(...)` blocks (silent no-ops when the ctx field is `None` — zero overhead on normal runs). `_run_index` and `index_repository` gain an `on_stage_timers: Callable[[Path, StageTimers], None] | None` parameter; when the CLI passes `--debug-timing` it collects per-file timers and renders an end-of-run breakdown to stderr (`[debug-timing] per-stage totals across N files: chunking_s … embed_s … upload_s … retry_s …` with percentages). Prose, PDF, and pipeline-stages uploader sites will be instrumented in follow-up PRs — the scaffold keeps the scope of this change reviewable. 11 primitive tests + 2 indexer-integration tests + 3 CLI tests (16 total) pin accumulation, retry-delta attribution, aggregate/report formatting, and CLI wiring.
- **`nx doctor --check-quotas` pre-flight diagnostic** (nexus-c590, Gap 5 of nexus-vatx split off at release). Emits a three-section report: ChromaDB Cloud free-tier limits (drawn from `nexus.db.chroma_quotas.QUOTAS` — `MAX_QUERY_RESULTS`, `MAX_RECORDS_PER_WRITE`, `MAX_CONCURRENT_*`, `MAX_DOCUMENT_BYTES`, and neighbours) plus a live reachability probe of the cloud tenant; Voyage AI per-model token + dimension caps (`voyage-3`, `voyage-code-3`, `voyage-context-3`) with `VOYAGE_API_KEY` presence check; and a cumulative retry-accumulator summary pulled from `nexus.retry.get_retry_stats()` so any transient-error backoffs observed in the current process surface alongside the static limits. Exits 1 when the cloud tenant is unreachable in cloud mode (actionable fail), 0 in local mode or on a healthy cloud connection. `--json` returns a structured `{chromadb, voyage, retry}` dict for dashboards / CI gates. Six new tests in `tests/test_doctor_cmd.py::TestCheckQuotas` cover reachable + unreachable + local-mode + no-voyage-key + nonzero-retry + JSON-schema paths.

### Fixed

- **`nx collection health` chunk count now comes from T3, not the catalog** (nexus-39zi). The old report computed ``chunk_count`` as ``SELECT SUM(chunk_count) FROM catalog.documents WHERE physical_collection = ?``, which silently drifted to 0 on 129/143 production collections because the catalog's ``chunk_count`` column is only written by the paths that register through the catalog — direct ``store_put``, cloud-side operations, and tenants that predate the column leave it untouched. The 2026-04-18 live shakeout surfaced the drift: ``nx collection list`` showed ``code__ART-8c2e74c0`` with 63 077 chunks while ``nx collection health --format=json`` reported 0. Health now calls ``coll.count()`` on the live T3 collection (the same source ``nx collection list`` uses) so the two commands cannot disagree. A new `chunk_count_fn` parameter on `compute_collection_health` makes the source injectable for tests; the existing `catalog_stats_fn` still owns `last_indexed` and `orphan_count` (catalog-side properties). Four new tests pin the precedence rule, the exact drift case from the live shakeout, backward-compat for legacy callers, and the removal of ``chunk_count`` from ``_default_catalog_stats_fn``'s return. Same PR also folds the `_default_chash_coverage_fn` to use the public `ChashIndex.count_for_collection()` method instead of reaching into `_lock` (carryover from the review-remediation sweep that missed this site).

## [4.8.0] - 2026-04-18

### Added

- **`nx index` end-of-run retry-time summary** (nexus-vatx Gap 4a). Process-local counters in `nexus.retry` track aggregate voyage + chroma backoff seconds and retry counts; `nx index repo` resets them on start and emits `Transient-error backoff: Xs total (voyage … , chroma … )` at the end when any retry fired. Silent on clean runs so the normal output stays tidy. New `get_retry_stats()` / `reset_retry_stats()` public API. Three new tests pin counter accumulation across the voyage and chroma paths plus reset semantics.
- **`nx index` periodic ETA line** (nexus-vatx Gap 3). A new background ticker in `nx index repo` emits `[eta] N/total files · C chunks · Xs/file avg · ~M min remaining` to stderr every 60 s, independent of stdout's TTY state. Tqdm's built-in bar suppresses itself when stdout is redirected (CI logs, `nohup`, `tail -f`), leaving operators with no pace signal — the ticker fills that gap. Lifecycle: starts on `on_start` when the total file count is known, stops in a `finally` block so a mid-run exception still reaps the daemon thread. The first tick before any file completes renders `pending` rather than dividing by zero. Three formatter tests + three ticker-lifecycle tests pin the behaviour.
- **`nx index` post-processing phase markers** (nexus-vatx Gap 2). After the per-file `[N/N]` progress bar finishes, the pipeline keeps running for several seconds to minutes — RDR discovery, misclassified-chunk pruning, deleted-file pruning, pipeline-version stamping, and catalog registration. Previously the operator saw silence and could not tell hung from busy. A new `on_phase` callback threaded through `index_repository` → `_run_index` emits `[post] <phase>…` / `[post] <phase> done (Xs)` lines to stderr for each phase, bookended by `[post] Post-processing complete (Xs)`. The `nx index` CLI wires the callback to `click.echo(..., err=True)` so markers are visible even when stdout is redirected to a file. Four new tests in `tests/test_indexer.py` pin the phase surface.

### Changed

- **Voyage AI retries are now visible to operators** (nexus-vatx Gap 1). Every `voyageai.Client(...)` construction in the tree now passes `max_retries=0` instead of the SDK's tenacity-based `max_retries=3`, and `_voyage_with_retry` is the sole retry authority. The retry predicate is extended from `APIConnectionError | TryAgain` to also cover `RateLimitError`, `ServiceUnavailableError`, `ServerError`, and `Timeout`. Each retry decision emits a WARN-level structlog line (`voyage_transient_error_retry` with `attempt`, `delay`, `error_type`, `error`) — previously voyageai's internal tenacity swallowed rate-limit backoffs, producing the 89–95 s per-file stalls with no log explanation that surfaced during the 2026-04-17 Delos re-index. Touches `db/t3.py`, `doc_indexer.py`, `indexer.py`, `scoring.py`. Six new tests in `tests/test_voyage_retry.py` pin the extended predicate, WARN-line contents, and per-error-class retry behaviour.

### Fixed

- **Review-remediation: Important + Suggestion findings.** Sweep of the remaining post-v4.7.0 review findings after the three Criticals landed. Grouped by reviewer scope:
  - **Indexing observability:** `_run_index` post-processing block is now wrapped in `try/finally` so the `[post] Post-processing complete …` marker fires even when a prune / catalog hook raises (Reviewer A/I-1); the marker includes `(interrupted: <ExcType>)` when abnormal. `nx index repo` emits the retry-time summary (`Transient-error backoff: Xs total …`) from a `finally` block so it's visible on exception paths (Reviewer A/I-2). `_ETATicker.start()` now clears the stop event inside the lock and refuses double-start to eliminate a thread-leak hazard on concurrent start/stop (Reviewer A/I-3 + S-4). `_format_eta` renders `done` instead of `~1 min remaining` when every file is complete (Reviewer A/S-1). `test_voyage_retry.py` now resets retry accumulators via an autouse fixture so assertion failures can't leak module state between tests (Reviewer A/I-4). The retry accumulator's record-before-sleep semantic is now documented (Reviewer A/S-2).
  - **Collection management:** `collection_audit.compute_chash_coverage` no longer reaches into `ChashIndex._lock` from outside the class — new public methods `ChashIndex.count_for_collection(name)` and `ChashIndex.doc_ids_present_in_collection(collection, ids)` provide locked alternatives, and the coverage pass uses a single `ChashIndex` open for both the count and missing-sample queries (Reviewer B/I-1, B/S-3, C/I-4). `indexer._catalog_hook` captures `source_mtime` with `stat()` BEFORE reading file bytes for the content hash so a concurrent write produces a stored mtime *older* than the indexed content (safe direction — future staleness checks fire correctly) instead of the reverse (Reviewer B/I-3). A parallel TOCTOU window exists in `doc_indexer._catalog_markdown_hook` and `pipeline_stages._catalog_pdf_hook` that requires threading mtime from the content-read entry point — comment annotations added; full fix deferred to a follow-up bead.
  - **Search / taxonomy / util:** `_CHILD_MARKERS` in `doc/resolvers.py` now uses trailing-boundary suffixes (`-calibration-` and `-calibration.`) so a primary RDR whose title starts with `calibration-` (e.g. `rdr-200-calibration-free-inference.md`) is no longer misclassified as a child artifact (Reviewer C/I-5). Stale `os.killpg(os.getpgid(pid), …)` comments in `session.py` now reference the canonical `safe_killpg` helper (Reviewer C/I-1). `_iter_count_matches` in `doc/ref_scanner.py` gained comments documenting plain-integer-before-k-shorthand iteration order, and `_extract_count_near`'s docstring pins the equidistant tie-break (Reviewer C/I-2 + I-3). `sample_live_distances` and the `--live` histogram probe now log at DEBUG on exception instead of silently returning empty, so a quota-exceeded or timeout path is observable in structured logs (Reviewer B/S-1 + C/S-2). Comment additions for the `merge_candidates` symmetric-pair averaging tradeoff (Reviewer C/S-4) and the `test_silent_zero_end_to_end_real_engine` `CliRunner.mix_stderr` contract (Reviewer C/S-1).

  Test additions: `test_pid_zero_returns_false_without_signalling`, `test_negative_pid_returns_false_without_signalling`, `test_rename_preserves_source_mtime_across_jsonl_rebuild`, `TestRenameCascadeFailureModes` (t2 + catalog), `test_primary_with_calibration_prefix_in_title_is_not_misclassified`, `test_equidistant_counts_tie_break_is_stable`, autouse `_reset_retry_stats_on_entry` fixture.
- **Review-remediation: `safe_killpg` rejects non-positive pids.** The `pid=0` path routed `os.getpgid(0)` to the *caller's* own pgid and then `os.killpg(own_pgid, SIGKILL)` — a truncated or zero-byte mineru pidfile that parsed as 0 would have self-terminated the running `nx` CLI. The helper now guards `pid <= 0` explicitly (in addition to the pre-existing `isinstance(pid, int)` mock guard) and emits a debug log on the skip. Two new regression tests (`test_pid_zero_returns_false_without_signalling`, `test_negative_pid_returns_false_without_signalling`) pin that `os.killpg` is never invoked on a non-positive pid.
- **Review-remediation: `Catalog.rename_collection` preserves `source_mtime` in JSONL.** The rename SELECT was fetching 12 columns (omitting `source_mtime`, column 13) and appending a JSONL record without the field. JSONL is the rebuild source of truth, so `Catalog.rebuild()` silently reset `source_mtime` to `0.0` for every renamed document — breaking stale-source detection (RDR-087 Phase 3.4) until the next re-index restamped the column. Fixed by adding `source_mtime` to the SELECT and the record dict. A new test (`test_rename_preserves_source_mtime_across_jsonl_rebuild`) seeds a document with a known mtime, renames the collection, rebuilds the catalog from JSONL, and asserts the mtime round-trips.
- **Review-remediation: explicit failure-mode tests for the rename cascade.** `nx collection rename` is intentionally fail-open — T3 renames first, then T2 and catalog are attempted independently so a partial failure leaves the system in a divergent but recoverable state with a stderr `warn:` line. That contract was documented in code comments but not pinned by tests. Added `TestRenameCascadeFailureModes` with two cases: T2 raises → exit 0 with `T2 cascade failed` on stderr + T3 still renamed; catalog raises → exit 0 with `catalog cascade failed` on stderr + T3 still renamed.
- **`nx taxonomy validate-refs` proximity false-positives on bullet lists and multi-count paragraphs** (nexus-7ay). Each markdown list item (`-`, `*`, `+`, ordered) is now its own count-binding scope — a count claim in one bullet no longer leaks into every sibling, which previously produced a single OK line followed by a cascade of spurious Drift lines. Within a prose paragraph that names more than one collection, each reference now binds to the textually nearest chunk-count claim instead of always the first one encountered. Seven new tests in `tests/test_ref_scanner.py` pin the expected proximity semantics.

## [4.7.0] - 2026-04-18

### Added

- **RDR-086 Phase 1 — T2 `chash_index` primitive + dual-write + cascade + reconciliation.**
  New `ChashIndex` domain store answers "which `(collection, doc_id)` holds this chunk hash?" in ~50 µs via a SQLite lookup, replacing the ~13-min serial-ChromaDB-filter alternative. Compound PK `(chash, physical_collection)` so the same chunk text can legitimately live in multiple collections (e.g. `knowledge__delos` and `knowledge__delos_docling`). Populated via best-effort dual-write at seven T3 upsert sites — `code_indexer`, `prose_indexer`, three `doc_indexer` paths, `pipeline_stages.uploader_loop`, and `indexer._index_pdf_file`. `nx collection backfill-hash [--all]` reconciles gaps with a tqdm progress bar (TTY-auto-detect via `disable=None`). `nx collection delete` cascade now also purges `chash_index` rows. 5 sub-phase beads: `nexus-l2k`, `nexus-4qm`, `nexus-ppl`, `nexus-r9b`, `nexus-jfi`.
- **RDR-086 Phase 2 — `Catalog.resolve_chash` collection-agnostic resolver** (nexus-9a8). Given a bare hex, `chash:<hex>`, or `chash:<hex>:<start>-<end>` input: look up T2 rows, self-heal stale rows (collection no longer exists in T3 → delete the row on access), tie-break by `prefer_collection` → newest `created_at` → deterministic name sort (newest wins so re-indexing into `_docling` variants supersedes the original), delegate to `resolve_span` for chunk text + metadata. On T2 miss, parallel ChromaDB fallback (10× concurrency matching `MAX_CONCURRENT_READS`, 30 s wall-clock deadline, one warning per process). New `ChunkRef` TypedDict documents the return shape.
- **RDR-086 Phase 3 — `chunk_text_hash` on structured returns** (nexus-d3h). `search(structured=True)` and `query(structured=True)` now include a `chunk_text_hash` list aligned with `ids`, plus a new per-result `chunk_collections` list so consumers that need per-chunk origin (e.g. `nx_answer`'s envelope) get the right collection for every hit, not just the top dedup'd one. `nx_answer(structured=True)` is a new opt-in kwarg returning `{final_text, chunks, plan_id, step_count}` where each chunk carries `{id, chash, collection, distance}`. Single-step guard path produces the same envelope with one `query()` round-trip (previously two).
- **RDR-086 Phase 4 — `nx doc` consumers on `resolve_chash`** (nexus-6iz). `nx doc check-grounding --fail-ungrounded` — exit 1 on any unresolved `chash:` span with file:line error output. `nx doc check-extensions` resolves chash → Chroma-scoped `doc_id` before calling `chunk_grounded_in` (caller-side fix; the taxonomy signature is unchanged), removing the RDR-083 v1 inertness case. `nx doc render --expand-citations` appends a `## Citations` footnote block with chunk text (truncated at 500 chars); unresolvable hashes render as `[unresolved chash: <first 8>…]`.
- **RDR-086 Phase 5 — `nx doc cite` authoring CLI** (nexus-3dk). Compose `search(structured=True)` with Phase 3's `chunk_text_hash` surface, resolve the top hash via `Catalog.resolve_chash` to fetch the excerpt, emit a paste-ready `[excerpt](chash:<hex>)` markdown link or the full `--json` schema `{candidates, query, threshold_met}`. Empty-index short-circuit exits 2 with a "run `nx collection backfill-hash --all`" hint instead of a 30 s fallback timeout. Tied candidates within 0.01 distance are surfaced in JSON; stdout picks first + notes `# N candidates tied (see --json)`.
- **`ChashIndex.delete_stale(chash, collection)`** and **`ChashIndex.is_empty()`** — locked public methods the self-healing read and fresh-install guard use instead of touching `.conn` directly.

### Changed

- **`nexus.config.nexus_config_dir()`** is now the single source of truth for every path under `~/.config/nexus`. Twenty sites that previously hard-coded `Path.home() / ".config" / "nexus" / ...` now route through it. Covers T2 database, catalog JSONL, sessions, checkpoints, pipeline buffer, ripgrep cache, MinerU PID + output root, context cache, git-hook registry, index log, doctor registry lookup. `NEXUS_CONFIG_DIR` was previously documented as an override but silently ignored at the load-bearing T2 path — meaning a "sandbox" run could overwrite the user's production `memory.db`. 22 new isolation tests in `tests/test_config_dir_isolation.py` verify each surface.
- **`search_engine.search_cross_corpus` per-collection `n_results`** is now capped at `QUOTAS.MAX_QUERY_RESULTS` (300). Without this, a large `offset` fed into `fetch_n = offset + limit` multiplied by `mult` (up to 4×) produced per-collection query values that punched through the ChromaDB Cloud cap.
- **`_prune_stale_chunks` and the three `doc_indexer` stale-chunk delete sites** now batch `col.delete(ids=...)` at `MAX_RECORDS_PER_WRITE=300`. A single unbounded delete violated the Cloud quota on re-indexes that dropped >300 chunks.
- **`CatalogDB` SQLite connection** sets `busy_timeout=5000` + `journal_mode=WAL` to match the five T2 domain stores. Without these, cross-process catalog writes during indexing raced CLI reads and raised `OperationalError: database is locked` immediately.
- **`search_by_tag` LIKE pattern** escapes `%` and `_` metacharacters. Bound parameters block SQL injection but not glob matching — a tag like `"rdr_078"` previously matched `"rdrX078"`.
- **`merge_topics`** uses `INSERT ... ON CONFLICT DO UPDATE` to preserve the higher-similarity projection row when source and target both carry assignments for the same `doc_id`. The previous `INSERT OR IGNORE` silently discarded higher-similarity data.
- **`nx_answer(structured=True)` single-step guard** synthesizes the result summary from the structured envelope instead of calling `query()` twice. Halves the T3 round-trips for the single-step path.
- **`reset_singletons()`** now also resets the T1 plan-match cache. Previously, tests that injected a fresh T1 saw stale plan embeddings from prior test state.
- **`plan_match`** evicts stale T1 rows when `library.get_plan` returns `None`. Prevents accumulating ghost embeddings after T2 plan deletes.
- **`frecency.batch_frecency`** uses a unique `|||nxcommit|||` sentinel around timestamps instead of the fragile `"COMMIT "` prefix. A file literally named `"COMMIT something"` could previously corrupt all subsequent scores in its commit.
- **`_flag_contradictions`** caps the O(n²) pairwise check at 30 indices per collection. A knowledge corpus with many near-duplicate chunks used to dominate search-engine latency.
- **Exporter import path** validates every embedding's byte-size matches the first record's (and that the first is a multiple of 4 for float32). Malformed `.nxexp` files now fail fast at the boundary instead of raising a cryptic ChromaDB dim-mismatch error deep in upsert.
- **`nx upgrade --dry-run`** no longer writes. Previously called `bootstrap_version()` which creates base tables and seeds `_nexus_version` — legitimate writes in non-dry-run but wrong for dry-run.
- **`nx upgrade` T3 steps** are now tracked in a `_nexus_t3_steps` table. A failed T3 step is retried on the next upgrade invocation; previously the overall version advanced on T2 success and the failed T3 step never retried.
- **`nx doctor --check-schema`** additionally verifies `memory_fts` and the `idx_chash_index_collection` index. Opens with `journal_mode=WAL` to avoid immediate lock errors during concurrent MCP writes.
- **`console` activity route** uses a bounded `collections.deque` instead of reading the whole JSONL into memory — keeps the async event loop unblocked on large catalogs.
- **`Catalog._ensure_consistent`** caches the JSONL max-mtime and skips the rebuild when nothing has changed. Previously re-parsed the full corpus on every `Catalog()` construction.
- **T1 access tracking** coalesces per-row `col.update` calls into a single batched call, dropping N serial HTTP round-trips per search.
- **`LocalEmbeddingFunction`** guards lazy init with a lock so concurrent callers don't both download the fastembed model.
- **`CatalogTaxonomy.detect_hubs`** replaces N per-hub `SELECT DISTINCT source_collection` queries with one grouped query + a Python dict lookup.
- **CLI exit codes** standardized across `commands/doc.py` (16 sites) and `commands/taxonomy_cmd.py` (4 sites) — `raise click.exceptions.Exit(N)` instead of `sys.exit(N)` so the Click error pipeline fires.
- **`nx mineru` output root** moves from world-writable `/tmp/mineru-output` to `$XDG_RUNTIME_DIR/nexus-mineru` or `$NEXUS_CONFIG_DIR/mineru-output` (mode 0o700). Path is recorded in the PID file and cleaned up on `nx mineru stop` so extracted PDF artifacts don't linger.
- **Console + mineru PID files** use `os.open(..., 0o600)` instead of `path.write_text()` (default umask 0o644). `nx console` also probes existing PID files on startup and refuses to start over a live server.
- **Activity-route `_event_summary`** routes the output `kind` through a frozenset whitelist as a defensive XSS hardening for the `<tr class="event-row {{ e.kind }}">` template.

### Fixed

- **RDR-086 review #1 (Critical) — `resolve_chash` self-heal bypassed `ChashIndex._lock`.** Self-healing `DELETE` now goes through the new `ChashIndex.delete_stale` method so concurrent `upsert` / `delete_collection` callers can't race the same SQLite connection.
- **RDR-086 review #2 (Critical) — `_fallback_chash_scan` future leak on early exit.** `ThreadPoolExecutor` now uses explicit manual lifecycle with `shutdown(wait=False, cancel_futures=True)` in a `finally` block so the 30-second deadline is a real deadline, not a ceiling bounded by the slowest in-flight probe.
- **Storage review C-1 (Critical) — migration race in `_upgrade_done`.** `apply_pending` now holds `_upgrade_lock` for the entire bootstrap + migrate sequence and only marks the path done on success. The previous shape reserved the slot before running migrations and relied on a try/except discard — leaving a window where a concurrent caller could see the path as done and proceed against a half-initialised schema.
- **Indexing review C1 (Critical) — wrong `killpg` target.** PDF-extractor `killpg(proc.pid, SIGKILL)` replaced with `killpg(os.getpgid(proc.pid), SIGKILL)` + `ProcessLookupError` swallow at all three MinerU subprocess kill sites. PID reuse could SIGKILL an unrelated process group under the old idiom.
- **Indexing review C2 (Critical) — `chunking_done` not in `finally`.** `chunker_loop` now wraps its body in try/finally so `_signal_done()` fires on every exit path including exceptions. Previously the orchestrator's `cancel.set()` was the implicit rescue — fragile and skipped the uploader's mark-completed logic.
- **Indexing review C3 (Critical) — parallel CCE tail-batch 429 swallowed.** Every future is now drained before re-raising, recording the first exception and re-raising it after collection. Previously the executor's `__exit__` discarded pending results when the first future's `.result()` raised.
- **Search review I-6 (Important) — `operators/dispatch.py` missing `killpg` on timeout.** `claude -p` now spawns with `start_new_session=True`; the `asyncio.TimeoutError` branch does `killpg(getpgid(pid), SIGKILL)` so children spawned by the planner are reaped, not orphaned.
- **Indexing review I-1 — `_index_pdf_incremental` resume with shrinking chunk count.** Discards the checkpoint when stored `chunks_upserted > total` (e.g. extractor version change re-chunked to fewer units) instead of silently skipping the loop and leaving stale chunks beyond `total`.
- **Indexing review I-2 — MinerU output-layout drift detected.** Raises a clear `RuntimeError` when subprocess exits 0 but the expected `.md` file is missing (layout change after a MinerU upgrade).
- **Storage review I-1 — 6+ lock-bypass sites in `taxonomy_cmd.py` / `migrations.py` backfill.** Every `db.taxonomy.conn.execute(...)` is now wrapped with `with db.taxonomy._lock:` to match the domain-store contract.
- **Storage review I-4 — `T2Database.delete` lock ordering contract documented.** Memory → taxonomy is the required order; the docstring now guards future edits from introducing a reverse-ordered caller that could deadlock.
- **`nx_answer` plan-miss error surfaces the dropped tool names** (e.g. "planner returned only non-dispatchable tools: Bash, grep") instead of a generic "planner failed" string.
- **Pre-existing local-mode RDR crash.** `LocalEmbeddingFunction.__call__` is 1-arg (`texts -> embeddings`) but `doc_indexer.EmbedFn` is 2-arg (`(texts, model) -> (embeddings, model)`). A `_local_embed_fn_tuple` adapter is now wired specifically to the `_discover_and_index_rdrs` call in local mode; code / prose / PDF paths continue to use the raw `LocalEmbeddingFunction` instance directly.
- **CI hang on `test_timeout_kills_process_and_raises`.** Both `operators/dispatch.py::claude_dispatch` and `pdf_extractor.py::_killpg_safe` now guard `killpg(getpgid(proc.pid), …)` behind `isinstance(proc.pid, int)`. `MagicMock` implements `__index__` returning 1, so unit tests that mock the subprocess previously signaled pgid=1 (init / launchd). On macOS this was benign (EPERM caught), but on GitHub ubuntu-latest containers the in-kernel signal delivery would stall deterministically — hanging the matrix pytest step for the full workflow run. Real `subprocess.Popen` / `asyncio.create_subprocess_exec` always yield an int pid; mock fixtures fall through to `proc.kill()` cleanly.

### Docs

- RDR-086 design document lives at `docs/rdr/rdr-086-chash-span-surface.md`.

## [4.6.5] - 2026-04-18

### Fixed

- **nexus-7ne1 — PDF extractor: MinerU-failed fallback returns `fast_result` without replaying `on_page` (silent 0-chunk pathology)** — when the auto-routing PDF extractor decides to use MinerU (`formula_count >= 5`) and MinerU then fails for any reason, the fallback returns the Docling probe pass's `fast_result` — but never replayed the `on_page` callbacks. The streaming pipeline (which only sees pages via `on_page`) got nothing → chunker emitted **0 chunks** for the entire document. The probe pass at `_extract_with_docling(..., enriched=False)` is intentionally invoked without `on_page` so callbacks aren't double-fired if MinerU takes over; the `formula_count < 5` happy path replays callbacks from `fast_result.metadata["page_boundaries"]`, but the `except` branch did not. **Fix**: mirror the replay logic into the `except` branch — every page from `fast_result.text` is re-emitted via `on_page` using stored `page_boundaries`. This bug masqueraded as MinerU brokenness during the 2026-04-17 Delos re-index (13/16 papers reported "0 chunks"); once MinerU succeeded after 4.6.4's `killpg` fix, the latent issue would still re-emerge for any future MinerU failure (transient network error, formula-density OOM, rate limit, etc.). Two regression tests in `tests/test_pdf_extractor.py::TestAutoDetectRouting` cover (1) the multi-page replay path and (2) the `on_page=None` no-callback contract.

## [4.6.4] - 2026-04-18

### Fixed

- **nexus-ze2a (P0) + nexus-dc57 (P1) — POSIX semaphore-leak root cause** — cross-corpus `_multiprocessing.SemLock()` was failing with `[Errno 28] No space left on device` whenever MinerU or orphaned T1 chroma children had accumulated. Both bugs shared a single root: we used `os.kill(pid, SIGTERM/SIGKILL)` on the long-running subprocess head, which did not propagate to its multiprocessing workers or their `resource_tracker`. Workers got orphaned and their POSIX named semaphores were never `sem_unlink()`-ed, eventually exhausting the kernel namespace (`kern.posix.sem.max = 10000`). **Fix**: (1) T1 chroma spawn in `session.py` now uses `start_new_session=True` so chroma plus its workers share one killable process group; (2) `stop_t1_server` uses `os.killpg(os.getpgid(pid), SIGTERM)` → `os.killpg(..., SIGKILL)` so the whole subtree receives the signal; (3) `nx mineru stop` uses the same `killpg` pattern so MinerU workers' `resource_tracker` runs before the group exits. No periodic-restart band-aid — the process-group contract itself was broken. New `nx doctor --check-resources` probes the POSIX semaphore namespace and exits 2 with actionable guidance on `[Errno 28]` pressure (pointing at both sources by name).

## [4.6.3] - 2026-04-17

### Fixed

- **Issue #190 follow-up** — `nx search` (and any cross-corpus query) no longer crashes when a T3 collection's stored embeddings don't match the current embedding model's output dimension. `T3Database.search` now catches `chromadb.errors.InvalidArgumentError` where the message contains `"dimension"`, logs a structured `collection_dimension_mismatch_skipped` warning with the collection name, and continues to the next collection. Non-dimension `InvalidArgumentError` subtypes (malformed `where` clause, bad query args, etc.) still propagate. Unblocks users who upgraded their local embedding model and left older collections around at the previous dimension — they can now search across the healthy collections without manually deleting the stale ones first.

## [4.6.2] - 2026-04-17

### Fixed

- **Issue #190** — `nx search` (and any `T2Database` construction) crashed with `sqlite3.OperationalError: no such column: verb` for anyone whose DB was created before 4.4.0 (RDR-078 dimensional-identity columns). `_PLANS_SCHEMA_SQL` in `db/t2/plan_library.py` created four `CREATE INDEX` statements referencing the `verb` / `scope` / `dimensions` columns inline, so `_create_base_tables` crashed before the 4.4.0 `_add_plan_dimensional_identity` migration had a chance to add those columns. Indexes removed from `_PLANS_SCHEMA_SQL`; the migration already creates all four idempotently (`CREATE INDEX IF NOT EXISTS`), so fresh installs still get them via the migration and upgrading installs get both the columns and the indexes added together. Two regression tests seed a pre-4.4.0 plans table and pin `bootstrap_version` + `apply_pending` behaviour.

## [4.6.1] - 2026-04-17

### Fixed

- **RDR-087 review follow-up** (nexus-yi4b.2.5) — two post-merge nits from the Phase 2 code review:
  - **Typed telemetry accessor on hot path** — `search_engine.search_cross_corpus` and `commands/search_cmd._emit_silent_zero_note` now read the `telemetry.search_enabled` / `telemetry.stderr_silent_zero` opt-outs via `config.get_telemetry_config(cfg=cfg)` instead of raw `cfg.get("telemetry", {}).get(...)`. Malformed `.nexus.yml` values (e.g. `search_enabled: "yes"`) now surface the structured `telemetry_config_malformed` warning on every call, matching the design intent of Phase 2.3's typed accessor. `get_telemetry_config` accepts an optional pre-loaded `cfg` kwarg so the hot path skips the disk re-read.
  - **Schema column rename** (migration 4.6.1) — `search_telemetry.dropped_count` → `kept_count` with value flip (`kept = raw − dropped`). RDR-087 §Proposed Solution specifies `kept_count`; 4.6.0 shipped with `dropped_count`. Idempotent ALTER + UPDATE migration; no-op on already-renamed or missing tables. Fresh installs get `kept_count` directly via `_TELEMETRY_SCHEMA_SQL`. Phase 3 consumers can now rely on spec-aligned column semantics.

## [4.6.0] - 2026-04-17

### Added

- **RDR-087 Phase 2: Telemetry Persistence** (nexus-yi4b.2) — four stacked beads that turn the Phase 1 silent-zero stderr diagnostic into queryable T2 state:
  - **2.1** — `search_telemetry` T2 table + registered migration; `nx doctor --check-schema` recognises it.
  - **2.2** — hot-path `INSERT OR IGNORE` from `search_cross_corpus` writing one row per (query, collection). Failure is swallowed at DEBUG so a telemetry fault never breaks search. Composite PK `(ts, query_hash, collection)` dedupes same-second writers.
  - **2.3** — `telemetry.search_enabled` / `telemetry.stderr_silent_zero` opt-out section in `.nexus.yml`; `TelemetryConfig` dataclass + `get_telemetry_config()` accessor with malformed-value structured warning.
  - **2.4** — `nx doctor --trim-telemetry [--days N]` (default 30d, `click.IntRange(min=1)`); safe on empty tables, missing-DB handled gracefully.

## [4.5.3] - 2026-04-17

### Fixed

- **MCP analytical-tool timeouts** — `claude_dispatch` default raised from 60s → 300s; per-tool defaults raised from 120s → 300s (`operator_extract`, `operator_rank`, `operator_compare`, `operator_summarize`, `operator_generate`, `nx_enrich_beads`, `_nx_answer_plan_miss`) and 120s → 600s (`nx_tidy`, `nx_plan_audit`). The prior 120s ceiling was producing false timeouts on real analytical workloads (observed: `nx_plan_audit` on the RDR-086 accept chain exhausted 120s mid-run). Timeouts now tier by workload: content transforms at 300s, whole-corpus sweeps at 600s. Callers pass an explicit timeout override when their input is known-short. Nine regression tests pin the new defaults via `inspect.signature`.
- **T3 bare-constructor credential fallback** — `chromadb.CloudClient()` invoked without explicit args now falls back through `get_credential()` before failing. Previously scripts that didn't know about the `make_t3()` factory surfaced `ChromaError: Permission denied`.

### Changed

- **RDR-086 scope expanded** — moved from draft to accepted. Original scope ("ship `resolve_chash` primitive + consumers") expanded after ART-instance feedback revealed authors have no CLI/MCP surface for obtaining chash values. New scope owns the primitive end-to-end: authoring (`nx doc cite`), resolution (T2 `chash_index` + global `Catalog.resolve_chash`), and verification (grounded citation coverage). 6 Gaps, 11 research findings, 5 implementation phases; compound PK `(chash, physical_collection)` for collision tolerance; empty-index short-circuit for fresh installs.

## [4.5.2] - 2026-04-17

### Fixed

- **nexus-51j** — `{{rdr:<id>.<field>}}` token resolution now works for projects using the uppercase `RDR-NNN-*.md` filename convention (common practice — makes RDR files visually distinct from other `docs/` content). The shipped `RdrResolver` (RDR-082) used `Path.glob("rdr-{key}-*.md")` which is case-sensitive on Linux/macOS filesystems and silently missed uppercase files. `_fetch` now iterates `*.md` files and matches case-insensitively via `re.IGNORECASE`. Zero-padding handling preserved; mixed-case cohabitation works (same directory with both `rdr-072-*.md` and `RDR-073-*.md` resolves both). Five regression tests in `tests/test_rdr_082_doc_tokens.py::TestRdrResolver` pin the behaviour. Observed on ART (70+ uppercase RDR files) during 2026-04-16 token pilot.

## [4.5.1] - 2026-04-17

### Fixed

- **nexus-lub follow-up** — `nx collection delete` cascade now runs even when the Chroma collection is already absent (discovered during the v4.5.0 live shakeout). The v4.5.0 cascade fix assumed the T3 delete succeeds before the cascade runs; when a user invokes the command to clean up orphan taxonomy state left by a pre-4.5.0 deletion (the recovery case the fix was supposed to unblock), `chromadb.errors.NotFoundError` bubbled out before the cascade, leaving orphans intact. v4.5.1 wraps the T3 delete in `try/except NotFoundError`, prints an informational `note:` to stderr, and proceeds with the taxonomy cleanup. One new regression test exercises the absent-collection path end-to-end via the Click runner.

## [4.5.0] - 2026-04-17

### Added

- **RDR-082 Doc-Build Token Resolution** — new `nx doc render` / `nx doc validate` commands that expand `{{bd:<id>[.field]}}` and `{{rdr:<id>[.field]}}` tokens against authoritative state (bead DB, RDR frontmatter) at build time. Resolver registry is the extension point for future namespaces. Emits `<stem>.rendered.md` sibling; fail-loud on unresolved tokens. Shared `src/nexus/doc/_common.py` fence helpers now used by both 082's tokenizer and RDR-081's `ref_scanner`.
- **RDR-083 Corpus-Evidence Tokens** — `{{nx-anchor:<collection>[|top=N]}}` token plus `nx doc check-grounding` (citation-coverage report) and `nx doc check-extensions` (projection-based author-extension flagger) subcommands. `AnchorResolver` is the first external consumer of RDR-082's registry — plugs in without parser/engine/CLI changes. v1 ships with a documented scope reduction: hash-to-chunk resolution (`resolve_chash`) deferred; `check-extensions` marked `[experimental]` with loud stderr WARNING when its inertness case fires. Deferrals owned by draft RDR-086.
- **RDR-084 Plan Library Growth** — successful ad-hoc plans produced by `nx_answer`'s plan-miss path are now auto-persisted via `save_plan(scope="personal", tags="ad-hoc,grown")`. Paraphrased questions match the grown plan through the plan_match gate instead of re-running the inline planner. New config key `plans.ad_hoc_ttl` (default 30d; set 0 to disable). T1 cosine cache receives `upsert(row)` so matches work without SessionStart re-populate.
- **RDR-085 Glossary-Aware Topic Labeler** — migrates `_generate_labels_batch` off its bespoke subprocess shell-out onto the shipped `claude_dispatch` substrate with schema-enforced output. Project vocabulary from `.nexus.yml#taxonomy.glossary` or `docs/glossary.md` prepended to the labeler prompt eliminates training-prior hallucinations (observed SSMF → "Single Mode Fiber" on ART corpus; live smoke on `rdr__nexus-571b8edd` shows 2/6 topics improved, 0/6 regressed). Supersedes the labeler portion of RDR-081.
- **RDR-086 Chash Span Resolution** (draft) — owner-RDR for RDR-083's deferred work. Proposes `catalog.resolve_chash(chash)` backed by a T2 `chash_index` table populated at indexing time. Unblocks `check-grounding --fail-ungrounded`, `check-extensions` meaningful candidates, and `nx doc render --expand-citations`.

### Fixed

- **nexus-lub** — `nx collection delete` now cascade-purges taxonomy state (`topics`, `topic_assignments`, `topic_links`, `taxonomy_meta`). Prior behaviour left orphan rows so `nx taxonomy status` and hub detection dragged ghost rows across deletions. New `CatalogTaxonomy.purge_collection(name)` method is transactional; delete command reports cleaned-row counts.
- **nexus-9ji** — `nx index pdf --force` now breaks the partial-ingest deadlock. `pipeline_index_pdf` gains `force: bool = False`; when True, `db.delete_pipeline_data(content_hash)` wipes stale pipeline.db state AND `col.delete(where={"content_hash": <hash>})` wipes T3 orphan chunks before `create_pipeline` runs. Both cleanups are pre-flight, so the normal "already running" skip still protects concurrent peers.

## [4.4.1] - 2026-04-16

### Fixed

- **Plugin auto-approval allow list** — added the 11 MCP tools shipped in 4.4.0 that were missing from `nx/hooks/scripts/auto-approve-nx-mcp.sh`: `nx_answer`, `nx_tidy`, `nx_enrich_beads`, `nx_plan_audit`, `traverse`, `store_get_many`, and the 5 operators (`operator_summarize`, `operator_extract`, `operator_rank`, `operator_compare`, `operator_generate`). Without this, every call to any of these tools surfaced a permission prompt instead of running silently. Plugin-only fix — no Python code changed.
- **Subagent-start operators block** — the analytical-operators guidance in `nx/hooks/scripts/subagent-start.sh` still told subagents to dispatch the removed `analytical-operator` agent. Replaced with direct invocations of the 5 `operator_*` MCP tools plus a pointer to `nx_answer` for plan-matched retrieval.
- **`nexus` skill reference** — `SKILL.md` common-operations block and `reference.md` tool catalog were frozen at the 15-tool v4.3.x surface. Added full entries for `nx_answer`, `traverse`, `store_get_many`, the 5 operators, and the 3 hygiene tools (`nx_tidy` / `nx_enrich_beads` / `nx_plan_audit`); corrected core-tool count to 26.
- **Stale "15 agents" reference** in `nx/agents/_shared/README.md` updated to "13 agents (10 active + 3 RDR-080 MCP-tool redirect stubs)".
- **`catalog.py` docstring** — removed lingering `query-planner` reference from `link_query`.

## [4.4.0] - 2026-04-16

### Added

- **RDR-078 Plan-Centric Retrieval** — semantic plan matching (T1 cosine + FTS5 fallback), typed-graph traversal as a first-class plan operator, scenario-plan library, dimensional plan identity. New MCP tools: `plan_match` (internal), `plan_run` (internal), `traverse` (catalog graph walk, depth cap 3), `store_get_many` (batch hydration past the ChromaDB 300-record quota). Migration `4.4.0` adds `plans.verb` / `scope` / `dimensions` / `default_bindings` / `parent_dims` columns + lifetime metrics. 9 builtin YAML scenario templates under `nx/plans/builtin/`. `PlanLibrary.get_plan_by_dimensions()` + `increment_match_metrics()` / `increment_run_started()` / `increment_run_outcome()`. `.github/workflows/plan-schema-check.yml` validates plan YAML on PR.
- **RDR-080 Retrieval Layer Consolidation** — single `nx_answer` MCP tool replaces the `query-planner` + `analytical-operator` agent pair and the inline three-path dispatcher. Trunk: `plan_match` → classify → `plan_run` → record (`nx_answer_runs` table). Plan-miss falls through to an inline `claude -p` planner. `nx_answer` accepts `dimensions={"verb": …}` so verb skills narrow the match to templates of the appropriate verb. Three stub agents (`knowledge-tidier`, `plan-auditor`, `plan-enricher`) shrink to 40-line redirects pointing at `nx_tidy`, `nx_plan_audit`, `nx_enrich_beads`. `pdf-chromadb-processor` agent removed (use `nx index pdf` or `/pdf-process`).
- **RDR-081 Stale-Reference Validator** — `nx taxonomy validate-refs <path>...` scans markdown for `<prefix>__<name>` collection references (default prefixes `docs`, `code`, `knowledge`, `rdr`) and proximate chunk-count claims ("12,900 chunks", "~13k chunks"), compares against current T3 state (`collection_list()` + `count()`), and reports `OK` / `Drift` / `Missing` per reference. Deterministic — pure regex + SQL, no LLM. Respects fenced code blocks (``` ``` ``` and `~~~`) so tutorial snippets don't false-positive. Config-driven whitelist via `.nexus.yml#taxonomy.collection_prefixes`. Exit-code contract: `0` = all OK, `1` = drift (or Missing with `--strict`), `2` = scanner/T3 failure. New module `src/nexus/doc/ref_scanner.py`.
- **5 operator MCP tools** — `operator_extract`, `operator_rank`, `operator_compare`, `operator_summarize`, `operator_generate` (each spawns `claude -p --output-format json --json-schema …`). Default timeout raised to 120s (from 60s) to fit real-corpora workloads.
- **`structured=True` mode** on `search()` and `query()` MCP tools — returns `{ids, tumblers, distances, collections}` dict instead of the human-readable string. Used by the plan runner so `$stepN.ids` / `$stepN.collections` references in plan YAML resolve to real data.
- **`corpus="all"` now means every live prefix** — computed from `get_collection_names()` instead of the hardcoded `"knowledge,code,docs,rdr"`. Projects with only `rdr__*` or custom prefixes no longer miss.
- **Inline-planner prompt is schema-aware** — `_PLANNER_TOOL_REFERENCE` in `nx_answer`'s plan-miss path now carries each tool's signature + output contract + two canonical chain patterns (search → store_get_many → operator; operator auto-hydration). Eliminates the silent `planner_step_dropped` / `missing required argument` failure modes.
- **Operator auto-hydration matches per-tool arg shapes** — the plan runner's `_default_dispatcher` now produces `content` for summarize, `context` for generate, `items` for rank/compare, `inputs` for extract. Previously produced `inputs` for all, which blew up summarize/generate with `missing required argument 'content'`.
- **`T3Database._embedding_fn` honors local mode** — returns `LocalEmbeddingFunction()` (ONNX MiniLM, 384-dim) when `local_mode=True` instead of always VoyageAI. Fixes `"Collection expecting embedding with dimension of 1024, got 384"` on local-mode sandboxes.
- **`nx index rdr` honors `NX_LOCAL`** — `batch_index_markdowns` + `_discover_and_index_rdrs` accept `embed_fn`; the CLI wraps `LocalEmbeddingFunction` with the correct `(texts, model) → (embs, model)` shape and forces Python floats (ChromaDB rejects `np.float32`).
- **`derive_title()` restored with initialism preservation** (`indexer_utils.py`). `my_api_v2.md` → `"My API V2"` instead of empty. `_PRESERVE_UPPER` covers 30+ common technical acronyms (`API`, `RDR`, `MCP`, `CLI`, `LLM`, …).
- **FTS5 special-character sanitization expanded** in `memory_store` to cover `'`, `,`, `;`, `?`, `!`, `#`, `@`, `$`, `%`, `&`, `|`, `\`, `<`, `>`, `[`, `]`, `{`, `}`, `=` (previously raised `OperationalError` on queries with apostrophes, URLs, or CLI flags).
- **Live validation harness** under `scripts/validate/` — 9 suites, 320+ runtime cases exercising every MCP tool, CLI command, hook, skill, and agent in an isolated sandbox. Per-case streaming + per-suite roll-up. LLM suites gated on `NX_VALIDATE_WITH_LLM=1`. Includes runtime exercise of all 43 skills + 13 agents via `claude -p` (suite 09) and full RDR lifecycle e2e (suite 08).

### Fixed

- **`claude_dispatch` now unwraps `structured_output`** from `claude -p --output-format json` wrapper (`src/nexus/operators/dispatch.py`). Before: `nx_tidy` / `nx_enrich_beads` / `nx_plan_audit` and all 5 `operator_*` tools silently returned empty strings because they read schema fields at the top level of claude's result wrapper. Surfaced by the harness's semantic assertions.
- **`Catalog.graph_many()` no longer produces dangling edges** when the node cap fires — edges referencing truncated nodes are filtered out of `merged_edges`; new `graph_many_node_limit_mid_seed` debug log.
- **`get_t1_plan_cache` init-failure short-circuit** via `_PLAN_CACHE_UNAVAILABLE` sentinel — prevents lock contention on the degraded-T1 hot path.
- **`store_get_many`** returns N contents for N input ids without silent truncation at the ChromaDB 300-record quota boundary.
- **Tool-name normalization in the inline planner** — LLM emits `mcp__plugin_nx_nexus__operator_extract`; dispatcher expected bare `extract`. `_TOOL_ALIASES` now maps both forms + the common catalog-tool names (`link_query` → `traverse`, etc.) so steps don't silently drop.
- **`T2Database.save_plan()` facade** accepts and forwards dimensional kwargs (`name`, `verb`, `scope`, `dimensions`, `default_bindings`, `parent_dims`). Previously stripped silently.
- **`_seed_plan_templates()` restores `load_all_tiers()` call** — 9 YAML templates under `nx/plans/builtin/` now actually seed into T2 (the call had been dead code).
- **`PlanLibrary.get_plan_by_dimensions()` restored** — `seed_loader.py` referenced it; method was missing. Seed-directory loads fail at the idempotency check without it.
- **`subprocess_git_toplevel()` removed** from `commands/catalog.py`; replaced with `find_repo_root(Path.cwd())` from `indexer_utils` to eliminate duplicate helpers.
- **CI git-identity fixture** in `tests/test_catalog_graph_many.py` — GitHub runners have no `user.email`/`user.name` configured; tests using `Catalog.init()` need env-scoped git identity.

### Changed

- **All 5 verb skills route through `nx_answer`** — `research`, `review`, `analyze`, `debug`, `document` now call `nx_answer(dimensions={"verb": <skill>})` instead of hand-rolling `plan_match` + `plan_run`. Picks up the record step automatically.
- **10 active agents include a "Retrieval preference (RDR-080)" section** — recommends `nx_answer` for multi-source retrieval; keeps direct `search()`/`query()` appropriate for single-step scoped lookups.

### Docs

- New concept pages: `docs/mcp-vs-agents.md` (RDR-080 boundary rule + stub-agent pattern) and `docs/plan-centric-retrieval.md` (`nx_answer` trunk, plan dimensions, scenario templates).
- Updated: `docs/cli-reference.md` (full `nx taxonomy validate-refs` section), `docs/querying-guide.md` (`nx_answer` trunk + verb skills), `docs/mcp-servers.md` (26-tool catalog broken out by category), `docs/configuration.md` (`taxonomy.collection_prefixes` key), `docs/catalog.md` (knowledge-tidier → `nx_tidy` MCP tool), `docs/memory-and-tasks.md`, `docs/rdr-nexus-integration.md`, `docs/rdr-workflow.md`, `nx/README.md`.
- New RDRs filed: RDR-082 (doc render tokens), RDR-083 (chunk-grounded citations), RDR-084 (plan library growth), RDR-085 (glossary-aware labeler) — all draft, tracked in index.

## [4.3.2] - 2026-04-15

### Added

- **`nx collection rewrite-metadata <coll>`** (load-bearing): paginate a collection, normalise each chunk's metadata via the same `_normalize_for_write` that fronts every live write, write back via `T3Database.update_chunks`. Idempotent. `--source-path PATH` filter, `--dry-run`, `--all`. Operationalises the PR #164 schema rationalisation on already-indexed corpora — `nx index --force` is a silent no-op when the pipeline-state DB still has the content_hash on file, so this command was the only path to retroactively rewrite legacy chunks.
- **`nexus.indexer_utils.detect_git_metadata(path)`** helper — walks up via `find_repo_root` and collects `git_project_name` / `git_branch` / `git_commit_hash` / `git_remote_url`. Returns `{}` outside a git repo so callers can `**`-merge unconditionally.

### Fixed

- **Empty `bib_*` placeholders no longer eat metadata budget** (nexus-2my): `normalize()` drops the four `bib_*` slots together when every value is the placeholder (`0` / `""`); a populated set rides through unchanged. Mirrors the `git_meta`-omitted-when-empty pattern from PR #164.
- **`git_meta` is now populated for `nx index pdf` and `nx index md`**: `_pdf_chunks`, `_markdown_chunks`, `pipeline_stages._build_chunk_metadata` accept a `git_meta` kwarg with auto-detect fallback. Pre-fix, single-file ingest paths emitted no `git_*` keys (the augment lived only in the repo-walk path), so `git_meta` was simply absent on directly-indexed PDFs/markdown. Resolved once at the entrypoint for the streaming pipeline so per-chunk overhead is zero.

### Notes

- Pipeline-state staleness (where `--force` is a silent no-op when `pipeline_buffer` still tracks the content_hash) is tracked separately as a follow-up — not blocking this release because `nx collection rewrite-metadata` is the operator-facing answer.

## [4.3.1] - 2026-04-15

### Fixed

- **T3 metadata schema rationalised (nexus-40t)**: fresh `nx index pdf` ingests on ChromaDB Cloud no longer trip the 32-key `NumMetadataKeys` quota. New `src/nexus/metadata_schema.py` defines the 31 canonical top-level keys actually read by `where=` filters, scoring, and display; every T3 `upsert`/`update` now funnels through `normalize()` + `validate()` at the write boundary. The prior insertion-order-dependent silent-trim heuristic — which dropped newly-enriched `bib_*` fields when total key count crossed 32 — is gone. Violations now raise `MetadataSchemaError` with the full key set.
- **Consolidated `git_*` provenance into a single `git_meta` JSON string** (4 slots → 1). Sub-keys: `project`, `branch`, `commit`, `remote`.
- **Confirmed cargo keys dropped**: `bib_semantic_scholar_id`, `pdf_subject`, `pdf_keywords`, `source_date`, `is_image_pdf`, `has_formulas`, `format`, `extraction_method`, `chunk_type`, `filename`, `file_extension`, `programming_language`, `ast_chunked`, `page_count`, `indexed_at`. All were written by the indexing pipeline but read by no call site.
- **New `content_type` field** (`code` / `pdf` / `markdown` / `prose`) injected by `normalize()` as the canonical routing signal; supersedes the overlapping legacy pair `(store_type, category)`, though both remain in the allowed schema for user-facing back-compat.

### Notes

- **No on-disk backfill** — existing records with >31 metadata keys remain readable. Only new writes are constrained. A dedicated `nx collection rewrite-metadata` command will land in a follow-up to rewrite historical ingests under the canonical schema.

## [4.3.0] - 2026-04-14

### Added

- **Projection quality (RDR-077)**: cross-collection projection now records raw cosine similarity, timestamp, and source collection for every projection assignment. Three new nullable columns on `topic_assignments`: `similarity` (REAL), `assigned_at` (TEXT, ISO-8601), `source_collection` (TEXT). Composite index `idx_topic_assignments_source` supports ICF aggregation and Phase 5/6 hub / audit queries. Migration is idempotent and applied by `nx upgrade` under the existing RDR-076 registry.
- **`nx taxonomy project --use-icf`**: Inverse Collection Frequency weighting. Suppresses hub topics (generic labels that span nearly every source corpus) before the threshold filter and top-K ranking. Stored similarity remains raw cosine — ICF is applied only at query time, never persisted (RDR-077 RF-8 invariant).
- **Per-corpus-type default thresholds** on `nx taxonomy project`: omitting `--threshold` now applies `code__*` 0.70, `knowledge__*` 0.50, `docs__*`/`rdr__*` 0.55. Explicit `--threshold` always wins. Exposed as `nexus.corpus.default_projection_threshold`.
- **`CatalogTaxonomy.compute_icf_map()`**: returns `{topic_id: icf}` where `icf = log2(N_effective / DF)`. Guards: `N_effective < 2` returns `{}`; `DF = N_effective` yields `0.0` (intentional hub suppression); legacy NULL-`source_collection` rows excluded from both numerator and denominator. Per-instance cache via `use_cache=True` + `clear_icf_cache()`. `log2` registered as a deterministic, null-safe SQLite scalar in `CatalogTaxonomy.__init__`.
- **`AssignResult` NamedTuple**: `assign_single()` now returns `AssignResult(topic_id, similarity)` instead of a bare `int`. Callers that only need the topic id use `.topic_id`. Distance → similarity inversion (`1.0 - distance`) happens inside the method.
- **Prefer-higher UPSERT for projection rows**: `assign_topic(assigned_by='projection', ...)` uses `INSERT … ON CONFLICT DO UPDATE SET similarity = MAX(COALESCE(-1.0), excluded)` so re-projection with a lower similarity never overwrites a higher one, and `assigned_at` / `source_collection` refresh only when the incoming match wins. HDBSCAN / centroid / manual rows keep `INSERT OR IGNORE`.
- **`docs/taxonomy-projection-tuning.md`**: operator guide — similarity semantics, ICF rationale, per-corpus-type defaults, calibration loop for new corpora, upsert semantics, staleness detection, troubleshooting.
- **`nx taxonomy hubs`**: generic-pattern hub detector. Flags topics whose projection assignments span `--min-collections N` or more source corpora with ICF `<= --max-icf` and/or labels containing bundled stopword tokens (`assert`, `junit`, `builder`, `class`, `import`, `exception`, `getter`, `setter`, `variable`, `declaration`, `operator`). Output sorted by `chunks × (1 - ICF)` descending. `--warn-stale` compares `MAX(taxonomy_meta.last_discover_at)` across contributing source collections against the hub's latest `assigned_at`; `--explain` shows DF / ICF / matched stopword tokens per row. Advisory — users decide.
- **`detect_hubs()`** on `CatalogTaxonomy` returning `list[HubRow]` with per-row staleness fields (`max_last_discover_at`, `never_discovered_count`, `is_stale`). Never-discovered source collections count as stale (O-3). `DEFAULT_HUB_STOPWORDS` constant exposes the bundled token list.
- **`nx taxonomy audit --collection NAME`**: projection-quality report per collection. Output: total projection assignments originating from the collection, p10 / p50 / p90 of raw cosine similarity (Python-side nearest-rank — SQLite has no `percentile_cont`), count below threshold (re-projection candidates), top receiving topics with ICF, pattern-pollution flags. `--threshold` defaults to the per-corpus-type value from `default_projection_threshold`; `--top-n` caps the receiving-topic list (default 5). Empty-projection case returns a clean "no projection data" message, no stack trace.
- **`audit_collection()`** on `CatalogTaxonomy` returning `AuditReport(collection, total_assignments, p10, p50, p90, below_threshold_count, threshold, top_receiving_hubs, pattern_pollution)`. Helper NamedTuple `AuditHub` carries per-row chunk_count, icf, and matched_stopwords.

### Changed

- `nx taxonomy project --threshold` is now optional (was `0.85`). Omitting it triggers the per-corpus-type default cascade.
- `project_against()` accepts optional `icf_map: dict[int, float] | None`. When supplied, adjusted scores (`sim * icf`) drive both the threshold filter and the top-K ranking; raw cosine is still what lands in `chunk_assignments`.
- `assign_batch(cross_collection=True)` now propagates per-row similarity and `source_collection` into `topic_assignments` (previously the distance was discarded — RDR-077 C-1 audit finding).
- `backfill_projection` (T3 upgrade step) unpacks the new 3-tuple `chunk_assignments` and passes `similarity` + `source_collection=src` through to `assign_topic`.

### Documentation

- `docs/taxonomy.md` — new cross-collection projection section, `project` subcommand added to command table, ICF summary, per-corpus threshold table.
- `docs/cli-reference.md` — `--use-icf` example, `project` row updated with per-corpus defaults + tuning-doc link.
- `docs/storage-tiers.md` — `topic_assignments` row now documents all post-RDR-077 columns and upsert semantics.
- `docs/architecture.md` — Projection quality subsection under Taxonomy linking to the tuning guide; `project` subcommand listed in the `nx taxonomy` CLI table.

## [4.2.2] - 2026-04-14

(Note: v4.2.1 was tagged but never published due to a test failure. v4.2.2 supersedes it and includes all 4.2.1 changes plus the ChromaDB Cloud quota audit and observability improvements found during a live shakeout.)

### Added

- **`nx doctor` PyPI version check**: `_check_cli_version` queries https://pypi.org/pypi/conexus/json (3-second timeout) and reports current vs latest. When behind, suggests `uv tool upgrade conexus`. Network failures are silent (offline-tolerant).
- **`nx upgrade --skip-t3` flag**: skip T3 upgrade steps (e.g., heavy cross-collection projection backfill) for fast T2-only migrations.
- **`backfill_projection` per-collection progress**: prints `[i/N] collection: chunks, matches, attempted (elapsed)` to stderr during the T3 backfill, plus a final summary with total time and actual rows stored. Previously the backfill was silent for many minutes on large repos.
- **`CatalogTaxonomy._paginated_get`** + **`_batched_upsert`** helpers: wrap ChromaDB calls with the 300-record per-call cap (`MAX_QUERY_RESULTS` / `MAX_RECORDS_PER_WRITE`).
- **CLAUDE.md "External Service Limits" section**: documents ChromaDB Cloud + Voyage AI quotas with a reference table. Mandatory consult before any new ChromaDB call.

### Fixed

- **`project_against` paginated `coll.get()`**: `_PAGE = 2000` exceeded the ChromaDB Cloud Get quota of 300, causing `nx taxonomy project` to fail on real cloud collections. Now `_PAGE = 300` with paginated source-collection fetch via `_paginated_get`.
- **4 unbounded `coll.get()` calls** in `catalog_taxonomy.py` (`_discover_cross_links`, `project_against` centroid filter, `rebuild_taxonomy` rebuild + cleanup paths) wrapped in `_paginated_get` to avoid OOM and quota errors at scale.
- **3 `centroid_coll.upsert()` sites** wrapped in `_batched_upsert` (defensive against `MAX_RECORDS_PER_WRITE = 300`).
- **`rebuild_taxonomy` cleanup**: paginated GET + batched DELETE so collections with >300 centroids don't fail rebuild.
- **`nx taxonomy links` invisible cross-collection links**: command queried `compute_topic_links` (catalog-derived only) and ignored the `topic_links` table. Cross-collection projection links written by `_discover_cross_links` and `generate_cooccurrence_links` were invisible. Now displays all rows in `topic_links` with `[collection]` prefix on each topic. New `--refresh` flag re-runs catalog-derived computation explicitly.
- **`backfill_projection` misleading count**: reported "X assignments" using the per-call attempt count, but `INSERT OR IGNORE` deduplicates. Now reports "X stored (Y attempted)" using `COUNT(*) FROM topic_assignments WHERE assigned_by = 'projection'`.
- **Plugin/CLI version mismatch UX**: when the nx plugin is upgraded but the conexus CLI is not, the `nx upgrade --auto` SessionStart hook would print a cryptic Click error. Now prints a helpful message: `nx plugin requires conexus >= 4.2.0 — run: uv tool upgrade conexus`.

## [4.2.0] - 2026-04-14

### Added

- **Idempotent upgrade mechanism** (RDR-076): centralised T2 schema migration registry in `src/nexus/db/migrations.py` with version-gated `Migration(introduced, name, fn)` entries. `apply_pending(conn, current_version)` runs migrations between last-seen version (stored in `_nexus_version` table) and current CLI version. Each migration is idempotent via `PRAGMA table_info()` / `sqlite_master` guards.
- **`nx upgrade` CLI command** with `--dry-run`, `--force`, `--auto` flags for applying pending T2 migrations and T3 upgrade steps.
- **Auto-upgrade on SessionStart**: `nx upgrade --auto` runs as the first SessionStart hook — T2 migrations apply silently on every session start.
- **`T3UpgradeStep` typed interface** for ChromaDB operations (backfills, re-indexing) that require a `T3Database` client.
- **`nx doctor --check-schema`** validates T2 database schema and reports pending migrations.
- **MCP version compatibility check**: synchronous check in MCP `main()` that warns on major/minor version divergence between CLI and stored version.
- **Cross-collection topic projection** (RDR-075): `nx taxonomy project SOURCE` command computes cosine similarity between source chunk embeddings and target collection centroids via normalized matrix multiply. Flags: `--against TARGETS`, `--threshold N` (default 0.85), `--top-k N`, `--persist`, `--backfill`.
- **Automatic cross-collection projection** in `taxonomy_assign_hook`: every `store_put` now projects against foreign collection centroids in addition to same-collection assignment. New rows use `assigned_by='projection'`.
- **Cross-collection topic links**: `_discover_cross_links` (centroid-level similarity at discover time) and `generate_cooccurrence_links` (SQL self-join on shared doc co-assignments) populate the `topic_links` table with `link_types=["projection"]` or `["cooccurrence"]`.
- **`list_sibling_collections()`** in `registry.py` auto-detects related collections from the `{prefix}{name}-{hash8}` naming scheme. Used as the default `--against` target for `nx taxonomy project`.
- **T3 projection backfill**: `T3UpgradeStep("4.2.0", "Backfill cross-collection projection", ...)` runs via `nx upgrade` (not `--auto`) to populate cross-collection assignments and links for existing installs.
- **`cross_collection` parameter** on `assign_single` and `assign_batch` — when True, queries only foreign centroids (`$ne collection_name` filter) for cross-collection projection.
- **Incremental taxonomy assignment during indexing** (RDR-070): `taxonomy_assign_batch` wired into `code_indexer`, `prose_indexer`, `pipeline_stages` uploader, and `doc_indexer`. Chunks assigned to nearest topics immediately after upsert.
- **`indexer_utils`** gitignore/repo-root helpers: `find_repo_root()`, `should_ignore()`, `load_ignore_patterns()`, `is_gitignored()`. PDF batch mode now respects `.nexusignore`.

### Fixed

- RDR close gate heading normalization: `_extract_section` now accepts both `## Problem` and `## Problem Statement` heading variants; gap regex broadened from `^#### Gap \d+:` to `^#{3,5} Gap \d+:` (accepts h3–h5).
- `doctor --check-schema` uses `PRAGMA busy_timeout=2000` to prevent `database is locked` during concurrent upgrades.
- `upsert_topic_links` no longer deletes all rows before inserting — preserves projection links from `_discover_cross_links`.
- `_parse_version` normalizes to 3-component tuples (`(3, 7)` → `(3, 7, 0)`) to avoid unexpected ordering.

### Changed

- `assign_batch` batches all embeddings into a single ChromaDB query (was N individual queries).
- `project_against` paginates source collection fetch (2000-chunk pages) to prevent OOM on large collections.
- `generate_cooccurrence_links` uses a SQL self-join on `topic_assignments` instead of loading the full table into Python memory.
- Domain store `_migrate_*_if_needed()` methods now delegate to the centralised migration registry.

### Docs

- Full `nx upgrade` section added to `docs/cli-reference.md`
- `nx taxonomy project` subcommand documented in `docs/cli-reference.md`
- Migration Registry section added to `docs/architecture.md` replacing old ad-hoc migration paragraph
- Source Layout in `CLAUDE.md` updated with `migrations.py`, `upgrade.py`, and updated descriptions
- Release checklist in `docs/contributing.md` now includes `migrations.py` verification

## [4.1.2] - 2026-04-13

### Fixed

- SubagentStart hook emitted literal `$(...)` bash code instead of the L1 knowledge map — the command substitution was inside a single-quoted heredoc (`<<'NXTOOLS'`) which suppresses expansion
- Added 4 integration tests verifying both SessionStart and SubagentStart hooks actually emit cached context

## [4.1.1] - 2026-04-13

### Fixed

- `nx context show` read the global cache file instead of the per-repo cache, showing stale or wrong content
- Corrected cache path in docs: `~/.config/nexus/context/<repo>-<hash>.txt` (was `context_l1_<repo>-<hash>.txt`)

## [4.1.0] - 2026-04-13

### Added

- **Query sanitizer** (RDR-071): `sanitize_query()` strips LLM prompt contamination (system prompts, tool preambles, chain-of-thought artifacts) from search queries before embedding. Wired into MCP `search` and `query` tools automatically — no user action needed. 24 TDD tests.
- **Progressive context loading** (RDR-072): generates a ~200 token topic map from taxonomy and caches it as a flat file. Injected at session start via the SessionStart hook so agents have project context before the first search query.
- `nx context refresh` CLI command to manually regenerate the context cache
- `nx context show` to display the current cached context
- Auto-refresh hooks: context cache regenerated automatically after `nx taxonomy discover` and `nx index repo`
- Per-repo context cache files (`context_l1_<repo>-<hash>.txt`) for multi-project support

### Fixed

- `reset_singletons` no longer clears module-level hook registrations (affected test isolation)

### Docs

- Added `nx context` section to CLI reference
- Added Context module to architecture.md module map
- Noted query sanitizer in architecture.md Search area description

## [4.0.3] - 2026-04-13

### Changed

- Batch topic labeling: 20 topics per claude -p call (amortizes startup overhead), 4 parallel workers. 654 topics labeled in ~3 minutes vs ~70 minutes sequential.


## [4.0.2] - 2026-04-13

### Changed

- **Hybrid clustering**: MiniBatchKMeans for collections over 5K chunks (O(n) vs HDBSCAN's O(n^2)). Reduces clustering time from 12+ minutes to 2.9 seconds on 63K-chunk collections.
- **Parallel labeling**: 4 concurrent `claude -p` workers for topic labeling. Labels incrementally per collection (crash-safe) instead of batch at end.
- **Progress tracking**: per-phase reporting for `nx taxonomy discover` (fetch milestones at 25/50/75%, embedding source, clustering time, labeling progress with worker count)

### Fixed

- Labeling limit raised from 100 to 1000 per collection (was silently truncating collections with 100+ topics)
- Progress output uses `stdout.buffer.flush()` for immediate display in pipes and redirects

## [4.0.1] - 2026-04-13

### Fixed

- `nx taxonomy` command group was not registered in `cli.py` (included in 4.0.0 squash merge but registration line was missing)
- Added `test_cli_registration.py` to prevent this class of bug: verifies all command modules, taxonomy subcommands, MCP tools, and post-store hooks are properly wired

## [4.0.0] - 2026-04-13

### Added

- **Topic taxonomy** (RDR-070): automatic topic discovery across T3 collections using HDBSCAN clustering on native embeddings (Voyage 1024d on cloud, MiniLM 384d on local). Topics are auto-labeled with Claude Haiku when the `claude` CLI is available. Search results are grouped by topic and boosted for relevance.
- `nx taxonomy discover --all` discovers topics for all eligible T3 collections in one command
- `nx taxonomy status` shows topic health: collections, coverage, review state
- `nx taxonomy review` interactive review: accept, rename, merge, delete, skip
- `nx taxonomy label` batch-relabels topics with Claude Haiku
- `nx taxonomy assign/rename/merge/split/links` manual curation commands
- `nx taxonomy rebuild` full re-cluster with merge strategy preserving operator labels
- Topic boost in search: same-topic results get -0.1 distance, linked-topic -0.05
- Topic grouping: `cluster_by="semantic"` groups results by topic label when >50% assigned
- Topic-scoped search: `search(query="...", topic="Label")` pre-filters to a topic cluster
- Incremental assignment: `store_put` auto-assigns new docs to nearest topic via centroid ANN
- `taxonomy.auto_label` config (default: true) controls Claude Haiku auto-labeling
- `taxonomy.local_exclude_collections` config (default: `["code__*"]`) skips code in local mode (MiniLM clusters poorly on code; cloud Voyage handles it well)
- Live smoke test script: `scripts/smoke-test-taxonomy.py`
- 15 E2E integration tests with real ChromaDB and MiniLM (no mocks)
- `docs/taxonomy.md` dedicated user guide

### Changed

- **Breaking**: `nx taxonomy rebuild` now takes `--collection` instead of `--project`. The old `--project` flag still works with a deprecation notice.
- **Breaking**: `cluster_and_persist()` and `rebuild_taxonomy()` in `nexus.taxonomy` now emit `DeprecationWarning` and return 0. Use `db.taxonomy.discover_topics()` or `nx taxonomy discover`.
- `search()` and `query()` MCP tools now pass taxonomy for topic boost and grouping on all searches
- `discover_for_collection` uses native T3 embeddings instead of re-embedding with MiniLM
- PDF metadata filtering: empty values dropped before ChromaDB upsert to stay under 32-key limit, fixing `git_project_name` loss on PDF chunks

### Fixed

- 30+ bugs found across 5 review rounds (substantive critique, deep review, 4x parallel sweep)
- Connection leak in MCP search when using topic filter
- Orphaned centroids after merge/delete/split operations
- Silent data loss on rebuild when HDBSCAN produces all noise
- Topic boost was writing to `hybrid_score` (overwritten by reranker) instead of `distance`
- Self-merge destroying a topic instead of no-op
- Double `get_assignments_for_docs` call per search
- `review_cmd` crash on EOF/Ctrl-C in interactive prompts
- Cloud quota violation: pagination reduced from 5000 to 250 per request
- Concurrency p95 threshold bumped 4.0x to 5.0x for CI noise tolerance

### Docs

- All user-facing docs source-verified by 6 parallel audit agents
- `docs/taxonomy.md` new dedicated taxonomy guide
- `docs/querying-guide.md` topic-aware search section
- `docs/cli-reference.md` all 12 taxonomy subcommands
- `docs/architecture.md` module map updated (10 missing files added)
- `CLAUDE.md` source layout expanded (18+ files added)
- `docs/configuration.md` 4 missing config keys documented
- BERTopic references removed (never used; sklearn HDBSCAN only)

## [3.9.3] - 2026-04-11

### Fixed

- **Agent model defaults restored to original values**: 3.9.2 downgraded
  agents aggressively; clean eval against ART RDR-073 (no T2 injection)
  proved haiku fails on complex architectural critique and sonnet uses
  more tool calls than opus. All defaults restored to v3.9.1 originals
  (4 opus, 10 sonnet, 2 haiku). The Model Selection tables added in 3.9.2
  are retained — they allow dispatchers to downgrade per-task when the
  task is simple enough, while keeping strong defaults.

  Lesson: initial haiku eval was contaminated by SubagentStart T2
  injection priming agents with the answer. T2 context ≠ cold capability.

## [3.9.3] - 2026-04-11

### Fixed

- **Agent model defaults recalibrated after clean evaluation**:

### Fixed

- **Agent model defaults recalibrated after clean evaluation**: 3.9.2
  set 8 agents to haiku default. Clean testing (no T2 context injection)
  against ART RDR-073 showed haiku fails on complex architectural
  critique — answers the wrong question, can't hold a dimensional thread
  through a 903-line RDR. Six analytical agents restored to sonnet
  default; three mechanical agents remain haiku.

  Agents restored to sonnet: substantive-critic, plan-auditor,
  deep-research-synthesizer, code-review-expert, codebase-deep-analyzer,
  query-planner.

  Agents remaining haiku: plan-enricher, test-validator, knowledge-tidier,
  analytical-operator, pdf-chromadb-processor.

  Finding: initial haiku evaluation was contaminated — SubagentStart hook
  injected T2 context about the exact failure mode being tested, making
  haiku appear capable of analysis it couldn't do cold.

## [3.9.2] - 2026-04-11

### Changed

- **Dynamic model selection for all agents**: agent defaults lowered to
  cheapest model that handles the common case (8 agents → haiku, 4 → sonnet
  from opus). Skills include Model Selection tables with escalation triggers.
  The Agent tool's `model` parameter overrides frontmatter at dispatch time
  (documented priority 2 in Claude Code resolution chain). Opus is now an
  explicit escalation, not a default.

  | Before | After (default) | Escalation |
  |--------|-----------------|------------|
  | 4 opus agents | 0 opus defaults | opus via `model` param when needed |
  | 8 sonnet agents | 4 sonnet defaults | sonnet via `model` param when needed |
  | 2 haiku agents | 12 haiku/sonnet defaults | — |

## [3.9.1] - 2026-04-11

### Fixed

- **rdr-audit canonical prompt v1.2**: added mandatory code-verification
  gate for PARTIAL and SCOPE-REDUCED audit verdicts. The audit read RDR
  text (success criteria checkboxes) but not code, producing false-positive
  SCOPE-REDUCED on RDR-056 when all 4 features had shipped. The gate
  requires Grep spot-checks against the source before any non-CLEAN verdict.

### Changed

- RDR-066 composition probe catch demonstration proven via synthetic test
  (10-dim vs 5-dim mismatch correctly attributed).
- RDR-067 CA-1 verified (prompt generalizes beyond ART), CA-2 partially
  verified (calibration drifts on severity grading).
- RDRs 057, 061, 062 closed as implemented; 065 status corrected; 068
  closed as won't-ship.

## [3.9.0] - 2026-04-11

Minor release: ships RDR-067 (Cross-Project RDR Audit Loop) — Phase 2 of
the 4-RDR silent-scope-reduction remediation. Adds the `nx:rdr-audit`
skill which wraps the proven 2026-04-11 audit pattern as a one-command
feedback loop, five management subcommands with a read-only / print-only
safety split, cross-project incident template, scheduling asset
templates for local cron/launchd, and softens six research-class agents
to honor relay-specified storage targets (T1/T2/T3).

### Added (nx plugin)

- **`nx:rdr-audit` skill** (`nx/skills/rdr-audit/SKILL.md`) — wraps the
  canonical audit prompt from RDR-067 Phase 1a (pinned in T2 at
  `nexus_rdr/067-canonical-prompt-v1`, ttl=0 permanent) as a one-command
  feedback loop. Dispatches the `deep-research-synthesizer` agent with
  the substituted prompt, parses the output, and persists findings to
  T2 `rdr_process/audit-<project>-<date>`. Enforces the Phase 1b
  invariant that transcript mining from `~/.claude/projects/*` is
  non-delegatable (main session must pre-gather excerpts before
  dispatch). Current-project derivation via `git remote` → pwd basename
  → user prompt precedence chain. Skill body owns `memory_put`
  persistence (the subagent returns findings; the skill writes T2).
- **Management subcommands** on `nx:rdr-audit`: `list`, `status`,
  `history`, `schedule`, `unschedule`. Enforces a safety split:
  read-only subcommands (`list`/`status`/`history`) must not mutate OS
  or T2 state; print-only subcommands (`schedule`/`unschedule`) must
  not execute `launchctl load`, `launchctl unload`, crontab edits, or
  plist file writes. Platform install/uninstall commands are printed
  for the user to review and run manually — the skill never performs
  privileged OS changes automatically.
- **`nx:rdr-audit` slash command** (`nx/commands/rdr-audit.md`) —
  preamble derives current project, pre-scopes the evidence layer
  (worktree detection, transcript directory detection), and classifies
  subcommands by safety class before routing to the skill body.
- **Cross-project incident template**
  (`nx/resources/rdr_process/INCIDENT-TEMPLATE.md`) — 6 frontmatter
  fields + 8 required narrative sections for cross-project
  silent-scope-reduction incident filings. Sibling projects file into
  T2 `rdr_process/<project>-incident-<slug>` so audit subagents can
  aggregate across projects.
- **Scheduling asset templates** (`scripts/`) — shell wrapper
  (`scripts/cron-rdr-audit.sh`, chmod +x, strict bash mode, log rotation
  at 10MB), macOS launchd plist template
  (`scripts/launchd/com.nexus.rdr-audit.PROJECT.plist`, monthly
  cadence), Linux crontab template (`scripts/cron/rdr-audit.crontab`,
  `0 3 1 */3 *` true 90-day cadence), and platform READMEs with
  explicit "do not run launchctl load automatically" safety notes.

### Changed (nx plugin)

- **Research-class agents honor relay-specified storage targets**. Six
  agents (`deep-research-synthesizer`, `deep-analyst`,
  `codebase-deep-analyzer`, `architect-planner`, `debugger`,
  `strategic-planner`) previously had hardcoded "MUST store to T3 via
  `store_put`" directives and `<HARD-GATE>` blocks that overrode
  dispatching skills' T2 target requests. Softened to "MUST persist …
  unless the dispatching relay specifies an alternative storage target
  in its Input Artifacts, Deliverable, or Operational Notes section".
  The T3 default is preserved for generic `/nx:research`,
  `/nx:deep-analysis`, `/nx:analyze-code`, etc. invocations (so the
  auto-linker and catalog graph behavior is unchanged). Dispatching
  skills like `nx:rdr-audit` can now redirect findings to T2 without
  fighting the agent's trained pattern.

### Docs

- RDR-067 (`docs/rdr/rdr-067-cross-project-rdr-audit-loop.md`) accepted
  2026-04-11, status `accepted`.

## [3.8.5] - 2026-04-11

Patch release: ships RDR-066 (Composition Smoke Probe at Coordinator
Beads) Phase 1 of the 4-RDR silent-scope-reduction remediation. Adds
plan-enricher coordinator detection and the `nx:composition-probe`
skill. Catches 3/4 historical ART audit incidents at the coordinator
boundary (inter-bead composition failures); the 4th (RDR-036 intra-class
HashMap short-circuit) is out of scope and re-attributed to RDR-068
dimensional contracts.

### Added (nx plugin)

- **Plan-enricher coordinator detection** (`nx/agents/plan-enricher.md`)
  — the enricher now inspects `bd show <id> --json .dependencies` in
  its per-bead walk. When the blocking-dependency count is ≥ 2, the
  bead is tagged `metadata.coordinator=true` via `bd update --metadata`,
  and a `/nx:composition-probe <id>` instruction is appended to the
  enriched bead description. Post-write verification asserts the tag
  actually persisted (CA-4 silent-omission mitigation) — on failure the
  enricher surfaces an explicit WARNING to the user rather than
  silently proceeding.
- **`nx:composition-probe` skill** (`nx/skills/composition-probe/SKILL.md`)
  — new skill fired on coordinator beads (or manually via
  `/nx:composition-probe <id>`). Reads the coordinator bead and its
  dependencies, dispatches a general-purpose subagent with a verbatim
  prompt to generate a 30-50 line composition smoke test, runs it via
  the project-native test runner (py/java/ts auto-detected), and
  reports PASS or FAIL with attribution to the specific failing
  dependency bead. Read-only subagent tool budget (Read + Grep + Glob),
  locked on Phase 1a spike that verified `search_cross_corpus` as a
  hard-case target without Serena symbol resolution needed.

### Fixed (nx plugin)

- **Coordinator convention documentation** in plan-enricher agent
  prompt header — clarifies what a coordinator is, how detection works
  (fallback heuristic, not full method-ownership lookup), and the
  over-tagging / under-tagging trade-offs. Inline references to the
  Phase 1b CA-5b retrospective (3/3 on in-scope historical targets).

### Performance

- Composition probe execution latency (Phase 1a empirical): **1.93s**
  for a 5-test probe against `search_engine.search_cross_corpus`
  (real `EphemeralClient` + ONNX MiniLM, no mocks, no API keys).
  Well under the documented 30-120s budget. Generation latency
  ~8 minutes wall-clock for reading source files and authoring the
  probe on a hard case.

### RDR

- RDR-066 Composition Smoke Probe at Coordinator Beads — Phase 1 of
  the 4-RDR silent-scope-reduction remediation cycle (Phase 0 was
  RDR-069, shipped 3.8.3). Catch ceiling revised from 4/4 to 3/4
  after Phase 1b retrospective found RDR-036 FactualTeacher.query
  HashMap short-circuit was an intra-class failure mode outside
  the probe framework's scope (re-attributes to RDR-068 dimensional
  contracts).

## [3.8.4] - 2026-04-11

Patch release: surgical close-time reindex. The `/nx:rdr-close` skill
was unconditionally walking the entire RDR corpus via `nx index rdr`
on every close, even when the diff was wholly inside the frontmatter
(status / closed_date / close_reason flip). This shipped two fixes
that should have landed together with RDR-069 in 3.8.3.

### Added

- **`nx index rdr <file.md>`** — single-file scoping for the RDR
  indexer. The command now accepts either a repo directory (existing
  behaviour — glob all `docs/rdr/*.md`) or a single `.md` file (new
  behaviour — index just that one file). File-mode resolves the repo
  root from the file path via `git rev-parse --show-toplevel`, falling
  back to the conventional `docs/rdr/<file>.md` layout when git is not
  available. Collection naming is computed from the resolved repo
  root, so file-mode and directory-mode write to the same
  `rdr__{basename}-{hash8}` collection. Rejects non-markdown files
  with a clean error; rejects unresolvable files with guidance to pass
  a directory instead.

### Fixed (nx plugin)

- **`rdr-close` skill unconditional corpus reindex** — Step 4.4 of
  the Implemented flow, Step 5 of the Reverted/Abandoned flow, and
  Step 3 of the Superseded flow all previously ran `nx index rdr`
  (no argument, whole-corpus walk) on every close. For
  frontmatter-only edits this is pure waste: chunk text is unchanged
  so embeddings would not shift. For body-level edits affecting only
  one RDR, it is still wasteful to walk every RDR file in the corpus.
  The skill now specifies: (a) skip the reindex entirely when the
  diff is wholly inside the frontmatter block, with a concrete
  `git diff | grep` recipe the user can run to check; (b) when a
  reindex IS warranted, use the single-file form
  `nx index rdr docs/rdr/rdr-NNN-<slug>.md` so the corpus walk is
  avoided. The whole-corpus form is explicitly called out as NOT
  appropriate at close time.

## [3.8.3] - 2026-04-11

Patch release: ships RDR-069 automatic substantive-critic dispatch at
`/nx:rdr-close`. New Step 1.75 (Automatic Critique) runs the
`substantive-critic` agent on every close and gates `close_reason` on
the critic's verdict category. Addresses ART's documented
silent-scope-reduction failure mode with the only intervention that has
empirical catch evidence (2/2 on ART RDR-073 + RDR-075).

### Added (nx plugin)

- **Step 1.75 Automatic Critique** in `nx/skills/rdr-close/SKILL.md` —
  dispatches `/nx:substantive-critique <rdr-id>` via a fixed-shape
  minimal relay (rdr_id + standard input artifacts only — never
  session-generated summaries, which is the exact rationalization-bias
  failure mode RDR-069 addresses). Parses the canonical `## Verdict`
  block and branches on outcome: `justified` passes through; `partial`
  blocks `close_reason: implemented` without override; `not-justified`
  blocks `close_reason: implemented` without override (while
  `close_reason: reverted` and `close_reason: partial` remain available
  without override as honest-failure-acknowledgment paths — only
  `implemented` requires `--force-implemented`); a fallback path
  counts `### Issue:` headers under `## Critical Issues` /
  `## Significant Issues` when the Verdict block is absent. Scenario 4
  surfaces dispatch timeouts and transport failures to the user —
  neither silently blocks nor silently proceeds.
- **Canonical Verdict block** in `nx/agents/substantive-critic.md`
  Output Format — 5 fields (`outcome`, `confidence`, `critical_count`,
  `significant_count`, `summary`) at the `- **outcome**:` line the
  close-flow parser greps. Fallback parse rule documented inline.
- **`--force-implemented "<reason>"`** flag in `nx/commands/rdr-close.md`
  preamble — escape hatch for false-positive critic blocks. Requires a
  non-empty reason (empty reason → `sys.exit(0)` with usage hint).
  Handles single-quoted, double-quoted, and bare-token reason forms.
  Writes a T2 audit entry at `nexus_rdr/<id>-close-override-<YYYY-MM-DD>`
  capturing `critic_verdict` (or "skipped"), `user_reason`,
  `final_close_reason`, `timestamp`, and `rdr_id`.
- **CA-4 override-rate threshold** documented in RDR-069 Day 2
  Operations — `>20%` override rate in any 30-day window degrades
  Phase 2 dispatch to advisory mode (critic runs, findings surface,
  close is not blocked). Measurement surface: the T2 override audit
  entries above.

### Fixed (nx plugin)

- **`--force` regex collision** in `rdr-close` preamble (plan-auditor
  SIG-1/SIG-2). Both occurrences — the `force = bool(...)` detection
  and the `args_clean` `re.sub(...)` stripping step — now use
  `r'--force(?!-)'` negative lookahead instead of `r'--force'`. A
  `\b`-based fix is explicitly rejected: word-boundary fires between
  `e` and `-` and still matches `--force-implemented`. Verified in
  Python REPL; concrete AC test from the Phase 2 bead passes.

### Performance

- **Critic dispatch latency** (CA-3): median ~111s, range 95-217s
  (n=9 runs on real RDRs during the Phase 0 research arc). Clean RDRs
  take longer than broken ones — the critic cannot short-circuit on
  "no Critical" and must exhaustively confirm. Budget 3-4 minutes for
  a clean close; use `--force-implemented "<reason>"` for
  high-confidence closes where the latency is not warranted.

### RDR

- RDR-069 Automatic Substantive-Critic Dispatch at Close — Phase 0 of
  the 4-RDR silent-scope-reduction remediation cycle (shipped first;
  RDR-066/067/068 are the later layers).

## [3.8.2] - 2026-04-11

Patch release: ships RDR-065 close-time funnel hardening for the nx plugin.
No core CLI changes — all surface area lives in the `nx` Claude Code plugin
(commands, hooks, skill text, RDR template scaffold). The new gates defend
the RDR close ritual against silent scope reduction.

### Added (nx plugin)

- **RDR template scaffold (Gap 4)** — `### Enumerated gaps to close`
  subsection with `#### Gap N: <title>` placeholders. Authors of new RDRs
  scaffold the structure required by the close gate out of the box.
- **Two-pass Problem Statement Replay preamble (Gap 1)** — added to
  `nx/commands/rdr-close.md`. Pass 1 enumerates `#### Gap N:` headings from
  the RDR's Problem Statement and exits cleanly when `--pointers` omitted.
  Pass 2 validates per-gap pointers (key coverage + file existence) and sets
  a T1 scratch `rdr-close-active,rdr-NNN` marker on success. Grandfathering
  is ID-based (`rdr_id_int < 65`), never date-based. Hard blocks all use
  `sys.exit(0)`.
- **`### Step 1.5: Problem Statement Replay`** — new section in
  `nx/skills/rdr-close/SKILL.md` documenting the four preamble outcomes
  (validation passed / Pass 1 enumeration / legacy WARN / hard block) and
  the verbatim user-facing framing prompt.
- **Divergence-language guard PostToolUse hook (Gap 2)** — new
  `nx/hooks/scripts/divergence-language-guard.sh` registered for `Write|Edit`
  matching `docs/rdr/post-mortem/`. Bakes in the LOCKED Rev 4 8-pattern
  regex bank with markdown header / table-row pre-filtering. Advisory
  only — never hard-blocks.
- **`bd create` commitment-metadata enforcement (Gap 3)** — extends
  `nx/hooks/scripts/pre_close_verification_hook.sh`. When an RDR close is
  active and a follow-up `bd create` mentions the active RDR, the hook
  requires `reopens_rdr`, `sprint`/`due`, and `drift_condition` markers in
  title+description. Missing markers → hard deny with reason. Audit log at
  `/tmp/nexus-rdr065-bd-create-audit.log`.

### RDR

- RDR-065 Close-Time Funnel Hardening Against Silent Scope Reduction —
  6 of 10 epic beads closed with this release.

## [3.8.1] - 2026-04-10

Patch release: four bug fixes from a live shakeout of v3.8.0. Every
user-visible feature from RDRs 057 / 061 / 062 / 063 was exercised
end-to-end against the shipped CLI, MCP servers, and SQLite store.
Three real bugs and one doc drift were found and fixed. **3466
non-integration tests passing** (+8 new regression pins), **20
integration tests passing**.

### Fixed

- **RDR-057 `overlap_detected` logic bug** — `T1.promote()` used the
  scratch entry's full first-100-char snippet as an FTS5 MATCH query.
  FTS5 MATCH is implicit-AND, so any scratch content containing even
  one token not present in the candidate returned zero matches — and
  by construction a similar-but-not-identical entry always has at
  least one new token. The feature was effectively unreachable for
  its intended use case. Rewrote the overlap detection to use the
  same two-phase pattern as `MemoryStore.find_overlapping_memories`:
  (1) pull the first 3 non-stopword content tokens as the FTS5
  candidate query, (2) confirm with Jaccard similarity ≥ 0.5 on the
  full non-stopword word sets. Threshold is 0.5 (more permissive than
  `find_overlapping_memories`' 0.7) because `promote()` is advisory —
  the row is written either way — while consolidation uses the higher
  bar for destructive merges. Four regression tests in
  `tests/test_scratch.py` pin the v3.8.0 shakeout failure plus edges
  (subset below threshold, too-short content, unrelated content).

- **`nx memory delete` taxonomy cascade** — deleting memory entries
  (via `--title`, `--all`, or `--id`) left dangling
  `topic_assignments` rows pointing to the deleted `doc_id`. Orphan
  topics surfaced in `nx taxonomy list` and `nx taxonomy show` as
  ghost entries referencing nonexistent docs. Added
  `CatalogTaxonomy.purge_assignments_for_doc(project, title)` that
  deletes matching assignments (scoped by collection) and drops any
  topics whose assignment count reaches zero. `T2Database.delete()`
  calls it after a successful memory row delete — cross-domain
  coordination lives in the facade (RDR-063 Phase 2 boundary).
  When the caller used `--id`, the facade resolves `(project,
  title)` via a direct SELECT on `memory.conn` before the delete so
  the cascade can scope correctly, avoiding the access-count side
  effect of `memory.get(id=...)` on a dying row. Four regression
  tests in `tests/test_taxonomy.py` cover the cascade, empty-topic
  cleanup, cross-project scoping, and delete-by-id.

### Docs

- **`nx catalog link --help` missing `formalizes`** — the built-in
  link types list was out of date; `formalizes` (added in RDR-057)
  was missing. The creation path accepts it correctly; only the help
  text was stale. One-line docstring update in
  `src/nexus/commands/catalog.py`.

- **`docs/mcp-servers.md` `nx catalog link-bulk` command name** —
  `docs/mcp-servers.md` listed `nx catalog link-bulk` as a CLI-only
  demoted tool. The actual command is `nx catalog link-bulk-delete`
  (hidden) and it is a bulk *delete* by filter, not a bulk create.
  Updated the demoted-tools table to use the real name and clarify
  the semantics. CHANGELOG entries for 3.7.0 and 3.8.0 use the
  Python function name `catalog_link_bulk` which is accurate —
  those are intentionally left as historical records.

## [3.8.0] - 2026-04-10

Ships **RDR-063 (T2 domain split)** — the Phase 1/Phase 2 refactor that was
drafted and gate-ready in 3.7.0. T2 is now a four-store package with per-store
`sqlite3.Connection` + `threading.Lock`; cross-domain reads no longer block on
unrelated writes. **3458 non-integration tests passing** (+2 for the new
`test_t2_concurrency.py` suite), **20 integration tests passing**, concurrency
acceptance gates all green.

### Added

- **RDR-063: T2 Domain Split** — `src/nexus/db/t2.py` (1,052 LOC monolith,
  four mixed domains) split into `src/nexus/db/t2/` package with four per-
  domain stores behind a composing `T2Database` facade:
  - `MemoryStore` (`db.memory`) — agent memory, FTS5 search, access tracking,
    heat-weighted TTL, consolidation helpers
  - `PlanLibrary` (`db.plans`) — plan templates, plan search, plan TTL
  - `CatalogTaxonomy` (`db.taxonomy`) — topic clustering, topic assignment
  - `Telemetry` (`db.telemetry`) — relevance log, retention-based expiry
  Each store opens its own `sqlite3.Connection` against the shared SQLite file
  in WAL mode with `busy_timeout=5000`. Reads in one domain are never blocked
  by writes in another. Concurrent writes across domains still serialize at
  SQLite's single-writer WAL lock but `busy_timeout` absorbs brief contention.
  Per-domain migration guards prevent double-`ALTER TABLE` under concurrent
  constructors. Phase 3 (physical file split) is explicitly deferred; requires
  its own RDR.

- **New concurrency test suite**: `tests/test_t2_concurrency.py` — 6 tests
  covering cross-domain parallel writes, same-store serialization, single-
  threaded baseline, memory_search under concurrent write load (acceptance
  gate), memory_get under concurrent write load, and memory_search during
  active `cluster_and_persist` runs. All gates stable across 10+ runs.

- `_is_sqlite_busy` helper in `memory_store.py` uses `exc.sqlite_errorcode`
  for precise SQLITE_BUSY detection (Python 3.12+). Extended codes
  (`SQLITE_BUSY_SNAPSHOT`, `SQLITE_BUSY_RECOVERY`, `SQLITE_BUSY_TIMEOUT`) are
  intentionally NOT swallowed — they indicate distinct failure modes.

### Changed

- **Best-effort access tracking** (behavior change): `memory.search(access="track")`
  and `memory.get()` now run the `access_count`/`last_accessed` UPDATE as a
  best-effort side-effect under a temporary `PRAGMA busy_timeout = 0`. Under
  sustained cross-domain write load, roughly 5–10% of updates fail-fast on
  `SQLITE_BUSY` and are logged at warning as `memory.access_tracking.skipped`.
  The returned row content is unaffected; only the counter update may be
  skipped. This trades counter precision for tail latency stability — the
  pre-refactor behavior would block the caller for up to 5 seconds on the
  busy_timeout. RDR-057 heat-weighted TTL remains approximate under load
  (see [Storage Tiers § Heat-Weighted Expiry](docs/storage-tiers.md#t2----memory-bank)).

- `T2Database` facade is now pure composition. `T2Database.conn` and
  `T2Database._lock` were removed. Callers that reached into the facade's
  raw connection must route through a specific domain store
  (`db.memory.conn`, `db.plans.conn`, etc.). All in-repo call sites migrated;
  no external callers should have depended on these (they were implementation
  details, not advertised API).

### Fixed

- README agent/skill/tool counts: 32 → 33 skills, 24 → 25 MCP tools (main
  README), 32 → 33 skills (nx plugin README), 17 → 16 agents and 32 → 33
  skills (getting-started.md), 17 → 16 agents (historical.md).
- `docs/architecture.md` Telemetry row mislabeled "access tracking" — moved
  to Memory row where it belongs; Telemetry reworded as "Relevance log …
  retention-based expiry".
- `nx/README.md` Hooks table rewritten to match `hooks.json` — removed
  non-existent `bd prime` entries, added missing `PostCompact`,
  `StopFailure`, `Stop`, `PreToolUse` (bd-close gate), and
  `PermissionRequest` (auto-approve MCP) hooks.
- `docs/storage-tiers.md` stale "Upcoming (RDR-063 draft)" blurb replaced
  with the shipped architecture description.
- `docs/rdr/README.md` RDR-063 status row updated from Accepted → Closed.
- `catalog_taxonomy.py::get_topic_docs` — added Phase 3 fragility note
  explaining that the cross-table JOIN depends on single-file architecture
  and will require redesign if Phase 3 proceeds.

### Docs

- **New**: `docs/rdr/post-mortem/063-t2-domain-split.md` — full post-mortem
  covering the 3 drifts (module LOC targets, access-tracking behavior change,
  now-addressed carry-forwards), 3 carry-forward items, and 4 process
  takeaways.
- `docs/architecture.md` — new § T2 Domain Stores section with the domain
  store table and Phase 1 → Phase 2 concurrency model comparison.
- `docs/contributing.md` — new § Adding a T2 Domain Feature with recipes for
  extending an existing store and adding a new domain store.
- `docs/storage-tiers.md` — RDR-063 interaction note under Heat-Weighted
  Expiry explaining best-effort access tracking.
- `docs/memory-and-tasks.md` — access tracking paragraph clarified to reflect
  best-effort semantics under load.
- `CLAUDE.md` — source layout updated from `db/t2.py` to `db/t2/` package.

## [3.7.0] - 2026-04-10

Three accepted RDRs ship together in a single release: RDR-057 (progressive
formalization), RDR-061 (literature-grounded search enhancement), and RDR-062
(MCP interface tiering). RDR-063 (T2 domain split) is drafted and gate-ready
for the next release. Six rounds of parallel multi-agent code review, 55+
findings addressed, 3426 unit tests + 20 integration tests passing.

### Added

- **RDR-062: MCP interface tiering (dual-server split)** — Single 30-tool
  `nexus` MCP server split into `nexus` (15 core tools) +
  `nexus-catalog` (10 catalog tools with short names — no `catalog_` prefix).
  New `nx-mcp-catalog` entry point. Six admin tools demoted to CLI-only:
  `store_delete`, `collection_info`, `collection_verify`, `catalog_unlink`,
  `catalog_link_audit`, `catalog_link_bulk`. Backward-compat shim at
  `nexus.mcp_server` re-exports all 30 functions for existing callers.
- **RDR-057: Progressive formalization across memory tiers**
  - T1 access tracking via ChromaDB metadata (`access_count`, `last_accessed`)
  - `PromotionReport` return type from `T1.promote()` with `new` / `overlap_detected` actions
  - T2 heat-weighted TTL: `effective_ttl = base_ttl * (1 + log(access_count + 1))` — highly-accessed entries survive longer
  - JIT contradiction detection in `search_cross_corpus`: flags same-collection result pairs with different `source_agent` provenance and cosine distance < 0.3 as `[CONTRADICTS ANOTHER RESULT]` in search output. Default-on; opt out via `search.contradiction_check: false`
  - New `formalizes` catalog link type for multi-representation equivalence
- **RDR-061: Literature-grounded search enhancement**
  - `memory_consolidate` MCP tool with `find-overlaps`, `merge`, `flag-stale` actions. Merge has `dry_run` + `confirm_destructive` safety gates. Uses SQLite `with self.conn:` context manager for atomic UPDATE+DELETE; raises `KeyError` if `keep_id` is missing (prevents silent data loss on `expire()` race)
  - Retrieval feedback loop (E2): new T2 `relevance_log` table records `(query, chunk_id, action)` triples when agents act on search results. Session-keyed in-process trace cache in `mcp_infra`. Purged by `T2Database.expire(relevance_log_days=90)`
  - Persistent taxonomy CLI (E5): `nx taxonomy list/rebuild/show` — Ward hierarchical clustering over T2 memory entries, capped vocab + stopword filter. CLI-only by design, no MCP tool
  - Memory consolidation helpers: `find_overlapping_memories`, `merge_memories`, `flag_stale_memories`
- **RDR-063: T2 domain split (drafted)** — Architecture RDR proposing a 3-phase
  refactor of `src/nexus/db/t2.py` into domain modules (memory, plans, catalog
  taxonomy, telemetry) with a facade preserving backward compatibility. Gate-ready.
- **Structured log event contracts** — `expire_complete`, `embedding_fetch_failed`,
  `embedding_fetch_shape_mismatch`, `contradiction_check`,
  `clustering_skipped_partial_failure` with field-level regression tests in
  `test_structlog_events.py`

### Changed

- **`T2Database.expire()`** now takes a `relevance_log_days: int = 90`
  parameter and purges the telemetry table alongside memory TTL expiry.
  Emits structured `expire_complete` log with `memory_deleted`,
  `relevance_log_deleted`, and optional `relevance_log_error` fields.
- **`T2Database.search()`** now takes `access: Literal["track", "silent"]`
  parameter (default `"track"`) replacing the former implicit access-count
  bump. `find_overlapping_memories` passes `access="silent"` to prevent
  consolidation scans from contaminating the staleness signal.
- **`search_cross_corpus()`** shares a single embedding fetch between
  contradiction detection and clustering via the new
  `_fetch_embeddings_for_results` helper. Partial per-collection failures
  now return `(embeddings, failed_indices)` so features process successful
  collections rather than being suppressed whole.
- **Migration guard** — T2 schema migrations run once per process per path
  via module-level `_migrated_paths` set, with the lock held across the
  full check-run-add sequence to prevent concurrent construction races.
  Path is canonicalized via `resolve()` to deduplicate symlinked aliases.

### Fixed

- **R4-1 (critical) merge_memories TOCTOU data loss** — `T2.merge_memories`
  now runs UPDATE + DELETE atomically via `with self.conn:` context manager;
  raises `KeyError` and rolls back when `keep_id` has 0 rowcount
  (prevents `delete_ids` from being destroyed when a concurrent `expire()`
  deletes `keep_id` mid-merge)
- **C2/F2 merge data loss guards** — `merge_memories` raises `ValueError`
  when `keep_id` appears in `delete_ids` (previously silently destroyed
  the kept entry)
- **R3-1 fail-per-collection embedding fetch** — One broken collection
  no longer suppresses contradiction flags or clustering for all other
  collections in a cross-corpus search
- **R4-2 clustering partial-failure observability** — Emits
  `clustering_skipped_partial_failure` warning when clustering is skipped
  due to a failed embedding fetch

### Removed

- **`src/nexus/catalog/llm_linker.py`** — 207-line dormant module (RDR-061
  E3 Phase 2b). Complete and tested but never wired to a call site,
  conflicting with RDR-057 RF-11 ("cheap at write, expensive at query").
  Cut with rationale recorded in RDR-061.
- **Monolithic `mcp_server.py`** — Replaced with a backward-compat shim.
  All tool definitions moved to `src/nexus/mcp/core.py` and
  `src/nexus/mcp/catalog.py`.

### Docs

- Comprehensive user-facing documentation audit: README, `docs/architecture.md`,
  `docs/catalog.md`, `docs/cli-reference.md`, `docs/configuration.md`,
  `docs/memory-and-tasks.md`, `docs/querying-guide.md`, `docs/storage-tiers.md`,
  `nx/README.md`, `nx/agents/_shared/CONTEXT_PROTOCOL.md`,
  `nx/skills/nexus/reference.md` updated for the dual-server architecture,
  new tools, heat-weighted TTL, contradiction detection, consolidation
  workflow, taxonomy CLI, and `formalizes` link type
- Plugin audit: skills, agents, commands, hooks, and plugin config all
  verified for stale MCP tool references (zero remaining)

## [3.6.5] - 2026-04-09

### Fixed
- **Stale lock file cleanup** — Background indexers launched by git hooks (`disown`/`&`) that crash before writing their PID left empty 0-byte lock files in `~/.config/nexus/locks/` that accumulated indefinitely. `_clear_stale_lock()` now uses age-based detection for empty files (>5s = stale). Added `_sweep_stale_locks()` to clean the entire locks directory on each index run.
- **Session lock hardening** — `session.lock` now writes PID for stale detection and clears stale locks before acquiring, using the same defensive pattern as the indexer.

## [3.6.4] - 2026-04-09

### Fixed
- **nx plugin.json version not bumped** — `nx/.claude-plugin/plugin.json` was stuck at 3.2.3 since the nx plugin was created, preventing Claude Code from refreshing the nx plugin cache on new releases. All plugin changes since 3.2.3 were invisible to users until manual cache clearing.
- **Release docs updated** — contributing.md now lists `nx/.claude-plugin/plugin.json` as a required release artifact alongside `sn/.claude-plugin/plugin.json`.

## [3.6.3] - 2026-04-09

### Fixed
- **Phantom Serena tool names eradicated** — `rename_symbol` (not a real JetBrains backend tool) replaced with `jet_brains_rename` in session-start.sh, mcp-inject.sh, and serena-code-nav skill.
- **Wrong MCP prefix** — `mcp__plugin_serena_serena__` corrected to `mcp__plugin_sn_serena__` in serena-code-nav skill and registry.yaml.
- **Sequential thinking prefix** — `mcp__sequential-thinking__` corrected to `mcp__plugin_nx_sequential-thinking__` in 7 skills and 1 command.
- **Phantom tools removed** — `restart_language_server`, `get_current_config`, and `activate_project` references removed from serena-code-nav skill (not exposed in `--context claude-code` MCP mode).

### Changed
- **Backend-agnostic Serena discovery** — mcp-inject.sh SubagentStart hook now uses dual-variant ToolSearch (JetBrains + LSP names) so the sn plugin works regardless of Serena backend. Delegates parameter docs to `initial_instructions`.
- **Generic tool names in skills** — debugging, development, and architecture skills use backend-neutral short names in pseudocode instead of hardcoded `jet_brains_*` names.

## [3.6.2] - 2026-04-08

### Fixed
- **CCE oversized chunk handling** — single chunks exceeding Voyage's 32K token context window are now truncated to ~30K tokens and retried, then degraded to zero vector if still too large. No more infinite retry spam.
- **Recursive CCE batch splitting** — batch halves now recurse through `_embed_one_batch` at all depth levels instead of calling the API directly.
- **Classifier: data files skipped** — `.txt`, `.csv`, `.tsv`, `.dat`, `.log` files are now classified as SKIP (not PROSE). Prevents wasting API calls on non-prose data files.
- **Catalog progress output** — `nx index repo` now shows progress lines during the catalog registration, link generation, and housekeeping phases instead of a silent pause.

## [3.6.1] - 2026-04-08

### Fixed
- **Subagent hook catalog context** — catalog link context (linked RDRs for files in task) now fires for all agent types. Was incorrectly skipped for code-nav and review agents.

## [3.6.0] - 2026-04-08

### Added
- **Catalog path rationalization (RDR-060)** — catalog `file_path` and T3 `source_path` now store relative paths. `resolve_path()` reconstructs absolute paths via `owner.repo_root` or registry fallback.
- **Link-aware search boost** — `query` MCP tool boosts results from documents with `implements` links. `implements-heuristic` links get zero boost (too noisy). Configurable per-type weights.
- **Discovery tools** — `nx catalog orphans`, `coverage`, `suggest-links` for link graph observability.
- **Incremental link generation** — link generators accept `new_tumblers` for O(new_n × m) incremental mode during `nx index repo`. `nx catalog link-generate` for full batch scans.
- **Agent integration** — `nx catalog links-for-file` and `session-summary` surface linked RDRs for code files. Ambient catalog context in subagent-start hook.
- **Catalog housekeeping** — `_run_housekeeping()` tracks `miss_count`, evicts orphans after 2 missed index runs, detects renames via content hash. `nx catalog gc` CLI.
- **`nx doctor --fix-paths [--dry-run]`** — one-time migration of absolute paths to relative (catalog + T3 metadata).
- **`nx:catalog` skill** — agent-friendly catalog manipulation (resolve, link, context, seed).

### Fixed
- **Test catalog isolation** — autouse `conftest.py` fixture prevents integration tests from polluting the user's live catalog.

## [3.5.2] - 2026-04-08

### Fixed
- **Batched ChromaDB deletes** — `--force` reindex failed with quota error when >300 stale chunks needed pruning. All delete paths now batch in 300-record pages.

### Added
- **`S2_API_KEY` support** — Semantic Scholar enrichment (`nx enrich`) now sends `x-api-key` header when set. Authenticated rate: 100 req/s vs 100/5min unauthenticated (50x speedup). Free key at https://www.semanticscholar.org/product/api#api-key

## [3.5.1] - 2026-04-08

### Fixed
- **Hook permissions** — `stop_failure_hook.py` now executable (was 644).
- **Hook robustness** — removed `set -euo pipefail` from all advisory and permission auto-approve hooks. Prevents silent failures under load.
- **Agent frontmatter** — synced 8 agent colors and 2 versions to match registry.yaml source of truth.

### Docs
- README: corrected agent/skill counts (14→16 agents, 28→32 skills).

## [3.5.0] - 2026-04-08

### Added
- **Quality-score reranking** (RDR-055 E2) — `quality_score()` and `apply_quality_boost()` in `scoring.py`. Log-scaled citation signal + exponential age decay, wired into CLI search after hybrid scoring. Dormant until `nx enrich` populates `bib_citation_count` metadata.
- **Shared where-filter module** (`nexus.filters`) — canonical `parse_where()` / `parse_where_str()` replacing duplicated parsers in MCP server and CLI. Strict mode for CLI validation, lenient for MCP.
- **Shared tumbler resolver** (`nexus.catalog.resolve_tumbler`) — canonical implementation replacing duplicated resolvers in MCP server and CLI catalog commands.
- **MCP infrastructure module** (`nexus.mcp_infra`) — singletons, caching, and test injection extracted from `mcp_server.py` (1752 → 1490 lines).
- **`PDFConfig` dataclass** in `config.py` — replaces 4 individual getter functions with a single structured config loader.

### Fixed
- **MCP cluster output** (RDR-056) — `search()` with `cluster_by="semantic"` now preserves cluster-grouped order and renders `── label ──` headers. Previously re-sorted by distance, destroying cluster grouping.
- **Flaky hook test** — `nx catalog sync` wrote "Catalog synced." to stdout, corrupting JSON output in `stop_verification_hook.sh`. Redirected to `/dev/null`.
- **Flaky integration test** — search by UID without metadata filter returned stale documents from prior runs. Added `--where title=` filter.
- **Advisory hooks hardened** — removed `set -euo pipefail` from `stop_verification_hook.sh` and `pre_close_verification_hook.sh` (advisory hooks must never fail).

### Changed
- **Test suite consolidated** — 44,243 → 30,799 lines (30% reduction). `@pytest.mark.parametrize` for redundant variants, 8 files deleted, 50+ files rewritten. All coverage preserved.
- **Corpus model selection** — `embedding_model_for_collection()` and `index_model_for_collection()` consolidated into single `voyage_model_for_collection()` with backward-compatible aliases.
- **Removed trivial wrappers** — `_entry_to_dict()` / `_link_to_dict()` replaced with direct `.to_dict()` calls (21 call sites).

## [3.4.0] - 2026-04-08

### Changed
- **Retire orchestrator agent** (RDR-058) — deleted `nx/agents/orchestrator.md`, removed from registry and model groups. Routing content preserved in `nx/skills/orchestration/reference.md`. Agent count 15 → 14.
- **Orchestration skill** — converted from agent-delegating to standalone reference skill. Points to routing tables and decision framework in `reference.md`.
- **Shared agent docs** — `CONTEXT_PROTOCOL.md` and `RELAY_TEMPLATE.md` updated: "orchestrator" → "caller" terminology.
- **rdr-accept command** — updated "orchestrator" → "caller" in planning chain prohibition.

### Added
- **Plan library integration** (RDR-058) — `using-nx-skills` Process Flow now checks `plan_search` before multi-agent dispatch and saves successful pipelines via `plan_save`.
- **5 pipeline templates** — RDR Chain, Plan-Audit-Implement, Research-Synthesize, Code Review, and Debug patterns stored as permanent T2 plan library entries.
- **Pipeline Pattern Catalog** — new table in `orchestration/reference.md` documenting all 5 standard pipeline patterns with agents, use cases, and prerequisites.
- **Orchestration standalone skill** — added to `registry.yaml` `standalone_skills` section.

### Docs
- README updated: 14 agents, 10 standalone skills, orchestration directory comment.
- RDR-058 accepted.

## [3.3.1] - 2026-04-07

### Fixed
- **RDR-055 code missing from v3.3.0** — `section_type` metadata (classify_section_type, 9 patterns, all 5 indexing paths) was lost during squash merge of PR #131. Cherry-picked from feature branch. `--where section_type!=references` now works.
- **CI failure on Python 3.13** — HNSW ef tests used `_local_db()` without EF override, causing VoyageAI key error on CI.

## [3.3.0] - 2026-04-07

### Added
- **Per-corpus distance thresholds** (RDR-056) — automatic noise filtering calibrated for Voyage AI embeddings. `code=0.45`, `knowledge/docs/rdr=0.65`, `default=0.55`. Configurable via `.nexus.yml` `search.distance_threshold.*`.
- **Multi-probe collection verification** (RDR-056) — `verify --deep` probes 5 documents (was 1), reports `probe_hit_rate`, new `degraded` status for partial failures.
- **HNSW ef tuning** (RDR-056) — local-mode collections created with `hnsw:search_ef=256`. Retroactive fix via `nx doctor --fix`. Cloud SPANN unaffected.
- **Corpus-specific over-fetch** (RDR-056) — knowledge/docs/rdr fetch 4x candidates before threshold filtering (was uniform 2x). Code stays at 2x.
- **Ward hierarchical clustering** (RDR-056) — new `search_clusterer.py` module. Opt-in via `cluster_by="semantic"` on MCP `search()` tool or `search.cluster_by` config key. Deterministic scipy Ward with numpy k-means fallback.
- **Catalog-scoped pre-filtering** (RDR-056) — high-selectivity metadata predicates (<5% match) route through catalog SQLite as `source_path $in` filter, avoiding HNSW/SPANN stalling.
- **Section-type metadata** (RDR-055) — markdown chunks carry `section_type` (abstract, introduction, methods, results, discussion, conclusion, references, acknowledgements, appendix). Filter with `--where section_type!=references`.
- **`T3Database.get_embeddings()`** — embedding post-fetch for clustering pipeline.
- **`Catalog.doc_count()`** — document count for selectivity calculation.

### Fixed
- **`hnsw:space` latent bug** — cloud SPANN collections don't populate `hnsw:space` metadata; `verify_collection_deep` now returns `cosine` directly in cloud mode instead of reading the absent key.
- **Broken status message** — `collection verify --deep` message updated from singular "probe chunk" to multi-probe semantics.

### Changed
- **`search_cross_corpus()` signature** — gains `cluster_by`, `catalog` parameters (both optional, backward compatible).
- **MCP `search()` tool** — gains `cluster_by` parameter.

### Docs
- Updated cli-reference.md, configuration.md, querying-guide.md with all new features.
- New "Search quality features" section in querying-guide.md.
- Plugin skill reference, session hooks, and subagent hooks updated for `cluster_by` and `section_type`.
- CLAUDE.md source layout updated.
- RDR-056 closed (implemented, Phases 1-3).

## [3.2.5] - 2026-04-07

### Fixed
- **Code search embedding mismatch** (RDR-059) — `code__*` collections were indexed with `voyage-code-3` but queried with `voyage-4`, producing random noise (0.038 distance spread). Query model now matches index model for all collection types. No reindexing required.
- **Flaky test determinism** — `_init_git_repo` in hook integration tests now disables GPG signing, eliminating SSH agent warmup race condition.
- **Stop hook pipefail** — replaced `printf | python3` pipe with `sys.argv` argument passing to avoid `set -eo pipefail` race.

### Changed
- **Embedding model routing** — `_embedding_fn()` in `t3.py` now routes via `embedding_model_for_collection()` instead of hardcoding `voyage-4`. Enforces index/query model match invariant.
- **Default fallback model** — unknown collection prefixes now default to `voyage-code-3` (was `voyage-4`) for both index and query.

### Removed
- **voyage-4 from all active code paths** — eradicated from `corpus.py`, `db/t3.py`, and all user-facing documentation. Only remains in historical changelog/RDR/postmortem references and one deliberate stale-data test fixture.
- **Superpowers plugin references** — removed from E2E test harness (`run.sh`, `00_debug_load.sh`).

### Docs
- **RDR-056**: Search Robustness and Result Clustering (17 research findings, 3 rounds)
- **RDR-057**: Progressive Formalization Across Memory Tiers
- **RDR-058**: Pipeline Orchestration and Plan Reuse
- **RDR-059**: Code Search Embedding Model Mismatch (critical bug, fixed)
- Updated CLAUDE.md, architecture.md, storage-tiers.md, repo-indexing.md, configuration.md — all voyage-4 references corrected.

## [3.2.4] - 2026-04-07

### Fixed
- **Chunk boundary overlap** (RDR-054) — wired dead `overlap_chars` into `SemanticMarkdownChunker._split_large_section`, which previously had zero overlap between sub-chunks. Bumped `PDFChunker` default overlap from 15% to 20% (225 → 300 chars). Fixed header duplication bug when overlap exceeds emitted content length. Guarded Python `[-0:]` edge case.

## [3.2.3] - 2026-04-07

### Fixed
- **Pagination completeness** — all list-returning MCP tools and CLI commands now include pagination footers when results are truncated. Tools fixed: `query`, `scratch` (search/list), `plan_search`, `catalog_search`, `catalog_list`, `catalog_link_query`. CLI commands fixed: `nx catalog list`, `nx catalog search`, `nx catalog links`. Docstrings updated to document pagination behavior.

## [3.2.2] - 2026-04-07

### Fixed
- **Plugin audit compliance** — added `nx/.claude-plugin/plugin.json` manifest; fixed 9 agents using non-standard `color` values (only `red`, `blue`, `green`, `yellow`, `purple`, `orange`, `pink`, `cyan` are valid per Claude Code docs).
- **PermissionRequest hooks** — added `sequential-thinking` to nx MCP auto-approve list (was causing CI failure); explicit matchers in hooks.json reverted to wildcard routing (decision logic stays explicit in shell scripts).

## [3.2.1] - 2026-04-07

### Added
- **File-path extraction linker** — `generate_rdr_filepath_links()` scans RDR content for source file paths and creates `implements` links to matching catalog code entries. `created_by="filepath_extractor"`. Wired into the indexer alongside the existing heuristic linker.

### Fixed
- **MCP auto-approve hooks** — replaced wildcard glob patterns with explicit full tool name lists in both nx (28 tools) and sn (27 tools) PermissionRequest hooks.
- **Agent self-seeding** — 5 analysis/research agents now self-seed T1 scratch with `link-context` when dispatched without a skill, so the auto-linker fires regardless of dispatch path.
- **Mandatory T3 persistence** — added `<HARD-GATE>` and Stop Criteria enforcement for `store_put` in deep-research-synthesizer, deep-analyst, debugger, architect-planner, and codebase-deep-analyzer.

## [3.2.0] - 2026-04-06

### Added
- **Auto-linker** — automatic catalog link creation at storage boundaries. When agents store findings via `store_put`, link-context entries seeded in T1 scratch by dispatching skills are read and catalog links are created automatically via `link_if_absent`. `created_by="auto-linker"` distinguishes mechanical links from agent-created and heuristic links.
- New module `src/nexus/catalog/auto_linker.py` with `auto_link()`, `read_link_contexts()`, and `LinkContext` dataclass.
- `_catalog_auto_link()` helper in MCP server, wired after `_catalog_store_hook` in `store_put`.

## [3.1.2] - 2026-04-06

### Added
- **Sub-chunk character ranges** — `chash:<sha256hex>:<start>-<end>` references a character range within a content-addressed chunk. The hash pins the chunk; the range pins the passage within it. Character offsets are inherently stable because the hash guarantees the content hasn't changed.
- **Custom link types** — the CLI `--type` flag now accepts any string, not just the seven built-in types.

### Docs
- **[Xanadu in Nexus](docs/xanadu-in-nexus.md)** — Xanadu lineage, cross-document linkage problem, and how the link graph enables plan-driven agentic search.
- **[Querying Guide](docs/querying-guide.md)** — `nx search` vs `query()` MCP vs `/nx:query` skill with catalog-aware routing and analytical query examples.
- Expanded catalog guide with tumbler addressing, link type guidance, span lifecycle, admin operations, and troubleshooting.

## [3.1.1] - 2026-04-06

### Added
- **chash: span validation at link creation** — `link()` and `link_if_absent()` now verify that `chash:` spans resolve to actual chunks in the document's physical collection before accepting the link. Raises `ValueError` with collection name if the hash doesn't exist. Skipped when `allow_dangling=True`.

### Fixed
- **`backfill-hash` live progress** — per-batch progress on stderr with carriage-return updates. Previously silent until completion.
- **`backfill-hash` ChromaDB quota handling** — chunks with 32+ metadata keys hit the `NumMetadataKeys` quota on update. Now caught per-batch and counted as skipped instead of crashing the entire run.

## [3.1.0] - 2026-04-06

### Added
- **Tumbler comparison operators** (RDR-053) — `__lt__`, `__le__`, `__gt__`, `__ge__` with -1 sentinel padding for cross-depth ordering. Parent tumblers sort before their children (e.g., `1.1.3 < 1.1.3.0`).
- **`Tumbler.spans_overlap()`** — static method for positional span overlap detection using the comparison operators.
- **Content-addressed spans** (RDR-053) — `chunk_text_hash` (SHA-256 of chunk text) added to ChromaDB metadata in all 5 indexers (code, prose markdown, prose non-markdown, doc PDF/markdown, streaming PDF pipeline). Distinct from file-level `content_hash`.
- **`chash:<sha256hex>` span format** — `_SPAN_PATTERN` and all link creation APIs accept content-hash spans alongside legacy positional formats. Content-hash spans survive re-indexing when chunk boundaries are unchanged.
- **`Catalog.resolve_span()`** — resolves `chash:` spans to chunk content via ChromaDB metadata query.
- **`link_audit()` chash verification** — optional `t3` parameter verifies each `chash:` span resolves to an actual chunk in ChromaDB. MCP `catalog_link_audit` tool now performs chash verification automatically.
- **`nx collection backfill-hash`** — backfill `chunk_text_hash` metadata on existing chunks without re-embedding. Also integrated into `nx catalog setup` and `nx catalog backfill` for automatic backfill during onboarding.
- **Querying guide** — new `docs/querying-guide.md` documenting `nx search` vs `query()` MCP vs `/nx:query` skill, catalog-aware routing, three-path dispatch, and analytical query examples.

### Fixed
- `resolve_span_text()` now handles `chash:` spans (was silently returning None for the preferred format).
- `stale_spans` audit excludes `chash:` spans (they survive re-indexing by design) and checks both `from_span` and `to_span`.
- `stale_chash` entries include `reason` field (`missing`, `document_deleted`, `error`) for actionable diagnostics.
- Plan template seeding uses direct SQL instead of fragile FTS search for idempotency.
- TTL migration detection uses `PRAGMA table_info` instead of DDL substring match.
- Stale-span timestamp comparison uses `datetime()` wrapping for safe ISO-8601 comparison.

### Changed
- CI runs only on PRs to main (not every push to every branch), with `concurrency: cancel-in-progress` to prevent run pile-ups. 10-minute job timeout added.

### Docs
- Expanded `docs/catalog.md` with tumbler addressing, link type guidance, span lifecycle, admin operations, and troubleshooting.
- Updated all 7 link-creating agents with `chash:` span format in tool signatures.
- Updated SubagentStart hook, session_start_hook, and CONTEXT_PROTOCOL with catalog-aware guidance.

## [3.0.0] - 2026-04-05

### Added
- **Document catalog with typed link graph** (RDR-049/050/051) — Xanadu-inspired document registry tracking every indexed document and the relationships between them. Tumblers (permanent hierarchical addresses) identify documents; typed links (`cites`, `supersedes`, `implements-heuristic`, `relates`) capture provenance.
  - `nx catalog setup` — one-command onboarding: init + populate from T3 + generate links
  - `nx catalog search` / `show` / `links` — find documents, browse metadata, traverse the link graph
  - `nx catalog link` / `unlink` — create and remove typed relationships
  - MCP tools: `catalog_search`, `catalog_show`, `catalog_list`, `catalog_register`, `catalog_update`, `catalog_link`, `catalog_links`, `catalog_unlink`, `catalog_link_query`, `catalog_link_audit`, `catalog_link_bulk`, `catalog_resolve`, `catalog_stats`
  - All indexing pathways auto-register in catalog (`index repo`, `index pdf`, `index rdr`, `index md`, MCP `store_put`)
  - Citation links from Semantic Scholar references (via `nx enrich`)
  - Code-RDR links auto-generated by title heuristic at index time
  - Span transclusion — links can reference specific line ranges or chunk positions
  - Permanent addressing — tumbler numbers are never reused, even after delete + compact
  - 12 agents/skills wired for catalog link creation and discovery
- **`defrag()`** — safe JSONL compaction that deduplicates overwrites but preserves tombstones. Auto-runs in `sync()`. Use `compact()` for full tombstone purge.

### Fixed
- **Silent data loss & corruption audit** (nexus-s5mf) — 11 bugs across 7 modules where errors were silently swallowed, causing data loss or corruption:
  - **P0**: CCE empty-result no longer falls through to voyage-4 (would corrupt vector space with mixed embedding models)
  - **P0**: Pipeline post-pass failures logged at WARNING and return bool; pipeline data preserved for retry on failure
  - **P0**: `_prune_stale_chunks` separates query/delete error handling; reports stale chunk count on delete failure
  - **P0**: `delete_pipeline_data` gated on all post-passes succeeding
  - **P0**: `git ls-files` failure in git repos raises RuntimeError instead of silently falling back to rglob (which would index .gitignored secrets)
  - **P1**: Catalog `_ensure_consistent` rebuild failure sets `degraded` flag and logs WARNING
  - **P1**: MCP `_get_catalog()` catches `OSError` only (was bare `except Exception`)
  - **P1**: `reindex_cmd` sourceless check paginates (was limit=100)
  - **P1**: T1 `list_entries` and `clear` paginate with limit=300 to avoid ChromaDB truncation
  - **P2**: `collection info` paginates for accurate MAX(indexed_at) timestamp
  - **P2**: T1 reconnect fallback to EphemeralClient logs WARNING about data loss
- **FTS5 dot/asterisk in queries** — `_sanitize_fts5` now quotes tokens containing `.`, `*`, `+`, `/`. Filenames like "types.py" no longer cause syntax errors in search or title resolution.
- **ChromaDB Cloud quota violations** — catalog setup handles rate limits with per-collection progress and timeouts.
- **RDR backfill pagination** — paginates through all chunks (was limited to first page).
- **Deadlock in sync→defrag** — fixed operation ordering.

### Changed
- **Breaking**: `catalog_links` MCP tool now returns `{"nodes": [...], "edges": [...]}` dict instead of flat edge list. Access edges via `result["edges"]`.
- `nx catalog links` command now handles both graph traversal (positional tumbler) and flat filter queries (`--created-by`, `--type`). The old `link-query` command is removed.
- Admin commands (`link-audit`, `link-bulk-delete`, `backfill`) are hidden from `--help` (still accessible).

### Upgrade notes
After upgrading, run `nx catalog setup` to create and populate the catalog. This is optional — everything works without it — but enables catalog search, link traversal, and agent citation queries. `nx doctor` will remind you.

## [2.12.0] - 2026-04-04

### Added
- **Streaming PDF pipeline** (RDR-048) — three-stage concurrent indexing pipeline (extractor → chunker → uploader) connected via a SQLite WAL buffer (`PipelineDB`). Replaces the sequential extract-then-chunk-then-embed pipeline for all PDFs. Pages stream to buffer as they're extracted; chunker processes the stable prefix while extraction continues; uploader pushes embedded chunks to T3 ChromaDB as they become available.
- **Crash recovery** — every page, chunk, and embedding is durably persisted in SQLite before the next stage processes it. Resume from any crash point (extraction, chunking, embedding, upload) with no re-work beyond the in-flight batch. Extraction metadata stored in `pipeline.db` for instant resume without re-extraction.
- **Incremental chunking** — chunker caches page text in memory, reads only new pages from SQLite (`O(new_pages)` not `O(all_pages)`), and holds back the last chunk until extraction completes (boundary may shift). Eliminates the O(pages^2) re-chunking overhead.
- **`--streaming` CLI flag** — `nx index pdf --streaming auto|always|never`. Default `auto` routes all PDFs through the streaming pipeline. `never` falls back to the batch+checkpoint path (RDR-047).
- **`PipelineCancelled` exception** — `on_page` callback raises on cancel, propagating through MinerU's subprocess batch loop for fast abort instead of silently skipping writes.
- **`on_page` streaming callback** on `PDFExtractor.extract()` — fires per page across all three backends (Docling, MinerU, PyMuPDF). Auto-mode Docling probe runs without callback to avoid double-firing; pages replayed from result if Docling wins.
- **Metadata enrichment post-pass** — after upload, queries T3 and enriches all chunks with source_title, source_author, extraction_method, page_count, is_image_pdf, has_formulas, chunk_count. Resolves key names from ExtractionResult (docling_title → pdf_title → filename).
- **table_regions post-pass** (RF-14) — tags chunks on table pages with `chunk_type=table_page` after extraction completes.
- **Stale chunk pruning** — after upload, deletes chunks from previous versions of the same PDF (uses full `content_hash` from metadata, not ID prefix).
- **`nx doctor --clean-pipelines`** — scans `pipeline.db` for orphaned entries (missing source PDF, stale running pipelines) and deletes them with cascade across all three buffer tables.
- **Incremental PDF upsert with checkpoints** (RDR-047) — batch-path crash recovery for the `--streaming never` path. Checkpoints track embed/upsert progress per batch.
- **Parallel CCE embedding** — `ThreadPoolExecutor(4)` with token-bucket rate limiter for Voyage API calls during embedding.
- **`nx config get` dotted-path traversal** — e.g. `nx config get pdf.extractor`.

### Fixed
- **Concurrent pipeline guard** — `create_pipeline` catches `IntegrityError` on concurrent INSERT (two processes indexing same file).
- **Credential resolution in streaming path** — `embed_fn=None` now resolved from `voyage_api_key` credential in the orchestrator, matching batch path behavior. Fast-fail `RuntimeError` when credentials are absent.
- **Auto-mode double `on_page`** — Docling probe no longer fires `on_page`; pages replayed from `page_boundaries` if Docling wins. Prevents `total_pages` showing double the actual count for formula PDFs.
- **Uploader completion guard** — removed early-exit on provisional `chunks_created` counter during incremental chunking (could cause premature completion). Resume path uses durable state.
- **Resume cursor** — counts all embedded chunks (both uploaded and not-yet-uploaded) to avoid re-embedding work on crash recovery.
- **Embedding heartbeat** — embeds in batches of 32 with `update_progress` between, preventing stale-pipeline detection during long embedding calls.
- **Schema migration** — `_migrate_if_needed` adds `extraction_meta` column to existing `pipeline.db` files.

### Docs
- `docs/rdr/rdr-048-streaming-pdf-pipeline.md` — full architecture spec with 16 research findings
- `docs/cli-reference.md` — `--streaming` flag, `--clean-pipelines`, `--clean-checkpoints`
- `docs/architecture.md` — `pipeline_buffer.py`, `pipeline_stages.py`, `checkpoint.py` in module table
- `CLAUDE.md` — new modules in source layout

## [2.11.2] - 2026-04-03

### Fixed
- **Visible progress during PDF extraction** — Docling and MinerU passes now print status to stderr via tqdm-safe `_progress()` helper that clears/refreshes active tqdm bars. Shows "Docling: extracting paper.pdf (formula detection)…" before the minutes-long enriched pass, "Formulas detected (N) — switching to MinerU", and per-page "MinerU: page N/M" during extraction.

## [2.11.1] - 2026-04-03

### Fixed
- **ChromaDB 32-metadata-key limit** — PDF extractors emit ~37 keys; `_write_batch` now strips empty droppable keys (pdf_creator, pdf_producer, etc.) while preserving load-bearing empty strings (expires_at="" for TTL). Hard truncation guard if still over limit.

## [2.11.0] - 2026-04-03

### Added
- **MinerU server-backed PDF extraction** (RDR-046) — `nx mineru start/stop/status` manages a persistent mineru-api server. HTTP client in pdf_extractor with subprocess fallback. Auto-restart on server OOM (2x budget). Dynamic port allocation.
- **Batch PDF indexing** — `nx index pdf --dir <path>` indexes all PDFs in a directory with progress `[i/N]`, timing, error isolation, and summary. Server-absent advisory.
- **`query` MCP tool** — document-level semantic search. Groups results by source document with full metadata (title, year, authors, citations, page count, extraction method). No LLM required.
- **`store_delete` MCP tool** — delete T3 knowledge entries by document ID
- **`memory_delete` MCP tool** — delete T2 memory entries by project and title
- **`search` `where` filter** — metadata filtering on MCP search and query tools. `KEY=VALUE` or `KEY>=VALUE` format, comma-separated. Numeric fields auto-coerced.
- **`store_list` `docs` mode** — document-level view deduplicating chunks by content_hash. Shows title, chunk count, page count, extraction method.
- **`collection_info` peek** — sample entry titles for collection discoverability
- **`scratch` delete action** — delete T1 scratch entries
- **Adaptive page ranges with OOM retry** — multi-page batch failure splits to 1-page retry. Config-driven `pdf.mineru_page_batch`.

### Fixed
- **CCE embedding model consistency** — eliminated voyage-4 fallback. On CCE batch failure, splits in half and retries with same model (voyage-context-3). Prevents embedding model mismatch within collections.
- **T3 list_store pagination** — real `offset` parameter passed to ChromaDB (was capped at 300 in cloud mode)
- **store_list title display** — falls back to `source_title` for PDF-indexed entries
- **`search` param `n` → `limit`** — consistent pagination parameter naming across all tools
- **FTS5 title search** — corrected documentation: memory_search searches title, content, and tags (was incorrectly documented as "title not searchable")
- **Agent tool discoverability** — all tool references use full `mcp__plugin_nx_nexus__` prefix. Fixed `mcp__sequential-thinking__` → `mcp__plugin_nx_sequential-thinking__` in 13 agent files.
- **`reinstall-tool.sh`** — symlinks `mineru-api` to `~/.local/bin` when `[mineru]` extra is present
- **`plan_save` schema** — documented minimal JSON schema in tool docstring

### Changed
- MCP tool count: 12 → 17
- `_CCE_TOKEN_LIMIT`: 32K → 24K (safety margin for academic text token estimation)
- Token estimate in CCE batching: `len//3` → `len//2` (conservative for academic text)

### Docs
- `reference.md` — 17 tools documented with full parameter tables and examples
- All 17 agent `.md` files updated with `nx Tool Reference` block and full tool names
- `CONTEXT_PROTOCOL.md` — full tool names throughout, search options table expanded
- `subagent-start.sh` — full tool names with `Tool:` prefix labels
- RDR-042, 043, 045, 046 closed

## [2.10.8] - 2026-04-02

### Changed
- **MinerU batched subprocess extraction** — large PDFs are now split
  into 5-page batches, each processed in an isolated subprocess. Prevents
  OOM on formula-dense documents (e.g. 108-page Grossberg 1986). GPU/model
  memory is fully reclaimed between batches.

### Docs
- Updated `cli-reference.md` and `architecture.md` with MinerU batching behavior.

## [2.10.7] - 2026-04-02

### Fixed
- **Preserve optional extras during reinstall** — added
  `scripts/reinstall-tool.sh` that reads `uv-receipt.toml` and preserves
  extras like `[mineru]` and `[local]` when reinstalling the CLI tool.
  Previously `uv tool install --reinstall .` silently dropped extras,
  breaking MinerU mid-session. Fixes #122.

## [2.10.6] - 2026-04-02

### Fixed
- **RDR close bead gate** — replaced "advisory only" bead status check
  with a hard gate that requires explicit user confirmation before closing
  an RDR with open or in-progress beads. Previously agents would see open
  beads and proceed to close anyway.

## [2.10.5] - 2026-04-02

### Fixed
- **MinerU extraction output paths** — updated `_extract_with_mineru` to
  match MinerU v2 `do_parse` API (positional output_dir, `pdf_bytes_list`,
  `p_lang_list`). Output directory now uses `pdf_path.name` (with extension)
  instead of `pdf_path.stem`. Tests updated to match.

## [2.10.4] - 2026-04-01

### Removed
- **PostToolUse prompt hook** — `type: "prompt"` is not valid for
  PostToolUse hooks, causing `PostToolUse:Bash hook error` on every
  Bash tool call. Removed entirely; `/nx:debug` remains available
  on demand.

## [2.10.3] - 2026-04-01

### Added
- **PostToolUse prompt hook** for debugger enforcement — detects
  repeated test failures and enforces `/nx:debug` invocation instead
  of manual retry loops.

## [2.10.2] - 2026-04-01

### Fixed
- **Restore skill routing guardrails** — re-added the Skill Directory
  tables, Process Flow graph, Storage Tier Protocol, and Red Flags
  anti-rationalization table to the `using-nx-skills` SessionStart
  injection. These were trimmed in RDR-039 for compactness but their
  removal caused agents to stop invoking specialized skills (debugger,
  architect, etc.).

## [2.10.1] - 2026-04-01

### Fixed
- **Verification hooks now advisory-only** — removed test suite
  execution from both Stop and PreToolUse hooks. Running tests inside
  hooks caused multi-minute delays on routine operations. Both hooks
  now perform fast checks only (uncommitted changes, open beads,
  review markers) and never block.
- **PreToolUse output format** — corrected to use `hookSpecificOutput`
  with `permissionDecision` (PreToolUse protocol), not `decision`/`reason`
  (Stop protocol).
- **Bead ID extraction** — fixed BSD sed compatibility on macOS (`sed -E`
  instead of GNU-only `\+`/`\b`/`\|`).

## [2.10.0] - 2026-04-01

### Added
- **Verification config** (RDR-045) — `_DEFAULTS["verification"]` section
  in `.nexus.yml` with `on_stop`, `on_close`, `test_command`, `lint_command`,
  `test_timeout` keys. New `get_verification_config()` and
  `detect_test_command()` in `config.py` with auto-detection for 7 project
  types (Maven, Gradle, Python, Node, Rust, Make, Go).

## [2.9.2] - 2026-03-31

### Changed
- **Eradicate superpowers references** — removed all live superpowers
  delegation from nx README, preflight check, and 4 skill files.
  nx is now fully self-contained with no superpowers dependency.
- **Move WIP tutorial to branch** — `docs/tutorial/` moved to
  `wip/tutorial` branch, off main.

## [2.9.1] - 2026-03-31

### Added
- **Math-aware PDF extraction (RDR-044)** — three-backend auto-detect
  routing: Docling detects formula regions, MinerU re-extracts math-heavy
  papers with superior LaTeX output, PyMuPDF normalized as terminal fallback.
  - `nx index pdf --extractor [auto|docling|mineru]` CLI option
  - `has_formulas` boolean on all PDF chunks for downstream filtering
  - `formula_count` in extraction metadata
- **Sticky PDF extractor config** — `nx config set pdf.extractor=mineru`
  sets the default globally (`~/.config/nexus/config.yml`) or per-repo
  (`.nexus.yml`). CLI `--extractor` flag overrides config when passed.
- **`nx config set` dotted keys** — `nx config set pdf.extractor=mineru`
  now writes nested YAML config, not just credentials.
- **MinerU optional dependency** — `uv pip install 'conexus[mineru]'`
  installs `mineru[all]` for math-aware extraction. ~2-3 GB model download
  on first run.

### Fixed
- **Missing Docling transitive deps** — added `python-pptx` and
  `opencv-python-headless` to fix Docling import failures on some platforms.

## [2.8.5] - 2026-03-30

### Changed
- **Plan-enricher widened scope (RDR-043)** — reframed from audit-findings
  delivery to general bead enrichment. Execution context (file paths, code
  patterns, constraints, test commands) is now the primary purpose. Audit
  findings incorporated when available, no longer required. "Degraded mode"
  removed.
- **Review gates in plans** — strategic planner includes mandatory
  code-review-expert tasks after implementation phases.

## [2.8.4] - 2026-03-30

### Added
- **Review gates in plans** — strategic planner now includes mandatory
  code-review-expert tasks after implementation phases in every plan.
- **Bib enrichment opt-in** — `nx index pdf --enrich` flag wired through
  CLI (was documented but not implemented). Default is off.

### Fixed
- **Pagination correctness** — `store_list` uses true collection count,
  `search` footer distinguishes "may have more" from "end". Standardized
  footer format across all paged tools. 11 pagination tests added.
- **Empty `--where` values rejected** — `key=` and `key>=` raise clear
  CLI errors instead of passing empty strings to ChromaDB.

## [2.8.3] - 2026-03-30

### Changed
- **Bib enrichment default flipped to opt-in** — `nx index pdf` no longer
  queries Semantic Scholar by default. Pass `--enrich` to enable inline
  metadata lookup. Use `nx enrich <collection>` for deliberate backfill.
- **MCP tool pagination** — `search`, `store_list`, and `memory_search`
  return paged results with `offset` parameter. Standardized footer:
  `--- showing X-Y of Z. next: offset=N` / `(end)`. `store_list` uses
  true collection count. No data lost — agents page through all results.
- **Hook output optimized for AI** — SubagentStart reduced 43% (6.3K→3.6K
  chars), SessionStart reduced 55%. Same information, structured for LLM
  parsing.

### Fixed
- **sn plugin Serena hook** — clarified `jet_brains_*` tools work with any
  LSP backend (not IntelliJ-specific). Added `find_file`, `list_dir`,
  Serena memories. Full MCP-prefixed tool names. Context7 prefixes fixed.
- **`--where` empty values rejected** — `key=` and `key>=` now raise
  `BadParameter` instead of silently passing empty strings to ChromaDB.
- **`store_list` missing collection** — returns "Collection not found"
  instead of misleading "No entries".

## [2.8.2] - 2026-03-30

### Added
- **SessionStart capabilities summary** — main conversation now gets a
  compact overview of `--where` operators, `/nx:query` pipeline, `nx enrich`,
  and plan library MCP tools on every session start.

## [2.8.1] - 2026-03-30

### Fixed
- **T2 plans table migration** — existing `memory.db` files created during
  v2.8.0 before the `project` column was added now auto-migrate on open.
  `ALTER TABLE plans ADD COLUMN project` + FTS5 rebuild runs transparently.

## [2.8.0] - 2026-03-30

### Added
- **Analytical query pipeline** — `/nx:query` skill decomposes complex questions
  into multi-step plans (search → extract → summarize → compare → generate),
  dispatched via new `query-planner` and `analytical-operator` agents. Step
  outputs persist in T1 scratch for cross-dispatch reference. (RDR-042)
- **Bibliographic metadata enrichment** — `nx index pdf` queries Semantic Scholar
  for year, venue, authors, and citation count. Opt-in with `--enrich`.
  Backfill existing collections with `nx enrich <collection>`. (RDR-042)
- **Structured table detection** — PDF chunks on pages containing tables are
  tagged `chunk_type=table_page`. Filter with `--where chunk_type=table_page`.
  Page-level granularity via Docling `TableItem` detection. (RDR-042)
- **`--where` comparison operators** — `nx search --where` now supports `>=`,
  `<=`, `>`, `<`, `!=` in addition to `=`. Known numeric fields (`bib_year`,
  `bib_citation_count`, `page_count`, etc.) are auto-coerced to int. (RDR-042)
- **T2 plan library** — `plans` table with FTS5 search, project scoping, and
  MCP tools (`plan_save`, `plan_search`). Saves successful query execution
  plans for future reuse. (RDR-042)
- **Orchestrator self-correction** — failure relay protocol distinguishes
  ESCALATION sentinels (route to debugger) from incomplete output (retry up
  to 2x with augmented context). (RDR-042)
- **NDCG retrieval smoke test** — synthetic corpus + ground-truth queries in
  `tests/benchmarks/` verify the search pipeline runs end-to-end with ONNX
  MiniLM. (RDR-042)

### Fixed
- **sn plugin Serena hook** — SubagentStart hook now uses full MCP-prefixed
  tool names (`mcp__plugin_sn_serena__*`) so subagents can actually resolve
  and call Serena tools. Previously used short names that subagents couldn't
  find.

## [2.7.1] - 2026-03-28

### Added
- **nx: three-tier storage guidance for all agents** — SubagentStart hook injects
  nx MCP tool signatures (T1 scratch, T2 memory, T3 search/store) into every
  subagent. Non-nx agents (general-purpose, superpowers, etc.) can now use
  T1 scratch for inter-agent communication, read T2 project context, and query
  the T3 knowledge store.

## [2.7.0] - 2026-03-28

### Added
- **sn plugin** — new lightweight Claude Code plugin that bundles Serena and
  Context7 MCP servers with a SubagentStart hook that injects tool usage
  guidance into all subagents. Serena configured with `--context claude-code`
  (minimal tool surface) and `--project-from-cwd` (auto-detect project).
  Install independently: `/plugin install sn@nexus-plugins`.
- **nx: sequential thinking injection** — SubagentStart hook injects usage
  guidance for the sequential thinking MCP tool.

## [2.6.1] - 2026-03-27

### Fixed
- **rdr-accept planning detection** — was matching only `## Implementation Plan`
  with `### Phase` subheadings. Now scans 6 section names (Implementation Plan,
  Approach, Plan, Design, Steps, Execution) and 4 subheading types (Phase, Step,
  Stage, Part, plus numbered `###`). Default flipped from "no" to "yes" — false
  positives are cheap, false negatives skip planning on complex work.

### Docs
- Tutorial video scripts (sections 0-9), companion cheatsheet
- Automated recording pipeline: expect + asciinema + agg + ffmpeg + speed-mapping
- `make` in `docs/tutorial/vhs/` reproduces the full demo video from scratch

## [2.6.0] - 2026-03-26

### Added
- **T1 scratch inter-agent context sharing** (RDR-041) — standardized scratch
  tag vocabulary (`impl`, `checkpoint`, `failed-approach`, `hypothesis`,
  `discovery`, `decision`), sibling context SHOULD for relay-reliant agents
  with relay-over-scratch precedence rule, developer writes failed approaches
  to scratch, code reviewer checks scratch for developer struggles before
  reviewing, debugger checks scratch for predecessor findings.
- **Debugger escalation relay** includes `nx scratch` field for pre-escalation
  failed-approach entries.
- **Re-dispatch developer relay template** with structured nx store/memory
  artifact references from debugger output.
- **Escalation guard** — if developer circuit breaker fires twice for the same
  bead, escalate to human instead of infinite developer→debugger loop.

## [2.5.0] - 2026-03-25

### Added
- **Developer agent circuit breaker** (RDR-040) — after 2 consecutive test
  failures, the developer agent stops and outputs a structured ESCALATION
  report. The parent dispatches the debugger with the failure context. Counter
  tracks test runs (not root causes), resets on green or new invocation.
  Supersedes the advisory "Recommend debugger" escalation trigger.
- **Debugger escalation relay template** in development skill — parent-side
  dispatch instructions with field mapping from escalation report to debugger
  relay.
- **Developer → debugger routing** in orchestration skill — escalation edge
  in routing diagram and quick reference table.

## [2.4.2] - 2026-03-25

### Docs
- **Python 3.14 troubleshooting** — `uv tool update` reuses the existing
  environment's Python, so upgrading under 3.14 doesn't auto-switch to 3.13
  despite the `requires-python` cap. Documented `--force --python 3.13` as
  the fix. Added `head -1 $(which nx)` diagnostic.

## [2.4.1] - 2026-03-24

### Fixed
- **`--collection` flag bypass of `t3_collection_name()`** — `nx index pdf --collection knowledge` now correctly normalizes to `knowledge__knowledge`, matching search conventions. Previously created bare collections invisible to `nx search` with wrong embedding model.
- **`memory promote --collection` same bug** — bare collection names in `nx memory promote` now normalized via `t3_collection_name()`.
- **Updated `--collection` help text** — no longer says "Fully-qualified" since bare names are now accepted and auto-normalized.
- **Updated CCE post-mortem** — linked RDR-040 resolution and documented the `--collection` naming variant.

## [2.4.0] - 2026-03-24

### Added
- **`nx collection reindex <name>`** — delete and re-index a collection from source files with pre-delete safety check, per-type dispatch (code/docs/rdr/knowledge), and post-reindex verification (A4)
- **`collection_list` MCP tool** — list all T3 collections with document counts and models (B2)
- **`collection_info` MCP tool** — detailed collection metadata including index/query models (B3)
- **`collection_verify` MCP tool** — known-document retrieval health probe (B4)
- **Per-chunk progress for pdf/md indexing** — `--monitor` now shows tqdm bar during embedding, not just post-hoc metadata (A5)
- **Retrieval quality unit tests** — assert semantic rank ordering with real ONNX embeddings (A1)
- **Cross-model invariant regression test** — fails if CCE index/query models diverge (A3)

### Fixed
- **Single-chunk CCE model mismatch** — documents with only 1 chunk in CCE collections now use `contextualized_embed()` instead of falling back to `voyage-4`, which produced vectors in an incompatible space (C1)
- **Unpaginated `col.get()` in indexer** — `_prune_deleted_files`, `_prune_misclassified`, and `_run_index_frecency_only` now paginate at 300 records to handle ChromaDB Cloud's hard cap (C2/C3)
- **Mixed-model CCE batches** — partial CCE failure now re-embeds the entire document with voyage-4 for consistency, preventing mixed-space vectors (C4)
- **MCP collection cache race** — `_get_collection_names()` uses atomic tuple assignment to eliminate the window where concurrent threads could see an empty list (C5)
- **`info_cmd` unbounded `col.get()`** — now uses `limit=300` for best-effort timestamp sampling
- **`reindex_cmd` corpus metadata** — derives corpus from collection name instead of storing empty string

### Changed
- **MCP `search` default** — changed from `corpus="knowledge"` to `corpus="knowledge,code,docs"` matching CLI behavior; added `"all"` alias for all corpora including rdr (B1)
- **`collection verify --deep`** — enhanced with known-document probe, distance reporting, and `VerifyResult` dataclass (A2)

### Docs
- Updated CLI reference, architecture docs, MCP tool reference, CLAUDE.md, and nx plugin CHANGELOG for all RDR-040 changes (D1–D6)

### References
- RDR-040: CCE Post-Mortem Gap Closure & MCP Server Enhancement
- Post-mortem: `docs/rdr/post-mortem/cce-query-model-mismatch.md`
- Epic: nexus-5rn1 (16 beads, all closed)
- PR: #118

## [2.3.6] - 2026-03-23

### Fixed
- **Restore voyageai as required dependency** — the `conexus[cloud]` optional extra
  was an unnecessary workaround. Since `requires-python < 3.14` blocks the only
  incompatible Python version, voyageai always works on supported Pythons. Reverted
  to direct `import voyageai` with no guards. Removed the `cloud` extra.

## [2.3.5] - 2026-03-23

### Docs
- **Streamlined getting-started guide** — linear flow from prerequisites through
  install, verify, use, plugin, cloud. Added `nx doctor` verify step, Python 3.14
  workaround, `uv tool update` instructions, and `conexus[cloud]`/`conexus[local]`
  extras documentation.
- **Three-pass substantive critique** — fixed query model docs (CCE collections use
  voyage-context-3 for both index and query), removed "T3 cloud" mislabeling from
  CLI reference, corrected tuning YAML structure (nested subsections, not flat keys),
  fixed local-mode auto-detection docs (either key absent, not both), added missing
  `--on-locked` flag and `NX_VOYAGEAI_READ_TIMEOUT_SECONDS` env var, corrected
  minified code detection description, and replaced all `pip install` with `uv` syntax.
- **Unprefixed skill references** — corrected `/rdr-create` → `/nx:rdr-create` etc.
  across all documentation and RDR files.

### Fixed
- **`is_local_mode()` docstring** — corrected to match implementation (either key
  absent triggers local mode, not both).

## [2.3.4] - 2026-03-23

### Fixed
- **Python 3.14 compatibility** — `voyageai` uses Pydantic v1 compat layer which
  is broken on Python ≥ 3.14. Moved `voyageai` from required to optional dependency
  (`pip install conexus[cloud]`). Capped `requires-python` to `<3.14` so uv/pip
  auto-select Python 3.13. All `import voyageai` sites guarded with clear error
  messages pointing to `conexus[cloud]`.
- **Unprefixed skill references in docs** — all `/rdr-create`, `/rdr-close`, etc.
  corrected to `/nx:rdr-create`, `/nx:rdr-close` across 11 documentation files.

## [2.3.3] - 2026-03-23

### Fixed
- **Python 3.14 compatibility** (partial) — guarded `import voyageai` in `t3.py`
  but missed `retry.py` and other import sites. Superseded by 2.3.4.

## [2.3.2] - 2026-03-22

### Fixed
- **Planning chain bypass prevention** — agents can no longer skip the
  strategic-planner → plan-auditor → plan-enricher chain by creating beads
  directly or compensating when subagents fail. PROHIBITION block added to
  rdr-accept, chain mandatory for multi-phase RDRs.
- **Silent bead content corruption** — `bd update --description "..."` silently
  destroys multi-line markdown (backticks, `$variables`, nested quotes). Replaced
  with Write tool → `--body-file` pattern in plan-enricher agent and skill.
- **Dead T2 idempotency code** — removed Python comparisons against always-None
  `t2_status`; self-healing logic moved to Action section with live `memory_get`.
- **Unbound placeholders** — fixed `{id}`, `{t2_status}`, `{repo_name}`, `{type}`
  leaking from Python into agent instructions; standardized to `<ID>` notation.

### Added
- **Known Pitfalls** section in writing-nx-skills skill — documents the
  `--description` corruption bug so future agent authors use `--body-file`.

## [2.3.1] - 2026-03-22

### Fixed
- **StopFailure hook junk beads** — guarded side effects behind `CLAUDECODE` env var
  so test runs no longer create junk beads and memories via `bd`.

## [2.3.0] - 2026-03-22

### Added
- **PostCompact hook** — re-injects in-progress bead state and T1 scratch entries
  after conversation compaction. Only emits output when there is content to show.
- **StopFailure hook** — logs API failure context to beads memory for observability.
  Creates a blocker bead on rate limits. Handles null `error_details` gracefully.
- **Integration tests in release checklist** — `uv run pytest -m integration` is now
  a required pre-release step in `docs/contributing.md`.

### Fixed
- **Test isolation** — patched `get_credential` in T3/store tests to prevent
  `~/.config/nexus/config.yml` from leaking real credentials into unit tests.
- **PostCompact scratch test** — no longer false-fails on CI when `nx scratch list`
  returns no entries.

### Docs
- RDR-039 closed: all 4 phases implemented.

## [2.2.0] - 2026-03-21

### Changed
- **Plugin hooks cleanup** — removed 5 dead/redundant hook scripts
  (`mcp_health_hook.sh`, `setup.sh`, `bead_context_hook.py`,
  `permission-request-stdin.sh`, `readonly-agent-guard.sh`) and 3 hook events
  (Setup, PostToolUse, PermissionRequest). Hooks reduced from 9 to 5.
- **Orchestrator upgraded** from haiku to sonnet — routing ambiguous requests
  needs reasoning depth.
- **T2 memory dedup** — removed duplicate T2 output from `session_start()`;
  `session_start_hook.py` via `t2_prefix_scan.py` is the single source.

### Fixed
- **rdr_hook.py** — added `closed` status to `_STATUS_ORDER` (was missing,
  caused wrong reconciliation direction), terminal conflicts now warn instead
  of auto-reconciling, fixed `_update_file_status` blank-line accumulation,
  reads `.nexus.yml` for RDR path instead of hardcoding `docs/rdr`.
- **"Task tool" → "Agent tool"** — corrected 19 stale references across skills,
  commands, and relay template.

## [2.1.1] - 2026-03-15

### Fixed
- **Plugin skill references** — all 19 nx plugin files now use fully-qualified
  `/nx:skill-name` form instead of short `/skill-name` which Claude Code cannot
  resolve for plugin-namespaced skills. Affected agents, commands, hooks, skills,
  and README.

## [2.1.0] - 2026-03-15

### Added
- **Local T3 backend** (RDR-038) — zero-config semantic search using ChromaDB
  `PersistentClient` + bundled ONNX MiniLM embeddings. `pip install conexus &&
  nx index repo . && nx search "query"` works with no API keys.
- `is_local_mode()` auto-detection: activates local mode when cloud credentials
  are absent. Force with `NX_LOCAL=1` or `NX_LOCAL=0`.
- `LocalEmbeddingFunction` with two tiers: tier 0 (bundled all-MiniLM-L6-v2,
  384d) and tier 1 (fastembed bge-base-en-v1.5, 768d via `pip install conexus[local]`).
- `NX_LOCAL_CHROMA_PATH` env var to override local ChromaDB storage path
  (default: `~/.local/share/nexus/chroma`).
- `nx doctor` shows local mode health checks: path, embedding model, collection
  count, disk usage. Cloud checks skipped in local mode.
- `[local]` optional dependency group: `pip install conexus[local]` for better
  embedding quality via fastembed.
- `sqlite3.OperationalError('database is locked')` added to retryable errors
  for PersistentClient concurrent write handling.
- Indexer pipeline local mode: `embed_fn` injection in `IndexContext`, local
  embedding in code/prose/PDF indexers.
- Search reranker skipped in local mode (no Voyage AI reranker available).
- `memory promote` uses `make_t3()` — works seamlessly in both local and cloud mode.

### Changed
- `T3Database.__init__` accepts `local_mode` and `local_path` parameters
  (first branch, before cloud probe).
- `make_t3()` returns local or cloud T3Database based on `is_local_mode()`.
- `store.py` `_t3()` skips cloud credential checks in local mode.
- MAX_QUERY_RESULTS clamping and CCE embedding paths gated on `_local_mode`.

### Docs
- `getting-started.md`: local-first zero-config section before cloud setup.
- `configuration.md`: local mode config reference (NX_LOCAL, NX_LOCAL_CHROMA_PATH).
- `storage-tiers.md`: local vs cloud T3 comparison table with tier details.
- `architecture.md`: updated T3 description for local/cloud backends.
- `README.md`: updated Quick Start and tier table for zero-config local mode.
- `CLAUDE.md`: updated T3 description and source layout for `local_ef.py`.

## [2.0.0] - 2026-03-14

### Breaking Changes
- **T3 storage consolidated from 4 databases to 1** (RDR-037) — `chroma_database`
  is now the actual database name, not a base prefix. All collection prefixes
  (`code__*`, `docs__*`, `rdr__*`, `knowledge__*`) coexist in a single ChromaDB
  Cloud database.
  - `nx config init` provisions 1 database instead of 4
  - `nx doctor` checks 1 database instead of 4
  - Old four-database layout is auto-detected on startup with migration guidance
  - Set `NX_MIGRATED=1` after migrating to skip the probe
  - **Migration is non-destructive** — old databases are never modified or deleted.
    They remain in your ChromaDB Cloud dashboard until you choose to remove them.
  - Migration steps:
    1. Export with the **pre-upgrade** version: `nx store export --all`
    2. Upgrade nexus
    3. Provision single DB: `nx config init` (creates `{chroma_database}`)
    4. Re-index repos: `nx index repo .`
    5. Import stored knowledge: `nx store import`
    6. Set flag: `export NX_MIGRATED=1` (or `nx config set migrated 1`)
    7. Verify: `nx doctor`
    8. Optional: delete the 4 old databases from the ChromaDB Cloud dashboard

### Changed
- `T3Database.__init__` uses probe-first single-client connection (was four-client loop)
- `_client_for()` is now a shim returning the single client (routing removed)
- `ensure_databases()` creates 1 database (was 4)
- `OldLayoutDetected` exception raised when old `{base}_code` database still exists

## [1.12.1] - 2026-03-14

### Docs
- **README intro paragraph** — rewritten for clarity: leads with what Nexus is,
  then what it provides, then the compounding value proposition.

## [1.12.0] - 2026-03-13

### Docs
- **README rewrite** — problem-first framing centered on knowledge management
  lifecycle rather than repository indexing. Three intro paragraphs: context loss
  problem, Nexus as solution with compounding knowledge, RDR as human-AI design
  system for team alignment.
- **Three tiers, one lifecycle** — storage tier section rewritten to explain why
  each tier exists (different lifetimes, different access patterns) and how agents
  use them cooperatively. T1 consistently framed as inter-agent coordination, not
  developer scratch pad.
- **Getting Started reorganized** — local-first flow: Install → T1/T2 (no keys) →
  Claude Code plugin → T3 semantic search. Readers get value before configuring
  cloud credentials.
- **RDR documentation overhaul** — Overview, Workflow, Nexus Integration, and
  Templates all edited for readability. Reduced density, removed duplication across
  documents, flattened deep heading nesting, removed excess section dividers.
- **docs/README restructured** — Core Concepts / RDR / Plugin / Reference grouping
  with improved descriptions.
- **Cross-document consistency** — T1 terminology, Getting Started descriptions,
  nav bar headers/footers, and link targets aligned across all docs.

## [1.11.1] - 2026-03-13

### Fixed
- **rdr-accept chain orchestration** — the planning chain (strategic-planner →
  plan-auditor → plan-enricher) broke after the planner completed because agents
  relied on impossible agent-to-agent relay (subagents cannot spawn subagents).
  The accept skill now explicitly orchestrates all three sequential dispatches.
- **Agent handoff model** — replaced "Successor Enforcement" sections across all
  15 agents with "Recommended Next Step" output blocks. Agents now output structured
  handoff recommendations; the caller (skill or main conversation) dispatches the
  next agent. Removes dead code that instructed agents to use tools they don't have.
- **Template variable mismatches** — `{rdr_file_path}` and `{path}` corrected to
  `{rdr_file}` in rdr-accept command and skill
- **Stale "spawn" imperatives** — architect-planner and developer agents updated
  from "spawn X" to output-oriented language matching the new handoff model
- **enrich-plan skill** added to using-nx-skills directory (was missing from
  skill registry table)
- **Flaky test on Python 3.13** — `test_entries_6_to_8_title_only` failed in CI
  because all entries shared the same second-level timestamp, making SQLite
  ordering non-deterministic. Test now asserts on snippet/title-only counts
  rather than specific entry names.

## [1.11.0] - 2026-03-12

### Added
- **Post-accept planning workflow** (RDR-036) — `/rdr-accept` now offers an optional
  planning handoff after acceptance: auto-detects multi-phase RDRs (2+ phases defaults
  yes), dispatches `strategic-planner → plan-auditor → plan-enricher` chain to create
  and enrich execution beads at accept time rather than close time
- **plan-enricher agent** (sonnet) — terminal node in planning chain; enriches beads
  with audit findings, execution context, file paths, and codebase alignment
- **`/enrich-plan` skill and command** — invoke plan-enricher standalone or as part of
  the RDR planning chain
- **Conditional successor routing in plan-auditor** — uses T1 `rdr-planning-context`
  tag with RDR ID correlation to route to plan-enricher only in RDR planning context

### Changed
- **`/rdr-close` bead decomposition replaced with advisory** — close no longer creates
  beads; displays a read-only bead status table (if beads exist from accept-time
  planning) and lets the human decide which to close
- **strategic-planner Phase 3** renamed from "Audit and Iteration" to "Audit Handoff";
  removed aspirational "iterate based on audit feedback" instruction
- Plugin now has 15 agents (was 14) and 28 skills (was 27)

## [1.10.3] - 2026-03-12

### Fixed
- **PyPI README links** — converted all relative markdown links to absolute GitHub URLs
  so documentation links work on the PyPI project page

### Docs
- Updated RDR section in README to reflect actual usage (35+ RDRs and counting) rather
  than hypothetical projections; added concrete cross-reference example (RDR-035/023)

## [1.10.2] - 2026-03-12

### Fixed
- **Remove `tools:` frontmatter from all 14 agents** (RDR-035) — Claude Code has a
  confirmed bug where explicit `tools:` declarations in plugin-defined agents filter
  out MCP tools, rendering the MCP server non-functional for subagents. Agents now
  inherit all tools from the parent session; the PermissionRequest hook remains as
  runtime enforcement.

### Docs
- Updated `nx/README.md` and `docs/contributing.md` to document the `tools:` bug
- Added supersession note to RDR-023, post-implementation note to RDR-034
- Created and closed RDR-035

## [1.10.1] - 2026-03-11

### Fixed
- Removed `SessionEnd` hook — Claude Code cancels hooks during process teardown,
  producing a spurious "Hook cancelled" error on every exit. The T1 server stops
  automatically when the process tree dies; the hook was effectively a no-op.

## [1.10.0] - 2026-03-11

### Added
- **MCP server for agent storage operations** (RDR-034) — FastMCP server (`nx-mcp`)
  exposing 8 structured tools for direct T1/T2/T3 access by agents without Bash
  dependency. Tools: `search`, `store_put`, `store_list`, `memory_put`, `memory_get`,
  `memory_search`, `scratch`, `scratch_manage`. Thread-safe lazy singletons with
  double-checked locking for T1/T3; per-call context managers for T2. Collection
  name cache with 60s TTL and short-circuit for fully-qualified corpus names.
  Entry point: `nx-mcp = "nexus.mcp_server:main"` in pyproject.toml.
- **Plugin migration to MCP tools** — all 14 agents, shared protocols, and 9 skills
  updated from CLI syntax (`nx scratch put ...`) to MCP tool syntax
  (`mcp__plugin_nx_nexus__scratch`). Human-facing docs (`docs/`) retain CLI syntax.

### Changed
- `id` parameter renamed to `entry_id` in `scratch()` and `scratch_manage()` MCP tools
  to avoid shadowing Python builtin.

### Docs
- Architecture diagram updated with dual Human→CLI / Agent→MCP access paths.
- Storage tiers doc notes two access paths (CLI for humans, MCP for agents).
- Plugin README expanded with full MCP Servers section and permission auto-approval.
- Contributing guide notes MCP tool requirements for agent authoring.

## [1.9.1] - 2026-03-10

### Docs
- **Documentation audit for 1.9.0 features** — all user-visible features now documented:
  - `architecture.md`: module map updated with decomposed indexer modules (`code_indexer.py`,
    `prose_indexer.py`, `index_context.py`, `indexer_utils.py`, `languages.py`) and `exporter.py`
  - `configuration.md`: new `[tuning]` section documenting all `TuningConfig` parameters
  - `storage-tiers.md`: T3 export/import section with usage examples and format description
  - `repo-indexing.md`: CODE extension count corrected (52), pipeline versioning section,
    minified code handling section
  - `README.md`: store command description updated
- **Release process hardened** — `docs/contributing.md` step 2 now requires a mandatory docs
  audit against `git log` before every release, with a checklist of docs to verify. Quick
  reference table expanded to list all docs that may need updates.

## [1.9.0] - 2026-03-10

### Added
- **Hybrid search score boosting** (RDR-026) — ripgrep exact-match results boost
  vector search scores by `EXACT_MATCH_BOOST=0.15`. Pre-reranker capture of
  `rg_file_paths` and `rg_matched_lines` metadata for downstream context windowing.
  Ripgrep-only results (files not in vector top-K) kept with `RG_FLOOR_SCORE * 0.8`
  penalty. Snapshot regression tests for search quality via syrupy.
- **Context line windowing** (RDR-027 Phase 1) — `-A`/`-B`/`-C` flags now center
  on matching lines within chunks (keyword match or rg_matched_lines) rather than
  always showing from chunk start. `-C N` changed from after-only alias to
  before+after (matching grep semantics). Bridge merging joins nearby matches
  separated by ≤2 lines.
- **Syntax highlighting** (RDR-027 Phase 2) — `--bat` flag pipes results through
  `bat` with per-file batching, merged line ranges, and graceful fallback. Skipped
  when `--no-color` or `NO_COLOR` is set.
- **Compact mode** (RDR-027 Phase 3) — `--compact` flag outputs one line per result
  in `path:line:text` format (grep-compatible).
- **Query-aware vimgrep** — `--vimgrep` now reports the best-matching line within
  the chunk when a query is provided, not always the first line.
- **Unified language registry** (RDR-028) — consolidated `LANGUAGE_REGISTRY` in
  `nexus.languages` maps 44 file extensions to 31 tree-sitter AST languages.
  Single source of truth replaces scattered `AST_EXTENSIONS`, `_COMMENT_CHARS`,
  and classifier extension sets. 8 new AST languages: Clojure, Dart, Elixir,
  Erlang, Haskell, Julia, OCaml, Perl.
- **Pipeline version stamping** (RDR-029) — `PIPELINE_VERSION` constant (currently 4)
  stored in collection metadata. `--force-stale` flag on `nx index repo` re-indexes
  only collections whose stamped version is outdated. `nx doctor` reports pipeline
  version status per collection.
- **Collection export/import** (RDR-031) — `nx store export` writes collections to
  portable `.nxexp` files (JSON header + gzip-compressed msgpack stream of records
  with embeddings). `nx store import` restores without re-embedding. Supports
  `--include`/`--exclude` glob filters, `--all` for bulk export, `--remap` for
  path substitution on import, and `--collection` for rename on import. Embedding
  model mismatch is rejected to prevent vector space corruption.
- **`nx store get`** — retrieve a T3 entry by its 16-char hex document ID, with
  optional `--json` output.
- **Minified code handling** — AST chunker detects minified files (avg line length
  > 500 chars) and falls back to byte-based splitting instead of producing
  single-chunk monsters.

### Changed
- **Indexer module decomposition** (RDR-032) — `indexer.py` split into focused
  modules: `code_indexer.py` (AST chunking + context extraction), `prose_indexer.py`
  (markdown indexing), `index_context.py` (IndexContext dataclass), `indexer_utils.py`
  (shared utilities). Backward-compatible re-exports from `nexus.indexer`.
- **TuningConfig externalized** (RDR-032) — `vector_weight`, `frecency_weight`,
  `file_size_threshold`, `ripgrep_timeout`, `pdf_chunk_chars`, and other knobs
  now read from `~/.config/nexus/config.yml` `[tuning]` section. Defaults derived
  from `TuningConfig()` dataclass to prevent drift.

### Fixed
- **Reliability hardening** (RDR-030) — silent error audit across 24 catch-and-pass
  blocks. All `except` blocks now log via structlog at appropriate levels. Log output
  directed to stderr; warnings suppressed in structured search output. T2 FTS5
  title field added to index for memory search.
- **Streaming export/import** — export writes page-by-page directly to gzip stream
  instead of accumulating all records in memory. Import flushes batches as records
  are unpacked from a single file handle (eliminates TOCTOU window). msgpack
  Unpacker limited to 10 MB buffer to prevent memory exhaustion on crafted input.
- **IndexContext.voyage_key** marked `repr=False` to prevent API key leakage in
  logs and tracebacks.
- **Empty remap prefix guard** — `nx store import --remap ":foo"` now raises
  `UsageError` instead of silently matching every path.
- **Code indexer double-encode fix** — content hashing uses `source_bytes` directly
  instead of re-encoding from the already-decoded string.

### Docs
- `cli-reference.md` updated with `nx store get`, `nx store export`, `nx store import`,
  and `--force-stale` flag documentation.

### Tests
- 2209 tests (up from ~2050 in 1.8.0). New coverage for `_extract_context` (5 AST
  scenarios), `index_code_file` happy path, `index_prose_file` non-markdown path,
  exporter edge cases (empty collection, corrupt msgpack, remap validation),
  and `TuningConfig` wiring.

## [1.8.0] - 2026-03-08

### Changed
- **Language-agnostic agents** (RDR-025) — renamed 3 Java-specific agents to
  language-agnostic names: `java-developer` → `developer`, `java-debugger` →
  `debugger`, `java-architect-planner` → `architect-planner`. Agents now read
  CLAUDE.md at runtime to detect language, build system, test command, and coding
  conventions. Slash commands renamed: `/java-implement` → `/implement`,
  `/java-debug` → `/debug`, `/java-architecture` → `/architecture`.
- **Plugin registry updated** — all pipelines, predecessor/successor chains,
  naming aliases, and model summary reflect new agent names.

### Added
- **CLAUDE.md preflight check** — `/nx-preflight` now includes a section 6 that
  validates CLAUDE.md has language, build system, and test command information.
  Missing sections show `[?]` warnings (not errors).

## [1.7.1] - 2026-03-07

### Added
- **Project-local `/release` skill** — enforces the full release checklist from
  `docs/contributing.md` as an actionable step-by-step workflow. Prevents skipping
  steps like `uv tool install --reinstall` or using `gh release create` instead
  of `git tag`.

## [1.7.0] - 2026-03-07

### Added
- **Agent tool permissions** (RDR-023) — all 14 nx agents now have explicit `tools`
  frontmatter following least-privilege assignments. Each agent declares only the
  tools it needs (Read/Grep/Glob, Bash, Write/Edit, WebSearch/WebFetch, Agent).
  Sequential thinking MCP tool added to all agents uniformly.
- **PermissionRequest hook expansion** (RDR-023) — auto-approve safe non-Bash tools
  (Read, Grep, Glob, Write, Edit, WebSearch, WebFetch, Agent, sequential thinking)
  so subagents are not silently denied. Bash allowlist expanded with `uv run pytest`,
  additional `bd` subcommands, and read-only `git branch`/`git tag` forms.
- **RDR process guardrails** (RDR-024) — soft-warning pre-checks at three workflow
  points to catch implementation attempts on ungated/unaccepted RDRs:
  brainstorming-gate skill (step 6), strategic-planner relay validation (step 6),
  and bead context hook (regex RDR-NNN detection).

### Fixed
- **git branch/tag hook patterns** — restricted to read-only forms only (`git branch -a`,
  `git tag -l`). Previously, bare `branch` and `tag` matched destructive operations
  like `git branch -D` and `git tag -d`.

## [1.6.1] - 2026-03-06

### Fixed
- **PermissionRequest hook** — auto-approve all `nx *` subcommands (previously only read-only
  subcommands were approved). `nx collection delete` is explicitly denied and requires
  user confirmation. New subcommands added in future releases are approved automatically.

## [1.6.0] - 2026-03-06

### Added
- **`nx memory delete`** (RDR-022) — delete T2 memory entries by `--project`/`--title`,
  `--id`, or `--project`/`--all`. Confirmation prompt shows `project/title` and content
  preview. `--yes` bypasses prompts. `--all` requires `--project` and is mutually
  exclusive with `--title` and `--id`.
- **`nx store delete`** (RDR-022) — delete T3 knowledge entries by exact 16-char `--id`
  or by `--title` (exact metadata match, paginated to handle multi-chunk documents).
  `--collection` is required. `--yes` bypasses the `--title` confirmation prompt.
- **`nx scratch delete`** (RDR-022) — delete a T1 scratch entry by ID prefix (as shown
  by `nx scratch list`). No confirmation prompt (T1 is ephemeral). Session ownership is
  verified before deleting — entries from other sessions cannot be removed.
- `T2Database.delete()` overloaded with `id: int | None` keyword argument, matching the
  `get()` API pattern.
- `T3Database.delete_by_id()`, `find_ids_by_title()` (paginated), `batch_delete()`.
- `T1Database.delete()` with two-step session-ownership check.

### Changed
- `nx store list` now shows the full 16-char document ID (previously truncated to 12),
  enabling copy-paste into `nx store delete --id`.

## [1.5.3] - 2026-03-05

### Docs
- Corrected release notes: 1.5.2 CHANGELOG now includes RDR-020 Voyage AI timeout
  entries that were missing from the initial squash merge.

## [1.5.2] - 2026-03-05

### Added
- **Voyage AI read timeout** (RDR-020) — all `voyageai.Client` construction sites now
  receive `timeout=120.0` (configurable via `voyageai.read_timeout_seconds` in config or
  `NX_VOYAGEAI_READ_TIMEOUT_SECONDS` env var) and `max_retries=3`. Prevents indefinite
  hangs on stalled Voyage AI API calls.
- **Voyage AI transient-error retry** — `_voyage_with_retry` wraps all six Voyage AI
  call sites (CCE embed, fallback embed, standard embed, code embed, rerank) with
  exponential backoff (1 → 2 → 4 s, capped at 10 s) retrying `APIConnectionError` and
  `TryAgain` up to 3 times. Errors handled by the built-in `max_retries` tenacity layer
  (Timeout, RateLimitError, ServiceUnavailableError) are kept disjoint.

### Refactor
- **`nexus.retry` leaf module** — moved `_chroma_with_retry`, `_is_retryable_chroma_error`,
  `_voyage_with_retry`, and `_is_retryable_voyage_error` from `db/t3.py` into a new
  `retry.py` with no `nexus.*` imports. Eliminates a local-import workaround in
  `scoring.py` that was required to avoid a circular-import test-isolation bug.

## [1.5.1] - 2026-03-04

### Fixed
- **ChromaDB transient error retry** — all ChromaDB Cloud network calls in `db/t3.py`,
  `indexer.py`, and `doc_indexer.py` are now wrapped with `_chroma_with_retry` (from
  `retry.py`): exponential
  backoff (2 → 4 → 8 → 16 → 30 s, capped) retrying up to 5 times on HTTP 429/502/503/504
  and transport-level errors (`ConnectError`, `ReadTimeout`). Non-retryable errors raise
  immediately. Fixes multi-thousand-file indexing runs aborted by a single transient 504.

### Docs
- **Transient Error Resilience section** added to `docs/repo-indexing.md` documenting
  retry behaviour and link to RDR-019.
- **Pre-push release checklist** added to `docs/contributing.md` to catch missing
  `uv.lock` commits before tagging.

### Tests
- Unit and integration tests for `_is_retryable_chroma_error` and `_chroma_with_retry`.
- `test_uv_lock_version_matches_pyproject` added to `TestMarketplaceVersion` — CI now
  enforces that `pyproject.toml`, `uv.lock`, and `marketplace.json` all carry the same
  version.

## [1.5.0] - 2026-03-04

### Added
- **Auto-provision T3 databases** — `nx config init` now creates the ChromaDB Cloud tenant
  and database automatically; `nx migrate` has been removed.

### Fixed
- `chroma_tenant` is now optional in credential validation.
- Resolve real tenant UUID before admin calls; use `get_database` for existence check.

### Docs
- White-glove UX polish: help text, wizard flow, troubleshooting, plugin agents/skills,
  and RDR documentation.
- RDR-001, 002, 017, 018 closed as implemented.

### Tests
- Coverage gaps from test-validator audit closed.

## [1.4.0] - 2026-03-03

### Added
- **File lock on `index_repository`** — per-repo `fcntl.flock` prevents concurrent
  indexing of the same repository. Supports `--on-locked skip` (return immediately,
  default) and `--on-locked wait` (block until lock released).
- **`nx hooks install / uninstall / status`** — installs `post-commit`, `post-merge`,
  and `post-rewrite` git hooks that automatically trigger `nx index repo` on each
  commit/merge. Hooks use a sentinel-bounded stanza so they compose safely with
  pre-existing hook scripts.
- **Hooks reminder in `nx index repo`** — on first successful index, if no hooks are
  installed the CLI prints a one-time suggestion to run `nx hooks install`.
- **`nx doctor` hooks check** — reports hook installation status and checks the index
  log for recent errors.

### Removed
- **`nx serve` / Flask / Waitress** — the polling server and all associated code
  (`server.py`, `server_main.py`, `polling.py`, `commands/serve.py`) have been
  deleted. Git hooks replace the auto-indexing use-case. Dependencies `flask>=3.0`
  and `waitress>=3.0` removed from `pyproject.toml`.

### Docs
- `cli-reference.md`: `nx serve` section replaced with `nx hooks` section.
- `repo-indexing.md`: HEAD polling explanation replaced with git hooks explanation.
- `architecture.md`: Server module row replaced with Hooks module row.
- `configuration.md`: `server.port` / `server.headPollInterval` rows removed.
- `contributing.md`: `nx hooks install` added to development setup steps.

## [1.3.0] - 2026-03-03

### Added
- **`--force` flag** on all four `nx index` subcommands (`repo`, `pdf`, `md`, `rdr`) —
  bypasses staleness check and re-chunks/re-embeds in-place. Mutually exclusive with
  `--frecency-only` (repo) and `--dry-run` (pdf).
- **`--monitor` flag** on all four `nx index` subcommands — prints per-file progress
  lines with file name, chunk count, and elapsed time. For `pdf` and `md`, prints
  page range, title, author, and section count after indexing.
- **Auto-enable monitor in non-TTY contexts** — per-file output is now emitted
  automatically when stdout is not a TTY (piped, backgrounded, CI), without needing
  `--monitor`. The flag remains available to force output in interactive sessions.
- **tqdm progress bar** on `repo` and `rdr` subcommands — shows a file-count bar in
  interactive TTY sessions; auto-suppressed when piped or backgrounded.
- **`on_start` / `on_file` progress callbacks** on the indexer layer — `index_repository`
  and `batch_index_markdowns` accept optional callbacks for real-time progress reporting.
- **`return_metadata`** parameter on `index_pdf` and `index_markdown` — returns a dict
  with chunk count, page range, title, author, and section count instead of a plain int.
- **Proactive 12 KB chunk byte cap** (`SAFE_CHUNK_BYTES = 12_288`) — single constant in
  `chroma_quotas.py` enforced across all three chunkers:
  - `chunker.py` escape hatch fixed: single oversized lines are now truncated at the
    UTF-8 boundary instead of emitted as-is.
  - `md_chunker.py` byte cap post-pass added after semantic/naive splitting.
  - `pdf_chunker.py` byte cap post-pass added after char splitting.
  - `t3.py _write_batch` last-resort drop-and-warn for any document exceeding
    `MAX_DOCUMENT_BYTES` (16 384) before upsert.

### Fixed
- **AST chunk line ranges** (RDR-016) — line numbers now derived from
  `node.start_char_idx` / `node.end_char_idx` instead of a hardcoded formula that
  produced systematically wrong ranges.
- **`_run_index` missing registry entry** — returns `{}` instead of raising when the
  path is not registered, preventing unhandled exceptions on first-run edge cases.

### Changed
- **Indexer helpers** return `int` chunk count instead of `bool` — callers get
  actionable count rather than a success/failure flag.

### Docs
- `cli-reference.md` updated with full `nx index` flag coverage: `--force`, `--monitor`
  (with auto-enable note), `--collection`, `--dry-run`, and `--frecency-only` mutual
  exclusion.

## [1.2.0] - 2026-03-03

### Added
- **`ContentClass.SKIP`** — fourth classification category silently ignores known-noise
  files (config, markup, shader, lock) instead of emitting them into `docs__` collections.
  18 extensions skipped: `.xml`, `.json`, `.yml`, `.yaml`, `.toml`, `.properties`,
  `.ini`, `.cfg`, `.conf`, `.gradle`, `.html`, `.htm`, `.css`, `.svg`, `.cmd`, `.bat`,
  `.ps1`, `.lock`.
- **Expanded code extensions** — 9 new extensions classified as CODE: `.proto`, `.cl`,
  `.comp`, `.frag`, `.vert`, `.metal`, `.glsl`, `.wgsl`, `.hlsl` (Protobuf and GPU
  shaders now indexed into `code__` with `voyage-code-3`).
- **Shebang detection** — extensionless files are classified as CODE when their first two
  bytes are `#!`, SKIP otherwise (catches `Makefile`, `LICENSE`, etc. correctly).
- **Context prefix injection (embed-only)** — each code chunk's embedding text is
  prefixed with `// File: X  Class: Y  Method: Z  Lines: N–M`. The raw chunk text is
  stored in ChromaDB unchanged; only the Voyage AI embedding call sees the prefix.
  Improves recall for algorithm-level queries in domain-specific codebases.
- **14-language class/method extraction** via tree-sitter `DEFINITION_TYPES` mapping
  (Python, Java, Go, TypeScript, Rust, C, C++, C#, Ruby, PHP, Swift, Kotlin, Scala).
  Used to populate the `class_name` and `method_name` fields in the context prefix.
- **AST language expansion** — `AST_EXTENSIONS` expanded from 16 to 28 mappings across
  19 parsers: Kotlin, Scala, Swift, PHP, Lua, Objective-C now receive AST-aware chunking.
- **`preserve_code_blocks`** — `SemanticMarkdownChunker` now defaults to
  `preserve_code_blocks=True`, preventing fenced code blocks from being split mid-content.
- **`_STRUCTURAL_TOKEN_TYPES` blocklist** — `paragraph_open`, `list_item_open`,
  `tr_open`, and similar structural markdown-it-py tokens are filtered so content
  appears exactly once per chunk (eliminates duplication from open/close token pairs).

### Changed
- **Chunk metadata** now includes `class_name`, `method_name`, and `embedding_model`
  fields on all code chunks.

### Removed
- **`--chunk-size` and `--no-chunk-warning`** flags removed from `nx index repo` —
  chunk size is not user-configurable; these flags were dead after the AST-first pipeline.

## [1.1.1] - 2026-03-02

### Fixed
- **`nx doctor` server check** — optional Nexus server now shows `✓` with status in
  detail string instead of `✗` with a Fix: hint, preventing false failures in
  preflight scripts that check exit code.

### Changed
- **Release process docs** — added explicit `uv sync` step and `uv.lock` to the
  `git add` list so lock file is never missed in a release commit.

### Docs
- RDR skill docs: `rdr-close` pre-check aligned with actual command behaviour
  (`"accepted"` not `"final"`); agent and skill counts corrected after PM removal.

## [1.1.0] - 2026-03-02

### Removed
- **`nx pm` command layer** — `nx pm new/status/close/list/archive/restore` commands
  removed. T2 memory (`nx memory`) serves this purpose directly with less overhead.
- **Mixedbread integration** — `--mxbai` search flag and `fetch_mxbai_results()` removed.
  Voyage AI via ChromaDB Cloud covers all semantic search needs.

### Added
- **`bd` and `uv` checks in `nx doctor`** — both reported as optional (informational only,
  no exit 1); `bd` includes install URL when absent.

### Fixed
- **`chroma` CLI no longer required on PATH** — `start_t1_server()` now locates the
  `chroma` entry-point relative to `sys.executable`, so it is always found when
  `conexus` is installed via `uv tool install` or `uv sync`. No separate install step.

## [1.0.0] - 2026-03-01

First stable release. Promoted from rc10 after live validation. No functional changes
from rc10 — this entry marks the API, CLI, and plugin contract as stable.

### Changed
- `Development Status` classifier promoted from `4 - Beta` to `5 - Production/Stable`.

## [1.0.0rc10] - 2026-03-01

### Changed
- Version bump to rc10 for release candidate validation prior to 1.0.0 final.
- Polish pass: CHANGELOG entries for rc7/rc8/rc9, hook script package name fix
  (conexus not nexus), skill count corrected to 28, serena-code-nav added to
  plugin README, free tier callout for ChromaDB and Voyage AI.

## [1.0.0rc9] - 2026-03-01

### Added
- **Storage tier awareness for agents**: SubagentStart hook injects live T1 scratch entries
  into every spawned agent's context — agents see what siblings and parent agents already
  discovered this session without duplicating work.
- **Storage Tier Protocol** in `using-nx-skills` SKILL.md: T3→T2→T1 read-widest-first
  table and T1→persist→knowledge-tidy write path, giving all agents a clear data discipline.

### Fixed
- **T2 FTS5 search crash on hyphenated queries**: `nx memory search "foo-bar"` raised
  `sqlite3.OperationalError: no such column: bar` — FTS5 was interpreting hyphens as column
  filter separators. Added `_sanitize_fts5()` helper that quotes special-character tokens
  before `MATCH`. Trailing `*` prefix wildcard preserved. Applies to `search()`,
  `search_glob()`, and `search_by_tag()`.

## [1.0.0rc8] - 2026-03-01

### Added
- **T1 ChromaDB HTTP server** (RDR-010): replaced `EphemeralClient` with a per-session
  `chroma run` subprocess. All agents spawned from the same Claude Code window share one
  T1 scratch namespace via PPID chain propagation — cross-process `nx scratch` reads and
  writes work correctly across separate shell invocations.
- **`serena-code-nav` skill**: navigate code by symbol — find definitions, all callers,
  type hierarchies, and safe renames without reading whole files.
- **`nx hook session-start` / `session-end`** (RDR-008): nx workflow integration hooks
  for session lifecycle management; T1 server is started on session-start and stopped on
  session-end.
- **`using-nx-skills` skill polish**: full 29-skill directory table with 5 categories,
  Announce step in process flow, 12 red flags (up from 7), `brainstorming-gate` replaces
  `verification-before-completion` in Skill Priority. Registry trigger conditions sharpened
  for knowledge-tidier, orchestrator, and substantive-critic. SessionStart hook matcher
  tightened to `startup|resume|clear|compact`.

### Removed
- **`--agentic` and `--answer` flags** removed from `nx search` (RDR-009): both modes
  required Anthropic API key and added latency for marginal benefit. Answer synthesis and
  agentic refinement are now agent responsibilities via the plugin skill suite.

### Fixed
- **T1 server startup**: removed `--log-level ERROR` from `chroma run` invocation — flag
  was dropped in chroma 1.x and silently caused every T1 start to exit code 2, falling
  back to isolated per-process EphemeralClient.
- **Session file keyed to grandparent PID**: `hooks.py` now calls `_ppid_of(os.getppid())`
  to reach the stable Claude Code PID rather than the transient shell subprocess that dies
  immediately after writing the session file.
- **T1 SESSIONS_DIR test isolation**: added `autouse` pytest fixture redirecting
  `SESSIONS_DIR` to `tmp_path`, preventing tests from discovering a live server's session
  records.

## [1.0.0rc7] - 2026-02-28

### Added
- **File-size scoring penalty for code search** (RDR-006): chunks from large files are
  down-ranked proportionally — `score *= min(1.0, 30 / chunk_count)`. Applied unconditionally
  to all `code__` results regardless of `--hybrid`. Files ≤ 30 chunks are unaffected.
- `nx search --max-file-chunks N`: pre-filters code results to files with at most N chunks
  via a ChromaDB `chunk_count $lte` where filter. Combines with `--where` using `$and`.
- **T2 multi-namespace prefix scan** (RDR-007): SubagentStart hook surfaces all
  `{repo}*` T2 namespaces (not just the bare project namespace) with a cap algorithm:
  5 entries with snippet + 3 title-only + remainder as count per namespace; 15-entry
  cross-namespace hard cap.
- `nx index repo --chunk-size N`: configurable lines-per-chunk for code files
  (default 150, minimum 1).
- `nx index repo --no-chunk-warning`: suppress the large-file pre-scan warning.
- **Large-file pre-scan warning**: detects code files exceeding 30× chunk size before
  indexing and suggests `--chunk-size 80`; adaptive recommendation when chunk size is
  already set.

## [1.0.0rc6] - 2026-02-28

### Fixed
- **CCE query model mismatch** (P0, affected rc1–rc5): `docs__`, `knowledge__`, and `rdr__`
  collections were indexed with `voyage-context-3` (CCE) but queried with `voyage-4`.
  These two models produce vectors in incompatible geometric spaces (cosine similarity ≈ 0.05
  — effectively random noise). All three collection types were returning semantically
  meaningless results since rc1. `code__` collections were unaffected.
  Fix: `corpus.py` returns `voyage-context-3` for CCE collections; `T3Database.search()`
  bypasses the ChromaDB `VoyageAIEmbeddingFunction` for CCE collections and calls
  `contextualized_embed([[query]], input_type="query")` directly. `T3Database.put()`
  likewise uses `contextualized_embed` with `input_type="document"` so single entries
  stored via `nx store put` land in the same CCE vector space as indexed chunks.
  **All CCE-indexed collections (`docs__*`, `knowledge__*`, `rdr__*`) must be re-indexed
  after upgrading from rc1–rc5.**

## [1.0.0rc5] - 2026-02-28

### Added
- **Four-store T3 architecture** (RDR-004): T3 now routes collections to four dedicated
  ChromaDB Cloud databases (`{base}_code`, `{base}_docs`, `{base}_rdr`, `{base}_knowledge`),
  one per content type. All routing is internal to `T3Database`; no CLI commands change.
- `nx migrate t3`: new command that copies collections from an old single-database T3 store
  to the new four-store layout. Idempotent; copies embeddings verbatim (no re-embedding).
- `nx doctor` now checks connectivity to all four T3 databases when credentials are present.

### Fixed
- Eliminated spurious per-corpus warning noise during `nx search`: warnings now fire once per unmatched corpus term across all collections, not once per internal resolver call

## [1.0.0rc4] - 2026-02-27

### Added
- `/rdr-accept` slash command with gate-result verification and T2 status synchronization
- `rdr-accept` skill: accepts gated RDRs, updates T2 and file frontmatter atomically (RDR-002)
- `-v`/`--verbose` flag for `nx` CLI: enables debug logging for network calls and index operations
- RDR indexing status shown in `nx index repo` output (count of RDRs indexed)
- MCP sequential-thinking server (`.mcp.json`): replaces `nx thought` for compaction-resilient reasoning chains

### Changed
- `nx thought` session isolation now uses Claude session ID instead of `getsid(0)` (RDR-002)
- SessionStart hook: T2 session records synchronized on startup (RDR-002)
- Sequential-thinking skill updated to use `mcp__sequential-thinking__sequentialthinking` instead of `nx thought add`

### Fixed
- `rdr-gate`: strip fenced code blocks before extracting section headings (false negatives on structured RDRs)
- `rdr-list` and all RDR commands: handle `RDR-NNN` naming convention; single-pass Python heredoc
- `hooks.json` format: wrap in `{hooks:{}}` with matcher/hooks nesting (plugin hook discovery was silently failing)
- Empty strings filtered from embedding batches before Voyage AI calls (prevented API errors on sparse content)
- Suppressed `llama_index` pydantic `validate_default` warning and `httpx`/`httpcore` wire-trace noise in `-v` mode
- structlog level in tests follows `pytest --log-level` (default: WARNING); debug logs surface on failure
- `rdr-accept` skill: description, relay template, PRODUCE section, and `nx scratch` reference now conform to plugin structure tests

## [1.0.0rc3] - 2026-02-26

## [1.0.0-rc2] - 2026-02-26

### Added
- Six RDR slash commands with live context injection (`/rdr-create`, `/rdr-list`, `/rdr-show`, `/rdr-research`, `/rdr-gate`, `/rdr-close`)
  - Each command pre-fetches project state (RDR dir, existing IDs, T2 metadata, active beads, git branch) before invoking the corresponding skill
  - Mirrors the context-injection pattern used by agent commands (`/review-code`, `/create-plan`, etc.)
- Plugin test suite: 752 unit + structural tests covering install simulation, `$CLAUDE_PLUGIN_ROOT` reference integrity, markdown link resolution, hook script presence, and marketplace version consistency
- E2E debug-load scenario (scenario 00): validates plugin load diagnostics, hook script execution, and component discovery via Claude `-p` mode without a live interactive session
- E2E test sandbox now includes locally-cached superpowers plugin alongside nx, enabling cross-plugin skill validation
- E2E isolation guard: verifies `nx@nexus-plugins` loads from dev repo, not the installed v1 cache

### Changed
- RDR skills now read the RDR directory from `.nexus.yml` `indexing.rdr_paths[0]` (default: `docs/rdr`) instead of hardcoding the path — consistent with the nx repo indexer config
- `registry.yaml` RDR skill entries updated with `command_file` references linking skills to their context-injecting command counterparts

### Fixed
- Marketplace version corrected from `1.0.0-rc1` to `1.0.0-rc2` (plugin structure test caught mismatch)
- E2E test harness: Python 3.14 incompatibility with chromadb/pydantic resolved by pinning install to Python 3.12

## [1.0.0-rc1] - 2026-02-25

### Added
- `nx thought` command group: session-scoped sequential thinking chains backed by T2 SQLite
  - `nx thought add CONTENT` — append thought, return full accumulated chain + MCP-equivalent metadata
  - `nx thought show` / `close` / `list` — chain lifecycle management
  - Chains scoped per session via `os.getsid(0)`, expire after 24 hours
  - Semantic equivalence with sequential-thinking MCP server: `thoughtHistoryLength`, `branches[]`, `nextThoughtNeeded`, `totalThoughts` auto-adjustment
  - Compaction-resilient: state stored externally in T2, not in Claude's context window
- `nx:sequential-thinking` skill: replaces external MCP dependency; uses `nx thought add` for compaction-resilient chains
- `/nx-preflight` slash command: checks all plugin dependencies (nx CLI, nx doctor, bd, superpowers) with PASS/FAIL per check
- Plugin prerequisites section in `nx/README.md` with dependency table and install commands
- Smart repository indexing: code routed to `code__` collections, prose to `docs__`, PDFs to `docs__`
- 12-language AST chunking via tree-sitter (Python, JS, TS, Java, Go, Rust, C, C++, Ruby, C#, Bash, TSX)
- Semantic markdown chunking via markdown-it-py with section-boundary awareness
- RDR (Research-Design-Review) document indexing into dedicated `rdr__` collections
- `nx index rdr` command for manual RDR indexing
- Frecency scoring: git commit history decay weighting for hybrid search ranking
- `--frecency-only` reindex flag: update scores without re-embedding
- Hybrid search: semantic + ripgrep keyword scoring with `--hybrid` flag
- Agentic search mode: multi-step Haiku query refinement with `--agentic` flag
- Answer synthesis mode: cited answers via Haiku with `--answer`/`-a` flag
- Reranking via Voyage AI `rerank-2.5` with automatic fallback
- Path-scoped search with `[path]` positional argument
- `--where` filter support for metadata queries
- `-A`/`-B`/`-C` context lines flags for `nx search`
- `--vimgrep` and `--files` output formats
- `nx pm` full lifecycle: init, status, resume, search, archive, restore
- `nx store list` subcommand
- `nx collection verify --deep` deep verification
- Background server HEAD polling for auto-reindex on commit
- Claude Code plugin (`nx/`): 15 agents, 26 skills, session hooks, slash commands
- RDR workflow skills: rdr-create, rdr-list, rdr-show, rdr-research, rdr-gate, rdr-close
- E2E test suite requiring no API keys (1258 tests)
- Integration test suite with real API keys (`-m integration`)

### Changed
- `sequential-thinking` skill now uses `nx thought add` as its tool-call mechanism (compaction-resilient by design)
- All agents previously using `mcp__sequential-thinking__sequentialthinking` updated to use `nx:sequential-thinking` skill
- All 11 agents with sequential-thinking now have domain-specific thought patterns, When to Use, and control reminders
- `nx doctor` improved: Python version check, inline credential fix hints, non-fatal server check
- CLI help text audited and aligned with `docs/cli-reference.md`; 15+ mismatches corrected
- Renamed `nx index code` → `nx index repo`
- Collection names use `__` separator (never `:`)
- Session ID scoped by `os.getsid(0)` (terminal group leader PID) for worktree isolation
- Stable collection names across git worktrees via `git rev-parse --git-common-dir`
- Embedding models: `voyage-code-3` for code indexing, `voyage-context-3` (CCE) for docs/knowledge, `voyage-4` for all queries
- T1 session architecture: shared EphemeralClient store + `getsid(0)` anchor
- Plugin discovery: `.claude-plugin/marketplace.json` at repo root (replaces `nx/.claude-plugin/plugin.json`)
- `nx pm` namespace collapsed; session hooks simplified
- Plugin slash commands: `/plan` → `/create-plan`, `/code-review` → `/review-code`

### Fixed
- CCE fallback metadata bug
- Search round-robin interleaving
- Collection name collision on overflow
- Registry resilience under concurrent access
- Credential TOCTOU race condition
- `nx serve stop` dead code removed
- Indexer ignorePatterns filtering
- Upsert idempotency in doc pipeline
- T1/T2 thread-safe reads

### Removed
- `nx install` / `nx uninstall` legacy commands
- `nx pm migrate` command
- Homebrew tap formula (superseded by `uv tool install`)
- `nx/.claude-plugin/` legacy plugin discovery directory

## [0.4.0] - 2026-02-24

### Added
- nx plugin v0.4.0: brainstorming-gate, verification-before-completion, receiving-code-review, using-nx-skills, dispatching-parallel-agents, writing-nx-skills skills
- Graphviz flowcharts in decision-heavy skills
- REQUIRED SUB-SKILL cross-reference markers
- Companion reference.md for nexus skill
- SessionStart hook for using-nx-skills injection
- PostToolUse hook with bd create matcher

### Changed
- All skill descriptions rewritten to CSO "Use when [condition]" pattern
- Relay templates deduplicated: hybrid cross-reference to RELAY_TEMPLATE.md
- Agent-delegating commands simplified with pre-filled relay parts
- Nexus skill split into quick-ref SKILL.md + detailed reference.md

### Fixed
- PostToolUse hook performance: now fires only on bd create, not every tool use
- Removed non-standard frontmatter fields from all skills

## [0.3.2] - 2026-02-22

### Added
- E2E tests for indexer pipeline and HEAD-polling logic

### Fixed
- `nx serve stop` dead code path

## [0.3.1] - 2026-02-22

### Added
- `nx store list` subcommand
- Integration test improvements: knowledge corpus scoping

### Changed
- README full readability pass: clearer setup path, optional vs required deps

## [0.3.0] - 2026-02-22

### Added
- Voyage AI CCE (`voyage-context-3`) for docs and knowledge collections at index time
- Ripgrep hybrid search: `rg` cache wired to `--hybrid` retrieval
- `--content` flag and `[path]` path-scoping for `nx search`
- `--where` metadata filter, `-A`/`-B`/`-C` context flags, `--reverse`, `-m` alias
- P0 regression test suite
- T3 factory extraction (`make_t3()`) with `_client`/`_ef_override` injection for tests
- `nx pm promote` and `NX_ANSWER` env override
- `nx collection verify --deep` and info enhancements
- Frecency-only reindex flag

### Changed
- Removed pdfplumber in favour of pymupdf4llm
- `search_engine.py` refactored into focused modules (`scoring.py`, `search_engine.py`, `answer.py`, `types.py`, `errors.py`)
- structlog migration

### Fixed
- 10 P0 bugs, 10 P1 bugs, 10 P2 bugs, 5 P3 observations
- CCE fallback metadata bug; `batch_size` dead parameter removed
- `serve` status/stop lifecycle, collection collision, registry resilience
- Credential TOCTOU, env override error handling
- T1 session architecture (getsid anchor, thread-safe reads)

## [0.2.0] - 2026-02-21

### Added
- `nx config` command with credential management and `config init` wizard
- Integration test suite (requires real API keys)
- E2E test suite (no API keys, 505 tests at release)
- T1 session architecture overhaul: shared EphemeralClient + getsid(0) anchor
- Scratch tier fix for CLI use outside Claude Code

### Changed
- Full README rewrite: installation, quickstart, command reference, architecture

### Fixed
- Scratch tier session isolation
- 5-stream global code review: 15 critical/significant fixes (mxbai chunk ID, security, resilience)

## [0.1.0] - 2026-02-21

### Added
- Project scaffold: `src/nexus/` package, `nx` CLI entry point via Click
- T1: `chromadb.EphemeralClient` + ONNX MiniLM, session-scoped scratch (`nx scratch`)
- T2: SQLite + FTS5 WAL, per-project persistent memory (`nx memory`)
- T3: `chromadb.CloudClient` + Voyage AI, permanent knowledge store (`nx store`, `nx search`)
- `nx index repo` (originally `nx index code`): git-aware code indexing with tree-sitter AST
- `nx serve`: Flask/Waitress background daemon with HEAD polling for auto-reindex
- `nx pm`: project management lifecycle (init, status, resume, search, archive, restore)
- `nx doctor`: prerequisite health check
- Claude Code plugin (`nx/`): initial agents, skills, hooks, registry
- Config system: 4-level precedence (defaults → global → per-repo → env vars)
- Hybrid search: semantic + ripgrep keyword scoring
- Answer synthesis: Haiku with cited `<cite i="N">` references
- Agentic search: multi-step Haiku query refinement
- Phase 1–8 implementations covering all CLI surface

[Unreleased]: https://github.com/Hellblazer/nexus/compare/v1.12.0...HEAD
[1.12.0]: https://github.com/Hellblazer/nexus/compare/v1.11.1...v1.12.0
[1.11.1]: https://github.com/Hellblazer/nexus/compare/v1.11.0...v1.11.1
[1.11.0]: https://github.com/Hellblazer/nexus/compare/v1.10.3...v1.11.0
[1.10.3]: https://github.com/Hellblazer/nexus/compare/v1.10.2...v1.10.3
[1.10.2]: https://github.com/Hellblazer/nexus/compare/v1.10.1...v1.10.2
[1.10.1]: https://github.com/Hellblazer/nexus/compare/v1.10.0...v1.10.1
[1.10.0]: https://github.com/Hellblazer/nexus/compare/v1.9.1...v1.10.0
[1.9.1]: https://github.com/Hellblazer/nexus/compare/v1.9.0...v1.9.1
[1.9.0]: https://github.com/Hellblazer/nexus/compare/v1.8.0...v1.9.0
[1.0.0]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc10...v1.0.0
[1.0.0rc10]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc9...v1.0.0rc10
[1.0.0rc10]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc9...v1.0.0rc10
[1.0.0rc9]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc8...v1.0.0rc9
[1.0.0rc8]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc7...v1.0.0rc8
[1.0.0rc7]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc6...v1.0.0rc7
[1.0.0rc6]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc5...v1.0.0rc6
[1.0.0rc5]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc4...v1.0.0rc5
[1.0.0rc4]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc3...v1.0.0rc4
[1.0.0rc3]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc2...v1.0.0rc3
[1.0.0rc2]: https://github.com/Hellblazer/nexus/compare/v1.0.0-rc1...v1.0.0rc2
[1.0.0-rc1]: https://github.com/Hellblazer/nexus/compare/v0.4.0...v1.0.0-rc1
[0.4.0]: https://github.com/Hellblazer/nexus/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/Hellblazer/nexus/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/Hellblazer/nexus/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Hellblazer/nexus/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Hellblazer/nexus/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Hellblazer/nexus/releases/tag/v0.1.0
