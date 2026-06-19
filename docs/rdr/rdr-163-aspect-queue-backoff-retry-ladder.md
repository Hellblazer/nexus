---
title: "Aspect-Extraction Queue Bounded Backoff-Retry Ladder: Wire the Dormant mark_retry Primitive into a Transient-Failure-Surviving Drain"
id: RDR-163
type: Architecture
status: accepted
accepted_date: 2026-06-19
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-19
related_issues: [nexus-vealn, nexus-cgu27]
related: [RDR-089, RDR-096, RDR-128, RDR-129, RDR-138, RDR-149, RDR-152, RDR-155, RDR-158, RDR-161]
---

> **Backend scope (read first).** The aspect queue has two backends. The
> **PG / Java service** table `nexus.aspect_extraction_queue` (Liquibase
> `service/src/main/resources/db/changelog/aspects-001-baseline.xml`,
> `AspectRepository.java`, claim via `SELECT … FOR UPDATE SKIP LOCKED LIMIT 1`,
> reached through `http_aspect_queue.py` on `NX_STORAGE_BACKEND_ASPECT_QUEUE=
> service`) is the **strategic target** — RDR-152 makes it the substrate for
> *both* cloud and native-local mode (RDR-161). **Both deployments must work:**
> local mode (embedded PG behind the native binary) and cloud (managed
> multitenant PG, RLS, PgBouncer transaction-mode). Same Liquibase + repository
> code runs in both; the cloud-only correctness hazards (clock skew, RLS,
> txn-mode GUC discipline) are called out in §Decision and §Consequences. The
> legacy **SQLite** path
> (`aspect_extraction_queue.py` + `migrations.py`, CAS loop) is being **retired**
> (RDR-158, accepted) and **deleted** (RDR-152 P4.2, `nexus-gmiaf.25`). This RDR
> therefore lands the durable schema + claim-gating change in the **PG service**,
> and the backend-agnostic ladder decision in the Python worker. It does **not**
> add a throwaway SQLite migration to a doomed schema (see §Decision 3, §Approach).

## Problem Statement

The aspect-extraction queue worker has a **binary failure disposition** with a
**built-but-dormant** retry mechanism. Verified on `develop` 2026-06-19
(`nx_plan_audit` + failure-class sweep, confirmed against source).

#### Gap 1: Sustained-transient failures terminal-fail with manual-only recovery (a missing capability)

An item either succeeds, or — on the first exception that escapes the extractor's
three internal attempts — goes **terminal `failed`**. The worker's two exception
paths both route there: `src/nexus/aspect_worker.py:491-499` (extract raise) and
`:538-545` (persist raise) call `_mark_failed_routed` (`:424-447`), which sets
`status='failed'`. `claim_next` selects only `status='pending'`
(`aspect_extraction_queue.py:348`; Java `AspectRepository.java:522`), so a
terminal-failed row is excluded from future drains — it does **not** wedge the
queue (good), but it is also never retried (the gap).

The consequence is a recurring operational nightmare: a **sustained-transient**
failure — a multi-minute Claude API `529 overload` window, a daemon
WAL-contention spike (`sqlite3.OperationalError: database is locked`), a brief
network loss — outlives the three quick internal attempts and **permanently
fails the item**. Recovery today is a manual re-enqueue at the same
`(collection, source_path)` key. Across a large ingest, a transient upstream blip
silently strands a batch of aspects that look "done" but aren't.

#### Gap 2: The mark_retry primitive is built but dormant; no backend has next_retry_at (a wiring + schema gap)

A `mark_retry` primitive **exists end-to-end but is called by no worker code**:
`aspect_extraction_queue.py:461-478` (SQLite), `AspectRepository.java:619` (PG),
the daemon RPC allowlist (`t2_daemon.py:710`), and the HTTP client
(`http_aspect_queue.py:214-219`). It resets `status='pending'` and increments
`retry_count` but **nothing invokes it**. There is also no `next_retry_at` column
in either backend (PG columns are `status`, `retry_count`, `enqueued_at`), so
even if wired, a retry could not be *scheduled* — it would re-claim immediately,
re-failing in a tight loop during the very outage it should back off from. The
mechanism is two-thirds built and inert.

