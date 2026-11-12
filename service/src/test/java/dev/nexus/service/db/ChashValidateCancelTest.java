// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.ChashConstraintValidator.Outcome;
import dev.nexus.service.db.ChashConstraintValidator.Step;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.UnaryOperator;

/**
 * nexus-f3yqd: the cancel of a running VALIDATE job ({@code DELETE /v1/admin/validate-constraints/{job_id}}) at the
 * validator, in each place a cancel can land. A cancel here is what the handler does: the job's stop reason {@code cancelled}
 * first, then {@link ChashConstraintValidator#cancelRunningFor} naming the job; nothing interrupts the worker thread (that is
 * the engine's shutdown, tested in {@link ChashConstraintValidatorBranchesTest}). The route itself is tested in
 * {@code ChashValidateJobRouteTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashValidateCancelTest {

    private static final String CHUNKS_CHECK = "chunks_chash_octet_check";
    private static final String MANIFEST_CHECK = "catalog_document_chunks_chash_octet_check";
    private static final String TENANT = "t-chashcancel-a";
    private static final String COLLECTION = "knowledge__chashcancel__bge-base-en-v15-768__v1";
    private static final String MODEL = "bge-base-en-v15-768";

    private PostgreSQLContainer<?> pg;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(ctx, "tok-a-chashcancel-0123456789abcd", TENANT, "a");
            PgContainerHelper.ensureTenantPartitions(ctx, TENANT);
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void fresh() throws Exception {
        System.setProperty(ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY, "200");
        System.setProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY, "5");
        System.setProperty(ChashConstraintValidator.RETRY_BACKOFF_PROPERTY, "10");
        System.setProperty(ChashConstraintValidator.PAUSE_PROPERTY, "0");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.alterTable(CHUNKS).dropConstraintIfExists(CHUNKS_CHECK).execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists(MANIFEST_CHECK).execute();
            ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).execute();
            ctx.deleteFrom(CHUNKS).execute();
            PgContainerHelper.readdChashOctetChecksNotValid(su);
        }
    }

    @AfterEach
    void clearProperties() {
        for (String p : new String[]{ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY, ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY,
            ChashConstraintValidator.RETRY_BACKOFF_PROPERTY, ChashConstraintValidator.PAUSE_PROPERTY}) {
            System.clearProperty(p);
        }
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    /** A listener a test cancels the way the DELETE does: a stop reason set from another thread, the run owned by itself. */
    private static final class Recorder implements ChashConstraintValidator.Listener {
        final List<Step> steps = new CopyOnWriteArrayList<>();
        volatile int total = -1;
        volatile String stopNow;
        volatile Runnable onLastStep = () -> { };

        @Override
        public void planned(int total) {
            this.total = total;
        }

        @Override
        public void current(String table, Long bytes) {
        }

        @Override
        public void stepped(Step step) {
            steps.add(step);
            if (steps.size() == total) onLastStep.run();
        }

        @Override
        public String stopReason() {
            return stopNow;
        }

        @Override
        public Object owner() {
            return this;
        }

        /** The DELETE: the reason first, then the statements in flight. */
        boolean cancel(ChashConstraintValidator v) {
            stopNow = "cancelled";
            return v.cancelRunningFor(this);
        }
    }

    @FunctionalInterface
    interface SqlHook {
        void before(String sql) throws SQLException;
    }

    /**
     * A validator whose step sessions run {@code beforePublish} before the validator gets the session (so a test can hold a
     * step between opening its session and publishing it), call {@code query} before each prepared statement, and send each
     * DDL statement through {@code ddl} and {@code ddlRewrite}. The controller session is the real one.
     */
    private ChashConstraintValidator validator(Runnable beforePublish, SqlHook query, SqlHook ddl, UnaryOperator<String> ddlRewrite) {
        return new ChashConstraintValidator(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()),
            (a, settings, lock) -> {
                if (lock) return AdminDdlSession.open(a, settings, true);
                try {
                    var props = new java.util.Properties();
                    props.setProperty("user", a.user());
                    props.setProperty("password", a.password());
                    props.setProperty("ApplicationName", settings.applicationName());
                    Connection real = DriverManager.getConnection(a.url(), props);
                    real.setAutoCommit(true);
                    var session = new AdminDdlSession(settings, proxied(real, query, ddl, ddlRewrite));
                    beforePublish.run();
                    return session;
                } catch (SQLException e) {
                    throw new IllegalStateException(e);
                }
            }, null, null);
    }

    private Connection proxied(Connection real, SqlHook query, SqlHook ddl, UnaryOperator<String> ddlRewrite) {
        return (Connection) Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{Connection.class}, (proxy, m, args) -> {
            try {
                if (m.getName().equals("prepareStatement") && args != null && args[0] instanceof String sql) query.before(sql);
                Object out = m.invoke(real, args);
                if (m.getName().equals("createStatement") && out instanceof Statement st) {
                    return Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{Statement.class}, (p2, m2, a2) -> {
                        try {
                            if (m2.getName().equals("execute") && a2 != null && a2.length == 1 && a2[0] instanceof String sql) {
                                ddl.before(sql);
                                a2[0] = ddlRewrite.apply(sql);
                            }
                            return m2.invoke(st, a2);
                        } catch (InvocationTargetException e) {
                            throw e.getCause();
                        }
                    });
                }
                return out;
            } catch (InvocationTargetException e) {
                throw e.getCause();
            }
        });
    }

    private ChashConstraintValidator validator(SqlHook ddl, UnaryOperator<String> ddlRewrite) {
        return validator(() -> { }, sql -> { }, ddl, ddlRewrite);
    }

    private static CompletableFuture<ChashConstraintValidator.Result> runInBackground(ChashConstraintValidator v, Recorder r) {
        CompletableFuture<ChashConstraintValidator.Result> run = new CompletableFuture<>();
        Thread worker = new Thread(() -> {
            try {
                run.complete(v.run(false, r));
            } catch (Throwable t) {
                run.completeExceptionally(t);
            }
        }, "chash-cancel-test-worker");
        worker.start();
        return run;
    }

    private Map<String, Boolean> validity() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var out = new java.util.LinkedHashMap<String, Boolean>();
            for (var r : ChashConformanceConstraints.query(DSL.using(su, SQLDialect.POSTGRES)).fetch()) {
                if (!"chunks_retired_225".equals(r.value3())) out.put(r.value3(), r.value5());
            }
            return out;
        }
    }

    private long validatedCount() throws Exception {
        return validity().values().stream().filter(v -> v).count();
    }

    /** True while a statement of a validator session containing {@code text} is active on the server. */
    private boolean activeOnServer(String text) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(DSL.selectOne()
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(DSL.field(DSL.name("application_name"), String.class).startsWith(ChashConstraintValidator.APPLICATION_NAME_PREFIX))
                .and(DSL.field(DSL.name("state"), String.class).eq("active"))
                .and(DSL.field(DSL.name("query"), String.class).contains(text)));
        }
    }

    private static void await(java.util.concurrent.Callable<Boolean> cond, String what) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (System.nanoTime() < deadline) {
            if (cond.call()) return;
            Thread.sleep(25);
        }
        throw new AssertionError("timed out waiting for: " + what);
    }

    private static Duration since(long startNanos) {
        return Duration.ofNanos(System.nanoTime() - startNanos);
    }

    // ── a cancel, wherever it lands ─────────────────────────────────────────

    /**
     * A cancel while a VALIDATE is mid-table. The stalled statement is the real VALIDATE of the leaf that holds rows, followed
     * in the same statement by a 60 s sleep: when the cancel lands the VALIDATE has scanned its table and its transaction is
     * still open. The cancel ends it on the server at once and rolls it back: that leaf stays NOT VALID, the constraints
     * validated before it stay validated, no VALIDATE starts after it, and the run reports {@code cancelled}. A cancel naming
     * another job leaves the run alone. Kills: a cancel that reaches only the job's flag (the statement runs its minute); the
     * run reporting {@code interrupted}; a step started after the cancel; a cancel that reaches a run it does not name.
     */
    @Test
    void aCancelMidVALIDATE_endsTheStatementOnTheServer_leavesThatTableNotValid_andReportsCancelled() throws Exception {
        String leaf;
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
            for (int i = 0; i < 200; i++) {
                ctx.insertInto(CHUNKS).set(CHUNKS.TENANT_ID, TENANT).set(CHUNKS.COLLECTION, COLLECTION)
                    .set(CHUNKS.CHASH, MessageDigest.getInstance("SHA-256").digest(("row " + i).getBytes(java.nio.charset.StandardCharsets.UTF_8)))
                    .set(CHUNKS.EMBEDDING_MODEL, MODEL).set(CHUNKS.CHUNK_TEXT, "row " + i)
                    .set(CHUNKS.EMBEDDING_768, Vector.of(new float[768])).execute();
            }
            leaf = ctx.resultQuery("select c.relname::text from pg_class c where c.oid = (select tableoid from nexus.chunks limit 1)")
                .fetchOne(0, String.class);
        }
        AtomicInteger validates = new AtomicInteger();
        AtomicInteger afterStall = new AtomicInteger();
        AtomicBoolean stalled = new AtomicBoolean();
        var v = validator(sql -> { }, sql -> {
            if (!sql.contains("VALIDATE CONSTRAINT")) return sql;
            validates.incrementAndGet();
            if (stalled.get()) afterStall.incrementAndGet();
            if (sql.contains("." + leaf + " ") || sql.contains("\"" + leaf + "\"")) {
                stalled.set(true);
                return "DO $cancel$ BEGIN EXECUTE '" + sql.replace("'", "''") + "'; PERFORM pg_sleep(60); END $cancel$";
            }
            return sql;
        });
        Recorder r = new Recorder();
        var run = runInBackground(v, r);
        await(() -> run.isDone() || activeOnServer("pg_sleep"), "the leaf's VALIDATE is in flight on the server");
        assertThat(run.isDone()).as("the run is still going").isFalse();
        int stepsBefore = r.steps.size();

        assertThat(v.cancelRunningFor(new Object())).as("a cancel naming another job is refused").isFalse();
        assertThat(activeOnServer("pg_sleep")).as("and leaves the VALIDATE in flight").isTrue();

        long start = System.nanoTime();
        assertThat(r.cancel(v)).isTrue();
        var result = run.get(30, TimeUnit.SECONDS);
        assertThat(since(start)).as("the VALIDATE was cancelled, not waited out").isLessThan(Duration.ofSeconds(20));
        assertThat(result.stopped()).isEqualTo("cancelled");
        assertThat(result.allValidated()).isNull();
        assertThat(r.steps).as("the cancelled step reported no result").hasSize(stepsBefore);
        assertThat(afterStall.get()).as("no VALIDATE started after the cancel").isZero();
        assertThat(activeOnServer("pg_sleep")).as("no statement of the step is left on the server").isFalse();
        assertThat(validity().get(leaf)).as("the cancelled VALIDATE had scanned the leaf and was rolled back").isFalse();
        long validated = r.steps.stream().filter(s -> s.outcome() == Outcome.VALIDATED).count();
        assertThat(validatedCount()).as("exactly the steps before the cancel stay validated").isEqualTo(validated);
        assertThat(validates.get()).as("the stalled VALIDATE was a real one, after others (non-vacuous)").isGreaterThan(1);
    }

    /**
     * A cancel that lands while a step has opened its session but not yet published it, so the cancel finds no step session
     * to cancel. The step reads the stop reason after publishing its session and starts no statement. Kills: the stop-reason
     * check after the publish gone (the step's session is not cancelled, so its pre-check and VALIDATE would run).
     */
    @Test
    void aCancelBeforeAStepPublishesItsSession_startsNoStatementOfThatStep() throws Exception {
        CountDownLatch entered = new CountDownLatch(1);
        CountDownLatch gate = new CountDownLatch(1);
        AtomicBoolean first = new AtomicBoolean(true);
        AtomicBoolean released = new AtomicBoolean();
        AtomicInteger stepStatements = new AtomicInteger();
        AtomicInteger validates = new AtomicInteger();
        var v = validator(() -> {
            if (!first.getAndSet(false)) return;
            entered.countDown();
            while (true) {   // held between opening the session and handing it over: not interruptible
                try {
                    gate.await();
                    break;
                } catch (InterruptedException e) {
                    // keep waiting
                }
            }
            released.set(true);
        }, sql -> {
            if (released.get()) stepStatements.incrementAndGet();
        }, sql -> {
            if (sql.contains("VALIDATE CONSTRAINT")) validates.incrementAndGet();
        }, UnaryOperator.identity());
        Recorder r = new Recorder();
        var run = runInBackground(v, r);
        assertThat(entered.await(30, TimeUnit.SECONDS)).as("the first step has opened its session").isTrue();
        assertThat(r.cancel(v)).as("the run is this job's").isTrue();
        gate.countDown();
        var result = run.get(30, TimeUnit.SECONDS);
        assertThat(result.stopped()).isEqualTo("cancelled");
        assertThat(stepStatements.get()).as("the step started no statement after the cancel").isZero();
        assertThat(validates.get()).isZero();
        assertThat(r.steps).isEmpty();
        assertThat(validatedCount()).isZero();
    }

    /**
     * A cancel during a step's retry backoff (its VALIDATE keeps hitting a deadlock, the backoff is 15 s) ends the run within a
     * slice of the sleep, not after it. Kills: a backoff sleep that only an interrupt can cut (a cancel interrupts nothing).
     */
    @Test
    void aCancelDuringAStepsBackoff_endsTheRunWithoutWaitingItOut() throws Exception {
        System.setProperty(ChashConstraintValidator.RETRY_BACKOFF_PROPERTY, "15000");
        AtomicInteger attempts = new AtomicInteger();
        var v = validator(sql -> {
            if (sql.contains("VALIDATE CONSTRAINT")) {
                attempts.incrementAndGet();
                throw new SQLException("injected 40P01", "40P01");
            }
        }, UnaryOperator.identity());
        Recorder r = new Recorder();
        var run = runInBackground(v, r);
        await(() -> run.isDone() || attempts.get() >= 1, "the first VALIDATE failed and the step is backing off");
        Thread.sleep(300);
        assertThat(run.isDone()).as("the step is in its 15 s backoff").isFalse();
        long start = System.nanoTime();
        r.cancel(v);
        var result = run.get(30, TimeUnit.SECONDS);
        assertThat(since(start)).as("the backoff was cut short").isLessThan(Duration.ofSeconds(5));
        assertThat(result.stopped()).isEqualTo("cancelled");
        assertThat(attempts.get()).as("no attempt after the cancel").isEqualTo(1);
        assertThat(r.steps).isEmpty();
    }

    /**
     * A cancel during the plan's read of the constraints, while a partition is locked ACCESS EXCLUSIVE and the read backs off
     * for 15 s, ends the run within a slice of the sleep. Kills: the plan read's backoff not cut by a cancel.
     */
    @Test
    void aCancelDuringThePlanReadsBackoff_endsTheRunWithoutWaitingItOut() throws Exception {
        System.setProperty(ChashConstraintValidator.RETRY_BACKOFF_PROPERTY, "15000");
        String partition;
        try (Connection su = pg.createConnection("")) {
            partition = DSL.using(su, SQLDialect.POSTGRES).resultQuery("select c.relname::text from pg_inherits i join pg_class c "
                + "on c.oid = i.inhrelid where i.inhparent = 'nexus.chunks'::regclass order by 1 limit 1").fetchOne(0, String.class);
        }
        Connection held = pg.createConnection("");
        held.setAutoCommit(false);
        DSL.using(held, SQLDialect.POSTGRES).alterTable(DSL.table(DSL.name("nexus", partition)))
            .renameTo(DSL.name(partition + "_held")).execute();
        try {
            Recorder r = new Recorder();
            var v = validator(sql -> { }, UnaryOperator.identity());
            var run = runInBackground(v, r);
            Thread.sleep(1000);   // past the first 200 ms lock timeout: the read is in its 15 s backoff
            assertThat(run.isDone()).as("the plan read is waiting on the locked partition").isFalse();
            assertThat(r.total).as("no plan yet").isEqualTo(-1);
            long start = System.nanoTime();
            assertThat(r.cancel(v)).isTrue();
            var result = run.get(30, TimeUnit.SECONDS);
            assertThat(since(start)).as("the backoff was cut short").isLessThan(Duration.ofSeconds(5));
            assertThat(result.stopped()).isEqualTo("cancelled");
        } finally {
            held.rollback();
            held.close();
        }
        assertThat(validatedCount()).isZero();
    }

    /**
     * A cancel that lands after the last step, before the run's final read of the constraints: the 202 told the caller the
     * job is stopping, so the run ends {@code cancelled}, not done with {@code all_validated} unknown. The steps' work stays.
     * Kills: no stop check after the loop (the cancelled final read was taken for an unreadable answer and the run "finished").
     */
    @Test
    void aCancelAfterTheLastStep_endsTheRunCancelled_keepingEveryStepsWork() throws Exception {
        var v = validator(sql -> { }, UnaryOperator.identity());
        Recorder r = new Recorder();
        r.onLastStep = () -> r.cancel(v);
        var result = v.run(false, r);
        assertThat(r.steps).as("every step ran (non-vacuous)").hasSize(r.total).isNotEmpty();
        assertThat(result.stopped()).isEqualTo("cancelled");
        assertThat(result.allValidated()).isNull();
        long validated = r.steps.stream().filter(s -> s.outcome() == Outcome.VALIDATED).count();
        assertThat(validated).isPositive();
        assertThat(validatedCount()).as("what the steps validated stays validated").isEqualTo(validated);
    }
}
