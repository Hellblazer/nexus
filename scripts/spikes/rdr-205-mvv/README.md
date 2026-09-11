# RDR-205 Phase 3 Step 3 — MVV run 2, engine-direct legs

Bead: `nexus-em75s.16`. The RDR-110 ten-worker work-stealing harness,
written as code for the first time (RDR-110 itself is `abandoned`,
carries a "do not implement against this design" tombstone, and
targeted the retired SQLite+Chroma substrate — this harness runs
against RDR-205's Postgres tuple space instead).

## What this is

A TEST-ONLY spike, not production code:

- `templates/mvv-run2.yaml` — a work-stealing queue template loaded
  only via `NX_TUPLE_TEMPLATE_DIR` (never shipped in engine resources;
  `service/src/main/resources/tuples/templates/` ships exactly the two
  v1 templates, `ledger.yaml` and `mailbox.yaml`, and no third).
- `harness.py` — boots a developer engine itself (the same hermetic
  bundled-Postgres-17 + `build-gate-jar.sh`-stamped-JAR machinery the
  unit suite's `tests/_engine_substrate.py` uses), runs three legs
  against it over the real `HttpTupleStore` client, and writes a JSON
  summary.

No engine source under `service/` is touched by this harness.

## The three legs

1. **Audit-shape leg** (`spike/mvv-run2/queue`): `--tuples` work items,
   `--workers` concurrent claimants draining them via blocking
   `in_()`/`ack()`. Verifies the May MVV audit shape verbatim (RDR-205
   Test Plan: "ten workers drain N tuples -> N consumed, zero claimed,
   zero available, exactly N claim and N ack log rows, zero expire
   rows, no tuple with two claim ids"). Records: claim transaction
   duration (client-observed `in_()` round-trip), oldest-unclaimed age
   (sampled through the drain), and empty claims after wake (a genuine
   lost race — see the docstring in `harness.py` for why this excludes
   the uninteresting "no more work left at all" case by construction).

2. **Lease-expiry probe** (`spike/mvv-run2/lease-probe`): one claim
   with a 1s lease, deliberately never acked, reclaimed by a second
   claimant after the lease lapses. The audit-shape leg's own expire
   count is required to be zero by the audit shape itself, so this
   probe is what gives "lease expiries" a real, non-trivial data point.

3. **Bloat leg** (`spike/mvv-run2/bloat`): the fifth metric — dead-
   tuple ratio against `nexus.tuples`' per-table
   `autovacuum_vacuum_scale_factor = 0.01` (`tuples-001-baseline.xml`),
   sampled throughout, up to `--bloat-max-cycles` (default 1,000,000)
   claim-release cycles bounded by `--bloat-max-seconds` (default
   600s — raise this for a real long run). The client API has no
   direct unconsume/reopen primitive, so this leg uses `claim, nack`
   on a small FIXED row pool (`--bloat-row-pool`, default 1,000) as the
   reopen analog to research-3's raw-SQL "claim, ack, reopen"
   methodology (T2 `nexus/rdr-110-revival-brief-2026-09-09`; RDR-205
   §Key Discoveries) — same churn shape (repeated non-HOT updates
   concentrated on a fixed row set), reached entirely through the
   client surface. This is a wall-clock-bounded leg: the actual cycle
   count reached is what gets recorded, never a target.

Dead-tuple ratio, last-autovacuum, and the claim-log counts (`claim`,
`ack`, `expire` transitions; "no tuple with two claim ids") are read
directly against the substrate's own bundled Postgres via `psql`
(nexus-20890's precedent — "a test can query the substrate's schema
DIRECTLY") since no client operation surfaces the claim log or
`pg_stat_user_tables`.

## Running it

```bash
# From the repo root (or a worktree of it). Rebuild the gate jar first
# if service/ changed since your last build:
scripts/build-gate-jar.sh

# Quick dev loop, skipping the long bloat leg:
uv run python scripts/spikes/rdr-205-mvv/harness.py --skip-bloat

# The real run, bloat leg bounded to 20 minutes of wall clock, detached
# so progress can be polled from a log file:
nohup uv run python scripts/spikes/rdr-205-mvv/harness.py \
    --bloat-max-seconds 1200 --out /tmp/mvv-run2-summary.json \
    > /tmp/mvv-run2-harness.log 2>&1 &
tail -f /tmp/mvv-run2-harness.log
```

The script boots its own engine substrate on first use (the shared
`NX_BUILD_LEASE_WAIT`-gated build lease still applies — if another
process is mid-build, `ensure_engine()` fails loud naming the holder;
rerun once it clears). On a busy box with siblings rebuilding the same
jar continuously, `NX_MVV2_PRIVATE_JAR=<path>` points the boot at a
private copy of an already-stamped jar instead (e.g. a copy taken from
`scripts/build-gate-jar.sh`'s own cache directory, which is written
once and only ever read from on a cache hit — never rewritten in
place), skipping the wait entirely without weakening the freshness
check for anyone else. Exit code is 0 iff the audit-shape leg's
verification held; the JSON summary at `--out` (default
`scripts/spikes/rdr-205-mvv/last-run-summary.json`, not committed) has
every field including the raw bloat-leg sample series, and the bloat
leg's own progress log lands beside it at `<out>.bloat.log`.

## Reproducibility

- Fixed worker counts (`--workers`, `--bloat-workers`), no randomness
  beyond `--run-id` (default a fresh UUID4, printed at the start of
  every run and mixed into every tuple's nonce so distinct runs never
  collide on the same subspace).
- The template is committed here, not generated — the boot-time
  `registry()` digest changes if it does.
- Every recorded number names the engine identity it ran against (the
  `/version` endpoint's `build_ref`, plus the worktree's own git HEAD
  sha for cross-reference when a build-lease cache hit served a jar
  stamped from an earlier commit with byte-identical `service/`
  content).
