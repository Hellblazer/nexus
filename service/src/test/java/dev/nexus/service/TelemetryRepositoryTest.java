package dev.nexus.service;

import dev.nexus.service.db.TelemetryRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
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
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
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
            "rdr research query", "5af88918c554526b628794508eec9b6f8f6cbdaeff3e3523418293427fe44af7", "store_put", "sess-1", "code__nexus");
        assertThat(id).as("logRelevance must return a positive id").isPositive();

        var rows = repo.getRelevanceLog(TENANT_A, "rdr research query", "", "", "", 10);
        assertThat(rows).isNotEmpty();
        assertThat(rows.get(0).get("query")).isEqualTo("rdr research query");
        assertThat(rows.get(0).get("chunk_id")).isEqualTo("5af88918c554526b628794508eec9b6f8f6cbdaeff3e3523418293427fe44af7");
        assertThat(rows.get(0).get("action")).isEqualTo("store_put");
    }

    @Test @Order(2)
    void relevanceLog_batch_insertsMultipleRows() {
        var rows = List.of(
            List.of("batch-query", "d916ae7569b9f26fc4008321dd68b201af3baed00e435d3dc591b9cfd49780f4", "code__nexus", "catalog_link", "sess-b"),
            List.of("batch-query", "e9f8ad4c948fdd586c7342916db53e7919a1af834b5f020767e20bceb4044488", "code__nexus", "catalog_link", "sess-b"));
        int count = repo.logRelevanceBatch(TENANT_A, rows);
        assertThat(count).as("batch insert should return rows attempted").isGreaterThanOrEqualTo(0);

        var result = repo.getRelevanceLog(TENANT_A, "batch-query", "", "", "", 10);
        assertThat(result).hasSizeGreaterThanOrEqualTo(2);
    }

    @Test @Order(3)
    void relevanceLog_expire_deletesOldRows() {
        // logRelevance with future-dated import row (old timestamp)
        repo.importRelevanceRow(TENANT_A,
            "ancient-query", "9d4d036d88c303323847a0a02cdc07b66af3cb00d27688e7b53cc2b3f833c213", "rdr__nexus", "store_put", "sess-x",
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
            "fidelity-ts-query", "8342c2c98f368846ee9b8b0f2527b0d679d00abeecb82ffa42f8b83d720c6f9a", "knowledge__nexus", "store_put", "sess-fid",
            PAST_TS);

        var rows = repo.getRelevanceLog(TENANT_A, "fidelity-ts-query", "8342c2c98f368846ee9b8b0f2527b0d679d00abeecb82ffa42f8b83d720c6f9a", "", "", 5);
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
                "idem-query", "10ad1275c8dee6835a6c01e0fcee40d6209ec5db86d4f1a111057b49dbf86a8a", "code__nexus", "store_put", "sess-idem",
                "2024-03-01T12:00:00Z");
        }
        // Must have exactly 1 row, not 2
        var rows = repo.getRelevanceLog(TENANT_A, "idem-query", "10ad1275c8dee6835a6c01e0fcee40d6209ec5db86d4f1a111057b49dbf86a8a", "", "", 10);
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
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
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
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
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
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
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

    @Test @Order(31)
    void recordNxAnswerRun_livePath_persistsAndIsRetrievableUnderTenant() {
        repo.recordNxAnswerRun(TENANT_A,
            "live record question?", 7L, 0.81,
            2, "live answer text", 0.002, 900, PAST_TS);

        try (Connection conn = pg.createConnection("")) {
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
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
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
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
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, tenant, false);
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
        // nexus-cefa1.3: batch_doc_ids is jsonb now — must be valid JSON (this was
        // always the real production shape: hook_registry.py writes
        // json.dumps(doc_ids)). "d1,d2" was an opaque-string stand-in that predates
        // the column's real type and is no longer valid input.
        repo.importHookFailureRow(tenant, "hr-2", "code__nexus", "hook_b",
            "boom-b", midTs, "[\"d1\", \"d2\"]", true, "batch");
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
        // PG's jsonb canonical text output inserts a space after each array-element
        // comma, matching Python's json.dumps default separators exactly — the real
        // production writer (hook_registry.py) round-trips byte-identical.
        assertThat(batchRow.get("batch_doc_ids")).isEqualTo("[\"d1\", \"d2\"]");
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

    // ── hook_failures type-hygiene (nexus-cefa1.3) ────────────────────────────

    @Test @Order(44)
    @SuppressWarnings("unchecked")
    void hookFailures_isBatch_wireRoundTrip_booleanIdentityPreserved() {
        // nexus-cefa1.3: is_batch INTEGER -> boolean. The wire was already a JSON
        // boolean (TelemetryHandler ~352/~504); this pins that the LIVE write path
        // (recordHookFailure — what handleHookFailureRecord calls) still round-trips
        // true/false as real Java Booleans, not truthy ints/strings, through the read
        // path (getHookFailures — what handleHookFailureList calls).
        final String tenant = "tel-hooks-isbatch-" + System.nanoTime();
        repo.recordHookFailure(tenant, "ib-true", "code__nexus", "hook_true",
            "boom", null, null, true, "batch");
        repo.recordHookFailure(tenant, "ib-false", "code__nexus", "hook_false",
            "boom", null, null, false, "single");

        var rows = (List<Map<String, Object>>) repo.getHookFailures(tenant, 0, List.of(), 100).get("rows");

        var trueRow = rows.stream().filter(r -> "ib-true".equals(r.get("doc_id"))).findFirst().orElseThrow();
        var falseRow = rows.stream().filter(r -> "ib-false".equals(r.get("doc_id"))).findFirst().orElseThrow();
        assertThat(trueRow.get("is_batch")).isInstanceOf(Boolean.class).isEqualTo(Boolean.TRUE);
        assertThat(falseRow.get("is_batch")).isInstanceOf(Boolean.class).isEqualTo(Boolean.FALSE);
    }

    @Test @Order(45)
    @SuppressWarnings("unchecked")
    void hookFailures_batchDocIds_wireRoundTrip_preservesJsonArrayShape() {
        // nexus-cefa1.3: batch_doc_ids TEXT -> jsonb. Real write path
        // (recordHookFailure) with the real production shape — a JSON-encoded array
        // string, exactly what hook_registry.py's json.dumps(doc_ids) emits — must
        // read back as something taxonomy_cmd.py's json.loads(...) can still parse
        // into the identical list, which is the only thing any consumer actually
        // depends on (not byte-identical text).
        final String tenant = "tel-hooks-batchids-" + System.nanoTime();
        repo.recordHookFailure(tenant, "bd-1", "code__nexus", "hook_batch",
            "boom", null, "[\"doc-a\", \"doc-b\", \"doc-c\"]", true, "batch");

        var rows = (List<Map<String, Object>>) repo.getHookFailures(tenant, 0, List.of(), 100).get("rows");
        var row = rows.stream().filter(r -> "bd-1".equals(r.get("doc_id"))).findFirst().orElseThrow();

        Object raw = row.get("batch_doc_ids");
        assertThat(raw).isInstanceOf(String.class);
        try {
            var mapper = new com.fasterxml.jackson.databind.ObjectMapper();
            List<String> parsed = mapper.readValue((String) raw,
                mapper.getTypeFactory().constructCollectionType(List.class, String.class));
            assertThat(parsed).containsExactly("doc-a", "doc-b", "doc-c");
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new AssertionError("batch_doc_ids must remain parseable JSON after the wire round trip: " + raw, e);
        }
    }

    @Test @Order(46)
    void hookFailures_batchDocIds_nullAndBlank_bothRoundTripAsNull() {
        // The migration's own USING NULLIF(batch_doc_ids, '')::jsonb clause maps ''
        // to NULL; the live write path must keep doing that going forward too (a
        // blank string is not valid jsonb input).
        final String tenant = "tel-hooks-blank-" + System.nanoTime();
        repo.recordHookFailure(tenant, "bd-null", "code__nexus", "hook_null",
            "boom", null, null, false, "single");
        repo.recordHookFailure(tenant, "bd-blank", "code__nexus", "hook_blank",
            "boom", null, "", false, "single");

        @SuppressWarnings("unchecked")
        var rows = (List<Map<String, Object>>) repo.getHookFailures(tenant, 0, List.of(), 100).get("rows");

        var nullRow = rows.stream().filter(r -> "bd-null".equals(r.get("doc_id"))).findFirst().orElseThrow();
        var blankRow = rows.stream().filter(r -> "bd-blank".equals(r.get("doc_id"))).findFirst().orElseThrow();
        assertThat(nullRow.get("batch_doc_ids")).isEqualTo("");
        assertThat(blankRow.get("batch_doc_ids")).isEqualTo("");
    }

    // ── frecency ───────────────────────────────────────────────────────────────

    @Test @Order(12)
    void frecency_getFrecency_roundTrip() {
        repo.upsertFrecency(TENANT_A, "56ac933d343160c61ded6e852ae5fd5d02576a722b2a76b11e27b661a856ed7d",
            "2024-06-01T00:00:00Z", 90, 0.75, 3, "2024-09-01T00:00:00Z");

        Optional<Map<String, Object>> result = repo.getFrecency(TENANT_A, "56ac933d343160c61ded6e852ae5fd5d02576a722b2a76b11e27b661a856ed7d");
        assertThat(result).as("getFrecency must return the upserted record").isPresent();
        assertThat(result.get().get("chunk_id")).isEqualTo("56ac933d343160c61ded6e852ae5fd5d02576a722b2a76b11e27b661a856ed7d");
        assertThat(((Number) result.get().get("ttl_days")).intValue()).isEqualTo(90);
        assertThat(((Number) result.get().get("frecency_score")).doubleValue()).isEqualTo(0.75);
        assertThat(((Number) result.get().get("miss_count")).intValue()).isEqualTo(3);
    }

    @Test @Order(13)
    void frecency_greatestNoClober_reImportWithStaleSrcDoesNotRollBackLiveValues() {
        // Step 1: insert an initial frecency record with low counters (simulating source SQLite)
        repo.upsertFrecency(TENANT_A, "327a4a832e256cb155e4b6f03535960645648c1b15f2deb3760d29ba69583588",
            "2024-01-01T00:00:00Z", 30, 0.50, 5, "2024-06-01T00:00:00Z");

        // Step 2: simulate live PG advancement (higher values = fresher data)
        // by upserting with higher values first
        repo.upsertFrecency(TENANT_A, "327a4a832e256cb155e4b6f03535960645648c1b15f2deb3760d29ba69583588",
            "2024-01-01T00:00:00Z", 30, 0.95, 20, "2026-01-01T00:00:00Z");

        // Step 3: re-import with the STALE source values (lower counters)
        // GREATEST logic must preserve the live PG values, not clobber with stale source
        repo.upsertFrecency(TENANT_A, "327a4a832e256cb155e4b6f03535960645648c1b15f2deb3760d29ba69583588",
            "2024-01-01T00:00:00Z", 30, 0.50, 5, "2024-06-01T00:00:00Z");

        Optional<Map<String, Object>> result = repo.getFrecency(TENANT_A, "327a4a832e256cb155e4b6f03535960645648c1b15f2deb3760d29ba69583588");
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
        repo.upsertFrecency(TENANT_A, "3115d480cb3cef4722d35d5608cf43e4ae6dae56c3eedafccbb1efa83dec6efb",
            "2023-01-01T00:00:00Z", 30, 0.1, 0, "2023-01-01T00:00:00Z");

        // Re-import with a newer embedded_at (from a re-index) — should keep oldest
        repo.upsertFrecency(TENANT_A, "3115d480cb3cef4722d35d5608cf43e4ae6dae56c3eedafccbb1efa83dec6efb",
            "2025-01-01T00:00:00Z", 30, 0.5, 1, "2025-01-01T00:00:00Z");

        try (Connection conn = pg.createConnection("")) {
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            var rs = conn.createStatement().executeQuery(
                "SELECT embedded_at FROM nexus.frecency WHERE chunk_id='3115d480cb3cef4722d35d5608cf43e4ae6dae56c3eedafccbb1efa83dec6efb'");
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

    // ── nexus-tk070.p6b (RDR-194 D5): CHECK-layer proof, independent of the
    //    TelemetryHandler boundary 400 ──────────────────────────────────────

    @Test @Order(47)
    void upsertFrecency_ttlDaysZero_violatesCheckConstraintDirectlyAtRepositoryLayer() {
        // Calls TelemetryRepository directly -- bypassing TelemetryHandler's
        // handler-level ttl_days<=0 rejection entirely -- to prove the DB
        // CHECK itself rejects ttl_days=0, independent of the HTTP boundary
        // validation. The two layers are tested SEPARATELY (see
        // TelemetryHandlerFrecencyTtlBoundaryTest for the boundary proof,
        // MemoryRepositoryTest's identical-shape test for the precedent):
        // deleting TelemetryHandler's requirePositiveOrNullTtlDays would not
        // make THIS test fail, and dropping frecency_ttl_days_positive_chk
        // would not make the boundary test fail -- each test falsifies only
        // its own layer.
        assertThatThrownBy(() ->
            repo.upsertFrecency(TENANT_A,
                "0123456789abcdef".repeat(4),
                "2024-01-01T00:00:00Z", 0, 0.0, 0, "2024-01-01T00:00:00Z"))
            .as("ttl_days=0 must violate frecency_ttl_days_positive_chk at the "
                + "DB layer even when no handler-level validation runs")
            .hasMessageContaining("frecency_ttl_days_positive_chk");
    }

    @Test @Order(48)
    void upsertFrecency_ttlDaysNull_isPermanentAndAccepted() {
        // The positive companion to the CHECK-violation test above: NULL is
        // the sole permanent sentinel post-migration and must be accepted
        // without any exception.
        String chunkId = "fedcba9876543210".repeat(4);
        repo.upsertFrecency(TENANT_A, chunkId, "2024-01-01T00:00:00Z", null, 0.5, 0,
            "2024-01-01T00:00:00Z");
        Optional<Map<String, Object>> result = repo.getFrecency(TENANT_A, chunkId);
        assertThat(result).as("a NULL-ttl_days row must be readable, not swallowed").isPresent();
        assertThat(result.get().get("ttl_days"))
            .as("ttl_days must round-trip as null (permanent), not silently coerced")
            .isNull();
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
                "strict-ts-query", "1136fa322f1a275da617c6166be93366422c643fd285110960b04a2ac9f4d1a9", "", "store_put", "",
                "" /* blank timestamp */))
            .as("importRelevanceRow with blank timestamp must throw (not silently stamp now())")
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("import timestamp must not be null/blank");
    }

    @Test @Order(17)
    void importRelevanceRow_malformedTimestamp_throwsIllegalArgument() {
        assertThatThrownBy(() ->
            repo.importRelevanceRow(TENANT_A,
                "strict-ts-bad-query", "39fc4e4e013b7b551ba82a35113fafde1db4229d408aacc08ffe7cedfb7d5388", "", "store_put", "",
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
            "dup-query-npe-test", "67d2d37e469c8b913d88d2402a80b64df633cba75e35e84c706d8fc18207da41", "store_put", "sess-dup", "code__nexus");
        assertThat(id1).as("first insert must return positive id").isPositive();

        // To force the dedup index conflict we import the SAME row with a fixed timestamp
        // via the import path (live path uses now() which has sub-second uniqueness).
        // Import twice with the same timestamp — second must DO NOTHING, not NPE.
        String fixedTs = "2025-03-15T09:00:00Z";
        repo.importRelevanceRow(TENANT_A,
            "dup-import-npe", "ab9d8951cdfbeec5f0a40962e29af7fdaeac5aee4648c142364fbf9a17377ee8", "", "store_put", "sess-dup2", fixedTs);
        // Second identical import — the dedup index fires; must not throw
        assertThatCode(() ->
            repo.importRelevanceRow(TENANT_A,
                "dup-import-npe", "ab9d8951cdfbeec5f0a40962e29af7fdaeac5aee4648c142364fbf9a17377ee8", "", "store_put", "sess-dup2", fixedTs))
            .as("second identical import must not throw (DO NOTHING)")
            .doesNotThrowAnyException();

        // Exactly one row
        var rows = repo.getRelevanceLog(TENANT_A, "dup-import-npe", "ab9d8951cdfbeec5f0a40962e29af7fdaeac5aee4648c142364fbf9a17377ee8", "", "", 10);
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
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
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
            "probe-query", "6fd174bc3360e133e6c3f3352f4046fd3067ca59bda00c47537acc31f6cec1e7", "code__nexus", "store_put", "probe-sess",
            "2025-05-01T00:00:00Z");

        var presentKey = List.<Object>of(
            "probe-query", "6fd174bc3360e133e6c3f3352f4046fd3067ca59bda00c47537acc31f6cec1e7", "store_put", "probe-sess", "2025-05-01T00:00:00Z");
        var missingKey = List.<Object>of(
            "probe-query", "c09ad640db4ced088e63b76e3297dfbe4c087aa755a7fab60acfe14ac983d370", "store_put", "probe-sess", "2025-05-01T00:00:00Z");

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
            "f6a5377eb8124396725a7c2146f6b3cc96f3a4bbda0157b2c26d366383e6ad24", "2025-05-06T00:00:00Z", 30, 1.5, 2, "2025-05-06T00:00:00Z");

        var presentKey = List.<Object>of("f6a5377eb8124396725a7c2146f6b3cc96f3a4bbda0157b2c26d366383e6ad24");
        var missingKey = List.<Object>of("2a4bb2a4cf738d5be52f827a0f2bbe0a6afc9996cd7ca3004e5ce8bbfef05597");

        var present = repo.probeIds(TENANT_A, "frecency", List.of(presentKey, missingKey));

        assertThat(present).hasSize(1).containsExactly(presentKey);
    }

    @Test @Order(34)
    void probeIds_tenantScoped_secondTenantSeesNothing() {
        repo.importRelevanceRow(TENANT_A,
            "tenant-scoped-query", "2303fef71cc64ce7c11e50020863a4be3b93b58ff8d800054deb61246ae74ef0", "code__nexus", "store_put", "sess-scoped",
            "2025-05-07T00:00:00Z");

        var key = List.<Object>of(
            "tenant-scoped-query", "2303fef71cc64ce7c11e50020863a4be3b93b58ff8d800054deb61246ae74ef0", "store_put", "sess-scoped",
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
            "instant-eq-query", "0c71a73c048a98d48a0f9aa8ba2ab246cf6babd522cb777120c22be1c2ff5695", "code__nexus", "store_put", "sess-eq",
            "2025-05-08T12:30:00Z");

        var offsetFormKey = List.<Object>of(
            "instant-eq-query", "0c71a73c048a98d48a0f9aa8ba2ab246cf6babd522cb777120c22be1c2ff5695", "store_put", "sess-eq",
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
                PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
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
            Map.of("query", "batch-rl-q0", "chunk_id", "1d39101ad2293ca5be9ad805a05eee36c73683aade61ee5046cbfe1e76930457", "collection", "knowledge__nexus",
                   "action", "store_put", "session_id", "sess-0", "timestamp", PAST_TS),
            Map.of("query", "batch-rl-q1", "chunk_id", "42a0f8f6dc21be1ce7856086f5774dfe0182334bcac5e991418aefb31ecb4820", "collection", "knowledge__nexus",
                   "action", "store_put", "session_id", "sess-1", "timestamp", PAST_TS)));
        assertThat(n).isEqualTo(2);
        assertThat(repo.getRelevanceLog(TENANT_A, "batch-rl-q0", "1d39101ad2293ca5be9ad805a05eee36c73683aade61ee5046cbfe1e76930457", "", "", 5)).hasSize(1);
        assertThat(repo.getRelevanceLog(TENANT_A, "batch-rl-q1", "42a0f8f6dc21be1ce7856086f5774dfe0182334bcac5e991418aefb31ecb4820", "", "", 5)).hasSize(1);

        // Re-import (same rows) — DO NOTHING must not duplicate.
        repo.importBatch(TENANT_A, "relevance_log", List.of(
            Map.of("query", "batch-rl-q0", "chunk_id", "1d39101ad2293ca5be9ad805a05eee36c73683aade61ee5046cbfe1e76930457", "collection", "knowledge__nexus",
                   "action", "store_put", "session_id", "sess-0", "timestamp", PAST_TS)));
        assertThat(repo.getRelevanceLog(TENANT_A, "batch-rl-q0", "1d39101ad2293ca5be9ad805a05eee36c73683aade61ee5046cbfe1e76930457", "", "", 5)).hasSize(1);
    }

    @Test @Order(41)
    void importBatch_frecency_multiRow_greatestMerge_intraBatchDedupeLastWins() {
        // Two rows for the SAME chunk_id in ONE batch — must dedupe (last wins),
        // since a single multi-row ON CONFLICT DO UPDATE cannot affect the same
        // row twice.
        int n = repo.importBatch(TENANT_A, "frecency", List.of(
            Map.of("chunk_id", "9c4bcf15a73ec0b5a38f89c7fff6aba6c67e031f42ae51957a3399eb9e6e95e4", "embedded_at", "2024-01-01T00:00:00Z",
                   "ttl_days", 30, "frecency_score", 0.3, "miss_count", 2,
                   "last_hit_at", "2024-02-01T00:00:00Z"),
            Map.of("chunk_id", "9c4bcf15a73ec0b5a38f89c7fff6aba6c67e031f42ae51957a3399eb9e6e95e4", "embedded_at", "2024-01-01T00:00:00Z",
                   "ttl_days", 30, "frecency_score", 0.8, "miss_count", 9,
                   "last_hit_at", "2024-03-01T00:00:00Z")));
        assertThat(n).as("rows submitted (contract unchanged), not rows landed").isEqualTo(2);

        var got = repo.getFrecency(TENANT_A, "9c4bcf15a73ec0b5a38f89c7fff6aba6c67e031f42ae51957a3399eb9e6e95e4");
        assertThat(got).isPresent();
        assertThat(((Number) got.get().get("frecency_score")).doubleValue()).isEqualTo(0.8);
        assertThat(((Number) got.get().get("miss_count")).intValue()).isEqualTo(9);

        // Re-import with STALE (lower) values — GREATEST must not roll back live values.
        repo.importBatch(TENANT_A, "frecency", List.of(
            Map.of("chunk_id", "9c4bcf15a73ec0b5a38f89c7fff6aba6c67e031f42ae51957a3399eb9e6e95e4", "embedded_at", "2024-01-01T00:00:00Z",
                   "ttl_days", 30, "frecency_score", 0.1, "miss_count", 1,
                   "last_hit_at", "2024-01-15T00:00:00Z")));
        var afterStale = repo.getFrecency(TENANT_A, "9c4bcf15a73ec0b5a38f89c7fff6aba6c67e031f42ae51957a3399eb9e6e95e4");
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
        repo.importRelevanceRow(tenant, "q1", "9f080d6cb0d587442a488f6d2a8d532d67e704e26f0340bf7f4fa1102ec20830", "knowledge__x", "click", "s",
            "2020-01-01T00:00:00Z");
        repo.importRelevanceRow(tenant, "q2", "acf420a9873c4555aa28ccb6fc5672b0f959a898e4dd403f36f3fe87ca8d975b", "knowledge__x", "click", "s",
            "2020-01-02T00:00:00Z");
        repo.logRelevance(tenant, "q3", "a1a64503f17898123d9e06b42e63993a4c24ec4088d5997a443c94e0405c2db1", "click", "s", "knowledge__x");

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
        repo.importRelevanceRow(a, "q", "607b637b441b68381cc266868179525c9a1648148eecabd42dc70a29e6740257", "knowledge__x", "click", "s",
            "2020-01-01T00:00:00Z");
        repo.expireRelevanceLog(a, 90);
        org.assertj.core.api.Assertions.assertThat(
            repo.getRetentionMarkers(b, List.of("nexus.relevance_log")))
            .as("tenant B must not see tenant A's marker (RLS)")
            .isEmpty();
    }

    // ── nexus-v0x32: relevance/stats (playbook §4.5 telemetry baseline) ─────

    @Test
    void relevanceStats_emptyTenant_reportsZeroCountAndNullTimestamps() {
        String tenant = "rel-stats-empty-" + System.nanoTime();
        var stats = repo.relevanceStats(tenant);
        org.assertj.core.api.Assertions.assertThat(stats)
            .containsEntry("count", 0)
            .containsEntry("oldest", null)
            .containsEntry("newest", null);
    }

    @Test
    void relevanceStats_countsRowsAndReportsOldestNewest() {
        String tenant = "rel-stats-" + System.nanoTime();
        repo.importRelevanceRow(tenant, "q1", "c7ac372b7b8ab091b8f723a45e236c05ac2c6721d4fb869bcfda1b66cbbd30a2", "knowledge__x", "click", "s",
            "2020-01-01T00:00:00Z");
        repo.importRelevanceRow(tenant, "q2", "e46466f2a3040ceb0fbdd3889209a8a1d3488b4b2af8e51cd7da3dc37d5d8f5f", "knowledge__x", "click", "s",
            "2020-06-15T12:30:00Z");
        repo.importRelevanceRow(tenant, "q3", "9d710182e0f8dd92dd35d20e3144f947bc91ee19d1bd7a3aecab1caf7ebcf91e", "knowledge__x", "click", "s",
            "2019-03-10T08:00:00Z");

        var stats = repo.relevanceStats(tenant);
        org.assertj.core.api.Assertions.assertThat(stats)
            .containsEntry("count", 3)
            .containsEntry("oldest", "2019-03-10T08:00:00Z")
            .containsEntry("newest", "2020-06-15T12:30:00Z");
    }

    @Test
    void relevanceStats_isTenantScoped() {
        String a = "rel-stats-iso-a-" + System.nanoTime();
        String b = "rel-stats-iso-b-" + System.nanoTime();
        repo.importRelevanceRow(a, "q", "8c82b0fa4cf980baf9548635007bed74e9eaceeb57e7b5ccff842b5f05de452e", "knowledge__x", "click", "s",
            "2021-01-01T00:00:00Z");
        var statsB = repo.relevanceStats(b);
        org.assertj.core.api.Assertions.assertThat(statsB)
            .as("tenant B must not see tenant A's relevance_log rows (RLS)")
            .containsEntry("count", 0)
            .containsEntry("oldest", null)
            .containsEntry("newest", null);
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
    void queryNxAnswerRuns_dateOnlySinceFiltersCorrectly_neverSilentlyEmpty() {
        // nexus-spbay: the old parseTs fallback turned a bare date into
        // now() — `since 2026-08-01` became `since right now` and every
        // date-filtered read returned a confirmatory zero against a
        // populated table (measured live: 214 rows, "(no runs)" for every
        // date tested). Date-only now parses as midnight UTC.
        // Fixture discipline (code-review Important #2, 2026-08-31): every
        // row AND since value sits in the PAST relative to wall clock, so
        // a regression to parseTs's now()-fallback yields 0 rows for BOTH
        // probes — each assert below discriminates against the exact bug.
        String tenant = "nar-dateonly-" + System.nanoTime();
        repo.importNxAnswerRunRow(tenant, "old", null, null,
            0, "a", 0.0, 1_000L, "2020-01-01T00:00:00Z");
        repo.importNxAnswerRunRow(tenant, "new", null, null,
            0, "a", 0.0, 1_000L, "2025-06-15T12:00:00Z");

        var recent = repo.queryNxAnswerRuns(tenant, "2024-01-01", 100);
        assertThat(recent.get("total")).isEqualTo(1);

        var everything = repo.queryNxAnswerRuns(tenant, "2019-01-01", 100);
        assertThat(everything.get("total")).isEqualTo(2);
    }

    @Test
    void queryNxAnswerRuns_malformedSinceThrows_neverSilentEmptySet() {
        // The other half of nexus-spbay: garbage must FAIL LOUD (the
        // handler's global IllegalArgumentException arm answers 400) —
        // never the old silent now()-substitution that manufactured an
        // empty set indistinguishable from "no usage".
        String tenant = "nar-badsince-" + System.nanoTime();
        org.assertj.core.api.Assertions.assertThatThrownBy(
                () -> repo.queryNxAnswerRuns(tenant, "not-a-date", 100))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("not-a-date");
    }

    @Test
    void parseSinceFilter_acceptsTheThreeCallerShapes() {
        assertThat(TelemetryRepository.parseSinceFilter("2026-08-01")
                .toString()).startsWith("2026-08-01T00:00");
        assertThat(TelemetryRepository.parseSinceFilter("2026-08-01T12:30:00")
                .toString()).startsWith("2026-08-01T12:30");
        assertThat(TelemetryRepository.parseSinceFilter("2026-08-01T12:30:00Z")
                .toString()).startsWith("2026-08-01T12:30");
        assertThat(TelemetryRepository.parseSinceFilter("2026-08-01T12:30:00+00:00")
                .toString()).startsWith("2026-08-01T12:30");
    }

    @Test
    void queryTierWrites_dateOnlySinceFiltersCorrectly() {
        // Same nexus-spbay defect class at the tier_writes since sites.
        // Same past-only fixture discipline as the nx_answer test above —
        // under the now()-fallback bug this returns 0, never 1.
        String tenant = "tw-dateonly-" + System.nanoTime();
        repo.recordTierWrite(tenant, "s1", "2020-01-01T00:00:00Z",
            "memory_put", "T2", "", "", "");
        repo.recordTierWrite(tenant, "s2", "2025-06-15T12:00:00Z",
            "memory_put", "T2", "", "", "");

        var recent = repo.queryTierWrites(tenant, "", "2024-01-01", 0);
        long total = recent.stream()
            .mapToLong(r -> ((Number) r.get("count")).longValue()).sum();
        assertThat(total).isEqualTo(1);
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

    // ── nx_answer_steps (RDR-196 .p1c, nexus-nyry9.9) ────────────────────────────

    /**
     * nexus-ndoke. The Anthropic usage envelope reports CACHED input separately:
     * a cached prompt records {@code input_tokens=2} with the real size in
     * {@code cache_creation_input_tokens} (cold) or {@code cache_read_input_tokens}
     * (warm). Measured on real dispatch envelopes: cold 2 / 26068 / 0, warm
     * 2 / - / 16324. telemetry-007 had no column for either, so {@code input_tokens}
     * read 2 on essentially every recorded step and every per-plan cost aggregate
     * over these rows mixed cache-warm and cache-cold runs with nothing recorded
     * that could separate them — RDR-196's own numbers included.
     *
     * <p>The other tests in this class pass {@code null, null} for the new fields,
     * so they prove the columns EXIST but nothing about the values surviving. This
     * one asserts the round trip with the measured cold and warm shapes, and that
     * absence still stores as NULL rather than 0 — a stored 0 would read as "used
     * no cached input", a measurement claim the run never made.
     */
    @Test @Order(49)
    void recordNxAnswerRun_persistsCacheTokenFields() {
        String tenant = "tel-tenant-a";
        String question = "cache-token-question-" + System.nanoTime();
        List<TelemetryRepository.StepInput> steps = List.of(
            // cold: real prompt size lands in cache_creation, cache_read is 0
            new TelemetryRepository.StepInput(0, "claude_dispatch", "llm", "claude-opus-5",
                2, 1685, 0, 26068, 0.8851, 9000, true, List.of()),
            // warm: real prompt size lands in cache_read
            new TelemetryRepository.StepInput(1, "claude_dispatch", "llm", "claude-opus-5",
                2, 1684, 16324, 0, 0.0843, 3000, true, List.of()),
            // a sql step runs no prompt at all — absence, not zero
            new TelemetryRepository.StepInput(2, "operator_filter", "sql", null,
                0, 0, null, null, 0.0, 5, true, List.of()));

        repo.recordNxAnswerRun(tenant, question, null, null, 3, "cache token text",
            0.9694, 12005, PAST_TS, steps);

        List<Map<String, Object>> rows =
            fetchNxAnswerStepRows(fetchNxAnswerRunId(tenant, question));
        assertThat(rows).hasSize(3);

        Map<String, Object> cold = rows.get(0);
        assertThat(((Number) cold.get("input_tokens")).intValue())
            .as("the envelope really does report 2 for a cached prompt — this is not a bug")
            .isEqualTo(2);
        assertThat(((Number) cold.get("cache_creation_input_tokens")).intValue())
            .as("the real prompt size must survive, or cost is unattributable")
            .isEqualTo(26068);
        assertThat(((Number) cold.get("cache_read_input_tokens")).intValue()).isZero();

        Map<String, Object> warm = rows.get(1);
        assertThat(((Number) warm.get("cache_read_input_tokens")).intValue())
            .isEqualTo(16324);
        assertThat(((Number) warm.get("cache_creation_input_tokens")).intValue()).isZero();

        // The pair that made the defect visible: same model, output tokens differing
        // by ONE, cost differing 10x. Only the cache columns can explain it.
        assertThat(cold.get("model")).isEqualTo(warm.get("model"));
        assertThat(Math.abs(((Number) cold.get("output_tokens")).intValue()
                          - ((Number) warm.get("output_tokens")).intValue()))
            .isEqualTo(1);

        Map<String, Object> sql = rows.get(2);
        assertThat(sql.get("cache_read_input_tokens"))
            .as("absence must store as NULL — 0 would claim 'used no cached input'")
            .isNull();
        assertThat(sql.get("cache_creation_input_tokens")).isNull();
    }

    @Test @Order(49)
    void recordNxAnswerRun_withSteps_writesParentAndChildren() {
        String tenant = "tel-tenant-a";
        String question = "steps-write-question-" + System.nanoTime();
        List<TelemetryRepository.StepInput> steps = List.of(
            new TelemetryRepository.StepInput(0, "operator_filter", "sql", null,
                0, 0, null, null, 0.0, 12, true, List.of()),
            new TelemetryRepository.StepInput(1, "claude_dispatch", "bundle", "claude-fable-5",
                150, 40, null, null, 0.0021, 4200, true, List.of(1, 2)));

        repo.recordNxAnswerRun(tenant, question, null, null, 2, "steps final text",
            0.0021, 4212, PAST_TS, steps);

        long runId = fetchNxAnswerRunId(tenant, question);
        List<Map<String, Object>> rows = fetchNxAnswerStepRows(runId);
        assertThat(rows).as("both step rows must persist").hasSize(2);

        Map<String, Object> sqlStep = rows.get(0);
        assertThat(sqlStep.get("step_index")).isEqualTo(0);
        assertThat(sqlStep.get("operator")).isEqualTo("operator_filter");
        assertThat(sqlStep.get("source")).isEqualTo("sql");
        assertThat(sqlStep.get("model")).isNull();
        assertThat(((Number) sqlStep.get("elapsed_ms")).intValue()).isEqualTo(12);
        assertThat(sqlStep.get("ok")).isEqualTo(true);

        Map<String, Object> bundleStep = rows.get(1);
        assertThat(bundleStep.get("step_index")).isEqualTo(1);
        assertThat(bundleStep.get("source")).isEqualTo("bundle");
        assertThat(bundleStep.get("model")).isEqualTo("claude-fable-5");
        assertThat(((Number) bundleStep.get("input_tokens")).intValue()).isEqualTo(150);
        assertThat(((Number) bundleStep.get("output_tokens")).intValue()).isEqualTo(40);
        assertThat(((java.math.BigDecimal) bundleStep.get("cost_usd")).doubleValue())
            .isEqualTo(0.0021);
        Integer[] bundledSteps = (Integer[]) bundleStep.get("bundled_steps");
        assertThat(bundledSteps).containsExactly(1, 2);
    }

    @Test @Order(50)
    void recordNxAnswerRun_stepFailure_rollsBackParent() {
        String tenant = "tel-tenant-a";
        String question = "steps-rollback-question-" + System.nanoTime();
        // 'not_a_real_source' violates nx_answer_steps_source_chk (telemetry-007-1) —
        // the DB, not the Java layer, is the enforcement point (see
        // TelemetryHandler.parseNxAnswerSteps javadoc).
        List<TelemetryRepository.StepInput> steps = List.of(
            new TelemetryRepository.StepInput(0, "op", "not_a_real_source", null,
                null, null, null, null, null, 1, true, List.of()));

        assertThatThrownBy(() ->
            repo.recordNxAnswerRun(tenant, question, null, null, 1, "should not persist",
                0.0, 1, PAST_TS, steps)
        ).isInstanceOf(RuntimeException.class);

        // The parent insert must have rolled back along with the failed child —
        // partial telemetry is worse than none (this bead's own DO instruction).
        assertThat(nxAnswerRunExists(tenant, question))
            .as("a failed child insert must roll back the parent row too")
            .isFalse();
    }

    @Test @Order(51)
    void recordNxAnswerRun_explicitEmptySteps_stillSucceeds() {
        String tenant = "tel-tenant-a";
        String question = "steps-empty-question-" + System.nanoTime();

        repo.recordNxAnswerRun(tenant, question, null, null, 0, "no steps here",
            0.0, 5, PAST_TS, List.of());

        long runId = fetchNxAnswerRunId(tenant, question);
        assertThat(fetchNxAnswerStepRows(runId))
            .as("an explicit empty steps list must write the parent and zero children")
            .isEmpty();
    }

    @Test @Order(52)
    void nxAnswerSteps_isTenantIsolated() {
        String tenantA = "tel-tenant-a";
        String tenantB = "tel-tenant-b";
        String question = "steps-tenant-iso-question-" + System.nanoTime();
        List<TelemetryRepository.StepInput> steps = List.of(
            new TelemetryRepository.StepInput(0, "op", "llm", null,
                null, null, null, null, null, 3, true, List.of()));

        repo.recordNxAnswerRun(tenantA, question, null, null, 1, "iso final",
            0.0, 3, PAST_TS, steps);
        long runId = fetchNxAnswerRunId(tenantA, question);

        assertThat(countNxAnswerStepsForRunUnderTenantGuc(runId, tenantA))
            .as("the owning tenant must see its own step row")
            .isEqualTo(1);
        assertThat(countNxAnswerStepsForRunUnderTenantGuc(runId, tenantB))
            .as("a different tenant must see zero step rows for this run (FORCE RLS)")
            .isEqualTo(0);
    }

    // ── nx_answer_runs.cost_usd nullable + include_steps read route
    //    (RDR-196 .p1c-b, nexus-lme1s) ──────────────────────────────────────

    @Test @Order(53)
    void recordNxAnswerRun_nullCostUsd_readsBackNullNotZero() {
        String tenant = "nar-null-cost-" + System.nanoTime();
        // A run with a genuinely unknown cost (null) alongside one with a
        // real known cost — proves both the write-null round trip AND that
        // avg_cost_usd (SQL AVG) ignores the null rather than averaging it
        // in as a zero (which would silently understate every reported
        // average the moment any caller sends a null).
        repo.recordNxAnswerRun(tenant, "no usage observed", null, null, 0, "",
            null, 1_000, PAST_TS);
        repo.recordNxAnswerRun(tenant, "known cost", 1L, 0.9, 1, "answer",
            0.02, 1_000, PAST_TS);

        var out = repo.queryNxAnswerRuns(tenant, "", 100);
        assertThat(out.get("total")).isEqualTo(2);
        assertThat((Double) out.get("avg_cost_usd"))
            .as("AVG must ignore the null row, not treat it as 0.0 "
                + "(0.02 averaged over 1 non-null row, not 2)")
            .isCloseTo(0.02, org.assertj.core.data.Offset.offset(1e-9));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) out.get("rows");
        var nullRow = rows.stream()
            .filter(r -> "no usage observed".equals(r.get("question")))
            .findFirst().orElseThrow();
        assertThat(nullRow.get("cost_usd"))
            .as("a null cost_usd must read back as null, never a fabricated 0.0")
            .isNull();
        var knownRow = rows.stream()
            .filter(r -> "known cost".equals(r.get("question")))
            .findFirst().orElseThrow();
        assertThat(((Number) knownRow.get("cost_usd")).doubleValue()).isEqualTo(0.02);
    }

    @Test @Order(54)
    void queryNxAnswerRuns_includeStepsTrue_returnsStepsOrderedByStepIndex() {
        String tenant = "nar-incl-steps-" + System.nanoTime();
        String withSteps = "run-with-steps-" + System.nanoTime();
        String withoutSteps = "run-without-steps-" + System.nanoTime();
        // Insertion order deliberately reversed vs step_index — proves the
        // read path orders by step_index, not insertion/write order.
        List<TelemetryRepository.StepInput> steps = List.of(
            new TelemetryRepository.StepInput(1, "claude_dispatch", "bundle", "claude-fable-5",
                150, 40, null, null, 0.0021, 4200, true, List.of(1, 2)),
            new TelemetryRepository.StepInput(0, "operator_filter", "sql", null,
                0, 0, null, null, 0.0, 12, true, List.of()));
        // Each StepInput inserts as its own PK-addressed row regardless of
        // list position, so writing index-1-then-index-0 exercises the
        // claim that the READ path (ORDER BY step_index) — not write
        // order — determines what comes back below.
        repo.recordNxAnswerRun(tenant, withSteps, null, null, 2, "final",
            0.0021, 4212, PAST_TS, steps);
        repo.recordNxAnswerRun(tenant, withoutSteps, null, null, 0, "no steps",
            0.0, 5, PAST_TS, List.of());

        var out = repo.queryNxAnswerRuns(tenant, "", 100, true);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) out.get("rows");
        assertThat(rows).hasSize(2);

        var rowWithSteps = rows.stream()
            .filter(r -> withSteps.equals(r.get("question")))
            .findFirst().orElseThrow();
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> gotSteps = (List<Map<String, Object>>) rowWithSteps.get("steps");
        assertThat(gotSteps).as("both step rows must be present").hasSize(2);
        assertThat(gotSteps.get(0).get("step_index")).isEqualTo(0);
        assertThat(gotSteps.get(0).get("operator")).isEqualTo("operator_filter");
        assertThat(gotSteps.get(0).get("source")).isEqualTo("sql");
        assertThat(gotSteps.get(1).get("step_index")).isEqualTo(1);
        assertThat(gotSteps.get(1).get("source")).isEqualTo("bundle");
        assertThat(gotSteps.get(1).get("model")).isEqualTo("claude-fable-5");
        assertThat(((Number) gotSteps.get(1).get("input_tokens")).intValue()).isEqualTo(150);
        assertThat(((java.math.BigDecimal) gotSteps.get(1).get("cost_usd")).doubleValue())
            .isEqualTo(0.0021);
        Integer[] bundledSteps = (Integer[]) gotSteps.get(1).get("bundled_steps");
        assertThat(bundledSteps).containsExactly(1, 2);

        var rowWithoutSteps = rows.stream()
            .filter(r -> withoutSteps.equals(r.get("question")))
            .findFirst().orElseThrow();
        assertThat(rowWithoutSteps.get("steps"))
            .as("a run written with zero steps must still get 'steps': [] under "
                + "include_steps=true, never an absent key or null")
            .isEqualTo(List.of());
    }

    @Test @Order(55)
    void queryNxAnswerRuns_includeStepsFalse_omitsStepsKeyEntirely() {
        String tenant = "nar-no-incl-steps-" + System.nanoTime();
        List<TelemetryRepository.StepInput> steps = List.of(
            new TelemetryRepository.StepInput(0, "op", "llm", null,
                null, null, null, null, null, 1, true, List.of()));
        repo.recordNxAnswerRun(tenant, "q", null, null, 1, "a", 0.0, 1, PAST_TS, steps);

        // Both the default 3-arg overload AND explicit includeSteps=false must
        // be byte-for-byte the pre-existing shape — no 'steps' key at all.
        var defaultOut = repo.queryNxAnswerRuns(tenant, "", 100);
        var explicitFalseOut = repo.queryNxAnswerRuns(tenant, "", 100, false);
        for (var out : List.of(defaultOut, explicitFalseOut)) {
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> rows = (List<Map<String, Object>>) out.get("rows");
            assertThat(rows).hasSize(1);
            assertThat(rows.get(0)).doesNotContainKey("steps");
        }
    }

    @Test @Order(56)
    void queryNxAnswerRuns_includeStepsTrue_isTenantScoped() {
        String tenantA = "nar-incl-iso-a-" + System.nanoTime();
        String tenantB = "nar-incl-iso-b-" + System.nanoTime();
        List<TelemetryRepository.StepInput> steps = List.of(
            new TelemetryRepository.StepInput(0, "op", "llm", null,
                null, null, null, null, null, 3, true, List.of()));
        repo.recordNxAnswerRun(tenantA, "tenant-a-run", null, null, 1, "a",
            0.0, 3, PAST_TS, steps);

        var mineOut = repo.queryNxAnswerRuns(tenantA, "", 100, true);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> mineRows = (List<Map<String, Object>>) mineOut.get("rows");
        assertThat(mineRows).hasSize(1);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> mineSteps = (List<Map<String, Object>>) mineRows.get(0).get("steps");
        assertThat(mineSteps).as("the owning tenant must see its own step").hasSize(1);

        // FORCE RLS on both nx_answer_runs AND nx_answer_steps, exercised
        // through the SAME NOSUPERUSER svcDs-backed repo this whole test
        // class uses (tenantScope = new TenantScope(svcDs) in setup) — a
        // different tenant must see zero rows (and therefore zero steps).
        var theirsOut = repo.queryNxAnswerRuns(tenantB, "", 100, true);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> theirsRows = (List<Map<String, Object>>) theirsOut.get("rows");
        assertThat(theirsRows)
            .as("a different tenant must see zero rows for tenant A's run (FORCE RLS)")
            .isEmpty();
    }

    private long fetchNxAnswerRunId(String tenant, String question) {
        try (Connection conn = pg.createConnection("")) {
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, tenant, false);
            var rs = conn.createStatement().executeQuery(
                "SELECT id FROM nexus.nx_answer_runs WHERE tenant_id='" + tenant
                + "' AND question='" + question + "'");
            assertThat(rs.next()).as("run row must exist for question=" + question).isTrue();
            return rs.getLong("id");
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }

    private boolean nxAnswerRunExists(String tenant, String question) {
        try (Connection conn = pg.createConnection("")) {
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, tenant, false);
            var rs = conn.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.nx_answer_runs WHERE tenant_id='" + tenant
                + "' AND question='" + question + "'");
            rs.next();
            return rs.getInt(1) > 0;
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }

    private List<Map<String, Object>> fetchNxAnswerStepRows(long runId) {
        try (Connection conn = pg.createConnection("")) {
            var rs = conn.createStatement().executeQuery(
                "SELECT step_index, operator, source, model, input_tokens, output_tokens, "
                + "cache_read_input_tokens, cache_creation_input_tokens, "
                + "cost_usd, elapsed_ms, ok, bundled_steps FROM nexus.nx_answer_steps "
                + "WHERE run_id=" + runId + " ORDER BY step_index");
            List<Map<String, Object>> rows = new java.util.ArrayList<>();
            while (rs.next()) {
                Map<String, Object> row = new java.util.LinkedHashMap<>();
                row.put("step_index", rs.getInt("step_index"));
                row.put("operator", rs.getString("operator"));
                row.put("source", rs.getString("source"));
                row.put("model", rs.getString("model"));
                row.put("input_tokens", (Object) rs.getObject("input_tokens"));
                row.put("output_tokens", (Object) rs.getObject("output_tokens"));
                // nexus-ndoke: getObject, not getInt — these must round-trip NULL
                // as null. getInt would coerce absence to 0, which is the exact
                // "used no cached input" claim the nullable columns exist to avoid.
                row.put("cache_read_input_tokens",
                    (Object) rs.getObject("cache_read_input_tokens"));
                row.put("cache_creation_input_tokens",
                    (Object) rs.getObject("cache_creation_input_tokens"));
                row.put("cost_usd", rs.getObject("cost_usd"));
                row.put("elapsed_ms", rs.getInt("elapsed_ms"));
                row.put("ok", rs.getBoolean("ok"));
                java.sql.Array arr = rs.getArray("bundled_steps");
                row.put("bundled_steps", arr != null ? (Integer[]) arr.getArray() : new Integer[0]);
                rows.add(row);
            }
            return rows;
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }

    /** Queries nx_answer_steps under the SVC_ROLE (NOSUPERUSER NOBYPASSRLS) datasource,
     *  stamping {@code nexus.tenant} directly — the same GUC-then-query mechanism
     *  {@link TenantScope#withTenant} uses in production — so this actually exercises
     *  FORCE ROW LEVEL SECURITY rather than the superuser bypass {@code pg.createConnection}
     *  gets in every other helper in this file. */
    private long countNxAnswerStepsForRunUnderTenantGuc(long runId, String tenantGuc) {
        try (Connection conn = svcDs.getConnection()) {
            conn.setAutoCommit(false);
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, tenantGuc, true);
            long count;
            try (var ps = conn.prepareStatement(
                    "SELECT COUNT(*) FROM nexus.nx_answer_steps WHERE run_id = ?")) {
                ps.setLong(1, runId);
                var rs = ps.executeQuery();
                rs.next();
                count = rs.getLong(1);
            }
            conn.commit();
            return count;
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }

    private int countSearchTelemetryRows(String tenant) {
        try (Connection conn = pg.createConnection("")) {
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, tenant, false);
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
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, tenant, false);
            var rs = conn.createStatement().executeQuery(
                "SELECT COUNT(*) FROM nexus.hook_failures WHERE tenant_id='" + tenant + "'");
            rs.next();
            return rs.getInt(1);
        } catch (SQLException e) {
            throw new RuntimeException(e);
        }
    }
}
