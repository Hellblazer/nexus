// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.PlanRepository;
import dev.nexus.service.db.TelemetryRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.JSONB;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.NX_ANSWER_RUNS;
import static dev.nexus.service.jooq.nexus.Tables.NX_ANSWER_STEPS;
import static dev.nexus.service.jooq.nexus.Tables.PLANS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-203 P2 — {@code TelemetryRepository.recordNxAnswerRunComplete}'s
 * one-transaction composite over {@code nx_answer_runs}, {@code
 * nx_answer_steps} and {@code plans}.
 *
 * <p>Sits beside {@link TelemetryRepositoryTest} and {@link
 * PlanRepositoryTest} (residual 7 — {@code service/src/test/java/dev/nexus/service/},
 * not a {@code db/} subpackage), and needs BOTH repositories against the same
 * {@link TenantScope} / service role, since the composite's own scope spans
 * both stores.
 *
 * <p>Hermetic embedded Postgres, one service role granted DML on the whole
 * {@code nexus} schema ({@link PgContainerHelper#bootstrapServiceRole}), so
 * one role can read/write {@code plans}, {@code nx_answer_runs} and {@code
 * nx_answer_steps} in the same test.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class NxAnswerRunCompleteTransactionTest {

    private static final String TENANT   = "rdr203-nx-answer-complete-tenant";
    private static final String SVC_ROLE = "svc_nx_answer_complete_test";
    private static final String SVC_PASS = "svc_nx_answer_complete_test_pass";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    TelemetryRepository telemetryRepo;
    PlanRepository planRepo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        svcDs         = buildSvcDataSource();
        tenantScope   = new TenantScope(svcDs);
        telemetryRepo = new TelemetryRepository(tenantScope);
        planRepo      = new PlanRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    // ── D2: the one-transaction property ─────────────────────────────────────

    @Test
    @Order(1)
    void runRowStepsAndPlanCountersLandInOneTransaction() {
        long planId = newPlan("proj-complete-1", "complete question one " + System.nanoTime());
        String question = "complete-question-" + System.nanoTime();
        OffsetDateTime createdAt = OffsetDateTime.now(ZoneOffset.UTC);
        List<TelemetryRepository.StepInput> steps = List.of(
            new TelemetryRepository.StepInput(0, "operator_filter", "sql", null,
                0, 0, null, null, 0.0, 12, true, List.of()));

        telemetryRepo.recordNxAnswerRunComplete(TENANT, question, planId, 0.71, 1,
            "final text", 0.0123, 81422, createdAt, steps, true);

        long runId = fetchNxAnswerRunId(question);
        assertThat(fetchNxAnswerStepCount(runId)).as("step child row must persist").isEqualTo(1);

        var row = planRepo.getById(TENANT, planId).orElseThrow();
        assertThat(row.getUseCount()).as("use_count must bump by 1").isEqualTo(1);
        assertThat(row.getLastUsed()).as("last_used must move").isNotNull();
        assertThat(row.getSuccessCount()).as("success_count must bump by 1").isEqualTo(1);
        assertThat(row.getFailureCount()).as("failure_count must stay 0").isEqualTo(0);
    }

    /**
     * The falsifier for D2's one-transaction property: a step whose {@code
     * source} violates {@code nx_answer_steps_source_chk} must roll back the
     * run row AND both counter updates. Reverting the composite to three
     * separate {@code withTenant} calls (one per table) turns this red.
     */
    @Test
    @Order(2)
    void stepConstraintViolationRollsBackRunRowAndPlanCounters() {
        long planId = newPlan("proj-complete-2", "complete rollback plan " + System.nanoTime());
        String question = "complete-rollback-question-" + System.nanoTime();
        OffsetDateTime createdAt = OffsetDateTime.now(ZoneOffset.UTC);
        List<TelemetryRepository.StepInput> steps = List.of(
            new TelemetryRepository.StepInput(0, "op", "not_a_real_source", null,
                null, null, null, null, null, 1, true, List.of()));

        assertThatThrownBy(() ->
            telemetryRepo.recordNxAnswerRunComplete(TENANT, question, planId, null, 1,
                "should not persist", 0.0, 1, createdAt, steps, true)
        ).isInstanceOf(RuntimeException.class);

        assertThat(nxAnswerRunExists(question))
            .as("a failed child insert must roll back the run row too")
            .isFalse();

        var row = planRepo.getById(TENANT, planId).orElseThrow();
        assertThat(row.getUseCount()).as("use_count must roll back with the run row").isEqualTo(0);
        assertThat(row.getLastUsed()).as("last_used must roll back too").isNull();
        assertThat(row.getSuccessCount()).as("success_count must roll back too").isEqualTo(0);
        assertThat(row.getFailureCount()).as("failure_count must roll back too").isEqualTo(0);
    }

    // ── D3: dedup skip means the whole composite is skipped ──────────────────

    @Test
    @Order(3)
    void dedupSkipLeavesPlanCountersUntouched() {
        long planId = newPlan("proj-complete-3", "complete dedup plan " + System.nanoTime());
        String question = "complete-dedup-question-" + System.nanoTime();
        // A FIXED created_at (not now()) so the replay hits the exact same
        // ETL dedup key (tenant_id, question, created_at) — the live-write
        // path's own now()-at-microsecond-resolution collision odds are near
        // zero (D3), so the test pins the timestamp to make the collision
        // certain rather than probable.
        OffsetDateTime createdAt = OffsetDateTime.of(2026, 9, 5, 18, 22, 31, 0, ZoneOffset.UTC);

        telemetryRepo.recordNxAnswerRunComplete(TENANT, question, planId, null, 0,
            "first attempt", 0.0, 1, createdAt, List.of(), true);
        // Replay of the identical (tenant, question, created_at): the parent
        // insert's onConflictDoNothing() must conflict-skip, and D3 says the
        // plan counters must not bump a second time either.
        telemetryRepo.recordNxAnswerRunComplete(TENANT, question, planId, null, 0,
            "second attempt (retry)", 0.0, 1, createdAt, List.of(), true);

        assertThat(fetchNxAnswerRunCount(question))
            .as("exactly one run row despite two composite POSTs")
            .isEqualTo(1);
        var row = planRepo.getById(TENANT, planId).orElseThrow();
        assertThat(row.getUseCount()).as("exactly one use_count bump").isEqualTo(1);
        assertThat(row.getSuccessCount()).as("exactly one success_count bump").isEqualTo(1);
    }

    // ── D1: null vs zero plan_id both mean "no library row to count against" ──

    @Test
    @Order(4)
    void nullPlanIdWritesRunRowAndNoCounters() {
        String question = "complete-null-plan-question-" + System.nanoTime();
        OffsetDateTime createdAt = OffsetDateTime.now(ZoneOffset.UTC);

        telemetryRepo.recordNxAnswerRunComplete(TENANT, question, null, null, 0,
            "no plan", 0.0, 1, createdAt, List.of(), true);

        assertThat(nxAnswerRunExists(question))
            .as("run row must persist with a null plan id")
            .isTrue();
    }

    /**
     * {@code plan_id} arrives as a boxed {@code Long}, so {@code null} and
     * {@code 0L} are different values, and D1 gives them the same meaning: no
     * library row to count against. Falsifier: relax the composite's guard to
     * {@code planId != null} alone and this test starts bumping counters
     * against plan id 0 — a sentinel row is planted at exactly that id so a
     * relaxed guard has something real to (wrongly) touch, rather than
     * silently no-op'ing against an absent row either way.
     */
    @Test
    @Order(5)
    void zeroPlanIdWritesRunRowAndNoCounters() {
        plantPlanRowAtIdZero();
        long syntheticInlinePlannerPlanId = 0L;
        String question = "complete-zero-plan-question-" + System.nanoTime();
        OffsetDateTime createdAt = OffsetDateTime.now(ZoneOffset.UTC);

        telemetryRepo.recordNxAnswerRunComplete(TENANT, question, syntheticInlinePlannerPlanId,
            null, 0, "ad-hoc plan", 0.0, 1, createdAt, List.of(), true);

        assertThat(nxAnswerRunExists(question))
            .as("run row must persist with plan_id=0")
            .isTrue();

        var sentinelRow = planRepo.getById(TENANT, 0L).orElseThrow();
        assertThat(sentinelRow.getUseCount())
            .as("plan id 0 is a sentinel, not a real library row — its counters must stay untouched")
            .isEqualTo(0);
        assertThat(sentinelRow.getSuccessCount()).isEqualTo(0);
        assertThat(sentinelRow.getFailureCount()).isEqualTo(0);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private long newPlan(String project, String query) {
        return planRepo.savePlan(TENANT, project, query, "{}", "success", "", null,
            null, "test", null, null, null, null, "", "");
    }

    /**
     * Plants a real {@code plans} row at the exact synthetic id (0) the
     * inline planner's ad-hoc {@code Match} sentinel uses, so {@code
     * zeroPlanIdWritesRunRowAndNoCounters}'s falsifier has a real row to
     * (wrongly) touch under a relaxed {@code planId != null}-only guard.
     * {@code BIGSERIAL} lets an explicit id be inserted without disturbing
     * the sequence (which starts at 1), so this cannot collide with any
     * plan {@link #newPlan} creates elsewhere in this class.
     */
    private void plantPlanRowAtIdZero() {
        tenantScope.withTenant(TENANT, ctx -> ctx.insertInto(PLANS)
            .set(PLANS.ID, 0L)
            .set(PLANS.TENANT_ID, TENANT)
            .set(PLANS.QUERY, "zero-sentinel-query-" + System.nanoTime())
            .set(PLANS.PLAN_JSON, JSONB.valueOf("{}"))
            .set(PLANS.VERB, "test")
            .set(PLANS.CREATED_AT, OffsetDateTime.now(ZoneOffset.UTC))
            .onConflictDoNothing()
            .execute());
    }

    private long fetchNxAnswerRunId(String question) {
        Long id = tenantScope.withTenant(TENANT, ctx -> ctx.select(NX_ANSWER_RUNS.ID)
            .from(NX_ANSWER_RUNS)
            .where(NX_ANSWER_RUNS.TENANT_ID.eq(TENANT).and(NX_ANSWER_RUNS.QUESTION.eq(question)))
            .fetchOne(NX_ANSWER_RUNS.ID));
        assertThat(id).as("run row must exist for question=" + question).isNotNull();
        return id;
    }

    private boolean nxAnswerRunExists(String question) {
        return fetchNxAnswerRunCount(question) > 0;
    }

    private int fetchNxAnswerRunCount(String question) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.fetchCount(NX_ANSWER_RUNS,
            NX_ANSWER_RUNS.TENANT_ID.eq(TENANT).and(NX_ANSWER_RUNS.QUESTION.eq(question))));
    }

    private int fetchNxAnswerStepCount(long runId) {
        return tenantScope.withTenant(TENANT, ctx ->
            ctx.fetchCount(NX_ANSWER_STEPS, NX_ANSWER_STEPS.RUN_ID.eq(runId)));
    }

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.addDataSourceProperty("options", "-c search_path=nexus,public");
        return new com.zaxxer.hikari.HikariDataSource(config);
    }
}
