/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.vectors;

import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.Tables;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Table;
import java.util.Map;

/**
 * Typed accessors for the unified, dim-columned pgvector tables (nexus-xtmtf,
 * RDR-191 Phase 4 / nexus-o8dil.16 + .48).
 *
 * <p>{@code nexus.chunks} / {@code nexus.taxonomy_centroids} are now single
 * tables carrying one {@code embedding_384}/{@code embedding_768}/
 * {@code embedding_1024} column each (exactly one non-null, enforced by a DB
 * CHECK constraint) rather than three separate per-dim tables. This is the
 * SINGLE authority for that unification on the Java side: every dim resolves
 * to the SAME {@link Tables#CHUNKS} / {@link Tables#TAXONOMY_CENTROIDS}
 * instance, with only the embedding {@code Field} selected per dim. Fields
 * are pulled FROM the generated table instance, so the pgvector
 * {@code VectorBinding} (codegen forcedType, {@code Vector} user type) rides
 * along — no {@code ?::vector} casts, no {@code vectorLiteral()} strings.
 *
 * <p>The raw-SQL channel (string-built statements in {@code PgVectorRepository}
 * / {@code TaxonomyCentroidRepository} / {@code RekeyOps} / etc.) cannot use
 * these typed {@code Field}/{@code Table} objects directly, but MUST consult
 * {@link #CHUNKS_TABLE_NAME} / {@link #CENTROIDS_TABLE_NAME} /
 * {@link #embeddingColumn(int)} for its table- and column-name strings
 * instead of re-deriving them (e.g. the old {@code "nexus.chunks_" + dim}
 * pattern). This is what stops the raw-SQL channel drifting independently of
 * the typed one — one name authority, two call shapes.
 *
 * <p>The dim key set mirrors {@code VALID_DIMS}; an unknown dim returns null
 * from the maps and callers keep their existing loud validation.
 */
public final class DimTables {

    /** Fully-qualified raw-SQL name of the unified chunks table. */
    public static final String CHUNKS_TABLE_NAME = "nexus.chunks";

    /** Fully-qualified raw-SQL name of the unified taxonomy-centroids table. */
    public static final String CENTROIDS_TABLE_NAME = "nexus.taxonomy_centroids";

    /**
     * Raw-SQL column name for the embedding at {@code dim}, e.g.
     * {@code "embedding_768"}. Mirrors the typed accessors' field selection
     * ({@code t.field("embedding_" + dim)}) for callers on the string-built
     * SQL channel that cannot use a typed {@code Field}.
     */
    public static String embeddingColumn(int dim) {
        return "embedding_" + dim;
    }

    /** {@code nexus.chunks} accessor, embedding column selected per dim. */
    public record ChunkTable(
        Table<?> table,
        Field<String> tenantId,
        Field<String> collection,
        Field<String> chash,
        Field<String> chunkText,
        Field<Vector> embedding,
        Field<JSONB> metadata
    ) {
        @SuppressWarnings("unchecked")
        static ChunkTable of(Table<?> t, int dim) {
            return new ChunkTable(
                t,
                t.field("tenant_id", String.class),
                t.field("collection", String.class),
                // RDR-180: bytea column carried as hex in Java — the
                // ChashHex converted type binds/fetches through the codec,
                // so every repository site stays hex-string-shaped.
                dev.nexus.service.db.ChashHex.hex(t, "chash"),
                t.field("chunk_text", String.class),
                (Field<Vector>) t.field(embeddingColumn(dim)),
                t.field("metadata", JSONB.class));
        }
    }

    /** {@code nexus.taxonomy_centroids} accessor, embedding column selected per dim. */
    public record CentroidTable(
        Table<?> table,
        Field<String> tenantId,
        Field<String> collection,
        Field<Long> topicId,
        Field<Vector> embedding,
        Field<String> label,
        Field<Integer> docCount
    ) {
        @SuppressWarnings("unchecked")
        static CentroidTable of(Table<?> t, int dim) {
            return new CentroidTable(
                t,
                t.field("tenant_id", String.class),
                t.field("collection", String.class),
                t.field("topic_id", Long.class),
                (Field<Vector>) t.field(embeddingColumn(dim)),
                t.field("label", String.class),
                t.field("doc_count", Integer.class));
        }
    }

    public static final Map<Integer, ChunkTable> CHUNKS = Map.of(
        384,  ChunkTable.of(Tables.CHUNKS, 384),
        768,  ChunkTable.of(Tables.CHUNKS, 768),
        1024, ChunkTable.of(Tables.CHUNKS, 1024));

    public static final Map<Integer, CentroidTable> CENTROIDS = Map.of(
        384,  CentroidTable.of(Tables.TAXONOMY_CENTROIDS, 384),
        768,  CentroidTable.of(Tables.TAXONOMY_CENTROIDS, 768),
        1024, CentroidTable.of(Tables.TAXONOMY_CENTROIDS, 1024));

    private DimTables() {
    }
}
