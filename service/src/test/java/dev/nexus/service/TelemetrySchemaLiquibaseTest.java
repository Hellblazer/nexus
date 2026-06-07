package dev.nexus.service;

import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
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
import java.util.HashSet;
import java.util.List;
import java.util.Set;

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

    // Tables that should NOT have a tsvector column (telemetry is never FTS-searched)
    private static final List<String> ALL_TEL_TABLES = List.of(
        "relevance_log", "search_telemetry", "tier_writes",
        "nx_answer_runs", "hook_failures", "frecency");

    EmbeddedPostgres pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = EmbeddedPostgres.builder().start();

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db);
            liquibase.update(new Contexts());
        }
    }

    @AfterAll
    void stopAll() throws Exception {
        if (pg != null) pg.close();
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

    // ── Test 2: RLS on every telemetry table ─────────────────────────────────

    @Test
    void allTelemetryTables_rlsEnabledForcedWithPolicy() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            for (String table : ALL_TEL_TABLES) {
                ResultSet cls = su.createStatement().executeQuery(
                    "SELECT relrowsecurity, relforcerowsecurity " +
                    "FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid " +
                    "WHERE n.nspname = 'nexus' AND c.relname = '" + table + "'");
                assertThat(cls.next()).as(table + " must exist in pg_class").isTrue();
                assertThat(cls.getBoolean("relrowsecurity"))
                    .as(table + ": relrowsecurity must be true").isTrue();
                assertThat(cls.getBoolean("relforcerowsecurity"))
                    .as(table + ": relforcerowsecurity must be true").isTrue();

                ResultSet pol = su.createStatement().executeQuery(
                    "SELECT COUNT(*) AS cnt FROM pg_policies " +
                    "WHERE schemaname = 'nexus' AND tablename = '" + table + "'");
                pol.next();
                assertThat(pol.getLong("cnt"))
                    .as(table + " must have at least one RLS policy").isGreaterThan(0);
            }
        }
    }

    // ── Test 3: NO tsvector columns (confirmed: telemetry is not FTS-searched) ─

    @Test
    void allTelemetryTables_noTsvectorColumns() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            for (String table : ALL_TEL_TABLES) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT COUNT(*) AS cnt " +
                    "FROM pg_attribute a " +
                    "JOIN pg_class c ON c.oid = a.attrelid " +
                    "JOIN pg_namespace n ON n.oid = c.relnamespace " +
                    "JOIN pg_type t ON t.oid = a.atttypid " +
                    "WHERE n.nspname = 'nexus' AND c.relname = '" + table + "' " +
                    "  AND t.typname = 'tsvector' AND a.attnum > 0 AND NOT a.attisdropped");
                rs.next();
                assertThat(rs.getLong("cnt"))
                    .as(table + " must NOT have any tsvector columns (telemetry is not FTS-searched)")
                    .isEqualTo(0L);
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

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            for (var entry : tableToTsCol) {
                String table = entry[0];
                String tsCol  = entry[1];

                ResultSet idx = su.createStatement().executeQuery(
                    "SELECT COUNT(*) AS cnt " +
                    "FROM pg_index ix " +
                    "JOIN pg_class c  ON c.oid = ix.indrelid " +
                    "JOIN pg_class i  ON i.oid = ix.indexrelid " +
                    "JOIN pg_namespace n ON n.oid = c.relnamespace " +
                    "JOIN pg_am am ON am.oid = i.relam " +
                    "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(ix.indkey) " +
                    "WHERE n.nspname = 'nexus' AND c.relname = '" + table + "' " +
                    "  AND am.amname = 'btree' AND a.attname = '" + tsCol + "'");
                idx.next();
                assertThat(idx.getLong("cnt"))
                    .as("BTree index on " + table + "." + tsCol + " must exist for time-range queries")
                    .isGreaterThan(0L);
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

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            for (var entry : dedupIndexNames) {
                String table     = entry[0];
                String indexName = entry[1];

                ResultSet idx = su.createStatement().executeQuery(
                    "SELECT COUNT(*) AS cnt " +
                    "FROM pg_indexes " +
                    "WHERE schemaname = 'nexus' AND tablename = '" + table + "' " +
                    "  AND indexname = '" + indexName + "'");
                idx.next();
                assertThat(idx.getLong("cnt"))
                    .as("ETL dedup index " + indexName + " must exist on " + table)
                    .isEqualTo(1L);
            }
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private void assertColumns(String table, Set<String> expected) throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
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
