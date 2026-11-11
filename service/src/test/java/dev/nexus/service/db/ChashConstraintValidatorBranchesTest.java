// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.ChashConformanceConstraints.Row;
import dev.nexus.service.db.ChashConstraintValidator.Outcome;
import dev.nexus.service.db.ChashConstraintValidator.Step;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Consumer;
import java.util.function.UnaryOperator;

/**
 * nexus-f3yqd, engine 0.1.190, fix round r1: the branches of {@link ChashConstraintValidator} the route tests do not reach,
 * each on a real PostgreSQL through the real validator, with a connection that fails where a real one rarely does (an
 * {@code Opener} that hands the validator a session over a {@link Proxy} connection which throws a chosen SQLSTATE on a
 * chosen statement, or rewrites it). Every test names the mutation it kills.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ChashConstraintValidatorBranchesTest {

    private static final String CHUNKS_CHECK = "chunks_chash_octet_check";
    private static final String MANIFEST_CHECK = "catalog_document_chunks_chash_octet_check";
    private static final String TENANT_A = "t-chashbr-a";
    private static final String COLLECTION = "knowledge__chashbr__bge-base-en-v15-768__v1";
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
            PgContainerHelper.seedServiceToken(ctx, "tok-a-chashbr-0123456789abcdef", TENANT_A, "a");
            PgContainerHelper.ensureTenantPartitions(ctx, TENANT_A);
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void fresh() throws Exception {
        System.setProperty(ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY, "200");
        System.setProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY, "3");
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
            ChashConstraintValidator.RETRY_BACKOFF_PROPERTY, ChashConstraintValidator.PAUSE_PROPERTY,
            ChashConstraintValidator.STATEMENT_TIMEOUT_PROPERTY, ChashConstraintValidator.PRECHECK_TIMEOUT_PROPERTY,
            ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY}) {
            System.clearProperty(p);
        }
    }

    // ── recording what a run tells ──────────────────────────────────────────

    private static final class Recorder implements ChashConstraintValidator.Listener {
        final List<Step> steps = new ArrayList<>();
        int total = -1;
        Runnable onPlanned = () -> { };
        Consumer<Step> onStepped = s -> { };
        volatile int stopAfter = Integer.MAX_VALUE;

        @Override
        public void planned(int total) {
            this.total = total;
            onPlanned.run();
        }

        @Override
        public void current(String table, Long bytes) {
        }

        @Override
        public void stepped(Step step) {
            steps.add(step);
            onStepped.accept(step);
        }

        @Override
        public String stopReason() {
            return steps.size() >= stopAfter ? "evicted" : null;
        }

        Step of(String table) {
            return steps.stream().filter(s -> s.table().equals(table)).findFirst().orElseThrow(
                () -> new AssertionError("no step for " + table + " in " + steps));
        }
    }

    private AdminConnection admin() {
        return new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
    }

    private ChashConstraintValidator validator() {
        return new ChashConstraintValidator(admin());
    }

    // ── a connection that fails where a real one rarely does ────────────────

    @FunctionalInterface
    interface SqlHook {
        void before(String sql) throws SQLException;
    }

    /** What the proxied sessions do to the statements they send. */
    private record Faults(SqlHook controllerSql, SqlHook stepSql, UnaryOperator<String> stepRewrite, SqlHook ddl,
                          UnaryOperator<String> ddlRewrite) {
        static Faults none() {
            return new Faults(null, sql -> { }, UnaryOperator.identity(), sql -> { }, UnaryOperator.identity());
        }

        Faults controller(SqlHook h) {
            return new Faults(h, stepSql, stepRewrite, ddl, ddlRewrite);
        }

        Faults step(SqlHook h) {
            return new Faults(controllerSql, h, stepRewrite, ddl, ddlRewrite);
        }

        Faults rewriteStep(UnaryOperator<String> r) {
            return new Faults(controllerSql, stepSql, r, ddl, ddlRewrite);
        }

        Faults ddl(SqlHook h) {
            return new Faults(controllerSql, stepSql, stepRewrite, h, ddlRewrite);
        }

        Faults rewriteDdl(UnaryOperator<String> r) {
            return new Faults(controllerSql, stepSql, stepRewrite, ddl, r);
        }
    }

    private Connection proxied(Connection real, SqlHook queryHook, UnaryOperator<String> rewrite, SqlHook ddlHook,
                               UnaryOperator<String> ddlRewrite) {
        return (Connection) Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{Connection.class}, (proxy, m, args) -> {
            try {
                if (m.getName().equals("prepareStatement") && args != null && args[0] instanceof String sql) {
                    queryHook.before(sql);
                    args[0] = rewrite.apply(sql);
                }
                Object out = m.invoke(real, args);
                if (m.getName().equals("createStatement") && out instanceof Statement st) {
                    return Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{Statement.class}, (p2, m2, a2) -> {
                        try {
                            if (m2.getName().equals("execute") && a2 != null && a2.length == 1 && a2[0] instanceof String sql) {
                                ddlHook.before(sql);
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

    /** The step sessions the last {@link #validator(Faults)} opened, so a test can cancel one and not the controller. */
    private final List<AdminDdlSession> stepSessions = new java.util.concurrent.CopyOnWriteArrayList<>();

    private ChashConstraintValidator validator(Faults f) {
        stepSessions.clear();
        return new ChashConstraintValidator(admin(), (a, settings, lock) -> {
            try {
                boolean proxyIt = lock ? f.controllerSql() != null : true;
                if (!proxyIt) return AdminDdlSession.open(a, settings, true);
                var props = new java.util.Properties();
                props.setProperty("user", a.user());
                props.setProperty("password", a.password());
                props.setProperty("ApplicationName", settings.applicationName());
                Connection real = DriverManager.getConnection(a.url(), props);
                real.setAutoCommit(true);
                Connection c = lock
                    ? proxied(real, f.controllerSql(), UnaryOperator.identity(), sql -> { }, UnaryOperator.identity())
                    : proxied(real, f.stepSql(), f.stepRewrite(), f.ddl(), f.ddlRewrite());
                var session = new AdminDdlSession(settings, c);
                if (!lock) stepSessions.add(session);
                return session;
            } catch (SQLException e) {
                throw new IllegalStateException(e);
            }
        }, null, null);
    }

    private static SQLException state(String sqlState) {
        return new SQLException("injected " + sqlState, sqlState);
    }

    /** A statement that answers 0 in place of the pre-check's count (it keeps the two bind places of the original). */
    private static final String ZERO_COUNT = DSL.using(SQLDialect.POSTGRES)
        .render(DSL.select(DSL.inline(0).mul(DSL.val(1)).mul(DSL.val(1))));
    /** A statement that does nothing, in place of a VALIDATE. */
    private static final String NO_OP = DSL.using(SQLDialect.POSTGRES).render(DSL.select(DSL.inline(1)));

    /** The relation names of the leaves of the chunks tree (constraint rows whose table has no copy below it). */
    private List<String> leaves() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var rows = ChashConformanceConstraints.query(DSL.using(su, SQLDialect.POSTGRES)).fetch();
            var parents = new java.util.HashSet<Long>();
            rows.forEach(r -> parents.add(r.value2()));
            return rows.stream().filter(r -> !"chunks_retired_225".equals(r.value3()) && r.value2() != 0 && !parents.contains(r.value1()))
                .map(r -> r.value3()).sorted().toList();
        }
    }

    private java.util.Map<String, Boolean> validity() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var out = new java.util.LinkedHashMap<String, Boolean>();
            for (var r : ChashConformanceConstraints.query(DSL.using(su, SQLDialect.POSTGRES)).fetch()) {
                if (!"chunks_retired_225".equals(r.value3())) out.put(r.value3(), r.value5());
            }
            return out;
        }
    }

    private static byte[] sha256(String text) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    /** One 16-byte chash in tenant A's chunks (the CHECK dropped, the row seeded, the CHECK back NOT VALID). */
    private void seedBadChunk() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT_A, COLLECTION);
            ctx.alterTable(CHUNKS).dropConstraint(CHUNKS_CHECK).execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraint(MANIFEST_CHECK).execute();
            try {
                ctx.insertInto(CHUNKS).set(CHUNKS.TENANT_ID, TENANT_A).set(CHUNKS.COLLECTION, COLLECTION)
                    .set(CHUNKS.CHASH, HexFormat.of().parseHex("c".repeat(32))).set(CHUNKS.EMBEDDING_MODEL, MODEL)
                    .set(CHUNKS.CHUNK_TEXT, "bad").set(CHUNKS.EMBEDDING_768, Vector.of(new float[768])).execute();
            } finally {
                PgContainerHelper.readdChashOctetChecksNotValid(su);
            }
        }
    }

    // ── bounds ──────────────────────────────────────────────────────────────

    /** Kills: a statement timeout property that is not clamped under the admin socket timeout (the client would read-timeout first). */
    @Test
    void theTimeoutPropertiesAreClampedUnderTheSocketTimeout_andThePauseDefaultsToASmallSpread() {
        System.setProperty(ChashConstraintValidator.STATEMENT_TIMEOUT_PROPERTY, "99999999999");
        System.setProperty(ChashConstraintValidator.PRECHECK_TIMEOUT_PROPERTY, "99999999999");
        System.setProperty(ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY, "99999999999");
        System.clearProperty(ChashConstraintValidator.PAUSE_PROPERTY);
        var cfg = ChashConstraintValidator.Config.fromProperties();
        long socketMs = 35L * 60 * 1000;
        assertThat(cfg.statementTimeoutMs()).isEqualTo(AdminDdlSession.MAX_STATEMENT_TIMEOUT_MS).isLessThan(socketMs);
        assertThat(cfg.precheckTimeoutMs()).isLessThan(socketMs);
        assertThat(cfg.lockTimeoutMs()).isLessThan(socketMs);
        assertThat(cfg.pauseMs()).as("a small default pause spreads the IO of a long job").isEqualTo(100L);
        System.setProperty(ChashConstraintValidator.STATEMENT_TIMEOUT_PROPERTY, "1");
        assertThat(ChashConstraintValidator.Config.fromProperties().statementTimeoutMs()).as("and has a floor").isEqualTo(1_000L);
    }

    // ── a step's own database errors ────────────────────────────────────────

    /** Kills: 57014 on the VALIDATE failing the job, or being retried as a lock, instead of deferring that step as statement_timeout. */
    @Test
    void aVALIDATEPastItsStatementTimeout_isDeferredAsStatementTimeout_andTheRunGoesOn() throws Exception {
        var v = validator(Faults.none().ddl(sql -> {
            if (sql.contains("VALIDATE CONSTRAINT") && sql.contains("chunks_m")) throw state("57014");
        }));
        Recorder r = new Recorder();
        var result = v.run(false, r);
        List<Step> deferred = r.steps.stream().filter(s -> s.outcome() == Outcome.DEFERRED && "statement_timeout".equals(s.reason())).toList();
        assertThat(deferred).as("every chunks relation timed out: " + r.steps).isNotEmpty();
        assertThat(result.stopped()).isNull();
        assertThat(r.of("nexus.catalog_document_chunks").outcome()).as("the manifest, untouched by the fault, validated").isEqualTo(Outcome.VALIDATED);
    }

    /** Kills: a deadlock (40P01) failing the job, or not being retried; and a retry with no bound. */
    @Test
    void aDeadlockOnTheVALIDATE_isRetried_andDeferredAfterTheAttempts() throws Exception {
        AtomicInteger first = new AtomicInteger();
        var once = validator(Faults.none().ddl(sql -> {
            if (sql.contains("catalog_document_chunks") && first.getAndIncrement() == 0) throw state("40P01");
        }));
        Recorder r1 = new Recorder();
        once.run(false, r1);
        assertThat(r1.of("nexus.catalog_document_chunks").outcome()).as("retried after one deadlock").isEqualTo(Outcome.VALIDATED);
        assertThat(first.get()).isEqualTo(2);

        fresh();
        AtomicInteger always = new AtomicInteger();
        var forever = validator(Faults.none().ddl(sql -> {
            if (sql.contains("catalog_document_chunks")) {
                always.incrementAndGet();
                throw state("40P01");
            }
        }));
        Recorder r2 = new Recorder();
        forever.run(false, r2);
        Step manifest = r2.of("nexus.catalog_document_chunks");
        assertThat(manifest.outcome()).isEqualTo(Outcome.DEFERRED);
        assertThat(manifest.reason()).isEqualTo("deadlock_detected");
        assertThat(always.get()).as("three attempts, then it gives up").isEqualTo(3);
    }

    /** Kills: a relation dropped between the plan and its step failing the whole job (42P01 propagating). */
    @Test
    @Order(Integer.MAX_VALUE)   // last: it drops a leaf for good
    void aLeafDroppedBetweenThePlanAndItsStep_defersThatStepOnly() throws Exception {
        List<String> leaves = leaves();
        assertThat(leaves).as("non-vacuous: the chunks tree has leaves").isNotEmpty();
        String dropped = leaves.get(0);
        Recorder r = new Recorder();
        r.onPlanned = () -> {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).dropTable(DSL.table(DSL.name("nexus", dropped))).cascade().execute();
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        };
        var result = validator().run(false, r);
        assertThat(result.stopped()).isNull();
        Step gone = r.of("nexus." + dropped);
        assertThat(gone.outcome()).isEqualTo(Outcome.DEFERRED);
        assertThat(gone.reason()).isEqualTo("relation_vanished");
        assertThat(r.of("nexus.catalog_document_chunks").outcome()).as("the job went on").isEqualTo(Outcome.VALIDATED);
        assertThat(r.steps.stream().filter(s -> s.outcome() == Outcome.VALIDATED)).hasSizeGreaterThan(2);
    }

    /** Kills: an error reading convalidated after a VALIDATE failing the job; and a VALIDATE that did not validate passing as done. */
    @Test
    void theStateReadAfterAVALIDATE_isDeferredOnAnErrorOrWhenNotValidated() throws Exception {
        var unreadable = validator(Faults.none().step(sql -> {
            if (sql.contains("convalidated")) throw state("58000");
        }));
        Recorder r1 = new Recorder();
        assertThat(unreadable.run(false, r1).stopped()).isNull();
        assertThat(r1.of("nexus.catalog_document_chunks").reason()).isEqualTo("state_unreadable");
        assertThat(r1.of("nexus.catalog_document_chunks").outcome()).isEqualTo(Outcome.DEFERRED);

        fresh();
        var noop = validator(Faults.none().rewriteDdl(sql -> sql.contains("VALIDATE CONSTRAINT") ? NO_OP : sql));
        Recorder r2 = new Recorder();
        noop.run(false, r2);
        assertThat(r2.of("nexus.catalog_document_chunks").reason()).isEqualTo("not_validated_after");
        assertThat(r2.steps.stream().filter(s -> s.outcome() == Outcome.VALIDATED)).as("nothing was validated").isEmpty();
    }

    // ── the pre-check ───────────────────────────────────────────────────────

    /** Kills: a pre-check timeout (57014) failing the job instead of being reported; and a dry run that guesses. */
    @Test
    void aPreCheckThatTimesOut_isReportedTimeout_aDryRunDefersIt_andARealRunLetsTheVALIDATEDecide() throws Exception {
        var v = validator(Faults.none().step(sql -> {
            if (sql.contains("octet_length")) throw state("57014");
        }));
        Recorder dry = new Recorder();
        v.run(true, dry);
        List<Step> leafSteps = dry.steps.stream().filter(s -> "precheck_timeout".equals(s.reason())).toList();
        assertThat(leafSteps).as("the leaves' pre-checks timed out: " + dry.steps).isNotEmpty();
        assertThat(leafSteps).allSatisfy(s -> {
            assertThat(s.outcome()).isEqualTo(Outcome.DEFERRED);
            assertThat(s.precheck()).isEqualTo("timeout");
        });
        Recorder real = new Recorder();
        v.run(false, real);
        assertThat(real.steps.stream().filter(s -> s.outcome() == Outcome.VALIDATED && "timeout".equals(s.precheck()))).as("the VALIDATE decided").isNotEmpty();
        assertThat(real.steps.stream().filter(s -> s.outcome() == Outcome.REFUSED)).isEmpty();
    }

    /** Kills: an error reading the role and policy catalogs failing the job instead of making the pre-check "failed". */
    @Test
    void aPreCheckWhoseCatalogReadFails_isFailed_notAJobFailure() throws Exception {
        var v = validator(Faults.none().step(sql -> {
            if (sql.contains("pg_roles")) throw state("58000");
        }));
        Recorder dry = new Recorder();
        assertThat(v.run(true, dry).stopped()).isNull();
        List<Step> failed = dry.steps.stream().filter(s -> "precheck_failed".equals(s.reason())).toList();
        assertThat(failed).isNotEmpty();
        assertThat(failed).allSatisfy(s -> assertThat(s.precheck()).isEqualTo("failed"));
        Recorder real = new Recorder();
        v.run(false, real);
        assertThat(real.steps.stream().filter(s -> s.outcome() == Outcome.VALIDATED)).isNotEmpty();
    }

    /**
     * A bad row the pre-check missed (here: its count rewritten to zero) is caught by the VALIDATE itself, 23514: refused,
     * the pre-check named {@code clean}, no count. Kills: 23514 treated as a deferral or a failure; a refusal that claims a count it did not take.
     */
    @Test
    void a23514AfterACleanPreCheck_isARefusalWithNoCount() throws Exception {
        seedBadChunk();
        var v = validator(Faults.none().rewriteStep(sql -> sql.contains("octet_length") && sql.contains("select count") ? ZERO_COUNT : sql));
        Recorder r = new Recorder();
        var result = v.run(false, r);
        List<Step> refused = r.steps.stream().filter(s -> s.outcome() == Outcome.REFUSED).toList();
        assertThat(refused).hasSize(1);
        assertThat(refused.get(0).reason()).isEqualTo("bad_rows");
        assertThat(refused.get(0).precheck()).isEqualTo("clean");
        assertThat(refused.get(0).badRows()).as("not counted: the count is what the pre-check would have said").isNull();
        assertThat(result.allValidated()).isFalse();
        assertThat(validity().get(refused.get(0).table().substring("nexus.".length()))).as("left NOT VALID").isFalse();
    }

    // ── the controller's own reads ──────────────────────────────────────────

    /** Kills: a failed final read of the constraints failing a job whose steps are all done (all_validated must be null, not an error). */
    @Test
    void aFinalReadThatFails_leavesAllValidatedNull_notAFailedJob() throws Exception {
        AtomicInteger reads = new AtomicInteger();
        var v = validator(Faults.none().controller(sql -> {
            if (sql.contains("pg_get_constraintdef") && reads.incrementAndGet() >= 2) throw state("58000");
        }));
        Recorder r = new Recorder();
        var result = v.run(false, r);
        assertThat(result.stopped()).isNull();
        assertThat(result.allValidated()).isNull();
        assertThat(r.steps.stream().filter(s -> s.outcome() == Outcome.VALIDATED)).as("every step ran").isNotEmpty();
        assertThat(validity().values()).doesNotContain(false);
    }

    private Connection holdAdvisory(long key) throws Exception {
        Connection c = pg.createConnection("");
        c.setAutoCommit(true);
        DSL.using(c, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_lock", SQLDataType.OTHER, DSL.val(key))).fetch();
        return c;
    }

    /** Kills: a second run starting beside the first (the 409 refusal gone). */
    @Test
    void aRunWhileAnotherHoldsTheRunLock_isRefused409() throws Exception {
        try (Connection held = holdAdvisory(ChashConstraintValidator.ADVISORY_LOCK_KEY)) {
            assertThatThrownBy(() -> validator().run(false, new Recorder())).isInstanceOfSatisfying(AdminDdlSession.Refused.class, e -> {
                assertThat(e.status()).isEqualTo(409);
                assertThat(e.reason()).isEqualTo("constraint_run_in_progress");
            });
        }
        assertThat(validity().values()).as("nothing was touched").doesNotContain(true);
    }

    /** Kills: a run that starts while a schema migration holds its lock (the 503 guard gone). */
    @Test
    void aRunWhileASchemaMigrationHoldsItsLock_isRefused503() throws Exception {
        try (Connection held = holdAdvisory(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY)) {
            assertThatThrownBy(() -> validator().run(false, new Recorder())).isInstanceOfSatisfying(AdminDdlSession.Refused.class, e -> {
                assertThat(e.status()).isEqualTo(503);
                assertThat(e.reason()).isEqualTo("migration_in_progress");
            });
        }
        assertThat(validity().values()).doesNotContain(true);
    }

    // ── stopping ────────────────────────────────────────────────────────────

    /** Kills: a stop reason (the tenant reset's eviction, the wall-clock cap) that is only asked at the start and not between steps. */
    @Test
    void aStopReasonBetweenSteps_endsTheRunAtThatBoundary_leavingTheRestUntouched() throws Exception {
        Recorder r = new Recorder();
        r.stopAfter = 3;
        var result = validator().run(false, r);
        assertThat(result.stopped()).isEqualTo("evicted");
        assertThat(result.allValidated()).isNull();
        assertThat(r.steps).hasSize(3);
        assertThat(validity().values().stream().filter(v -> v).count()).as("exactly the three steps' constraints").isEqualTo(3);
    }

    /** Kills: an interrupt (the engine's shutdown) that the run does not notice between steps. */
    @Test
    void anInterruptBetweenSteps_endsTheRunAsInterrupted() throws Exception {
        System.setProperty(ChashConstraintValidator.PAUSE_PROPERTY, "50");
        Recorder r = new Recorder();
        Thread self = Thread.currentThread();
        r.onStepped = s -> {
            if (r.steps.size() == 2) self.interrupt();
        };
        var result = validator().run(false, r);
        Thread.interrupted();
        assertThat(result.stopped()).isEqualTo("interrupted");
        assertThat(r.steps).hasSize(2);
    }

    // ── the engine's stop, in every phase of a step ─────────────────────────

    private static final String SLEEPING_COUNT = DSL.using(SQLDialect.POSTGRES).render(DSL.select(
        DSL.function("pg_sleep", SQLDataType.OTHER, DSL.inline(60)), DSL.val(1).cast(SQLDataType.INTEGER),
        DSL.val(1).cast(SQLDataType.INTEGER)));

    private boolean stepRunsASleep() throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(DSL.selectOne()
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(DSL.field(DSL.name("application_name"), String.class).startsWith(ChashConstraintValidator.APPLICATION_NAME_PREFIX + "-step"))
                .and(DSL.field(DSL.name("state"), String.class).eq("active"))
                .and(DSL.field(DSL.name("query"), String.class).contains("pg_sleep")));
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

    /**
     * A stop that lands in a step's prelude (here: stalled in the size read, before any statement of the pre-check or the
     * VALIDATE) leaves no statement in flight to cancel, so the step must see the stop at its next checkpoint: no pre-check
     * and no VALIDATE is ever started. Kills: the checkpoints between the prelude and the first statement of the phase gone
     * (the VALIDATE would start after the engine had stopped).
     */
    @Test
    void aStopDuringTheStepsPrelude_startsNoPreCheckAndNoVALIDATE() throws Exception {
        CountDownLatch entered = new CountDownLatch(1);
        CountDownLatch gate = new CountDownLatch(1);
        AtomicInteger prechecks = new AtomicInteger();
        AtomicInteger validates = new AtomicInteger();
        var v = validator(Faults.none().step(sql -> {
            if (sql.contains("pg_total_relation_size")) {
                entered.countDown();
                while (true) {   // the stalled prelude: not interruptible, as a JDBC read is not
                    try {
                        gate.await();
                        break;
                    } catch (InterruptedException e) {
                        // keep waiting; the flag is lost here, the engine's cancel flag is what the step must see
                    }
                }
            }
            if (sql.contains("octet_length")) prechecks.incrementAndGet();
        }).ddl(sql -> {
            if (sql.contains("VALIDATE CONSTRAINT")) validates.incrementAndGet();
        }));
        Recorder r = new Recorder();
        CompletableFuture<ChashConstraintValidator.Result> run = new CompletableFuture<>();
        Thread worker = new Thread(() -> {
            try {
                run.complete(v.run(false, r));
            } catch (Throwable t) {
                run.completeExceptionally(t);
            }
        });
        worker.start();
        assertThat(entered.await(30, TimeUnit.SECONDS)).as("the step is in its prelude").isTrue();
        worker.interrupt();       // the engine's stop: interrupt first,
        v.cancelRunning();        // then cancel what is in flight (nothing is: the step is between statements)
        gate.countDown();
        var result = run.get(30, TimeUnit.SECONDS);
        assertThat(result.stopped()).isEqualTo("interrupted");
        assertThat(prechecks.get()).as("no pre-check statement was started after the stop").isZero();
        assertThat(validates.get()).as("no VALIDATE was ever started after the stop").isZero();
        assertThat(validity().values()).as("nothing was validated").doesNotContain(true);
    }

    /**
     * A stop during a pre-check (a count that would run for 60 s) cancels that statement and ends the step as a stop: the
     * cancelled pre-check must not read as a "timeout". In a real run the VALIDATE must not follow; in a DRY run (no VALIDATE
     * to guard) the step must not report a deferred "precheck_timeout" for a statement the stop cut. Kills: the cancel
     * reaching DDL only (the sleep runs its minute); and, in the dry run, the stop checkpoint after the pre-check gone (the
     * step would report {@code DEFERRED precheck_timeout}, where a real run's later checkpoints would still catch it).
     */
    @Test
    void aStopDuringAPreCheck_cancelsItsStatement_andNoVALIDATEFollows() throws Exception {
        stopDuringAPreCheck(false);
    }

    @Test
    void aStopDuringADryRunsPreCheck_reportsNoStepForTheCutStatement() throws Exception {
        stopDuringAPreCheck(true);
    }

    private void stopDuringAPreCheck(boolean dryRun) throws Exception {
        AtomicInteger validates = new AtomicInteger();
        var v = validator(Faults.none()
            .rewriteStep(sql -> sql.contains("octet_length") && sql.contains("select count") ? SLEEPING_COUNT : sql)
            .ddl(sql -> {
                if (sql.contains("VALIDATE CONSTRAINT")) validates.incrementAndGet();
            }));
        Recorder r = new Recorder();
        CompletableFuture<ChashConstraintValidator.Result> run = new CompletableFuture<>();
        Thread worker = new Thread(() -> {
            try {
                run.complete(v.run(dryRun, r));
            } catch (Throwable t) {
                run.completeExceptionally(t);
            }
        });
        long start = System.nanoTime();
        worker.start();
        await(() -> run.isDone() || stepRunsASleep(), "the pre-check's statement is in flight on the server");
        assertThat(run.isDone()).as("the run is still going (it ended with: " + (run.isDone() ? run.handle((x, e) -> e == null ? x : e).get() : "") + ")").isFalse();
        worker.interrupt();
        v.cancelRunning();
        var result = run.get(30, TimeUnit.SECONDS);
        assertThat(result.stopped()).isEqualTo("interrupted");
        assertThat(java.time.Duration.ofNanos(System.nanoTime() - start)).as("the sleep was cancelled, not waited out")
            .isLessThan(java.time.Duration.ofSeconds(30));
        assertThat(validates.get()).isZero();
        assertThat(r.steps).as("the interrupted step reported no result").isEmpty();
    }

    // ── a cancelled session starts no statement (F1), and cancels airtight (F2) ──

    private AdminDdlSession.Settings testSettings(long lockKey) {
        return new AdminDdlSession.Settings("nexus-chash-validate-step-test", lockKey, "test", "nexus", "chunks", "x");
    }

    private static final org.jooq.Field<?> SLEEP_30 = DSL.function("pg_sleep", SQLDataType.OTHER, DSL.inline(30));

    /**
     * After a cancel (or on an interrupted thread) a jOOQ query of the session is refused with 57014 before it reaches the
     * server: a 30 s sleep returns at once and none is ever active on the server. A cancelled locked session still closes and
     * releases its run lock (its unlock is refused and swallowed, and closing the connection frees the lock). Kills: the check
     * in the listener's executeStart gone (the sleep runs).
     */
    @Test
    void aCancelledSession_refusesAJooqQueryBeforeItReachesTheServer_butStillCloses() throws Exception {
        long key = 0x6368617368313131L;
        try (AdminDdlSession s = AdminDdlSession.open(admin(), testSettings(key), true)) {
            s.cancel();
            long start = System.nanoTime();
            assertThatThrownBy(() -> s.ctx().select(SLEEP_30).fetch()).isInstanceOfSatisfying(org.jooq.exception.DataAccessException.class,
                e -> assertThat(e.sqlState()).isEqualTo("57014"));
            assertThat(java.time.Duration.ofNanos(System.nanoTime() - start)).as("it never reached the server").isLessThan(java.time.Duration.ofSeconds(5));
            assertThat(stepRunsASleep()).as("no sleep is active on the server").isFalse();
        }
        try (Connection c = pg.createConnection("")) {
            assertThat(DSL.using(c, SQLDialect.POSTGRES).select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN, DSL.val(key)))
                .fetchOne(0, Boolean.class)).as("the cancelled session released its run lock on close").isTrue();
        }
        try (AdminDdlSession s = AdminDdlSession.open(admin(), testSettings(1L), false)) {
            Thread.currentThread().interrupt();
            try {
                assertThatThrownBy(() -> s.ctx().select(SLEEP_30).fetch()).isInstanceOfSatisfying(org.jooq.exception.DataAccessException.class,
                    e -> assertThat(e.sqlState()).isEqualTo("57014"));
            } finally {
                Thread.interrupted();
            }
        }
    }

    /**
     * The backend cancel alone ends a statement in flight (the driver's statement cancel is switched off by the test seam),
     * which is what covers a cancel the driver sent in the instant before the server started the statement. Kills: the
     * pg_cancel_backend re-send gone (the sleep runs its minute and the join times out).
     */
    @Test
    void theBackendCancelAlone_endsAStatementInFlight() throws Exception {
        try (AdminDdlSession s = AdminDdlSession.open(admin(), testSettings(2L), false)) {
            s.statementCancelDisabledForTests = true;
            CompletableFuture<Object> ended = new CompletableFuture<>();
            Thread worker = new Thread(() -> {
                try {
                    s.ctx().select(DSL.function("pg_sleep", SQLDataType.OTHER, DSL.inline(60))).fetch();
                    ended.complete("finished");
                } catch (org.jooq.exception.DataAccessException e) {
                    ended.complete(e.sqlState());
                }
            });
            worker.start();
            await(this::stepRunsASleep, "the sleep is in flight");
            long start = System.nanoTime();
            s.cancel();
            assertThat(ended.get(30, TimeUnit.SECONDS)).isEqualTo("57014");
            assertThat(java.time.Duration.ofNanos(System.nanoTime() - start)).isLessThan(java.time.Duration.ofSeconds(20));
        }
    }

    private static void clearCancelProperty() {
        System.clearProperty(AdminDdlSession.CANCEL_DEADLINE_PROPERTY);
    }

    private void terminateStepBackends() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.select(DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN, DSL.field(DSL.name("pid"), Integer.class)))
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(DSL.field(DSL.name("application_name"), String.class).startsWith("nexus-chash-validate-step-test")).fetch();
        }
    }

    private CompletableFuture<Object> sleepOn(AdminDdlSession s) {
        CompletableFuture<Object> ended = new CompletableFuture<>();
        Thread worker = new Thread(() -> {
            try {
                s.ctx().select(DSL.function("pg_sleep", SQLDataType.OTHER, DSL.inline(30))).fetch();
                ended.complete("finished");
            } catch (org.jooq.exception.DataAccessException e) {
                ended.complete(e.sqlState());
            }
        });
        worker.setDaemon(true);
        worker.start();
        return ended;
    }

    /**
     * The re-send's server does not answer (a socket that accepts and says nothing): cancel() still returns within its
     * wall-clock deadline, with the statement still running (the signal never arrived) and the session cancelled, so the
     * engine's synchronous shutdown stays bounded. Kills: an iteration-count bound (50 re-sends of up to 10 s each) or timeouts
     * that do not fit the deadline: the call would take far longer than the deadline.
     */
    @Test
    void anUnreachableResend_stillReturnsWithinTheDeadline() throws Exception {
        System.setProperty(AdminDdlSession.CANCEL_DEADLINE_PROPERTY, "1500");
        try (java.net.ServerSocket blackhole = new java.net.ServerSocket(0, 50, java.net.InetAddress.getLoopbackAddress());
             AdminDdlSession s = AdminDdlSession.open(admin(), testSettings(3L), false)) {
            s.statementCancelDisabledForTests = true;
            s.resendAdminForTests = new AdminConnection("jdbc:postgresql://127.0.0.1:" + blackhole.getLocalPort() + "/postgres", "u", "p");
            CompletableFuture<Object> ended = sleepOn(s);
            await(this::stepRunsASleep, "the sleep is in flight");
            long start = System.nanoTime();
            s.cancel();
            assertThat(java.time.Duration.ofNanos(System.nanoTime() - start)).as("bounded by the deadline, not by the hung connection")
                .isLessThan(java.time.Duration.ofMillis(4_000));
            assertThat(s.isCancelled()).isTrue();
            assertThat(ended.isDone()).as("the signal never reached the backend: the statement runs on").isFalse();
            terminateStepBackends();
            assertThat(ended.get(30, TimeUnit.SECONDS)).isNotEqualTo("finished");
        } finally {
            clearCancelProperty();
        }
    }

    /**
     * The re-send signals only the backend recorded at open, by pid AND start time: a session whose recorded start differs
     * (the pid has been reused by another backend) is not signalled, and the same session with its true start is. Kills: the
     * backend_start condition gone (a reused pid would be signalled).
     */
    @Test
    void aPidWhoseBackendStartDiffers_isNotSignalled() throws Exception {
        System.setProperty(AdminDdlSession.CANCEL_DEADLINE_PROPERTY, "1200");
        try (AdminDdlSession s = AdminDdlSession.open(admin(), testSettings(4L), false)) {
            s.statementCancelDisabledForTests = true;
            java.time.OffsetDateTime real = s.backendStartForTests();
            assertThat(real).as("recorded at open").isNotNull();
            CompletableFuture<Object> ended = sleepOn(s);
            await(this::stepRunsASleep, "the sleep is in flight");

            s.backendStartForTests(real.minusDays(1));
            s.cancel();
            assertThat(ended.isDone()).as("a different start time: not the same backend, not signalled").isFalse();
            assertThat(stepRunsASleep()).isTrue();

            s.backendStartForTests(real);
            s.cancel();
            assertThat(ended.get(30, TimeUnit.SECONDS)).as("the recorded backend is signalled and the statement ends cancelled").isEqualTo("57014");
        } finally {
            clearCancelProperty();
        }
    }

    // ── a statement the stop cancelled is the run's stop, not its failure (F3) ──

    /** Kills: the controller-level catch gone (the cancelled plan read surfaces as a failed job, not stopped=interrupted). */
    @Test
    void aControllerStatementCancelledByTheStop_endsTheRunAsInterrupted_notAsAFailure() throws Exception {
        java.util.concurrent.atomic.AtomicReference<ChashConstraintValidator> self = new java.util.concurrent.atomic.AtomicReference<>();
        var v = validator(Faults.none().controller(sql -> {
            if (sql.contains("pg_get_constraintdef")) {
                self.get().cancelRunning();   // the engine's stop lands during the plan read ...
                throw state("57014");         // ... and the statement ends cancelled
            }
        }));
        self.set(v);
        var result = v.run(false, new Recorder());
        assertThat(result.stopped()).isEqualTo("interrupted");
    }

    /**
     * Only the STEP's session is cancelled (not the controller's), and its guard statement ends 57014: the step's own mapping
     * returns the run's stop. Kills: the step prelude's catch gone (the controller-level catch does not see a cancelled step,
     * so the job would fail with the DataAccessException).
     */
    @Test
    void aStepPreludeStatementCancelledByTheStop_endsTheRunAsInterrupted_notAsAFailure() throws Exception {
        var v = validator(Faults.none().step(sql -> {
            if (sql.contains("set_config")) {   // the guard's session settings
                stepSessions.get(0).cancel();
                throw state("57014");
            }
        }));
        Recorder r = new Recorder();
        var result = v.run(false, r);
        assertThat(result.stopped()).isEqualTo("interrupted");
        assertThat(r.steps).isEmpty();
    }

    /**
     * A cancel that arrives before a DDL statement starts (no statement in flight to cancel) makes the session refuse to
     * start it: the constraint stays NOT VALID. Kills: the flag check after the statement is published gone (the race the
     * engine's stop would lose: the VALIDATE would run).
     */
    @Test
    void aCancelBeforeADDLStarts_makesTheSessionRefuseIt_andAnInterruptedThreadToo() throws Exception {
        String leaf = leaves().get(0);
        var settings = new AdminDdlSession.Settings("nexus-chash-validate-step-test", 1L, "test", "nexus", leaf, "x");
        try (AdminDdlSession s = AdminDdlSession.open(admin(), settings, false)) {
            s.cancel();
            assertThat(s.isCancelled()).isTrue();
            assertThatThrownBy(() -> s.ddl("ALTER TABLE %I.%I VALIDATE CONSTRAINT %I", List.of("nexus", leaf, CHUNKS_CHECK)))
                .isInstanceOfSatisfying(AdminDdlSession.Refused.class, e -> assertThat(e.sqlState()).isEqualTo("57014"));
        }
        assertThat(validity().get(leaf)).as("not validated: the statement never started").isFalse();
        try (AdminDdlSession s = AdminDdlSession.open(admin(), settings, false)) {
            Thread.currentThread().interrupt();
            try {
                assertThatThrownBy(() -> s.ddl("ALTER TABLE %I.%I VALIDATE CONSTRAINT %I", List.of("nexus", leaf, CHUNKS_CHECK)))
                    .isInstanceOfSatisfying(AdminDdlSession.Refused.class, e -> assertThat(e.sqlState()).isEqualTo("57014"));
            } finally {
                Thread.interrupted();
            }
        }
        assertThat(validity().get(leaf)).isFalse();
    }

    // ── a weaker copy on a partition ────────────────────────────────────────

    /**
     * One leaf's copy deparses to something weaker (a hand-edited catalog: PostgreSQL will not attach such a partition): that
     * leaf is refused, and every table above it is deferred, because validating the parent would recurse into the weak copy
     * and validate it. Kills: an ancestor of a refused (not merely a deferred) descendant being validated anyway.
     */
    @Test
    void aWeakerPartitionCopy_isRefused_andBlocksItsAncestors() throws Exception {
        List<String> leaves = leaves();
        String weak = leaves.get(0);
        var v = validator();
        v.planRowsForTests(rows -> rows.stream().map(r -> r.relationName().equals(weak)
            ? new Row(r.relation(), r.relationParent(), r.relationName(), r.name(), r.validated(),
                "CHECK ((octet_length(chash) >= 1)) NOT VALID") : r).toList());
        Recorder r = new Recorder();
        v.run(false, r);
        assertThat(r.of("nexus." + weak).outcome()).isEqualTo(Outcome.REFUSED);
        assertThat(r.of("nexus." + weak).reason()).isEqualTo("definition_mismatch");
        assertThat(r.of("nexus.chunks").outcome()).isEqualTo(Outcome.DEFERRED);
        assertThat(r.of("nexus.chunks").reason()).isEqualTo("descendant_not_validated");
        assertThat(validity().get(weak)).isFalse();
        assertThat(validity().get("chunks")).as("the parent stays NOT VALID").isFalse();
        assertThat(r.steps.stream().filter(s -> s.outcome() == Outcome.REFUSED)).hasSize(1);
        assertThat(r.steps.stream().filter(s -> s.outcome() == Outcome.VALIDATED).map(Step::table)).as("every other leaf validated")
            .contains("nexus." + leaves.get(1));
    }
}
