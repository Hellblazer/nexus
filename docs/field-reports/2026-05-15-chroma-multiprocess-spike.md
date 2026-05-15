# Chroma multi-process race spike

Date: 2026-05-15
Bead: nexus-23lr (epic nexus-hp9f, RDR-111)
Author: spike investigation
Platform: darwin arm64 (Darwin 25.4.0), chromadb 1.5.9, Python 3.12

## Background

PR #786 operational-readiness critique flagged that the cockpit hook bridge
(`src/nexus/cockpit/hook_bridge.py::_emit_direct_auto`) opens
`chromadb.PersistentClient(path=~/.config/nexus/chroma)` directly from each
hook-event subprocess. With three concurrent Claude Code instances driving
hook events, bursts of 3 or more bridge subprocesses can hit the same persist
directory at the same time. Chromadb does not document any cross-process
locking guarantee. This spike asked: does the bridge access pattern actually
race in practice?

## Setup

Driver script: `tests/spike/chroma_multiprocess_spike.py`.

The driver spawns N independent worker processes (multiprocessing `spawn`
context, not fork, so each child gets a fresh interpreter that re-imports
chromadb just like the real bridge subprocesses do). Each worker:

1. Waits on a file-presence barrier so all workers start within ~10ms.
2. Calls `chromadb.PersistentClient(path=<shared_dir>)`.
3. Calls `get_or_create_collection("spike_coll")`.
4. Issues `coll.upsert(ids=..., documents=..., embeddings=..., metadatas=...)`
   for its own slice of IDs (`wN-rM`).
5. Issues `coll.get(ids=...)`, `coll.query(...)`, and `coll.count()`.

The main process then re-opens the dir with a fresh client and reads the
final `count()`. Verdict is `CLEAN` if final count equals
`workers * records_per_worker` and every worker returned `ok=True`,
`RACE_OR_BLOCK` otherwise.

Each run uses a fresh `tempfile.mkdtemp()` and cleans up afterward; the real
`~/.config/nexus/chroma` is never touched.

## Observations

### Run 1: 5 workers, 50 records each, fresh persist dir

```
expected_total: 250
final_count:    150
ok_workers:     3
failed_workers: 2
verdict:        RACE_OR_BLOCK
```

Two workers crashed during `PersistentClient(path=...)` construction with:

```
chromadb.errors.InternalError: error returned from database:
  (code: 1) table collections already exists
```

The traceback bottoms out in `chromadb/api/rust.py:122` →
`chromadb_rust_bindings.Bindings(...)`. The rust core opens the SQLite system
db at startup and runs schema-creation DDL unconditionally; the
`CREATE TABLE` lacks `IF NOT EXISTS`, so when N workers race to construct
the persist dir's schema, all-but-one lose.

### Run 2: same params, but persist dir pre-initialized by a warmup client

```
expected_total: 250
final_count:    250
ok_workers:     5
failed_workers: 0
verdict:        CLEAN
```

Once the schema exists, 5 concurrent workers slamming `upsert` against the
same collection completed without errors, and the post-mortem count matched
exactly. `coll.count()` from inside each worker showed stale values
(50, 100, 100, 150, 250) consistent with SQLite snapshot isolation: each
worker sees the writes committed before its read but not in-flight writes
from peers. The final post-mortem read sees everything.

### Run 3: 10 workers, 100 records each, pre-initialized dir

```
expected_total: 1000
final_count:    1000
verdict:        CLEAN
```

No corruption, no contention failures. Wallclock per worker ranged
0.34s–0.61s; the slowest worker shows ~2x the fastest, consistent with
write-side serialization through a single SQLite writer but not blocking
hard enough to time out.

### Reproducibility sweep: 8 fresh-dir runs at 8 workers x 30 records

```
run 1: 240 / fail=0 / CLEAN
run 2: 210 / fail=1 / RACE_OR_BLOCK
run 3: 240 / fail=0 / CLEAN
run 4: 210 / fail=1 / RACE_OR_BLOCK
run 5: 240 / fail=0 / CLEAN
run 6: 210 / fail=1 / RACE_OR_BLOCK
run 7: 210 / fail=1 / RACE_OR_BLOCK
run 8: 240 / fail=0 / CLEAN
```

~50% of fresh-dir runs lost exactly one worker. The failure is real and
reproducible but probabilistic (depends on exact interleaving of the
schema-create DDL across rust bindings).

## Failure mode

**Init-time race in `chromadb.PersistentClient.__init__` against an
uninitialized persist directory, surfacing as
`InternalError: table collections already exists`.**

* Not corruption: surviving workers' data is intact, no torn rows, no
  count drift beyond "the crashed worker's writes never landed".
