// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.TenantConstants;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.List;
import java.util.TreeSet;

/**
 * The tenants a move runs for (RDR-246, Row-level security). It is the set the table-wide retire-keys routes check
 * ({@code ChunkKeysAdminHandler}): the default tenant, every tenant holding a {@code nexus.service_tokens} row (revoked
 * or expired included), and every tenant holding rows in the driving table, which one probe on the admin connection finds
 * ({@code ChunkConstraintAdmin}'s probe is the precedent). The worker builds the list at the start of each pass.
 */
final class MoveTenants {

    private MoveTenants() { /* static */ }

    private static final Field<String> TENANT_ID = DSL.field(DSL.name("tenant_id"), String.class);

    /** True when the driving table has a {@code tenant_id} column; a table without one is run once, under no tenant. */
    static boolean hasTenantColumn(DSLContext admin, String schema, String table) {
        var a = DSL.table(DSL.name("pg_catalog", "pg_attribute")).as("a");
        var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        return admin.fetchExists(DSL.selectOne().from(a)
            .join(c).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("a", "attrelid"))))
            .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(table))
            .and(DSL.field(DSL.name("a", "attname"), String.class).eq("tenant_id"))
            .and(DSL.field(DSL.name("a", "attnum"), Integer.class).gt(0))
            .and(DSL.field(DSL.name("a", "attisdropped"), Boolean.class).isFalse()));
    }

    /**
     * The tenants to run the move for: the default tenant first, then the rest in name order. The probe reads every
     * tenant's rows, so the admin session must see them all (the pass checks that before it builds the list).
     */
    static List<String> tenants(DSLContext admin, String schema, String table) {
        TreeSet<String> rest = new TreeSet<>();
        Table<?> tokens = DSL.table(DSL.name("nexus", "service_tokens"));
        rest.addAll(admin.selectDistinct(TENANT_ID).from(tokens).where(TENANT_ID.isNotNull()).fetch(TENANT_ID));
        rest.addAll(admin.selectDistinct(TENANT_ID).from(DSL.table(DSL.name(schema, table)))
            .where(TENANT_ID.isNotNull()).fetch(TENANT_ID));
        rest.remove(TenantConstants.DEFAULT_TENANT);
        List<String> out = new ArrayList<>(rest.size() + 1);
        out.add(TenantConstants.DEFAULT_TENANT);
        out.addAll(rest);
        return out;
    }
}