#### What is NOT broken (preserved, out of scope)

Crash recovery is solid — `reclaim_stale` (`:491+`) leases stuck `in_progress`
rows back to `pending` after 300s; `ExtractFail` is a deliberate terminal *skip*
(`:515`, not a failure); unsupported collections `mark_done` silently. The
failure-class sweep (both trees) confirmed **no other worker-queue shares this
disposition** — the ETL drivers, post-store hooks, and domain stores are
different classes. This RDR does not touch those.

## Decision

**Wire the dormant `mark_retry` into a bounded exponential-backoff retry ladder,
gated on failure classification, with terminal `failed` as the cap-exhausted /
non-retryable floor.**

1. **Classify failures.** A new `_is_retryable(exc)` predicate splits exceptions
   into *transient* (API overload/timeout, transient I/O, Voyage transients,
   `sqlite3.OperationalError "database is locked"` — reuse the vocabulary in
   `nexus.retry._voyage_with_retry`) versus *non-retryable* (programming-bug
   classes: `ValueError`, type errors, malformed records). Non-retryable goes
   terminal immediately — no wasted retries.

2. **Bounded ladder, server-stamped timestamp.** On a retryable failure under an
   attempt cap (default 5, a typed constant), the worker calls `mark_retry`
   through `t2_index_write` (routed to the service); **the PG service computes
   `next_retry_at = now() + interval` server-side** (the worker passes the attempt
   number / backoff interval, NOT an absolute client timestamp). This is
   deployment-correct: in cloud the worker host and the DB are different machines,
   so a client-computed timestamp would skew against the DB-clock claim gate; in
   local mode they are co-located, but server-side stamping is right for both.
   At/over the cap, fall to terminal `mark_failed`. `retry_count` already exists
   on the row (`QueueRow`, schema `:88`); the cap check reads it directly.

3. **Backoff-aware claiming — in the PG service.** Add a `next_retry_at
   TIMESTAMPTZ` column to `nexus.aspect_extraction_queue` (neither backend has it
   today) via a **new Liquibase changeset** (sibling to `aspects-001-baseline.xml`,
   not an edit to the baseline). The `FOR UPDATE SKIP LOCKED` claim query in the
   Java queue handler / `AspectRepository` gains
   `AND (next_retry_at IS NULL OR next_retry_at <= now())` so backed-off rows are
   not claimed early; the FIFO index `idx_aspect_queue_fifo` already covers the
   ordering. The legacy SQLite path is **out of scope** — it is being deleted
   (RDR-152 P4.2); adding a `migrations.py` ALTER + CAS gate there is throwaway
   work on a doomed schema. If a local-SQLite-mode fix is judged necessary during
   the deprecation window, it is a separately-scoped follow-on, not this RDR's
   default.

4. **Determinism without a host clock.** Because `next_retry_at` is stamped
   server-side (`now() + interval`), the durable timestamp is the DB's, not the
   worker host's. Test the interval deterministically against the DB clock
   (assert `next_retry_at - claimed_at ≈ expected_interval` within a tolerance,
   or pin a transaction timestamp), and unit-test the worker's retryable-vs-fail
   decision + attempt/interval selection in isolation. No global host-clock patch.

This is the **minimal** change that closes the gap: it reuses an existing
primitive, adds one nullable column, and inserts a decision in front of two
existing call sites. It deliberately keeps the terminal-failed state for the
genuinely unrecoverable; the ladder is for the transient class only.

## Approach (phased)

1. **P0 — PG schema + claim gating (Java service).** New Liquibase changeset
   adding `next_retry_at TIMESTAMPTZ` to `nexus.aspect_extraction_queue`. Extend
   the `FOR UPDATE SKIP LOCKED` claim query (`AspectRepository.java:522`) with
   `AND (next_retry_at IS NULL OR next_retry_at <= now())`; `markRetry`
   (`:619`) stamps `next_retry_at`; `markFailed` (`:600`) stays the terminal
   floor. **`reclaimStale` (`:629-637`) MUST NOT write `next_retry_at`** — leave
   it untouched when resetting `in_progress → pending` (clearing it bypasses the
   cap; see §Consequences). Tests:
   `service/src/test/java/.../AspectRepositoryTest.java` (+ a queue-claim test) —
   column present, backed-off row not claimed before `next_retry_at`, claimable
   at/after, AND a `markRetry → claim → (worker dies) → reclaimStale` case
   asserting `next_retry_at` is unchanged and the row is immediately claimable;
   verify with the **full mvn suite** (Java schema/constraint changes break
   fixtures a diff-read cannot enumerate; CI Java job is advisory-only).

