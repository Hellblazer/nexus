# Post-Mortem: RDR-141 — T2 Version-Skew Double-Writer

**Closed:** 2026-06-01 (implemented) · **Epic:** nexus-4bgkx · **PR:** #1055 · **Successor to:** RDR-128 P3 (the A1 boundary RDR-140 left out of P3 scope)

## What shipped

`t2_index_write` previously caught `T2DaemonNotReachableError` and `T2SchemaVersionMismatchError` in one `except` and degraded both to a direct `T2Database` writer. The two conditions have **opposite** single-writer implications:

- **Unreachable** = no daemon writer exists → direct write is safe (the RDR-128 documented availability fallback). Unchanged.
- **Version-mismatch** = a stale-version daemon is ALIVE, holds the spawn lock, and is serving → a direct writer is a SECOND live writer on `memory.db` (the version-skew double-writer).

The fix splits the arms. The mismatch arm now re-asserts the supervisor (`_reassert_t2_daemon` → the extracted non-Click `_t2_ensure_running_inner`, which reaps the stale daemon and spawns a current one), re-probes once through the fresh daemon, and falls back to a bounded direct write only when a current daemon cannot be reached. Each degraded sub-path emits a distinct, operator-visible WARNING; the generic "start the daemon" banner is suppressed for version-skew paths (its advice is wrong when the stale daemon is still up).

## What went well

- **The central insight surfaced early**: the two exception arms encode opposite liveness states. Framing the fix around that (not "make the fallback smarter") kept the change surgical.
- **P0-first sequencing held.** Extracting a non-Click inner function with a rich `T2EnsureOutcome` (not a bare bool) was the load-bearing prerequisite — the rich outcome is what lets the caller distinguish D_old-alive (cycle-deferred residual) from no-live-incumbent (safe down-arm).
- **Stacked reviews earned their keep.** code-review-expert + substantive-critic returned 0 critical on both P0 and P2, but caught: (P0) the `CRASHLOOP_SUPPRESSED` comment wrongly implying a prior D_old always existed (cold-start reaches it with none); (P2) the re-probe-failed sub-path and the DEFERRED paths silently hitting the misleading generic banner. Both were absorbed before commit.

## Residual / deferred (honest accounting)

- **Cycle-deferred residual is NOT fully closed** for two abort paths: write-lock-held (~30s) and SIGTERM-not-exited (~10s) return with D_old still alive, so the bounded direct fallback is a temporary second writer there. This collapses to the pre-existing RDR-128 documented-availability residual (WAL non-corrupting, old-schema writes error loud) and is now operator-visible via distinct events. Worst-case write latency on the skew window is ~55s.
- **P3 concurrency (CA-2, election-lock one-reap)** is a process-level property; it is covered by `tests/daemon/test_t2_multistack_race.py` + CA-2 verification, not re-tested at the re-assert seam (fcntl locks are per-process; a thread test would not exercise it). Recorded, not silently dropped.
- **Field validation deferred.** The mechanism is unit- and integration-tested (8531-test suite green); the production version-skew symptom has not yet been observed post-fix. Next real stale-daemon-vs-current-client window is the validation opportunity — watch for the distinct `t2_index_write_version_skew_*` events.

## Acceptance signal

§Validation criterion is "zero IMMEDIATE-mismatch direct writes" (not "zero direct writes") — the ~55s-deferred residual is the accepted tradeoff, distinguishable in telemetry by its own events.
