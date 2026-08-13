// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.jooq.nexus.Tables;
import org.junit.jupiter.api.Test;

/**
 * Pure unit coverage for {@link DimTables} — the single Java-side authority
 * for the unified, dim-columned {@code nexus.chunks} /
 * {@code nexus.taxonomy_centroids} tables (RDR-191 Phase 4, nexus-o8dil.16 +
 * .48). No PG container: these are static jOOQ metadata accessors, not
 * queries, so construction alone proves the field wiring.
 *
 * <p>Pins the post-unification API surface: {@code ChunkTable.of}/
 * {@code CentroidTable.of} now take a {@code (Table<?>, int dim)} pair (was
 * one arg, one {@code Table} per dim) because all three dims resolve to the
 * SAME generated table, with only the embedding column varying by dim.
 */
class DimTablesTest {

    @Test
    void chunks_hasEntryForEveryValidDim() {
        assertThat(DimTables.CHUNKS).containsOnlyKeys(384, 768, 1024);
    }

    @Test
    void chunks_allDimsResolveToTheSameUnifiedTable() {
        for (var dim : new int[] {384, 768, 1024}) {
            assertThat(DimTables.CHUNKS.get(dim).table())
                .as("dim %d table identity", dim)
                .isSameAs(Tables.CHUNKS);
        }
    }

    @Test
    void chunks_embeddingFieldNamedPerDim() {
        assertThat(DimTables.CHUNKS.get(384).embedding().getName()).isEqualTo("embedding_384");
        assertThat(DimTables.CHUNKS.get(768).embedding().getName()).isEqualTo("embedding_768");
        assertThat(DimTables.CHUNKS.get(1024).embedding().getName()).isEqualTo("embedding_1024");
    }

    @Test
    void chunks_nonEmbeddingFieldsShareTheSameNamesAcrossDims() {
        for (var dim : new int[] {384, 768, 1024}) {
            var ch = DimTables.CHUNKS.get(dim);
            assertThat(ch.tenantId().getName()).isEqualTo("tenant_id");
            assertThat(ch.collection().getName()).isEqualTo("collection");
            assertThat(ch.chash()).as("dim %d chash field present", dim).isNotNull();
            assertThat(ch.chunkText().getName()).isEqualTo("chunk_text");
            assertThat(ch.metadata().getName()).isEqualTo("metadata");
        }
    }

    @Test
    void centroids_hasEntryForEveryValidDim() {
        assertThat(DimTables.CENTROIDS).containsOnlyKeys(384, 768, 1024);
    }

    @Test
    void centroids_allDimsResolveToTheSameUnifiedTable() {
        for (var dim : new int[] {384, 768, 1024}) {
            assertThat(DimTables.CENTROIDS.get(dim).table())
                .as("dim %d table identity", dim)
                .isSameAs(Tables.TAXONOMY_CENTROIDS);
        }
    }

    @Test
    void centroids_embeddingFieldNamedPerDim() {
        assertThat(DimTables.CENTROIDS.get(384).embedding().getName()).isEqualTo("embedding_384");
        assertThat(DimTables.CENTROIDS.get(768).embedding().getName()).isEqualTo("embedding_768");
        assertThat(DimTables.CENTROIDS.get(1024).embedding().getName()).isEqualTo("embedding_1024");
    }

    @Test
    void centroids_nonEmbeddingFieldsShareTheSameNamesAcrossDims() {
        for (var dim : new int[] {384, 768, 1024}) {
            var ct = DimTables.CENTROIDS.get(dim);
            assertThat(ct.tenantId().getName()).isEqualTo("tenant_id");
            assertThat(ct.collection().getName()).isEqualTo("collection");
            assertThat(ct.topicId().getName()).isEqualTo("topic_id");
            assertThat(ct.label().getName()).isEqualTo("label");
            assertThat(ct.docCount().getName()).isEqualTo("doc_count");
        }
    }

    @Test
    void embeddingColumn_derivesRawSqlColumnNamePerDim() {
        assertThat(DimTables.embeddingColumn(384)).isEqualTo("embedding_384");
        assertThat(DimTables.embeddingColumn(768)).isEqualTo("embedding_768");
        assertThat(DimTables.embeddingColumn(1024)).isEqualTo("embedding_1024");
    }

    @Test
    void tableNameConstants_areTheUnifiedFullyQualifiedNames() {
        assertThat(DimTables.CHUNKS_TABLE_NAME).isEqualTo("nexus.chunks");
        assertThat(DimTables.CENTROIDS_TABLE_NAME).isEqualTo("nexus.taxonomy_centroids");
    }
}
