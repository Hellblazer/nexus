# RDR-101 Phase 0: Disposition of `bib_semantic_scholar_id` (Catalog vs T3 Home)

**Bead**: nexus-o6aa.2
**RDR**: RDR-101 (Event-Sourced Catalog with Immutable Document Identity)
**Phase**: 0 (Acceptance and survey)
**Author**: Phase 0 survey agent
**Status**: Decided (Option A, with one carve-out)

## Decision

**Option A: bibliographic fields move off T3 chunk metadata onto the Document projection (T2 SQLite), populated by a typed `DocumentEnriched` event.**

The `bib_semantic_scholar_id` skip marker also moves to the Document projection. `nx enrich bib`'s skip query becomes a catalog read, not a T3 chunk-metadata scan.

Carve-out: the T3 metadata field is retained as a write-through projection cache through Phase 4 (the deprecation window) so existing readers, plugins, and the citation-link generator continue to work without ordering hazards. Phase 5 strips the field from T3 the same release that strips `source_path` and per-chunk `title`. This is the standard RDR-101 deprecation pattern (Phase 5 §"T3 metadata schema: remove `source_path`, per-chunk `title`, `git_*`, `corpus`, `store_type`"); bib fields are added to that list.

Departure from the brief's lean toward A: minimal. The brief's option A says "bib fields move to Document projection." This decision says exactly that, with the only nuance being that the migration uses the same dual-write window as every other field RDR-101 strips from T3, rather than cutting over in one PR.

### Why A over B

The brief poses A as cleaner-but-more-plumbing and B as simpler-but-contradicts-RDR-101. Three concrete reasons make A the only workable choice in context:

1. **B is structurally inconsistent with RDR-101 §"One canonical fact per attribute"** (Core Invariant 3). RDR-101's whole thesis is that no per-document fact lives on chunks. `bib_semantic_scholar_id` is a per-document fact (one S2 paper ID per source paper, written identically to every chunk of that paper today, line 198-200 of `enrich.py`). Letting it stay on the chunk under a `bib_*` prefix while every other per-document field migrates makes the field a permanent special case and reopens the drift surface that RDR-101 §"Gap 1" enumerates.
2. **Today's enrich.py skip logic is already at risk from Phase 4.** RDR-101 Phase 4's `aspect_readers.py:CHROMA_IDENTITY_FIELD` rewrite (substantive critique C2 in the RDR) is a structural mandate that aspect extraction joins via `doc_id` instead of `source_path`. The same agent that owns Phase 4's identity-field rewrite also owns the join paths that bib enrichment currently bypasses by reading T3 metadata directly. Leaving `bib_semantic_scholar_id` on T3 chunks under option B would require a second special-case identity dispatch at exactly the moment Phase 4 is consolidating onto one.
3. **The skip query is cheaper from the catalog projection than from T3.** Today's skip query (line 102-125 of `enrich.py`) paginates 300 rows at a time through ChromaDB Cloud, fetching `metadatas` for every chunk in the collection just to count which are already enriched. Under option A, the same skip is a single SQLite query: `SELECT doc_id FROM documents WHERE bib_id_field IS NOT NULL AND coll_id = ?`. Local read, indexed, sub-millisecond, and proportional to document count rather than chunk count. For a paper collection with ~50 chunks per paper, this is a 50x reduction in network I/O and ChromaDB Cloud quota burn at every `nx enrich bib` invocation.

Option B would defer all three problems into a permanent special case. None of them is forced by ordering or migration cost. A is cheaper to migrate AND structurally consistent.

## Background: today's flow

Source files traced (file:line is verbatim from the codebase as of `feature/art-arch-docs-corpus-nexus-4.5`):

### Where `bib_semantic_scholar_id` is set

