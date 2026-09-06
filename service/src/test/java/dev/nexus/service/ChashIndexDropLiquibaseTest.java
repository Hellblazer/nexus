package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-187 bead nexus-piwya.9 — the DROP of {@code nexus.chash_index}, the
 * router remnant of the split-store architecture (and, with it, the
 * 292,230 orphaned pointer rows production measured — they die at the DROP
 * by design, subsuming nexus-uu4ue step 2's DELETE).
 *
 * <p>Applies the full Liquibase master changelog to a fresh store and pins:
 * <ol>
 *   <li>{@code nexus.chash_index} does NOT exist (nor its indexes or octet
 *       CHECK — they die with the table)</li>
 *   <li>{@code staging.chash_index} (the dead-sink landing twin) is ALSO
 *       gone — dropped by rdr187-002 at nexus-piwya.11</li>
 *   <li>the SURVIVORS are intact: {@code idx_chunks_tenant_chash} (RDR-191
 *       Phase 4: the former three per-dim probe indexes are now ONE index on
 *       the unified {@code nexus.chunks} table), and the surviving chash
 *       octet CHECKs ({@code chunks_chash_octet_check}, also unified from
 *       three to one, and the manifest's own, still NOT VALID until
 *       nexus-uu4ue). {@code nexus.chash_alias} itself is NO LONGER a
 *       survivor — RDR-180 called it permanent, but nexus-lgdel.l1 dropped
 *       it once its beneficiary population reached zero; this test now pins
 *       that it is ALSO gone (legacy-001-drop-chash-alias.xml), a second,
 *       independent DROP in a later changelog than this test's own subject.</li>
 *   <li>a second Liquibase update is a clean no-op (MARK_RAN-safe
 *       preconditions)</li>
 * </ol>
 *
 * <p>Hermetic: Testcontainers pgvector, requires Docker.
 *
 * <p>nexus-cbo4a batch 6 (Sam's no-raw-SQL-in-Java directive, nexus-zrcj7): the
 * hand-rolled {@code DO $$ ... CREATE ROLE $$} + hand-rolled {@code Liquibase}
 * invocation is replaced by {@link PgContainerHelper#applyProductSchema} (see
 * {@code CatalogSchemaLiquibaseTest}'s identical conversion). The generic
 * {@code intOf(String sql)} wrapper — which took an arbitrary raw COUNT query
 * per call site — is replaced by typed per-predicate helpers over jOOQ's
 * {@code Meta} API (table existence) and {@code DSL.table(DSL.name(...))}/
 * {@code DSL.field(DSL.name(...), Class)} composition over
 * {@code pg_constraint}/{@code pg_indexes} (no jOOQ codegen exists for either
 * information_schema or pg_catalog — same category as {@code SchemaMigrator}'s
 * {@code countChangelogRows}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashIndexDropLiquibaseTest {

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        runLiquibaseUpdate();
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    private void runLiquibaseUpdate() throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    private DSLContext ctx(Connection c) {
        return DSL.using(c, SQLDialect.POSTGRES);
    }

    private boolean tableExists(DSLContext ctx, String schema, String table) {
        return !ctx.meta()
            .filterSchemas(s -> s.getName().equals(schema))
            .filterTables(t -> t.getName().equals(table))
            .getTables()
            .isEmpty();
    }

    private int constraintCount(DSLContext ctx, String... conNames) {
        Field<String> conname = DSL.field(DSL.name("conname"), String.class);
        return ctx.fetchCount(DSL.table(DSL.name("pg_constraint")), conname.in(conNames));
    }

    private int indexCountExact(DSLContext ctx, String schema, String indexName) {
        Field<String> schemaname = DSL.field(DSL.name("schemaname"), String.class);
        Field<String> indexname = DSL.field(DSL.name("indexname"), String.class);
        return ctx.fetchCount(DSL.table(DSL.name("pg_indexes")),
            schemaname.eq(schema).and(indexname.eq(indexName)));
    }

    private int indexCountLike(DSLContext ctx, String schema, String pattern) {
        Field<String> schemaname = DSL.field(DSL.name("schemaname"), String.class);
        Field<String> indexname = DSL.field(DSL.name("indexname"), String.class);
        return ctx.fetchCount(DSL.table(DSL.name("pg_indexes")),
            schemaname.eq(schema).and(indexname.like(pattern)));
    }

    @Test
    void routerTableIsGone() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = ctx(su);
            assertThat(tableExists(ctx, "nexus", "chash_index"))
                .as("nexus.chash_index must not exist — the router is retired (RDR-187)")
                .isFalse();
            assertThat(constraintCount(ctx, "chash_index_chash_octet_check"))
                .as("the router's octet CHECK dies with the table")
                .isZero();
            assertThat(indexCountLike(ctx, "nexus", "idx_chash_index%"))
                .as("the router's indexes die with the table")
                .isZero();
        }
    }

    @Test
    void survivorsAreIntact() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = ctx(su);
            assertThat(tableExists(ctx, "nexus", "chash_alias"))
                .as("chash_alias is dropped at nexus-lgdel.l1 (legacy-001-drop-chash-alias.xml) "
                    + "— RDR-180 called it permanent, but its beneficiary population reached zero")
                .isFalse();
            assertThat(tableExists(ctx, "staging", "chash_index"))
                .as("staging.chash_index (dead-sink landing) is dropped at "
                    + "nexus-piwya.11 (rdr187-002)")
                .isFalse();
            // RDR-191 Phase 4 (repoint-batch lane F1): the three per-dim probe
            // indexes collapsed into ONE idx_chunks_tenant_chash on the unified
            // nexus.chunks table (vectors-004-unify-chunks.xml step 5).
            assertThat(indexCountExact(ctx, "nexus", "idx_chunks_tenant_chash"))
                .as("the (tenant_id, chash) probe index serves the reroute — must survive")
                .isEqualTo(1);
            // The surviving octet CHECKs: chunks_chash_octet_check is now ONE
            // unified constraint (was three per-dim), plus the manifest's own.
            assertThat(constraintCount(ctx,
                "chunks_chash_octet_check", "catalog_document_chunks_chash_octet_check"))
                .isEqualTo(2);
        }
    }

    @Test
    void secondUpdateIsCleanNoOp() throws Exception {
        runLiquibaseUpdate();
        try (Connection su = pg.createConnection("")) {
            assertThat(tableExists(ctx(su), "nexus", "chash_index")).isFalse();
        }
    }
}
