// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.conf.ParamType;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

/**
 * Phase-A SQL-shape test for
 * {@link PgVectorRepository#referenceOnlyInsertQuery} (RDR-169 G4,
 * nexus-xvb6b; DSL form since nexus-xtmtf).
 *
 * <p>In the same package as {@link PgVectorRepository} so it can access the
 * package-private builder. Pure unit test — no DB, no Testcontainers: renders
 * the jOOQ query against the POSTGRES dialect and asserts the SQL fragment is
 * correctly formed (NULL chunk_text, retention column present, chunk_text
 * absent from DO UPDATE) without executing it against the live schema (the
 * {@code retention} column does not exist until Phase B / nexus-dtnpu).
 */
class ReferenceOnlySqlShapeTest {

    @Test
    void referenceOnlyInsertQuery_hasNullChunkTextAndRetentionColumn() {
        DSLContext ctx = DSL.using(SQLDialect.POSTGRES);
        String sql = PgVectorRepository.referenceOnlyInsertQuery(
                ctx, 1024, "t", "code__x__voyage-code-3__v1",
                dev.nexus.service.db.Chash.ofText("chash0000").toHex(),
                new float[1024], "{}")
            .getSQL(ParamType.INLINED)
            .toLowerCase();

        assertThat(sql)
            .as("INSERT targets the correct per-dim table")
            .contains("chunks_1024");
        assertThat(sql)
            .as("column list includes retention")
            .contains("retention");
        assertThat(sql)
            .as("chunk_text binds NULL (no content stored)")
            .contains("null");
        assertThat(sql)
            .as("retention value is 'reference-only'")
            .contains("reference-only");
        assertThat(sql)
            .as("DO UPDATE does NOT set chunk_text (defense-in-depth against full→ref clobber)")
            .doesNotContainPattern("chunk_text\"?\\s*=\\s*excluded");
        assertThat(sql)
            .as("DO UPDATE refreshes embedding from EXCLUDED")
            .containsPattern("embedding\"?\\s*=\\s*excluded");
    }
}
