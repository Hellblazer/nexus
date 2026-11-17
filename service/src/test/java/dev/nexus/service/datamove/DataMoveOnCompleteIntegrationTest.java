// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.DriverManager;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * nexus-f3yqd.2.14 (RDR-246 Step 4, The move worker step 3, Finishing a move) on the pg_background image: the
 * {@code on_complete} half of the finish. {@code on_complete} and the {@code completed_at} UPDATE are one transaction, a
 * pause set after the last read stops both, {@code on_complete} runs only after a verification that found nothing, and on
 * a move over the chunk family (nexus-f3yqd.2.1) the index builders are ended first and a lock it cannot get fails at
 * {@code lock_timeout}. The verification half is {@link DataMoveVerificationIntegrationTest}.
 */
class DataMoveOnCompleteIntegrationTest extends DataMoveIntegrationBase {

    private static final String BUILDER_NAME = "nexus-pci-builder-oc-test";
    private static final String CONSTRAINT = "tenant_plain_new_shape";
    private static final Field<Long> ID = DSL.field(DSL.name("id"), Long.class);

    /** A move over {@code dm_test.tenant_plain} (one run, no tenant list) that VALIDATEs the new-shape CHECK on completion. */
    private void registerValidating(String label) {
        register(label, "tenant_plain", 50, 0, "verification-tenant-plain.sql", 60_000, "on-complete-validate.sql");
    }

    private void register(String label, String table, int chunk, int sleep, String verification, int statementMs,
                          String onComplete) {
        DataMoveTestFixtures.register(ctx(), label, table, chunk, sleep, null, "stamp-template.sql", verification,
            statementMs, onComplete);
    }

    private boolean validated() {
        try (var session = adminSession("tenant_plain")) {
            return Boolean.TRUE.equals(session.states(List.of(CONSTRAINT)).get(0).validated());
        }
    }

    private boolean columnExists(String column) {
        return ctx().fetchExists(table("information_schema", "columns"),
            DSL.field(DSL.name("table_schema"), String.class).eq("dm_test")
                .and(DSL.field(DSL.name("table_name"), String.class).eq("tenant_plain"))
                .and(DSL.field(DSL.name("column_name"), String.class).eq(column)));
    }

    /** A connection of the admin role, named as the index builder's backend is, holding ROW SHARE on nexus.chunks. */
    private Connection builderHoldingChunks() throws Exception {
        return holdingChunks(adminLogin(BUILDER_NAME));
    }

    private Connection adminLogin(String applicationName) throws Exception {
        var props = new Properties();
        props.setProperty("user", ADMIN);
        props.setProperty("password", ADMIN_PASSWORD);
        props.setProperty("ApplicationName", applicationName);
        return DriverManager.getConnection(pg.getJdbcUrl(), props);
    }

    /** ROW SHARE on the parent, held until the connection ends its transaction: what an ALTER TABLE queues behind. */
    private static Connection holdingChunks(Connection c) throws Exception {
        c.setAutoCommit(false);
        DSL.using(c, SQLDialect.POSTGRES).select(DSL.one()).from(table("nexus", "chunks")).forShare().fetch();
        return c;
    }

    private void insertOldShapeRow(long id) {
        ctx().insertInto(table("dm_test", "tenant_plain")).columns(TENANT_ID, ID).values("acme", id).execute();
    }

    // ── one transaction ──────────────────────────────────────────────────────────────────────────────────

