// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import liquibase.database.Database;
import liquibase.exception.LiquibaseException;
import liquibase.lockservice.DatabaseChangeLogLock;
import liquibase.lockservice.LockService;
import liquibase.lockservice.LockServiceFactory;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.time.Duration;

import dev.nexus.service.db.SchemaMigrator.MigrationException;

import static dev.nexus.service.db.SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY;
import static dev.nexus.service.db.SchemaMigrator.migrationLockHolderPid;

/**
 * The migrator's session-level advisory lock, held from before Liquibase takes its changelog lock until
 * after the walk (nexus-8sph2).
 *
 * <p>Liquibase's lock is a committed row in {@code databasechangeloglock}. A walker stopped mid-walk
 * (SIGTERM, CTRL_BREAK, a crash) leaves the row {@code locked=true}, and the next boot waited five
 * minutes for it and failed (measured 2026-10-05, T2 nexus_rdr/224-research-19). The row cannot tell a
 * dead walker from a live one. A session-level advisory lock can: PostgreSQL drops it when the holding
 * session ends, however the process stopped. Every migrator takes it first, so a migrator that holds it
 * knows no other migrator is walking, and a locked row it finds is stale. It releases that row through
 * Liquibase's own {@link LockService#forceReleaseLock()} and lets Liquibase take its lock as before.
 * Liquibase's lock stays in place, so an engine that predates this lock still waits on the row.
 *
 * <p>The lock is SESSION-scoped, so the migration connection must be a real PostgreSQL session for the
 * whole walk. A transaction-mode pooler (PgBouncer {@code pool_mode=transaction}) between the migrator and
 * PostgreSQL can hand each statement a different server session, which silently voids the lock: the
 * migrator would then release a live walker's row. Point the migrator at PostgreSQL directly or through
 * a session-mode pool.
 *
 * <p>Accepted gap: an engine that predates this lock, walking at the same moment as one that has it,
 * holds the row but not the advisory lock, and would have its row released. That needs two engine
 * versions booting against one database at once; the cloud runs one engine and a local install one.
 */
final class MigrationLock implements AutoCloseable {
    /** The migrator's logger on purpose: tests and operators filter the walk's events by it. */
    private static final Logger log = LoggerFactory.getLogger(SchemaMigrator.class);
    private static final Duration POLL = Duration.ofSeconds(1);
    private final Connection conn;

    private MigrationLock(Connection conn) {
        this.conn = conn;
    }

    static MigrationLock acquire(Connection conn, Database database, Duration wait) throws LiquibaseException {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        long deadline = System.nanoTime() + wait.toNanos();
        boolean logged = false;
        while (!tryLock(ctx)) {
            if (!logged) {
                log.info("event=schema_migration_lock_wait key={} holder_pid={} wait_s={}",
                         MIGRATION_ADVISORY_LOCK_KEY, holderPid(ctx), wait.toSeconds());
                logged = true;
            }
            long remaining = deadline - System.nanoTime();
            if (remaining <= 0) {
                throw new MigrationException("another migrator holds the migration lock (advisory key "
                    + MIGRATION_ADVISORY_LOCK_KEY + ", holder pid " + holderPid(ctx) + ") after waiting "
                    + wait.toSeconds() + " s");
            }
            try {
                Thread.sleep(Math.min(POLL.toMillis(), Math.max(1, remaining / 1_000_000)));
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new MigrationException("interrupted waiting for the migration lock", e);
            }
        }
        MigrationLock lock = new MigrationLock(conn);
        try {
            releaseStaleChangelogLock(database);
        } catch (LiquibaseException | RuntimeException e) {
            lock.close();
            throw e;
        }
        return lock;
    }

    private static boolean tryLock(DSLContext ctx) {
        return Boolean.TRUE.equals(ctx.select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN,
                DSL.val(MIGRATION_ADVISORY_LOCK_KEY)))
            .fetchOne(0, Boolean.class));
    }

    /** The pid holding the migration lock, or -1 when none is visible; for the log line only. */
    private static int holderPid(DSLContext ctx) {
        try {
            return migrationLockHolderPid(ctx);
        } catch (DataAccessException e) {
            return -1;
        }
    }

    /** Under the migration lock any locked changelog row is stale: release it, naming who left it. */
    static void releaseStaleChangelogLock(Database database) throws LiquibaseException {
        LockService lockService = LockServiceFactory.getInstance().getLockService(database);
        DatabaseChangeLogLock[] locks = lockService.listLocks();
        if (locks.length == 0) {
            return;
        }
        for (DatabaseChangeLogLock stale : locks) {
            log.warn("event=schema_changelog_lock_stale_released lockedby=\"{}\" lockgranted={}",
                     stale.getLockedBy(), stale.getLockGranted() == null ? null
                         : stale.getLockGranted().toInstant());
        }
        lockService.forceReleaseLock();
    }

    @Override
    public void close() {
        try {
            DSL.using(conn, SQLDialect.POSTGRES)
                .select(DSL.function("pg_advisory_unlock", SQLDataType.BOOLEAN,
                    DSL.val(MIGRATION_ADVISORY_LOCK_KEY)))
                .fetchOne();
        } catch (DataAccessException e) {
            // The session ending releases it anyway; a failed unlock on a broken connection is not fatal.
            log.warn("event=schema_migration_lock_release_failed cause=\"{}\"", e.toString());
        }
    }
}