2. **P1 — service-stamped `mark_retry` (signature change) + worker ladder.**
   This changes a **live API contract**, not just a dormant path. Add an
   `intervalSeconds: Long` parameter to the Java `markRetry`
   (`AspectRepository.java:615-624`); the body computes `next_retry_at =
   OffsetDateTime.now(ZoneOffset.UTC).plusSeconds(intervalSeconds)` **server-side**
   (do NOT accept an absolute client timestamp — avoids worker↔DB clock skew in
   cloud). Update the HTTP client (`http_aspect_queue.py:214-219`) to POST
   `{collection, source_path, interval_seconds}`, and update the store contract
   (`t2_store_contract` / its RDR-158 successor) in lock-step. **The doomed SQLite
   `mark_retry` (`aspect_extraction_queue.py:461-478`) must accept-and-ignore the
   new param** so the parity tripwire (`tests/db/test_http_t2_store_parity.py`)
   stays green until RDR-152 P4.2 deletes it. The **interval the worker passes**
   is `base * 2**retry_count` with **±20% jitter** (reuse the `retry.py:147`
   pattern) — the worker chooses the interval; the service stamps the absolute
   time. In the Python worker (`src/nexus/aspect_worker.py`, backend-agnostic):
   add `_is_retryable(exc)` (reuse `retry.py:96-117` vocabulary) +
   `_mark_retry_or_fail_routed(row, exc)` that the two exception sites
   (`:491-499`, `:538-545`) call instead of `_mark_failed_routed`; route through
   `t2_index_write`. Keep the broad `except` + log so a routing failure falls to
   `reclaim_stale`, not a dead worker thread. Tests: `tests/test_aspect_worker.py`
   — retryable under cap → pending + `retry_count+1` + `next_retry_at` within the
   jittered interval (assert `next_retry_at - claimed_at ≈ interval`); cap+1 →
   `failed`; non-retryable → `failed` immediate. HTTP/contract parity:
   `tests/db/test_http_t2_store_parity.py` (both client and SQLite stub updated).

3. **P-GATE — phase-review + dual-deployment validation.** Cross-walk this
   §Approach against the closing beads (`nexus-vealn` + any split children); full
   Python unit suite (`uv run pytest tests/ -k aspect`) + **full mvn suite**
   (Java schema change). **Validate against BOTH deployments:** local embedded PG
   (single-tenant default) and a cloud-parity run (multitenant, RLS, PgBouncer
   txn-mode) — reuse the xr7.8.9 cross-deployment gate harness (the same harness
   that caught the cloud-only `conexus-1az` collapse). Stacked review
   (code-review-expert + substantive-critic).

## Alternatives Considered

