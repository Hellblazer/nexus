// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.util.Collection;
import java.util.List;

/**
 * Typed jOOQ probes over {@code information_schema} and {@code pg_catalog} for
 * schema-assertion tests (nexus-cbo4a batch 7, Sam's no-raw-SQL rule
 * nexus-zrcj7). Neither catalog is in jOOQ codegen scope, so every probe here
 * is built from {@code DSLContext#meta()} or {@code DSL.table(DSL.name(...))} /
 * {@code DSL.field(DSL.name(...), Class)} references -- the idioms batch 6
 * established per file, hoisted into one place so the shape is written once.
 * Result-string semantics are preserved exactly where a test asserts on the
 * catalog's own text ({@code information_schema.columns.data_type},
 * {@code pg_indexes.indexdef}, {@code pg_policies.qual}, ...): those read the
 * same view/column the raw SQL read; only existence/name probes go through
 * {@code Meta}.
 */
public final class PgCatalogProbes {

    private PgCatalogProbes() {
    }

    // ── table / column shape (Meta + information_schema) ─────────────────

    /** Column of {@code information_schema.columns} a test asserts on. */
    public record ColumnInfo(String dataType, String isNullable, String columnDefault, String udtName) {
        public boolean nullable() {
            return "YES".equals(isNullable);
        }
    }

    /**
     * Does {@code schema.table} exist? Covers views too: jOOQ's JDBC-backed
     * {@code Meta} asks the driver for TABLE and VIEW (and materialized view)
     * relation types by default, which ReadShapeViewsTest's view-existence
     * assertions exercise against live Postgres; a jOOQ upgrade that narrowed
     * that default would surface there first.
     */
    public static boolean tableExists(DSLContext ctx, String schema, String table) {
        return !ctx.meta()
            .filterSchemas(s -> s.getName().equals(schema))
            .filterTables(t -> t.getName().equals(table))
            .getTables()
            .isEmpty();
    }

    /** Does {@code schema.view} exist as a VIEW ({@code information_schema.views})? */
    public static boolean viewExists(DSLContext ctx, String schema, String view) {
        return ctx.fetchExists(DSL.table(DSL.name("information_schema", "views")),
            DSL.field(DSL.name("table_schema"), String.class).eq(schema)
                .and(DSL.field(DSL.name("table_name"), String.class).eq(view)));
    }

    /**
     * Ordinal-ordered column names of {@code schema.table} (table or view) from
     * {@code information_schema.columns}; empty when the relation is absent.
     */
    public static List<String> columnNames(DSLContext ctx, String schema, String table) {
        Field<String> columnName = DSL.field(DSL.name("column_name"), String.class);
        return ctx.select(columnName)
            .from(DSL.table(DSL.name("information_schema", "columns")))
            .where(DSL.field(DSL.name("table_schema"), String.class).eq(schema))
            .and(DSL.field(DSL.name("table_name"), String.class).eq(table))
            .orderBy(DSL.field(DSL.name("ordinal_position"), Integer.class))
            .fetch(columnName);
    }

    public static boolean columnExists(DSLContext ctx, String schema, String table, String column) {
        return columnInfo(ctx, schema, table, column) != null;
    }

    /** {@code information_schema.columns} row for one column, or {@code null} when absent. */
    public static ColumnInfo columnInfo(DSLContext ctx, String schema, String table, String column) {
        Field<String> dataType = DSL.field(DSL.name("data_type"), String.class);
        Field<String> isNullable = DSL.field(DSL.name("is_nullable"), String.class);
        Field<String> columnDefault = DSL.field(DSL.name("column_default"), String.class);
        Field<String> udtName = DSL.field(DSL.name("udt_name"), String.class);
        Record r = ctx.select(dataType, isNullable, columnDefault, udtName)
            .from(DSL.table(DSL.name("information_schema", "columns")))
            .where(DSL.field(DSL.name("table_schema"), String.class).eq(schema))
            .and(DSL.field(DSL.name("table_name"), String.class).eq(table))
            .and(DSL.field(DSL.name("column_name"), String.class).eq(column))
            .fetchOne();
        return r == null ? null
            : new ColumnInfo(r.get(dataType), r.get(isNullable), r.get(columnDefault), r.get(udtName));
    }

    /** {@code pg_tables.tablename} for every table in {@code schema}. */
    public static List<String> tablesInSchema(DSLContext ctx, String schema) {
        Field<String> tablename = DSL.field(DSL.name("tablename"), String.class);
        return ctx.select(tablename)
            .from(DSL.table(DSL.name("pg_tables")))
            .where(DSL.field(DSL.name("schemaname"), String.class).eq(schema))
            .fetch(tablename);
    }

    // ── pg_class / pg_attribute ──────────────────────────────────────────

    private static Table<?> pgClassAs(String alias) {
        return DSL.table(DSL.name("pg_class")).as(alias);
    }

    private static Table<?> pgNamespaceAs(String alias) {
        return DSL.table(DSL.name("pg_namespace")).as(alias);
    }

    /** {@code (relrowsecurity, relforcerowsecurity)} for {@code schema.table}. */
    public record RowSecurity(boolean enabled, boolean forced) {
    }

