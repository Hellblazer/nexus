// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/**
 * Phase-A SQL-shape test for {@link PgVectorRepository#referenceOnlyInsertSql}
 * (RDR-169 G4, nexus-xvb6b).
 *
 * <p>In the same package as {@link PgVectorRepository} so it can access the
 * package-private {@code referenceOnlyInsertSql} method.  Pure unit test — no
 * DB, no Testcontainers.  Asserts the INSERT SQL fragment is correctly formed
 * (NULL chunk_text, retention column present, chunk_text absent from DO UPDATE)
 * without executing it against the live schema (the {@code retention} column
 * does not exist until Phase B / nexus-dtnpu).
 */
class ReferenceOnlySqlShapeTest {

    @Test
    void referenceOnlyInsertSql_hasNullChunkTextAndRetentionColumn() {
        String sql = PgVectorRepository.referenceOnlyInsertSql("nexus.chunks_1024");

        assertThat(sql)
            .as("INSERT targets the correct table")
            .contains("nexus.chunks_1024");
        assertThat(sql)
            .as("column list includes retention")
            .contains("retention");
        assertThat(sql)
            .as("VALUES binds NULL for chunk_text (no placeholder for content)")
            .contains("NULL");
        assertThat(sql)
            .as("retention value is 'reference-only'")
            .contains("'reference-only'");
        assertThat(sql)
            .as("DO UPDATE does NOT set chunk_text (defense-in-depth against full→ref clobber)")
            .doesNotContainPattern("chunk_text\\s*=\\s*EXCLUDED\\.chunk_text");
        assertThat(sql)
            .as("DO UPDATE refreshes embedding")
            .contains("embedding  = EXCLUDED.embedding");
    }
}
