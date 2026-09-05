package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record2;
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
 * RDR-156 bead nexus-t1hnc.1 — pgvector taxonomy-centroid Liquibase schema test.
 *
 * <p>RDR-191 Phase 4 (bead nexus-jv3ue, taxonomy-007-unify-centroids.xml): the
 * three per-dim centroid tables this test originally verified
 * ({@code nexus.taxonomy_centroids_384/768/1024}) are unified into ONE
 * {@code nexus.taxonomy_centroids} table with three nullable typed
 * {@code embedding_<dim>} columns, mirroring {@code nexus.chunks}'s shape. This
 * test now verifies the UNIFIED table: one PK, one RLS policy, three per-dim
 * HNSW indexes on the three embedding columns.
 *
 * <p>Exact assertions, not existence-only: each HNSW index must use access method
 * {@code hnsw}, opclass {@code vector_cosine_ops}, and carry the
 * {@code m=16, ef_construction=64} reloptions — the centroid-ANN read path
 * (assign_single / compute_assignments parity) depends on cosine distance.
 *
 * <p>nexus-cbo4a batch 6 fold-in (Sam's no-raw-SQL-in-Java directive,
 * nexus-zrcj7): all 11 raw execute()/executeQuery() call sites are retired
 * onto typed jOOQ DSL. The DO-block CREATE ROLE + hand-rolled Liquibase ->
 * {@link PgContainerHelper#applyProductSchema}; information_schema/pg_catalog
 * reads -> {@code DSLContext#meta()} or typed
 * {@code DSL.table(DSL.name(...))}/{@code DSL.field(DSL.name(...), Class)}
 * composition -- same category and shape as the other three files converted
 * in this batch. The two sites that genuinely unnest a Postgres array
 * ({@code primaryKeyColumns}'s {@code unnest(con.conkey) WITH ORDINALITY},
 * {@code indexReloptions}'s {@code unnest(reloptions)}) use jOOQ's typed
 * {@code DSL.unnest(Field)}/{@code Table#withOrdinality()} plus
 * {@code Table#crossApply(TableLike)} (renders {@code CROSS JOIN LATERAL} on
 * Postgres) -- verified present in the pinned jOOQ 3.20.11 jar via javap,
 * correcting this file's initial (batch-6-draft) exclusion, which assumed no
 * typed form existed for either shape.
 */
class TaxonomyCentroidSchemaLiquibaseTest {

    private static final String TABLE = "taxonomy_centroids";

    /** dim -> embedding vector dimension; selects embedding_&lt;dim&gt;. */
    private static final List<Integer> DIMS = List.of(384, 768, 1024);

    @Test
    void centroidChangeset_appliesAndCreatesUnifiedTable() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.start()) {

            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.applyProductSchema(su);
            }

            try (Connection c = pg.createConnection("")) {
                DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);

                // Table exists in nexus schema
                boolean exists = !ctx.meta()
                    .filterSchemas(s -> s.getName().equals("nexus"))
                    .filterTables(t -> t.getName().equals(TABLE))
                    .getTables()
                    .isEmpty();
                assertThat(exists).as("table nexus." + TABLE + " must exist").isTrue();

                // Exact column set: three nullable embedding_<dim> columns, no chash
                // (taxonomy-007's own DIVERGENCE 1 note: centroids have no content-hash
                // concept at all).
                List<String> cols = columnNames(ctx, "nexus", TABLE);
                assertThat(cols).as("columns of nexus." + TABLE).containsExactlyInAnyOrder(
                    "tenant_id", "collection", "topic_id",
                    "embedding_384", "embedding_768", "embedding_1024",
                    "label", "doc_count", "created_at");

                // Primary key is (tenant_id, collection, topic_id) in order
                assertThat(primaryKeyColumns(ctx, "nexus", TABLE))
                    .as("PK of nexus." + TABLE)
                    .containsExactly("tenant_id", "collection", "topic_id");

                // exactly-one-embedding CHECK constraint present
                assertThat(constraintExists(ctx, "taxonomy_centroids_exactly_one_embedding"))
                    .as("taxonomy_centroids_exactly_one_embedding CHECK must exist").isTrue();

                for (int dim : DIMS) {
                    String column = "embedding_" + dim;
                    String index = "idx_taxonomy_centroids_embedding_" + dim;

                    // embedding_<dim> column is vector(dim)
                    assertThat(vectorDimension(ctx, "nexus", TABLE, column))
                        .as("dimension of nexus." + TABLE + "." + column).isEqualTo(dim);

                    // HNSW cosine index: access method, opclass, reloptions
                    assertThat(indexAccessMethod(ctx, index))
                        .as("access method of " + index).isEqualTo("hnsw");
                    assertThat(indexOpclass(ctx, index))
                        .as("opclass of " + index).isEqualTo("vector_cosine_ops");
                    List<String> reloptions = indexReloptions(ctx, index);
                    assertThat(reloptions).as("reloptions of " + index)
                        .contains("m=16", "ef_construction=64");
                }

                // RLS enabled + FORCED (once, on the unified table)
                Table<?> pgClass = DSL.table(DSL.name("pg_class"));
                Table<?> pgNamespace = DSL.table(DSL.name("pg_namespace"));
                Field<Boolean> relrowsecurity =
                    DSL.field(DSL.name("pg_class", "relrowsecurity"), Boolean.class);
                Field<Boolean> relforcerowsecurity =
                    DSL.field(DSL.name("pg_class", "relforcerowsecurity"), Boolean.class);
                Record2<Boolean, Boolean> rlsRow = ctx.select(relrowsecurity, relforcerowsecurity)
                    .from(pgClass)
                    .join(pgNamespace)
                        .on(DSL.field(DSL.name("pg_class", "relnamespace"))
                            .eq(DSL.field(DSL.name("pg_namespace", "oid"))))
                    .where(DSL.field(DSL.name("pg_class", "relname"), String.class).eq(TABLE))
                    .and(DSL.field(DSL.name("pg_namespace", "nspname"), String.class).eq("nexus"))
                    .fetchOne();
                assertThat(rlsRow).as("pg_class entry for " + TABLE).isNotNull();
                assertThat(rlsRow.value1()).as("RLS enabled on nexus." + TABLE).isTrue();
                assertThat(rlsRow.value2()).as("RLS forced on nexus." + TABLE).isTrue();

                // tenant_isolation policy present (once, on the unified table)
                boolean policyExists = ctx.fetchExists(DSL.table(DSL.name("pg_policies")),
                    DSL.field(DSL.name("schemaname"), String.class).eq("nexus")
                        .and(DSL.field(DSL.name("tablename"), String.class).eq(TABLE))
                        .and(DSL.field(DSL.name("policyname"), String.class).eq("tenant_isolation")));
                assertThat(policyExists)
                    .as("tenant_isolation policy on nexus." + TABLE).isTrue();
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

    /** pgvector stores the declared dimension in atttypmod (no -4 adjustment for vector). */
    private static int vectorDimension(DSLContext ctx, String schema, String table, String column) {
        Table<?> pgAttribute = DSL.table(DSL.name("pg_attribute")).as("a");
        Table<?> pgClass = DSL.table(DSL.name("pg_class")).as("cl");
        Table<?> pgNamespace = DSL.table(DSL.name("pg_namespace")).as("n");
        Field<Integer> atttypmod = DSL.field(DSL.name("a", "atttypmod"), Integer.class);

        Integer value = ctx.select(atttypmod)
            .from(pgAttribute)
            .join(pgClass).on(DSL.field(DSL.name("a", "attrelid")).eq(DSL.field(DSL.name("cl", "oid"))))
            .join(pgNamespace)
                .on(DSL.field(DSL.name("cl", "relnamespace")).eq(DSL.field(DSL.name("n", "oid"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("cl", "relname"), String.class).eq(table))
            .and(DSL.field(DSL.name("a", "attname"), String.class).eq(column))
            .fetchOne(atttypmod);
        assertThat(value).as("atttypmod row for " + table + "." + column).isNotNull();
        return value;
    }

    /** Ordered by key position via the conkey array. {@code con.conkey} is a
     * Postgres {@code int2[]} (smallint array); {@link DSL#unnest(Field)} +
     * {@link Table#withOrdinality()} render the same
     * {@code unnest(...) WITH ORDINALITY} the raw SQL used, and
     * {@link Table#crossApply(org.jooq.TableLike)} renders
     * {@code CROSS JOIN LATERAL} on Postgres (the standard-SQL form of the
     * original's {@code JOIN LATERAL ... ON true}). */
    private static List<String> primaryKeyColumns(DSLContext ctx, String schema, String table) {
        Table<?> pgConstraint = DSL.table(DSL.name("pg_constraint")).as("con");
        Table<?> pgClass = DSL.table(DSL.name("pg_class")).as("cl");
        Table<?> pgNamespace = DSL.table(DSL.name("pg_namespace")).as("n");
        Table<?> pgAttribute = DSL.table(DSL.name("pg_attribute")).as("a");

        Field<Short[]> conkey = DSL.field(DSL.name("con", "conkey"), Short[].class);
        Table<?> k = DSL.unnest(conkey).withOrdinality().as("k", "attnum", "ord");
        Field<Short> kAttnum = DSL.field(DSL.name("k", "attnum"), Short.class);
        Field<Long> kOrd = DSL.field(DSL.name("k", "ord"), Long.class);
        Field<String> attname = DSL.field(DSL.name("a", "attname"), String.class);

        return ctx.select(attname)
            .from(pgConstraint
                .join(pgClass)
                    .on(DSL.field(DSL.name("con", "conrelid")).eq(DSL.field(DSL.name("cl", "oid"))))
                .join(pgNamespace)
                    .on(DSL.field(DSL.name("cl", "relnamespace")).eq(DSL.field(DSL.name("n", "oid"))))
                .crossApply(k)
                .join(pgAttribute)
                    .on(DSL.field(DSL.name("a", "attrelid")).eq(DSL.field(DSL.name("cl", "oid"))))
                    .and(DSL.field(DSL.name("a", "attnum"), Short.class).eq(kAttnum)))
            .where(DSL.field(DSL.name("con", "contype"), String.class).eq("p"))
            .and(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("cl", "relname"), String.class).eq(table))
            .orderBy(kOrd)
            .fetch(attname);
    }

    private static boolean constraintExists(DSLContext ctx, String conname) {
        return ctx.fetchExists(DSL.table(DSL.name("pg_constraint")),
            DSL.field(DSL.name("conname"), String.class).eq(conname));
    }

    private static String indexAccessMethod(DSLContext ctx, String index) {
        Table<?> pgClass = DSL.table(DSL.name("pg_class")).as("i");
        Table<?> pgAm = DSL.table(DSL.name("pg_am")).as("am");
        Field<String> amname = DSL.field(DSL.name("am", "amname"), String.class);

        String value = ctx.select(amname)
            .from(pgClass)
            .join(pgAm).on(DSL.field(DSL.name("i", "relam")).eq(DSL.field(DSL.name("am", "oid"))))
            .where(DSL.field(DSL.name("i", "relname"), String.class).eq(index))
            .fetchOne(amname);
        assertThat(value).as("index " + index + " must exist").isNotNull();
        return value;
    }

    /** {@code pg_index.indclass} is a Postgres {@code oidvector} -- a system-
     * catalog pseudo-array type, 0-INDEXED by internal Postgres convention
     * (unlike a normal array, which defaults to 1-based). {@link
     * DSL#arrayGet(Field, int)} passes its {@code int} argument straight
     * through into the rendered {@code (...)[index]} subscript with no
     * normalization (jOOQ's own docs: "these values are passed directly to
     * the SQL engine without interpretation") -- {@code arrayGet(indclass, 0)}
     * therefore renders the exact {@code indclass[0]} the raw SQL used, not
     * jOOQ's own 1-based array convention. The element type is left as
     * {@code Object[]}/{@code Object} (matching this file's other untyped
     * oid comparisons) since the subscripted value is never fetched
     * directly -- only compared against {@code pg_opclass.oid} inside the
     * JOIN condition -- so no JDBC array marshaling of {@code oidvector}
     * (which has no standard array read support) is ever needed. */
    private static String indexOpclass(DSLContext ctx, String index) {
        Table<?> pgIndex = DSL.table(DSL.name("pg_index")).as("ix");
        Table<?> pgClass = DSL.table(DSL.name("pg_class")).as("i");
        Table<?> pgOpclass = DSL.table(DSL.name("pg_opclass")).as("opc");
        Field<String> opcname = DSL.field(DSL.name("opc", "opcname"), String.class);
        Field<Object[]> indclass = DSL.field(DSL.name("ix", "indclass"), Object[].class);
        Field<Object> indclass0 = DSL.arrayGet(indclass, 0);

        String value = ctx.select(opcname)
            .from(pgIndex)
            .join(pgClass)
                .on(DSL.field(DSL.name("ix", "indexrelid")).eq(DSL.field(DSL.name("i", "oid"))))
            .join(pgOpclass).on(DSL.field(DSL.name("opc", "oid")).eq(indclass0))
            .where(DSL.field(DSL.name("i", "relname"), String.class).eq(index))
            .fetchOne(opcname);
        assertThat(value).as("opclass row for " + index).isNotNull();
        return value;
    }

    /** {@code reloptions} is a Postgres {@code text[]}; {@link DSL#unnest(Field)}
     * + {@link Table#crossApply(org.jooq.TableLike)} render the same
     * {@code unnest(reloptions)} the raw SQL used, without WITH ORDINALITY
     * (the original selected only the unnested value, never the position). */
    private static List<String> indexReloptions(DSLContext ctx, String index) {
        Table<?> pgClass = DSL.table(DSL.name("pg_class"));
        Field<String[]> reloptions = DSL.field(DSL.name("reloptions"), String[].class);
        Table<?> opt = DSL.unnest(reloptions).as("opt");
        Field<String> optField = DSL.field(DSL.name("opt"), String.class);

        return ctx.select(optField)
            .from(pgClass.crossApply(opt))
            .where(DSL.field(DSL.name("relname"), String.class).eq(index))
            .fetch(optField);
    }
}
