// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.db.TenantConstants;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.TreeSet;

/**
 * The tenants a move runs for (RDR-246, Row-level security): the default tenant, every tenant holding a {@code nexus.service_tokens} row (revoked
 * or expired included), and every tenant that holds rows in the driving table. The rows are found by one probe on the admin
 * connection, bounded by {@link #PROBE_STATEMENT_TIMEOUT}; on a
 * table partitioned one tenant per leaf (nexus.chunks) the leaf bounds name the tenants, so no scan runs. The worker builds
 * the list at the start of each pass.
 */
final class MoveTenants {

    private MoveTenants() { /* static */ }

    /** The probe's statement bound. */
    static final String PROBE_STATEMENT_TIMEOUT = "10min";

    /** The GUC the probe bounds; set through {@code AdminDdlSession#setSession} only (NetworkBoundStatementTimeoutGateTest). */
    private static final String STATEMENT_TIMEOUT_GUC = "statement_timeout";

    private static final Field<String> TENANT_ID = DSL.field(DSL.name("tenant_id"), String.class);

    /** True when the driving table has a {@code tenant_id} column. */
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
     * The tenants to run the move for, found by the probe of the driving table: the default tenant first, then the rest in
     * name order. The probe reads every tenant's rows, so the admin session must see them all (the pass checks that before
     * it builds the list). The session's statement bound is {@link #PROBE_STATEMENT_TIMEOUT} for the probe, and what it was
     * before after it ({@code AdminDdlSession#setSession} is the one route to a statement bound outside PgSession).
     */
    static List<String> byProbe(AdminDdlSession session, String schema, String table) {
        DSLContext admin = session.ctx();
        String prior = admin.select(DSL.function("current_setting", SQLDataType.VARCHAR, DSL.val(STATEMENT_TIMEOUT_GUC)))
            .fetchOne(0, String.class);
        session.setSession(STATEMENT_TIMEOUT_GUC, PROBE_STATEMENT_TIMEOUT);
        try {
            return list(admin, admin.selectDistinct(TENANT_ID).from(DSL.table(DSL.name(schema, table)))
                .where(TENANT_ID.isNotNull()).fetch(TENANT_ID));
        } finally {
            session.setSession(STATEMENT_TIMEOUT_GUC, prior);
        }
    }

    /** The tenants to run the move for, taken from the leaf bounds of a table partitioned one tenant per leaf: no scan. */
    static List<String> byLeafBounds(DSLContext admin, Collection<String> boundValues) {
        return list(admin, boundValues);
    }

    private static List<String> list(DSLContext admin, Collection<String> found) {
        TreeSet<String> rest = new TreeSet<>(found);
        Table<?> tokens = DSL.table(DSL.name("nexus", "service_tokens"));
        rest.addAll(admin.selectDistinct(TENANT_ID).from(tokens).where(TENANT_ID.isNotNull()).fetch(TENANT_ID));
        rest.remove(TenantConstants.DEFAULT_TENANT);
        List<String> out = new ArrayList<>(rest.size() + 1);
        out.add(TenantConstants.DEFAULT_TENANT);
        out.addAll(rest);
        return out;
    }
}
