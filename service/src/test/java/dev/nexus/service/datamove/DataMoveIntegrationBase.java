// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.DmlUtilsTestFixtures;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.DmlUtilsInstaller;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.TreeSet;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * nexus-f3yqd.2.13 (RDR-246 Step 4, The move worker steps 1 and 2) on the pg_background image: the worker's passes over
 * real dml_utils runs. One container per test class, migrated once by a non-superuser admin role exactly as production's
 * {@code nexus_admin} is, and {@code dm_test} rebuilt from nothing, with an empty registry, before each scenario (the
 * fixture changelogs hold the tables; no SQL text lives here). Each scenario registers its own labels.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Timeout(value = 20, unit = TimeUnit.MINUTES)
abstract class DataMoveIntegrationBase {

    static final String ADMIN = DmlUtilsTestFixtures.ADMIN;
    static final String ADMIN_PASSWORD = DmlUtilsTestFixtures.ADMIN_PASSWORD;
    /** The call connection's application name for a worker built by {@link #worker}. */
    static final String WORKER_NAME = "nexus-data-move-test";
    static final String CALL_APPLICATION_NAME = WORKER_NAME + "-call";

    static final Field<Integer> N = DSL.field(DSL.name("n"), Integer.class);
    static final Field<String> TOUCHED_BY = DSL.field(DSL.name("touched_by"), String.class);
    static final Field<String> TENANT_ID = DSL.field(DSL.name("tenant_id"), String.class);