- **Schema declaration** (`src/nexus/metadata_schema.py:78-86`): declared as one of five `bib_*` keys in `ALLOWED_TOP_LEVEL`. Line 80-81 comment is explicit: "load-bearing 'this title was enriched' marker (commands/enrich.py uses presence to skip already-enriched titles; catalog/link_generator.py uses it for citation links)."
- **Schema placeholder filter** (`src/nexus/metadata_schema.py:124-127`): listed in `_BIB_FIELDS`. The `normalize()` step at line 200-202 drops the entire bib_* set together when every value is the empty placeholder, so non-enriched chunks do not consume metadata budget.
- **Factory parameter** (`src/nexus/metadata_schema.py:290`): `bib_semantic_scholar_id: str = ""` in `make_chunk_metadata()` signature. The factory is the single entry point for indexer-side metadata construction.
- **Writer, T3 side** (`src/nexus/commands/enrich.py:198-200`): `merged["bib_semantic_scholar_id"] = bib.get("semantic_scholar_id", "")` inside the per-chunk merge loop. The merged dict is then passed to `col.update(ids=..., metadatas=...)` at line 211-214.
- **Writer, catalog side** (`src/nexus/commands/enrich.py:478-480`): `meta_update["bib_semantic_scholar_id"] = bib_meta.get("semantic_scholar_id", "")` inside `_catalog_enrich_hook`. The meta_update dict is passed to `cat.update(tumbler, ..., meta=meta_update)` at line 492-497. So the field is **already written to both stores today**, which is exactly the duplication surface RDR-101 §"Gap 3" enumerates.

### Where `bib_semantic_scholar_id` is read

- **Skip-if-already-enriched, T3 read** (`src/nexus/commands/enrich.py:113-114`): `if meta.get(id_field, ""): already_enriched += 1; continue`. `id_field` is set by `_resolve_bib_backend()` at `src/nexus/commands/enrich.py:369` to `"bib_semantic_scholar_id"` for the S2 backend (or `"bib_openalex_id"` for OpenAlex). This is the only true skip-logic read.
- **Citation link generation, catalog read** (`src/nexus/catalog/link_generator.py:50-58`): iterates over `cat.all_documents()` and reads `entry.meta.get("bib_semantic_scholar_id", "")` to build `id_to_tumbler` and `references` mappings. Catalog-side read, not T3.

### What the skip-presence check is for

Idempotency. `nx enrich bib <coll>` is invoked repeatedly during paper-corpus enrichment runs (rate-limited by S2/OpenAlex API quotas; sometimes resumed across sessions when an API key flips, sometimes restarted after process kills). Without the skip, every invocation re-queries Semantic Scholar for every paper, burning the S2 rate budget on titles already enriched. The presence-of-`bib_semantic_scholar_id` test says "this title produced a hit on this backend; do not re-query." It does NOT serve as a value lookup; it is a cardinality test (empty string vs non-empty string). Anything that owns the same cardinality signal (`bib_id IS NOT NULL`, `last_enriched_at IS NOT NULL`, etc.) on the catalog side is a drop-in replacement.

Today's flow exposes the duplication: the field is already written to both T3 and catalog (lines 198-200 and 478-480 of `enrich.py`), but the skip-test reads only T3, while the citation-link generator reads only the catalog. The two writers can drift if a partial failure interleaves them, exactly the RDR-101 §"Gap 1" failure mode (silent empty results) that prompted the greenfield design.

## Migration plan

The plan slots into RDR-101's existing six-phase rollout. Each step references the phase that owns it.

### Phase 0 (this deliverable, complete)

- This document records the decision. No code changes. No event-type addition; `DocumentEnriched` is already declared in RDR-101 §"Event log" (line 287 of the RDR markdown).

### Phase 1 (Event log infrastructure)

When Phase 1 defines the event-type schemas in `src/nexus/catalog/events.py`, `DocumentEnriched`'s `payload` field is given a typed structure for the bib case:

```python
@dataclass(frozen=True)
class DocumentEnrichedPayload:
    """Payload schema for DocumentEnriched events.

    The model_version column on the Aspect projection projects from
    `payload.schema_version`; the bib_* fields project to the Document
    projection's bib_* columns.

    schema_version literals defined in this file:
      - "bib-s2-v1": Semantic Scholar enrichment.
      - "bib-openalex-v1": OpenAlex enrichment.
      - "scholarly-paper-v1": aspect-extraction enrichment (pre-existing).
    """
    schema_version: str
    bib_year: int = 0
    bib_authors: str = ""
    bib_venue: str = ""
    bib_citation_count: int = 0
    bib_semantic_scholar_id: str = ""
    bib_openalex_id: str = ""
    bib_doi: str = ""
    references: tuple[str, ...] = ()
    enriched_at: str = ""
```

