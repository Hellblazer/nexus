// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;


/**
 * Ends the backends a stopped engine left behind, BEFORE the schema migration (only this database's and this role's: a backend of another database on the cluster, or of another role, is never signalled) (nexus-p2m1x.21; linda, 2026-11-23). A validation with no
 * statement bound can outlive the engine that started it: the client socket closing does not stop a running VALIDATE, and it holds ROW SHARE on
 * the collection registry and SHARE UPDATE EXCLUSIVE on its leaf for the rest of its run. A migration changeset that takes ACCESS EXCLUSIVE on
 * either would wait behind it and stall the boot. {@link DataMoveWorker#terminateOrphans} does the same after the migration; this runs
 * first. The caller holds the engine singleton lock, so no live engine owns one of these backends.
 */
public final class DataMoveOrphans {

    private static final Logger log = LoggerFactory.getLogger(DataMoveOrphans.class);

    private DataMoveOrphans() { /* static */ }

    /** Every move session's name starts with this (the worker names its registry session, so its completion transactions, with it). */
    public static final String PREFIX = DataMoveWorker.APPLICATION_NAME_PREFIX;

    /**
     * Cancels the statement a backend is running ({@code pg_cancel_backend}), from a connection of its own, after checking in the same
     * statement that the pid is still the backend it was: this database, this role, the same {@code application_name} and the same start. A
     * pid the server has reused for another session is left alone. Never throws.
     */
    static void cancelBackend(AdminConnection admin, int pid, String applicationName, java.time.OffsetDateTime backendStart) {
        try (AdminDdlSession session = openSession(admin, "cancel")) {
            var pidField = DSL.field(DSL.name("pid"), Integer.class);
            session.ctx().select(DSL.function("pg_cancel_backend", SQLDataType.BOOLEAN, pidField))
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(ours()).and(pidField.eq(pid))
                .and(DSL.field(DSL.name("application_name"), String.class).eq(applicationName))
                .and(DSL.field(DSL.name("backend_start"), java.time.OffsetDateTime.class).eq(backendStart))
                .fetch();
        } catch (RuntimeException e) {   // a refused session (the server is unreachable) or a failed statement
            log.warn("event=data_move_completion_cancel_failed pid={} cause=\"{}\"", pid, FailureHandler.oneLine(e.toString()));
        }
    }

    /**
     * A short session on the admin values, from the one sanctioned opener ({@link AdminDdlSession#open}, whose URL is the admin's), named
     * {@code nexus-data-move-<purpose>}. It takes no run lock and alters nothing. Throws {@link AdminDdlSession.Refused} when it cannot connect.
     */
    static AdminDdlSession openSession(AdminConnection admin, String purpose) {
        return AdminDdlSession.open(admin, new AdminDdlSession.Settings(PREFIX + purpose, 0L, "data move " + purpose, "nexus", "data_moves", "none"),
            false);
    }

    /** A backend of this database, run by the role this connection is: the ownership check before any signal (never a process that is not ours). */
    static org.jooq.Condition ours() {
        return DSL.field(DSL.name("datname"), String.class).eq(DSL.function("current_database", SQLDataType.VARCHAR))
            .and(DSL.field(DSL.name("usename"), String.class).eq(DSL.currentUser()));
    }

    /**
     * @return how many backends were ended; -1 when the database could not be reached (logged, never fatal: the boot goes on)
     */
    public static int terminateLeftover(AdminConnection admin) {
        try (AdminDdlSession session = openSession(admin, "boot-reaper")) {
            var ctx = session.ctx();
            var pid = DSL.field(DSL.name("pid"), Integer.class);
            var name = DSL.field(DSL.name("application_name"), String.class);
            var rows = ctx.select(pid, name, DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN, pid))
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(DSL.condition(DSL.function("starts_with", SQLDataType.BOOLEAN, name, DSL.val(PREFIX))))
                .and(ours())
                .and(pid.ne(DSL.function("pg_backend_pid", SQLDataType.INTEGER)))
                .fetch();
            for (var r : rows) {
                log.warn("event=data_move_leftover_ended pid={} application_name=\"{}\"", r.value1(), r.value2());
            }
            return rows.size();
        } catch (RuntimeException e) {
            log.warn("event=data_move_leftover_not_ended cause=\"{}\"", FailureHandler.oneLine(e.toString()));
            return -1;
        }
    }
}
