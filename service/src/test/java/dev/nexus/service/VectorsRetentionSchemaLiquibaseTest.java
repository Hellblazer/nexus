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
 * ('reference-only','full'))} column, {@code chunk_text} becomes nullable,
 * and (fix round 1, T2 critique-nexus-zw2em-rdr169-phase-b-2026-09-11) a
 * biconditional CHECK ({@code chunks_content_retention_consistent})
 * enforces {@code (chunk_text IS NULL) = (retention = 'reference-only')}.
 *
 * <p>Modelled on {@link Catalog036EmbeddingProfileSchemaLiquibaseTest}: runs
 * against {@link PgContainerHelper#start()}'s shared, already-fully-migrated
 * cluster (a real full Liquibase walk from empty), using a superuser
 * connection (RLS-bypassing by construction, so no {@code TenantScope}
 * ceremony is needed for a pure schema-shape/CHECK-behaviour probe).
 *
 * <p>Uses {@link DimTables#CHUNKS} for every column referenced, including
 * {@link DimTables.ChunkTable#retention()} -- fix round 1 replaced
 * {@code PgVectorRepository#referenceOnlyInsertQuery}'s Phase-A ad-hoc
 * {@code DSL.field(DSL.name("retention"), ...)} placeholder with the same
 * generated accessor this test now uses, so there is no longer a distinct
 * "ad-hoc idiom" to model here (no raw SQL strings either way; the
 * RawSqlGateTest ratchet forbids new raw-SQL sites in the test tree,
 * nexus-cbo4a).
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
            String tenant = "t-retention-schema-check";
            String collection = "knowledge__retention-schema-check__voyage-context-3__v1";
            String chash = dev.nexus.service.db.Chash.ofText("retention-check-bogus").toHex();
            PgContainerHelper.insertCollection(ctx, tenant, collection);

            assertThatThrownBy(() ->
                ctx.insertInto(ch.table())
                   .columns(ch.tenantId(), ch.collection(), ch.chash(), ch.chunkText(),
                            ch.embedding(), ch.retention())
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

            String stored = ctx.select(ch.retention()).from(ch.table())
                .where(ch.tenantId().eq(tenant).and(ch.chash().eq(chash)))
                .fetchOne(ch.retention());
            assertThat(stored).as("an omitted retention column must default to 'full'").isEqualTo("full");
        }
    }

    @Test
    void referenceOnlyRow_nullChunkTextYieldsNullChunkTsv() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DimTables.ChunkTable ch = DimTables.CHUNKS.get(DIM);
            var chunkTsv = DSL.field(DSL.name("chunk_tsv"), Object.class);
            String tenant = "t-retention-schema-tsv";
            String collection = "knowledge__retention-schema-tsv__voyage-context-3__v1";
            String chash = dev.nexus.service.db.Chash.ofText("retention-check-tsv").toHex();
            PgContainerHelper.insertCollection(ctx, tenant, collection);

            ctx.insertInto(ch.table())
               .columns(ch.tenantId(), ch.collection(), ch.chash(), ch.chunkText(),
                        ch.embedding(), ch.retention())
               .values(tenant, collection, chash, null,
                       Vector.of(new float[DIM]), "reference-only")
               .execute();

            var row = ctx.select(ch.chunkText(), chunkTsv, ch.retention()).from(ch.table())
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
            assertThat(row.get(ch.retention())).isEqualTo("reference-only");
        }
    }

    // -------------------------------------------------------------------------
    // Biconditional CHECK (fix round 1, T2 critique-nexus-zw2em-rdr169-phase-b-
    // 2026-09-11): (chunk_text IS NULL) = (retention = 'reference-only')
    // -------------------------------------------------------------------------

    @Test
    void biconditionalCheck_rejectsNullContentMarkedFull() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DimTables.ChunkTable ch = DimTables.CHUNKS.get(DIM);
            String tenant = "t-retention-biconditional-null-full";
            String collection = "knowledge__retention-biconditional-null-full__voyage-context-3__v1";
            String chash = dev.nexus.service.db.Chash.ofText("retention-biconditional-null-full").toHex();
            PgContainerHelper.insertCollection(ctx, tenant, collection);

            // chunk_text=NULL + retention='full' -- the dangerous mismatch: a
            // consumer trusting retention='full' as "safe to assume content
            // present" would get NULL. Must be rejected.
            assertThatThrownBy(() ->
                ctx.insertInto(ch.table())
                   .columns(ch.tenantId(), ch.collection(), ch.chash(), ch.chunkText(),
                            ch.embedding(), ch.retention())
                   .values(tenant, collection, chash, null,
                           Vector.of(new float[DIM]), "full")
                   .execute())
                .as("chunk_text=NULL with retention='full' must violate the biconditional CHECK")
                .isInstanceOf(DataAccessException.class);
        }
    }

    /**
     * chunks_content_retention_consistent is now added NOT VALID (vectors-014-1) and
     * separately VALIDATEd (vectors-014-2) — a conexus deploy-side revision (unreleased)
     * to avoid an ACCESS EXCLUSIVE full-table scan on a ~440k-row production table. This
     * asserts the END STATE a fresh Liquibase walk reaches is unchanged by the split: the
     * constraint exists AND {@code pg_constraint.convalidated} is {@code true} — i.e. the
     * two-changeset split is genuinely transparent to every consumer of the constraint,
     * not just "the constraint is present" (which NOT VALID alone would already satisfy).
     */
    @Test
    void biconditionalCheck_existsAndIsValidated() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            assertThat(PgCatalogProbes.constraintExists(ctx, "chunks_content_retention_consistent"))
                .as("chunks_content_retention_consistent must exist after the full walk "
                    + "(vectors-014-1's ADD CONSTRAINT ... NOT VALID)")
                .isTrue();
            assertThat(PgCatalogProbes.constraintValidated(ctx, "chunks_content_retention_consistent"))
                .as("chunks_content_retention_consistent must be VALIDATED after the full "
                    + "walk (vectors-014-2's ALTER TABLE ... VALIDATE CONSTRAINT) -- NOT "
                    + "VALID alone is not the end state a fresh install reaches")
                .isTrue();
        }
    }

    @Test
    void biconditionalCheck_rejectsNonNullContentMarkedReferenceOnly() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            DimTables.ChunkTable ch = DimTables.CHUNKS.get(DIM);
            String tenant = "t-retention-biconditional-content-refonly";
            String collection = "knowledge__retention-biconditional-content-refonly__voyage-context-3__v1";
            String chash = dev.nexus.service.db.Chash.ofText("retention-biconditional-content-refonly").toHex();
            PgContainerHelper.insertCollection(ctx, tenant, collection);

            // chunk_text NOT NULL + retention='reference-only' -- the OTHER
            // mismatch: content present but marked as if it weren't. Must
            // also be rejected.
            assertThatThrownBy(() ->
                ctx.insertInto(ch.table())
                   .columns(ch.tenantId(), ch.collection(), ch.chash(), ch.chunkText(),
                            ch.embedding(), ch.retention())
                   .values(tenant, collection, chash, "real content present",
                           Vector.of(new float[DIM]), "reference-only")
                   .execute())
                .as("chunk_text present with retention='reference-only' must violate the "
                    + "biconditional CHECK")
                .isInstanceOf(DataAccessException.class);
        }
    }
}
