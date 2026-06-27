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

## Research Findings

All findings below are **Verified** against the codebase at develop tip (2026-06-27) unless
marked Assumed. Recorded in T2 `nexus_rdr/172-research-*`.

### RF-1 (Verified): `store_put` is the *only* `fire_document` caller passing the T3 chunk id

Every other caller already passes the catalog tumbler (`catalog_doc_id`):
`src/nexus/doc_indexer.py:746` (`doc_id=_catalog_doc_id_for_batch`),
`src/nexus/pipeline_stages.py:858` (`doc_id=_lookup_existing_doc_id(...)`),
`src/nexus/code_indexer.py:509` (`doc_id=catalog_doc_id`),
`src/nexus/prose_indexer.py:266` (`doc_id=catalog_doc_id`). Only
`src/nexus/mcp/core.py:2068` passes `doc_id=doc_id`, where `doc_id = t3.put(...)` returns
the chunk natural-id (`chunk_chroma_id = sha256(content)[:32]`, core.py ~1995), **not** the
tumbler. **Impact:** the client fix (Approach 3) is a one-line consistency repair to a
convention already proven at four call sites — the lowest-risk, primary fix.

### RF-2 (Verified): the FK is composite `(tenant_id, doc_id) → catalog_documents(tenant_id, tumbler)`, and NULL satisfies it

`fk-001-catalog-cross-store.xml` (RDR-156, nexus-b7v6i): `aspect_extraction_queue
(tenant_id, doc_id) → catalog_documents(tenant_id, tumbler)` ON DELETE CASCADE. The column
was converted from `DEFAULT ''` to nullable; "NULL doc_id satisfies the FK; only non-NULL
values are checked." A non-NULL, non-tumbler value (the chunk hash) raises SQLSTATE 23503.
**Impact:** confirms both the failure mechanism and that NULL-coercion is a valid escape.

### RF-3 (Verified): the server *already* NULL-coerces blank `doc_id` — that is why the CLI path works

`AspectRepository.enqueue` writes `nullIfBlank((String) body.get("doc_id"))`
(`service/.../AspectRepository.java:230`; same at lines 500/715/1070 for the other insert
paths; `nullIfBlank` defined at :1522). The CLI ingest path passes `doc_id=''` → coerced to
NULL → FK satisfied. `store_put` passes a non-blank chunk hash → not coerced, non-NULL,
non-tumbler → FK violation. **Impact:** Approach 2 is a narrow extension of existing
behavior — "NULL if blank **or not a registered tumbler**" — the runtime mirror of the
migration's one-time orphan-nullify pre-flight (fk-001 Step 2).

### RF-4 (Verified): the worker tolerates NULL/empty `doc_id` — NULL-coercion is extraction-safe

`src/nexus/aspect_worker.py:574`: `queued_doc_id = getattr(row, "doc_id", "") or ""`; an
empty `doc_id` passes through and the `doc_id_lookup` falls back to the document text carried
on the queue row (RDR-089 P0.1 content-sourcing contract). **Impact:** Approach 2 causes no
extraction regression; a NULL-`doc_id` row still extracts from row content.

### RF-5 (Verified): `AspectHandler` bare-500s any non-`IllegalArgumentException`

`service/.../AspectHandler.java`: only `IllegalArgumentException → 400`; every other
`Exception → 500` with `e.getMessage()`. A constraint violation is therefore an unhandled
500. **Impact:** Approach 1 (map SQLSTATE class 23 → 4xx) is correct server hygiene; it ships
only in a fresh `engine-service` cut (two-lifecycle release rule).

### RF-6 (Verified): telemetry infra exists for the tripwire; `--fullstack` already has psql

Telemetry stores exist (`src/nexus/db/t2/telemetry.py`, `http_telemetry_store.py`,
`telemetry_etl.py`), so a counter-based CI tripwire on `aspect_extraction_enqueue_failed`
(Approach 4) needs no new infra. `--fullstack` (`rehearse_fullstack.sh`) already runs psql
against the service, so the `document_aspects > 0` assertion (Approach 5) is a one-line add.