The `(type, v)` projector dispatch in RDR-101 §RF-101-2 handles `DocumentEnriched(v=1)` by writing the bib_* fields onto the `Document` projection columns (and the `references` tuple onto the `Aspect` projection if the schema chooses to denormalize cite-graph data there; the citation-link generator already iterates `cat.all_documents()`, so a Document-projection home is sufficient).

The `Document` projection schema gains four bib_* columns plus an `enriched_at` timestamp:

```sql
ALTER TABLE documents ADD COLUMN bib_year INTEGER DEFAULT 0;
ALTER TABLE documents ADD COLUMN bib_authors TEXT DEFAULT '';
ALTER TABLE documents ADD COLUMN bib_venue TEXT DEFAULT '';
ALTER TABLE documents ADD COLUMN bib_citation_count INTEGER DEFAULT 0;
ALTER TABLE documents ADD COLUMN bib_semantic_scholar_id TEXT DEFAULT '';
ALTER TABLE documents ADD COLUMN bib_openalex_id TEXT DEFAULT '';
ALTER TABLE documents ADD COLUMN bib_doi TEXT DEFAULT '';
ALTER TABLE documents ADD COLUMN bib_enriched_at TEXT DEFAULT '';
CREATE INDEX idx_documents_s2_id ON documents(bib_semantic_scholar_id) WHERE bib_semantic_scholar_id != '';
CREATE INDEX idx_documents_oa_id ON documents(bib_openalex_id) WHERE bib_openalex_id != '';
```

The two partial indexes mirror the existing skip-query cardinality test (presence-of-non-empty-string).

### Phase 2 (Synthesize log from existing state)

When the Phase 2 walker emits `DocumentRegistered` events from existing catalog rows, an extra step backfills bib enrichment as `DocumentEnriched` events. For each catalog row whose `meta` carries a non-empty `bib_semantic_scholar_id` or `bib_openalex_id`, emit a synthesized `DocumentEnriched(doc_id, schema_version="bib-s2-v1" or "bib-openalex-v1", payload={...}, ts=row.indexed_at)` immediately after the corresponding `DocumentRegistered`. Tag the synthesized event with `_synthesized: true` per the convention RDR-101 §Phase 1 already establishes for tombstone and alias synthesis.

The walker reads the source-of-truth from the catalog row, not from T3 chunks, because the catalog already holds the bib_* values today (lines 478-480 of `enrich.py`) and is the side RDR-101 designates as the projection home. T3 chunks may carry the same values, but the catalog is the authority for synthesis.

Synthesis cross-checks:

- For every catalog row with non-empty `bib_semantic_scholar_id`, every chunk for that doc_id in T3 should carry the same value. If they disagree, the doctor logs a `bib_drift` warning. Pre-RDR-101 drift is not actionable in synthesis; it is a known consequence of the duplication surface and is healed by Phase 5's strip-from-T3 step.
- For every T3 chunk carrying a non-empty `bib_semantic_scholar_id` whose catalog row has empty `bib_semantic_scholar_id`, synthesize a `DocumentEnriched` event sourcing the chunk-side value. This recovers cases where the catalog hook silently failed in `enrich.py:498` (`except Exception: _log.debug("catalog_enrich_hook_failed")`) and only the T3 side was updated.

### Phase 3 (New write path)

`enrich.py:enrich_bib` is rewritten so the per-title write path emits one `DocumentEnriched` event per resolved bib hit, instead of doing a per-chunk `col.update(metadatas=...)` followed by `_catalog_enrich_hook`. The event flows through the projector, which writes the bib_* columns onto the Document projection AND (during the deprecation window) writes the same values to T3 chunk metadata as a write-through cache. The dual-write is identical to what RDR-101 §Phase 3 already specifies for `source_path`/`title`/`git_*`: the deprecated T3 fields are still written so existing readers see live values, not stale pre-Phase-3 snapshots.