- **Do nothing / keep manual re-enqueue.** Rejected: the operational pain is
  real and recurring; `nx aspects requeue-failed` (sibling bead `nexus-2c51v`)
  reduces the toil but does not address the root cause (transient failures
  shouldn't reach terminal in the first place).
- **Unbounded retry.** Rejected: a genuinely poison item (permanently broken
  source) would churn forever. The cap + non-retryable classification bounds it.
- **In-memory retry inside the worker (no schema change).** Rejected: loses the
  backoff across worker restarts and across the daemon write boundary; the queue
  is the durable substrate and the backoff state belongs there. Also can't honor
  a multi-minute backoff without blocking a worker slot.
- **A separate dead-letter table.** Rejected as over-engineering for v1: the
  existing terminal `failed` state already *is* the dead-letter (visible via
  `nx doctor --check-aspect-queue`); a `next_retry_at` column on the existing
  table is the smaller surface.

## Consequences / Risks

- **Observable drain-semantics change.** Items now linger in `pending` with a
  future `next_retry_at` instead of going straight to `failed`. `nx doctor
  --check-aspect-queue` output and any test asserting immediate-terminal on a
  transient failure must be updated. This is the primary reason this work gets an
  RDR rather than landing as a bare bead.
- **`reclaim_stale` interaction (rule, not deferred).** `reclaimStale`
  (`AspectRepository.java:629-637`) **does NOT modify `next_retry_at`** — that
  column is written ONLY by `markRetry`. When reclaim resets a stale `in_progress`
  row to `pending`, it leaves `next_retry_at` as-is: either `NULL` (no prior
  backoff) or a past timestamp (the backoff window already elapsed), both of which
  make the row immediately claimable — the correct outcome for a crashed worker.
  Clearing it to `NULL` would be a **cap-bypass bug** (a high-`retry_count` row
  that exhausted its window would look brand-new to the gate and get one extra
  attempt); bumping it would wrongly punish a crash with extended backoff. The
  monotonic `retry_count` (incremented by both `markRetry` and `markFailed`,
  never reset by reclaim) is the cap's source of truth.
- **Cap is `MAX ± 1` under concurrent reclaim, by design.** The worker reads
  `retry_count`, decides `< cap`, then `mark_retry` increments — non-atomic, so a
  reclaim racing an active retry decision can overshoot by one attempt. Accepted:
  the cap exists to prevent *indefinite* loops, not to enforce exactly-`MAX`. The
  monotonic `retry_count` guarantees termination.
- **`isDrained` semantics.** Backed-off rows (`status='pending'`, future
  `next_retry_at`) correctly count as not-drained; `nx doctor
  --check-aspect-queue` may report `drained=false` after a transient wave until
  the windows elapse. Correct behavior — note it in implementation.
- **Backoff schedule tuning.** The base/cap defaults are a guess (e.g.
  30s/2m/10m/30m/2h, cap 5); they are config-knob candidates if real workloads
  show the window is wrong. No preventive over-tuning beyond the evidence.
- **Dual-deployment correctness (local PG + cloud).** The PG service runs as
  embedded local PG (RDR-161) and managed cloud PG. `next_retry_at TIMESTAMPTZ`
  + server-side `now()` are deployment-agnostic. Two cloud-only hazards must be
  defended: (1) **clock skew** — resolved by server-side stamping (worker never
  supplies the absolute time); (2) **PgBouncer transaction-mode + RLS** — claim
  (`FOR UPDATE SKIP LOCKED`) and `mark_retry` must each be a single transaction
  with no reliance on session-level state, and the new `next_retry_at` gate must
  compose with the per-tenant RLS predicate (the claim is already tenant-scoped
  via `idx_aspect_queue_fifo (tenant_id, status, enqueued_at)`). The cap +
  backoff are per-row, so there is no cross-tenant interference. Both deployments
  are gated (see §Approach P-GATE).
- **Cross-mode parity.** The `mark_retry(next_retry_at)` signature change must
  keep the HTTP client and the Java service in parity (enforced by
  `tests/db/test_http_t2_store_parity.py` + the `t2_store_contract`). The
  still-present SQLite `mark_retry` may be left dim-compatible (accepts/ignores
  the param) until RDR-152 P4.2 deletes it, rather than fully implementing the
  ladder there.

## Open Questions

- ~~Jitter?~~ **RESOLVED:** backoff carries **±20% jitter** (reuse `retry.py:147`)
  to avoid thundering-herd re-claims when a wide outage clears and all backed-off
  rows become eligible at once — critical for the cloud multi-worker case. Locked
  into §Approach P1.
- Is `retry_count` the right cap key, or should non-retryable-then-retryable
  transitions reset it? (Lean: monotonic `retry_count`, cap is total attempts.)

## Related

- `nexus-vealn` (implementing bead) under epic `nexus-cgu27` (queue hardening).
- RDR-128 / RDR-129 / RDR-149: T2 daemon single-writer + write-path hardening —
  the `t2_index_write` daemon-routing contract this RDR writes through.
- RDR-138: rename-cascade / aspect-worker lock hierarchy (`rename_lock`
  outermost) the queue methods obey.
- RDR-089 / RDR-096: the aspect-extraction pipeline this queue feeds.
