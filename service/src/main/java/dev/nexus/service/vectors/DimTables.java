/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.vectors;

import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.Tables;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Table;
import java.util.Map;

/**
 * Typed accessors for the per-dimension pgvector tables (nexus-xtmtf).
 *
 * <p>{@code chunks_<dim>} / {@code taxonomy_centroids_<dim>} are generated as
 * separate jOOQ classes with no common typed interface; these holders expose
 * one {@code Table} + its fields per dim so repositories write plain DSL
 * instead of {@code "INSERT INTO " + chunksTable(dim)} string concatenation.
 * Fields are pulled FROM the generated table instances, so the pgvector
 * {@code VectorBinding} (codegen forcedType, {@code Vector} user type)
 * rides along — no {@code ?::vector} casts, no {@code vectorLiteral()}
 * strings.
 *
 * <p>The dim key set mirrors {@code VALID_DIMS}; an unknown dim returns null
 * from the maps and callers keep their existing loud validation.
 */
public final class DimTables {

    /** chunks_&lt;dim&gt; accessor. */
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
        static ChunkTable of(Table<?> t) {
            return new ChunkTable(
                t,
                t.field("tenant_id", String.class),
                t.field("collection", String.class),
                // RDR-180: bytea column carried as hex in Java — the
                // ChashHex converted type binds/fetches through the codec,
                // so every repository site stays hex-string-shaped.
                dev.nexus.service.db.ChashHex.hex(t, "chash"),
                t.field("chunk_text", String.class),
                (Field<Vector>) t.field("embedding"),
                t.field("metadata", JSONB.class));
        }
    }

    /** taxonomy_centroids_&lt;dim&gt; accessor. */
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
        static CentroidTable of(Table<?> t) {
            return new CentroidTable(
                t,
                t.field("tenant_id", String.class),
                t.field("collection", String.class),
                t.field("topic_id", Long.class),
                (Field<Vector>) t.field("embedding"),
                t.field("label", String.class),
                t.field("doc_count", Integer.class));
        }
    }

    public static final Map<Integer, ChunkTable> CHUNKS = Map.of(
        384,  ChunkTable.of(Tables.CHUNKS_384),
        768,  ChunkTable.of(Tables.CHUNKS_768),
        1024, ChunkTable.of(Tables.CHUNKS_1024));

    public static final Map<Integer, CentroidTable> CENTROIDS = Map.of(
        384,  CentroidTable.of(Tables.TAXONOMY_CENTROIDS_384),
        768,  CentroidTable.of(Tables.TAXONOMY_CENTROIDS_768),
        1024, CentroidTable.of(Tables.TAXONOMY_CENTROIDS_1024));

    private DimTables() {
    }
}
