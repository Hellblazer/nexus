---
title: "Service-Mode Aspect-Enqueue Silent Failure: store_put's Enqueue 500s on the RDR-156 doc_id FK and the Hook Swallows It"
id: RDR-172
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-27
related_issues: [nexus-ov0sw]
related_rdrs: [RDR-089, RDR-152, RDR-156, RDR-142, RDR-145, RDR-163]
supersedes: []
related_tests: [tests/e2e/migration-rehearsal/rehearse_fullstack.sh, tests/test_aspect_worker.py, service/src/test/java/dev/nexus/service/db/AspectRepositoryTest.java]
---

# RDR-172: Service-Mode Aspect-Enqueue Silent Failure: store_put's Enqueue 500s on the RDR-156 doc_id FK and the Hook Swallows It

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

In 6.0.0 (service mode: PG + Java service per RDR-152/155), every MCP `store_put` silently
skips aspect enqueue. RDR-089 structured-aspect extraction is **inert for the store_put
path**: `document_aspects` is never populated, the service `aspect_extraction_queue` stays
empty, and nothing surfaces — not in logs the operator reads, not in any green test. The
failure was found only by an out-of-band full-stack rehearsal probe
(`migration-rehearsal --fullstack`, 2026-06-27).

#### Gap 1: the service returns a bare HTTP 500 on a constraint violation it should handle

`store_put` fires the aspect hook with a **non-empty** `doc_id`
(`src/nexus/mcp/core.py:2068`):

```python
_hooks.fire_document(doc_id, col_name, content, doc_id=doc_id)
```

where `doc_id` at that point is the `t3.put(...)` return (the chunk/content identity),
**distinct** from `catalog_doc_id` (the catalog tumbler returned by `catalog_store_hook`
and passed to `fire_batch` immediately above). RDR-156 `fk-001` (nexus-b7v6i) adds a
composite foreign key `aspect_extraction_queue.doc_id → catalog_documents`
("non-NULL values are checked against the parent table"; `''` is pre-flight coerced to
`NULL`). The non-empty `doc_id` carried into the enqueue is not a committed
`catalog_documents` key at enqueue time, so the INSERT raises a Postgres FK violation
(SQLSTATE 23503).

`AspectHandler.java` catches only `IllegalArgumentException → 400`; **every other
`Exception` → bare `500`** with `e.getMessage()` in the body. A constraint violation is
therefore an unhandled server error, not a typed 4xx the client can reason about.

Captured directly (no-swallow probe through the exact client path
`mcp_infra.t2_index_write → http_aspect_queue.enqueue → _post → raise_for_status`):

```
httpx.HTTPStatusError: Server error '500 Internal Server Error'
  for url 'http://127.0.0.1:<port>/v1/aspects/queue/enqueue'
```

A probe with a deliberately bogus `doc_id` (`"ov0sw-probe"`, absent from
`catalog_documents`) reproduces the identical 500 — confirming the `doc_id` FK is the
dependent constraint.

#### Gap 2: the Python enqueue hook swallows the failure, so the inert pipeline is invisible

`aspect_extraction_enqueue_hook` (`src/nexus/aspect_worker.py`) is best-effort by design
(RDR-089 P0.1: ingest must never block on extraction):

```python
try:
    t2_index_write(lambda t2: t2.aspect_queue.enqueue(collection, source_path, content=content, doc_id=doc_id))
except Exception:  # noqa: BLE001 — enqueue is best-effort
    _log.warning("aspect_extraction_enqueue_failed", source_path=source_path, collection=collection, exc_info=True)
    return
```

The 500 is swallowed to a `log.warning` and discarded. "Never block ingest" is correct;
"never tell anyone the queue write failed, on every single document, forever" is the
defect. There is no CI tripwire on `aspect_extraction_enqueue_failed` and no post-ingest
assertion that `document_aspects > 0`, so the entire feature can regress to a no-op while
the suite stays green. This is the same loud-fail-closed class as RDR-142 (nexus-3lbhb).

