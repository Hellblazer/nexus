// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.Field;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.DriverManager;
import java.util.List;
import java.util.Properties;
import java.util.Set;
import java.util.TreeSet;

/**
 * RDR-246 amendment (Sam, 2026-11-21, T2 nexus/sam-ruling-split-move-bounds-2026-11-21): the finisher's two bounds. The first,
 * READ ONLY verification pass runs under {@code verify_statement_timeout_ms} and is the warm-up; the completion transaction
 * keeps {@code statement_timeout_ms} and {@code lock_timeout}, and runs the verification again, warm, before {@code completed_at}.
 *
 * <p>A cold scan is made slow without a clock or a cache: {@code verification-cold.sql} sleeps 2 s while the one row of
 * {@code dm_test.cold_marker} says cold, and the test flips the row between the two transactions at the finisher's
 * {@code beforeCompletionHook} (a READ ONLY transaction cannot write the marker itself). That is the whole of the model of
 * warming: the first run is slow and the second is fast. The move's {@code statement_timeout_ms} is 1 s, the sleep is 2 s,
 * {@code verify_statement_timeout_ms} is 10 s, so a finisher that bounds the first pass by {@code statement_timeout_ms} times out.
 */
class DataMoveSplitBoundsIntegrationTest extends DataMoveIntegrationBase {

    private static final Field<Long> ID = DSL.field(DSL.name("id"), Long.class);
    private static final Field<Boolean> COLD = DSL.field(DSL.name("cold"), Boolean.class);

    private void setCold(boolean cold) {
        ctx().update(table("dm_test", "cold_marker")).set(COLD, cold).execute();
    }

    /** A move over {@code dm_test.tenant_plain} (runs done) whose verification sleeps while cold, with the two bounds. */
    private void registerCold(String label, int statementMs, int verifyMs, String onComplete) {
        DataMoveTestFixtures.register(ctx(), label, "tenant_plain", 50, 0, null, "stamp-template.sql",
            "verification-cold.sql", statementMs, onComplete);
        DataMoveTestFixtures.setVerifyStatementTimeout(ctx(), label, verifyMs);
        runsOnly(label);
    }

    private boolean columnExists(String column) {
        return ctx().fetchExists(table("information_schema", "columns"),
            DSL.field(DSL.name("table_schema"), String.class).eq("dm_test")
                .and(DSL.field(DSL.name("table_name"), String.class).eq("tenant_plain"))
                .and(DSL.field(DSL.name("column_name"), String.class).eq(column)));
    }

    private Connection adminLogin(String applicationName) throws Exception {
        var props = new Properties();
        props.setProperty("user", ADMIN);
        props.setProperty("password", ADMIN_PASSWORD);
        props.setProperty("ApplicationName", applicationName);
        return DriverManager.getConnection(pg.getJdbcUrl(), props);
    }

    /** ROW SHARE on {@code dm_test.tenant_plain}, held until the connection ends its transaction: what an ALTER TABLE queues behind. */
    private static Connection holdingTenantPlain(Connection c) throws Exception {
        c.setAutoCommit(false);
        DSL.using(c, org.jooq.SQLDialect.POSTGRES).select(DSL.one()).from(table("dm_test", "tenant_plain")).forShare().fetch();
        return c;
    }

    // ── (a) a cold verification that outlasts the completion's bound but fits the first pass's ─────────────

    @Test
    void aColdVerification_overStatementTimeoutMsButInsideVerifyStatementTimeoutMs_completesTheMove() {
        registerCold("sb-cold", 1_000, 10_000, null);
        setCold(true);
        var finisher = new VerifyingFinisher();
        finisher.beforeCompletionHook = () -> setCold(false);   // the first pass warmed it

        var result = finish(finisher, "sb-cold", "tenant_plain", List.of());

        assertThat(result.outcome()).as("the first pass took 2 s under a 10 s bound; the re-check took none under a 1 s bound")
            .isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(completedAt("sb-cold")).isNotNull();
    }