The projector's T3 write side is a single per-doc batched update, replacing the current per-title in-memory loop in `enrich.py:181-205`. The chunk-fetch logic (lines 102-125) that currently builds `title_to_ids` becomes unnecessary; the projector knows which chunks belong to which doc_id from the `ChunkIndexed` events.

### Phase 4 (Reader migration)

The skip-query in `enrich.py` is rewritten to read the catalog projection:

```python
# Before (lines 102-125 of today's enrich.py):
#   batch = col.get(include=["metadatas"], limit=300, offset=offset)
#   for chunk_id, meta in zip(batch_ids, batch_meta):
#       if meta.get(id_field, ""):
#           already_enriched += 1
#           continue
#       title = meta.get("title", "") or ""
#       ...
#
# After (Phase 4):
#   cat = Catalog(catalog_path(), catalog_path() / ".catalog.db")
#   coll_id = _coll_id_for(collection)
#   already_enriched_doc_ids = {
#       row.doc_id for row in cat._db.execute(
#           "SELECT doc_id FROM documents "
#           "WHERE coll_id = ? AND ((? = 's2' AND bib_semantic_scholar_id != '') "
#           "                      OR (? = 'openalex' AND bib_openalex_id != ''))",
#           (coll_id, backend, backend),
#       )
#   }
#   pending_docs = [
#       (row.doc_id, row.title, row.source_uri)
#       for row in cat.list_documents(coll_id=coll_id)
#       if row.doc_id not in already_enriched_doc_ids
#   ]
```

