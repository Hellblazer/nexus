package dev.nexus.service;

import org.jooq.impl.DSL;
import org.jooq.SQLDialect;
import org.jooq.DSLContext;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.sql.ResultSet;
import java.time.OffsetDateTime;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.NX_ANSWER_RUNS;
import static dev.nexus.service.jooq.nexus.Tables.NX_ANSWER_STEPS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-gmiaf.12 — Liquibase telemetry baseline integration test.
 *
 * <p>Hermetic embedded Postgres. Applies the full Liquibase master changelog and asserts:
 * <ol>
 *   <li>All six telemetry tables exist with correct column sets.</li>
 *   <li>RLS: each table has relrowsecurity=t, relforcerowsecurity=t + tenant_isolation policy.</li>
 *   <li>No FTS tsvector columns (telemetry is time-range queried, not full-text).</li>
 *   <li>BTree indexes on timestamp columns present for time-range queries.</li>
 *   <li>ETL dedup indexes present (unique indexes for idempotent import).</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TelemetrySchemaLiquibaseTest {

    private static final Set<String> RELEVANCE_LOG_COLS = Set.of(
        "id", "tenant_id", "query", "chunk_id", "collection", "action", "session_id", "timestamp");
    private static final Set<String> SEARCH_TELEMETRY_COLS = Set.of(
        "tenant_id", "ts", "query_hash", "collection", "raw_count", "kept_count",
        "top_distance", "threshold");
    private static final Set<String> TIER_WRITES_COLS = Set.of(
        "id", "tenant_id", "session_id", "ts", "tool", "tier", "agent", "project", "target_title");
    private static final Set<String> NX_ANSWER_RUNS_COLS = Set.of(
        "id", "tenant_id", "question", "plan_id", "matched_confidence", "step_count",
        "final_text", "cost_usd", "duration_ms", "created_at");
    private static final Set<String> HOOK_FAILURES_COLS = Set.of(
        "id", "tenant_id", "doc_id", "collection", "hook_name", "error", "occurred_at",
        "batch_doc_ids", "is_batch", "chain");
    private static final Set<String> FRECENCY_COLS = Set.of(
        "tenant_id", "chunk_id", "embedded_at", "ttl_days", "frecency_score",
        "miss_count", "last_hit_at");
    // RDR-196 .p1c (nexus-nyry9.9): per-step cost/quality telemetry, child of nx_answer_runs.
    // telemetry-008 (nexus-ndoke): the two prompt-cache token columns.
    private static final Set<String> NX_ANSWER_STEPS_COLS = Set.of(
        "run_id", "tenant_id", "step_index", "operator", "source", "model",
        "input_tokens", "output_tokens", "cost_usd", "elapsed_ms", "ok", "bundled_steps",
        "cache_read_input_tokens", "cache_creation_input_tokens");

    // Tables that should NOT have a tsvector column (telemetry is never FTS-searched)
    private static final List<String> ALL_TEL_TABLES = List.of(
        "relevance_log", "search_telemetry", "tier_writes",
        "nx_answer_runs", "hook_failures", "frecency", "nx_answer_steps");

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // role-001 (the master changelog's first include) creates nexus_svc.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stopAll() throws Exception {
        if (pg != null) pg.stop();
    }

    // ── Test 1: exact column sets ────────────────────────────────────────────

    @Test
    void relevanceLog_hasExactColumnSet() throws Exception {
        assertColumns("relevance_log", RELEVANCE_LOG_COLS);
    }

    @Test
    void searchTelemetry_hasExactColumnSet() throws Exception {
        assertColumns("search_telemetry", SEARCH_TELEMETRY_COLS);
    }

    @Test
    void tierWrites_hasExactColumnSet() throws Exception {
        assertColumns("tier_writes", TIER_WRITES_COLS);
    }

    @Test
    void nxAnswerRuns_hasExactColumnSet() throws Exception {
        assertColumns("nx_answer_runs", NX_ANSWER_RUNS_COLS);
    }

    @Test
    void hookFailures_hasExactColumnSet() throws Exception {
        assertColumns("hook_failures", HOOK_FAILURES_COLS);
    }

    @Test
    void frecency_hasExactColumnSet() throws Exception {
        assertColumns("frecency", FRECENCY_COLS);
    }

    @Test
    void nxAnswerSteps_hasExactColumnSet() throws Exception {
        assertColumns("nx_answer_steps", NX_ANSWER_STEPS_COLS);
    }

    // ── nx_answer_steps: PK, FK, source CHECK (RDR-196 .p1c, nexus-nyry9.9) ──

    @Test
    void nxAnswerSteps_primaryKeyIsRunIdStepIndex() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.getMetaData().getPrimaryKeys(null, "nexus", "nx_answer_steps");
            Set<String> pkCols = new HashSet<>();
            while (rs.next()) {
                pkCols.add(rs.getString("COLUMN_NAME").toLowerCase());
            }
            assertThat(pkCols)
                .as("nx_answer_steps primary key must be exactly (run_id, step_index)")
                .isEqualTo(Set.of("run_id", "step_index"));
        }
    }

    @Test
    void nxAnswerSteps_runIdForeignKeyCascadesOnDelete() throws Exception {
        try (Connection su = pg.createConnection("")) {
            List<String> deleteActions = PgCatalogProbes.foreignKeyDeleteActions(
                DSL.using(su, SQLDialect.POSTGRES), "nexus", "nx_answer_steps");
            assertThat(deleteActions).as("nx_answer_steps must have a foreign key").isNotEmpty();
            // 'c' = ON DELETE CASCADE (pg_constraint.confdeltype)
            assertThat(deleteActions.get(0)).isEqualTo("c");
        }
    }

    @Test
    void nxAnswerSteps_sourceCheckRejectsUnknownValue() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.setTenant(su, TenantScope.DEFAULT_TENANT_GUC, "schema-check-tenant", false);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.insertInto(NX_ANSWER_RUNS, NX_ANSWER_RUNS.TENANT_ID, NX_ANSWER_RUNS.QUESTION, NX_ANSWER_RUNS.CREATED_AT)
               .values("schema-check-tenant", "check q", OffsetDateTime.now())
               .execute();
            Long runId = ctx.select(NX_ANSWER_RUNS.ID)
                .from(NX_ANSWER_RUNS)
                .where(NX_ANSWER_RUNS.TENANT_ID.eq("schema-check-tenant")
                    .and(NX_ANSWER_RUNS.QUESTION.eq("check q")))
                .orderBy(NX_ANSWER_RUNS.ID.desc())
                .limit(1)
                .fetchOne(NX_ANSWER_RUNS.ID);
            assertThat(runId).isNotNull();
            long finalRunId = runId;
            org.assertj.core.api.Assertions.assertThatThrownBy(() ->
                ctx.insertInto(NX_ANSWER_STEPS,
                        NX_ANSWER_STEPS.RUN_ID, NX_ANSWER_STEPS.TENANT_ID, NX_ANSWER_STEPS.STEP_INDEX,
                        NX_ANSWER_STEPS.OPERATOR, NX_ANSWER_STEPS.SOURCE, NX_ANSWER_STEPS.ELAPSED_MS, NX_ANSWER_STEPS.OK)
                    .values(finalRunId, "schema-check-tenant", 0, "op", "not_a_real_source", 0, true)
                    .execute()
            ).isInstanceOf(org.jooq.exception.DataAccessException.class)
             .hasMessageContaining("nx_answer_steps_source_chk");
        }
    }

    // ── nx_answer_runs.cost_usd nullable (RDR-196 .p1c-b, nexus-lme1s) ───────

    @Test
    void nxAnswerRuns_costUsdIsNullableNoDefault() throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgCatalogProbes.ColumnInfo col = PgCatalogProbes.columnInfo(
                DSL.using(su, SQLDialect.POSTGRES), "nexus", "nx_answer_runs", "cost_usd");
            assertThat(col).as("nx_answer_runs.cost_usd column must exist").isNotNull();
            assertThat(col.isNullable())
                .as("telemetry-007-3 must DROP NOT NULL on nx_answer_runs.cost_usd "
                    + "(RDR-196 risk 1: a client null must not be forced to 0.0)")
                .isEqualTo("YES");
            assertThat(col.columnDefault())
                .as("telemetry-007-3 must DROP DEFAULT on nx_answer_runs.cost_usd")
                .isNull();
        }
    }

    // ── Test 2: RLS on every telemetry table ─────────────────────────────────

    @Test
    void allTelemetryTables_rlsEnabledForcedWithPolicy() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String table : ALL_TEL_TABLES) {
                PgCatalogProbes.RowSecurity cls = PgCatalogProbes.rowSecurity(ctx, "nexus", table);
                assertThat(cls).as(table + " must exist in pg_class").isNotNull();
                assertThat(cls.enabled())
                    .as(table + ": relrowsecurity must be true").isTrue();
                assertThat(cls.forced())
                    .as(table + ": relforcerowsecurity must be true").isTrue();

                assertThat(PgCatalogProbes.policies(ctx, "nexus", table))
                    .as(table + " must have at least one RLS policy").isNotEmpty();
            }
        }
    }

    // ── Test 3: NO tsvector columns (confirmed: telemetry is not FTS-searched) ─

    @Test
    void allTelemetryTables_noTsvectorColumns() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String table : ALL_TEL_TABLES) {
                assertThat(PgCatalogProbes.columnCountOfType(ctx, "nexus", table, "tsvector"))
                    .as(table + " must NOT have any tsvector columns (telemetry is not FTS-searched)")
                    .isZero();
            }
        }
    }

    // ── Test 4: BTree indexes on timestamp columns ────────────────────────────

    @Test
    void telemetryTables_btreeTimestampIndexesExist() throws Exception {
        // Map table → expected timestamp column name used for time-range queries
        var tableToTsCol = List.of(
            new String[]{ "relevance_log",    "timestamp" },
            new String[]{ "search_telemetry", "ts" },
            new String[]{ "tier_writes",      "ts" },
            new String[]{ "nx_answer_runs",   "created_at" },
            new String[]{ "hook_failures",    "occurred_at" },
            new String[]{ "frecency",         "last_hit_at" }
        );

        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (var entry : tableToTsCol) {
                String table = entry[0];
                String tsCol  = entry[1];

                assertThat(PgCatalogProbes.indexCountOnColumn(ctx, "nexus", table, "btree", tsCol))
                    .as("BTree index on " + table + "." + tsCol + " must exist for time-range queries")
                    .isPositive();
            }
        }
    }

    // ── Test 5: ETL dedup unique indexes exist ────────────────────────────────

    @Test
    void telemetryEventLogTables_etlDedupIndexesExist() throws Exception {
        // Event log tables must have a unique index for idempotent import (DO NOTHING on conflict)
        var dedupIndexNames = List.of(
            new String[]{ "relevance_log",  "idx_relevance_log_etl_dedup" },
            new String[]{ "tier_writes",    "idx_tier_writes_etl_dedup" },
            new String[]{ "nx_answer_runs", "idx_nx_answer_runs_etl_dedup" },
            new String[]{ "hook_failures",  "idx_hook_failures_etl_dedup" }
        );

        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (var entry : dedupIndexNames) {
                String table     = entry[0];
                String indexName = entry[1];

                assertThat(PgCatalogProbes.indexExists(ctx, "nexus", indexName))
                    .as("ETL dedup index " + indexName + " must exist on " + table)
                    .isTrue();
            }
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private void assertColumns(String table, Set<String> expected) throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.getMetaData().getColumns(null, "nexus", table, null);
            Set<String> actual = new HashSet<>();
            while (rs.next()) {
                actual.add(rs.getString("COLUMN_NAME").toLowerCase());
            }
            assertThat(actual)
                .as("nexus." + table + " must have exact column set")
                .isEqualTo(expected);
        }
    }
}
