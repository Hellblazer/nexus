package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record2;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-gmiaf.18 — Catalog Liquibase schema apply test.
 *
 * <p>Starts an embedded PG, applies the full master changelog, then verifies
 * that all 6 catalog tables exist in the nexus schema with the expected columns.
 *
 * <p>nexus-cbo4a batch 6 (Sam's no-raw-SQL-in-Java directive, nexus-zrcj7): the
 * hand-rolled {@code DO $$ ... CREATE ROLE $$} + hand-rolled {@code Liquibase}
 * invocation is replaced by {@link PgContainerHelper#applyProductSchema} (the
 * same helper nexus-cbo4a batch 1a centralized for exactly this purpose — its
 * own javadoc already documents that {@code role-001-nexus-svc.xml}, the FIRST
 * include in the master changelog, creates {@code nexus_svc} if absent, so the
 * DO-block pre-create here was always redundant). Every information_schema/
 * pg_catalog read is retired onto jOOQ's {@code Meta} API (table/column
 * existence, unique-key existence — no jOOQ codegen exists for
 * information_schema, so {@code DSLContext#meta()} is the typed-DSL
 * equivalent) or typed {@code DSL.table(DSL.name(...))}/{@code
 * DSL.field(DSL.name(...), Class)} composition over {@code pg_class}/{@code
 * pg_namespace}/{@code pg_roles} (no jOOQ codegen exists for pg_catalog either
 * — same category and same conversion shape as {@code SchemaMigrator}'s
 * {@code countChangelogRows}/{@code countChangelogRowsSince}).
 */
class CatalogSchemaLiquibaseTest {

    @Test
    void catalogSchemaAppliesCleanly() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }

        // Re-open a fresh connection after changelog committed to verify schema
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);

            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            // Verify all 6 catalog tables exist in nexus schema
            for (String table : new String[]{
                "catalog_owners", "catalog_documents", "catalog_links",
                "catalog_document_chunks", "catalog_collections", "catalog_meta"}) {
                boolean exists = !ctx.meta()
                    .filterSchemas(s -> s.getName().equals("nexus"))
                    .filterTables(t -> t.getName().equals(table))
                    .getTables()
                    .isEmpty();
                assertThat(exists)
                    .as("table nexus." + table + " should exist after Liquibase")
                    .isTrue();
            }

            Table<?> catalogDocuments = ctx.meta()
                .filterSchemas(s -> s.getName().equals("nexus"))
                .filterTables(t -> t.getName().equals("catalog_documents"))
                .getTables()
                .get(0);

            // Spot-check catalog_documents has fts_vector column
            assertThat(catalogDocuments.field("fts_vector"))
                .as("catalog_documents.fts_vector column should exist")
                .isNotNull();

            Table<?> catalogLinks = ctx.meta()
                .filterSchemas(s -> s.getName().equals("nexus"))
                .filterTables(t -> t.getName().equals("catalog_links"))
                .getTables()
                .get(0);

            // Spot-check catalog_links has BIGSERIAL id
            assertThat(catalogLinks.field("id"))
                .as("catalog_links.id column should exist")
                .isNotNull();

            // Spot-check catalog_links UNIQUE constraint
            assertThat(catalogLinks.getUniqueKeys())
                .as("catalog_links UNIQUE constraint should exist")
                .isNotEmpty();

            // Spot-check RLS is enabled on catalog_documents
            Table<?> pgClass = DSL.table(DSL.name("pg_class"));
            Table<?> pgNamespace = DSL.table(DSL.name("pg_namespace"));
            Field<Boolean> relrowsecurity = DSL.field(DSL.name("pg_class", "relrowsecurity"), Boolean.class);
            Field<Boolean> relforcerowsecurity =
                DSL.field(DSL.name("pg_class", "relforcerowsecurity"), Boolean.class);
            Record2<Boolean, Boolean> rlsRow = ctx.select(relrowsecurity, relforcerowsecurity)
                .from(pgClass)
                .join(pgNamespace)
                    .on(DSL.field(DSL.name("pg_namespace", "oid"))
                        .eq(DSL.field(DSL.name("pg_class", "relnamespace"))))
                .where(DSL.field(DSL.name("pg_namespace", "nspname"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("pg_class", "relname"), String.class).eq("catalog_documents"))
                .fetchOne();
            assertThat(rlsRow).isNotNull();
            assertThat(rlsRow.value1())
                .as("RLS ENABLE on catalog_documents")
                .isTrue();
            assertThat(rlsRow.value2())
                .as("RLS FORCE on catalog_documents")
                .isTrue();
        }
    }

    /**
     * nexus-v80f2 (2026-08-15, substantive-critic finding S2): pin
     * {@code nexus_svc}'s NOINHERIT attribute on the SHARED-CLUSTER path,
     * not just {@code GrantsPgMonitorTest}'s hand-rolled {@code
     * startDedicated()} fixture. Uses the same shared, already-migrated
     * cluster every other test in this class (and the large majority of
     * this suite) runs against -- {@link PgContainerHelper#start()} --
     * so this exercises the REAL {@code role-001-nexus-svc.xml} changeset
     * as it actually executes for the shared-cluster population, not a
     * hand-replayed literal.
     *
     * <p>Falsifiable: {@link SharedCluster} no longer pre-creates {@code
     * nexus_svc} itself (that redundant pre-create, which raced and won
     * against role-001's own {@code CREATE ROLE ... IF NOT EXISTS}, is
     * exactly what let this attribute drift unnoticed -- see {@code
     * SharedCluster#ensureBootstrapped}'s comment). role-001-nexus-svc.xml
     * is therefore the SOLE source of {@code nexus_svc} on this template;
     * dropping {@code NOINHERIT} from its {@code CREATE ROLE} turns this
     * test red.
     */
    @Test
    void nexusSvcRoleIsNoinherit() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Field<Boolean> rolinherit = DSL.field(DSL.name("rolinherit"), Boolean.class);
            Boolean value = ctx.select(rolinherit)
                .from(DSL.table(DSL.name("pg_roles")))
                .where(DSL.field(DSL.name("rolname"), String.class).eq("nexus_svc"))
                .fetchOne(rolinherit);
            assertThat(value)
                .as("nexus_svc role must exist on the shared-cluster template "
                    + "(created by role-001-nexus-svc.xml)")
                .isNotNull();
            assertThat(value)
                .as("nexus_svc must be NOINHERIT (nexus-v80f2: the posture in "
                    + "every mode -- cloud measured, local provisioning aligned, "
                    + "role-001-nexus-svc.xml's fallback bootstrap always has)")
                .isFalse();
        }
    }
}
