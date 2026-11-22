// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.ArrayList;
import java.util.List;

/**
 * RDR-229 P3.3b (nexus-p2m1x.20): reads the unique id-key indexes of the two partitioned tables straight from the catalog, by
 * what they index and never by name, so a test of the pass and a test of the leaf helper agree on what "the id unique index"
 * is. The keys are the RDR's (item 6, "Switch"): {@code nexus.chunks (tenant_id, collection_id, chash, embedding_model)} and
 * {@code nexus.taxonomy_centroids (tenant_id, collection_id, topic_id, embedding_model)}, beside the name primary keys
 * ({@code collection} in place of {@code collection_id}), which are not matched. Both columns exist since P3.1
 * ({@code vectors-068}); P3.3a (nexus-p2m1x.19) adds foreign-key twins, not these key columns.
 *
 * <p>Typed jOOQ over {@code pg_class}, {@code pg_inherits}, {@code pg_index} and {@code pg_constraint} (the test tree holds no raw
 * SQL: {@code RawSqlGateTest}, {@code SqlStringLiteralGateTest}). The partition tree is walked by {@code pg_inherits} from the
 * table: level 0 the parent, level 1 its partitions, level 2 their partitions; a relation with no partition is a leaf. Relations and
 * indexes are named {@code schema.name}, unquoted (every name here is a plain lower-case identifier).
 */
public final class IdKeyIndexProbe {

    private IdKeyIndexProbe() { /* static */ }

    /** A table and the columns of its id key. */
    public record Key(String table, String columns) {
        String indexdefSuffix() {
            return "(" + columns + ")";
        }
    }

    public static final Key CHUNKS = new Key("chunks", "tenant_id, collection_id, chash, embedding_model");
    public static final Key CENTROIDS = new Key("taxonomy_centroids", "tenant_id, collection_id, topic_id, embedding_model");
    public static final List<Key> KEYS = List.of(CHUNKS, CENTROIDS);

    /**
     * One unique index on the key. {@code level} is the relation's depth in the partition tree: 0 the parent, 1 a model
     * partition, 2 a tenant leaf. {@code constraint} is the UNIQUE constraint the index backs, or null for a bare index.
     * {@code parentIndex} is the index this one is attached to ({@code pg_inherits}), or null.
     */
    public record Idx(String relation, int level, boolean leaf, String index, long indexOid, boolean valid,
                      String constraint, String parentIndex) { }

    /** A relation of the tree: its depth and whether it has no partition. */
    private record Rel(String schema, String name, int level, boolean leaf) {
        String qualified() {
            return schema + "." + name;
        }
    }

    private static final Table<?> CLASS = DSL.table(DSL.name("pg_catalog", "pg_class"));
    private static final Table<?> NAMESPACE = DSL.table(DSL.name("pg_catalog", "pg_namespace"));
    private static final Table<?> INHERITS = DSL.table(DSL.name("pg_catalog", "pg_inherits"));
    private static final Table<?> INDEX = DSL.table(DSL.name("pg_catalog", "pg_index"));
    private static final Table<?> CONSTRAINT = DSL.table(DSL.name("pg_catalog", "pg_constraint"));

    private static Field<Object> col(String alias, String column) {
        return DSL.field(DSL.name(alias, column));
    }

    private static Field<String> text(String alias, String column) {
        return DSL.field(DSL.name(alias, column), String.class);
    }

    /** The partitions of {@code schema.name}, as (schema, name), ordered by name. */
    private static List<String[]> children(DSLContext ctx, String schema, String name) {
        return ctx.select(text("n", "nspname"), text("c", "relname"))
            .from(CLASS.as("pc"))
            .join(NAMESPACE.as("pn")).on(col("pn", "oid").eq(col("pc", "relnamespace")))
            .join(INHERITS.as("i")).on(col("i", "inhparent").eq(col("pc", "oid")))
            .join(CLASS.as("c")).on(col("c", "oid").eq(col("i", "inhrelid")))
            .join(NAMESPACE.as("n")).on(col("n", "oid").eq(col("c", "relnamespace")))
            .where(text("pn", "nspname").eq(schema)).and(text("pc", "relname").eq(name))
            .orderBy(text("c", "relname"))
            .fetch(r -> new String[] {r.value1(), r.value2()});
    }

    /** The table's partition tree: the parent, then the partitions, then their partitions. */
    private static List<Rel> tree(DSLContext ctx, Key key) {
        List<Rel> out = new ArrayList<>();
        List<String[]> level1 = children(ctx, "nexus", key.table());
        out.add(new Rel("nexus", key.table(), 0, level1.isEmpty()));
        List<Rel> level2 = new ArrayList<>();
        for (String[] mid : level1) {
            List<String[]> leaves = children(ctx, mid[0], mid[1]);
            out.add(new Rel(mid[0], mid[1], 1, leaves.isEmpty()));
            for (String[] leaf : leaves) {
                level2.add(new Rel(leaf[0], leaf[1], 2, true));
            }
        }
        out.addAll(level2);
        return out;
    }

    /** One unique index of a relation whose definition ends with the key's columns. */
    private record Row(String schema, String index, long oid, boolean valid, String constraint, boolean constraintIsRoot,
                       String parentIndex) { }

