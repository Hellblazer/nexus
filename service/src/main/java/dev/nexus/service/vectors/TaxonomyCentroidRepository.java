// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.db.TenantScope;
import org.jooq.Record;
import org.jooq.impl.DSL;
import org.jooq.Result;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/**
 * RDR-156 bead nexus-t1hnc.2 — pgvector taxonomy-centroid repository.
 *
 * <p>Service-backed replacement for the {@code taxonomy__centroids} ChromaDB collection the
 * oracle ({@code catalog_taxonomy.py}) assumed. Backs the centroid-ANN reads
 * ({@code assign_single} / {@code compute_assignments} / {@code compute_cross_links} /
 * {@code project_against}) and the {@code discover_topics} centroid upsert so service-mode
 * taxonomy compute is chroma-free (RDR-155 retires ChromaDB).
 *
 * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-jv3ue item 5): centroids are
 * now stored in ONE unified table, {@code nexus.taxonomy_centroids}, with three nullable
 * typed embedding columns (mirroring {@code nexus.chunks}'s own RDR-191 unification) —
 * formerly three per-dim tables ({@code nexus.taxonomy_centroids_384/768/1024}). Routing
 * is by EMBEDDING LENGTH, not by parsing a collection-name model segment: taxonomy
 * collection names are not four-segment conformant (RDR-075 uses
 * {@code <content_type>__<owner>} two-segment names), and the centroid vector itself is
 * the unambiguous dimension authority — dim now selects the {@code embedding_<dim>}
 * COLUMN via {@link DimTables#embeddingColumn(int)} / {@link DimTables#CENTROIDS}, not a
 * target table.
 *
 * <p>Collection-keyed maintenance ops ({@link #count}, {@link #getByCollection},
 * {@link #deleteByIds}, {@link #purgeByCollection}) carry no vector. Since every dim key
 * in {@link DimTables#CENTROIDS} now resolves to the SAME unified table instance, a
 * per-dim loop over that map no longer scopes by table membership — each iteration MUST
 * filter on {@code embedding_<dim> IS NOT NULL} (the RDR-191 D1 hazard: without that
 * filter, a loop over the same physical table N times triples/N-tuples counts or
 * duplicates rows, rather than genuinely visiting N disjoint physical stores as it did
 * pre-unification). A deployment is single-dim per RDR-075/077 (the chroma centroid
 * collection fixed its dimension on first write), so in practice only one dim's rows
 * exist per tenant; the per-dim-column filter is correct regardless and needs no
 * conformant name.
 *
 * <p>Tenant scoping is identical to {@link PgVectorRepository}: every operation runs inside
 * {@link TenantScope#withTenant} so the {@code nexus.tenant} GUC stamps the transaction and
 * FORCE RLS scopes every row. The centroid embeddings are PRECOMPUTED (HDBSCAN/c-TF-IDF
 * client-side) — this class does no embedding.
 */
public final class TaxonomyCentroidRepository {

    private static final Logger log = LoggerFactory.getLogger(TaxonomyCentroidRepository.class);

    /**
     * The supported centroid embedding dims — post-RDR-191 unification, each
     * selects the {@code embedding_<dim>} column of the ONE {@code
     * nexus.taxonomy_centroids} table, not a per-dim table.
     */
    private static final int[] DIMS = {384, 768, 1024};
    private static final Set<Integer> VALID_DIMS = Set.of(384, 768, 1024);

    /** A centroid row: precomputed cluster centroid keyed on (collection, topic_id). */
    public record CentroidRecord(String collection, long topicId, float[] embedding,
                                 String label, Integer docCount) {}

    /** An ANN hit: nearest topic + raw cosine similarity (1 - distance). */
    public record AnnHit(long topicId, double similarity) {}

    private final TenantScope tenantScope;

