// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.time.Duration;
import java.util.Objects;
import java.util.Properties;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27; linda 2026-11-22, addendum 2): the engine pass, after the engine serves, that registers the name-key drop move
 * ({@link NameKeyDropMove#LABEL}) once the completion check's latest snapshot says gate 3 is LATCHED. The completion check stays read only
 * (Sam's rule, no boot-time data migrations) and the walk never registers it: this pass writes, on the admin connection, one
 * {@code INSERT ... ON CONFLICT (label) DO NOTHING} ({@link NameKeyDropMove#registerIfAbsent}), the way P3.3b's {@code LeafIndexPass} registers the swaps.
 *
 * <p><b>Gate.</b> The snapshot is read before any connection opens, so a gate that does not hold costs the database nothing. A missing snapshot, or one
 * whose latch is not written, ends the tick ({@link Outcome#WAITING}). The latch, not the live gate 3: a live dip after the latch must not strand
 * renames at {@code name_key_drop_move}.
 *
 * <p><b>Yielding.</b> A schema migration holds its advisory lock for the whole walk; while it does, the pass registers nothing
 * ({@link Outcome#MIGRATING}), and a lock that cannot be read counts as held (unknown is never "not held"). The pass takes no session lock of its own: the one
 * statement is idempotent, so a peer engine's pass, a second boot and this one add exactly one row between them.
 *
 * <p><b>Reporting.</b> The registered move is a {@code data_moves} row, which the data-move worker runs and {@code MoveBootWork} reports in
 * {@code boot_work} like every move; the pass has no entry of its own (one that never finished would read as work in progress to {@code nx upgrade}).
 * Once the row exists, however it got there, the pass is {@link Outcome#FINISHED} and later ticks open no connection (until the engine restarts).
 */
public final class NameKeyDropPass implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(NameKeyDropPass.class);

    /** The backend's {@code application_name}. */
    public static final String APPLICATION_NAME = "nexus-rdr229-name-key-drop-pass";
    static final Duration INITIAL_DELAY = Duration.ofSeconds(5);
    static final Duration PERIOD = Duration.ofSeconds(10);

    /** What one tick did. */
    public enum Outcome {
        /** No snapshot yet, or the latch is not written: nothing was done and no connection opened. */
        WAITING,
        /** A schema migration holds its lock, or the lock could not be read: nothing was registered. */
        MIGRATING,
        /** This tick inserted the row. */
        REGISTERED,
        /** The row was already there (an earlier boot, a peer engine). */
        PRESENT,
        /** An earlier tick of this pass saw the row: nothing was done and no connection opened. */
        FINISHED,
        /** The database could not be reached or refused the statement: the next tick tries again. */
        FAILED
    }

    private final AdminConnection admin;
    private final Supplier<CompletionCheck.Result> latest;
    private final String engineVersion;
    private final Object passLock = new Object();
    private boolean finished;   // guarded by passLock
    private ScheduledExecutorService scheduler;   // guarded by this

    /**
     * @param latest the completion check's latest result ({@code CompletionBootWork#latest}), or null before the first read
     * @param engineVersion the registering engine's version, written into the row's {@code changeset_id}; blank is a wiring defect
     */
    public NameKeyDropPass(AdminConnection admin, Supplier<CompletionCheck.Result> latest, String engineVersion) {
        this.admin = Objects.requireNonNull(admin, "admin");
        this.latest = Objects.requireNonNull(latest, "latest");
        NameKeyDropMove.changesetId(engineVersion);   // refused now, not at the first tick
        this.engineVersion = engineVersion;
    }

    /** Starts the schedule: a tick every {@link #PERIOD}, the first after {@link #INITIAL_DELAY}. Never throws into the executor. */
    public synchronized void start() {
        start(INITIAL_DELAY, PERIOD);
    }

    /** As {@link #start()} with the schedule given (a test runs it every 100 ms). */
    public synchronized void start(Duration initialDelay, Duration period) {
        if (scheduler != null) {
            return;
        }
        scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "name-key-drop-pass");
            t.setDaemon(true);
            return t;
        });
        scheduler.scheduleWithFixedDelay(() -> {
            try {
                runOnce();
            } catch (VirtualMachineError e) {
                throw e;
            } catch (Throwable e) {
                log.warn("event=name_key_drop_pass_failed cause=\"{}\"", e.toString());
            }
        }, initialDelay.toMillis(), period.toMillis(), TimeUnit.MILLISECONDS);
    }

    @Override
    public synchronized void close() {
        if (scheduler != null) {
            scheduler.shutdownNow();
            scheduler = null;
        }
    }

    /** One tick, now. */
    public Outcome runOnce() {
        synchronized (passLock) {
            if (finished) {
                return Outcome.FINISHED;
            }
            CompletionCheck.Result snapshot;
            try {
                snapshot = latest.get();
            } catch (RuntimeException e) {
                log.warn("event=name_key_drop_pass_snapshot_unread cause=\"{}\"", e.toString());
                return Outcome.WAITING;
            }
            if (snapshot == null || !snapshot.gate3Latched()) {
                log.debug("event=name_key_drop_pass_waiting reason=gate3_not_latched");
                return Outcome.WAITING;
            }
            Properties props = new Properties();
            props.setProperty("user", admin.user());
            props.setProperty("password", admin.password());
            props.setProperty("ApplicationName", APPLICATION_NAME);
            props.setProperty("connectTimeout", "10");
            props.setProperty("loginTimeout", "30");
            try (Connection conn = DriverManager.getConnection(admin.url(), props)) {
                conn.setAutoCommit(true);
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
                boolean migrating;
                try {
                    migrating = SchemaMigrator.migrationLockHolderPid(ctx) >= 0;
                } catch (DataAccessException e) {
                    log.warn("event=name_key_drop_pass_skipped reason=migration_lock_unread cause=\"{}\"", e.toString());
                    return Outcome.MIGRATING;
                }
                if (migrating) {
                    log.info("event=name_key_drop_pass_skipped reason=migration_in_progress key={}", SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY);
                    return Outcome.MIGRATING;
                }
                boolean inserted = NameKeyDropMove.registerIfAbsent(snapshot, ctx, engineVersion);
                finished = true;   // the row exists now, by this insert or an earlier one
                if (inserted) {
                    log.info("event=name_key_drop_registered label={} engine_version={}", NameKeyDropMove.LABEL, engineVersion);
                }
                return inserted ? Outcome.REGISTERED : Outcome.PRESENT;
            } catch (SQLException | DataAccessException e) {
                log.warn("event=name_key_drop_pass_failed cause=\"{}\"", e.toString());
                return Outcome.FAILED;
            }
        }
    }
}