    private static List<Row> uniqueIdIndexes(DSLContext ctx, Rel rel, Key key) {
        Field<String> def = DSL.function(DSL.name("pg_catalog", "pg_get_indexdef"), String.class, col("i", "indexrelid"));
        Field<String> conname = DSL.field(DSL.select(text("k", "conname")).from(CONSTRAINT.as("k"))
            .where(col("k", "conindid").eq(col("i", "indexrelid"))).and(text("k", "contype").eq(DSL.inline("u"))).limit(1));
        Field<Long> conparent = DSL.field(DSL.select(col("k2", "conparentid").cast(SQLDataType.BIGINT))
            .from(CONSTRAINT.as("k2"))
            .where(col("k2", "conindid").eq(col("i", "indexrelid"))).and(text("k2", "contype").eq(DSL.inline("u"))).limit(1));
        Field<String> parentIdx = DSL.field(DSL.select(DSL.concat(text("pn", "nspname"), DSL.inline("."), text("pc", "relname")))
            .from(INHERITS.as("ih"))
            .join(CLASS.as("pc")).on(col("pc", "oid").eq(col("ih", "inhparent")))
            .join(NAMESPACE.as("pn")).on(col("pn", "oid").eq(col("pc", "relnamespace")))
            .where(col("ih", "inhrelid").eq(col("i", "indexrelid"))).limit(1));
        return ctx.select(text("icn", "nspname"), text("ic", "relname"), col("i", "indexrelid").cast(SQLDataType.BIGINT),
                DSL.field(DSL.name("i", "indisvalid"), Boolean.class), conname, conparent, parentIdx)
            .from(INDEX.as("i"))
            .join(CLASS.as("ic")).on(col("ic", "oid").eq(col("i", "indexrelid")))
            .join(NAMESPACE.as("icn")).on(col("icn", "oid").eq(col("ic", "relnamespace")))
            .join(CLASS.as("t")).on(col("t", "oid").eq(col("i", "indrelid")))
            .join(NAMESPACE.as("tn")).on(col("tn", "oid").eq(col("t", "relnamespace")))
            .where(text("tn", "nspname").eq(rel.schema())).and(text("t", "relname").eq(rel.name()))
            .and(DSL.field(DSL.name("i", "indisunique"), Boolean.class))
            .and(def.like("%" + key.indexdefSuffix()))
            .orderBy(text("ic", "relname"))
            .fetch(r -> new Row(r.value1(), r.value2(), r.value3(), Boolean.TRUE.equals(r.value4()), r.value5(),
                r.value6() != null && r.value6() == 0L, r.value7()));
    }

    /** Every unique index on {@code key} in the table's partition tree, parent first. */
    public static List<Idx> indexes(DSLContext ctx, Key key) {
        List<Idx> out = new ArrayList<>();
        for (Rel rel : tree(ctx, key)) {
            for (Row row : uniqueIdIndexes(ctx, rel, key)) {
                out.add(new Idx(rel.qualified(), rel.level(), rel.leaf(), row.schema() + "." + row.index(), row.oid(), row.valid(),
                    row.constraint(), row.parentIndex()));
            }
        }
        return out;
    }

    /** The leaves of the table's tree, as {@code schema.name}, ordered. */
    public static List<String> leaves(DSLContext ctx, Key key) {
        return tree(ctx, key).stream().filter(r -> r.leaf() && r.level() > 0).map(Rel::qualified).sorted().toList();
    }

    /** The model partitions (level 1) of the table's tree, as {@code schema.name}, ordered. */
    public static List<String> modelPartitions(DSLContext ctx, Key key) {
        return tree(ctx, key).stream().filter(r -> r.level() == 1).map(Rel::qualified).sorted().toList();
    }

    /** The leaves of {@code key}'s table with no VALID unique index on the key. */
    public static List<String> leavesWithoutValidIndex(DSLContext ctx, Key key) {
        List<String> valid = indexes(ctx, key).stream().filter(i -> i.leaf() && i.valid()).map(Idx::relation).toList();
        return leaves(ctx, key).stream().filter(l -> !valid.contains(l)).toList();
    }

    /**
     * Removes every unique index on the keys from the trees, so each test starts from the state before P3.3b: the top-level
     * constraints first (dropping one drops the attached ones under it), then every bare index that is not attached to another.
     * A test that leaves a constraint on a parent would make every later leaf get an index from PostgreSQL itself.
     */
    @SuppressWarnings("deprecation")
    public static void reset(DSLContext su) {
        for (Key key : KEYS) {
            for (Rel rel : tree(su, key)) {   // parent first
                for (Row row : uniqueIdIndexes(su, rel, key)) {
                    if (row.constraint() != null && row.constraintIsRoot()) {
                        su.alterTable(DSL.table(DSL.name(rel.schema(), rel.name()))).dropConstraintIfExists(DSL.name(row.constraint()))
                            .execute();
                    }
                }
            }
            for (Rel rel : tree(su, key)) {
                for (Row row : uniqueIdIndexes(su, rel, key)) {
                    if (row.parentIndex() == null) {
                        // jOOQ renders dropIndex unqualified on POSTGRES (PgContainerHelper#dropIndexIfExists says so): the qualified name
                        // goes through nexus_test.drop_index(regclass), as the other tests' drops do
                        dev.nexus.service.jooq.test.Routines.dropIndex(su.configuration(), su.render(DSL.name(row.schema(), row.index())));
                    }
                }
            }
        }
    }
}