    public TaxonomyCentroidRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /**
     * Upsert centroids, routing each to the {@code embedding_<dim>} column of the unified
     * {@code nexus.taxonomy_centroids} table by the vector's length. Re-upserting an
     * existing {@code (tenant, collection, topic_id)} updates the embedding, label, and
     * doc_count in place (ON CONFLICT update — chroma upsert parity).
     *
     * <p><b>Dim transition (nexus-2qryr).</b> A re-upsert of the same key with a vector of
     * a DIFFERENT length — a re-embed under a new model — REPLACES the embedding: the
     * incoming dim's column is set and the other two are cleared in the same UPDATE, so
     * the row always satisfies {@code taxonomy_centroids_exactly_one_embedding}. Before
     * the tables were unified this silently landed the new vector in another shard and
     * orphaned the old row; after unification, without the clearing, it raised the CHECK
     * violation. "Fail loud" was considered and rejected: a re-embed is exactly the
     * legitimate upsert this method's contract promises, and a deployment is single-dim
     * (RDR-075/077), so a stranded old-dim vector would be unreachable dead data, not
     * information worth protecting. Pinned by
     * {@code TaxonomyCentroidRepositoryTest.upsert_dimTransition_replacesEmbedding}.
     *
     * @throws IllegalArgumentException if any embedding length is not 384/768/1024
     */
    public void upsertCentroids(String tenant, List<CentroidRecord> records) {
        if (records == null || records.isEmpty()) return;
        // Fail loud BEFORE any SQL if a vector has no per-dim table.
        for (CentroidRecord r : records) {
            int dim = r.embedding().length;
            if (!VALID_DIMS.contains(dim)) {
                throw new IllegalArgumentException(
                    "centroid for topic " + r.topicId() + " in collection '" + r.collection()
                    + "' is " + dim + "-dim — no taxonomy_centroids_<dim> table (valid: "
                    + VALID_DIMS + ")");
            }
        }
        tenantScope.withTenant(tenant, ctx -> {
            for (CentroidRecord r : records) {
                DimTables.CentroidTable ct = DimTables.CENTROIDS.get(r.embedding().length);
                var upsert = ctx.insertInto(ct.table())
                   .columns(ct.tenantId(), ct.collection(), ct.topicId(),
                            ct.embedding(), ct.label(), ct.docCount())
                   .values(tenant, r.collection(), r.topicId(),
                           Vector.of(r.embedding()), r.label(), r.docCount())
                   .onConflict(ct.tenantId(), ct.collection(), ct.topicId())
                   .doUpdate()
                   .set(ct.embedding(), DSL.excluded(ct.embedding()))
                   .set(ct.label(),     DSL.excluded(ct.label()))
                   .set(ct.docCount(),  DSL.excluded(ct.docCount()));
                // nexus-2qryr: clear the OTHER dim columns so a dim transition
                // replaces the embedding instead of tripping the exactly-one CHECK.
                for (DimTables.CentroidTable other : DimTables.CENTROIDS.values()) {
                    if (!other.embedding().getName().equals(ct.embedding().getName())) {
                        upsert = upsert.set(other.embedding(), (Vector) null);
                    }
                }
                upsert.execute();
            }
            return null;
        });
        log.debug("event=centroid_upsert_done count={}", records.size());
    }