### RF-7 (Assumed → risk): without the loudness arm, the class silently recurs

The failure is observable today only as a `log.warning("aspect_extraction_enqueue_failed")`
inside the best-effort `try/except` (`aspect_worker.py` hook). No counter, no test gate.
**Risk:** a client-only fix leaves the next field-level constraint change free to recur the
same silent-total failure. **Assessment:** the loudness arm (Approach 4/5) is the recurrence
guard, not optional polish — this is the explicit lesson of the RDR-142/3lbhb fail-closed
class. Classified Assumed because "a future constraint change will reintroduce silence" is a
forward-looking risk, not a present-state fact; the mitigation cost is low (one counter, two
assertions) and the downside of omission is another invisible total outage.

### RF-8 (Verified): the catalog write is committed before the enqueue runs — no visibility race behind the identity bug

A natural second failure mode (raised at gate Layer 3): `store_put` issues `catalog_store_hook`
and the enqueue as **separate HTTP round-trips**; even a correct `catalog_doc_id` would 500
if the catalog row were not yet visible to the enqueue's PG session. This is **ruled out**
under the supported topology: `service/.../db/TenantScope.java` runs every unit of work with
`autoCommit=false` inside a transaction and **eager-commits before returning the connection
to the pool** (`:25-31`, `:76`); Postgres default `synchronous_commit=on` makes that commit
durable before the HTTP 200 returns; both stores use connections to the **same PG server**
(`:105`), and the enqueue runs in a later READ COMMITTED txn that sees the committed parent
row. The design mandates a transaction-mode pooler (`:35-38`); a session-mode pooler or
`synchronous_commit=off` would reopen the window, which is out of scope here. **Impact:** the
identity mismatch (RF-1) is the *sole* cause; the fix does not need a distributed transaction
or retry for correctness. It also means a non-blank-unregistered `doc_id` at runtime can only
come from a **client bug**, not a race — see the revised Decision §1.

### RF-9 (Verified): `store_put` of a `knowledge__` note *does* reach the enqueue; the `--fullstack` harness comment is stale

