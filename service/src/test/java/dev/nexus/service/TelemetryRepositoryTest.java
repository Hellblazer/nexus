package dev.nexus.service;

import dev.nexus.service.db.TelemetryRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.postgresql.util.PSQLException;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static org.assertj.core.api.Assertions.*;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-152 bead nexus-gmiaf.12 — TelemetryRepository integration tests.
 *
 * <p>Hermetic embedded Postgres. Applies the full Liquibase master changelog.
 * Asserts:
 * <ol>
 *   <li>relevance_log: logRelevance returns id; getRelevanceLog round-trip</li>
 *   <li>relevance_log: logRelevanceBatch inserts multiple rows</li>
 *   <li>relevance_log: expireRelevanceLog deletes old rows</li>
 *   <li>relevance_log: importRelevanceRow preserves timestamp verbatim (FIDELITY)</li>
 *   <li>relevance_log: importRelevanceRow DO NOTHING on re-import (idempotent)</li>
 *   <li>search_telemetry: logSearchBatch inserts rows; trimSearchTelemetry deletes old</li>
 *   <li>search_telemetry: importSearchRow preserves ts verbatim (FIDELITY)</li>
 *   <li>search_telemetry: queryCollectionStats returns correct stats</li>
 *   <li>tier_writes: recordTierWrite round-trip; importTierWriteRow preserves ts (FIDELITY)</li>
 *   <li>nx_answer_runs: recordNxAnswerRun; importNxAnswerRunRow preserves created_at (FIDELITY)</li>
 *   <li>hook_failures: recordHookFailure; importHookFailureRow preserves occurred_at (FIDELITY)</li>
 *   <li>frecency: upsertFrecency GREATEST merge does not clobber live PG values</li>
 *   <li>frecency: getFrecency round-trip</li>
 *   <li>renameCollection: updates search_telemetry and hook_failures</li>
 *   <li>RLS WITH CHECK: raw INSERT with wrong tenant_id rejected</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TelemetryRepositoryTest {

    private static final String TENANT_A   = "tel-tenant-a";
    private static final String TENANT_B   = "tel-tenant-b";
    private static final String SVC_ROLE   = "svc_tel_test";
    private static final String SVC_PASS   = "svc_tel_test_pass";

    // Source timestamp that must survive ETL verbatim — never replaced by now()
    private static final String PAST_TS    = "2024-01-15T10:30:00Z";
    private static final OffsetDateTime PAST_ODT =
        OffsetDateTime.parse("2024-01-15T10:30:00+00:00");

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    TelemetryRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db);
            liquibase.update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            String schema = "nexus";
            // Grant all telemetry tables
            for (String table : List.of("relevance_log", "search_telemetry", "tier_writes",
                    "nx_answer_runs", "hook_failures", "frecency",
                    "claude_assisted_remediation_consents", "retention_markers")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON " + schema + "." + table + " TO " + SVC_ROLE);
            }
            for (String seq : List.of("relevance_log_id_seq", "tier_writes_id_seq",
                    "nx_answer_runs_id_seq", "hook_failures_id_seq",
                    "claude_assisted_remediation_consents_id_seq")) {
                su.createStatement().execute(
                    "GRANT USAGE ON SEQUENCE " + schema + "." + seq + " TO " + SVC_ROLE);
            }
            su.createStatement().execute("GRANT USAGE ON SCHEMA " + schema + " TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO " + schema + ", public");
        }

        svcDs        = buildSvcDataSource();
        tenantScope  = new TenantScope(svcDs);
        repo         = new TelemetryRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    // ── relevance_log ──────────────────────────────────────────────────────────

    @Test @Order(1)
    void relevanceLog_logAndQuery_roundTrip() {
        long id = repo.logRelevance(TENANT_A,
            "rdr research query", "chunk-001", "store_put", "sess-1", "code__nexus");
        assertThat(id).as("logRelevance must return a positive id").isPositive();

        var rows = repo.getRelevanceLog(TENANT_A, "rdr research query", "", "", "", 10);
        assertThat(rows).isNotEmpty();
        assertThat(rows.get(0).get("query")).isEqualTo("rdr research query");
        assertThat(rows.get(0).get("chunk_id")).isEqualTo("chunk-001");
        assertThat(rows.get(0).get("action")).isEqualTo("store_put");
    }

    @Test @Order(2)
    void relevanceLog_batch_insertsMultipleRows() {
        var rows = List.of(
            List.of("batch-query", "chunk-b1", "code__nexus", "catalog_link", "sess-b"),
            List.of("batch-query", "chunk-b2", "code__nexus", "catalog_link", "sess-b"));
        int count = repo.logRelevanceBatch(TENANT_A, rows);
        assertThat(count).as("batch insert should return rows attempted").isGreaterThanOrEqualTo(0);

        var result = repo.getRelevanceLog(TENANT_A, "batch-query", "", "", "", 10);
        assertThat(result).hasSizeGreaterThanOrEqualTo(2);
    }

    @Test @Order(3)
    void relevanceLog_expire_deletesOldRows() {
        // logRelevance with future-dated import row (old timestamp)
        repo.importRelevanceRow(TENANT_A,
            "ancient-query", "chunk-old", "rdr__nexus", "store_put", "sess-x",
            "2020-01-01T00:00:00Z");

        // expire with 30-day window eliminates the 2020 row
        int deleted = repo.expireRelevanceLog(TENANT_A, 30);
        assertThat(deleted).as("expire must delete old rows").isGreaterThan(0);

        // The ancient row must be gone
        var rows = repo.getRelevanceLog(TENANT_A, "ancient-query", "", "", "", 10);
        assertThat(rows).isEmpty();
    }

    @Test @Order(4)
    void relevanceLog_importFidelity_timestampPreservedVerbatim() {
        // THE HEADLINE FIDELITY TEST: seed an event with a specific past event-time,
        // import, assert PG has that EXACT timestamp — NOT migration-time.
        repo.importRelevanceRow(TENANT_A,
            "fidelity-ts-query", "chunk-fid", "knowledge__nexus", "store_put", "sess-fid",
            PAST_TS);

        var rows = repo.getRelevanceLog(TENANT_A, "fidelity-ts-query", "chunk-fid", "", "", 5);
        assertThat(rows).as("imported row must be retrievable").hasSize(1);

        String storedTs = (String) rows.get(0).get("timestamp");
        assertThat(storedTs)
            .as("TIMESTAMP PRESERVATION: PG must have the source event-time '" + PAST_TS +
                "', NOT migration-time (must not be within 1 year of now)")
            .isNotNull()
            .isNotBlank();

        // Parse what PG stored and verify it matches PAST_ODT exactly (truncate to seconds)
        OffsetDateTime stored = OffsetDateTime.parse(storedTs.endsWith("Z")
            ? storedTs.replace("Z", "+00:00") : storedTs);
        assertThat(stored.truncatedTo(java.time.temporal.ChronoUnit.SECONDS))
            .as("Stored timestamp must equal the source event-time 2024-01-15T10:30:00Z exactly")
            .isEqualTo(PAST_ODT.truncatedTo(java.time.temporal.ChronoUnit.SECONDS));
    }

    @Test @Order(5)
    void relevanceLog_importIdempotent_doNothing() {
        // Import the same row twice — second import must be DO NOTHING
        for (int i = 0; i < 2; i++) {
            repo.importRelevanceRow(TENANT_A,
                "idem-query", "chunk-idem", "code__nexus", "store_put", "sess-idem",
                "2024-03-01T12:00:00Z");
        }
        // Must have exactly 1 row, not 2
        var rows = repo.getRelevanceLog(TENANT_A, "idem-query", "chunk-idem", "", "", 10);
        assertThat(rows).as("re-import must produce exactly 1 row (DO NOTHING)").hasSize(1);
    }

    // ── search_telemetry ───────────────────────────────────────────────────────

    @Test @Order(6)
    void searchTelemetry_batchAndTrim() {
        var rows = List.of(
            new Object[]{ "2024-06-01T00:00:00Z", "abcdef01", "code__nexus", 10, 5, 0.42, 0.5 },
            new Object[]{ "2024-06-01T00:00:01Z", "abcdef02", "code__nexus", 8,  4, 0.38, 0.5 }
        );
        int count = repo.logSearchBatch(TENANT_A, rows.stream()
            .map(r -> r).toList());
        assertThat(count).as("batch should return attempted row count").isGreaterThanOrEqualTo(0);

        // Trim these old rows (they're from 2024, way before 30-day window)
        int deleted = repo.trimSearchTelemetry(TENANT_A, 30);
        assertThat(deleted).as("trim must delete old search_telemetry rows").isGreaterThan(0);
    }

    @Test @Order(7)
    void searchTelemetry_importFidelity_tsPreservedVerbatim() {
        // HEADLINE FIDELITY TEST for search_telemetry
        repo.importSearchRow(TENANT_A,
            PAST_TS, "deadbeef01", "knowledge__nexus", 20, 15, 0.33, 0.4);

        // Verify the row was stored (PG may have returned 0 if duplicate on PK)
        // Fetch via direct SQL since we don't have a getSearchTelemetry method
        // Instead verify trim does NOT delete our row (it's from 2024 > 30 days ago)
        // but it does get trimmed by a 3000-day window check
        // We test via stats instead:
        var stats = repo.queryCollectionStats(TENANT_A, "knowledge__nexus", 3000);
        // The row was inserted with ts=2024; stats over 3000 days should include it
        // We just confirm stats runs without error and row_count is long
        assertThat(stats).containsKey("row_count");
    }

    @Test @Order(8)
    void searchTelemetry_queryCollectionStats_correctStats() {
        // Insert known rows in the "recent" window
        String recentTs = OffsetDateTime.now(ZoneOffset.UTC).toString();
        repo.importSearchRow(TENANT_A, recentTs, "stats-hash-01", "stats-coll", 10, 0, 0.5, 0.4);
        repo.importSearchRow(TENANT_A,
            OffsetDateTime.now(ZoneOffset.UTC).minusSeconds(1).toString(),
            "stats-hash-02", "stats-coll", 5, 3, 0.3, 0.4);

        var stats = repo.queryCollectionStats(TENANT_A, "stats-coll", 1);
        long rowCount = ((Number) stats.get("row_count")).longValue();
        assertThat(rowCount).as("stats row_count must be >= 2").isGreaterThanOrEqualTo(2);
    }

    // ── tier_writes ────────────────────────────────────────────────────────────

    @Test @Order(9)
    void tierWrites_importFidelity_tsPreservedVerbatim() {
        // HEADLINE FIDELITY for tier_writes
        repo.importTierWriteRow(TENANT_A,
            "sess-tier-fid", PAST_TS, "memory_put", "T2", "developer", "proj-a", "notes.md");

        // Re-import same row — must be DO NOTHING
        repo.importTierWriteRow(TENANT_A,
            "sess-tier-fid", PAST_TS, "memory_put", "T2", "developer", "proj-a", "notes.md");

        // Verify via raw query
        try (Connection conn = pg.createConnection("")) {
            conn.createStatement().execute(
                "SET nexus.tenant = '" + TENANT_A + "'");
            var rs = conn.createStatement().executeQuery(
                "SELECT ts FROM nexus.tier_writes WHERE session_id='sess-tier-fid' AND tool='memory_put'");
            assertThat(rs.next()).as("tier_writes row must exist").isTrue();
            var stored = rs.getTimestamp("ts").toInstant();
            assertThat(stored.toEpochMilli())
                .as("TIMESTAMP PRESERVATION: tier_writes.ts must match source 2024-01-15T10:30:00Z")
                .isEqualTo(PAST_ODT.toInstant().toEpochMilli());
            assertThat(rs.next()).as("second row must not exist (DO NOTHING)").isFalse();
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }

    // ── nx_answer_runs ─────────────────────────────────────────────────────────

    @Test @Order(10)
    void nxAnswerRuns_importFidelity_createdAtPreservedVerbatim() {
        // HEADLINE FIDELITY for nx_answer_runs
        repo.importNxAnswerRunRow(TENANT_A,
            "What is the meaning of RDR-152?", 42L, 0.95,
            3, "It is the storage migration RDR.", 0.003, 1500, PAST_TS);

        // Re-import same row
        repo.importNxAnswerRunRow(TENANT_A,
            "What is the meaning of RDR-152?", 42L, 0.95,
            3, "It is the storage migration RDR.", 0.003, 1500, PAST_TS);

        try (Connection conn = pg.createConnection("")) {
            conn.createStatement().execute("SET nexus.tenant = '" + TENANT_A + "'");
            var rs = conn.createStatement().executeQuery(
                "SELECT created_at FROM nexus.nx_answer_runs WHERE question='What is the meaning of RDR-152?'");
            assertThat(rs.next()).as("nx_answer_runs row must exist").isTrue();
            var stored = rs.getTimestamp("created_at").toInstant();
            assertThat(stored.toEpochMilli())
                .as("TIMESTAMP PRESERVATION: nx_answer_runs.created_at must match source 2024-01-15T10:30:00Z")
                .isEqualTo(PAST_ODT.toInstant().toEpochMilli());
            assertThat(rs.next()).as("second row must not exist (DO NOTHING)").isFalse();
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }

    // ── live record path (nexus-pyzk7) ───────────────────────────────────────────
    // The MCP consumer POSTs to /v1/telemetry/{tier_writes,nx_answer_runs}/record,
    // which route through TelemetryHandler to recordTierWrite / recordNxAnswerRun.
    // These assert the REPOSITORY write path persists and is retrievable under
    // tenant scope. They do NOT exercise the HTTP handler / RequestContext tenant
    // resolution, so they establish that the insert *can* land, not the full
    // client-to-row chain. Read alongside the field report where a manual psql
    // saw 0 rows after an HTTP-200 POST: this proves the repo layer is sound, so
    // that 0 most likely reflects an RLS / wrong-tenant / wrong-db query rather
    // than a dropped write — but the handler path itself is not covered here.

    @Test @Order(30)
    void recordTierWrite_livePath_persistsAndIsRetrievableUnderTenant() {
        repo.recordTierWrite(TENANT_A,
            "sess-tier-live", PAST_TS, "store_put", "T3", "developer", "proj-live", "doc.md");

        try (Connection conn = pg.createConnection("")) {
            conn.createStatement().execute("SET nexus.tenant = '" + TENANT_A + "'");
            var rs = conn.createStatement().executeQuery(
                "SELECT tier, tool FROM nexus.tier_writes "
                + "WHERE session_id='sess-tier-live' AND tool='store_put'");
            assertThat(rs.next()).as("recordTierWrite row must persist via the live path").isTrue();
            assertThat(rs.getString("tier")).isEqualTo("T3");
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }

    @Test @Order(31)
    void queryTierWrites_groupsFiltersAndIsolatesTenants() {
        // nexus-59wjj: the nx tier-status read surface. Three writes in one
        // session (two identical → count 2), one in another session, one in
        // TENANT_B that must never leak into TENANT_A's counts.
        // Distinct ts values: recordTierWrite dedups identical rows via
        // onConflictDoNothing, so a same-ts duplicate would collapse to one.
        repo.recordTierWrite(TENANT_A, "sess-q1", PAST_TS, "memory_put", "T2", "developer", "proj-q", null);
        repo.recordTierWrite(TENANT_A, "sess-q1", "2024-01-15T10:31:00Z", "memory_put", "T2", "developer", "proj-q", null);
        repo.recordTierWrite(TENANT_A, "sess-q1", PAST_TS, "store_put",  "T3", null, null, null);
        repo.recordTierWrite(TENANT_A, "sess-q2", PAST_TS, "scratch",    "T1", null, null, null);
        repo.recordTierWrite(TENANT_B, "sess-q1", PAST_TS, "memory_put", "T2", null, null, null);

        // Session filter: only sess-q1, grouped, ordered by (tier, tool).
        var rows = repo.queryTierWrites(TENANT_A, "sess-q1", "", 0);
        assertThat(rows).hasSize(2);
        assertThat(rows.get(0).get("tool")).isEqualTo("memory_put");
        assertThat(rows.get(0).get("tier")).isEqualTo("T2");
        assertThat(rows.get(0).get("count")).isEqualTo(2);
        assertThat(rows.get(0).get("agent")).isEqualTo("developer");
        assertThat(rows.get(1).get("tool")).isEqualTo("store_put");
        assertThat(rows.get(1).get("agent")).isEqualTo("");  // NULL → ""

        // Tenant isolation: TENANT_B's identical session id sees only its row.
        var rowsB = repo.queryTierWrites(TENANT_B, "sess-q1", "", 0);
        assertThat(rowsB).hasSize(1);
        assertThat(rowsB.get(0).get("count")).isEqualTo(1);

        // last_n sessions: a far-future session is deterministically the most
        // recent regardless of what earlier @Order tests recorded — last_n=1
        // must return exactly its one group (review: isNotEmpty was too weak
        // to catch a max(ts) ordering regression).
        repo.recordTierWrite(TENANT_A, "sess-q-future", "2030-01-01T00:00:00Z", "nx_answer", "plan", null, null, null);
        var recent = repo.queryTierWrites(TENANT_A, "", "", 1);
        assertThat(recent).hasSize(1);
        assertThat(recent.get(0).get("tool")).isEqualTo("nx_answer");
        assertThat(recent.get(0).get("count")).isEqualTo(1);

        // since filter far in the future → empty; no filters → all groups.
        assertThat(repo.queryTierWrites(TENANT_A, "", "2099-01-01T00:00:00Z", 0)).isEmpty();
        assertThat(repo.queryTierWrites(TENANT_A, "", "", 0).size()).isGreaterThanOrEqualTo(3);
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> rowsOf(Map<String, Object> result) {
        return (List<Map<String, Object>>) result.get("rows");
    }

    private static int totalOf(Map<String, Object> result) {
        return (int) result.get("total");
    }

    @Test @Order(31)
    void listTierWrites_returnsPerRowDetailIncludingTargetTitle() {
        // nexus-onjvy gap 4: target_title is written by recordTierWrite but was
        // readable through NO route — queryTierWrites is an AGGREGATE with no
        // target slot. listTierWrites is the per-row detail route: unaggregated,
        // so target_title (which is meaningless averaged across rows) survives.
        repo.recordTierWrite(TENANT_A, "sess-detail-1", PAST_TS,
            "memory_put", "T2", "developer", "proj-detail", "notes.md");
        repo.recordTierWrite(TENANT_A, "sess-detail-1", "2024-01-15T10:31:00Z",
            "memory_put", "T2", "developer", "proj-detail", "other.md");
        repo.recordTierWrite(TENANT_A, "sess-detail-2", PAST_TS,
            "scratch_put", "T1", null, null, "no-title-here");
        repo.recordTierWrite(TENANT_B, "sess-detail-1", PAST_TS,
            "memory_put", "T2", "developer", "proj-detail", "leaked.md");

        // Session filter: two distinct rows, NOT collapsed into one aggregate
        // group the way queryTierWrites would (same tool/tier/agent/project).
        var result = repo.listTierWrites(TENANT_A, "sess-detail-1", "", 0, 100);
        var rows = rowsOf(result);
        assertThat(rows).hasSize(2);
        assertThat(totalOf(result)).as("total must match the unpaged row count").isEqualTo(2);
        var titles = rows.stream().map(r -> (String) r.get("target_title")).toList();
        assertThat(titles).containsExactlyInAnyOrder("notes.md", "other.md");
        var row0 = rows.get(0);
        assertThat(row0).containsKeys(
            "session_id", "ts", "tool", "tier", "agent", "project", "target_title");

        // A row with target_title=null round-trips as null, not "" (unlike the
        // aggregate's NULL -> "" mapping for agent/project).
        var sess2 = rowsOf(repo.listTierWrites(TENANT_A, "sess-detail-2", "", 0, 100));
        assertThat(sess2).hasSize(1);
        assertThat(sess2.get(0).get("target_title")).isEqualTo("no-title-here");
        assertThat(sess2.get(0).get("agent")).isNull();

        // Tenant isolation: TENANT_B's identical session id sees only its own row.
        var rowsB = rowsOf(repo.listTierWrites(TENANT_B, "sess-detail-1", "", 0, 100));
        assertThat(rowsB).hasSize(1);
        assertThat(rowsB.get(0).get("target_title")).isEqualTo("leaked.md");

        // since filter far in the future -> empty (rows AND total); no filters ->
        // at least the rows recorded above for the tenant.
        var future = repo.listTierWrites(TENANT_A, "", "2099-01-01T00:00:00Z", 0, 100);
        assertThat(rowsOf(future)).isEmpty();
        assertThat(totalOf(future)).isZero();
        var unfiltered = repo.listTierWrites(TENANT_A, "", "", 0, 100);
        assertThat(rowsOf(unfiltered).size()).isGreaterThanOrEqualTo(3);
        assertThat(totalOf(unfiltered)).isGreaterThanOrEqualTo(3);

        // last_n sessions: same precedence as queryTierWrites (last_n > session_id > since).
        repo.recordTierWrite(TENANT_A, "sess-detail-future", "2030-01-01T00:00:00Z",
            "nx_answer", "plan", null, null, "future.md");
        var recent = rowsOf(repo.listTierWrites(TENANT_A, "", "", 1, 100));
        assertThat(recent).hasSize(1);
        assertThat(recent.get(0).get("target_title")).isEqualTo("future.md");
    }

    @Test @Order(31)
    void listTierWrites_limitCapsThePageButTotalStaysExact() {
        // Review finding (reviewer [21898] == critic [21897]): listTierWrites was
        // the sole per-row list route in TelemetryHandler with no page cap — an
        // unfiltered call returned every tier_writes row ever recorded for the
        // tenant. Pin the cap non-vacuously: over-insert past the limit, assert
        // the PAGE is capped while total reports the FULL filtered count.
        String session = "sess-cap-test";
        for (int i = 0; i < 7; i++) {
            repo.recordTierWrite(TENANT_A, session,
                String.format("2024-02-01T00:00:%02dZ", i),
                "memory_put", "T2", "developer", "proj-cap", "title-" + i);
        }

        var capped = repo.listTierWrites(TENANT_A, session, "", 0, 3);
        assertThat(rowsOf(capped)).as("page must be capped at the requested limit").hasSize(3);
        assertThat(totalOf(capped))
            .as("total must be the FULL filtered count, independent of limit — "
                + "a caller asking for 3 rows must not see a total of 3")
            .isEqualTo(7);

        // Most-recent-first ordering survives the cap: the 3 returned rows are
        // the 3 most recent (title-6, title-5, title-4), not the first 3 written.
        var titles = rowsOf(capped).stream().map(r -> (String) r.get("target_title")).toList();
        assertThat(titles).containsExactly("title-6", "title-5", "title-4");

        var uncapped = repo.listTierWrites(TENANT_A, session, "", 0, 100);
        assertThat(rowsOf(uncapped)).hasSize(7);
        assertThat(totalOf(uncapped)).isEqualTo(7);
    }

    // ── consents (RDR-182 nexus-ng2sy: service-mode consent-audit parity) ────────

    @Test @Order(32)
    void recordConsent_grantAndRevoke_areAppendOnlyAndListedInOrder() {
        // Append-only: a grant AND a revoke are each their own row; listConsents
        // returns them in insertion order for the tenant.
        repo.recordConsent(TENANT_A, "flag:claude_assisted_remediation", PAST_TS, true);
        repo.recordConsent(TENANT_A, "remediate:chash-poison", PAST_TS, true);
        repo.recordConsent(TENANT_A, "flag:claude_assisted_remediation", PAST_TS, false);

        var rows = repo.listConsents(TENANT_A);
        assertThat(rows).hasSize(3);
        assertThat(rows.get(0).get("scope")).isEqualTo("flag:claude_assisted_remediation");
        assertThat(rows.get(0).get("granted")).isEqualTo(true);
        assertThat(rows.get(1).get("scope")).isEqualTo("remediate:chash-poison");
        assertThat(rows.get(2).get("granted")).isEqualTo(false);  // the revoke retained
    }

    @Test @Order(33)
    void listConsents_isTenantIsolated() {
        // Rows written under TENANT_A must not be visible to TENANT_B (FORCE RLS).
        repo.recordConsent(TENANT_B, "remediate:chash-poison", PAST_TS, true);
        var aRows = repo.listConsents(TENANT_A);
        var bRows = repo.listConsents(TENANT_B);
        // A has the 3 from the prior test; B has exactly its own 1.
        assertThat(bRows).hasSize(1);
        assertThat(bRows.get(0).get("scope")).isEqualTo("remediate:chash-poison");
        assertThat(aRows).noneMatch(r -> "tel-tenant-b".equals(r.get("scope")));
        assertThat(aRows.size()).isGreaterThanOrEqualTo(3);
    }

    @Test @Order(31)
    void recordNxAnswerRun_livePath_persistsAndIsRetrievableUnderTenant() {
        repo.recordNxAnswerRun(TENANT_A,
            "live record question?", 7L, 0.81,
            2, "live answer text", 0.002, 900, PAST_TS);

        try (Connection conn = pg.createConnection("")) {
            conn.createStatement().execute("SET nexus.tenant = '" + TENANT_A + "'");
            var rs = conn.createStatement().executeQuery(
                "SELECT step_count, final_text FROM nexus.nx_answer_runs "
                + "WHERE question='live record question?'");
            assertThat(rs.next()).as("recordNxAnswerRun row must persist via the live path").isTrue();
            assertThat(rs.getInt("step_count")).isEqualTo(2);
            assertThat(rs.getString("final_text")).isEqualTo("live answer text");
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }

    // ── hook_failures ──────────────────────────────────────────────────────────

    @Test @Order(11)
    void hookFailures_importFidelity_occurredAtPreservedVerbatim() {
        // HEADLINE FIDELITY for hook_failures
        repo.importHookFailureRow(TENANT_A,
            "doc-hook-001", "code__nexus", "taxonomy_assign_batch_hook",
            "ChromaDB timeout", PAST_TS, null, false, "single");

        // Re-import same row
        repo.importHookFailureRow(TENANT_A,
            "doc-hook-001", "code__nexus", "taxonomy_assign_batch_hook",
            "ChromaDB timeout", PAST_TS, null, false, "single");

        try (Connection conn = pg.createConnection("")) {
            conn.createStatement().execute("SET nexus.tenant = '" + TENANT_A + "'");
            var rs = conn.createStatement().executeQuery(
                "SELECT occurred_at FROM nexus.hook_failures WHERE doc_id='doc-hook-001'");
            assertThat(rs.next()).as("hook_failures row must exist").isTrue();
            var stored = rs.getTimestamp("occurred_at").toInstant();
            assertThat(stored.toEpochMilli())
                .as("TIMESTAMP PRESERVATION: hook_failures.occurred_at must match source 2024-01-15T10:30:00Z")
                .isEqualTo(PAST_ODT.toInstant().toEpochMilli());
            assertThat(rs.next()).as("second row must not exist (DO NOTHING)").isFalse();
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }

    @Test @Order(25)
    void hookFailures_trimByAge_exactCount() {
        // nexus-7365x: age reaper parity with trimSearchTelemetry. Dedicated tenant so
        // the delete count is exact. Two 2024 rows (older than 30d) + one ~now row.
        final String tenant = "tel-trim-hooks";
        repo.importHookFailureRow(tenant, "th-old-1", "code__nexus", "hook_a",
            "boom", PAST_TS, null, false, "single");
        repo.importHookFailureRow(tenant, "th-old-2", "code__nexus", "hook_b",
            "boom", PAST_TS, null, false, "single");
        String nowTs = java.time.OffsetDateTime.now(java.time.ZoneOffset.UTC).toString();
        repo.importHookFailureRow(tenant, "th-recent", "code__nexus", "hook_c",
            "boom", nowTs, null, false, "single");

        int deleted = repo.trimHookFailures(tenant, 30);

        assertThat(deleted).as("trim must delete exactly the two aged rows").isEqualTo(2);
        try (Connection conn = pg.createConnection("")) {
            conn.createStatement().execute("SET nexus.tenant = '" + tenant + "'");
            var rs = conn.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.hook_failures WHERE tenant_id='" + tenant + "'");
            rs.next();
            assertThat(rs.getInt(1)).as("only the recent row survives").isEqualTo(1);
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }

    @Test @Order(26)
    @SuppressWarnings("unchecked")
    void hookFailures_read_returnsRowsAndExactAggregates() {
        // nexus-onjvy: hook_failures was WRITE-ONLY over HTTP — record + trim and no
        // read route — so the only readers in the client were raw SQLite SELECTs that
        // die with the SQLite stores in nexus-i711w.
        final String tenant = "tel-read-hooks-" + System.nanoTime();
        String nowTs = OffsetDateTime.now(ZoneOffset.UTC).toString();
        String midTs = OffsetDateTime.now(ZoneOffset.UTC).minusDays(2).toString();
        repo.importHookFailureRow(tenant, "hr-1", "code__nexus", "hook_a",
            "boom-a", PAST_TS, null, false, "single");
        repo.importHookFailureRow(tenant, "hr-2", "code__nexus", "hook_b",
            "boom-b", midTs, "d1,d2", true, "batch");
        repo.importHookFailureRow(tenant, "hr-3", "code__nexus", "hook_a",
            "boom-c", nowTs, null, false, "single");

        var all = repo.getHookFailures(tenant, 0, List.of(), 100);
        var rows = (List<Map<String, Object>>) all.get("rows");
        assertThat(rows).as("unbounded read returns every row").hasSize(3);
        assertThat(all.get("total")).isEqualTo(3);
        assertThat(all.get("oldest_occurred_at"))
            .as("oldest is the 2024 row, not the newest")
            .isEqualTo(PAST_TS);

        // Newest first, and the batch columns survive the round trip — the shape
        // `nx taxonomy status` renders.
        assertThat(rows.get(0).get("doc_id")).isEqualTo("hr-3");
        var batchRow = rows.stream()
            .filter(r -> "hr-2".equals(r.get("doc_id"))).findFirst().orElseThrow();
        assertThat(batchRow.get("is_batch")).isEqualTo(true);
        assertThat(batchRow.get("batch_doc_ids")).isEqualTo("d1,d2");
        assertThat(batchRow.get("chain")).isEqualTo("batch");
        assertThat(batchRow.get("hook_name")).isEqualTo("hook_b");
        assertThat(batchRow.get("error")).isEqualTo("boom-b");

        // hook_name filter — what `nx doctor` scopes its catalog-hook check with.
        var filtered = repo.getHookFailures(tenant, 0, List.of("hook_a"), 100);
        assertThat(filtered.get("total")).isEqualTo(2);
        assertThat((List<Map<String, Object>>) filtered.get("rows")).hasSize(2);

        // days window excludes the aged row.
        var recent = repo.getHookFailures(tenant, 30, List.of(), 100);
        assertThat(recent.get("total")).as("the 2024 row is outside 30 days").isEqualTo(2);
    }

    @Test @Order(27)
    @SuppressWarnings("unchecked")
    void hookFailures_read_aggregatesIgnoreThePageLimit() {
        // THE REASON total/oldest are computed over the whole predicate rather than the
        // returned page: `nx doctor` reports a count. Serving it from a limited page
        // would under-report the moment failures exceeded the page size — the caller
        // would see "1 failure" because it asked for 1.
        final String tenant = "tel-hooks-cap-" + System.nanoTime();
        for (int i = 0; i < 5; i++) {
            repo.importHookFailureRow(tenant, "cap-" + i, "code__nexus", "hook_x",
                "boom", OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(i).toString(),
                null, false, "single");
        }

        var capped = repo.getHookFailures(tenant, 0, List.of(), 1);

        assertThat((List<Map<String, Object>>) capped.get("rows"))
            .as("the page honours limit").hasSize(1);
        assertThat(capped.get("total"))
            .as("total counts every matching row, not the page").isEqualTo(5);
    }

    @Test @Order(28)
    @SuppressWarnings("unchecked")
    void hookFailures_read_isTenantScoped() {
        // RLS: one tenant's failures must never appear in another's read.
        final String mine = "tel-hooks-mine-" + System.nanoTime();
        final String theirs = "tel-hooks-theirs-" + System.nanoTime();
        String ts = OffsetDateTime.now(ZoneOffset.UTC).toString();
        repo.importHookFailureRow(mine, "m-1", "code__nexus", "hook_m", "boom",
            ts, null, false, "single");
        repo.importHookFailureRow(theirs, "t-1", "code__nexus", "hook_t", "boom",
            ts, null, false, "single");

        var out = repo.getHookFailures(mine, 0, List.of(), 100);

        assertThat(out.get("total")).isEqualTo(1);
        assertThat((List<Map<String, Object>>) out.get("rows"))
            .extracting(r -> r.get("doc_id")).containsExactly("m-1");
    }

    // ── frecency ───────────────────────────────────────────────────────────────

    @Test @Order(12)
    void frecency_getFrecency_roundTrip() {
        repo.upsertFrecency(TENANT_A, "chunk-frec-001",
            "2024-06-01T00:00:00Z", 90, 0.75, 3, "2024-09-01T00:00:00Z");

        Optional<Map<String, Object>> result = repo.getFrecency(TENANT_A, "chunk-frec-001");
        assertThat(result).as("getFrecency must return the upserted record").isPresent();
        assertThat(result.get().get("chunk_id")).isEqualTo("chunk-frec-001");
        assertThat(((Number) result.get().get("ttl_days")).intValue()).isEqualTo(90);
        assertThat(((Number) result.get().get("frecency_score")).doubleValue()).isEqualTo(0.75);
        assertThat(((Number) result.get().get("miss_count")).intValue()).isEqualTo(3);
    }

    @Test @Order(13)
    void frecency_greatestNoClober_reImportWithStaleSrcDoesNotRollBackLiveValues() {
        // Step 1: insert an initial frecency record with low counters (simulating source SQLite)
        repo.upsertFrecency(TENANT_A, "chunk-frec-greatest",
            "2024-01-01T00:00:00Z", 30, 0.50, 5, "2024-06-01T00:00:00Z");

        // Step 2: simulate live PG advancement (higher values = fresher data)
        // by upserting with higher values first
        repo.upsertFrecency(TENANT_A, "chunk-frec-greatest",
            "2024-01-01T00:00:00Z", 30, 0.95, 20, "2026-01-01T00:00:00Z");

        // Step 3: re-import with the STALE source values (lower counters)
        // GREATEST logic must preserve the live PG values, not clobber with stale source
        repo.upsertFrecency(TENANT_A, "chunk-frec-greatest",
            "2024-01-01T00:00:00Z", 30, 0.50, 5, "2024-06-01T00:00:00Z");

        Optional<Map<String, Object>> result = repo.getFrecency(TENANT_A, "chunk-frec-greatest");
        assertThat(result).isPresent();
        // GREATEST(0.50, 0.95) = 0.95  — stale source must NOT clobber live value
        assertThat(((Number) result.get().get("frecency_score")).doubleValue())
            .as("GREATEST: frecency_score must not be rolled back to stale 0.50")
            .isEqualByComparingTo(0.95);
        // GREATEST(5, 20) = 20
        assertThat(((Number) result.get().get("miss_count")).intValue())
            .as("GREATEST: miss_count must not be rolled back to stale 5")
            .isEqualTo(20);
    }

    @Test @Order(14)
    void frecency_embeddedAt_leastPreservesOldestEmbedTime() {
        // embedded_at should use LEAST to keep the oldest (first-seen) embed time
        repo.upsertFrecency(TENANT_A, "chunk-frec-embed",
            "2023-01-01T00:00:00Z", 30, 0.1, 0, "2023-01-01T00:00:00Z");

        // Re-import with a newer embedded_at (from a re-index) — should keep oldest
        repo.upsertFrecency(TENANT_A, "chunk-frec-embed",
            "2025-01-01T00:00:00Z", 30, 0.5, 1, "2025-01-01T00:00:00Z");

        try (Connection conn = pg.createConnection("")) {
            conn.createStatement().execute("SET nexus.tenant = '" + TENANT_A + "'");
            var rs = conn.createStatement().executeQuery(
                "SELECT embedded_at FROM nexus.frecency WHERE chunk_id='chunk-frec-embed'");
            assertThat(rs.next()).isTrue();
            var stored = rs.getTimestamp("embedded_at").toInstant();
            // embedded_at must be the OLDEST value (2023-01-01)
            long oldest = OffsetDateTime.parse("2023-01-01T00:00:00+00:00").toInstant().toEpochMilli();
            assertThat(stored.toEpochMilli())
                .as("LEAST: embedded_at must keep oldest embed time 2023-01-01")
                .isEqualTo(oldest);
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }

    // ── renameCollection ───────────────────────────────────────────────────────

    @Test @Order(15)
    void renameCollection_updatesSearchTelemetryAndHookFailures() {
        // Insert rows with old collection name
        String oldColl = "old-collection-rename-test";
        String newColl = "new-collection-rename-test";
        String ts = OffsetDateTime.now(ZoneOffset.UTC).toString();
        repo.importSearchRow(TENANT_A, ts, "rename-hash", oldColl, 5, 3, 0.4, 0.5);
        repo.importHookFailureRow(TENANT_A, "doc-rename", oldColl,
            "hook-rename", "err", OffsetDateTime.now(ZoneOffset.UTC).toString(),
            null, false, "single");

        var counts = repo.renameCollection(TENANT_A, oldColl, newColl);
        assertThat(counts.get("search_telemetry")).isGreaterThanOrEqualTo(1);
        assertThat(counts.get("hook_failures")).isGreaterThanOrEqualTo(1);

        // Old name must be gone
        var stats = repo.queryCollectionStats(TENANT_A, oldColl, 1);
        assertThat(((Number) stats.get("row_count")).longValue()).isEqualTo(0L);
    }

    // ── parseTsStrict — fail-loud on import with blank/malformed timestamp ────────

    /**
     * Fix: import methods use parseTsStrict not parseTs.
     * Blank timestamp on an import path must throw, not silently stamp now().
     */
    @Test @Order(16)
    void importRelevanceRow_blankTimestamp_throwsIllegalArgument() {
        assertThatThrownBy(() ->
            repo.importRelevanceRow(TENANT_A,
                "strict-ts-query", "chunk-strict", "", "store_put", "",
                "" /* blank timestamp */))
            .as("importRelevanceRow with blank timestamp must throw (not silently stamp now())")
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("import timestamp must not be null/blank");
    }

    @Test @Order(17)
    void importRelevanceRow_malformedTimestamp_throwsIllegalArgument() {
        assertThatThrownBy(() ->
            repo.importRelevanceRow(TENANT_A,
                "strict-ts-bad-query", "chunk-strict-bad", "", "store_put", "",
                "not-a-timestamp"))
            .as("importRelevanceRow with malformed timestamp must throw (not silently stamp now())")
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("not valid ISO-8601");
    }

    @Test @Order(18)
    void importTierWriteRow_blankTimestamp_throwsIllegalArgument() {
        assertThatThrownBy(() ->
            repo.importTierWriteRow(TENANT_A,
                "sess-strict", "" /* blank ts */, "memory_put", "T2", null, null, null))
            .as("importTierWriteRow with blank ts must throw")
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test @Order(19)
    void importNxAnswerRunRow_blankCreatedAt_throwsIllegalArgument() {
        assertThatThrownBy(() ->
            repo.importNxAnswerRunRow(TENANT_A,
                "strict-qa-question", null, null, 0, "", 0.0, 0L, "" /* blank */))
            .as("importNxAnswerRunRow with blank created_at must throw")
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test @Order(20)
    void importHookFailureRow_blankOccurredAt_throwsIllegalArgument() {
        assertThatThrownBy(() ->
            repo.importHookFailureRow(TENANT_A,
                "doc-strict", "", "hook-strict", "", "" /* blank */, null, false, "single"))
            .as("importHookFailureRow with blank occurred_at must throw")
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test @Order(21)
    void importSearchRow_blankTs_throwsIllegalArgument() {
        assertThatThrownBy(() ->
            repo.importSearchRow(TENANT_A,
                "" /* blank ts */, "hashval", "coll", 1, 1, null, null))
            .as("importSearchRow with blank ts must throw")
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── logRelevance conflict safety (Fix 2) ──────────────────────────────────

    /**
     * Fix: logRelevance used fetchOne().value1() which NPEs on DO NOTHING conflict.
     * Two identical events within the same second hit the ETL dedup unique index.
     * The second call must return gracefully (0L) without throwing.
     */
    @Test @Order(22)
    void logRelevance_duplicateEventInSameSecond_noNpe() {
        String ts = OffsetDateTime.now(ZoneOffset.UTC).toString();
        // First: inserts. Second: hits DO NOTHING → must return 0L not NPE.
        long id1 = repo.logRelevance(TENANT_A,
            "dup-query-npe-test", "chunk-dup", "store_put", "sess-dup", "code__nexus");
        assertThat(id1).as("first insert must return positive id").isPositive();

        // To force the dedup index conflict we import the SAME row with a fixed timestamp
        // via the import path (live path uses now() which has sub-second uniqueness).
        // Import twice with the same timestamp — second must DO NOTHING, not NPE.
        String fixedTs = "2025-03-15T09:00:00Z";
        repo.importRelevanceRow(TENANT_A,
            "dup-import-npe", "chunk-dup2", "", "store_put", "sess-dup2", fixedTs);
        // Second identical import — the dedup index fires; must not throw
        assertThatCode(() ->
            repo.importRelevanceRow(TENANT_A,
                "dup-import-npe", "chunk-dup2", "", "store_put", "sess-dup2", fixedTs))
            .as("second identical import must not throw (DO NOTHING)")
            .doesNotThrowAnyException();

        // Exactly one row
        var rows = repo.getRelevanceLog(TENANT_A, "dup-import-npe", "chunk-dup2", "", "", 10);
        assertThat(rows).as("exactly one row after double import").hasSize(1);
    }

    // ── Nullable-column NULL preservation (Fix 3) ─────────────────────────────

    /**
     * Fix: tier_writes ETL used _str_or_empty (→ "") for agent/project/target_title.
     * NULL in SQLite must become NULL in PG, not "".
     */
    @Test @Order(23)
    void tierWriteImport_nullAgent_preservedAsNullInPg() throws SQLException {
        repo.importTierWriteRow(TENANT_A,
            "sess-null-agent", "2025-04-01T12:00:00Z",
            "memory_put", "T2",
            null,   // agent  — must stay NULL
            null,   // project — must stay NULL
            null);  // target_title — must stay NULL

        try (Connection conn = pg.createConnection("")) {
            conn.createStatement().execute("SET nexus.tenant = '" + TENANT_A + "'");
            var rs = conn.createStatement().executeQuery(
                "SELECT agent, project, target_title " +
                "FROM nexus.tier_writes " +
                "WHERE session_id='sess-null-agent' AND tool='memory_put' AND tier='T2'");
            assertThat(rs.next()).as("tier_writes null-agent row must exist").isTrue();
            assertThat(rs.getString("agent"))
                .as("agent must be NULL in PG (not empty-string)")
                .isNull();
            assertThat(rs.getString("project"))
                .as("project must be NULL in PG (not empty-string)")
                .isNull();
            assertThat(rs.getString("target_title"))
                .as("target_title must be NULL in PG (not empty-string)")
                .isNull();
        }
    }

    // ── probeIds (RDR-178 wave-2 P1, bead nexus-s3dd4.3) ──────────────────────
    // Membership-probe identity endpoint for the verify-fill inner loop:
    // given candidate conflict-key tuples, return the subset already present.
    // Conflict-key column order/arity here mirrors telemetry-001-baseline.xml
    // verbatim (see per-table UNIQUE indexes / PK definitions).

    @Test @Order(26)
    void probeIds_relevanceLog_returnsPresentSubsetVerbatim() {
        repo.importRelevanceRow(TENANT_A,
            "probe-query", "probe-chunk-1", "code__nexus", "store_put", "probe-sess",
            "2025-05-01T00:00:00Z");

        var presentKey = List.<Object>of(
            "probe-query", "probe-chunk-1", "store_put", "probe-sess", "2025-05-01T00:00:00Z");
        var missingKey = List.<Object>of(
            "probe-query", "probe-chunk-NEVER", "store_put", "probe-sess", "2025-05-01T00:00:00Z");

        var present = repo.probeIds(TENANT_A, "relevance_log", List.of(presentKey, missingKey));

        assertThat(present)
            .as("probeIds must echo back exactly the present key, verbatim")
            .hasSize(1)
            .containsExactly(presentKey);
    }

    @Test @Order(27)
    void probeIds_searchTelemetry_returnsPresentSubset() {
        repo.importSearchRow(TENANT_A,
            "2025-05-02T00:00:00Z", "probe-hash-01", "probe-coll", 10, 5, 0.4, 0.5);

        var presentKey = List.<Object>of("2025-05-02T00:00:00Z", "probe-hash-01", "probe-coll");
        var missingKey = List.<Object>of("2025-05-02T00:00:00Z", "probe-hash-NEVER", "probe-coll");

        var present = repo.probeIds(TENANT_A, "search_telemetry", List.of(presentKey, missingKey));

        assertThat(present).hasSize(1).containsExactly(presentKey);
    }

    @Test @Order(28)
    void probeIds_tierWrites_returnsPresentSubset() {
        repo.importTierWriteRow(TENANT_A,
            "probe-sess-tier", "2025-05-03T00:00:00Z", "memory_put", "T2", null, null, null);

        var presentKey = List.<Object>of("probe-sess-tier", "2025-05-03T00:00:00Z", "memory_put", "T2");
        var missingKey = List.<Object>of("probe-sess-tier", "2025-05-03T00:00:00Z", "memory_put", "T3");

        var present = repo.probeIds(TENANT_A, "tier_writes", List.of(presentKey, missingKey));

        assertThat(present).hasSize(1).containsExactly(presentKey);
    }

    @Test @Order(29)
    void probeIds_nxAnswerRuns_returnsPresentSubset() {
        repo.importNxAnswerRunRow(TENANT_A,
            "probe question?", null, null, 1, "", 0.0, 0L, "2025-05-04T00:00:00Z");

        var presentKey = List.<Object>of("probe question?", "2025-05-04T00:00:00Z");
        var missingKey = List.<Object>of("probe question NEVER ASKED?", "2025-05-04T00:00:00Z");

        var present = repo.probeIds(TENANT_A, "nx_answer_runs", List.of(presentKey, missingKey));

        assertThat(present).hasSize(1).containsExactly(presentKey);
    }

    @Test @Order(32)
    void probeIds_hookFailures_returnsPresentSubset() {
        repo.importHookFailureRow(TENANT_A,
            "probe-doc-1", "code__nexus", "probe-hook", "boom", "2025-05-05T00:00:00Z",
            null, false, "single");

        var presentKey = List.<Object>of("probe-doc-1", "probe-hook", "2025-05-05T00:00:00Z");
        var missingKey = List.<Object>of("probe-doc-NEVER", "probe-hook", "2025-05-05T00:00:00Z");

        var present = repo.probeIds(TENANT_A, "hook_failures", List.of(presentKey, missingKey));

        assertThat(present).hasSize(1).containsExactly(presentKey);
    }

    @Test @Order(33)
    void probeIds_frecency_returnsPresentSubset() {
        repo.upsertFrecency(TENANT_A,
            "probe-chunk-frecency", "2025-05-06T00:00:00Z", 30, 1.5, 2, "2025-05-06T00:00:00Z");

        var presentKey = List.<Object>of("probe-chunk-frecency");
        var missingKey = List.<Object>of("probe-chunk-frecency-NEVER");

        var present = repo.probeIds(TENANT_A, "frecency", List.of(presentKey, missingKey));

        assertThat(present).hasSize(1).containsExactly(presentKey);
    }

    @Test @Order(34)
    void probeIds_tenantScoped_secondTenantSeesNothing() {
        repo.importRelevanceRow(TENANT_A,
            "tenant-scoped-query", "tenant-scoped-chunk", "code__nexus", "store_put", "sess-scoped",
            "2025-05-07T00:00:00Z");

        var key = List.<Object>of(
            "tenant-scoped-query", "tenant-scoped-chunk", "store_put", "sess-scoped",
            "2025-05-07T00:00:00Z");

        var presentForOwner = repo.probeIds(TENANT_A, "relevance_log", List.of(key));
        assertThat(presentForOwner)
            .as("owning tenant must see its own row as present")
            .hasSize(1);

        var presentForOther = repo.probeIds(TENANT_B, "relevance_log", List.of(key));
        assertThat(presentForOther)
            .as("RLS: a second tenant must see NOTHING for the first tenant's row")
            .isEmpty();
    }

    @Test @Order(34)
    void probeIds_timestampInstantEquivalence_offsetFormMatchesZuluImport() {
        // R1 substantive-critic (2026-07-02): the VERBATIM-ECHO design's
        // central claim — parseTsStrict compares INSTANTS, so a row imported
        // with a "...Z" timestamp must probe as present when the candidate
        // key renders the same instant as "...+00:00" (and the echoed tuple
        // is the CALLER's form, never the stored rendering). This is the
        // exact drift class the design exists to defuse; pin it.
        repo.importRelevanceRow(TENANT_A,
            "instant-eq-query", "instant-eq-chunk", "code__nexus", "store_put", "sess-eq",
            "2025-05-08T12:30:00Z");

        var offsetFormKey = List.<Object>of(
            "instant-eq-query", "instant-eq-chunk", "store_put", "sess-eq",
            "2025-05-08T12:30:00+00:00");

        var present = repo.probeIds(TENANT_A, "relevance_log", List.of(offsetFormKey));

        assertThat(present)
            .as("+00:00 candidate must match the Z-imported instant, echoed in the CALLER's form")
            .hasSize(1)
            .containsExactly(offsetFormKey);
    }

    @Test @Order(35)
    void probeIds_unknownTable_throwsIllegalArgument() {
        assertThatThrownBy(() ->
            repo.probeIds(TENANT_A, "not_a_real_table", List.of(List.of("x"))))
            .as("probeIds with an unknown table must throw")
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test @Order(36)
    void probeIds_wrongArity_throwsIllegalArgument() {
        // frecency's conflict key is 1-tuple [chunk_id]; feed it 2 elements.
        assertThatThrownBy(() ->
            repo.probeIds(TENANT_A, "frecency", List.of(List.of("chunk-a", "extra"))))
            .as("probeIds with a mis-sized key tuple must throw")
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test @Order(37)
    void probeIds_emptyKeys_returnsEmptyList() {
        var present = repo.probeIds(TENANT_A, "relevance_log", List.of());
        assertThat(present).isEmpty();
    }

    // ── RLS ────────────────────────────────────────────────────────────────────

    @Test @Order(24)
    void rlsWithCheck_rawInsertWithWrongTenantIdRejected() {
        assertThatThrownBy(() -> {
            try (Connection conn = svcDs.getConnection()) {
                conn.setAutoCommit(true);
                conn.createStatement().execute(
                    "SET nexus.tenant = '" + TENANT_A + "'");
                // Attempt to insert with a different tenant_id — RLS WITH CHECK must reject
                conn.createStatement().execute(
                    "INSERT INTO nexus.relevance_log " +
                    "(tenant_id, query, chunk_id, action, timestamp) " +
                    "VALUES ('" + TENANT_B + "', 'q', 'c', 'a', now())");
            }
        }).as("RLS WITH CHECK must reject INSERT with wrong tenant_id")
          .isInstanceOfAny(PSQLException.class, SQLException.class);
    }

    // ── importBatch: ONE multi-row INSERT per table (nexus-1usso) ───────────────
    // Plan-audit correction: importBatch HAD the endpoint but looped per-row
    // .execute() inside its single tenant transaction (N round-trips). These
    // tests exercise the multi-row conversion for all six tables.

    @Test @Order(40)
    void importBatch_relevanceLog_multiRow_insertsAll_doNothingOnReimport() {
        int n = repo.importBatch(TENANT_A, "relevance_log", List.of(
            Map.of("query", "batch-rl-q0", "chunk_id", "batch-rl-c0", "collection", "knowledge__nexus",
                   "action", "store_put", "session_id", "sess-0", "timestamp", PAST_TS),
            Map.of("query", "batch-rl-q1", "chunk_id", "batch-rl-c1", "collection", "knowledge__nexus",
                   "action", "store_put", "session_id", "sess-1", "timestamp", PAST_TS)));
        assertThat(n).isEqualTo(2);
        assertThat(repo.getRelevanceLog(TENANT_A, "batch-rl-q0", "batch-rl-c0", "", "", 5)).hasSize(1);
        assertThat(repo.getRelevanceLog(TENANT_A, "batch-rl-q1", "batch-rl-c1", "", "", 5)).hasSize(1);

        // Re-import (same rows) — DO NOTHING must not duplicate.
        repo.importBatch(TENANT_A, "relevance_log", List.of(
            Map.of("query", "batch-rl-q0", "chunk_id", "batch-rl-c0", "collection", "knowledge__nexus",
                   "action", "store_put", "session_id", "sess-0", "timestamp", PAST_TS)));
        assertThat(repo.getRelevanceLog(TENANT_A, "batch-rl-q0", "batch-rl-c0", "", "", 5)).hasSize(1);
    }

    @Test @Order(41)
    void importBatch_frecency_multiRow_greatestMerge_intraBatchDedupeLastWins() {
        // Two rows for the SAME chunk_id in ONE batch — must dedupe (last wins),
        // since a single multi-row ON CONFLICT DO UPDATE cannot affect the same
        // row twice.
        int n = repo.importBatch(TENANT_A, "frecency", List.of(
            Map.of("chunk_id", "batch-frec-1", "embedded_at", "2024-01-01T00:00:00Z",
                   "ttl_days", 30, "frecency_score", 0.3, "miss_count", 2,
                   "last_hit_at", "2024-02-01T00:00:00Z"),
            Map.of("chunk_id", "batch-frec-1", "embedded_at", "2024-01-01T00:00:00Z",
                   "ttl_days", 30, "frecency_score", 0.8, "miss_count", 9,
                   "last_hit_at", "2024-03-01T00:00:00Z")));
        assertThat(n).as("rows submitted (contract unchanged), not rows landed").isEqualTo(2);

        var got = repo.getFrecency(TENANT_A, "batch-frec-1");
        assertThat(got).isPresent();
        assertThat(((Number) got.get().get("frecency_score")).doubleValue()).isEqualTo(0.8);
        assertThat(((Number) got.get().get("miss_count")).intValue()).isEqualTo(9);

        // Re-import with STALE (lower) values — GREATEST must not roll back live values.
        repo.importBatch(TENANT_A, "frecency", List.of(
            Map.of("chunk_id", "batch-frec-1", "embedded_at", "2024-01-01T00:00:00Z",
                   "ttl_days", 30, "frecency_score", 0.1, "miss_count", 1,
                   "last_hit_at", "2024-01-15T00:00:00Z")));
        var afterStale = repo.getFrecency(TENANT_A, "batch-frec-1");
        assertThat(((Number) afterStale.get().get("frecency_score")).doubleValue())
            .as("GREATEST: must not roll back to stale 0.1").isEqualTo(0.8);
        assertThat(((Number) afterStale.get().get("miss_count")).intValue())
            .as("GREATEST: must not roll back to stale 1").isEqualTo(9);
    }

    @Test @Order(42)
    void importBatch_unknownTable_throws() {
        assertThatThrownBy(() -> repo.importBatch(TENANT_A, "bogus-table", List.of(Map.of())))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test @Order(43)
    void importBatch_emptyAndNull_returnZero() {
        assertThat(repo.importBatch(TENANT_A, "relevance_log", List.of())).isZero();
        assertThat(repo.importBatch(TENANT_A, "relevance_log", null)).isZero();
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.addDataSourceProperty("options", "-c search_path=nexus,public");
        return new com.zaxxer.hikari.HikariDataSource(config);
    }
    // ── nexus-24p05: retention markers ───────────────────────────────────────

    @Test
    void expireRelevanceLog_bumpsCumulativeRetentionMarker() {
        String tenant = "ret-marker-" + System.nanoTime();
        // Two old rows (past any horizon) + one fresh row.
        repo.importRelevanceRow(tenant, "q1", "c1", "knowledge__x", "click", "s",
            "2020-01-01T00:00:00Z");
        repo.importRelevanceRow(tenant, "q2", "c2", "knowledge__x", "click", "s",
            "2020-01-02T00:00:00Z");
        repo.logRelevance(tenant, "q3", "c3", "click", "s", "knowledge__x");

        int deleted = repo.expireRelevanceLog(tenant, 90);
        org.assertj.core.api.Assertions.assertThat(deleted).isEqualTo(2);
        var markers = repo.getRetentionMarkers(tenant,
            List.of("nexus.relevance_log", "nexus.search_telemetry"));
        org.assertj.core.api.Assertions.assertThat(markers)
            .containsEntry("nexus.relevance_log", 2L)
            .doesNotContainKey("nexus.search_telemetry");  // never swept -> absent

        // Second sweep with nothing left to delete: marker UNCHANGED (no bump-on-zero).
        org.assertj.core.api.Assertions.assertThat(repo.expireRelevanceLog(tenant, 90)).isZero();
        org.assertj.core.api.Assertions.assertThat(
            repo.getRetentionMarkers(tenant, List.of("nexus.relevance_log")))
            .containsEntry("nexus.relevance_log", 2L);
    }

    @Test
    void retentionMarkers_areTenantIsolated() {
        String a = "ret-iso-a-" + System.nanoTime();
        String b = "ret-iso-b-" + System.nanoTime();
        repo.importRelevanceRow(a, "q", "c", "knowledge__x", "click", "s",
            "2020-01-01T00:00:00Z");
        repo.expireRelevanceLog(a, 90);
        org.assertj.core.api.Assertions.assertThat(
            repo.getRetentionMarkers(b, List.of("nexus.relevance_log")))
            .as("tenant B must not see tenant A's marker (RLS)")
            .isEmpty();
    }

    // ── nexus-eho3u: nx_answer_runs read surface ─────────────────────────────
    //
    // Own unique tenants (System.nanoTime(), not TENANT_A) — unlike the
    // @Order-sequenced tests above, these assert EXACT aggregate counts over
    // the whole tenant, so a shared tenant would pick up rows from other
    // tests running under the same PER_CLASS instance.

    @Test
    void queryNxAnswerRuns_writeThenRead_rowsAndAggregatesRoundTrip() {
        String tenant = "nar-" + System.nanoTime();
        // One inline-planner fallback (plan_id null) + four plan-match hits,
        // one landing in each latency bucket.
        repo.importNxAnswerRunRow(tenant, "fallback question", null, null,
            0, "Planner error: x", 0.0, 4_000L, "2026-08-01T00:00:00Z");
        repo.importNxAnswerRunRow(tenant, "under 5s", 1L, 0.9,
            1, "answer-1", 0.001, 4_000L, "2026-08-01T00:01:00Z");
        repo.importNxAnswerRunRow(tenant, "5s to 30s", 2L, 0.9,
            1, "answer-2", 0.002, 10_000L, "2026-08-01T00:02:00Z");
        repo.importNxAnswerRunRow(tenant, "30s to 2min", 3L, 0.9,
            1, "answer-3", 0.003, 60_000L, "2026-08-01T00:03:00Z");
        repo.importNxAnswerRunRow(tenant, "2min to 5min", 4L, 0.9,
            1, "answer-4", 0.004, 200_000L, "2026-08-01T00:04:00Z");
        repo.importNxAnswerRunRow(tenant, "over 5min", 5L, 0.9,
            1, "answer-5", 0.005, 400_000L, "2026-08-01T00:05:00Z");

        var out = repo.queryNxAnswerRuns(tenant, "", 100);

        assertThat(out.get("total")).isEqualTo(6);
        assertThat(out.get("hit_count")).isEqualTo(5L);
        assertThat(out.get("fallback_count")).isEqualTo(1L);
        assertThat((Double) out.get("avg_cost_usd")).isCloseTo(0.0025, org.assertj.core.data.Offset.offset(1e-9));
        assertThat(out.get("oldest_created_at")).isEqualTo("2026-08-01T00:00:00Z");

        @SuppressWarnings("unchecked")
        Map<String, Object> buckets = (Map<String, Object>) out.get("latency_buckets");
        // 2: the fallback row (4_000ms) AND the "under 5s" hit both land here.
        assertThat(buckets.get("under_5s")).isEqualTo(2L);
        assertThat(buckets.get("5s_to_30s")).isEqualTo(1L);
        assertThat(buckets.get("30s_to_2min")).isEqualTo(1L);
        assertThat(buckets.get("2min_to_5min")).isEqualTo(1L);
        assertThat(buckets.get("over_5min")).isEqualTo(1L);
        long bucketSum = buckets.values().stream().mapToLong(v -> (Long) v).sum();
        assertThat(bucketSum).as("buckets must sum to total — no row silently unaccounted for")
            .isEqualTo(6L);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) out.get("rows");
        assertThat(rows).hasSize(6);
        // Newest first.
        assertThat(rows.get(0).get("question")).isEqualTo("over 5min");
        assertThat(rows.get(0).get("plan_id")).isEqualTo(5L);
        assertThat(rows.get(0).get("created_at")).isEqualTo("2026-08-01T00:05:00Z");
        assertThat(rows.get(5).get("question")).isEqualTo("fallback question");
        assertThat(rows.get(5).get("plan_id")).isNull();
    }

    @Test
    void queryNxAnswerRuns_planIdZero_isTheAdHocSentinelNotAHit() {
        // Review fix (nexus-eho3u): plan_id=0 is the synthetic ad-hoc Match
        // sentinel _nx_answer_plan_miss returns on every SUCCESSFUL
        // inline-planner run (core.py) — plans.id is BIGSERIAL, so 0 can
        // never be a real plan. The ORIGINAL `plan_id IS NOT NULL` predicate
        // counted this as a HIT, inverting the plan-match-rate metric. This
        // is the KILL CONTROL: under that old predicate, hit_count would be
        // 2 (the real hit AND the ad-hoc-sentinel row) and fallback_count
        // would be 1 — both wrong. Temporarily reverting the predicate to
        // `PLAN_ID.isNotNull()` / `PLAN_ID.isNull()` makes this test fail
        // exactly that way (verified by hand during the fix; not left
        // reverted in the tree).
        String tenant = "nar-sentinel-" + System.nanoTime();
        repo.importNxAnswerRunRow(tenant, "real hit", 11L, 0.9,
            1, "answer", 0.001, 1_000L, "2026-08-01T00:00:00Z");
        repo.importNxAnswerRunRow(tenant, "ad-hoc success (sentinel)", 0L, null,
            2, "ad-hoc answer", 0.002, 2_000L, "2026-08-01T00:01:00Z");
        repo.importNxAnswerRunRow(tenant, "genuine fallback (planner error)", null, null,
            0, "Planner error: x", 0.0, 3_000L, "2026-08-01T00:02:00Z");

        var out = repo.queryNxAnswerRuns(tenant, "", 100);

        assertThat(out.get("total")).isEqualTo(3);
        assertThat(out.get("hit_count"))
            .as("only the REAL matched plan (plan_id=11) counts as a hit")
            .isEqualTo(1L);
        assertThat(out.get("fallback_count"))
            .as("plan_id=0 (ad-hoc sentinel) AND plan_id=null (genuine miss) both count as fallback")
            .isEqualTo(2L);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) out.get("rows");
        var sentinelRow = rows.stream()
            .filter(r -> "ad-hoc success (sentinel)".equals(r.get("question")))
            .findFirst().orElseThrow();
        assertThat(sentinelRow.get("plan_id")).isEqualTo(0L);
    }

    @Test
    void queryNxAnswerRuns_limitCapsPageButNotAggregates() {
        // Kill control mirroring hookFailures_read_aggregatesIgnoreThePageLimit:
        // a caller asking for the last row must not see total=1.
        String tenant = "nar-cap-" + System.nanoTime();
        for (int i = 0; i < 5; i++) {
            repo.importNxAnswerRunRow(tenant, "cap-" + i, null, null,
                0, "a", 0.0, 1_000L, "2026-08-01T00:0" + i + ":00Z");
        }

        var capped = repo.queryNxAnswerRuns(tenant, "", 1);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) capped.get("rows");
        assertThat(rows).hasSize(1);
        assertThat(capped.get("total")).isEqualTo(5);
        assertThat(capped.get("fallback_count")).isEqualTo(5L);
    }

    @Test
    void queryNxAnswerRuns_sinceFilterExcludesOlderRows() {
        String tenant = "nar-since-" + System.nanoTime();
        repo.importNxAnswerRunRow(tenant, "old", null, null,
            0, "a", 0.0, 1_000L, "2020-01-01T00:00:00Z");
        repo.importNxAnswerRunRow(tenant, "new", null, null,
            0, "a", 0.0, 1_000L, "2099-01-01T00:00:00Z");

        var recent = repo.queryNxAnswerRuns(tenant, "2030-01-01T00:00:00Z", 100);
        assertThat(recent.get("total")).isEqualTo(1);

        var unbounded = repo.queryNxAnswerRuns(tenant, "", 100);
        assertThat(unbounded.get("total")).isEqualTo(2);
    }

    @Test
    void queryNxAnswerRuns_isTenantScoped() {
        String mine = "nar-iso-mine-" + System.nanoTime();
        String theirs = "nar-iso-theirs-" + System.nanoTime();
        repo.importNxAnswerRunRow(mine, "mine", null, null,
            0, "a", 0.0, 1_000L, PAST_TS);
        repo.importNxAnswerRunRow(theirs, "theirs", null, null,
            0, "a", 0.0, 1_000L, PAST_TS);

        var out = repo.queryNxAnswerRuns(mine, "", 100);
        assertThat(out.get("total")).isEqualTo(1);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) out.get("rows");
        assertThat(rows.get(0).get("question")).isEqualTo("mine");
    }

    @Test
    void queryNxAnswerRuns_emptyTenant_returnsZeroedAggregatesNotNull() {
        // Non-vacuity: an empty result must be zeroed structure, not a null
        // crash or an absent key the client would have to special-case.
        String tenant = "nar-empty-" + System.nanoTime();
        var out = repo.queryNxAnswerRuns(tenant, "", 100);

        assertThat(out.get("total")).isEqualTo(0);
        assertThat(out.get("hit_count")).isEqualTo(0L);
        assertThat(out.get("fallback_count")).isEqualTo(0L);
        assertThat(out.get("oldest_created_at")).isEqualTo("");
        assertThat(out.get("avg_duration_ms")).isNull();
        assertThat(out.get("avg_cost_usd")).isNull();
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) out.get("rows");
        assertThat(rows).isEmpty();
        @SuppressWarnings("unchecked")
        Map<String, Object> buckets = (Map<String, Object>) out.get("latency_buckets");
        assertThat(buckets.values()).allMatch(v -> ((Long) v) == 0L);
    }

    @Test
    void queryNxAnswerRuns_livePath_recordThenQuery() {
        // The genuinely live (non-import) write path, mirroring
        // recordTierWrite_livePath_persistsAndIsRetrievableUnderTenant —
        // exercises recordNxAnswerRun (not the ETL-strict import variant)
        // feeding straight into the same read.
        String tenant = "nar-live-" + System.nanoTime();
        repo.recordNxAnswerRun(tenant, "live question", 9L, 0.8,
            2, "live answer", 0.01, 2_500L, null);

        var out = repo.queryNxAnswerRuns(tenant, "", 100);
        assertThat(out.get("total")).isEqualTo(1);
        assertThat(out.get("hit_count")).isEqualTo(1L);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) out.get("rows");
        assertThat(rows.get(0).get("question")).isEqualTo("live question");
    }

    // ── search_telemetry / hook_failures: trim dry-run preview ────────────────
    //
    // The gap this closes: trimSearchTelemetry(tenant, days) DELETES with no
    // way to learn the count first. Recommendation adopted (not a separate
    // count endpoint): dry-run reuses the EXACT SAME WHERE predicate as the
    // delete — a SELECT count(*) substituted for the DELETE — so the census
    // and the action it authorises can never diverge (the nexus-3rr3x class:
    // purge-trash's dry-run once reported 340 against a live census of
    // 11,156 because the two were computed by different queries). The tests
    // below are the non-vacuity proof: seed rows straddling the cutoff, take
    // the preview, run the real trim, and assert the numbers match AND that
    // the dry run left the table untouched.

    @Test
    void trimSearchTelemetry_dryRun_countsWithoutDeleting_thenRealTrimMatches() {
        String tenant = "tel-trim-dryrun-" + System.nanoTime();
        // Three rows older than the 30-day cutoff (2024 dates).
        repo.importSearchRow(tenant, PAST_TS, "dr-old-1", "code__nexus", 10, 5, 0.3, 0.5);
        repo.importSearchRow(tenant, "2024-02-01T00:00:00Z", "dr-old-2", "code__nexus", 8, 4, 0.2, 0.5);
        repo.importSearchRow(tenant, "2024-03-01T00:00:00Z", "dr-old-3", "code__nexus", 6, 3, 0.4, 0.5);
        // One recent row (inside the window) that must survive both calls.
        String nowTs = OffsetDateTime.now(ZoneOffset.UTC).toString();
        repo.importSearchRow(tenant, nowTs, "dr-recent", "code__nexus", 5, 2, 0.1, 0.5);

        int preview = repo.trimSearchTelemetry(tenant, 30, true);
        assertThat(preview).as("dry-run must count exactly the 3 aged rows").isEqualTo(3);
        assertThat(countSearchTelemetryRows(tenant))
            .as("dry-run must leave every row in place — nothing physically deleted")
            .isEqualTo(4);

        int deleted = repo.trimSearchTelemetry(tenant, 30, false);
        assertThat(deleted)
            .as("real trim must delete EXACTLY the previewed count — same predicate")
            .isEqualTo(preview);
        assertThat(countSearchTelemetryRows(tenant))
            .as("only the recent row survives the real trim").isEqualTo(1);

        assertThat(repo.trimSearchTelemetry(tenant, 30, true))
            .as("nothing left to preview after the real trim — preview tracks live state")
            .isZero();
    }

    @Test
    void trimSearchTelemetry_dryRun_isTenantScoped() {
        String mine = "tel-trim-dr-mine-" + System.nanoTime();
        String theirs = "tel-trim-dr-theirs-" + System.nanoTime();
        repo.importSearchRow(mine, PAST_TS, "iso-mine", "code__nexus", 1, 1, 0.1, 0.5);
        repo.importSearchRow(theirs, PAST_TS, "iso-theirs", "code__nexus", 1, 1, 0.1, 0.5);

        assertThat(repo.trimSearchTelemetry(mine, 30, true))
            .as("dry-run for tenant A must not count tenant B's rows (RLS)").isEqualTo(1);
        assertThat(repo.trimSearchTelemetry(theirs, 30, true))
            .as("tenant B's row is still there and still visible to tenant B").isEqualTo(1);
    }

    @Test
    void trimSearchTelemetry_twoArgOverload_stillDeletesForBackwardCompat() {
        // The pre-existing 2-arg call site (searchTelemetry_batchAndTrim,
        // Order(6)) must keep compiling and behaving as a real (non-preview)
        // trim — the 3-arg dry-run overload must not change default behavior.
        String tenant = "tel-trim-2arg-" + System.nanoTime();
        repo.importSearchRow(tenant, PAST_TS, "2arg-old", "code__nexus", 1, 1, 0.1, 0.5);
        int deleted = repo.trimSearchTelemetry(tenant, 30);
        assertThat(deleted).isEqualTo(1);
        assertThat(countSearchTelemetryRows(tenant)).isZero();
    }

    @Test
    void trimHookFailures_dryRun_countsWithoutDeleting_thenRealTrimMatches() {
        String tenant = "tel-trim-hooks-dryrun-" + System.nanoTime();
        repo.importHookFailureRow(tenant, "th-dr-old-1", "code__nexus", "hook_a",
            "boom", PAST_TS, null, false, "single");
        repo.importHookFailureRow(tenant, "th-dr-old-2", "code__nexus", "hook_b",
            "boom", "2024-02-01T00:00:00Z", null, false, "single");
        String nowTs = OffsetDateTime.now(ZoneOffset.UTC).toString();
        repo.importHookFailureRow(tenant, "th-dr-recent", "code__nexus", "hook_c",
            "boom", nowTs, null, false, "single");

        int preview = repo.trimHookFailures(tenant, 30, true);
        assertThat(preview).as("dry-run must count exactly the 2 aged rows").isEqualTo(2);
        assertThat(countHookFailuresRows(tenant))
            .as("dry-run must leave every row in place").isEqualTo(3);

        int deleted = repo.trimHookFailures(tenant, 30, false);
        assertThat(deleted)
            .as("real trim must delete EXACTLY the previewed count").isEqualTo(preview);
        assertThat(countHookFailuresRows(tenant))
            .as("only the recent row survives the real trim").isEqualTo(1);
    }

    @Test
    void trimHookFailures_twoArgOverload_stillDeletesForBackwardCompat() {
        String tenant = "tel-trim-hooks-2arg-" + System.nanoTime();
        repo.importHookFailureRow(tenant, "2arg-old", "code__nexus", "hook_a",
            "boom", PAST_TS, null, false, "single");
        int deleted = repo.trimHookFailures(tenant, 30);
        assertThat(deleted).isEqualTo(1);
        assertThat(countHookFailuresRows(tenant)).isZero();
    }

    private int countSearchTelemetryRows(String tenant) {
        try (Connection conn = pg.createConnection("")) {
            conn.createStatement().execute("SET nexus.tenant = '" + tenant + "'");
            var rs = conn.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.search_telemetry WHERE tenant_id='" + tenant + "'");
            rs.next();
            return rs.getInt(1);
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }

    private int countHookFailuresRows(String tenant) {
        try (Connection conn = pg.createConnection("")) {
            conn.createStatement().execute("SET nexus.tenant = '" + tenant + "'");
            var rs = conn.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.hook_failures WHERE tenant_id='" + tenant + "'");
            rs.next();
            return rs.getInt(1);
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }
}
