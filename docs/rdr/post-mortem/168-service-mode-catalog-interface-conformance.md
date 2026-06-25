# Post-Mortem: RDR-168 — Service-Mode Catalog Interface Conformance

**Closed:** 2026-06-25 · **Status:** closed · **Type:** Bug Fix · **Epic:** nexus-njrcn

## Problem

`HttpCatalogClient` (the service-mode catalog client) was not a faithful drop-in for the
local `Catalog`. Signatures diverged, so callers written against the local interface
`TypeError`'d in service mode — caught and swallowed by best-effort handlers. The visible
symptom: service-mode `nx index repo` left the catalog empty (`Documents: 0`,
`manifest_empty`), while T3 vector search still worked (which is why it stayed hidden).

## What we shipped

Audit-first, per the project's anti-patch-thrash discipline:

- **P1–P3**: an introspection **conformance test** (the recurrence guard) with a
  gate-locked predicate — every explicit named param of the local method must be matched
  by an explicit named param on the client; a `**kwargs` does **not** satisfy it (that is
  exactly how the `link_if_absent` silent-data-loss class hid). A `CatalogReader` /
  `CatalogWriter` `Protocol` pair as the caller-facing contract, and reconciliation of all
  **19** divergent signatures (18 breaking + 1 silent).
- **P4**: a **live** Java+Postgres index MVV (not a mocked client — the gate's
  requirement). It **falsified CA-4**: signature reconciliation alone did **not** restore
  catalog population. The MVV peeled back a chain of further service-mode defects, each
  hidden behind the last.

The full chain, all fixed: `collection_for` rendering v1 client-side (the 404-on-new-tuple
first-index break); three indexer init-gates that skipped catalog registration in service
mode; the manifest hook silently aborting on `HttpCatalogClient._db`'s `RuntimeError`
(`getattr` does not swallow it); chash length (client must send the 32-char natural ID);
`get_manifest`/links return-type parity (typed objects, not dicts); the Java
`updateDocument` metadata jsonb encoding; link created-vs-merged; and `resolve_span` /
`resolve_chash` over pgvector. Service-mode `nx index repo` now populates the catalog with
documents **and** manifest end-to-end; `nx doc cite` footnotes are restored.

## Lessons

1. **The conformance test checks signatures, not return types — that is a distinct bug
   class.** The same divergence (client returns `dict` where local returns a typed object)
   crashed service-mode housekeeping (`_prune_misclassified` on `.chash`). Fixed
   `get_manifest`/`links_*` to return `ManifestRow`/`CatalogLink` and added a return-type
   parity guard (`test_client_return_types_match_local_typed_returns`) so the class can't
   recur. A future hardening: extend the conformance test itself to cover return types.
2. **The live MVV was load-bearing.** A mocked client would have reported green while
   service-mode indexing stayed broken. CA-4 ("signatures alone fix it") was wrong, and only
   the real Java+PG wire path proved it — exactly why the gate mandated integration-level.
3. **The stacked substantive-critic earned its keep on nearly every behavioural change** —
   it caught the P3 `link_if_absent` upsert-collapse and an `all_documents` infinite loop,
   the `links` return-type Critical, and njrcn.4's chash-truncation High, all of which
   green tests and the line-level reviewer missed.
4. **`getattr(obj, "_db", None)` only swallows `AttributeError`.** A property that raises
   `RuntimeError` (service-mode `_db`) propagates through the default and silently aborted
   the manifest hook. A subtle, high-impact Python footgun.

## Deferred (tracked)

- **nexus-njrcn.8** (P3) — a live handler↔repo wire test for the thin catalog GET handlers
  (`resolve_span`/`resolve_chash` etc.). Both njrcn.4 reviewers deemed it deferrable: the
  handlers are thin glue, params verified, repo + client tested independently.

## Cross-references

Originating defect: **nexus-7y0ab**. Related: RDR-152 (Postgres/Java service), RDR-155
(pgvector T3), RDR-108 (catalog/T3 split), RDR-103 (collection-name authority).
Merged to `develop` as one 12-commit arc.
