// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.db.DmlUtilsInstaller;
import org.jooq.DSLContext;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.Optional;

/**
 * Step 1 of a pass (RDR-246, The move worker): grants what the libraries need on the driving table, then checks that
 * dml_utils is installed, that the admin connection reads every row of the driving table, and that a
 * {@code max_worker_processes} slot is free. Each miss is a reason the move is {@code blocked:} with; none is permanent.
 */
final class MovePreflight {

    private static final Logger log = LoggerFactory.getLogger(MovePreflight.class);

    private MovePreflight() { /* static */ }

    /** The end of the refusal's "Point NX_DB_ADMIN_* at ..." sentence: the role that reads every row of this table. */
    private static final String VIEW_POINT_AT = "the role that owns the driving table and carries its owner-read SELECT "
        + "policy (a migrating role, never the serving role)";

    /**
     * The backend types that do not take a {@code max_worker_processes} slot; every other row of
     * {@code pg_stat_activity} (a parallel worker, a logical replication worker, an extension's worker such as
     * pg_background's) is a background worker holding one.
     */
    private static final List<String> NO_SLOT = List.of("client backend", "autovacuum launcher", "autovacuum worker",
        "background writer", "checkpointer", "archiver", "startup", "walreceiver", "walsender", "walwriter",
        "standalone backend", "walsummarizer", "io worker");

    /**
     * Runs the preflight on {@code session}, which names the move's driving table.
     *
     * @return the reason the move is blocked, or empty when the pass may go on to the runs
     */
    static Optional<String> blockedReason(AdminDdlSession session, DataMove move, DmlUtilsInstaller installer) {
        Optional<String> install = installer.status().blockedReason();
        if (install.isPresent()) {
            return install;
        }
        DSLContext ctx = session.ctx();
        try {
            grant(session, move);
            if (!committingEntryPointInstalled(ctx)) {
                return Optional.of("dml_utils.run_migration_chunks_committing is not installed");
            }
            if (rowSecurityNarrowsAdmin(ctx, move)) {
                try {
                    session.requireUnrestrictedView(VIEW_POINT_AT);
                } catch (AdminDdlSession.Refused refused) {
                    return Optional.of(refused.getMessage());
                }
            }
            if (freeWorkerSlots(ctx) < 1) {
                return Optional.of("waiting for a background worker slot");
            }
        } catch (AdminDdlSession.Refused refused) {
            return Optional.of("the pass could not grant dml_utils_owner its access to "
                + move.drivingTableLabel() + ": " + refused.getMessage());
        } catch (DataAccessException e) {
            return Optional.of("a preflight check on " + move.drivingTableLabel() + " failed: sqlstate=" + e.sqlState()
                + " " + FailureHandler.oneLine(e.getMessage()));
        }
        return Optional.empty();
    }

    /**
     * Grants {@code dml_utils_owner} USAGE on the driving schema and SELECT on the parent and every existing leaf, only
     * when the role exists and only where it does not already hold the privilege. Its SECURITY DEFINER boundary scan reads
     * the table, and a per-leaf run names a leaf directly. Not left to the registering changeset, which runs once and may
     * run before the bootstrap created the role. A leaf created later gets the parent's grants from the partition grant
     * loop ({@code nexus.partition_copy_access}).
     */
    static void grant(AdminDdlSession session, DataMove move) {
        DSLContext ctx = session.ctx();
        String owner = DmlUtilsInstaller.OWNER_ROLE;
        boolean roleExists = ctx.fetchExists(DSL.selectOne().from(DSL.table(DSL.name("pg_catalog", "pg_roles")))
            .where(DSL.field(DSL.name("rolname"), String.class).eq(owner)));
        if (!roleExists) {
            return;
        }
        boolean schemaUsage = Boolean.TRUE.equals(ctx.select(DSL.function("has_schema_privilege", SQLDataType.BOOLEAN,
                DSL.val(owner), DSL.val(move.drivingSchema()), DSL.val("USAGE"))).fetchOne(0, Boolean.class));
        if (!schemaUsage) {
            session.ddl("GRANT USAGE ON SCHEMA %I TO %I", List.of(move.drivingSchema(), owner));
            log.info("event=data_move_grant label={} object=schema:{} role={}", move.label(), move.drivingSchema(), owner);
        }
        for (LeafCatalog.Leaf relation : LeafCatalog.relationsLackingSelect(ctx, move.drivingSchema(),
                move.drivingTable(), owner)) {
            session.ddl("GRANT SELECT ON TABLE %I.%I TO %I", List.of(relation.schema(), relation.name(), owner));
            log.info("event=data_move_grant label={} object={}.{} role={}", move.label(), relation.schema(),
                relation.name(), owner);
        }
    }

    /**
     * Whether row-level security can hide rows from the admin session. A table without row security, or one the session
     * owns that is not FORCE, is read whole by its owner; only then is {@code requireUnrestrictedView}'s test for a
     * permissive true-qual policy, a superuser or BYPASSRLS the one that applies. (That method asks for one of the three
     * whatever the table's state, which is right for {@code nexus.chunks}, always FORCE, and would block every move over a
     * table with no row security.) A table that does not exist is treated as narrowing, so the check says why it fails.
     */
    static boolean rowSecurityNarrowsAdmin(DSLContext ctx, DataMove move) {
        var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        var row = ctx.select(DSL.field(DSL.name("c", "relrowsecurity"), Boolean.class),
                DSL.field(DSL.name("c", "relforcerowsecurity"), Boolean.class),
                DSL.function("pg_has_role", SQLDataType.BOOLEAN, DSL.currentUser(),
                    DSL.field(DSL.name("c", "relowner")), DSL.inline("USAGE")))
            .from(c).join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(move.drivingSchema()))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(move.drivingTable()))
            .fetchOne();
        if (row == null) {
            return true;
        }
        boolean enabled = Boolean.TRUE.equals(row.value1());
        boolean forced = Boolean.TRUE.equals(row.value2());
        boolean owned = Boolean.TRUE.equals(row.value3());
        return enabled && (forced || !owned);
    }

    static boolean committingEntryPointInstalled(DSLContext ctx) {
        var p = DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        return ctx.fetchExists(DSL.selectOne().from(p)
            .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("dml_utils"))
            .and(DSL.field(DSL.name("p", "proname"), String.class).eq("run_migration_chunks_committing")));
    }

    /** {@code max_worker_processes} less the background workers running now. */
    static int freeWorkerSlots(DSLContext ctx) {
        Integer max = ctx.select(DSL.cast(DSL.function("current_setting", SQLDataType.VARCHAR,
            DSL.val("max_worker_processes")), SQLDataType.INTEGER)).fetchOne(0, Integer.class);
        Integer used = ctx.selectCount().from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("backend_type"), String.class).notIn(NO_SLOT)).fetchOne(0, Integer.class);
        return (max == null ? 0 : max) - (used == null ? 0 : used);
    }
}
