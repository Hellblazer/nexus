// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.DimTables;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-169 Phase B (bead nexus-zw2em) -- Liquibase schema-apply test for
 * {@code vectors-014-retention.xml}: {@code nexus.chunks} gains a
 * {@code retention TEXT NOT NULL DEFAULT 'full' CHECK (retention IN
 * ('reference-only','full'))} column and {@code chunk_text} becomes nullable.
 *
 * <p>Modelled on {@link Catalog036EmbeddingProfileSchemaLiquibaseTest}: runs
 * against {@link PgContainerHelper#start()}'s shared, already-fully-migrated
 * cluster (a real full Liquibase walk from empty), using a superuser
 * connection (RLS-bypassing by construction, so no {@code TenantScope}
 * ceremony is needed for a pure schema-shape/CHECK-behaviour probe).
 *
 * <p>Uses {@link DimTables#CHUNKS} for the pre-existing typed columns
 * (tenant_id/collection/chash/chunk_text/embedding/metadata) plus an
 * AD-HOC {@code DSL.field(DSL.name("retention"), ...)} reference for the
 * new column -- exactly the idiom {@code PgVectorRepository
 * #referenceOnlyInsertQuery} itself uses pre-jOOQ-regen (typed jOOQ DSL,
 * no raw SQL strings; the RawSqlGateTest ratchet forbids new raw-SQL
 * sites in the test tree, nexus-cbo4a).
 */
class VectorsRetentionSchemaLiquibaseTest {

    private static final int DIM = 1024;

    @Test
    void retentionColumn_existsWithFullDefaultAndNotNull() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            PgCatalogProbes.ColumnInfo retention =
                PgCatalogProbes.columnInfo(ctx, "nexus", "chunks", "retention");
            assertThat(retention).as("nexus.chunks.retention must exist after Liquibase").isNotNull();
            assertThat(retention.nullable())
                .as("retention is NOT NULL (every row -- 'full' or 'reference-only')")
                .isFalse();
            assertThat(retention.columnDefault())
                .as("retention DEFAULT must backfill existing/omitted rows as 'full'")
                .contains("full");
        }
    }

    @Test
    void chunkTextColumn_isNowNullable() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            PgCatalogProbes.ColumnInfo chunkText =
                PgCatalogProbes.columnInfo(ctx, "nexus", "chunks", "chunk_text");
            assertThat(chunkText).as("nexus.chunks.chunk_text must still exist").isNotNull();
            assertThat(chunkText.nullable())
                .as("chunk_text DROP NOT NULL -- a reference-only chunk carries no content")
                .isTrue();
        }
    }

    @Test
    void embeddingAndTenantIdConstraints_areUnchanged() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            assertThat(PgCatalogProbes.constraintExists(ctx, "exactly_one_embedding"))
                .as("exactly_one_embedding CHECK must survive the retention ALTER unchanged "
                    + "(a reference-only chunk is still a vector)")
                .isTrue();

            assertThat(PgCatalogProbes.columnNotNull(ctx, "nexus", "chunks", "tenant_id"))
                .as("tenant_id stays NOT NULL -- RLS is unconditional regardless of retention "
                    + "(RDR-169 binding constraint 3)")
                .isTrue();
        }
    }

    @Test
    void retentionCheck_rejectsAnyValueOutsideTheEnum() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DimTables.ChunkTable ch = DimTables.CHUNKS.get(DIM);
            var retention = DSL.field(DSL.name("retention"), String.class);
            String tenant = "t-retention-schema-check";
            String collection = "knowledge__retention-schema-check__voyage-context-3__v1";
            String chash = dev.nexus.service.db.Chash.ofText("retention-check-bogus").toHex();
            PgContainerHelper.insertCollection(ctx, tenant, collection);

            assertThatThrownBy(() ->
                ctx.insertInto(ch.table())
                   .columns(ch.tenantId(), ch.collection(), ch.chash(), ch.chunkText(),
                            ch.embedding(), retention)
                   .values(tenant, collection, chash, "text",
                           Vector.of(new float[DIM]), "bogus-retention-value")
                   .execute())
                .as("a retention value outside {'reference-only','full'} must violate the CHECK")
                .isInstanceOf(DataAccessException.class);
        }
    }

    @Test
    void retentionDefault_backfillsFullWhenOmitted() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DimTables.ChunkTable ch = DimTables.CHUNKS.get(DIM);
            var retention = DSL.field(DSL.name("retention"), String.class);
            String tenant = "t-retention-schema-default";
            String collection = "knowledge__retention-schema-default__voyage-context-3__v1";
            String chash = dev.nexus.service.db.Chash.ofText("retention-check-default").toHex();
            PgContainerHelper.insertCollection(ctx, tenant, collection);

            // retention intentionally omitted from the column list.
            ctx.insertInto(ch.table())
               .columns(ch.tenantId(), ch.collection(), ch.chash(), ch.chunkText(), ch.embedding())
               .values(tenant, collection, chash, "full content, retention omitted",
                       Vector.of(new float[DIM]))
               .execute();

            String stored = ctx.select(retention).from(ch.table())
                .where(ch.tenantId().eq(tenant).and(ch.chash().eq(chash)))
                .fetchOne(retention);
            assertThat(stored).as("an omitted retention column must default to 'full'").isEqualTo("full");
        }
    }

    @Test
    void referenceOnlyRow_nullChunkTextYieldsNullChunkTsv() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DimTables.ChunkTable ch = DimTables.CHUNKS.get(DIM);
            var retention = DSL.field(DSL.name("retention"), String.class);
            var chunkTsv = DSL.field(DSL.name("chunk_tsv"), Object.class);
            String tenant = "t-retention-schema-tsv";
            String collection = "knowledge__retention-schema-tsv__voyage-context-3__v1";
            String chash = dev.nexus.service.db.Chash.ofText("retention-check-tsv").toHex();
            PgContainerHelper.insertCollection(ctx, tenant, collection);

            ctx.insertInto(ch.table())
               .columns(ch.tenantId(), ch.collection(), ch.chash(), ch.chunkText(),
                        ch.embedding(), retention)
               .values(tenant, collection, chash, null,
                       Vector.of(new float[DIM]), "reference-only")
               .execute();

            var row = ctx.select(ch.chunkText(), chunkTsv, retention).from(ch.table())
                .where(ch.tenantId().eq(tenant).and(ch.chash().eq(chash)))
                .fetchOne();
            assertThat(row).isNotNull();
            assertThat(row.get(ch.chunkText()))
                .as("chunk_text must round-trip as NULL for a reference-only row").isNull();
            assertThat(row.get(chunkTsv))
                .as("chunk_tsv (GENERATED ALWAYS AS to_tsvector('english', chunk_text)) must be "
                    + "NULL when chunk_text is NULL -- the generated-column expression is "
                    + "UNCHANGED (CA-2 verified: to_tsvector('english', NULL) -> NULL), which is "
                    + "what excludes the row from FTS (@@ on NULL is false) and the GIN index")
                .isNull();
            assertThat(row.get(retention)).isEqualTo("reference-only");
        }
    }
}
