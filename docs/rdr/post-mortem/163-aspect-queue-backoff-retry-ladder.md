# Post-Mortem — RDR-163: Aspect-Extraction Queue Bounded Backoff-Retry Ladder

**Closed:** 2026-06-24 · **Status:** closed (implemented) · **Epic:** nexus-cgu27 (sub-epic nexus-vealn)

## What shipped

The dormant `mark_retry` primitive is now wired into a transient-failure-surviving drain, landed in the strategic PG/Java service (the SQLite path was accept-and-ignore-stubbed only, since it is retired per RDR-158 / deleted per RDR-152 P4.2).

- **P0 (nexus-795gv)** — `aspects-002-next-retry-at.xml` adds `next_retry_at TIMESTAMPTZ`; `AspectRepository.claimNext` gates the FOR UPDATE SKIP LOCKED claim on `next_retry_at IS NULL OR next_retry_at <= now()` (server clock). `reclaimStale` leaves `next_retry_at` untouched (cap-bypass / crash-punish defense).
- **P1 (nexus-ztpt6)** — `markRetry(intervalSeconds)` server-stamps `next_retry_at = now() + make_interval(secs)`; the Python worker gains `_is_retryable` (reusing both retry.py transient predicate classes), `_backoff_interval_seconds` (base·2ⁿ ±20% jitter), and `_mark_retry_or_fail_routed` wired into both worker exception sites (non-retryable / retry_count ≥ cap → terminal; else backed-off retry).
- **P-GATE (nexus-bsm0p)** — §Approach cross-walk PASSED (all 12 named artifacts traced); dual-deployment validated via the real txn-mode PgBouncer harness (claim + markRetry single-transaction, `next_retry_at` gate composes with RLS, `isDrained=false` for backed-off rows, no cross-tenant claim) plus embedded-PG coverage.

## What went well

- Server-side `now()+interval` stamping (not a client-computed absolute timestamp) was the correct cloud clock-skew defense, validated through a real transaction-mode pooler.
- The stacked-reviewer gate caught a genuine **High** at P1: `enqueue`'s ON CONFLICT reset `retry_count=0` but did not clear `next_retry_at`, so re-enqueuing a backed-off row would have been silently held for up to the full backoff. Fixed pre-merge.

## What we learned

- **PG `TIMESTAMPTZ` is microsecond precision.** A P0 test stamped a nanosecond Java `OffsetDateTime` and asserted exact round-trip equality; it passed locally by luck and failed in CI on the sub-microsecond digits. Lesson: truncate to `ChronoUnit.MICROS` (or assert with tolerance) for any PG timestamp round-trip equality. The full CI matrix caught what the local run did not.
- **A doomed backend's stub can hide a real-vs-theatre question.** The SQLite `mark_retry` accept-and-ignores `interval_seconds`, so in SQLite mode the backoff is dropped (hot-loop to cap with zero delay). This is honest and documented (the path is retired), but the integration test's "ladder climbs to cap" only proves the cap, not the timing — the real backoff timing is proven in the Java/PG tests. Decomposed coverage across the HTTP boundary, with the gap named, was the right call rather than building a disproportionate live Python↔Java harness.

## Deferred / follow-on

- Full live-service Python→HTTP→DB timing round-trip: not built (both sides of the HTTP boundary independently verified). Documented, not silent.
- Remaining `nexus-cgu27` epic work (NOT part of RDR-163): `nexus-2c51v` (bulk requeue-failed CLI), `nexus-m26oq` / `nexus-yrlbd` (MinerU OOM/timeout resilience).