    /**
     * Nearest-centroid ANN for one embedding, routed by the query vector's length.
     *
     * <p>Mirrors {@code assign_single}/{@code compute_assignments}: returns
     * {@code topic_id + similarity = 1 - cosine_distance}, ordered by distance ascending.
     * When {@code crossCollection} is false the search is scoped to {@code collection};
     * when true it queries FOREIGN centroids ({@code collection <> ?}) for cross-collection
     * projection (RDR-075 SC-6).
     *
     * @throws IllegalArgumentException if the embedding length is not 384/768/1024,
     *                                  or {@code nResults < 1}
     */
    public List<AnnHit> annQuery(String tenant, float[] embedding, String collection,
                                 boolean crossCollection, int nResults) {
        int dim = embedding.length;
        if (!VALID_DIMS.contains(dim)) {
            throw new IllegalArgumentException(
                "query embedding is " + dim + "-dim — no taxonomy_centroids_<dim> table");
        }
        if (nResults < 1) {
            throw new IllegalArgumentException("nResults must be >= 1, got " + nResults);
        }
        String op = crossCollection ? "<>" : "=";
        String embeddingCol = DimTables.embeddingColumn(dim);
        // SANCTIONED RAW (nexus-mzuj9): the pgvector `<=>` distance operator ordered directly
        // off a bind-parameter vector literal has no jOOQ DSL form (same category as
        // PgVectorRepository's search()/hybridSearch() — see that class's rawVectorFetch
        // javadoc). Registered in RawSqlGateTest's sanctioned method allowlist.
        //
        // RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-jv3ue item 5): table name
        // consulted from DimTables.CENTROIDS_TABLE_NAME (was a hand-rolled
        // "nexus.taxonomy_centroids_" + dim string that resolves to a table DROPped by
        // the unify changeset — a silent RUNTIME failure, not a compile error, since this
        // is plain string interpolation). Embedding column consulted from
        // DimTables.embeddingColumn(dim) rather than the former bare "embedding" (every
        // dim's rows now share the one physical table, so an unqualified "embedding"
        // column no longer exists at all post-unification). The explicit
        // "embedding_<dim> IS NOT NULL" guard is NEW and load-bearing: unlike
        // PgVectorRepository's searchWithTokens (dim-homogeneous by collection, D2's
        // hazard analysis), a centroid collection can legitimately hold rows at two
        // dims mid-migration (this class's own dimensionProbe javadoc) — without the
        // guard, ordering by "embedding_<dim> <=> ?::vector" would compute a NULL
        // distance for foreign-dim rows and additionally defeats the embedding_<dim>
        // partial-population correlation the HNSW index needs to be selective.
        Result<Record> result = tenantScope.withTenant(tenant, ctx -> {
            // Filtered-ANN recall: the collection predicate + RLS narrow the candidate set;
            // keep HNSW scanning past ef_search so a narrow collection returns its full set
            // (RDR-156 — without this, filtered HNSW silently under-returns). SET LOCAL is
            // txn-scoped, same pool discipline as the TenantScope GUC stamp.
            PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
            // nexus-4ktfm: crowd-out headroom for the traversal (see
            // PgSession.DEFAULT_EF_SEARCH_FLOOR) — centroid tables share the
            // same one-index-all-tenants + RLS-after-scan shape as chunks.
            PgSession.setHnswEfSearch(ctx, nResults);
            // nexus-g17tf: bound the statement so an orphaned or pathological
            // scan cancels (57014) instead of pinning xmin for hours.
            PgSession.setSearchStatementTimeout(ctx);
            // nexus-6nkn3: a custom plan per execution so the planner sees the
            // collection set's selectivity (a cached generic HNSW plan on a tiny
            // collection ran ~30s and returned EMPTY in production).
            PgSession.setSearchPlanCacheMode(ctx);
            return ctx.fetch(
                "SELECT topic_id, (" + embeddingCol + " <=> ?::vector) AS distance FROM "
                + centroidTable(dim)
                + " WHERE " + embeddingCol + " IS NOT NULL AND collection " + op + " ?"
                + " ORDER BY distance ASC, topic_id ASC LIMIT ?",
                vectorLiteral(embedding), collection, nResults);
        });
        List<AnnHit> hits = new ArrayList<>(result.size());
        for (Record rec : result) {
            double distance = rec.get("distance", Double.class);
            hits.add(new AnnHit(rec.get("topic_id", Long.class), 1.0 - distance));
        }
        return hits;
    }

    /**
     * Count centroids for {@code collection} (or all, when {@code collection} is null)
     * visible to {@code tenant}.
     *
     * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-jv3ue item 5): was a
     * three-iteration loop over {@link DimTables#CENTROIDS}, SUMMING a count query — a
     * genuine D1-hazard correctness bug post-unification (every dim key now resolves to
     * the SAME physical table, so the loop TRIPLE-COUNTED every row rather than visiting
     * three disjoint stores). Collapsed to one count against the unified table; any dim
     * key works as the representative accessor since {@code .table()}/{@code
     * .collection()} are identical across dims and a row's dim does not affect whether
     * it should be counted here (this method counts centroids, not centroids-at-a-dim).
     */
    public int count(String tenant, String collection) {
        DimTables.CentroidTable ct = DimTables.CENTROIDS.get(384);
        long total = tenantScope.withTenant(tenant, ctx -> collection != null
            ? ctx.fetchCount(ct.table(), ct.collection().eq(collection))
            : ctx.fetchCount(ct.table()));
        if (total > Integer.MAX_VALUE) {
            throw new IllegalStateException("centroid count overflow: " + total);
        }
        return (int) total;
    }