    PostgreSQLContainer<?> pg;
    HikariDataSource adminPool;
    Connection su;
    AdminConnection adminConnection;
    private final List<DataMoveWorker> workers = new ArrayList<>();

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.startDataMove();
        su = pg.createConnection("");
        su.setAutoCommit(true);
        DmlUtilsTestFixtures.createAdmin(su);
        PgContainerHelper.createDataMoveExtensions(su);
        DmlUtilsTestFixtures.bootstrapDmlUtils(su);
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN);
        cfg.setPassword(ADMIN_PASSWORD);
        cfg.setMaximumPoolSize(1);
        adminPool = new HikariDataSource(cfg);
        adminConnection = new AdminConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD);
        installed();
    }

    @AfterAll
    void stop() throws Exception {
        if (adminPool != null) adminPool.close();
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void reset() throws Exception {
        DataMoveTestFixtures.releaseWorkerSlots(su);
        DmlUtilsTestFixtures.bootstrapDmlUtils(su);   // a scenario that dropped the roles left them gone
        installed();                                   // and the libraries with them
        DataMoveTestFixtures.rebuild(su);
        ctx().deleteFrom(table("nexus", "data_moves")).execute();   // no earlier scenario's move is open
        ctx().deleteFrom(table("nexus", "service_tokens")).execute();   // nor its tenants
    }

    @AfterEach
    void closeWorkers() {
        workers.forEach(DataMoveWorker::close);
        workers.clear();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    /** A boot: the libraries are applied (or skipped) by the migration, and the installer keeps which. */
    DmlUtilsInstaller installed() {
        var installer = new DmlUtilsInstaller(true);
        SchemaMigrator.migrate(adminPool, installer);
        return installer;
    }

    DataMoveWorker worker(DmlUtilsInstaller installer, Integer sleepOverrideMs) {
        return worker(installer, sleepOverrideMs, FailureHandler.BLOCK_ALWAYS, MoveFinisher.RUNS_ONLY);
    }

    DataMoveWorker worker(DmlUtilsInstaller installer, Integer sleepOverrideMs, FailureHandler failures,
                          MoveFinisher finisher) {
        return worker(installer, sleepOverrideMs, failures, finisher, true);
    }

    DataMoveWorker worker(DmlUtilsInstaller installer, Integer sleepOverrideMs, FailureHandler failures,
                          MoveFinisher finisher, boolean singletonLockHeld) {
        var w = new DataMoveWorker(adminConnection, installer,
            new DataMoveWorker.Settings(Duration.ofMillis(50), Duration.ofMillis(10), sleepOverrideMs),
            WORKER_NAME, Clock.systemUTC(), new MoveStates(Clock.systemUTC()), failures, finisher, singletonLockHeld);
        workers.add(w);
        return w;
    }

    DSLContext ctx() {
        return DSL.using(su, SQLDialect.POSTGRES);
    }

    static Table<?> table(String schema, String name) {
        return DSL.table(DSL.name(schema, name));
    }

    void register(String label, String table, int chunkSize, int sleepMs) {
        DataMoveTestFixtures.register(ctx(), label, table, chunkSize, sleepMs);
    }

    void register(String label, String table, int chunkSize, int sleepMs, String supersedes, String templateResource,
                  String verificationResource, int statementTimeoutMs, String onCompleteResource) {
        DataMoveTestFixtures.register(ctx(), label, table, chunkSize, sleepMs, supersedes, templateResource,
            verificationResource, statementTimeoutMs, onCompleteResource);
    }

    int countWhere(String table, Condition where) {
        return ctx().fetchCount(table("dm_test", table), where);
    }

    /** Every row of {@code dm_test.<table>} was moved exactly once. */
    void assertEveryRowMovedOnce(String table, int expectedRows) {
        assertThat(countWhere(table, N.eq(1))).as("rows of %s moved once", table).isEqualTo(expectedRows);
        assertThat(countWhere(table, N.ne(1))).as("rows of %s moved zero or several times", table).isZero();
    }

    /** Every row of a tenant-keyed table was stamped by a session carrying its own tenant, and no other. */
    void assertEachRowTouchedByItsOwnTenant(String table) {
        assertThat(countWhere(table, TOUCHED_BY.isNull().or(TOUCHED_BY.ne(TENANT_ID)))).isZero();
    }

    TreeSet<String> activeRuns(String label) {
        return new TreeSet<>(DataMoveRuns.activeRunLabels(ctx(), label));
    }

    int partitionWiseRuns(String label) {
        return ctx().fetchCount(table("dml_utils_data", "migration_run"),
            DataMoveRuns.belongsTo(label).and(DSL.field(DSL.name("partition_wise"), Boolean.class).isTrue()));
    }

    DataMoveRuns.Progress progress(String label) {
        return DataMoveRuns.progress(ctx(), label);
    }

    static final Field<String> LABEL = DSL.field(DSL.name("label"), String.class);
    static final Field<java.time.OffsetDateTime> COMPLETED_AT =
        DSL.field(DSL.name("completed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);

    /** {@code completed_at} of the registered move, or null while it is open. */
    java.time.OffsetDateTime completedAt(String label) {
        return ctx().select(COMPLETED_AT).from(table("nexus", "data_moves")).where(LABEL.eq(label)).fetchOne(COMPLETED_AT);
    }

    /** The runs of {@code label} done by a worker that stops there (no finisher): the state the finish starts from. */
    void runsOnly(String label) {
        assertThat(worker(installed(), null).runCycle()).isEqualTo(1);
        assertThat(completedAt(label)).as("no completion without a finisher").isNull();
    }

    /** The finish of {@code label} by {@code finisher}, on an admin session of the pass, as the worker calls it. */
    MoveFinisher.Result finish(VerifyingFinisher finisher, String label, String table, List<String> tenants) {
        DataMove move = DataMoveRegistry.reload(ctx(), label).orElseThrow();
        try (var session = adminSession(table)) {
            return finisher.finish(new MoveFinisher.Context(move, tenants, session.ctx(), adminConnection, WORKER_NAME));
        }
    }

    MoveFinisher.Result finish(String label, String table, List<String> tenants) {
        return finish(new VerifyingFinisher(), label, table, tenants);
    }

    /**
     * The worker's CALL backend: the newest client backend named {@link #CALL_APPLICATION_NAME}, or -1. pg_background
     * workers copy the caller's settings, application_name included, and a previous test's call backend can still be
     * exiting, so the name alone can match more than one row (develop run 44021463117).
     */
    int callPid() {
        Integer pid = ctx().select(DSL.field(DSL.name("pid"), Integer.class))
            .from(table("pg_catalog", "pg_stat_activity"))
            .where(DSL.field(DSL.name("application_name"), String.class).eq(CALL_APPLICATION_NAME))
            .and(DSL.field(DSL.name("backend_type"), String.class).eq("client backend"))
            .orderBy(DSL.field(DSL.name("backend_start")).desc())
            .limit(1)
            .fetchOne(0, Integer.class);
        return pid == null ? -1 : pid;
    }

    static <T> CompletableFuture<T> async(java.util.concurrent.Callable<T> work) {
        return CompletableFuture.supplyAsync(() -> {
            try {
                return work.call();
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
    }

    /** Samples the move's progress every 20 ms until {@code cycle} ends. */
    List<DataMoveRuns.Progress> sampleUntilDone(CompletableFuture<Integer> cycle, String label)
            throws InterruptedException, ExecutionException, TimeoutException {
        List<DataMoveRuns.Progress> samples = new ArrayList<>();
        long deadline = System.nanoTime() + Duration.ofMinutes(5).toNanos();
        while (!cycle.isDone()) {
            samples.add(progress(label));
            Thread.sleep(20);
            if (System.nanoTime() > deadline) {
                throw new TimeoutException("the cycle did not end");
            }
        }
        cycle.get();
        samples.add(progress(label));
        return samples;
    }

    static void assertDoneNeverFalls(List<DataMoveRuns.Progress> samples) {
        long previous = 0;
        for (var s : samples) {
            assertThat(s.done()).as("done fell from %d", previous).isGreaterThanOrEqualTo(previous);
            previous = s.done();
        }
    }

    void awaitProgress(String label, long chunks) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofMinutes(2).toNanos();
        while (progress(label).done() < chunks) {
            assertThat(System.nanoTime()).as("progress never reached %d chunks", chunks).isLessThan(deadline);
            Thread.sleep(20);
        }
    }

    long relationSize(String relation) {
        return ctx().select(DSL.function("pg_relation_size", SQLDataType.BIGINT,
            DSL.function("to_regclass", Object.class, DSL.val(relation)))).fetchOne(0, Long.class);
    }

    /** An admin session on {@code dm_test.<table>}, as the worker opens one; the caller closes it. */
    dev.nexus.service.db.AdminDdlSession adminSession(String table) {
        return dev.nexus.service.db.AdminDdlSession.open(adminConnection,
            new dev.nexus.service.db.AdminDdlSession.Settings(WORKER_NAME + "-probe", 0L, "test", "dm_test", table, "none"),
            false);
    }

    /** How many sessions the server shows under {@code applicationName}. */
    int sessionsNamed(String applicationName) {
        return ctx().fetchCount(table("pg_catalog", "pg_stat_activity"),
            DSL.field(DSL.name("application_name"), String.class).eq(applicationName));
    }
}