    @Test
    void anOnCompleteThatFails_leavesNeitherItselfNorTheCompletion() {
        register("oc-fail", "tenant_plain", 50, 0, "verification-tenant-plain.sql", 60_000, "on-complete-partial-failure.sql");
        runsOnly("oc-fail");

        var result = finish("oc-fail", "tenant_plain", List.of());

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.ON_COMPLETE);
        assertThat((Throwable) result.failure().raised()).isNotNull();
        assertThat(result.failure().raised().getSQLState()).as("undefined_object: the second statement").isEqualTo("42704");
        assertThat(columnExists("marker")).as("the first statement of on_complete was rolled back").isFalse();
        assertThat(completedAt("oc-fail")).as("completed_at stays NULL").isNull();
        assertThat(DataMoveRegistry.reload(ctx(), "oc-fail")).as("the move is still open").isPresent();
    }

    @Test
    void aMoveWithNoOnComplete_completesWithTheUpdateAlone() {
        register("oc-none", "tenant_plain", 50, 0, "verification-tenant-plain.sql", 60_000, null);
        runsOnly("oc-none");

        assertThat(finish("oc-none", "tenant_plain", List.of()).outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);

        assertThat(completedAt("oc-none")).isNotNull();
    }

    // ── on_complete only after a verification that found nothing; the later changeset ─────────────────────

    @Test
    void validateRunsOnlyAfterAnEmptyVerification_andTheLaterChangesetWaitsForCompletedAt() throws Exception {
        registerValidating("oc-validate");
        runsOnly("oc-validate");
        insertOldShapeRow(9_000);
        DataMoveTestFixtures.addNewShapeCheck(su);
        DataMoveTestFixtures.walkGatedDrop(su);
        assertThat(columnExists("touched_by")).as("the gated changeset is skipped while the move is open").isTrue();

        var blocked = finish("oc-validate", "tenant_plain", List.of());

        assertThat(blocked.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(blocked.failure().stage()).isEqualTo(FailureHandler.Stage.VERIFICATION_NONEMPTY);
        assertThat(validated()).as("VALIDATE did not run: the verification found an old-shape row").isFalse();
        assertThat(completedAt("oc-validate")).isNull();
        DataMoveTestFixtures.walkGatedDrop(su);
        assertThat(columnExists("touched_by")).as("skipped again on the next walk").isTrue();

        ctx().update(table("dm_test", "tenant_plain")).set(N, 1).where(ID.eq(9_000L)).execute();   // the row is put right
        var completed = finish("oc-validate", "tenant_plain", List.of());

        assertThat(completed.outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(validated()).as("on_complete ran, after the verification").isTrue();
        assertThat(completedAt("oc-validate")).isNotNull();
        DataMoveTestFixtures.walkGatedDrop(su);
        assertThat(columnExists("touched_by")).as("the gated changeset applies once completed_at is set").isFalse();
    }

    @Test
    void aWorkerPass_runsOnCompleteAndSetsCompletedAt_andReportsNothingForTheMove() throws Exception {
        registerValidating("oc-worker");
        DataMoveTestFixtures.addNewShapeCheck(su);
        var seen = new CopyOnWriteArrayList<FailureHandler.Failure>();
        var w = worker(installed(), null, f -> {
            seen.add(f);
            return FailureHandler.Disposition.blocked(f.detail());
        }, new VerifyingFinisher());

        assertThat(w.runCycle()).isEqualTo(1);

        assertThat(seen).isEmpty();
        assertThat(completedAt("oc-worker")).isNotNull();
        assertThat(validated()).as("on_complete ran with the completion").isTrue();
        assertThat(w.states().state("oc-worker")).isEmpty();
    }

    // ── a pause between the last read and the commit ────────────────────────────────────────────────────

    @Test
    void aPauseSetAfterTheLastRead_stopsOnCompleteAndTheCompletionTogether() throws Exception {
        registerValidating("oc-pause");
        runsOnly("oc-pause");
        DataMoveTestFixtures.addNewShapeCheck(su);
        var finisher = new VerifyingFinisher();
        finisher.beforeCompletionHook = () -> DataMoveTestFixtures.pause(ctx(), "oc-pause", true);

        var paused = finish(finisher, "oc-pause", "tenant_plain", List.of());

        assertThat(paused.outcome()).isEqualTo(MoveFinisher.Outcome.PAUSED);
        assertThat(paused.move().paused()).isTrue();
        assertThat(validated()).as("on_complete did not run").isFalse();
        assertThat(completedAt("oc-pause")).isNull();

        DataMoveTestFixtures.pause(ctx(), "oc-pause", false);
        assertThat(finish("oc-pause", "tenant_plain", List.of()).outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(validated()).isTrue();
    }

    // ── the chunk family (nexus-f3yqd.2.1) ───────────────────────────────────────────────────────────────

    @Test
    void anOnCompleteOnTheChunkFamily_endsTheIndexBuilders_thenCompletes() throws Exception {
        register("oc-chunks", "tenant_plain", 50, 0, "verification-tenant-plain.sql", 60_000, "on-complete-chunks.sql");
        runsOnly("oc-chunks");
        try (Connection builder = builderHoldingChunks()) {
            assertThat(sessionsNamed(BUILDER_NAME)).as("the builder is running, holding a lock on nexus.chunks").isEqualTo(1);

            var result = finish("oc-chunks", "tenant_plain", List.of());

            assertThat(result.outcome()).as("the ALTER got its lock: the builder was ended first")
                .isEqualTo(MoveFinisher.Outcome.COMPLETED);
            assertThat(sessionsNamed(BUILDER_NAME)).isZero();
            assertThat(builder.isValid(2)).as("its session is gone").isFalse();
        }
        assertThat(completedAt("oc-chunks")).isNotNull();
    }

    @Test
    void anOnCompleteOutsideTheChunkFamily_leavesTheIndexBuildersAlone() throws Exception {
        registerValidating("oc-other");
        runsOnly("oc-other");
        DataMoveTestFixtures.addNewShapeCheck(su);
        try (Connection builder = builderHoldingChunks()) {
            var result = finish("oc-other", "tenant_plain", List.of());

            assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
            assertThat(sessionsNamed(BUILDER_NAME)).as("a build that took tens of minutes is not thrown away").isEqualTo(1);
            assertThat(builder.isValid(2)).isTrue();
        }
    }

    @Test
    void anOnCompleteThatCannotGetItsLock_failsAtLockTimeout_belowStatementTimeoutMs_andIsRetriedOnceTheLockIsFree()
            throws Exception {
        register("oc-lock", "tenant_plain", 50, 0, "verification-tenant-plain.sql", 4_000, "on-complete-chunks.sql");
        runsOnly("oc-lock");
        Connection holder = holdingChunks(adminLogin("oc-reader-holding-a-lock"));   // not a builder: it is not ended
        try {
            long started = System.nanoTime();
            var result = finish("oc-lock", "tenant_plain", List.of());
            long elapsedMs = (System.nanoTime() - started) / 1_000_000L;

            assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
            assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.ON_COMPLETE);
            assertThat(result.failure().raised().getSQLState()).as("lock_not_available, not the statement timeout")
                .isEqualTo("55P03");
            assertThat(elapsedMs).as("lock_timeout is half of statement_timeout_ms: 2 s").isBetween(1_500L, 3_900L);
            assertThat(completedAt("oc-lock")).isNull();
            assertThat(holder.isValid(2)).as("a reader is not ended").isTrue();
        } finally {
            holder.rollback();
            holder.close();
        }

        assertThat(finish("oc-lock", "tenant_plain", List.of()).outcome()).isEqualTo(MoveFinisher.Outcome.COMPLETED);
        assertThat(completedAt("oc-lock")).isNotNull();
    }

    @Test
    void anOnCompleteThatOutlastsItsStatementTimeout_isReportedAsTheTimeout() {
        register("oc-slow", "tenant_plain", 50, 0, "verification-tenant-plain.sql", 1_000, "on-complete-slow.sql");
        runsOnly("oc-slow");

        var result = finish("oc-slow", "tenant_plain", List.of());

        assertThat(result.outcome()).isEqualTo(MoveFinisher.Outcome.REPORT);
        assertThat(result.failure().stage()).isEqualTo(FailureHandler.Stage.ON_COMPLETE);
        assertThat(result.failure().raised().getSQLState()).as("query_canceled").isEqualTo("57014");
        assertThat(completedAt("oc-slow")).isNull();
    }
}
