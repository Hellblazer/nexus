// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.UUID;
import java.util.function.Consumer;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOBS;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOB_MOVES;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): the shape of {@code nexus.reembed_jobs} and
 * {@code nexus.reembed_job_moves} ({@code vectors-037-reembed-jobs.xml}) on real Postgres, the CHECK
 * constraints and the source-lock index enforced, and tenant isolation through a non-superuser service
 * role. Same layout as {@link RequestLedgerSchemaLiquibaseTest}.
 *
 * <p>Tests 1-4 read the shared, already-migrated cluster ({@link PgContainerHelper#start()}); Test 5 gets
 * its own database and a non-superuser role, because a superuser connection bypasses RLS and proves
 * nothing. Grants are pinned in {@link GrantsNexusDiagViewAccessIntegrationTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReembedJobsSchemaLiquibaseTest {

    private static final String SRC = "docs__owner__bge-base-en-v15-768__v1";
    private static final String TGT = "docs__owner__minilm-l6-v2-384__v1";

    /** column -> information_schema udt_name + is_nullable. */
    private static final Map<String, String> JOB_COLUMNS = new TreeMap<>(Map.ofEntries(
        Map.entry("tenant_id", "text NO"),
        Map.entry("job_id", "uuid NO"),
        Map.entry("source_collection", "text NO"),
        Map.entry("target_collection", "text NO"),
        Map.entry("state", "text NO"),
        Map.entry("disposition", "text YES"),
        Map.entry("doc_cursor", "text NO"),
        Map.entry("pass_number", "int2 NO"),
        Map.entry("cutover_step", "text YES"),
        Map.entry("documents_moved", "int8 NO"),
        Map.entry("documents_skipped", "int8 NO"),
        Map.entry("chunks_embedded", "int8 NO"),
        Map.entry("chunks_reused", "int8 NO"),
        Map.entry("tokens_billed", "int8 NO"),
        Map.entry("token_cap", "int8 YES"),
        Map.entry("keep_source", "bool NO"),
        Map.entry("force_delete", "bool NO"),
        Map.entry("started_at", "timestamptz NO"),
        Map.entry("updated_at", "timestamptz NO"),
        Map.entry("finished_at", "timestamptz YES"),
        Map.entry("failure_reason", "text YES"),
        Map.entry("last_error", "text YES")));

    private static final Map<String, String> MOVE_COLUMNS = new TreeMap<>(Map.ofEntries(
        Map.entry("tenant_id", "text NO"),
        Map.entry("job_id", "uuid NO"),
        Map.entry("doc_id", "text NO"),
        Map.entry("outcome", "text NO"),
        Map.entry("reason", "text YES"),
        Map.entry("pass_number", "int2 NO"),
        Map.entry("at", "timestamptz NO")));

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() {
        pg = PgContainerHelper.start();
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    // ── Test 1: columns, types, nullability, PK, FK, CHECKs, the lock index ──

    @Test
    void reembedTables_haveExactColumnsKeysChecksAndLockIndex() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            assertThat(columns(ctx, "reembed_jobs")).as("nexus.reembed_jobs exact column set and types")
                .isEqualTo(JOB_COLUMNS);
            assertThat(columns(ctx, "reembed_job_moves")).as("nexus.reembed_job_moves exact column set and types")
                .isEqualTo(MOVE_COLUMNS);

            Map<String, String> jobs = constraints(ctx, "reembed_jobs");
            assertThat(jobs).containsOnlyKeys("reembed_jobs_pk", "reembed_jobs_state_check",
                "reembed_jobs_disposition_check", "reembed_jobs_cutover_step_check",
                "reembed_jobs_failure_reason_check");
            assertThat(jobs.get("reembed_jobs_pk")).isEqualTo("PRIMARY KEY (tenant_id, job_id)");
            assertThat(jobs.get("reembed_jobs_state_check")).contains("'planned'", "'running'", "'paused'",
                "'failed'", "'aborted'", "'cutover'", "'done'", "'done_with_skips'");
            assertThat(jobs.get("reembed_jobs_disposition_check")).contains("'deleted'", "'kept_by_flag'",
                "'kept_tombstoned'", "'kept_residue'", "'revived'");
            assertThat(jobs.get("reembed_jobs_cutover_step_check")).contains("'superseded'", "'delete_started'");
            assertThat(jobs.get("reembed_jobs_failure_reason_check")).contains("'batch_failed'",
                "'profile_changed'", "'token_cap'", "'parity_mismatch'");

            Map<String, String> moves = constraints(ctx, "reembed_job_moves");
            assertThat(moves).containsOnlyKeys("reembed_job_moves_pk", "reembed_job_moves_job_fk",
                "reembed_job_moves_outcome_check");
            assertThat(moves.get("reembed_job_moves_pk")).isEqualTo("PRIMARY KEY (tenant_id, job_id, doc_id)");
            assertThat(moves.get("reembed_job_moves_job_fk"))
                .contains("FOREIGN KEY (tenant_id, job_id)").contains("reembed_jobs(tenant_id, job_id)")
                .endsWith("ON DELETE CASCADE");
            assertThat(moves.get("reembed_job_moves_outcome_check")).contains("'moved'", "'skipped'");

            String lock = PgCatalogProbes.indexDef(ctx, "nexus", "reembed_jobs_source_lock");
            assertThat(lock).as("the source lock").contains("UNIQUE INDEX reembed_jobs_source_lock")
                .contains("(tenant_id, source_collection)")
                .contains("'planned'", "'running'", "'paused'", "'cutover'", "'failed'")
                .doesNotContain("'done'", "'aborted'");
        }
    }

    // ── Test 2: RLS ENABLE + FORCE with the tenant_isolation policy, both tables ──

    @Test
    void reembedTables_rlsEnabledForcedWithTenantIsolation() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String table : List.of("reembed_jobs", "reembed_job_moves")) {
                PgCatalogProbes.RowSecurity rls = PgCatalogProbes.rowSecurity(ctx, "nexus", table);
                assertThat(rls).as("nexus." + table + " must exist in pg_class").isNotNull();
                assertThat(rls.enabled()).as(table + " RLS ENABLE").isTrue();
                assertThat(rls.forced()).as(table + " RLS FORCE").isTrue();
                List<PgCatalogProbes.Policy> policies = PgCatalogProbes.policies(ctx, "nexus", table);
                assertThat(policies).as(table + " policies").hasSize(1);
                assertThat(policies.get(0).policyname()).isEqualTo("tenant_isolation");
            }
        }
    }

    // ── Test 3: the CHECK constraints reject what they name ──────────────────

    @Test
    void reembedJobs_checksRejectUnknownValues() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            insertJob(ctx, "check-tenant", UUID.randomUUID(), "docs__c__a__v1", "planned");

            assertThatThrownBy(() -> insertJob(ctx, "check-tenant", UUID.randomUUID(), "docs__c__b__v1", "done!"))
                .as("a state outside the eight is refused").hasMessageContaining("reembed_jobs_state_check");
            assertThatThrownBy(() -> insertJob(ctx, "check-tenant", UUID.randomUUID(), "docs__c__c__v1", "done",
                    j -> j.set(REEMBED_JOBS.DISPOSITION, "gone")))
                .as("an unknown disposition is refused").hasMessageContaining("reembed_jobs_disposition_check");
            assertThatThrownBy(() -> insertJob(ctx, "check-tenant", UUID.randomUUID(), "docs__c__d__v1", "cutover",
                    j -> j.set(REEMBED_JOBS.CUTOVER_STEP, "half")))
                .as("an unknown cutover step is refused").hasMessageContaining("reembed_jobs_cutover_step_check");
            assertThatThrownBy(() -> insertJob(ctx, "check-tenant", UUID.randomUUID(), "docs__c__e__v1", "failed",
                    j -> j.set(REEMBED_JOBS.FAILURE_REASON, "boom")))
                .as("an unknown failure reason is refused").hasMessageContaining("reembed_jobs_failure_reason_check");

            UUID job = UUID.randomUUID();
            insertJob(ctx, "check-tenant", job, "docs__c__f__v1", "planned");
            assertThatThrownBy(() -> insertMove(ctx, "check-tenant", job, "1.1.1", "lost"))
                .as("an unknown outcome is refused").hasMessageContaining("reembed_job_moves_outcome_check");
        }
    }

    // ── Test 4: the source lock holds for the five lock states, releases for the rest ──

    @Test
    void sourceLock_refusesASecondLockStateJob_andIgnoresTerminalOnes() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String tenant = "lock-tenant";
            for (String locking : List.of("planned", "running", "paused", "cutover", "failed")) {
                String source = SRC + "-" + locking;
                insertJob(ctx, tenant, UUID.randomUUID(), source, locking);
                assertThatThrownBy(() -> insertJob(ctx, tenant, UUID.randomUUID(), source, "planned"))
                    .as("a job in " + locking + " holds its source")
                    .hasMessageContaining("reembed_jobs_source_lock");
            }
            // terminal jobs hold nothing: any number of them, and a live job beside them
            String source = SRC + "-terminal";
            insertJob(ctx, tenant, UUID.randomUUID(), source, "done");
            insertJob(ctx, tenant, UUID.randomUUID(), source, "done_with_skips");
            insertJob(ctx, tenant, UUID.randomUUID(), source, "aborted");
            insertJob(ctx, tenant, UUID.randomUUID(), source, "planned");
            // another tenant's job on the same source name is not a conflict
            insertJob(ctx, "lock-tenant-b", UUID.randomUUID(), SRC + "-planned", "planned");
        }
    }

    // ── Test 5: tenant isolation through a non-superuser service role ────────

    @Test
    void tenantIsolation_secondTenantSeesNoneOfFirstTenantsRows_andCannotWriteThem() throws Exception {
        PostgreSQLContainer<?> dedicated = PgContainerHelper.start();
        com.zaxxer.hikari.HikariDataSource svcDs = null;
        try {
            try (Connection su = dedicated.createConnection("")) {
                PgContainerHelper.applyProductSchema(su);
            }
            String svcRole = "svc_reembed_jobs_test";
            String svcPass = "svc_reembed_jobs_test_pass";
            try (Connection su = dedicated.createConnection("")) {
                PgContainerHelper.bootstrapServiceRole(su, svcRole, svcPass);
            }

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(dedicated.getJdbcUrl());
            cfg.setUsername(svcRole);
            cfg.setPassword(svcPass);
            cfg.setMaximumPoolSize(5);
            cfg.setAutoCommit(true);
            svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
            TenantScope tenantScope = new TenantScope(svcDs);

            UUID jobA = UUID.randomUUID();
            UUID jobB = UUID.randomUUID();
            tenantScope.withTenant("reembed-tenant-a", ctx -> insertJob(ctx, "reembed-tenant-a", jobA, SRC, "planned"));
            tenantScope.withTenant("reembed-tenant-b", ctx -> insertJob(ctx, "reembed-tenant-b", jobB, SRC, "planned"));
            tenantScope.withTenant("reembed-tenant-a", ctx -> insertMove(ctx, "reembed-tenant-a", jobA, "1.1.1", "moved"));

            List<UUID> seenByA = tenantScope.withTenant("reembed-tenant-a", ctx ->
                ctx.select(REEMBED_JOBS.JOB_ID).from(REEMBED_JOBS).fetch(REEMBED_JOBS.JOB_ID));
            assertThat(seenByA).as("tenant-a sees exactly its own job").containsExactly(jobA);
            List<UUID> seenByB = tenantScope.withTenant("reembed-tenant-b", ctx ->
                ctx.select(REEMBED_JOBS.JOB_ID).from(REEMBED_JOBS).fetch(REEMBED_JOBS.JOB_ID));
            assertThat(seenByB).as("tenant-b sees exactly its own job, none of tenant-a's").containsExactly(jobB);
            int movesSeenByB = tenantScope.withTenant("reembed-tenant-b", ctx ->
                ctx.fetchCount(REEMBED_JOB_MOVES));
            assertThat(movesSeenByB).as("tenant-b sees none of tenant-a's moves").isZero();

            assertThatThrownBy(() -> tenantScope.withTenant("reembed-tenant-b", ctx ->
                    insertJob(ctx, "reembed-tenant-a", UUID.randomUUID(), TGT, "planned")))
                .as("WITH CHECK: tenant-b cannot write a job stamped for tenant-a")
                .hasMessageContaining("row-level security");
            assertThatThrownBy(() -> tenantScope.withTenant("reembed-tenant-b", ctx ->
                    insertMove(ctx, "reembed-tenant-a", jobA, "1.1.2", "moved")))
                .as("WITH CHECK: tenant-b cannot write a move stamped for tenant-a")
                .hasMessageContaining("row-level security");
        } finally {
            if (svcDs != null) svcDs.close();
            dedicated.stop();
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static Map<String, String> columns(DSLContext ctx, String table) {
        Map<String, String> actual = new TreeMap<>();
        for (String col : PgCatalogProbes.columnNames(ctx, "nexus", table)) {
            PgCatalogProbes.ColumnInfo info = PgCatalogProbes.columnInfo(ctx, "nexus", table, col);
            actual.put(col, info.udtName() + " " + info.isNullable());
        }
        return actual;
    }

    private static Map<String, String> constraints(DSLContext ctx, String table) {
        return PgCatalogProbes.constraintDefinitionsIn(ctx, List.of("nexus")).stream()
            .filter(c -> c.table().equals(table))
            .collect(Collectors.toMap(
                PgCatalogProbes.ConstraintDefinition::conname,
                PgCatalogProbes.ConstraintDefinition::definition));
    }

    private static int insertJob(DSLContext ctx, String tenant, UUID job, String source, String state) {
        return insertJob(ctx, tenant, job, source, state, j -> { });
    }

    private static int insertJob(DSLContext ctx, String tenant, UUID job, String source, String state,
                                 Consumer<org.jooq.InsertSetMoreStep<dev.nexus.service.jooq.nexus.tables.records.ReembedJobsRecord>> more) {
        OffsetDateTime now = OffsetDateTime.now();
        var step = ctx.insertInto(REEMBED_JOBS)
            .set(REEMBED_JOBS.TENANT_ID, tenant)
            .set(REEMBED_JOBS.JOB_ID, job)
            .set(REEMBED_JOBS.SOURCE_COLLECTION, source)
            .set(REEMBED_JOBS.TARGET_COLLECTION, TGT)
            .set(REEMBED_JOBS.STATE, state)
            .set(REEMBED_JOBS.STARTED_AT, now)
            .set(REEMBED_JOBS.UPDATED_AT, now);
        more.accept(step);
        return step.execute();
    }

    private static int insertMove(DSLContext ctx, String tenant, UUID job, String doc, String outcome) {
        return ctx.insertInto(REEMBED_JOB_MOVES)
            .set(REEMBED_JOB_MOVES.TENANT_ID, tenant)
            .set(REEMBED_JOB_MOVES.JOB_ID, job)
            .set(REEMBED_JOB_MOVES.DOC_ID, doc)
            .set(REEMBED_JOB_MOVES.OUTCOME, outcome)
            .set(REEMBED_JOB_MOVES.PASS_NUMBER, (short) 1)
            .set(REEMBED_JOB_MOVES.AT, OffsetDateTime.now())
            .execute();
    }
}