    /** RLS flags of {@code schema.table}, or {@code null} when the relation is absent. */
    public static RowSecurity rowSecurity(DSLContext ctx, String schema, String table) {
        Field<Boolean> rls = DSL.field(DSL.name("c", "relrowsecurity"), Boolean.class);
        Field<Boolean> forced = DSL.field(DSL.name("c", "relforcerowsecurity"), Boolean.class);
        Record r = ctx.select(rls, forced)
            .from(pgClassAs("c"))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("c", "relnamespace")).eq(DSL.field(DSL.name("n", "oid"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(table))
            .fetchOne();
        return r == null ? null : new RowSecurity(r.get(rls), r.get(forced));
    }

    /** {@code pg_class.relpersistence} ('p' permanent, 'u' unlogged, 't' temp), or {@code null}. */
    public static String relPersistence(DSLContext ctx, String schema, String table) {
        Field<String> relpersistence = DSL.field(DSL.name("c", "relpersistence"), String.class);
        return ctx.select(relpersistence)
            .from(pgClassAs("c"))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("c", "relnamespace")).eq(DSL.field(DSL.name("n", "oid"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(table))
            .fetchOne(relpersistence);
    }

    /** {@code pg_class.reloptions} of {@code schema.relation} ({@code text[]}, may be null). */
    public static String[] relOptions(DSLContext ctx, String schema, String relation) {
        Field<String[]> reloptions = DSL.field(DSL.name("c", "reloptions"), String[].class);
        return ctx.select(reloptions)
            .from(pgClassAs("c"))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("c", "relnamespace")).eq(DSL.field(DSL.name("n", "oid"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(relation))
            .fetchOne(reloptions);
    }

    private static Condition attributeOf(String schema, String table, String column) {
        return DSL.field(DSL.name("n", "nspname"), String.class).eq(schema)
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(table))
            .and(DSL.field(DSL.name("a", "attname"), String.class).eq(column));
    }

    /** {@code pg_attribute.attnotnull} for one column, or {@code null} when absent. */
    public static Boolean columnNotNull(DSLContext ctx, String schema, String table, String column) {
        Field<Boolean> attnotnull = DSL.field(DSL.name("a", "attnotnull"), Boolean.class);
        return ctx.select(attnotnull)
            .from(DSL.table(DSL.name("pg_attribute")).as("a"))
            .join(pgClassAs("c")).on(DSL.field(DSL.name("a", "attrelid")).eq(DSL.field(DSL.name("c", "oid"))))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("c", "relnamespace")).eq(DSL.field(DSL.name("n", "oid"))))
            .where(attributeOf(schema, table, column))
            .fetchOne(attnotnull);
    }

    /** {@code (attgenerated, format_type(atttypid, atttypmod))} for one live column. */
    public record GeneratedColumn(String attgenerated, String colType) {
    }