* Not block / hang: failed workers crash immediately, they do not deadlock.
* Not a steady-state race: once the schema is created, 5–10 concurrent
  writer processes against the same dir behave correctly.

## What this means for the bridge

The bridge path
(`src/nexus/cockpit/hook_bridge.py::_emit_direct_auto`, lines 255–275)
re-opens `PersistentClient(path=~/.config/nexus/chroma)` in every hook
subprocess. In a steady-state environment where the persist dir was
created by an earlier client (e.g., the user's first `nx` invocation),
this is safe — the steady-state spike was clean.

The risk window is:

1. **First-use bursts.** A fresh nexus install where 3+ hook events fire
   before any non-bridge process has initialized the chroma dir. Each
   bridge subprocess will race on schema creation; ~1 of N will crash.
2. **Persist-dir migrations.** Anything that wipes / recreates the persist
   dir (RDR migrations that drop and recreate collections at the OS level,
   user-triggered `rm -rf`, disaster recovery) re-opens the same race
   window for the next concurrent burst.
3. **Per-test fixtures.** Test code that points the bridge at a fresh tmp
   dir per test will hit this any time the test fires concurrent hooks.

Steady-state hook traffic (the common case) is fine. The risk is real but
narrow.

## Recommended protection (in order of preference)

1. **Route through the daemon** — preferred. The daemon (RDR-112,
   resolves under nexus-pce1.6) becomes the sole owner of the chroma
   persist dir; bridge subprocesses talk to it over the existing IPC
   surface rather than opening their own `PersistentClient`. This
   eliminates the multi-process problem entirely instead of patching
   around it. Long-term correct.
2. **OS-level file lock around `PersistentClient` construction in the
   bridge.** Wrap the `_chromadb.PersistentClient(path=str(chroma_dir))`
   call in a `fcntl.flock()` on a sibling file
   (`~/.config/nexus/chroma.init.lock`), held just long enough to
   serialize the schema-creation step. After init the lock can be
   released; subsequent writes are safe concurrent. Cheap, surgical,
   ships before the daemon lands.
3. **Pre-warm on `nx` startup.** Make `nx doctor` / first-run idempotently
   open and close a `PersistentClient(path=...)` so the schema is created
   exactly once, in the user-foreground process, before any bridge
   subprocess can race. Reduces frequency but doesn't fully close the
   migration / wipe window.
4. **Skip-if-busy.** Have the bridge detect contention and drop the hook
   write rather than block. Lossy and the bridge writes are observability,
   not control flow, but it's the cheapest mitigation if (2) is rejected.

## Protection plan — follow-up beads

Do not implement protection in this PR. Spawn the following:

* **(P1) Daemon-routed bridge.** Already on the books as part of
  RDR-112 / nexus-pce1.6. Confirm scope explicitly covers the bridge's
  chroma access; if not, file an extension bead.
* **(P2) `flock`-guarded init in `hook_bridge._emit_direct_auto`.** Wrap
  lines 273–275 in a context manager that takes an exclusive lock on
  `<nexus_dir>/.chroma-init.lock` for the duration of the
  `PersistentClient` constructor call. Release after construction. Ships
  before the daemon as a backstop.
* **(P3) Pre-warm on `nx doctor`.** Have `nx doctor` and `nx upgrade`
  open a transient `PersistentClient` against the configured persist dir
  to force schema creation. Low cost, defense in depth.
* **(P4) Regression test.** Promote
  `tests/spike/chroma_multiprocess_spike.py` into a real integration test
  once protection (2) lands, asserting `verdict == "CLEAN"` across N=20
  fresh-dir runs at 8 workers. This locks in the fix.

## Coverage limits

* Tested on darwin arm64 only. Linux ext4/xfs may behave differently; the
  underlying chromadb rust bindings use the same SQLite path so the failure
  mode should reproduce, but worth confirming once the protection lands.
* The harness uses synthetic 3-dim embeddings and skips the embedding
  provider entirely. The bridge's real write path goes through
  `TupleIndex.from_registry` and the Voyage / local embedder. The
  init-race is upstream of all that (it's in `PersistentClient.__init__`),
  so the embedding layer doesn't change the verdict, but the steady-state
  write timings here are not directly comparable to live bridge timings.
* No attempt was made to corrupt the persist dir (e.g., kill -9 mid-write).
  That's a separate concern.

## Verdict

**RACE in `PersistentClient.__init__` against an uninitialized persist
directory; clean in steady state.** Real, reproducible, narrow. Protection
should land as `flock`-guarded init in the bridge (P2 above) as a backstop,
with the daemon (RDR-112 / nexus-pce1.6) as the long-term resolution.
