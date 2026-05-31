# Proposal: Applying "Beyond Similarity Search" to Nexus

**Status:** draft — proposal only, no code or doc changes yet
**Author:** synthesis of /nx:research run 2026-05-17
**Source paper:** Budigi & Sirigiri, *Beyond Similarity Search: A Unified Data Layer for Production RAG Systems*, arXiv:2605.03275 (2026)
**Indexed at:** `knowledge__dt-papers__voyage-context-3__v1`, tumbler `1.12.1`
**Research finding:** T3 doc `24c5da9677eb700f93ecb2772d2f76b9` (`knowledge__knowledge__voyage-context-3__v1`)
**Related bead:** nexus-1714 (P0 doctor check)
**Related fix:** PR #835 / nexus-m8a7 (manifest-chash reader)

This document is a **proposal**, not an in-place edit of `docs/architecture.md` or any existing RDR. If the proposal is accepted, the inline edits and any new RDR are tracked as separate work.

---

## 1. What the paper claims

The paper's thesis: split-tier RAG architectures (specialised vector store + relational metadata store + cache) suffer three structural failure modes:

1. **Data staleness** — inconsistency window between the vector store and the metadata store. Paper measures 3.54 ms mean window on a simulated split stack.
2. **Tenant data leakage** — application-layer filtering bugs leak rows across tenants (0.2 % leakage rate on their bench).
3. **Query composition explosion** — multi-constraint queries (similarity + columnar filter + ACL) require multiple round-trips and ~1 800 lines of synchronisation glue.

Proposed remedy: a unified PostgreSQL layer with pgvector + HNSW + row-level security, where similarity, filter, and access control execute in one SQL statement and one transaction. Fallback for extreme scale: a hot (PG+pgvector) / warm (specialised vector DB) / cold (object store) three-tier hybrid.

Reported results (synthetic 50 k-doc, 128-dim, 20-tenant corpus): 92 % latency reduction on date-filtered queries, 74 % on tenant-scoped queries, 0 ms inconsistency window, 0 % leakage, ~93 % less glue code.

## 2. Where it applies to nexus

### 2.1 Split-tier inconsistency — APPLIES

Nexus is a split-tier RAG stack. T2 SQLite (catalog + manifests + aspects) and T3 ChromaDB (chunks) are not in one transaction. The post-store hook fires after T3 returns, leaving T2 as the lagging side for one request.