    @Test
    void aVerificationThatIsStillColdInTheCompletionTransaction_isBoundedByStatementTimeoutMs_andRetried() {
        registerCold("sb-still-cold", 1_000, 10_000, "on-complete-add-marker.sql");
        setCold(true);   // never warmed: the re-check sleeps 2 s under the completion's 1 s bound

        long started = System.nanoTime();
        var result = finish("sb-still-cold", "tenant_plain", List.of());
        long elapsedMs = (System.nanoTime() - started) / 1_000_000L;

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage().name()).isEqualTo("RECHECK");
        assertThat(result.failure().raised().getSQLState()).as("query_canceled: statement_timeout").isEqualTo("57014");
        assertThat(result.failure().detail()).contains("completion transaction");
        assertThat(elapsedMs).as("the first pass's 2 s, then the re-check cut at 1 s").isBetween(2_800L, 6_000L);
        assertThat(DataMoveFailureClassifier.classify(result.failure().stage(), result.failure().raised(), List.of(),
            result.failure().detail()).permanent()).as("a timed-out re-check retries").isFalse();
        assertThat(completedAt("sb-still-cold")).as("completed_at rolled back with the re-check").isNull();
        assertThat(columnExists("marker")).as("on_complete did not run").isFalse();
    }

    @Test
    void aFirstPassThatOutlastsVerifyStatementTimeoutMs_isAVerificationFailure_notACompletion() {
        registerCold("sb-first-slow", 60_000, 1_000, null);
        setCold(true);

        var result = finish("sb-first-slow", "tenant_plain", List.of());

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.VERIFICATION);
        assertThat(result.failure().raised().getSQLState()).isEqualTo("57014");
        assertThat(DataMoveFailureClassifier.classify(result.failure().stage(), result.failure().raised(), List.of(),
            result.failure().detail()).permanent()).as("the existing retry and backoff").isFalse();
        assertThat(completedAt("sb-first-slow")).isNull();
    }

    // ── (b) the completion's lock bound ─────────────────────────────────────────────────────────────────

    @Test
    void theCompletionsLockTimeout_isStillEnforced_whenAnotherConnectionHoldsTheTargetsLock() throws Exception {
        DataMoveTestFixtures.register(ctx(), "sb-lock", "tenant_plain", 50, 0, null, "stamp-template.sql",
            "verification-tenant-plain.sql", 4_000, "on-complete-add-marker.sql");
        runsOnly("sb-lock");
        // ROW SHARE on the table: neither verification SELECT queues behind it, the ALTER TABLE does
        try (Connection holder = holdingTenantPlain(adminLogin("sb-lock-holder"))) {
            long started = System.nanoTime();
            var result = finish("sb-lock", "tenant_plain", List.of());
            long elapsedMs = (System.nanoTime() - started) / 1_000_000L;

            assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
            assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.ON_COMPLETE);
            assertThat(result.failure().raised().getSQLState()).as("lock_not_available").isEqualTo("55P03");
            assertThat(elapsedMs).as("lock_timeout is half of statement_timeout_ms: 2 s").isBetween(1_500L, 3_900L);
            assertThat(completedAt("sb-lock")).as("the completion rolled back with it").isNull();
            assertThat(columnExists("marker")).isFalse();
            assertThat(DataMoveFailureClassifier.classify(result.failure().stage(), result.failure().raised(), List.of(),
                result.failure().detail()).permanent()).isFalse();
            holder.rollback();
        }

        assertThat(finish("sb-lock", "tenant_plain", List.of()).outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(columnExists("marker")).as("on_complete ran once the lock was free").isTrue();
    }

    // ── (c) a write between the two transactions ────────────────────────────────────────────────────────

    @Test
    void aWriteBetweenTheTwoTransactions_thatMakesTheVerificationNonEmpty_isCaughtByTheRecheck() {
        DataMoveTestFixtures.register(ctx(), "sb-slip", "tenant_plain", 50, 0, null, "stamp-template.sql",
            "verification-tenant-plain.sql", 60_000, "on-complete-add-marker.sql");
        runsOnly("sb-slip");
        var finisher = new VerifyingFinisher();
        // after the first pass found nothing and the row was read again: an old-shape row (n = 0) lands
        finisher.beforeCompletionHook = () ->
            ctx().insertInto(table("dm_test", "tenant_plain")).columns(TENANT_ID, ID).values("acme", 9_000L).execute();

        var result = finish(finisher, "sb-slip", "tenant_plain", List.of());

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.VERIFICATION_NONEMPTY);
        assertThat(result.reason()).contains("completion transaction").contains("table-wide=1");
        assertThat(completedAt("sb-slip")).as("the move does not complete").isNull();
        assertThat(columnExists("marker")).as("on_complete did not run").isFalse();
        assertThat(DataMoveRegistry.reload(ctx(), "sb-slip")).as("the move is still open for the failure handler").isPresent();
    }

    // ── (c2) a write that only a tenant's own session can see ───────────────────────────────────────────

    /**
     * {@code dm_test.forced_noread} is FORCE row-level security with no policy for the migrating role: a session with no
     * {@code nexus.tenant} reads none of its rows (as with a verification that reads an RLS-scoped helper), so the table-wide run
     * is empty whatever the table holds and only a per-tenant run is a check. The runs are done by marking every row moved.
     */
    private void registerForcedNoRead(String label) {
        DataMoveTestFixtures.register(ctx(), label, "forced_noread", 50, 0, null, "stamp-template.sql",
            "verification-forced-noread.sql", 60_000, "on-complete-add-marker.sql");
        ctx().update(table("dm_test", "forced_noread")).set(N, 1).execute();   // the superuser's session reads every row
    }

    @Test
    void aWriteOnlyATenantsOwnSessionCanSee_isCaughtByTheRecheck_perTenant() {
        registerForcedNoRead("sb-tenant-slip");
        var finisher = new VerifyingFinisher();
        finisher.beforeCompletionHook = () ->
            ctx().insertInto(table("dm_test", "forced_noread")).columns(TENANT_ID, ID, N).values("acme", 9_000L, 0).execute();

        var result = finish(finisher, "sb-tenant-slip", "forced_noread", List.of("acme", "globex"));

        assertThat(result.outcome()).as("the table-wide run reads no row without a tenant, so only acme's own run can see it")
            .isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.VERIFICATION_NONEMPTY);
        assertThat(result.reason()).contains("completion transaction").contains("tenant acme=1").doesNotContain("tenant globex");
        assertThat(completedAt("sb-tenant-slip")).isNull();
        assertThat(columnExists("marker")).as("on_complete did not run").isFalse();
    }

    @Test
    void theForcedFixture_isInvisibleWithoutATenant_soTheFirstPassAloneCannotSeeALateRow() {
        // control (non-vacuity): the same late row, present before the finish, is found by the first pass's per-tenant run
        // and by nothing else
        registerForcedNoRead("sb-tenant-control");
        ctx().insertInto(table("dm_test", "forced_noread")).columns(TENANT_ID, ID, N).values("acme", 9_000L, 0).execute();

        var result = finish("sb-tenant-control", "forced_noread", List.of("acme", "globex"));

        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.VERIFICATION_NONEMPTY);
        assertThat(result.reason()).contains("tenant acme=1").doesNotContain("table-wide");
        // and with no tenant listed, the first pass is the table-wide run alone, which sees nothing: the move completes
        // although a row of the old shape is there (the registrant's duty to list the tenants of a tenant-scoped move)
        assertThat(finish("sb-tenant-control", "forced_noread", List.of()).outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
    }

    // ── (b2) the re-check under the completion's lock_timeout ──────────────────────────────────────────

    @Test
    void aLockWaitInTheRecheck_isTransient_notAPermanentVerificationFailure_andTheMoveCompletesAfterTheRelease() throws Exception {
        DataMoveTestFixtures.register(ctx(), "sb-recheck-lock", "tenant_plain", 50, 0, null, "stamp-template.sql",
            "verification-tenant-plain.sql", 4_000, null);
        runsOnly("sb-recheck-lock");
        Connection[] holder = new Connection[1];
        var finisher = new VerifyingFinisher();
        // after the first pass, before the completion transaction: another session takes ACCESS EXCLUSIVE on the driving table
        // (an uncommitted rename of it), so the re-check's SELECT waits behind it and meets the completion's lock_timeout (half of 4 s)
        finisher.beforeCompletionHook = () -> {
            try {
                holder[0] = adminLogin("sb-recheck-lock-holder");
                holder[0].setAutoCommit(false);
                // an uncommitted rename: DDL in an open transaction holds ACCESS EXCLUSIVE until it ends (typed, as ChashValidateJobRouteTest does)
                DSL.using(holder[0], org.jooq.SQLDialect.POSTGRES).alterTable(table("dm_test", "tenant_plain"))
                    .renameTo(DSL.name("tenant_plain_held")).execute();
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        };
        try {
            long started = System.nanoTime();
            var result = finish(finisher, "sb-recheck-lock", "tenant_plain", List.of());
            long elapsedMs = (System.nanoTime() - started) / 1_000_000L;

            assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
            assertThat(result.failure().raised().getSQLState()).as("lock_not_available, not the statement timeout").isEqualTo("55P03");
            assertThat(DataMoveFailureClassifier.classify(result.failure().stage(), result.failure().raised(), List.of(),
                result.failure().detail()).permanent()).as("a lock wait is the host's: retried, never failed for good").isFalse();
            assertThat(result.failure().stage().name()).isEqualTo("RECHECK");
            assertThat(elapsedMs).as("lock_timeout is half of statement_timeout_ms: 2 s").isBetween(1_500L, 3_900L);
            assertThat(completedAt("sb-recheck-lock")).isNull();
            assertThat(DataMoveRegistry.reload(ctx(), "sb-recheck-lock")).as("the move is still open").isPresent();
        } finally {
            if (holder[0] != null) {
                holder[0].rollback();
                holder[0].close();
            }
        }

        assertThat(finish("sb-recheck-lock", "tenant_plain", List.of()).outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(completedAt("sb-recheck-lock")).isNotNull();
    }

    // ── (b3) what a re-check failure is, by cause ───────────────────────────────────────────────────────

    @Test
    void aNonSqlExceptionInTheRecheck_isAPermanentPassFailure_notATransientRetryLoop() {
        DataMoveTestFixtures.register(ctx(), "sb-bug", "tenant_plain", 50, 0, null, "stamp-template.sql",
            "verification-tenant-plain.sql", 60_000, "on-complete-add-marker.sql");
        runsOnly("sb-bug");
        var finisher = new VerifyingFinisher();
        finisher.recheckHook = tx -> {
            throw new IllegalStateException("a defect of the worker's own");
        };

        var result = finish(finisher, "sb-bug", "tenant_plain", List.of());

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).as("not RECHECK: nothing the host did, and no retry changes it")
            .isEqualTo(FailureHandler.Stage.PASS);
        assertThat((Throwable) result.failure().raised()).isNull();
        assertThat(DataMoveFailureClassifier.classify(result.failure().stage(), result.failure().raised(), List.of(),
            result.failure().detail()).permanent()).isTrue();
        assertThat(result.failure().detail()).contains("completion transaction").contains("a defect of the worker's own");
        assertThat(completedAt("sb-bug")).isNull();
        assertThat(columnExists("marker")).as("on_complete did not run").isFalse();
    }

    @Test
    void aSessionThatCarriesATenantIntoTheRecheck_isTheOneTransientRecheckWithNothingRaised() {
        DataMoveTestFixtures.register(ctx(), "sb-leak", "tenant_plain", 50, 0, null, "stamp-template.sql",
            "verification-tenant-plain.sql", 60_000, "on-complete-add-marker.sql");
        runsOnly("sb-leak");
        var finisher = new VerifyingFinisher();
        finisher.recheckHook = tx -> tx.select(DSL.function("set_config", String.class, DSL.val("nexus.tenant"),
            DSL.val("acme"), DSL.inline(true))).fetch();

        var result = finish(finisher, "sb-leak", "tenant_plain", List.of());

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage().name()).isEqualTo("RECHECK");
        assertThat((Throwable) result.failure().raised()).isNull();
        assertThat(result.failure().detail()).contains("carries nexus.tenant=acme");
        assertThat(DataMoveFailureClassifier.classify(result.failure().stage(), result.failure().raised(), List.of(),
            result.failure().detail()).permanent()).as("the connection's state: retried").isFalse();
        assertThat(completedAt("sb-leak")).isNull();
    }

    // ── (c1) the tenant list is read again for the re-check ─────────────────────────────────────────────

    @Test
    void aTenantWithATokenSinceTheFirstPass_isRunByTheRecheck() {
        registerForcedNoRead("sb-new-token");
        var finisher = new VerifyingFinisher();
        finisher.beforeCompletionHook = () -> {
            ctx().insertInto(table("nexus", "service_tokens"))
                .columns(DSL.field(DSL.name("token_hash"), String.class), TENANT_ID).values("sb-newco-token", "newco").execute();
            ctx().insertInto(table("dm_test", "forced_noread")).columns(TENANT_ID, ID, N).values("newco", 9_000L, 0).execute();
        };

        var result = finish(finisher, "sb-new-token", "forced_noread", List.of("default", "acme", "globex"));

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.VERIFICATION_NONEMPTY);
        assertThat(result.reason()).as("a tenant the first pass did not list, found in the completion transaction")
            .contains("completion transaction").contains("tenant newco=1");
        assertThat(completedAt("sb-new-token")).isNull();
    }

    @Test
    void aTenantWithNeitherATokenNorALeaf_isTheRemainingLimit_theRecheckDoesNotFindIt() {
        // the limit recheck's Javadoc states: only the probe's scan finds a tenant by its rows, and the probe ran at the start
        registerForcedNoRead("sb-ghost");
        var finisher = new VerifyingFinisher();
        finisher.beforeCompletionHook = () ->
            ctx().insertInto(table("dm_test", "forced_noread")).columns(TENANT_ID, ID, N).values("ghost", 9_000L, 0).execute();

        var result = finish(finisher, "sb-ghost", "forced_noread", List.of("default", "acme", "globex"));

        assertThat(result.outcome()).as("documents the limit, not a wish").isEqualTo(MoveFinisher.Outcome.COMPLETED);
    }

    // ── (f) the tenant setting during on_complete ───────────────────────────────────────────────────────

    @Test
    void duringOnComplete_nexusTenantIsEmpty_afterTheRechecksPerTenantRuns() {
        DataMoveTestFixtures.register(ctx(), "sb-tenant-oc", "forced_noread", 50, 0, null, "stamp-template.sql",
            "verification-forced-noread.sql", 60_000, "on-complete-record-tenant.sql");
        ctx().update(table("dm_test", "forced_noread")).set(N, 1).execute();

        var result = finish("sb-tenant-oc", "forced_noread", List.of("default", "acme", "globex"));

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(ctx().select(DSL.field(DSL.name("setting"), String.class)).from(table("dm_test", "on_complete_seen"))
            .fetch(DSL.field(DSL.name("setting"), String.class)))
            .as("on_complete ran once, with no tenant carried over from the last re-check run").containsExactly("");
    }

    // ── (c3) the re-check's runs share statement_timeout_ms ─────────────────────────────────────────────

    /**
     * The choice (see {@code VerifyingFinisher#recheck}): the table-wide run and every listed tenant's run share ONE
     * {@code statement_timeout_ms}, each getting what is left of it, rather than each its own. Four runs of 0.4 s (the table-wide
     * run and three tenants; {@code verification-slow-run.sql} sleeps that long on every run, the first pass's included, which has
     * 10 s) are each far under any single bound, so the sum is what these two tests tell apart.
     */
    private void registerSlowRuns(String label, int statementMs) {
        DataMoveTestFixtures.register(ctx(), label, "tenant_plain", 50, 0, null, "stamp-template.sql",
            "verification-slow-run.sql", statementMs, "on-complete-add-marker.sql");
        DataMoveTestFixtures.setVerifyStatementTimeout(ctx(), label, 10_000);
        runsOnly(label);
    }

    @Test
    void theRechecksOfNTenants_whoseSumFitsStatementTimeoutMs_completeTheMove() {
        registerSlowRuns("sb-sum-fits", 3_000);   // 4 x 0.4 s = 1.6 s inside 3 s

        var result = finish("sb-sum-fits", "tenant_plain", List.of("default", "acme", "globex"));

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(columnExists("marker")).as("on_complete ran after the re-check, with its own bounds restored").isTrue();
    }

    @Test
    void theRechecksOfNTenants_eachUnderStatementTimeoutMs_butTogetherOverIt_areATransientTimeout_andCompleteNothing() {
        registerSlowRuns("sb-sum-over", 1_000);   // 4 x 0.4 s = 1.6 s over 1 s; no single run is over it

        long started = System.nanoTime();
        var result = finish("sb-sum-over", "tenant_plain", List.of("default", "acme", "globex"));
        long elapsedMs = (System.nanoTime() - started) / 1_000_000L;

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage().name()).isEqualTo("RECHECK");
        assertThat(result.failure().raised().getSQLState()).as("the shared budget ran out: query_canceled").isEqualTo("57014");
        assertThat(result.failure().detail()).contains("completion transaction");
        assertThat(DataMoveFailureClassifier.classify(result.failure().stage(), result.failure().raised(), List.of(),
            result.failure().detail()).permanent()).as("retried under the existing backoff").isFalse();
        assertThat(completedAt("sb-sum-over")).isNull();
        assertThat(columnExists("marker")).as("on_complete did not run").isFalse();
        // the first pass takes 4 x 0.4 s; the completion transaction is cut at about 1 s, not run to the 1.6 s the runs need
        assertThat(elapsedMs).as("first pass ~1.6 s plus a re-check cut near 1 s").isLessThan(4_500L);
    }

    // ── (d) the first pass takes ACCESS SHARE only ──────────────────────────────────────────────────────

    @Test
    void theFirstPass_takesNoLockAboveAccessShare_andDoesNotBlockAWriter() throws Exception {
        registerCold("sb-locks", 1_000, 10_000, null);
        setCold(true);   // the pass sleeps 2 s: long enough to look at it
        var finisher = new VerifyingFinisher();
        finisher.beforeCompletionHook = () -> setCold(false);
        var finishing = async(() -> finish(finisher, "sb-locks", "tenant_plain", List.of()));

        Set<String> modes = new TreeSet<>();
        Set<String> relations = new TreeSet<>();
        long deadline = System.nanoTime() + java.time.Duration.ofSeconds(10).toNanos();
        var l = table("pg_catalog", "pg_locks").as("l");
        var a = table("pg_catalog", "pg_stat_activity").as("a");
        var c = table("pg_catalog", "pg_class").as("c");
        Field<String> mode = DSL.field(DSL.name("l", "mode"), String.class);
        Field<String> relname = DSL.field(DSL.name("c", "relname"), String.class);
        while (modes.isEmpty() && System.nanoTime() < deadline && !finishing.isDone()) {
            ctx().select(mode, relname).from(l)
                .join(a).on(DSL.field(DSL.name("a", "pid"), Integer.class).eq(DSL.field(DSL.name("l", "pid"), Integer.class)))
                .join(c).on(DSL.field(DSL.name("c", "oid"), Long.class).eq(DSL.field(DSL.name("l", "relation"), Long.class)))
                .where(DSL.field(DSL.name("a", "application_name"), String.class).eq(WORKER_NAME + "-verify-all"))
                .and(DSL.field(DSL.name("l", "locktype"), String.class).eq("relation"))
                .and(DSL.field(DSL.name("l", "granted"), Boolean.class).isTrue())
                .and(DSL.field(DSL.name("c", "relname"), String.class).in("tenant_plain", "cold_marker"))
                .forEach(r -> {
                    modes.add(r.value1());
                    relations.add(r.value2());
                });
            if (modes.isEmpty()) {
                Thread.sleep(20);
            }
        }
        // a writer is not blocked by the pass: an UPDATE of a row's touched_by (n stays 1) while the pass sleeps
        long writerStarted = System.nanoTime();
        ctx().update(table("dm_test", "tenant_plain")).set(TOUCHED_BY, "writer").where(ID.eq(1L)).execute();
        long writerMs = (System.nanoTime() - writerStarted) / 1_000_000L;

        assertThat(finishing.get().outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(relations).as("the pass read the driving table and the marker").contains("tenant_plain", "cold_marker");
        assertThat(modes).as("every relation lock of the READ ONLY pass").containsExactly("AccessShareLock");
        assertThat(writerMs).as("a row writer ran while the pass held its locks").isLessThan(1_500L);
    }

    // ── (e) the default ─────────────────────────────────────────────────────────────────────────────────

    @Test
    void aMoveRegisteredWithoutTheColumn_takesSixtySeconds_andCompletesAsBefore() {
        register("sb-default", "tenant_rows", 50, 0);
        runsOnly("sb-default");

        DataMove move = DataMoveRegistry.reload(ctx(), "sb-default").orElseThrow();
        var result = finish("sb-default", "tenant_rows", List.of("default", "acme", "globex", "orphan", "sl/ash"));

        assertThat(move.verifyStatementTimeoutMs()).isEqualTo(DataMove.DEFAULT_VERIFY_STATEMENT_TIMEOUT_MS).isEqualTo(60_000);
        assertThat(move.statementTimeoutMs()).as("the completion's bound is the registered one").isEqualTo(60_000);
        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(completedAt("sb-default")).isNotNull();
    }
}
