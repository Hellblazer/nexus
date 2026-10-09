// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.lockservice.LockService;
import liquibase.lockservice.LockServiceFactory;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Duration;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-8sph2: an engine stopped during the Liquibase walk left {@code databasechangeloglock}
 * {@code locked=true}, and the next boot waited Liquibase's five minutes for a lock no live process
 * held, then failed (measured 2026-10-05 on qwentescence, T2 nexus_rdr/224-research-19).
 *
 * <p>The migrator now takes a session-level advisory lock on its migration connection before Liquibase
 * runs. PostgreSQL drops that lock when the holding session ends, however the process stopped, so
 * holding it proves no other migrator is walking, and a lock row it finds is stale. A live migrator
 * keeps the advisory lock, so the next one waits for it and never touches the live walker's row.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class SchemaMigratorStaleChangelogLockIntegrationTest {

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;

    @BeforeAll
    void bootstrap() throws Exception {
        pg = PgContainerHelper.startDedicated();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        // A completed first walk, so the lock table exists and later walks are the no-op re-walk a
        // restarted engine performs.
        SchemaMigrator.migrate(ds);
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    @Test
    @Timeout(value = 120, unit = TimeUnit.SECONDS)
    void aLockRowLeftByAStoppedWalkerIsReleased_andTheWalkSucceeds() throws Exception {
        // A walker that took Liquibase's lock and then stopped: its row stays locked=true and its
        // session (and so any advisory lock it held) is gone.
        try (Connection dead = pg.createConnection("")) {
            changelogLockService(dead).acquireLock();
        }
        assertThat(lockRowLocked()).as("precondition: the stale row is locked").isTrue();

        // Without the fix Liquibase waits its five minutes for this row; the @Timeout catches that.
        SchemaMigrator.migrate(ds);

        assertThat(lockRowLocked()).as("the walk released its own lock and left none").isFalse();
        assertThat(advisoryLockHolders()).as("the migration lock is not left held").isZero();
    }

    @Test
    @Timeout(value = 120, unit = TimeUnit.SECONDS)
    void aLiveWalkerIsWaitedFor_andItsLockRowIsNotTaken() throws Exception {
        try (Connection live = pg.createConnection("")) {
            live.setAutoCommit(true);
            assertThat(DSL.using(live, SQLDialect.POSTGRES)
                    .select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN,
                        DSL.val(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY)))
                    .fetchOne(0, Boolean.class))
                .as("precondition: the live walker holds the migration lock").isTrue();
            LockService liveLiquibaseLock = changelogLockService(live);
            liveLiquibaseLock.acquireLock();
            try {
                assertThatThrownBy(() -> SchemaMigrator.migrate(ds, Duration.ofSeconds(2)))
                    .isInstanceOf(SchemaMigrator.MigrationException.class)
                    .hasMessageContaining("migration lock");
                assertThat(lockRowLocked())
                    .as("the waiting migrator left the live walker's lock row alone").isTrue();
            } finally {
                liveLiquibaseLock.releaseLock();
            }
        }
        assertThat(lockRowLocked()).isFalse();
    }

    @Test
    @Timeout(value = 120, unit = TimeUnit.SECONDS)
    void anOrdinaryWalkLeavesNoMigrationLockHeld() {
        SchemaMigrator.migrate(ds);
        assertThat(advisoryLockHolders()).isZero();
    }

    @Test
    void theLockKeyIsOutsideTheInt4KeySpaceOtherAdvisoryLocksUse() {
        // TaxonomyRepository keys its transaction locks on hashtext(...), an int4, widened to bigint.
        assertThat(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY)
            .isGreaterThan(Integer.MAX_VALUE);
    }

    private static LockService changelogLockService(Connection conn) throws Exception {
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
        db.setLiquibaseSchemaName("public");
        db.setDefaultSchemaName("public");
        return LockServiceFactory.getInstance().getLockService(db);
    }

    private boolean lockRowLocked() throws Exception {
        try (Connection su = pg.createConnection("")) {
            Boolean locked = DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.field(DSL.name("locked"), Boolean.class))
                .from(DSL.table(DSL.name("public", "databasechangeloglock")))
                .where(DSL.field(DSL.name("id"), Integer.class).eq(1))
                .fetchOne(0, Boolean.class);
            return Boolean.TRUE.equals(locked);
        }
    }

    private int advisoryLockHolders() {
        try (Connection su = pg.createConnection("")) {
            long key = SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY;
            return DSL.using(su, SQLDialect.POSTGRES)
                .fetchCount(DSL.selectOne()
                    .from(DSL.table(DSL.name("pg_catalog", "pg_locks")))
                    .where(DSL.field(DSL.name("locktype"), String.class).eq("advisory"))
                    .and(DSL.field(DSL.name("classid"), Long.class).cast(SQLDataType.BIGINT).eq(key >>> 32))
                    .and(DSL.field(DSL.name("objid"), Long.class).cast(SQLDataType.BIGINT).eq(key & 0xffffffffL))
                    .and(DSL.field(DSL.name("objsubid"), Integer.class).eq(1)));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
