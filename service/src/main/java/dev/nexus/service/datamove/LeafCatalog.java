// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.List;

/**
 * The partition catalog of a move's driving table (RDR-246, The move worker step 2), read on the admin connection as
 * {@code nexus_admin}, which owns {@code nexus.partition_bound_value} (EXECUTE is revoked from PUBLIC, vectors-030).
 *
 * <p>A driving table that is list-partitioned by model and then by tenant, as {@code nexus.chunks} is, is never run
 * partition-wise: the move worker runs once per leaf that holds the tenant's rows, with the leaf as the driving table. The
 * tenant's leaves are the children of each model partition whose single-value bound is the tenant, found the way
 * {@code nexus.drop_tenant_partitions} finds them (vectors-030): from the catalog, never from a scan or a name pattern.
 */
final class LeafCatalog {

    private LeafCatalog() { /* static */ }

    /** A leaf partition: the relation a per-leaf run names as its driving table. */
    record Leaf(String schema, String name) { }

    /** A leaf and the tenant its single-value bound names. */
    record TenantLeaf(String tenant, Leaf leaf) { }

    private static final Table<?> CLASS = DSL.table(DSL.name("pg_catalog", "pg_class"));
    private static final Table<?> NAMESPACE = DSL.table(DSL.name("pg_catalog", "pg_namespace"));
    private static final Table<?> INHERITS = DSL.table(DSL.name("pg_catalog", "pg_inherits"));

    private static Field<Object> oid(String alias) {
        return DSL.field(DSL.name(alias, "oid"));
    }

    private static Field<String> text(String alias, String column) {
        return DSL.field(DSL.name(alias, column), String.class);
    }

    /** True when {@code schema.table} is a declarative partitioned parent ({@code relkind = 'p'}). */
    static boolean isPartitioned(DSLContext admin, String schema, String table) {
        return admin.fetchExists(DSL.selectOne().from(CLASS.as("c"))
            .join(NAMESPACE.as("n")).on(oid("n").eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(text("n", "nspname").eq(schema)).and(text("c", "relname").eq(table))
            .and(DSL.field(DSL.name("c", "relkind"), String.class).eq(DSL.inline("p"))));
    }

    /**
     * The leaves under the table's model partitions (the children of its children) whose single-value bound is
     * {@code tenant}, or, with a null {@code tenant}, every leaf that has one, tagged with its tenant. Ordered by model
     * partition, then leaf, so a pass visits them in one order.
     */
    static List<TenantLeaf> tenantLeaves(DSLContext admin, String schema, String table, String tenant) {
        var par = CLASS.as("par");
        var pn = NAMESPACE.as("pn");
        var i1 = INHERITS.as("i1");
        var mp = CLASS.as("mp");
        var i2 = INHERITS.as("i2");
        var lc = CLASS.as("lc");
        var ln = NAMESPACE.as("ln");
        Field<String> bound = DSL.function(DSL.name("nexus", "partition_bound_value"), SQLDataType.VARCHAR, oid("lc"));
        Condition tenantMatch = tenant == null ? bound.isNotNull() : bound.eq(tenant);
        return admin.select(bound, text("ln", "nspname"), text("lc", "relname"))
            .from(par)
            .join(pn).on(oid("pn").eq(DSL.field(DSL.name("par", "relnamespace"))))
            .join(i1).on(DSL.field(DSL.name("i1", "inhparent")).eq(oid("par")))
            .join(mp).on(oid("mp").eq(DSL.field(DSL.name("i1", "inhrelid"))))
            .join(i2).on(DSL.field(DSL.name("i2", "inhparent")).eq(oid("mp")))
            .join(lc).on(oid("lc").eq(DSL.field(DSL.name("i2", "inhrelid"))))
            .join(ln).on(oid("ln").eq(DSL.field(DSL.name("lc", "relnamespace"))))
            .where(text("pn", "nspname").eq(schema)).and(text("par", "relname").eq(table))
            .and(tenantMatch)
            .orderBy(text("mp", "relname"), text("lc", "relname"))
            .fetch(r -> new TenantLeaf(r.value1(), new Leaf(r.value2(), r.value3())));
    }

    /** The tenant-leaf mode: the table is partitioned and at least one leaf under a model partition names a tenant. */
    static boolean hasTenantLeaves(DSLContext admin, String schema, String table) {
        return isPartitioned(admin, schema, table) && !tenantLeaves(admin, schema, table, null).isEmpty();
    }

    /**
     * Every relation of the table's tree (the parent, its partitions and their leaves) that {@code owner} cannot yet read:
     * {@code dml_utils_owner}'s boundary scan reads the parent, and a per-leaf run names a leaf directly. A relation whose
     * SELECT the owner already holds is left out, so a pass that has nothing to grant sends nothing. Parent first.
     */
    static List<Leaf> relationsLackingSelect(DSLContext admin, String schema, String table, String owner) {
        var par = CLASS.as("par");
        var pn = NAMESPACE.as("pn");
        var rel = CLASS.as("rel");
        var rn = NAMESPACE.as("rn");
        var i1 = INHERITS.as("i1");
        var i2 = INHERITS.as("i2");
        Field<Boolean> lacks = DSL.not(DSL.function("has_table_privilege", SQLDataType.BOOLEAN,
            DSL.val(owner), oid("rel"), DSL.val("SELECT")));
        Condition inTree = oid("rel").eq(oid("par"))
            .or(DSL.exists(DSL.selectOne().from(i1).where(DSL.field(DSL.name("i1", "inhrelid")).eq(oid("rel")))
                .and(DSL.field(DSL.name("i1", "inhparent")).eq(oid("par")))))
            .or(DSL.exists(DSL.selectOne().from(i1).join(i2).on(DSL.field(DSL.name("i2", "inhparent"))
                    .eq(DSL.field(DSL.name("i1", "inhrelid"))))
                .where(DSL.field(DSL.name("i2", "inhrelid")).eq(oid("rel")))
                .and(DSL.field(DSL.name("i1", "inhparent")).eq(oid("par")))));
        Condition isParent = oid("rel").eq(oid("par"));
        return admin.select(text("rn", "nspname"), text("rel", "relname"))
            .from(par)
            .join(pn).on(oid("pn").eq(DSL.field(DSL.name("par", "relnamespace"))))
            .join(rel).on(inTree)
            .join(rn).on(oid("rn").eq(DSL.field(DSL.name("rel", "relnamespace"))))
            .where(text("pn", "nspname").eq(schema)).and(text("par", "relname").eq(table))
            .and(DSL.field(DSL.name("rel", "relkind"), String.class).in(DSL.inline("r"), DSL.inline("p")))
            .and(DSL.condition(lacks))
            .orderBy(DSL.when(isParent, 0).else_(1), text("rn", "nspname"), text("rel", "relname"))
            .fetch(r -> new Leaf(r.value1(), r.value2()));
    }
}