#### Gap 3: the client passes the wrong identity (chunk id, not the registered catalog id)

Even with a graceful server, the enqueue is semantically wrong: it stamps the queue row's
`doc_id` with the T3 chunk identity rather than the registered `catalog_doc_id`. The worker
and the `doc_id → catalog_documents` FK both expect a catalog document key. RDR-145 already
identified the note-backed-document-identity seam in the aspect pipeline; this is the
service-mode instance of the same identity confusion, now hard-failing against a real FK
instead of producing a soft orphan.

## Context

- **Best-effort by contract.** RDR-089 P0.1 makes the enqueue synchronous-but-best-effort
  so a slow/failed extraction never blocks the ~microsecond ingest path. The swallow is
  intentional; the silence is not.
- **Not a release blocker by itself.** Ingest, search, and `nx_answer` all work in service
  mode; only the aspect-derived surfaces (problem/method/dataset extraction, salience,
  highlights) are silently absent. But the RDR-089 value is wholly lost in service mode
  until this is fixed, and 6.0.0 is the first release where service mode is the only path.
- **Identity model (RDR-101/108).** Catalog documents are addressed by tumbler; T3 chunks
  are content-addressed blobs (`sha256(chunk_text)[:32]`). `store_put` pre-registers the
  catalog entry (`catalog_doc_id`) and separately gets a `t3.put` return; the bug is that
  the enqueue is handed the latter.
- **Reproduction vehicle.** `tests/e2e/migration-rehearsal/run.sh --fullstack` stands up the
  real topology (PG16+pgvector + native service + nx-mcp + claude CLI) and drives `store_put`
  through the MCP. It currently carries a documented gap note for `document_aspects = 0`;
  this RDR converts that into an assertion.

## Decision

Fix the failure on all three surfaces it touches, because each alone is insufficient: a
graceful server still drops aspects if the client sends a bad id; a correct client still
leaves the server fragile to the next constraint; and both fixed silently is how the class
recurs. Specifically:

1. **Server — never bare-500 a constraint violation.** `AspectHandler` (and the shared
   handler error path) maps Postgres integrity errors (SQLSTATE class 23) to a typed 4xx
   with a structured body, distinct from genuine 5xx. Independently, `AspectRepository.enqueue`
   **NULL-coerces an unregistered `doc_id`** (mirroring the existing `'' → NULL` pre-flight
   contract) so a not-yet-registered document degrades to a queue row with `doc_id = NULL`
   rather than hard-failing — the worker already tolerates NULL/empty `doc_id` (CLI path
   passes `''`).
2. **Client — enqueue with the registered catalog identity.** `store_put` passes
   `catalog_doc_id` (the tumbler) to `fire_document`, not the `t3.put` chunk id; when no
   catalog id was minted, it passes `doc_id=''` (the documented NULL-coercion sentinel)
   rather than a chunk id that can never satisfy the FK.
3. **Loudness — make the swallow observable and tripwired.** Keep the enqueue best-effort
   (never block ingest), but (a) emit a counter/structured signal on
   `aspect_extraction_enqueue_failed` that CI asserts is zero across the ingest E2E, and
   (b) have `--fullstack` assert `document_aspects > 0` after the MCP workload.

The combination of server-side NULL-coercion (1) and the client identity fix (2) is
belt-and-suspenders on purpose: either alone stops the 500, but Gap 3 means we want the
*correct* identity on the row when one exists, and Gap 1 means we want the server robust to
the next caller that gets identity wrong.

## Approach

1. **Server: typed integrity-error mapping.** In `AspectHandler.java` (and any sibling
   handler sharing the catch ladder), add a `catch` arm that detects SQLSTATE class `23`
   (e.g. via `org.postgresql.util.PSQLException#getSQLState`) and responds `409`/`422` with
   `{"error": ..., "sqlstate": ...}`, ahead of the generic `Exception → 500`. Test:
   `AspectRepositoryTest`/handler test asserting a violating enqueue yields the typed 4xx,
   not 500. (Closes Gap 1, server half.)
