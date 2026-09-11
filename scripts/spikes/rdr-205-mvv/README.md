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

# RDR-205 Phase 4 Step 3 — MVV run 1 (`run1.py`)

Bead: `nexus-em75s.21`, the run that closes Phase 4. Ten sub-agents of
two types, twenty `ledger/<session_id>` tuples, a genuine parked `rd`
that wakes when a report lands, and the space census agreeing with the
`expectations.sh` TSV census row for row.

**Why this drives the checkout's hook scripts directly rather than a
real `Agent` dispatch:** a real Claude Code session on this box runs
the INSTALLED conexus plugin, which predates this RDR's tuple-space
projection hooks (`subagent-start-tuple-async.sh`, `subagent-stop-
tuple-async.sh`, the `agent-dispatch-expect.sh` EXPECT wiring). A real
dispatch here would exercise the OLD hooks and prove nothing about this
checkout. `run1.py` instead invokes `conexus/hooks/scripts/{agent-
dispatch-expect,subagent-start,subagent-start-stamp,subagent-start-
tuple-async,subagent-stop,subagent-stop-tuple-async}.sh` as real
subprocesses, with stdin JSON payloads shaped exactly like the ones
`tests/hooks/test_subagent_start_hook.py`, `test_agent_dispatch_expect.
py` and `test_subagent_stop_hook.py` already pin as measured Claude
Code wire shapes. The in-session repeat against a real dispatch, once
the plugin ships these hooks, is a residual for the RDR close — not
for this bead.

Unlike MVV run 2, `ledger/<session_id>` is a SHIPPED v1 template
(`service/src/main/resources/tuples/templates/ledger.yaml`), so no
`NX_TUPLE_TEMPLATE_DIR` override is needed.

**Tenant identity is the one thing run 2 didn't have to think about.**
The async projector (`tuple_ledger_project.py`) is hard-pinned to
tenant `"default"` (no hook mints anything; it only presents whatever
cross-process data-token lease it finds). `run1.py` therefore issues a
mint-scoped credential for tenant `"default"` via the real consumer
surface (`HttpTokenStore.issue_token(..., scope="mint")`, the same call
`nx service token issue --scope mint` makes against the boot admin
bearer), then lets the real `nexus.db.data_token.DataTokenManager` mint
the short-TTL data token AND write the cross-process lease file —
exactly what `tuple_ledger_project.py` reads, produced by the real
client code rather than hand-rolled JSON. The harness's own
verification store uses that SAME data token (measured directly
against this engine: `AuthFilter`'s Decision 1, Phase E/nexus-
gmiaf.32.5, means a token's server-side-bound tenant is authoritative
and the `X-Nexus-Tenant` header is ignored outright — the older
"wildcard bootstrap token" docstring describes a retired posture, not
this engine's current behavior, and using the root/admin bearer with a
`tenant=` header for verification reads silently sees the WRONG
tenant's rows, zero every time, no error).

## What it verifies

1. **Twenty tuples over ten agent ids** — `subspace_stats("ledger/
   <sid>").total == 20`, ten agents each carrying both a `start` and a
   `report` kind.
2. **The parked `rd`.** One agent's `SubagentStop` pair is deliberately
   delayed from a background thread; the main thread parks on that
   agent's report tuple (`HttpTupleStore.rd(..., timeout_s=25)`,
   *before* the delayed write lands) and LOOPS past the engine's 25s
   per-call cap (CA 3) rather than sending one longer request — the
   same "a wait of minutes is a loop of parked calls" contract the
   orchestration skill's parked-`rd` consumer (`nexus-em75s.20`) uses.
   Verified at two delays: 3s (found on the first call, woken within
   ~0.2s of the write) and 28s (found on the SECOND call, after the
   first 25s park legitimately timed out with nothing to see) — the
   second run is the genuine evidence that this loops rather than
   blocking on one oversized request.
3. **Census agreement, row for row.** `expectations.sh`'s
   `expectations_census` reads the session's TSV ledger AND (RDR-205
   Phase 4.1, `nexus-em75s.19`) the space via `nx tuple list --prefix
   ledger/ --json`; this run compares the ten TSV `AGENT` rows
   (terminal `REPORTED`) against the space's per-agent kind sets
   (`{start, report}`) one by one. `nx tuple` ships only in this
   checkout's own dev build (not yet in the globally installed
   release), so the script drops a tiny `nx` wrapper
   (`exec uv run --project <worktree> nx "$@"`) onto `PATH` for the
   census subprocess only.

The ledger template's retention is 90 days (`retention_seconds:
7776000` in `ledger.yaml`) — the window the RDR-205 Phase 4.1 census
comparison is bounded to; irrelevant to this run (session is minutes
old) but recorded in the JSON summary for completeness.

## Running it

```bash
scripts/build-gate-jar.sh   # if service/ changed since the last build
uv run python scripts/spikes/rdr-205-mvv/run1.py
# exercise the loop explicitly (>25s single-call cap):
uv run python scripts/spikes/rdr-205-mvv/run1.py --late-delay-s 28 --overall-park-budget-s 40
```

Exit code is 0 iff every verification held. The JSON summary at
`--out` (default `scripts/spikes/rdr-205-mvv/last-run1-summary.json`,
not committed) carries the ten agent ids/types, the parked-`rd`
timing, the space and TSV census raw output, and the row-for-row
agreement table.