The enqueue hook's only gate is `select_config(collection) is None`
(`aspect_worker.py`); `select_config("knowledge__…")` returns `_SCHOLARLY_PAPER_CONFIG`
(non-None — `aspect_extractor.py:353` registers the `knowledge__` prefix). There is **no**
`_classify_document_shape` gate in the hook — shape routing happens worker-side, *after*
claim, when choosing the per-document extractor. So `store_put` of a `knowledge__` note
reaches the enqueue and 500s; the empty queue is the swallowed failure, not a by-design skip.
The current `rehearse_fullstack.sh` comment ("store_put of plain knowledge notes does NOT
enqueue aspects: by design") is a **stale pre-root-cause misexplanation** and must be
corrected. **Impact on Approach 5:** `document_aspects > 0` is only a non-vacuous assertion
if the workload produces *extractable* aspects — note-shaped content can route to a
low/empty extraction worker-side. Approach 5 must therefore drive a **paper-shaped** document
(not a one-line note) and the assertion is validated post-fix by an actual `--fullstack` run,
not asserted in advance.

### Net effect on the Approach

The findings **re-rank and tighten** the surfaces:
- **Approach 3 (client) is the primary, lowest-risk fix** — make `store_put` match the
  convention already used at four call sites (RF-1); RF-8 confirms this fully closes the FK
  failure (no residual race).
- **Server NULL-coercion is scoped to BLANK only (the existing `nullIfBlank`, no change).**
  A non-blank-unregistered `doc_id` is a client bug (RF-8), so the server must surface it
  **loudly** as the typed 4xx (Approach 1) — *not* silently NULL it. Silent coercion of a
  non-blank id would mask exactly the loud-fail class this RDR exists to fix (RDR-142).
  (This revises the original Approach 2; see Decision.)
- **Approach 1 (SQLSTATE → 4xx)** is the server's loud surface, gated on an engine cut (RF-5).
- **Approach 4/5 (loudness)** are feasible with existing infra (RF-6) and are the recurrence
  guard (RF-7); Approach 5's workload must be paper-shaped and post-fix-validated (RF-9).

## Decision

Fix the failure on all three surfaces it touches, because each alone is insufficient: a
graceful server still drops aspects if the client sends a bad id; a correct client still
leaves the server fragile to the next constraint; and both fixed silently is how the class
recurs. Specifically:

1. **Server — never bare-500 a constraint violation; keep failures LOUD.** `AspectHandler`
   (and the shared handler error path) maps Postgres integrity errors (SQLSTATE class 23) to
   a typed 4xx with a structured body, distinct from genuine 5xx. The existing blank→NULL
   coercion (`nullIfBlank`, for the legitimate "no catalog id" sentinel) stays. The server
   does **not** silently NULL-coerce a *non-blank* unregistered `doc_id` — RF-8 shows such a
   value can only be a client bug (there is no visibility race), so masking it would
   reintroduce the silent-failure class this RDR exists to kill (RDR-142). It surfaces as the
   typed 4xx, which the client's loudness counter (3) then catches.
2. **Client — enqueue with the registered catalog identity.** `store_put` passes
   `catalog_doc_id` (the tumbler) to `fire_document`, not the `t3.put` chunk id; when no
   catalog id was minted, it passes `doc_id=''` (the existing blank→NULL sentinel) rather than
   a chunk id that can never satisfy the FK. RF-8 confirms this alone fully closes the FK
   failure — no residual ordering/visibility race.
3. **Loudness — make the swallow observable and tripwired.** Keep the enqueue best-effort
   (never block ingest), but (a) emit a counter/structured signal on
   `aspect_extraction_enqueue_failed` that CI asserts is zero across the ingest E2E, and
   (b) have `--fullstack` assert `document_aspects > 0` after a **paper-shaped** MCP workload
   (RF-9), as a hard `bad`/fail, not a soft note.

The client identity fix (2) is the correctness fix; the server typed-4xx (1) and the loudness
arm (3) are the recurrence guards — every arm keeps failures **loud**, none silently masks a
mis-identified id.

## Approach

1. **Server: typed integrity-error mapping.** In `AspectHandler.java` (and any sibling
   handler sharing the catch ladder), add a `catch` arm that detects SQLSTATE class `23`
   (e.g. via `org.postgresql.util.PSQLException#getSQLState`) and responds `409`/`422` with
   `{"error": ..., "sqlstate": ...}`, ahead of the generic `Exception → 500`. Test:
   `AspectRepositoryTest`/handler test asserting a violating enqueue yields the typed 4xx,
   not 500. (Closes Gap 1, server half.)
2. **Server: keep blank→NULL; do not silently coerce a non-blank id.** Leave the existing
   `nullIfBlank` (blank `doc_id` → NULL, the legitimate sentinel) unchanged. A *non-blank*
   `doc_id` that fails the FK is a client bug (RF-8: no race) and must surface as the typed
   4xx from step 1, not a silent NULL. Test: enqueue with `doc_id=''` lands a `pending` row
   with `doc_id IS NULL` and 200 (unchanged); enqueue with a non-blank unregistered `doc_id`
   returns the typed 4xx, never 500 and never a silent 200. (Reinforces Gap 1 + the loudness
   thesis; replaces the original silent-NULL-coerce plan per gate Layer 3.)
3. **Client: pass the catalog identity.** In `src/nexus/mcp/core.py` `store_put`, change the
   `fire_document` call to forward `catalog_doc_id` when non-empty, else `''`. No change to
   the other `fire_document` callers — RF-1 verified all four already pass `catalog_doc_id`.
   Test: a service-mode `store_put` enqueues a row whose `doc_id` equals the registered
   tumbler (exact `==`, not just non-null). (Closes Gap 3; this is the correctness fix.)
4. **Loudness: enqueue-failure tripwire.** Add a structured signal (telemetry counter or a
   well-known log event the E2E greps) on `aspect_extraction_enqueue_failed`. Wire a CI
   assertion that the ingest E2E completes with zero such events. Verify the counter actually
   increments on a forced failure (so the gate is non-vacuous). (Closes Gap 2, half.)
5. **Loudness: `--fullstack` positive assertion + correct the stale comment.** Fix the stale
   `rehearse_fullstack.sh` comment (RF-9: `knowledge__` *does* enqueue). Drive a **paper-shaped**
   document through the MCP workload (a short research-paper fragment, not a one-line note),
   then assert `SELECT count(*) FROM nexus.document_aspects WHERE tenant_id='default' > 0` as a
   hard `bad`/`FAILS++` (not a soft `note`), guarded by a non-vacuity check that the store_put
   actually landed the document. This assertion is **validated by a real post-fix `--fullstack`
   run** before the RDR closes, not asserted in advance. (Closes Gap 2, half.)
6. **Regression seam: service-mode round-trip test — vehicle specified.** Two layers, because
   "fast" and "real service" trade off: (a) a fast in-process test that asserts `store_put`
   forwards `catalog_doc_id` (not the chunk id) to the enqueue hook (mock/capture the hook
   args — proves Gap 3 without a container); (b) the full `store_put → enqueue → worker drain →
   document_aspects` chain lives in the `--fullstack` harness (same vehicle as step 5, with the
   stub extractor on PATH). No new mock service is introduced.
7. **Docs.** Update `src/nexus/mcp/AGENTS.md` / `docs/architecture.md` post-store hook
   contract to state the enqueue identity is the catalog id (blank → NULL for the no-catalog
   case; non-blank unregistered → typed 4xx, never silent); note the tripwire.

**RDR-145 dependency.** RDR-145 Gap 3 (whether `knowledge__` collections belong on the aspect
surface at all) is unresolved. If it resolves to "exclude `knowledge__`", Approach 5's
paper-shaped workload must move to an explicitly extractor-eligible collection, and the
exclusion must be reconciled with RF-9. This RDR fixes the *transport* failure (the 500 +
swallow); RDR-145 owns the *policy* of which shapes get aspects. They do not overlap, but
Approach 5's workload choice must track the RDR-145 Gap 3 decision.

## Consequences

- **Positive.** RDR-089 aspect extraction works in service mode; the failure class is
  observable and tripwired, so it cannot silently regress; the server stops emitting 500s
  for caller/data errors; identity on the queue row is correct, unblocking RDR-145's
  note-backed-identity work and any worker logic keyed on `doc_id`.
- **Negative / cost.** Touches both the Java service and the Python client (coordinated
  change across the HTTP boundary — requires a fresh `engine-service` cut if the server arm
  ships, per the two-lifecycle release rules). The client fix (step 3) alone is correctness-
  complete (RF-8) and ships in a normal PyPI release; the server typed-4xx (step 1) is a
  separate engine cut and can land independently.
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
- **Server silently NULL-coerces *non-blank* unregistered ids (original Approach 2).**
  Rejected at gate Layer 3: RF-8 shows a non-blank-unregistered id can only be a client bug
  (no visibility race), so silently NULLing it would mask exactly the silent-failure class
  this RDR exists to kill (RDR-142). The server surfaces it loudly as a typed 4xx instead.
- **Distributed transaction / retry-on-FK between catalog write and enqueue.** Rejected as
  unnecessary: RF-8 rules out the visibility race (eager-commit before HTTP return,
  `synchronous_commit=on`, same PG server), so the correct id always satisfies the FK.
- **Drop the `doc_id → catalog_documents` FK.** Rejected: the FK is deliberate RDR-156
  schema-enforced integrity; removing it trades a loud, fixable failure for silent orphans
  (the pre-RDR-156 state RDR-145 was filed against).
- **Make the enqueue blocking/transactional with the catalog write.** Rejected: violates
  RDR-089 P0.1 (ingest must not block on extraction) and couples two HTTP transactions.
- **Inline extraction at store_put (skip the queue).** Rejected: ~25s/doc on the ingest
  path (RDR-089 P1.3 spike); the queue exists precisely to avoid this.
