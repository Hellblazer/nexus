// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.List;

/**
 * RDR-229 P3.3b (nexus-p2m1x.20): what {@link LeafIndexPass} reads from the catalog, by what an index indexes and never by name
 * pattern. Typed jOOQ over {@code pg_class}, {@code pg_inherits}, {@code pg_index} and {@code pg_constraint}; no statement is sent
 * from here. A partitioned table here is list-partitioned by model and then by tenant (parent, model partitions, tenant leaves),
 * but the walk is by {@code relkind}, so a leaf directly under the parent is a leaf too.
 */
final class LeafIndexCatalog {

    private LeafIndexCatalog() { /* static */ }

    /** A relation: {@code schema.name}. */
    record Rel(String schema, String name) { }

    /** A node of the partition tree under a table: a relation and the relation it is a partition of. */
    record Node(Rel rel, Rel parent) { }

    /** The partition tree of one table: its model-level partitions (partitioned themselves) and its leaves, ordered by name. */
    record Tree(Rel parent, List<Node> models, List<Node> leaves) { }

    /**
     * One unique index on the id key. {@code constraint} is true when a UNIQUE constraint owns the index; {@code attachedTo} is the
     * name of the index it is attached to ({@code pg_inherits}), or null. The index lives in its table's schema.
     */
    record Idx(String name, boolean valid, boolean constraint, String attachedTo) { }

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

    /** True when {@code rel} exists as a partitioned table. */
    static boolean isPartitioned(DSLContext ctx, Rel rel) {
        return ctx.fetchExists(DSL.selectOne().from(CLASS.as("c"))
            .join(NAMESPACE.as("n")).on(col("n", "oid").eq(col("c", "relnamespace")))
            .where(text("n", "nspname").eq(rel.schema())).and(text("c", "relname").eq(rel.name()))
            .and(text("c", "relkind").eq(DSL.inline("p"))));
    }

    /** The children of {@code parent} (its partitions) with their relkind, ordered by name. */
    private static List<org.jooq.Record3<String, String, String>> children(DSLContext ctx, Rel parent) {
        var pc = CLASS.as("pc");
        var pn = NAMESPACE.as("pn");
        var i = INHERITS.as("i");
        var c = CLASS.as("c");
        var n = NAMESPACE.as("n");
        return ctx.select(text("n", "nspname"), text("c", "relname"), text("c", "relkind"))
            .from(pc)
            .join(pn).on(col("pn", "oid").eq(col("pc", "relnamespace")))
            .join(i).on(col("i", "inhparent").eq(col("pc", "oid")))
            .join(c).on(col("c", "oid").eq(col("i", "inhrelid")))
            .join(n).on(col("n", "oid").eq(col("c", "relnamespace")))
            .where(text("pn", "nspname").eq(parent.schema())).and(text("pc", "relname").eq(parent.name()))
            .orderBy(text("c", "relname"))
            .fetch();
    }

    /** The partition tree under {@code parent}: partitioned children are models (and their children leaves), any other child is a leaf. */
    static Tree tree(DSLContext ctx, Rel parent) {
        List<Node> models = new ArrayList<>();
        List<Node> leaves = new ArrayList<>();
        for (var child : children(ctx, parent)) {
            Rel rel = new Rel(child.value1(), child.value2());
            if ("p".equals(child.value3())) {
                models.add(new Node(rel, parent));
                for (var leaf : children(ctx, rel)) {
                    leaves.add(new Node(new Rel(leaf.value1(), leaf.value2()), rel));
                }
            } else {
                leaves.add(new Node(rel, parent));
            }
        }
        return new Tree(parent, List.copyOf(models), List.copyOf(leaves));
    }

    /**
     * The unique indexes of {@code rel} whose definition ends with {@code (columns)}, ordered by name: the id key, found by what it
     * indexes. {@code columns} is the comma-and-space list as {@code pg_get_indexdef} writes it.
     */
    static List<Idx> idIndexes(DSLContext ctx, Rel rel, String columns) {
        var t = CLASS.as("t");
        var tn = NAMESPACE.as("tn");
        var i = INDEX.as("i");
        var ic = CLASS.as("ic");
        Field<String> def = DSL.function(DSL.name("pg_catalog", "pg_get_indexdef"), String.class, col("i", "indexrelid"));
        Field<Boolean> owned = DSL.field(DSL.exists(DSL.selectOne().from(CONSTRAINT.as("k"))
            .where(col("k", "conindid").eq(col("i", "indexrelid"))).and(text("k", "contype").eq(DSL.inline("u")))));
        var ih = INHERITS.as("ih");
        var pc = CLASS.as("pc");
        Field<String> attached = DSL.field(DSL.select(text("pc", "relname")).from(ih)
            .join(pc).on(col("pc", "oid").eq(col("ih", "inhparent")))
            .where(col("ih", "inhrelid").eq(col("i", "indexrelid"))).limit(1));
        return ctx.select(text("ic", "relname"), DSL.field(DSL.name("i", "indisvalid"), Boolean.class), owned, attached)
            .from(i)
            .join(ic).on(col("ic", "oid").eq(col("i", "indexrelid")))
            .join(t).on(col("t", "oid").eq(col("i", "indrelid")))
            .join(tn).on(col("tn", "oid").eq(col("t", "relnamespace")))
            .where(text("tn", "nspname").eq(rel.schema())).and(text("t", "relname").eq(rel.name()))
            .and(DSL.field(DSL.name("i", "indisunique"), Boolean.class))
            .and(def.like("%(" + columns + ")"))
            .orderBy(text("ic", "relname"))
            .fetch(r -> new Idx(r.value1(), Boolean.TRUE.equals(r.value2()), Boolean.TRUE.equals(r.value3()), r.value4()));
    }

    /** The valid index among {@code indexes}, preferring one that already backs a constraint, or null. */
    static Idx valid(List<Idx> indexes) {
        Idx best = null;
        for (Idx idx : indexes) {
            if (idx.valid() && (best == null || idx.constraint() && !best.constraint())) {
                best = idx;
            }
        }
        return best;
    }
}