**The nexus-m8a7 bug (PR #835) is the canonical instance of the paper's diagnosis.** RDR-108 Phase 3 retired `doc_id` from T3 chunk metadata, making the T2 manifest authoritative. The aspect reader had not been updated and queried `where={doc_id: ...}` on T3, returning zero rows even though the manifest knew the exact chashes. Two systems' identity models drifted. The fix was to make the reader respect the manifest as the source of truth.

This is exactly the failure class the paper diagnoses, with one twist: nexus already has the **structural** fix (manifest-first contract, RDR-108). What it lacks is universal enforcement.

### 2.2 Residual exposure — tolerance branches

Code that still reads identity fields from T3 chunk metadata as fallback:

| Location | Field | Purpose |
|---|---|---|
| `src/nexus/mcp/core.py:740` | `source_path` | display label fallback |
| `src/nexus/mcp/core.py:1004,1008,1038,1040` | `doc_id`, `source_path` | tolerance read with explanatory comment |
| `src/nexus/indexer_utils.py:275,369–372` | `source_path` | `StalenessCache.by_source_path` secondary index, explicit pre-Phase-3 path |
| `src/nexus/search_engine.py:189–207` | `doc_id` | metadata-first, catalog-fallback ordering |

These are deliberate, documented tolerance paths for chunks predating Phase 3. They are not bugs. But they are **live split-identity dependencies with no expiry condition**: until operators can prove the legacy corpus is fully pruned, the branches stay forever, and the failure class nexus-m8a7 surfaced remains reachable for any future contributor who adds a similar read.

The P0 recommendation (§4) is to gate removal of those branches on an operator-visible doctor check.

### 2.3 GC freshness gap

`src/nexus/indexer.py:1778–1782` documents an explicit split: the user-facing `nx t3 gc` CLI verb uses the legacy `meta.doc_id`-keyed path and "reports zero candidates for post-Phase-3 chunks." The manifest-keyed `_gc_orphan_chunks()` is the authoritative post-Phase-3 path but isn't wired to the CLI (bead nexus-e5aw, already filed).

Until that's reconciled, T3 orphans from re-indexing accumulate invisibly to operators — the paper's "stale data" failure mode at the GC layer.

### 2.4 Catalog-aware pre-filtering — APPLIES (already done)

`src/nexus/search_engine.py:267–308` (`_build_doc_id_prefilter`) queries T2 SQLite for matching doc_ids and injects them as a ChromaDB `where {"doc_id": {"$in": [...]}}` filter. This is the "filter before similarity" pattern the paper advocates as the unified-architecture's win. Nexus already does it.

The remaining gap: the join key inside T3 is still `doc_id` chunk metadata, not manifest-derived chash ids. Closing that gap is one of the residual-exposure items in §2.2.

## 3. Where it does NOT apply to nexus

| Paper claim | Why inapplicable |
|---|---|
| Multi-tenancy + RLS | Nexus is single-user. No tenant boundaries to leak across. |
| Query composition explosion | ChromaDB quota caps `where` predicates at 8 (`chroma_quotas.py:MAX_WHERE_PREDICATES`). High-cardinality filter explosion isn't a workload nexus faces. |
| ~1 800 LOC of glue | Paper measures multi-service synchronisation cost (Pinecone + Postgres + Redis + custom sync code). Nexus's post-store hook is in-process and small. |
| 50 k-doc "enterprise scale" | Nexus has ~290 k chunks (per T2 memory `project_t3_metadata_audit_2026_05_08`), larger in absolute terms but with no concurrent tenant pressure or per-read RLS. Access pattern differs. |

## 4. Recommendations

| Priority | Recommendation | Confidence | Bead / Next step |
|---|---|---|---|
| **P0** | `nx doctor` check: per-collection count of chunks carrying `doc_id` or `source_path` in T3 metadata. Non-zero warns; gate removal of §2.2 tolerance branches on the check reporting 0 across all collections. | HIGH | **nexus-1714** (filed) |
| P1 | Wire `nx t3 gc` CLI to the manifest-keyed GC path. Operators are currently blind to post-Phase-3 orphans. | MEDIUM | **nexus-e5aw** (filed; unblock) |
| P2 | Add `docs/architecture.md` section: "Cross-tier write ordering and recovery." Document the actual inconsistency window (post-store hook, same-process, sub-millisecond), the recovery path (re-index), and the manifest-first invariant. Cite paper §5.3 as external validation. | MEDIUM | doc-only |
| P3 | Name the hot / warm / cold framing in `docs/architecture.md` and `CLAUDE.md`. Cite RDR-105 (T1 contract) and paper §7.2. External validation at zero cost. | LOW | doc-only |

The P0 + P1 pair has a natural sequence: P0 surfaces the data, P1 closes the GC loop, and only then do the §2.2 tolerance branches become removable.

## 5. Skepticism about the paper

The structural arguments in the paper are sound engineering reasoning. **The numbers are not portable.** Calibrate any quotation from §6 / §7 with these caveats:

- **Synthetic corpus.** 50 k documents, 128-dim embeddings, 20 tenants, 5 categories. Voyage embeddings in nexus are 1 024-dim. HNSW recall, index build time, and query latency all scale differently in higher dimensions; the 92 % and 74 % latency-reduction figures do not transfer to nexus's embedding space.
- **No ablations.** `ablations_present: false` per the extracted aspect record (tumbler 1.12.1). The 92 % reduction is a single-condition comparison.
- **Simulated vector DB.** The "specialised vector store" baseline is simulated as a separate ChromaDB HTTP service, not a real Pinecone / Qdrant / Weaviate deployment. Proprietary HNSW optimisations in production vector DBs are not characterised.
- **No code released, zero external citations.** OpenAlex `W7160446511` reports `cited_by_count: 0` and an empty `referenced_works` list as of 2026-05-17. Fresh arXiv preprint, not peer-reviewed.
- **Future-dated.** Publication year 2026; nexus indexed it within days of upload. Treat conclusions as directional, not validated.

Use the paper for **architectural framing** (split-tier failure modes are real; manifest-as-source-of-truth is correct; hot/warm/cold is a sensible tiering pattern). Do not cite it for **quantitative claims** about latency or consistency without your own benchmark.

## 6. Decision asked

Three decisions, separable:

1. **Accept P0 (nexus-1714) as the next concrete action?** — yes / no / modify.
2. **Promote this proposal to an RDR?** The §2 findings touch RDR-108 enforcement and RDR-101 reranking and could warrant a dedicated RDR if the §4 P2/P3 doc edits land. — yes / no / defer.
3. **Persist the research finding (already in T3 doc `24c5da9677eb700f93ecb2772d2f76b9`) as a permanent reference, or let it age out?** — keep / age-out.

No code changes from this document. The accepted recommendations become beads; bead work happens on its own branches.
