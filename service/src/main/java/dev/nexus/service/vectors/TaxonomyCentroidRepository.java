// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.CollectionRow;
import dev.nexus.service.db.ModelPartitions;
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

import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_ANN_QUERY_1024;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_ANN_QUERY_384;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_ANN_QUERY_768;

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
     * Upsert centroids into the {@code embedding_<dim>} column of the unified
     * {@code nexus.taxonomy_centroids} table. Re-upserting an existing
     * {@code (tenant, collection, topic_id)} updates the embedding, label, and doc_count in place
     * (ON CONFLICT update, chroma upsert parity).
     *
     * <p><b>Model and partition (RDR-225, nexus-3wh8d.13).</b> The table is LIST-partitioned by
     * {@code embedding_model}, then by tenant. Each centroid carries the model of its collection's
     * {@code catalog_collections} row (an unregistered collection is refused with a 422, since there
     * is no model to file it under), the primary key is {@code (tenant, collection, topic_id, model)},
     * and the conflict target names all four. A model with no partition is refused before any SQL,
     * naming the model. A vector whose length is not the model's dimension is refused before any SQL
     * too; the partition's CHECK would refuse it anyway, with a message that names neither.
     *
     * <p><b>Dim transition (nexus-2qryr).</b> A centroid whose dimension changes is a re-embed under
     * a new model: the collection was re-registered, so its model differs from the model of the rows
     * already stored. An upsert cannot move a row across partitions, so the stored row of the same
     * {@code (tenant, collection, topic_id)} under any OTHER model is deleted and the new one
     * inserted, in the same transaction. Before the tables were partitioned this was a column swap
     * inside one row (the other dimension columns cleared in the same UPDATE); a re-embed is still
     * exactly the legitimate upsert this method's contract promises, and a stranded old-dim vector
     * would be unreachable dead data. Pinned by
     * {@code TaxonomyCentroidRepositoryTest.upsert_dimTransition_replacesEmbedding}.
     *
     * @throws IllegalArgumentException if any embedding length is not 384/768/1024, or is not the
     *         dimension of its collection's model
     * @throws dev.nexus.service.db.UnregisteredCollectionException if a record's collection has no
     *         {@code catalog_collections} row
     * @throws dev.nexus.service.db.ModelPartitions.ModelPartitionMissingException if a collection's
     *         model has no partition of {@code nexus.taxonomy_centroids}
     */
    public void upsertCentroids(String tenant, List<CentroidRecord> records) {
        if (records == null || records.isEmpty()) return;
        // Fail loud BEFORE any SQL if a vector has no per-dim column.
        for (CentroidRecord r : records) {
            int dim = r.embedding().length;
            if (!VALID_DIMS.contains(dim)) {
                throw new IllegalArgumentException(
                    "centroid for topic " + r.topicId() + " in collection '" + r.collection()
                    + "' is " + dim + "-dim — no taxonomy_centroids embedding column (valid: "
                    + VALID_DIMS + ")");
            }
        }
        tenantScope.withTenant(tenant, ctx -> {
            for (CentroidRecord r : records) {
                int dim = r.embedding().length;
                // RDR-225: the collection's registry row is the authority for the model; the vector's
                // length must agree with it, and the model must have a partition, before any write.
                CollectionRow collectionRow = CollectionRegistry.require(ctx, tenant, r.collection());
                String model = collectionRow.embeddingModel();
                if (dim != collectionRow.dimension()) {
                    throw new IllegalArgumentException(
                        "centroid for topic " + r.topicId() + " in collection '" + r.collection()
                        + "' is " + dim + "-dim but the collection's embedding model '" + model
                        + "' is " + collectionRow.dimension() + "-dim");
                }
                ModelPartitions.require(ctx, ModelPartitions.CENTROIDS, model);
                DimTables.CentroidTable ct = DimTables.CENTROIDS.get(dim);
                // nexus-2qryr, RDR-225: a re-embed under a new model deletes the row stored under the
                // old one (an upsert cannot move a row across partitions) before the new one is written.
                ctx.deleteFrom(ct.table())
                   .where(ct.tenantId().eq(tenant)
                       .and(ct.collection().eq(r.collection()))
                       .and(ct.topicId().eq(r.topicId()))
                       .and(ct.embeddingModel().ne(model)))
                   .execute();
                ctx.insertInto(ct.table())
                   .columns(ct.tenantId(), ct.collection(), ct.topicId(), ct.embeddingModel(),
                            ct.embedding(), ct.label(), ct.docCount())
                   .values(tenant, r.collection(), r.topicId(), model,
                           Vector.of(r.embedding()), r.label(), r.docCount())
                   .onConflict(ct.tenantId(), ct.collection(), ct.topicId(), ct.embeddingModel())
                   .doUpdate()
                   .set(ct.embedding(), DSL.excluded(ct.embedding()))
                   .set(ct.label(),     DSL.excluded(ct.label()))
                   .set(ct.docCount(),  DSL.excluded(ct.docCount()))
                   .execute();
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
        // nexus-zrcj7 (Sam's no-SQL-strings-in-Java directive, step 4): retired the
        // former string-concatenated raw SQL (pgvector `<=>` has no jOOQ typed-DSL
        // form) onto nexus.taxonomy_ann_query_<dim> (vectors-013), an inlinable schema
        // function mirroring PgVectorRepository's plain_search_<dim> precedent
        // (vectors-009) — same "move the whole query server-side" resolution rather
        // than a DSL.field/DSL.condition raw-text template. The embedding_<dim> IS NOT
        // NULL guard and the crossCollection ("=" vs "<>") comparator selection both
        // moved into the function body (a CASE expression, never Java string
        // concatenation) — see the changeset's own header for the full derivation of
        // why both are load-bearing (a centroid collection can hold rows at two dims
        // mid-migration; this class's own dimensionProbe javadoc).
        Vector queryVec = Vector.of(embedding);
        // RDR-225: the function reads the one (model, tenant) leaf of taxonomy_centroids. The model is the
        // SOURCE collection's, also in the cross-collection branch: a centroid of another model is not a
        // candidate (the same rule assign_from_chashes and cross_preview apply).
        // An unregistered collection can hold no centroid (every centroid write needs the registration) and has
        // no model to match, so the answer is none. That is unchanged for a same-collection query (v0.1.149 never
        // looked the registry up and found no centroid either). For a cross-collection query it is a change: an
        // unregistered source used to get other collections' centroids of the same width, and now gets none (a
        // wire-ledger entry records it).
        String model;
        try {
            model = CollectionRegistry.lookup(tenantScope, tenant, collection).embeddingModel();
        } catch (dev.nexus.service.db.UnregisteredCollectionException e) {
            return List.of();
        }
        org.jooq.Table<?> fn = switch (dim) {
            case 384  -> TAXONOMY_ANN_QUERY_384.call(queryVec, collection, crossCollection, nResults, model, tenant);
            case 768  -> TAXONOMY_ANN_QUERY_768.call(queryVec, collection, crossCollection, nResults, model, tenant);
            case 1024 -> TAXONOMY_ANN_QUERY_1024.call(queryVec, collection, crossCollection, nResults, model, tenant);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
        Result<? extends Record> result = tenantScope.withTenant(tenant, ctx -> {
            // nexus-g17tf: bound the statement so an orphaned or pathological
            // scan cancels (57014) instead of pinning xmin for hours. First, so the
            // GUC round trips below run under its network bound too (nexus-u9zkn).
            PgSession.setSearchStatementTimeout(ctx);
            // Filtered-ANN recall: the collection predicate + RLS narrow the candidate set;
            // keep HNSW scanning past ef_search so a narrow collection returns its full set
            // (RDR-156 — without this, filtered HNSW silently under-returns). SET LOCAL is
            // txn-scoped, same pool discipline as the TenantScope GUC stamp.
            PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
            // nexus-4ktfm: crowd-out headroom for the traversal (see
            // PgSession.DEFAULT_EF_SEARCH_FLOOR) — centroid tables share the
            // same one-index-all-tenants + RLS-after-scan shape as chunks.
            PgSession.setHnswEfSearch(ctx, nResults);
            // nexus-wbfpw.47: the shared serving scan budget. For centroids this is crowd-out
            // headroom (no liveness predicate here): a same-collection query is a selective
            // filter on the unified table and a collection with fewer centroids than nResults
            // would otherwise exhaust the default cap. A no-op below 20000 centroid rows.
            PgSession.setHnswScanBudget(ctx);
            // nexus-6nkn3: a custom plan per execution so the planner sees the
            // collection set's selectivity (a cached generic HNSW plan on a tiny
            // collection ran ~30s and returned EMPTY in production).
            PgSession.setSearchPlanCacheMode(ctx);
            return ctx.selectFrom(fn).fetch();
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

    // centroidTable(int)/vectorLiteral(float[]): REMOVED (nexus-zrcj7, step 4). Both
    // existed solely to serve annQuery's former string-concatenated raw SQL (the
    // table-name text and the pgvector cast-safe literal respectively); annQuery now
    // reads through nexus.taxonomy_ann_query_<dim> (vectors-013) via the typed
    // Vector/VectorBinding path (Vector.of(embedding) passed straight to the generated
    // function's Vector-bound parameter), so neither has a remaining caller. Per the
    // dead-entry-avoidance discipline this bead already established elsewhere
    // (RawSqlGateTest's own RekeyOps.java removal), a helper with no remaining caller
    // is deleted outright, not kept as a no-op.

}
