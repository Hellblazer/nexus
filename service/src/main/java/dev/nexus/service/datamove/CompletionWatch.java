// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminConnection;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.Properties;

/**
 * Watches one move while its validation runs with no statement bound, and cancels the statement when the move is paused or superseded
 * meanwhile (the same cancel as a stop, scoped to this move). The statement runs for as long as it takes, so the pause an operator sets for
 * a down-step or a rollback window would otherwise wait for it. A daemon thread reads the move's row on a connection of its own every
 * {@link #INTERVAL_MS}; the cancel goes through {@link DataMoveOrphans#cancelBackend}, which checks the backend is still the one started.
 */
final class CompletionWatch implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(CompletionWatch.class);

    /** How often the move's row is read, in milliseconds. */
    static final long INTERVAL_MS = 1_000L;

    private final AdminConnection admin;
    private final String label;
    private final int pid;
    private final String applicationName;
    private final OffsetDateTime backendStart;
    private volatile boolean closed;
    private volatile boolean fired;
    private final Thread thread;

    private CompletionWatch(AdminConnection admin, String label, int pid, String applicationName, OffsetDateTime backendStart) {
        this.admin = admin;
        this.label = label;
        this.pid = pid;
        this.applicationName = applicationName;
        this.backendStart = backendStart;
        this.thread = new Thread(this::run, "data-move-completion-watch");
        thread.setDaemon(true);
    }

    static CompletionWatch start(AdminConnection admin, String label, int pid, String applicationName, OffsetDateTime backendStart) {
        CompletionWatch w = new CompletionWatch(admin, label, pid, applicationName, backendStart);
        w.thread.start();
        return w;
    }

    /** True when the watch cancelled the statement because the move was paused, superseded or gone. */
    boolean fired() {
        return fired;
    }

    private void run() {
        while (!closed) {
            try {
                Thread.sleep(INTERVAL_MS);
            } catch (InterruptedException e) {
                return;
            }
            if (closed) {
                return;
            }
            try {
                if (heldOrGone()) {
                    fired = true;
                    DataMoveOrphans.cancelBackend(admin, pid, applicationName, backendStart);
                    return;
                }
            } catch (SQLException | DataAccessException e) {
                log.debug("event=data_move_completion_watch_failed label={} cause=\"{}\"", label, FailureHandler.oneLine(e.toString()));
            }
        }
    }

    private boolean heldOrGone() throws SQLException {
        Properties props = new Properties();
        props.setProperty("user", admin.user());
        props.setProperty("password", admin.password());
        props.setProperty("ApplicationName", DataMoveOrphans.PREFIX + "watch");
        props.setProperty("connectTimeout", "3");
        props.setProperty("socketTimeout", "5");
        try (Connection c = DriverManager.getConnection(admin.url(), props)) {
            var ctx = DSL.using(c, SQLDialect.POSTGRES);
            var row = DataMoveRegistry.reload(ctx, label);
            if (row.isEmpty() || row.get().paused()) {
                return true;
            }
            return ctx.fetchExists(DSL.table(DSL.name("nexus", "data_moves")),
                DSL.field(DSL.name("supersedes"), String.class).eq(label));
        }
    }

    @Override
    public void close() {
        closed = true;
        thread.interrupt();
    }
}