The chunk-fetch loop is retained only for the rows that need the per-chunk source-text DOI/arXiv extraction (`_resolve_bib_for_title` at line 258 of today's enrich.py reads chunk text via `col.get(ids=chunk_ids)`). That stays as a `get(where={doc_id: X})` join, identical to the Phase 4 aspect-extraction migration.

The citation-link generator at `src/nexus/catalog/link_generator.py:50-58` does not change. It already reads `entry.meta.get("bib_semantic_scholar_id")` from the catalog. Post-migration the value comes from the Document-projection column instead of the legacy `meta` JSON blob, which is a projection internal change invisible to `link_generator.py`. The `entry.meta` access is preserved by `CatalogEntry`'s field projection logic; the column read is wired through during the same refactor that adds the bib_* columns in Phase 1.

Telemetry: the Phase 4 `direct_t3_metadata_read_total{field}` counter (RDR-101 §RF-101-5 + Phase 4 deliverable) covers the bib_* fields automatically; they are part of the "deprecated T3 fields readers should migrate off of" set.

### Phase 5 (Remove deprecated surface)

`bib_semantic_scholar_id` (and the four sibling `bib_*` keys) are removed from `metadata_schema.py:ALLOWED_TOP_LEVEL` and from `make_chunk_metadata()`'s signature. The opt-in `nx t3 strip-deprecated-metadata --collection X` verb that Phase 5 already ships removes the deprecated keys from existing chunks. Gate: 30+ contiguous days of zero `direct_t3_metadata_read_total{field=bib_*}` reads, same gate Phase 5 uses for the other deprecated fields.

`enrich.py` no longer references `id_field` as a T3 metadata key; the skip-query is catalog-only. The Phase 3 dual-write path (the projector's T3-side write of bib_* during deprecation) is removed in the same Phase 5 PR, leaving the Document projection as the single home.

### Phase 6 (Enforcement)

`nx catalog doctor` validates that no T3 chunk in a non-grandfathered collection carries any `bib_*` key. Pre-RDR-101 grandfathered collections (RDR-101 §Phase 6 §"legacy_grandfathered = TRUE") are exempt from the check; they keep their bib_* fields on chunks until they are renamed via `nx catalog rename-collection` (which triggers a Chroma re-create + re-embed and naturally drops the deprecated keys).

## Candidate RDR-101 amendment

The block below is suitable for insertion into RDR-101 as a new resolved item under §"Resolved Open Questions" (numbered RF-101-6) or as a new bullet under §Phase 0 §"Field-by-field disposition audit." The RDR file itself is not modified by this deliverable; the amendment is the canonical proposal for the next RDR-101 revision.

> **RF-101-6 (Verified)**: `bib_semantic_scholar_id` and the four sibling `bib_*` keys (`bib_year`, `bib_authors`, `bib_venue`, `bib_citation_count`, plus `bib_openalex_id` and `bib_doi` for the OpenAlex backend) move from T3 chunk metadata to the Document projection (T2 SQLite). Bib enrichment is recorded as a `DocumentEnriched(doc_id, schema_version="bib-s2-v1" | "bib-openalex-v1", payload={...})` event; the projector writes the bib_* values onto Document-projection columns. `nx enrich bib`'s skip-already-enriched query becomes `SELECT doc_id FROM documents WHERE bib_<id_field> != '' AND coll_id = ?` (single SQLite query, indexed, sub-millisecond) instead of the current ChromaDB pagination over chunk metadata. The T3-side write is retained as a write-through projection cache through Phase 4 (parallel to `source_path`, `title`, `git_*`); Phase 5 strips the bib_* keys from T3 the same release as the other deprecated chunk fields, gated on the `direct_t3_metadata_read_total{field=bib_*}` counter showing 30+ contiguous days of zero direct-T3 reads. Phase 2 synthesis emits `DocumentEnriched` events for every catalog row carrying a non-empty bib backend ID; T3-only enrichment records (where the catalog hook silently failed) are captured by a parallel pass over T3 chunks. The citation-link generator at `src/nexus/catalog/link_generator.py` does not change; it already reads from the catalog row's meta. Counter-argument considered: leaving bib_* on T3 chunks under a `bib_*` prefix that Phase 5's removal sweep treats as a special case is simpler to migrate but reopens the per-document-fact-on-chunks duplication that Core Invariant 3 prohibits, and it forces the Phase 4 identity-field rewrite (substantive critique C2) to handle a permanent second special case alongside the `doc_id`-keyed dispatch it consolidates onto.

## Gaps surfaced

1. **Cross-backend dedup**. Today the skip-query is keyed by backend (`bib_semantic_scholar_id` for S2, `bib_openalex_id` for OpenAlex). A title enriched by S2 will be re-queried under OpenAlex, and vice versa. The current behavior is intentional (the two ID spaces are distinct, per `link_generator.py:30-38`), so the migration preserves it via two separate WHERE clauses keyed on `backend`. No change required, but worth noting: a future "any backend has hit" skip would need a coalesced cardinality column (`bib_enriched_at IS NOT NULL` on the Document projection) to avoid the per-backend OR.
2. **`references` tuple denormalization**. The bib enrichment payload includes a `references` list (`enrich.py:481-483` writes it to catalog meta when present). The amendment proposes putting it on the Document projection, but a normalized `Reference(from_doc, ref_id, backend)` table would be more queryable for citation-graph analytics. Out of scope for this disposition; flagged as a Phase 1 Document-vs-Reference-projection sub-decision when `events.py` is authored.
3. **Phase 2 catalog-vs-T3 disagreement**. The synthesis cross-check (catalog has bib value, T3 chunks do not, or vice versa) will surface the existing duplication drift. The disposition treats catalog as authoritative for synthesis but emits `bib_drift` doctor warnings on every disagreement. The expected count of disagreements on the live host catalog is unknown; a Phase 0 follow-up bead could run a read-only audit (`SELECT doc_id, source_uri, bib_semantic_scholar_id FROM documents WHERE bib_semantic_scholar_id != ''` cross-joined against `col.get(where={source_path: X})`) to size the cleanup before Phase 2 ships.
4. **`make_chunk_metadata()` factory churn during the deprecation window**. Phase 5 removes the bib_* keyword arguments from the factory. Every call site that passes them must be updated in the same PR. The bib enrichment write path is the only known caller that supplies non-default values (the indexer always passes the empty-string defaults), so the blast radius is tractable. Flagged for the Phase 5 implementer.