    /**
     * The dimension of the centroid table that holds rows for {@code tenant}, or
     * {@code -1} when the tenant has no centroids. Mirrors the oracle's
     * {@code _check_centroid_dimension} probe: a deployment is single-dim, so this
     * resolves the active centroid space for collection-keyed ops that have no vector.
     *
     * <p>SINGLE-DIM INVARIANT (RDR-156 t1hnc Phase-1 review S2): this returns the FIRST
     * non-empty table in ascending dim order. If a tenant ever has centroids in two
     * dimensions at once — only reachable mid-migration during a model switch
     * (e.g. MiniLM-384 -> Voyage-1024) — this reports the smaller dim as the tenant's
     * active space, which is wrong for that transient window. {@link #count} is NOT
     * affected (nexus-evqoc, RDR-191 Phase 4 correction of this paragraph's stale claim):
     * post-unification it runs one dim-agnostic query against the shared table and no
     * longer sums per dim, so a two-dim overlap cannot make it over-count. The invariant
     * the post-RDR-155 mode-switch migration MUST hold:
     * {@link #purgeByCollection} the old-dimension centroids BEFORE
     * {@link #upsertCentroids} at the new dimension. A doctor-level
     * "at most one centroid dim per tenant" check is tracked as a follow-on, not built
     * here (the storage primitive is single-dim by contract; enforcing the migration
     * ordering belongs to the migration tool).
     *
     * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-jv3ue item 5): a bare
     * {@code ctx.fetchExists(ct.table())} used to mean "does chunks_&lt;dim&gt; have any
     * row for this tenant" — table membership WAS the dim signal. Post-unification every
     * dim key resolves to the SAME table, so an unguarded existence check would ALWAYS
     * report dim 384 (the first entry) for any tenant with ANY centroid, regardless of
     * its actual dim — a genuine D1-hazard correctness bug, not merely wasted round
     * trips. Fixed with an explicit {@code embedding_<dim> IS NOT NULL} predicate so each
     * iteration checks its own dim's population.
     */
    public int dimensionProbe(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            for (int dim : DIMS) {
                DimTables.CentroidTable ct = DimTables.CENTROIDS.get(dim);
                if (ctx.fetchExists(ctx.selectOne().from(ct.table())
                        .where(ct.embedding().isNotNull()))) {
                    return dim;
                }
            }
            return -1;
        });
    }

    /**
     * All centroids for {@code collection} visible to {@code tenant}, across all per-dim
     * tables, ordered by topic_id. Mirrors the {@code _paginated_get} embeddings+metadatas
     * shape the rebuild/project paths index into.
     */
    public List<CentroidRecord> getByCollection(String tenant, String collection) {
        return fetchCentroids(tenant, ct -> ct.collection().eq(collection));
    }

    /**
     * All centroids in collections OTHER than {@code collection} (cross-collection
     * projection source set), across all per-dim tables, ordered by (collection, topic_id).
     *
     * <p>Serves the oracle's two bulk centroid reads (RDR-156 t1hnc Phase-1 review S1):
     * {@code compute_cross_links} ({@code where collection $ne name}) directly, and
     * {@code project_against} ({@code where collection $in targets}) by client-side
     * filtering this foreign set to the target collections (the projection matrix multiply
     * already filters). The {@code $ne} super-set is sufficient for both; a dedicated
     * {@code $in} endpoint was not added (YAGNI — no third caller).
     *
     * <p>Each row carries its own {@code collection} so the caller can group/filter; the
     * embedding is hydrated like {@link #getByCollection}.
     */
    public List<CentroidRecord> getForeignCentroids(String tenant, String collection) {
        return fetchCentroids(tenant, ct -> ct.collection().ne(collection));
    }

    /**
     * Shared per-dim centroid fetch with a typed collection predicate.
     *
     * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-jv3ue item 5): every dim key
     * in {@link DimTables#CENTROIDS} now resolves to the SAME unified table, so a
     * per-dim loop without an {@code embedding_<dim> IS NOT NULL} guard would fetch and
     * return each matching row THREE TIMES (once per dim iteration) — a genuine D1-hazard
     * correctness bug (silent row duplication), not merely wasted round trips. Added the
     * guard so each iteration visits only the rows actually populated at that dim.
     */
    private List<CentroidRecord> fetchCentroids(
            String tenant,
            java.util.function.Function<DimTables.CentroidTable, org.jooq.Condition> predicate) {
        return tenantScope.withTenant(tenant, ctx -> {
            List<CentroidRecord> out = new ArrayList<>();
            for (int dim : DIMS) {
                DimTables.CentroidTable ct = DimTables.CENTROIDS.get(dim);
                var rows = ctx.select(ct.collection(), ct.topicId(), ct.embedding(),
                                      ct.label(), ct.docCount())
                              .from(ct.table())
                              .where(predicate.apply(ct).and(ct.embedding().isNotNull()))
                              .orderBy(ct.collection().asc(), ct.topicId().asc())
                              .fetch();
                for (var rec : rows) {
                    Vector v = rec.value3();
                    out.add(new CentroidRecord(
                        rec.value1(),
                        rec.value2(),
                        v != null ? v.floats() : new float[0],
                        rec.value4(),
                        rec.value5()));
                }
            }
            return out;
        });
    }

    /**
     * Delete centroids by topic_id within {@code collection}, across all per-dim tables.
     * Mirrors the rebuild path's {@code centroid_coll.delete}.
     *
     * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-jv3ue item 5): unlike {@link
     * #count}/{@link #dimensionProbe}/{@link #fetchCentroids}, a DELETE loop over the
     * unified table's three dim keys is net-correct WITHOUT an {@code embedding_<dim> IS
     * NOT NULL} guard — the first matching iteration deletes every row regardless of its
     * dim, so later iterations always find zero rows left and contribute zero. Added the
     * guard anyway for uniformity with the rest of this class and to stop the net
     * correctness depending on iteration ORDER (a future re-order, e.g. to parallelize
     * these three no-longer-independent deletes, would silently double-delete or race
     * without it — each iteration is now independently correct rather than
     * correct-by-accident).
     *
     * @return number of rows actually deleted (RLS makes other tenants' rows invisible)
     */
    public int deleteByIds(String tenant, String collection, List<Long> topicIds) {
        if (topicIds == null || topicIds.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            int deleted = 0;
            for (int dim : DIMS) {
                DimTables.CentroidTable ct = DimTables.CENTROIDS.get(dim);
                deleted += ctx.deleteFrom(ct.table())
                              .where(ct.collection().eq(collection)
                                  .and(ct.topicId().in(topicIds))
                                  .and(ct.embedding().isNotNull()))
                              .execute();
            }
            return deleted;
        });
    }

    /**
     * Remove every centroid for {@code collection}, across all per-dim tables.
     *
     * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-jv3ue item 5): same
     * per-iteration {@code embedding_<dim> IS NOT NULL} guard added as {@link
     * #deleteByIds}, same reasoning (net-correct without it, but only by relying on
     * execution order).
     *
     * @return number of rows deleted
     */
    public int purgeByCollection(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            int deleted = 0;
            for (int dim : DIMS) {
                DimTables.CentroidTable ct = DimTables.CENTROIDS.get(dim);
                deleted += ctx.deleteFrom(ct.table())
                              .where(ct.collection().eq(collection)
                                  .and(ct.embedding().isNotNull()))
                              .execute();
            }
            return deleted;
        });
    }

    // ── Internal helpers ────────────────────────────────────────────────────────

    /**
     * RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-jv3ue item 5): the
     * raw-string table-name channel now consults {@link
     * DimTables#CENTROIDS_TABLE_NAME} — {@code dim} stays a parameter for
     * call-site symmetry with {@link DimTables#embeddingColumn(int)} (every
     * caller already has {@code dim} in scope for the column choice right
     * alongside the table name), even though the table name itself no
     * longer varies by dim post-unification. Was {@code
     * "nexus.taxonomy_centroids_" + dim}, a hand-rolled string resolving to
     * a table the unify changeset DROPs.
     */
    private static String centroidTable(int dim) {
        return DimTables.CENTROIDS_TABLE_NAME;
    }

    /** pgvector cast-safe text literal: {@code [f1,f2,...]}. */
    private static String vectorLiteral(float[] vec) {
        StringBuilder sb = new StringBuilder(vec.length * 8 + 2).append('[');
        for (int i = 0; i < vec.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(vec[i]);
        }
        return sb.append(']').toString();
    }

}