    /** Generated-column flag + rendered type for one live (non-dropped, user) column, or null. */
    public static GeneratedColumn generatedColumn(DSLContext ctx, String schema, String table, String column) {
        Field<String> attgenerated = DSL.field(DSL.name("a", "attgenerated"), String.class);
        Field<String> colType = DSL.function("format_type", String.class,
            DSL.field(DSL.name("a", "atttypid")), DSL.field(DSL.name("a", "atttypmod")));
        Record r = ctx.select(attgenerated, colType)
            .from(DSL.table(DSL.name("pg_attribute")).as("a"))
            .join(pgClassAs("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("a", "attrelid"))))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(attributeOf(schema, table, column))
            .and(DSL.field(DSL.name("a", "attnum"), Short.class).gt((short) 0))
            .and(DSL.field(DSL.name("a", "attisdropped"), Boolean.class).isFalse())
            .fetchOne();
        return r == null ? null : new GeneratedColumn(r.get(attgenerated), r.get(colType));
    }

    /** {@code pg_get_expr(adbin, adrelid)} -- the column's default/generation expression, or null. */
    public static String columnExpression(DSLContext ctx, String schema, String table, String column) {
        Field<String> expr = DSL.function("pg_get_expr", String.class,
            DSL.field(DSL.name("d", "adbin")), DSL.field(DSL.name("d", "adrelid")));
        return ctx.select(expr)
            .from(DSL.table(DSL.name("pg_attrdef")).as("d"))
            .join(DSL.table(DSL.name("pg_attribute")).as("a"))
                .on(DSL.field(DSL.name("a", "attrelid")).eq(DSL.field(DSL.name("d", "adrelid"))))
                .and(DSL.field(DSL.name("a", "attnum")).eq(DSL.field(DSL.name("d", "adnum"))))
            .join(pgClassAs("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("d", "adrelid"))))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(attributeOf(schema, table, column))
            .fetchOne(expr);
    }

    /** Number of live columns of {@code schema.table} whose {@code pg_type.typname} is {@code typname}. */
    public static int columnCountOfType(DSLContext ctx, String schema, String table, String typname) {
        return ctx.fetchCount(
            DSL.table(DSL.name("pg_attribute")).as("a")
                .join(pgClassAs("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("a", "attrelid"))))
                .join(pgNamespaceAs("n"))
                    .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .join(DSL.table(DSL.name("pg_type")).as("t"))
                    .on(DSL.field(DSL.name("t", "oid")).eq(DSL.field(DSL.name("a", "atttypid")))),
            DSL.field(DSL.name("n", "nspname"), String.class).eq(schema)
                .and(DSL.field(DSL.name("c", "relname"), String.class).eq(table))
                .and(DSL.field(DSL.name("t", "typname"), String.class).eq(typname))
                .and(DSL.field(DSL.name("a", "attnum"), Short.class).gt((short) 0))
                .and(DSL.field(DSL.name("a", "attisdropped"), Boolean.class).isFalse()));
    }

    /** {@code COMMENT ON COLUMN} text via {@code pg_description}, or null when none. */
    public static String columnComment(DSLContext ctx, String schema, String table, String column) {
        Field<String> description = DSL.field(DSL.name("pgd", "description"), String.class);
        return ctx.select(description)
            .from(DSL.table(DSL.name("pg_description")).as("pgd"))
            .join(pgClassAs("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("pgd", "objoid"))))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .join(DSL.table(DSL.name("pg_attribute")).as("a"))
                .on(DSL.field(DSL.name("a", "attrelid")).eq(DSL.field(DSL.name("c", "oid"))))
                .and(DSL.field(DSL.name("a", "attnum")).eq(DSL.field(DSL.name("pgd", "objsubid"))))
            .where(attributeOf(schema, table, column))
            .fetchOne(description);
    }

    // ── pg_policies ──────────────────────────────────────────────────────

    /** One {@code pg_policies} row. */
    public record Policy(String policyname, String cmd, String qual, String withCheck) {
    }

    public static List<Policy> policies(DSLContext ctx, String schema, String table) {
        Field<String> policyname = DSL.field(DSL.name("policyname"), String.class);
        Field<String> cmd = DSL.field(DSL.name("cmd"), String.class);
        Field<String> qual = DSL.field(DSL.name("qual"), String.class);
        Field<String> withCheck = DSL.field(DSL.name("with_check"), String.class);
        return ctx.select(policyname, cmd, qual, withCheck)
            .from(DSL.table(DSL.name("pg_policies")))
            .where(DSL.field(DSL.name("schemaname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("tablename"), String.class).eq(table))
            .fetch(r -> new Policy(r.get(policyname), r.get(cmd), r.get(qual), r.get(withCheck)));
    }

    public static boolean policyExists(DSLContext ctx, String schema, String table, String policyname) {
        return ctx.fetchExists(DSL.table(DSL.name("pg_policies")),
            DSL.field(DSL.name("schemaname"), String.class).eq(schema)
                .and(DSL.field(DSL.name("tablename"), String.class).eq(table))
                .and(DSL.field(DSL.name("policyname"), String.class).eq(policyname)));
    }

    // ── pg_indexes / pg_index ────────────────────────────────────────────

    /** {@code pg_indexes.indexdef} of {@code schema.indexname}, or null when absent. */
    public static String indexDef(DSLContext ctx, String schema, String indexname) {
        Field<String> indexdef = DSL.field(DSL.name("indexdef"), String.class);
        return ctx.select(indexdef)
            .from(DSL.table(DSL.name("pg_indexes")))
            .where(DSL.field(DSL.name("schemaname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("indexname"), String.class).eq(indexname))
            .fetchOne(indexdef);
    }

    /** Every {@code pg_indexes.indexdef} on {@code schema.table}. */
    public static List<String> indexDefs(DSLContext ctx, String schema, String table) {
        Field<String> indexdef = DSL.field(DSL.name("indexdef"), String.class);
        return ctx.select(indexdef)
            .from(DSL.table(DSL.name("pg_indexes")))
            .where(DSL.field(DSL.name("schemaname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("tablename"), String.class).eq(table))
            .fetch(indexdef);
    }

    public static boolean indexExists(DSLContext ctx, String schema, String indexname) {
        return ctx.fetchExists(DSL.table(DSL.name("pg_indexes")),
            DSL.field(DSL.name("schemaname"), String.class).eq(schema)
                .and(DSL.field(DSL.name("indexname"), String.class).eq(indexname)));
    }

    /** {@code pg_index.indisvalid} for the index named {@code indexname}, or null when absent. */
    public static Boolean indexIsValid(DSLContext ctx, String indexname) {
        Field<Boolean> indisvalid = DSL.field(DSL.name("i", "indisvalid"), Boolean.class);
        return ctx.select(indisvalid)
            .from(DSL.table(DSL.name("pg_index")).as("i"))
            .join(pgClassAs("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("i", "indexrelid"))))
            .where(DSL.field(DSL.name("c", "relname"), String.class).eq(indexname))
            .fetchOne(indisvalid);
    }

    /** {@code (pg_get_indexdef(indexrelid), pg_am.amname)} for one index. */
    public record IndexShape(String indexdef, String amname) {
    }

    public static IndexShape indexShape(DSLContext ctx, String indexname) {
        Field<String> indexdef = DSL.function("pg_get_indexdef", String.class,
            DSL.field(DSL.name("i", "indexrelid")));
        Field<String> amname = DSL.field(DSL.name("am", "amname"), String.class);
        Record r = ctx.select(indexdef, amname)
            .from(DSL.table(DSL.name("pg_index")).as("i"))
            .join(pgClassAs("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("i", "indexrelid"))))
            .join(DSL.table(DSL.name("pg_am")).as("am"))
                .on(DSL.field(DSL.name("am", "oid")).eq(DSL.field(DSL.name("c", "relam"))))
            .where(DSL.field(DSL.name("c", "relname"), String.class).eq(indexname))
            .fetchOne();
        return r == null ? null : new IndexShape(r.get(indexdef), r.get(amname));
    }

    /**
     * Number of indexes on {@code schema.table} using access method {@code amname}
     * that cover column {@code column} ({@code attnum = ANY(pg_index.indkey)}).
     */
    public static int indexCountOnColumn(DSLContext ctx, String schema, String table,
                                         String amname, String column) {
        Field<Short[]> indkey = DSL.field(DSL.name("ix", "indkey"), Short[].class);
        return ctx.fetchCount(
            DSL.table(DSL.name("pg_index")).as("ix")
                .join(pgClassAs("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("ix", "indrelid"))))
                .join(pgClassAs("i")).on(DSL.field(DSL.name("i", "oid")).eq(DSL.field(DSL.name("ix", "indexrelid"))))
                .join(pgNamespaceAs("n"))
                    .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .join(DSL.table(DSL.name("pg_am")).as("am"))
                    .on(DSL.field(DSL.name("am", "oid")).eq(DSL.field(DSL.name("i", "relam"))))
                .join(DSL.table(DSL.name("pg_attribute")).as("a"))
                    .on(DSL.field(DSL.name("a", "attrelid")).eq(DSL.field(DSL.name("c", "oid"))))
                    .and(DSL.field(DSL.name("a", "attnum"), Short.class).eq(DSL.any(indkey))),
            DSL.field(DSL.name("n", "nspname"), String.class).eq(schema)
                .and(DSL.field(DSL.name("c", "relname"), String.class).eq(table))
                .and(DSL.field(DSL.name("am", "amname"), String.class).eq(amname))
                .and(DSL.field(DSL.name("a", "attname"), String.class).eq(column)));
    }

    // ── pg_constraint ────────────────────────────────────────────────────
    //
    // Schema qualification is deliberately MIXED in this section, each probe
    // matching the raw SQL it replaced: constraintExists / constraintValidated /
    // constraintCountLike look up a BARE conname across every schema (constraint
    // names are unique per table, not per database, so two schemas may each carry
    // a constraint of the same name -- the callers today probe names that occur
    // once), while foreignKey / foreignKeyDeleteActions / constraintCountByType
    // join pg_namespace and are scoped to one schema. Pick the qualified form for
    // any new caller whose name could recur across nexus / staging / t1.

    /** The FK-relevant columns of one {@code pg_constraint} row. */
    public record Constraint(boolean convalidated, boolean condeferrable, boolean condeferred,
                             String confupdtype, String confdeltype) {
    }

    /** Any constraint named {@code conname} (schema-agnostic, as the raw probes were). */
    public static boolean constraintExists(DSLContext ctx, String conname) {
        return ctx.fetchExists(DSL.table(DSL.name("pg_constraint")),
            DSL.field(DSL.name("conname"), String.class).eq(conname));
    }

    /** {@code pg_constraint.convalidated} for the constraint named {@code conname}, or null. */
    public static Boolean constraintValidated(DSLContext ctx, String conname) {
        Field<Boolean> convalidated = DSL.field(DSL.name("convalidated"), Boolean.class);
        return ctx.select(convalidated)
            .from(DSL.table(DSL.name("pg_constraint")))
            .where(DSL.field(DSL.name("conname"), String.class).eq(conname))
            .fetchOne(convalidated);
    }

    /** The FOREIGN KEY constraint {@code conname} in {@code schema}, or null when absent. */
    public static Constraint foreignKey(DSLContext ctx, String schema, String conname) {
        Field<Boolean> convalidated = DSL.field(DSL.name("c", "convalidated"), Boolean.class);
        Field<Boolean> condeferrable = DSL.field(DSL.name("c", "condeferrable"), Boolean.class);
        Field<Boolean> condeferred = DSL.field(DSL.name("c", "condeferred"), Boolean.class);
        Field<String> confupdtype = DSL.field(DSL.name("c", "confupdtype"), String.class);
        Field<String> confdeltype = DSL.field(DSL.name("c", "confdeltype"), String.class);
        Record r = ctx.select(convalidated, condeferrable, condeferred, confupdtype, confdeltype)
            .from(DSL.table(DSL.name("pg_constraint")).as("c"))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "connamespace"))))
            .where(DSL.field(DSL.name("c", "contype"), String.class).eq("f"))
            .and(DSL.field(DSL.name("c", "conname"), String.class).eq(conname))
            .and(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .fetchOne();
        return r == null ? null : new Constraint(r.get(convalidated), r.get(condeferrable),
            r.get(condeferred), r.get(confupdtype), r.get(confdeltype));
    }

    /** {@code confdeltype} of every FOREIGN KEY declared on {@code schema.table}. */
    public static List<String> foreignKeyDeleteActions(DSLContext ctx, String schema, String table) {
        Field<String> confdeltype = DSL.field(DSL.name("c", "confdeltype"), String.class);
        return ctx.select(confdeltype)
            .from(DSL.table(DSL.name("pg_constraint")).as("c"))
            .join(pgClassAs("t")).on(DSL.field(DSL.name("t", "oid")).eq(DSL.field(DSL.name("c", "conrelid"))))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("t", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("t", "relname"), String.class).eq(table))
            .and(DSL.field(DSL.name("c", "contype"), String.class).eq("f"))
            .fetch(confdeltype);
    }

    // ── pg_proc / routines ───────────────────────────────────────────────

    private static Condition routineIn(String schema) {
        return DSL.field(DSL.name("n", "nspname"), String.class).eq(schema);
    }

    private static Table<?> pgProcJoined() {
        return DSL.table(DSL.name("pg_proc")).as("p")
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))));
    }

    public static boolean routineExists(DSLContext ctx, String schema, String name) {
        return ctx.fetchExists(pgProcJoined(),
            routineIn(schema).and(DSL.field(DSL.name("p", "proname"), String.class).eq(name)));
    }

    /** Distinct-by-row {@code proname}s in {@code schema} matching {@code likePattern}, sorted. */
    public static List<String> routineNamesLike(DSLContext ctx, String schema, String... likePatterns) {
        Field<String> proname = DSL.field(DSL.name("p", "proname"), String.class);
        Condition any = DSL.falseCondition();
        for (String p : likePatterns) {
            any = any.or(proname.like(p));
        }
        return ctx.select(proname)
            .from(pgProcJoined())
            .where(routineIn(schema))
            .and(any)
            .orderBy(proname)
            .fetch(proname);
    }

    /**
     * {@code pg_proc.prosecdef} (SECURITY DEFINER) of ONE overload of {@code name},
     * or null when absent. The raw SQL this replaced (ManifestFunctionsTest,
     * ManifestVerifyTest, UpdatedAtTriggerTest) was {@code ... LIMIT 1} with no
     * ORDER BY, and so is this: for an OVERLOADED function name the row chosen is
     * whichever Postgres returns first, so the answer is only meaningful when every
     * overload shares the same security mode, or the name has a single overload
     * (true of every caller today). Add an argument-signature filter before
     * probing an overloaded name.
     */
    public static Boolean routineSecurityDefiner(DSLContext ctx, String schema, String name) {
        Field<Boolean> prosecdef = DSL.field(DSL.name("p", "prosecdef"), Boolean.class);
        return ctx.select(prosecdef)
            .from(pgProcJoined())
            .where(routineIn(schema))
            .and(DSL.field(DSL.name("p", "proname"), String.class).eq(name))
            .limit(1)
            .fetchOne(prosecdef);
    }

    /** {@code (pg_get_function_arguments, pg_get_function_result)} of one routine. */
    public record RoutineSignature(String arguments, String result) {
    }

    public static RoutineSignature routineSignature(DSLContext ctx, String schema, String name) {
        Field<String> args = DSL.function("pg_get_function_arguments", String.class,
            DSL.field(DSL.name("p", "oid")));
        Field<String> result = DSL.function("pg_get_function_result", String.class,
            DSL.field(DSL.name("p", "oid")));
        Record r = ctx.select(args, result)
            .from(pgProcJoined())
            .where(routineIn(schema))
            .and(DSL.field(DSL.name("p", "proname"), String.class).eq(name))
            .fetchOne();
        return r == null ? null : new RoutineSignature(r.get(args), r.get(result));
    }

    /** {@code pg_get_functiondef(to_regprocedure(signature))}; null when the signature resolves to nothing. */
    public static String routineDefinition(DSLContext ctx, String signature) {
        Field<String> def = DSL.function("pg_get_functiondef", String.class,
            DSL.function("to_regprocedure", Object.class, DSL.val(signature)));
        return ctx.select(def).fetchOne(def);
    }

    // ── pg_roles ─────────────────────────────────────────────────────────

    /** {@code (rolsuper, rolbypassrls, rolinherit)} of one role. */
    public record RoleFlags(boolean superuser, boolean bypassRls, boolean inherit) {
    }

    public static RoleFlags roleFlags(DSLContext ctx, Field<String> rolname) {
        Field<Boolean> rolsuper = DSL.field(DSL.name("rolsuper"), Boolean.class);
        Field<Boolean> rolbypassrls = DSL.field(DSL.name("rolbypassrls"), Boolean.class);
        Field<Boolean> rolinherit = DSL.field(DSL.name("rolinherit"), Boolean.class);
        Record r = ctx.select(rolsuper, rolbypassrls, rolinherit)
            .from(DSL.table(DSL.name("pg_roles")))
            .where(DSL.field(DSL.name("rolname"), String.class).eq(rolname))
            .fetchOne();
        return r == null ? null : new RoleFlags(r.get(rolsuper), r.get(rolbypassrls), r.get(rolinherit));
    }

    public static RoleFlags roleFlags(DSLContext ctx, String rolname) {
        return roleFlags(ctx, DSL.val(rolname));
    }

    /** Flags of the connection's {@code current_user}. */
    public static RoleFlags currentRoleFlags(DSLContext ctx) {
        return roleFlags(ctx, DSL.currentUser());
    }

    /** {@code pg_has_role(role, member, 'member')}. */
    public static boolean hasRole(DSLContext ctx, String role, String member) {
        Field<Boolean> f = DSL.function("pg_has_role", Boolean.class,
            DSL.val(role), DSL.val(member), DSL.inline("member"));
        return Boolean.TRUE.equals(ctx.select(f).fetchOne(f));
    }

    // ── pg_stat_* / pg_locks ─────────────────────────────────────────────

    /** {@code (last_vacuum, last_analyze)} from {@code pg_stat_user_tables}. */
    public record TableStats(java.time.OffsetDateTime lastVacuum, java.time.OffsetDateTime lastAnalyze) {
    }

    public static TableStats tableStats(DSLContext ctx, String schema, String table) {
        Field<java.time.OffsetDateTime> lastVacuum =
            DSL.field(DSL.name("last_vacuum"), java.time.OffsetDateTime.class);
        Field<java.time.OffsetDateTime> lastAnalyze =
            DSL.field(DSL.name("last_analyze"), java.time.OffsetDateTime.class);
        Record r = ctx.select(lastVacuum, lastAnalyze)
            .from(DSL.table(DSL.name("pg_stat_user_tables")))
            .where(DSL.field(DSL.name("schemaname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("relname"), String.class).eq(table))
            .fetchOne();
        return r == null ? null : new TableStats(r.get(lastVacuum), r.get(lastAnalyze));
    }

    /** {@code pg_stat_user_tables.relname} of every table in {@code schema} with a recorded ANALYZE. */
    public static List<String> analyzedTables(DSLContext ctx, String schema) {
        Field<String> relname = DSL.field(DSL.name("relname"), String.class);
        return ctx.select(relname)
            .from(DSL.table(DSL.name("pg_stat_user_tables")))
            .where(DSL.field(DSL.name("schemaname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("last_analyze")).isNotNull())
            .fetch(relname);
    }

    /** {@code pg_stat_reset_single_table_counters(oid)} for {@code schema.table}. */
    public static void resetTableCounters(DSLContext ctx, String schema, String table) {
        Field<Object> reset = DSL.function("pg_stat_reset_single_table_counters", Object.class,
            DSL.field(DSL.name("c", "oid")));
        ctx.select(reset)
            .from(pgClassAs("c"))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(table))
            .fetch();
    }

    public static int backendCount(DSLContext ctx, String applicationName) {
        return ctx.fetchCount(DSL.table(DSL.name("pg_stat_activity")),
            DSL.field(DSL.name("application_name"), String.class).eq(applicationName));
    }

    public static List<Integer> activeBackendPids(DSLContext ctx, String applicationName) {
        Field<Integer> pid = DSL.field(DSL.name("pid"), Integer.class);
        return ctx.select(pid)
            .from(DSL.table(DSL.name("pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class).eq(applicationName))
            .and(DSL.field(DSL.name("state"), String.class).eq("active"))
            .fetch(pid);
    }

    public static boolean backendExists(DSLContext ctx, int pid) {
        return ctx.fetchExists(DSL.table(DSL.name("pg_stat_activity")),
            DSL.field(DSL.name("pid"), Integer.class).eq(pid));
    }

    /** {@code pg_terminate_backend(pid)} for every other backend on {@code datname}. */
    public static void terminateOtherBackends(DSLContext ctx, String datname) {
        Field<Integer> pid = DSL.field(DSL.name("pid"), Integer.class);
        Field<Boolean> terminate = DSL.function("pg_terminate_backend", Boolean.class, pid);
        ctx.select(terminate)
            .from(DSL.table(DSL.name("pg_stat_activity")))
            .where(DSL.field(DSL.name("datname"), String.class).eq(datname))
            .and(pid.ne(DSL.function("pg_backend_pid", Integer.class)))
            .fetch();
    }

    /** Number of ungranted advisory locks cluster-wide ({@code pg_locks}). */
    public static int advisoryLockWaiters(DSLContext ctx) {
        return ctx.fetchCount(DSL.table(DSL.name("pg_locks")),
            DSL.field(DSL.name("locktype"), String.class).eq("advisory")
                .and(DSL.field(DSL.name("granted"), Boolean.class).isFalse()));
    }

    /** {@code count(*) FROM pg_class WHERE relname = ?} -- a cheap, plan-cacheable probe. */
    public static int pgClassRowsNamed(DSLContext ctx, String relname) {
        return ctx.fetchCount(DSL.table(DSL.name("pg_class")),
            DSL.field(DSL.name("relname"), String.class).eq(relname));
    }

    // ── whole-schema snapshots (rollback / rehearsal harnesses) ──────────

    /** Number of constraints (any schema) whose {@code conname LIKE pattern}. */
    public static int constraintCountLike(DSLContext ctx, String pattern) {
        return ctx.fetchCount(DSL.table(DSL.name("pg_constraint")),
            DSL.field(DSL.name("conname"), String.class).like(pattern));
    }

    /** Number of constraints of {@code contype} ('c','f','p','u',...) declared on {@code schema.table}. */
    public static int constraintCountByType(DSLContext ctx, String schema, String table, String contype) {
        return ctx.fetchCount(
            DSL.table(DSL.name("pg_constraint")).as("c")
                .join(pgClassAs("t")).on(DSL.field(DSL.name("t", "oid")).eq(DSL.field(DSL.name("c", "conrelid"))))
                .join(pgNamespaceAs("n"))
                    .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("t", "relnamespace")))),
            DSL.field(DSL.name("n", "nspname"), String.class).eq(schema)
                .and(DSL.field(DSL.name("t", "relname"), String.class).eq(table))
                .and(DSL.field(DSL.name("c", "contype"), String.class).eq(contype)));
    }

    /**
     * How many of the named {@code schema.table} relations currently carry
     * {@code relforcerowsecurity} -- the "FORCE ROW LEVEL SECURITY restored on
     * every toggled table" pin the migration rehearsals make after each
     * NO FORCE / FORCE toggle-wrapped changeset. A relation that does not exist
     * counts as not forced.
     */
    public static int forcedRowSecurityCount(DSLContext ctx, String... qualifiedTables) {
        int forced = 0;
        for (String qualified : qualifiedTables) {
            String[] parts = qualified.split("\\.", 2);
            RowSecurity rls = rowSecurity(ctx, parts[0], parts[1]);
            if (rls != null && rls.forced()) {
                forced++;
            }
        }
        return forced;
    }

    /** {@code pg_get_userbyid(pg_class.relowner)} of {@code schema.relation}, or null when absent. */
    public static String relationOwner(DSLContext ctx, String schema, String relation) {
        Field<String> owner = DSL.function("pg_get_userbyid", String.class, DSL.field(DSL.name("c", "relowner")));
        return ctx.select(owner)
            .from(pgClassAs("c"))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("c", "relnamespace")).eq(DSL.field(DSL.name("n", "oid"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(relation))
            .fetchOne(owner);
    }

    /** Installed extension names ({@code pg_extension.extname}), sorted. */
    public static List<String> extensionNames(DSLContext ctx) {
        Field<String> extname = DSL.field(DSL.name("extname"), String.class);
        return ctx.select(extname).from(DSL.table(DSL.name("pg_extension"))).orderBy(extname).fetch(extname);
    }

    /** One {@code pg_indexes} row. */
    public record IndexRow(String schema, String table, String indexname, String indexdef) {
    }

    public static List<IndexRow> indexesIn(DSLContext ctx, Collection<String> schemas) {
        Field<String> schemaname = DSL.field(DSL.name("schemaname"), String.class);
        Field<String> tablename = DSL.field(DSL.name("tablename"), String.class);
        Field<String> indexname = DSL.field(DSL.name("indexname"), String.class);
        Field<String> indexdef = DSL.field(DSL.name("indexdef"), String.class);
        return ctx.select(schemaname, tablename, indexname, indexdef)
            .from(DSL.table(DSL.name("pg_indexes")))
            .where(schemaname.in(schemas))
            .fetch(r -> new IndexRow(r.get(schemaname), r.get(tablename), r.get(indexname), r.get(indexdef)));
    }

    /** A generated column's stored expression ({@code pg_get_expr(adbin, adrelid)}). */
    public record GeneratedExpression(String schema, String table, String column, String expression) {
    }

    /** Every generated column ({@code attgenerated <> ''}) in {@code schemas} with its expression. */
    public static List<GeneratedExpression> generatedExpressionsIn(DSLContext ctx, Collection<String> schemas) {
        Field<String> nspname = DSL.field(DSL.name("n", "nspname"), String.class);
        Field<String> relname = DSL.field(DSL.name("cl", "relname"), String.class);
        Field<String> attname = DSL.field(DSL.name("a", "attname"), String.class);
        Field<String> expr = DSL.function("pg_get_expr", String.class,
            DSL.field(DSL.name("d", "adbin")), DSL.field(DSL.name("d", "adrelid")));
        return ctx.select(nspname, relname, attname, expr)
            .from(DSL.table(DSL.name("pg_attrdef")).as("d"))
            .join(pgClassAs("cl")).on(DSL.field(DSL.name("cl", "oid")).eq(DSL.field(DSL.name("d", "adrelid"))))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("cl", "relnamespace"))))
            .join(DSL.table(DSL.name("pg_attribute")).as("a"))
                .on(DSL.field(DSL.name("a", "attrelid")).eq(DSL.field(DSL.name("d", "adrelid"))))
                .and(DSL.field(DSL.name("a", "attnum")).eq(DSL.field(DSL.name("d", "adnum"))))
            .where(nspname.in(schemas))
            .and(DSL.field(DSL.name("a", "attgenerated"), String.class).ne(""))
            .fetch(r -> new GeneratedExpression(r.get(nspname), r.get(relname), r.get(attname), r.get(expr)));
    }

    /** One constraint with its rendered definition ({@code pg_get_constraintdef}). */
    public record ConstraintDefinition(String schema, String table, String conname, String definition) {
    }

    public static List<ConstraintDefinition> constraintDefinitionsIn(DSLContext ctx, Collection<String> schemas) {
        Field<String> nspname = DSL.field(DSL.name("n", "nspname"), String.class);
        Field<String> relname = DSL.field(DSL.name("cl", "relname"), String.class);
        Field<String> conname = DSL.field(DSL.name("con", "conname"), String.class);
        Field<String> def = DSL.function("pg_get_constraintdef", String.class, DSL.field(DSL.name("con", "oid")));
        return ctx.select(nspname, relname, conname, def)
            .from(DSL.table(DSL.name("pg_constraint")).as("con"))
            .join(pgClassAs("cl")).on(DSL.field(DSL.name("cl", "oid")).eq(DSL.field(DSL.name("con", "conrelid"))))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("cl", "relnamespace"))))
            .where(nspname.in(schemas))
            .fetch(r -> new ConstraintDefinition(r.get(nspname), r.get(relname), r.get(conname), r.get(def)));
    }

    /**
     * One {@code information_schema.role_table_grants} row, joined to the granted
     * relation's {@code pg_class.relkind} ('r' table, 'p' partitioned, 'v' view, ...)
     * so callers can separate base-table grants from view grants.
     */
    public record TableGrant(String grantee, String privilege, String schema, String table, String relkind) {
    }

    /** Every table-level grant in {@code schemas}, all grantees (PUBLIC and the current user included). */
    public static List<TableGrant> tableGrantsIn(DSLContext ctx, Collection<String> schemas) {
        Field<String> grantee = DSL.field(DSL.name("g", "grantee"), String.class);
        Field<String> privilege = DSL.field(DSL.name("g", "privilege_type"), String.class);
        Field<String> schema = DSL.field(DSL.name("g", "table_schema"), String.class);
        Field<String> table = DSL.field(DSL.name("g", "table_name"), String.class);
        Field<String> relkind = DSL.field(DSL.name("cl", "relkind"), String.class);
        return ctx.select(grantee, privilege, schema, table, relkind)
            .from(DSL.table(DSL.name("information_schema", "role_table_grants")).as("g"))
            .join(pgNamespaceAs("n")).on(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .join(pgClassAs("cl")).on(DSL.field(DSL.name("cl", "relname"), String.class).eq(table))
                .and(DSL.field(DSL.name("cl", "relnamespace")).eq(DSL.field(DSL.name("n", "oid"))))
            .where(schema.in(schemas))
            .fetch(r -> new TableGrant(r.get(grantee), r.get(privilege), r.get(schema), r.get(table), r.get(relkind)));
    }

    /** One {@code pg_policies} row with its schema and table. */
    public record PolicyRow(String schema, String table, String policyname, String qual, String withCheck) {
    }

    public static List<PolicyRow> policiesIn(DSLContext ctx, Collection<String> schemas) {
        Field<String> schemaname = DSL.field(DSL.name("schemaname"), String.class);
        Field<String> tablename = DSL.field(DSL.name("tablename"), String.class);
        Field<String> policyname = DSL.field(DSL.name("policyname"), String.class);
        Field<String> qual = DSL.field(DSL.name("qual"), String.class);
        Field<String> withCheck = DSL.field(DSL.name("with_check"), String.class);
        return ctx.select(schemaname, tablename, policyname, qual, withCheck)
            .from(DSL.table(DSL.name("pg_policies")))
            .where(schemaname.in(schemas))
            .fetch(r -> new PolicyRow(r.get(schemaname), r.get(tablename), r.get(policyname),
                r.get(qual), r.get(withCheck)));
    }

    /** RLS flags of one ordinary table ({@code relkind = 'r'}). */
    public record RowSecurityRow(String schema, String table, boolean enabled, boolean forced) {
    }

    public static List<RowSecurityRow> rowSecurityIn(DSLContext ctx, Collection<String> schemas) {
        Field<String> nspname = DSL.field(DSL.name("n", "nspname"), String.class);
        Field<String> relname = DSL.field(DSL.name("cl", "relname"), String.class);
        Field<Boolean> rls = DSL.field(DSL.name("cl", "relrowsecurity"), Boolean.class);
        Field<Boolean> forced = DSL.field(DSL.name("cl", "relforcerowsecurity"), Boolean.class);
        return ctx.select(nspname, relname, rls, forced)
            .from(pgClassAs("cl"))
            .join(pgNamespaceAs("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("cl", "relnamespace"))))
            .where(nspname.in(schemas))
            .and(DSL.field(DSL.name("cl", "relkind"), String.class).eq("r"))
            .fetch(r -> new RowSecurityRow(r.get(nspname), r.get(relname), r.get(rls), r.get(forced)));
    }

    /** One {@code information_schema.columns} row with its schema and table. */
    public record ColumnRow(String schema, String table, String column, String dataType, String isNullable) {
    }

    public static List<ColumnRow> columnsIn(DSLContext ctx, Collection<String> schemas) {
        Field<String> schema = DSL.field(DSL.name("table_schema"), String.class);
        Field<String> table = DSL.field(DSL.name("table_name"), String.class);
        Field<String> column = DSL.field(DSL.name("column_name"), String.class);
        Field<String> dataType = DSL.field(DSL.name("data_type"), String.class);
        Field<String> isNullable = DSL.field(DSL.name("is_nullable"), String.class);
        return ctx.select(schema, table, column, dataType, isNullable)
            .from(DSL.table(DSL.name("information_schema", "columns")))
            .where(schema.in(schemas))
            .fetch(r -> new ColumnRow(r.get(schema), r.get(table), r.get(column), r.get(dataType), r.get(isNullable)));
    }
}
