// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.TenantScope;
import org.jooq.Record;
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
 * <p>Centroids are stored across three per-dim tables —
 * {@code nexus.taxonomy_centroids_384/768/1024} — mirroring the {@code chunks_<dim>}
 * convention. Unlike {@link PgVectorRepository}, routing is by EMBEDDING LENGTH, not by
 * parsing a collection-name model segment: taxonomy collection names are not four-segment
 * conformant (RDR-075 uses {@code <content_type>__<owner>} two-segment names), and the
 * centroid vector itself is the unambiguous dimension authority.
 *
 * <p>Collection-keyed maintenance ops ({@link #count}, {@link #getByCollection},
 * {@link #deleteByIds}, {@link #purgeByCollection}) carry no vector, so they span all three
 * per-dim tables. A deployment is single-dim per RDR-075/077 (the chroma centroid collection
 * fixed its dimension on first write), so in practice only one table is non-empty; spanning
 * all three is correct regardless and needs no conformant name.
 *
 * <p>Tenant scoping is identical to {@link PgVectorRepository}: every operation runs inside
 * {@link TenantScope#withTenant} so the {@code nexus.tenant} GUC stamps the transaction and
 * FORCE RLS scopes every row. The centroid embeddings are PRECOMPUTED (HDBSCAN/c-TF-IDF
 * client-side) — this class does no embedding.
 */
public final class TaxonomyCentroidRepository {

    private static final Logger log = LoggerFactory.getLogger(TaxonomyCentroidRepository.class);

    /** The per-dim centroid tables, mirroring chunks_384/768/1024. */
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
     * Upsert centroids, routing each to {@code taxonomy_centroids_<dim>} by the vector's
     * length. Re-upserting an existing {@code (tenant, collection, topic_id)} updates the
     * embedding, label, and doc_count in place (ON CONFLICT update — chroma upsert parity).
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
                String table = centroidTable(r.embedding().length);
                ctx.execute(
                    "INSERT INTO " + table
                    + " (tenant_id, collection, topic_id, embedding, label, doc_count)"
                    + " VALUES (?, ?, ?, ?::vector, ?, ?)"
                    + " ON CONFLICT (tenant_id, collection, topic_id) DO UPDATE SET"
                    + "   embedding = EXCLUDED.embedding,"
                    + "   label     = EXCLUDED.label,"
                    + "   doc_count = EXCLUDED.doc_count",
                    tenant, r.collection(), r.topicId(), vectorLiteral(r.embedding()),
                    r.label(), r.docCount());
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
        Result<Record> result = tenantScope.withTenant(tenant, ctx -> {
            // Filtered-ANN recall: the collection predicate + RLS narrow the candidate set;
            // keep HNSW scanning past ef_search so a narrow collection returns its full set
            // (RDR-156 — without this, filtered HNSW silently under-returns). SET LOCAL is
            // txn-scoped, same pool discipline as the TenantScope GUC stamp.
            ctx.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'");
            return ctx.fetch(
                "SELECT topic_id, (embedding <=> ?::vector) AS distance FROM " + centroidTable(dim)
                + " WHERE collection " + op + " ?"
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
     * visible to {@code tenant}, summed across all per-dim tables.
     */
    public int count(String tenant, String collection) {
        long total = tenantScope.withTenant(tenant, ctx -> {
            long sum = 0;
            for (int dim : DIMS) {
                String sql = "SELECT count(*) FROM " + centroidTable(dim)
                    + (collection != null ? " WHERE collection = ?" : "");
                Record rec = collection != null
                    ? ctx.fetchOne(sql, collection)
                    : ctx.fetchOne(sql);
                sum += rec.get(0, Long.class);
            }
            return sum;
        });
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
     */
    public int dimensionProbe(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            for (int dim : DIMS) {
                Record rec = ctx.fetchOne("SELECT 1 FROM " + centroidTable(dim) + " LIMIT 1");
                if (rec != null) return dim;
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
        return tenantScope.withTenant(tenant, ctx -> {
            List<CentroidRecord> out = new ArrayList<>();
            for (int dim : DIMS) {
                Result<Record> rows = ctx.fetch(
                    "SELECT topic_id, embedding::text AS embedding_text, label, doc_count FROM "
                    + centroidTable(dim) + " WHERE collection = ? ORDER BY topic_id ASC",
                    collection);
                for (Record rec : rows) {
                    out.add(new CentroidRecord(
                        collection,
                        rec.get("topic_id", Long.class),
                        parseVectorLiteral(rec.get("embedding_text", String.class)),
                        rec.get("label", String.class),
                        rec.get("doc_count", Integer.class)));
                }
            }
            return out;
        });
    }

    /**
     * Delete centroids by topic_id within {@code collection}, across all per-dim tables.
     * Mirrors the rebuild path's {@code centroid_coll.delete}.
     *
     * @return number of rows actually deleted (RLS makes other tenants' rows invisible)
     */
    public int deleteByIds(String tenant, String collection, List<Long> topicIds) {
        if (topicIds == null || topicIds.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            int deleted = 0;
            for (int dim : DIMS) {
                List<Object> binds = new ArrayList<>();
                binds.add(collection);
                binds.addAll(topicIds);
                deleted += ctx.execute(
                    "DELETE FROM " + centroidTable(dim)
                    + " WHERE collection = ? AND topic_id IN (" + placeholders(topicIds.size()) + ")",
                    binds.toArray());
            }
            return deleted;
        });
    }

    /**
     * Remove every centroid for {@code collection}, across all per-dim tables.
     *
     * @return number of rows deleted
     */
    public int purgeByCollection(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            int deleted = 0;
            for (int dim : DIMS) {
                deleted += ctx.execute(
                    "DELETE FROM " + centroidTable(dim) + " WHERE collection = ?", collection);
            }
            return deleted;
        });
    }

    // ── Internal helpers ────────────────────────────────────────────────────────

    private static String centroidTable(int dim) {
        return "nexus.taxonomy_centroids_" + dim;
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

    /** Parse a pgvector text literal {@code "[0.1,0.2,...]"} into a float array. */
    private static float[] parseVectorLiteral(String literal) {
        if (literal == null || literal.length() < 2) {
            // Schema says NOT NULL; a null/short literal means a malformed row.
            throw new IllegalStateException("malformed centroid embedding literal: " + literal);
        }
        String body = literal.substring(1, literal.length() - 1);
        if (body.isBlank()) return new float[0];
        String[] parts = body.split(",");
        float[] out = new float[parts.length];
        for (int i = 0; i < parts.length; i++) {
            out[i] = Float.parseFloat(parts[i].trim());
        }
        return out;
    }

    /** {@code IN}-list placeholder string: {@code ?,?,...} (n >= 1). */
    private static String placeholders(int n) {
        if (n <= 0) {
            throw new IllegalArgumentException("placeholders requires n >= 1, got " + n);
        }
        return String.join(",", java.util.Collections.nCopies(n, "?"));
    }
}
