package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record2;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-gmiaf.14 — Taxonomy Liquibase schema smoke test.
 *
 * <p>Verifies that the taxonomy-001-baseline.xml changeset applies cleanly
 * and produces the expected tables, columns, and RLS policies. No FTS
 * verification needed (Store 4 explicitly forbids tsvector/GIN).
 *
 * <p>nexus-cbo4a batch 6 (Sam's no-raw-SQL-in-Java directive, nexus-zrcj7): the
 * hand-rolled {@code DO $$ ... CREATE ROLE $$} + hand-rolled {@code Liquibase}
 * invocation is replaced by {@link PgContainerHelper#applyProductSchema}
 * (see {@code CatalogSchemaLiquibaseTest}'s identical conversion for the full
 * rationale). Every information_schema/pg_catalog read is retired onto jOOQ's
 * {@code Meta} API or typed {@code DSL.table(DSL.name(...))}/{@code
 * DSL.field(DSL.name(...), Class)} composition — no jOOQ codegen exists for
 * either schema, same category and conversion shape as {@code
 * SchemaMigrator}'s {@code countChangelogRows}/{@code countChangelogRowsSince}.
 */
class TaxonomySchemaLiquibaseTest {

    @Test
    void taxonomyChangeset_appliesAndCreatesExpectedTables() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.start()) {

            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.applyProductSchema(su);
            }

            try (Connection c = pg.createConnection("")) {
                DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);

                // All four tables exist in nexus schema
                for (String table : List.of("topics", "taxonomy_meta",
                                            "topic_assignments", "topic_links")) {
                    boolean exists = !ctx.meta()
                        .filterSchemas(s -> s.getName().equals("nexus"))
                        .filterTables(t -> t.getName().equals(table))
                        .getTables()
                        .isEmpty();
                    assertThat(exists).as("table nexus." + table + " must exist").isTrue();
                }

                // topics columns
                List<String> topicCols = columnNames(ctx, "nexus", "topics");
                assertThat(topicCols).containsAll(List.of(
                    "id", "tenant_id", "label", "parent_id", "collection",
                    "centroid_hash", "doc_count", "created_at", "review_status", "terms"));

                // No tsvector column anywhere (Store 4 contract)
                for (String table : List.of("topics", "taxonomy_meta", "topic_assignments", "topic_links")) {
                    List<String> cols = columnNames(ctx, "nexus", table);
                    cols.forEach(col ->
                        assertThat(col).as("No FTS column in " + table)
                            .doesNotContainIgnoringCase("tsvec")
                            .doesNotContainIgnoringCase("tsv_")
                            .doesNotContainIgnoringCase("_fts"));
                }

                // RLS enabled on all four tables
                Table<?> pgClass = DSL.table(DSL.name("pg_class"));
                Field<String> relname = DSL.field(DSL.name("pg_class", "relname"), String.class);
                Field<Object> relnamespace = DSL.field(DSL.name("pg_class", "relnamespace"));
                Field<Boolean> relrowsecurity =
                    DSL.field(DSL.name("pg_class", "relrowsecurity"), Boolean.class);
                Field<Object> nsOid = DSL.field(DSL.name("pg_namespace", "oid"));
                Field<String> nspname = DSL.field(DSL.name("pg_namespace", "nspname"), String.class);
                for (String table : List.of("topics", "taxonomy_meta", "topic_assignments", "topic_links")) {
                    Boolean rls = ctx.select(relrowsecurity)
                        .from(pgClass)
                        .where(relname.eq(table))
                        .and(relnamespace.eq(
                            ctx.select(nsOid).from(DSL.table(DSL.name("pg_namespace")))
                                .where(nspname.eq("nexus"))))
                        .fetchOne(relrowsecurity);
                    assertThat(rls).as("pg_class entry for " + table).isNotNull();
                    assertThat(rls).as("RLS must be enabled on nexus." + table).isTrue();
                }

                // taxonomy_meta columns
                List<String> metaCols = columnNames(ctx, "nexus", "taxonomy_meta");
                assertThat(metaCols).containsAll(List.of(
                    "tenant_id", "collection", "last_discover_doc_count", "last_discover_at"));

                // topic_assignments columns
                List<String> assignCols = columnNames(ctx, "nexus", "topic_assignments");
                assertThat(assignCols).containsAll(List.of(
                    "tenant_id", "doc_id", "topic_id", "assigned_by",
                    "similarity", "assigned_at", "source_collection"));

                // topic_links columns
                List<String> linkCols = columnNames(ctx, "nexus", "topic_links");
                assertThat(linkCols).containsAll(List.of(
                    "tenant_id", "from_topic_id", "to_topic_id", "link_count", "link_types"));
            }
        }
    }

    /**
     * RDR-154 P0 (bead nexus-i7ivk): taxonomy-003 doc_count trigger changeset.
     * Asserts the two recompute functions exist and are SECURITY INVOKER
     * (prosecdef=false), both statement-level triggers exist on
     * topic_assignments, and the trigger-maintained COMMENT is recorded on
     * topics.doc_count.
     */
    @Test
    void docCountTrigger_functionsTriggersAndComment() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.start()) {

            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.applyProductSchema(su);
            }

            try (Connection c = pg.createConnection("")) {
                DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);

                // Both recompute functions exist and are SECURITY INVOKER (prosecdef=false).
                Table<?> pgProc = DSL.table(DSL.name("pg_proc")).as("p");
                Table<?> pgNamespaceP = DSL.table(DSL.name("pg_namespace")).as("n");
                Field<String> proname = DSL.field(DSL.name("p", "proname"), String.class);
                Field<Boolean> prosecdef = DSL.field(DSL.name("p", "prosecdef"), Boolean.class);
                Field<Object> pronamespace = DSL.field(DSL.name("p", "pronamespace"));
                Field<Object> nOid = DSL.field(DSL.name("n", "oid"));
                Field<String> nspnameP = DSL.field(DSL.name("n", "nspname"), String.class);

                Result<Record2<String, Boolean>> fns = ctx.select(proname, prosecdef)
                    .from(pgProc)
                    .join(pgNamespaceP).on(nOid.eq(pronamespace))
                    .where(nspnameP.eq("nexus"))
                    .and(proname.in("topics_doc_count_recount_ins", "topics_doc_count_recount_del"))
                    .orderBy(proname)
                    .fetch();
                List<String> invokerFns = new ArrayList<>();
                for (Record2<String, Boolean> row : fns) {
                    assertThat(row.value2())
                        .as("function %s MUST be SECURITY INVOKER (prosecdef=false)", row.value1())
                        .isFalse();
                    invokerFns.add(row.value1());
                }
                assertThat(invokerFns).containsExactly(
                    "topics_doc_count_recount_del", "topics_doc_count_recount_ins");

                // Both statement-level triggers exist on nexus.topic_assignments.
                Table<?> pgTrigger = DSL.table(DSL.name("pg_trigger")).as("t");
                Table<?> pgClassT = DSL.table(DSL.name("pg_class")).as("cl");
                Table<?> pgNamespaceT = DSL.table(DSL.name("pg_namespace")).as("n2");
                Field<String> tgname = DSL.field(DSL.name("t", "tgname"), String.class);
                Field<Object> tgrelid = DSL.field(DSL.name("t", "tgrelid"));
                Field<Object> clOid = DSL.field(DSL.name("cl", "oid"));
                Field<Object> relnamespaceT = DSL.field(DSL.name("cl", "relnamespace"));
                Field<Object> n2Oid = DSL.field(DSL.name("n2", "oid"));
                Field<String> n2Nspname = DSL.field(DSL.name("n2", "nspname"), String.class);
                Field<String> clRelname = DSL.field(DSL.name("cl", "relname"), String.class);
                Field<Boolean> tgisinternal = DSL.field(DSL.name("t", "tgisinternal"), Boolean.class);

                List<String> triggers = ctx.select(tgname)
                    .from(pgTrigger)
                    .join(pgClassT).on(clOid.eq(tgrelid))
                    .join(pgNamespaceT).on(n2Oid.eq(relnamespaceT))
                    .where(n2Nspname.eq("nexus"))
                    .and(clRelname.eq("topic_assignments"))
                    .and(tgisinternal.isFalse())
                    .orderBy(tgname)
                    .fetch(tgname);
                assertThat(triggers).contains(
                    "trg_topic_assignments_doc_count_del",
                    "trg_topic_assignments_doc_count_ins");

                // Trigger-maintained COMMENT recorded on topics.doc_count.
                Table<?> pgDescription = DSL.table(DSL.name("pg_description")).as("pgd");
                Table<?> pgClassD = DSL.table(DSL.name("pg_class")).as("cl2");
                Table<?> pgNamespaceD = DSL.table(DSL.name("pg_namespace")).as("n3");
                Table<?> pgAttribute = DSL.table(DSL.name("pg_attribute")).as("a");
                Field<String> description = DSL.field(DSL.name("pgd", "description"), String.class);
                Field<Object> objoid = DSL.field(DSL.name("pgd", "objoid"));
                Field<Object> cl2Oid = DSL.field(DSL.name("cl2", "oid"));
                Field<Object> relnamespaceD = DSL.field(DSL.name("cl2", "relnamespace"));
                Field<Object> n3Oid = DSL.field(DSL.name("n3", "oid"));
                Field<String> n3Nspname = DSL.field(DSL.name("n3", "nspname"), String.class);
                Field<String> cl2Relname = DSL.field(DSL.name("cl2", "relname"), String.class);
                Field<Object> attrelid = DSL.field(DSL.name("a", "attrelid"));
                Field<Object> attnum = DSL.field(DSL.name("a", "attnum"));
                Field<Object> objsubid = DSL.field(DSL.name("pgd", "objsubid"));
                Field<String> attname = DSL.field(DSL.name("a", "attname"), String.class);

                String comment = ctx.select(description)
                    .from(pgDescription)
                    .join(pgClassD).on(cl2Oid.eq(objoid))
                    .join(pgNamespaceD).on(n3Oid.eq(relnamespaceD))
                    .join(pgAttribute).on(attrelid.eq(cl2Oid)).and(attnum.eq(objsubid))
                    .where(n3Nspname.eq("nexus"))
                    .and(cl2Relname.eq("topics"))
                    .and(attname.eq("doc_count"))
                    .fetchOne(description);
                assertThat(comment).as("doc_count must carry a COMMENT").isNotNull();
                assertThat(comment).contains("Trigger-maintained");
            }
        }
    }

    private static List<String> columnNames(DSLContext ctx, String schema, String table) {
        List<Table<?>> tables = ctx.meta()
            .filterSchemas(s -> s.getName().equals(schema))
            .filterTables(t -> t.getName().equals(table))
            .getTables();
        List<String> cols = new ArrayList<>();
        if (!tables.isEmpty()) {
            for (Field<?> f : tables.get(0).fields()) {
                cols.add(f.getName());
            }
        }
        return cols;
    }
}
