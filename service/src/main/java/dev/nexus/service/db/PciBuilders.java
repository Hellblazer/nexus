// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;

import java.util.List;

/**
 * The per-collection index builder's backends (RDR-227), found and ended in one place so the two callers that must end
 * them share one query: the migration walk ({@link SchemaMigrator}) and the data-move worker's {@code on_complete} on a
 * chunk-family table (RDR-246, Finishing a move; nexus-f3yqd.2.14).
 *
 * <p>Terminate, never cancel: a cancel ends the statement, not the pass, and the pass would start its next build in the
 * middle of the caller's work. Ending the session ends the pass ({@code PciBuilderSession} reads {@code 57P01} and
 * {@code 08xxx} as the end of the pass) and releases the builder's advisory lock; the interrupted build leaves an INVALID
 * index that a later pass drops and rebuilds. Scope: this database only, and not the calling session. Failure to read or
 * signal is logged at ERROR and swallowed: the builder is optional, and a caller that died because it could not end one
 * would turn an optional feature into an outage.
 *
 * <p>The per-leaf unique-index pass (RDR-229 P3.3b, {@code LeafIndexPass}) names its backend under the same prefix on purpose (CR-S1),
 * so the one termination ends it with no second list to keep; the same query therefore also finds it, and {@link #count} and
 * {@link #terminate} count and end it with the builders. It is not a per-collection builder: it takes no {@code NX_SEARCH_PCI}
 * setting and runs with PCI off.
 *
 * <p>Every line is logged through the caller's logger, never this class's: each caller's events (and the tests that
 * capture them, {@code PciBuilderTerminationIntegrationTest} and {@code PciReconcilerIntegrationTest} on
 * {@code SchemaMigrator}) belong to the caller's log, as they did before the extraction.
 */
public final class PciBuilders {

    /**
     * The {@code application_name} prefix of the per-collection index builder's backend. Equal to
     * {@code PciBuilderSession.APPLICATION_NAME_PREFIX}; held here too because {@code db} does not depend on
     * {@code vectors}, and {@code PciBuilderSessionIntegrationTest}/{@code PciBuilderTerminationIntegrationTest} pin the
     * two together through the real backend's name.
     */
    static final String APPLICATION_NAME_PREFIX = "nexus-pci-builder-";

    private PciBuilders() { /* static */ }

    /** Builder-named backends of this database other than the calling session. */
    static Condition backends() {
        return backends(null);
    }

    /** {@link #backends()} with the columns qualified by {@code alias} ({@code null}: unqualified), for a join that has a second {@code pid}. */
    private static Condition backends(String alias) {
        var applicationName = DSL.field(column(alias, "application_name"), String.class);
        var datname = DSL.field(column(alias, "datname"), String.class);
        return applicationName.startsWith(APPLICATION_NAME_PREFIX)
            .and(DSL.field(column(alias, "pid"), Integer.class).ne(DSL.function("pg_backend_pid", SQLDataType.INTEGER)))
            .and(datname.eq(DSL.function("current_database", SQLDataType.VARCHAR)));
    }

    private static org.jooq.Name column(String alias, String name) {
        return alias == null ? DSL.name(name) : DSL.name(alias, name);
    }

    /**
     * The builder backends that are running {@code CREATE INDEX} or {@code REINDEX} now ({@code count}, {@code -1} when they
     * cannot be read, with no pids): what {@link #building} finds. A builder-named session that holds a lock, counts leaves or
     * waits for its next pass is not among them.
     */
    public record Building(int count, List<Integer> pids) {
        public Building {
            pids = List.copyOf(pids);
        }

        public static Building unreadable() {
            return new Building(-1, List.of());
        }

        public boolean isUnreadable() {
            return count < 0;
        }
    }

    /**
     * The builder backends of this database (other than the calling session) that appear in {@code pg_stat_progress_create_index}
     * now: a build in flight, as against {@link #count}'s every builder-named session (a reconcile pass holds its session through
     * leaf counting and canaries). A build that began after the read is not in it; a caller that must not miss one rereads after
     * the failure the build causes.
     *
     * @param log the caller's logger, for the failure line
     * @return the pids and their count; {@link Building#unreadable} when the read failed
     */
    public static Building building(DSLContext ctx, Logger log) {
        try {
            var a = DSL.table(DSL.name("pg_catalog", "pg_stat_activity")).as("a");
            var p = DSL.table(DSL.name("pg_catalog", "pg_stat_progress_create_index")).as("p");
            List<Integer> pids = ctx.select(DSL.field(DSL.name("a", "pid"), Integer.class))
                .from(a)
                .join(p).on(DSL.field(DSL.name("p", "pid"), Integer.class).eq(DSL.field(DSL.name("a", "pid"), Integer.class)))
                .where(backends("a"))
                .orderBy(DSL.field(DSL.name("a", "pid"), Integer.class))
                .fetch(0, Integer.class);
            return new Building(pids.size(), pids);
        } catch (DataAccessException e) {
            log.warn("event=pci_builders_building_failed cause=\"{}\"", e.toString());
            return Building.unreadable();
        }
    }

    /** {@link #building}'s count: the builder sessions running a {@code CREATE INDEX} now, or -1 when it cannot be read. */
    public static int countBuilding(DSLContext ctx, Logger log) {
        return building(ctx, log).count();
    }

    /**
     * How many builder backends this database has now (other than the calling session), or -1 when it cannot be read.
     *
     * @param log the caller's logger, for the failure line
     */
    public static int count(DSLContext ctx, Logger log) {
        try {
            Integer count = ctx.selectCount()
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(backends())
                .fetchOne(0, Integer.class);
            return count == null ? 0 : count;
        } catch (DataAccessException e) {
            log.warn("event=pci_builders_count_failed cause=\"{}\"", e.toString());
            return -1;
        }
    }

    /**
     * Ends every builder backend of this database. The signal needs the target's role, which is the caller's own when
     * {@code NX_DB_ADMIN_*} serve both, as they do in the supported configuration; with a split-role configuration a
     * failure means a build the caller can deadlock with was left running, which an operator must see, hence ERROR.
     *
     * @param ctx a context on the calling session (autocommit)
     * @param why the {@code reason=...} fields of the log line
     * @param log the caller's logger, for every line
     * @return how many backends were signalled, or -1 when the read or the signal failed
     */
    public static int terminate(DSLContext ctx, String why, Logger log) {
        try {
            return terminateOrThrow(ctx, why, log);
        } catch (DataAccessException e) {
            log.error("event=pci_builders_terminate_failed cause=\"{}\"", e.toString());
            return -1;
        }
    }

    /**
     * {@link #terminate} without the swallow, for a caller inside a transaction: a failed statement aborts the transaction
     * unless the caller runs this in a nested one (a savepoint) and catches what it throws.
     *
     * @param log the caller's logger, for the {@code event=pci_builders_terminated} line
     */
    public static int terminateOrThrow(DSLContext ctx, String why, Logger log) {
        var pid = DSL.field(DSL.name("pid"), Integer.class);
        int terminated = 0;
        for (Boolean ended : ctx
                .select(DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN, pid))
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(backends())
                .fetch(0, Boolean.class)) {
            if (Boolean.TRUE.equals(ended)) {
                terminated++;
            }
        }
        log.info("event=pci_builders_terminated count={} {}", terminated, why);
        return terminated;
    }
}