2. **Server: NULL-coerce unregistered `doc_id` in the enqueue insert.** In
   `AspectRepository.enqueue`, before the INSERT, resolve `doc_id` against `catalog_documents`
   (same tenant); if absent, write `NULL`. Mirror the `aspects-001`/`fk-001` `'' → NULL`
   pre-flight contract. Test: enqueue with an unregistered `doc_id` lands a `pending` row with
   `doc_id IS NULL` and returns 200. (Closes Gap 1, durable half.)
3. **Client: pass the catalog identity.** In `src/nexus/mcp/core.py` `store_put`, change the
   `fire_document` call to forward `catalog_doc_id` when non-empty, else `''`. Audit the
   non-MCP ingest callers of `fire_document` (`doc_indexer`, pipeline_stages) for the same
   identity expectation. Test: a service-mode `store_put` enqueues a row whose `doc_id`
   equals the registered tumbler. (Closes Gap 3.)
4. **Loudness: enqueue-failure tripwire.** Add a structured signal (telemetry counter or a
   well-known log event the E2E greps) on `aspect_extraction_enqueue_failed`. Wire a CI
   assertion that the ingest E2E completes with zero such events. (Closes Gap 2, half.)
5. **Loudness: `--fullstack` positive assertion.** In `rehearse_fullstack.sh`, after the MCP
   workload, assert `SELECT count(*) FROM nexus.document_aspects WHERE tenant_id='default' > 0`
   (with a non-vacuity guard that store_put actually ran). Replace the current documented-gap
   note. (Closes Gap 2, half.)
6. **Regression seam: end-to-end aspect round-trip test.** A service-mode test
   (`store_put → enqueue → worker drain → document_aspects row`) using the stub extractor, so
   the full chain is covered by a fast suite, not only the heavy `--fullstack` container.
7. **Docs.** Update `src/nexus/mcp/AGENTS.md` / `docs/architecture.md` post-store hook
   contract to state the enqueue identity is the catalog id and that unregistered ids
   NULL-coerce; note the tripwire.

## Consequences

- **Positive.** RDR-089 aspect extraction works in service mode; the failure class is
  observable and tripwired, so it cannot silently regress; the server stops emitting 500s
  for caller/data errors; identity on the queue row is correct, unblocking RDR-145's
  note-backed-identity work and any worker logic keyed on `doc_id`.
- **Negative / cost.** Touches both the Java service and the Python client (coordinated
  change across the HTTP boundary — requires a fresh `engine-service` cut if the server arm
  ships, per the two-lifecycle release rules). The NULL-coercion adds a per-enqueue
  catalog lookup (one indexed point query; negligible, and only on the ingest path).
- **Neutral.** The best-effort contract is preserved — ingest still never blocks on enqueue;
  the only behavioral change is that failures are now loud and rare instead of silent and
  total.

## Alternatives Considered

- **Client-only fix (pass `catalog_doc_id`, leave server bare-500).** Rejected: leaves the
  server fragile to the next caller that gets identity wrong, and keeps the swallow silent —
  the class recurs the next time any enqueue field violates a constraint.
- **Server-only fix (NULL-coerce, leave client passing the chunk id).** Rejected: the queue
  row then always has `doc_id = NULL` even when a real catalog id exists, discarding the
  identity the worker and downstream FKs want (Gap 3 unaddressed).
- **Drop the `doc_id → catalog_documents` FK.** Rejected: the FK is deliberate RDR-156
  schema-enforced integrity; removing it trades a loud, fixable failure for silent orphans
  (the pre-RDR-156 state RDR-145 was filed against).
- **Make the enqueue blocking/transactional with the catalog write.** Rejected: violates
  RDR-089 P0.1 (ingest must not block on extraction) and couples two HTTP transactions.
- **Inline extraction at store_put (skip the queue).** Rejected: ~25s/doc on the ingest
  path (RDR-089 P1.3 spike); the queue exists precisely to avoid this.
