package dev.nexus.service;

import dev.nexus.service.db.ScratchRepository;
import dev.nexus.service.db.TenantScope;
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
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-gmiaf.13 — ScratchRepository integration test.
 *
 * <p>Proves jOOQ-generated T1 scratch code compiles and executes correctly.
 *
 * <p>Coverage:
 * <ol>
 *   <li>put/get round-trip via repository (not HTTP)</li>
 *   <li>RLS tenant isolation: tenant-A entries invisible to tenant-B</li>
 *   <li>Session isolation: session-A entries invisible to session-B queries</li>
 *   <li>FTS search: prose content and tag identifiers both match</li>
 *   <li>flag/unflag cycle: flagged=true then flagged=false</li>
 *   <li>flaggedEntries: only flagged rows returned</li>
 *   <li>listEntries: all rows for session returned</li>
 *   <li>delete: row removed, second delete returns false</li>
 *   <li>closeSession: all rows for session deleted, returns count</li>
 *   <li>sweepTenant: TTL rows deleted by cutoff</li>
 *   <li>resolvePrefix: full UUID found by prefix</li>
 * </ol>
 *
 * <p>Hermetic: embedded Postgres (io.zonky), port 0, no Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ScratchRepositoryTest {

    private static final String SVC_ROLE = "svc_scratch_repo_test";
    private static final String SVC_PASS = "svc_scratch_repo_test_pass";

    private static final String TENANT_A = "scratch-tenant-a";
    private static final String TENANT_B = "scratch-tenant-b";
    private static final String SESSION_A = "session-aaaa";
    private static final String SESSION_B = "session-bbbb";

    EmbeddedPostgres pg;
    TenantScope tenantScope;
    ScratchRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = EmbeddedPostgres.builder().start();

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
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

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA t1 TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON t1.scratch TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, t1, public");
        }

        svcDs = buildSvcDs();
        tenantScope = new TenantScope(svcDs);
        repo = new ScratchRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.close();
    }

    // ── Test 1: put/get round-trip ────────────────────────────────────────────

    @Test
    void put_get_roundTrip() {
        String id = uuid();
        repo.put(TENANT_A, SESSION_A, id, "hello world scratch", "tag1,tag2",
                 "agent-x", false, null, null);

        Map<String, Object> row = repo.get(TENANT_A, SESSION_A, id);

        assertThat(row).isNotEmpty();
        assertThat(row.get("id")).isEqualTo(id);
        assertThat(row.get("content")).isEqualTo("hello world scratch");
        assertThat(row.get("session_id")).isEqualTo(SESSION_A);
        assertThat(row.get("tags")).isEqualTo("tag1,tag2");
        assertThat(row.get("agent")).isEqualTo("agent-x");
        assertThat(row.get("flagged")).isEqualTo(false);
        // access_count should be 1 after first get (incremented by get())
        assertThat(row.get("access_count")).isEqualTo(1);
    }

    // ── Test 2: get returns empty when id not in session ─────────────────────

    @Test
    void get_absentId_returnsEmpty() {
        Map<String, Object> row = repo.get(TENANT_A, SESSION_A, "nonexistent-id-xyz");
        assertThat(row).isEmpty();
    }

    // ── Test 3: RLS tenant isolation ─────────────────────────────────────────

    @Test
    void rls_tenantIsolation_crossTenantCannotSeeEntry() {
        String id = uuid();
        repo.put(TENANT_A, SESSION_A, id, "tenant-A secret content", null,
                 null, false, null, null);

        // Tenant-B cannot GET tenant-A's entry
        Map<String, Object> crossRow = repo.get(TENANT_B, SESSION_A, id);
        assertThat(crossRow).as("tenant-B must not see tenant-A's entry").isEmpty();

        // Tenant-B list returns nothing for session_A
        List<Map<String, Object>> bEntries = repo.listEntries(TENANT_B, SESSION_A);
        assertThat(bEntries).as("tenant-B list must see no tenant-A entries").isEmpty();
    }

    // ── Test 4: session isolation — session-A cannot see session-B entries ───

    @Test
    void sessionIsolation_crossSessionCannotSee() {
        String idA = uuid();
        String idB = uuid();
        repo.put(TENANT_A, SESSION_A, idA, "session A content", null, null, false, null, null);
        repo.put(TENANT_A, SESSION_B, idB, "session B content", null, null, false, null, null);

        // GET with wrong session_id returns empty (session_id column filter)
        Map<String, Object> wrongSession = repo.get(TENANT_A, SESSION_B, idA);
        assertThat(wrongSession)
            .as("get with wrong session_id must return empty (session_id column filter)").isEmpty();

        // listEntries for SESSION_A does NOT include SESSION_B entries
        List<Map<String, Object>> listA = repo.listEntries(TENANT_A, SESSION_A);
        List<String> idsInA = listA.stream().map(e -> (String) e.get("id")).toList();
        assertThat(idsInA).as("SESSION_A list must not contain SESSION_B entry id").doesNotContain(idB);
    }

    // ── Test 5: FTS search — prose content ───────────────────────────────────

    @Test
    void search_fts_matchesProseStemming() {
        String id = uuid();
        String proj = "search-proj-" + System.nanoTime();
        repo.put(TENANT_A, SESSION_A, id, "training neural networks gradient descent " + proj,
                 "ml,optimization", null, false, null, null);

        // English stemmer: "network" matches "networks"
        List<Map<String, Object>> results = repo.search(TENANT_A, SESSION_A, proj, 10);
        assertThat(results).as("FTS search must find the entry by unique project term").isNotEmpty();
        List<String> ids = results.stream().map(e -> (String) e.get("id")).toList();
        assertThat(ids).contains(id);
    }

    // ── Test 6: FTS search — tag identifiers (simple config) ─────────────────

    @Test
    void search_fts_matchesTagIdentifiers() {
        String id = uuid();
        String uniqueTag = "nexus-fts-tag-" + System.nanoTime();
        repo.put(TENANT_A, SESSION_A, id, "plain content body", uniqueTag,
                 null, false, null, null);

        List<Map<String, Object>> results = repo.search(TENANT_A, SESSION_A, uniqueTag, 10);
        assertThat(results).as("FTS search must match exact tag (simple config)").isNotEmpty();
        List<String> ids = results.stream().map(e -> (String) e.get("id")).toList();
        assertThat(ids).contains(id);
    }

    // ── Test 7: FTS search is session-scoped ─────────────────────────────────

    @Test
    void search_scopedToSession() {
        String uniqueTerm = "uniqueftsterm-" + System.nanoTime();
        String idA = uuid();
        String idB = uuid();
        repo.put(TENANT_A, SESSION_A, idA, uniqueTerm + " session-a entry", null, null, false, null, null);
        repo.put(TENANT_A, SESSION_B, idB, uniqueTerm + " session-b entry", null, null, false, null, null);

        // Search from SESSION_A perspective — must find idA but not idB
        List<Map<String, Object>> resultsA = repo.search(TENANT_A, SESSION_A, uniqueTerm, 10);
        List<String> idsA = resultsA.stream().map(e -> (String) e.get("id")).toList();
        assertThat(idsA).contains(idA);
        assertThat(idsA).doesNotContain(idB);
    }

    // ── Test 8: flag/unflag cycle ─────────────────────────────────────────────

    @Test
    void flag_unflag_cycle() {
        String id = uuid();
        repo.put(TENANT_A, SESSION_A, id, "flaggable content", null, null, false, null, null);

        // Flag it
        boolean flagged = repo.flag(TENANT_A, SESSION_A, id, "target-project", "target-title");
        assertThat(flagged).as("flag must return true when entry exists").isTrue();

        // Verify flaggedEntries includes it
        List<Map<String, Object>> flaggedList = repo.flaggedEntries(TENANT_A, SESSION_A);
        List<String> flaggedIds = flaggedList.stream().map(e -> (String) e.get("id")).toList();
        assertThat(flaggedIds).as("flaggedEntries must include the flagged entry").contains(id);

        // Verify flush_project and flush_title round-trip
        Map<String, Object> row = repo.get(TENANT_A, SESSION_A, id);
        assertThat(row.get("flush_project")).isEqualTo("target-project");
        assertThat(row.get("flush_title")).isEqualTo("target-title");

        // Unflag it
        boolean unflagged = repo.unflag(TENANT_A, SESSION_A, id);
        assertThat(unflagged).as("unflag must return true when entry exists").isTrue();

        // Verify no longer in flaggedEntries
        List<Map<String, Object>> afterUnflag = repo.flaggedEntries(TENANT_A, SESSION_A);
        List<String> afterIds = afterUnflag.stream().map(e -> (String) e.get("id")).toList();
        assertThat(afterIds).as("unflagged entry must not appear in flaggedEntries").doesNotContain(id);
    }

    // ── Test 9: flag/unflag on absent entry returns false ────────────────────

    @Test
    void flag_absentEntry_returnsFalse() {
        boolean ok = repo.flag(TENANT_A, SESSION_A, "no-such-id", "p", "t");
        assertThat(ok).as("flag on absent entry must return false").isFalse();
    }

    // ── Test 10: listEntries — all for session, ordered ts desc ─────────────

    @Test
    void listEntries_allForSession_orderedTsDesc() {
        String proj = "list-repo-test-" + System.nanoTime();
        String id1 = uuid();
        String id2 = uuid();
        String id3 = uuid();
        repo.put(TENANT_A, SESSION_A, id1, "entry 1 " + proj, null, null, false, null, null);
        repo.put(TENANT_A, SESSION_A, id2, "entry 2 " + proj, null, null, false, null, null);
        repo.put(TENANT_A, SESSION_A, id3, "entry 3 " + proj, null, null, false, null, null);
        // Another session — must not appear in listing
        repo.put(TENANT_A, SESSION_B, uuid(), "session B entry " + proj, null, null, false, null, null);

        List<Map<String, Object>> entries = repo.listEntries(TENANT_A, SESSION_A);
        List<String> ids = entries.stream().map(e -> (String) e.get("id")).toList();
        assertThat(ids).as("listEntries must contain all 3 SESSION_A entries")
            .contains(id1, id2, id3);
        assertThat(ids).as("listEntries must NOT include SESSION_B entry").doesNotContain(proj);
    }

    // ── Test 11: delete — removes row, second delete returns false ───────────

    @Test
    void delete_removesRow_secondDeleteReturnsFalse() {
        String id = uuid();
        repo.put(TENANT_A, SESSION_A, id, "delete me", null, null, false, null, null);

        boolean first = repo.delete(TENANT_A, SESSION_A, id);
        assertThat(first).as("first delete must return true").isTrue();

        // Entry gone
        Map<String, Object> gone = repo.get(TENANT_A, SESSION_A, id);
        assertThat(gone).as("entry must not be findable after delete").isEmpty();

        boolean second = repo.delete(TENANT_A, SESSION_A, id);
        assertThat(second).as("second delete must return false").isFalse();
    }

    // ── Test 12: cross-session delete returns false ───────────────────────────

    @Test
    void delete_wrongSession_returnsFalse() {
        String id = uuid();
        repo.put(TENANT_A, SESSION_A, id, "session-protected content", null, null, false, null, null);

        // Delete from SESSION_B perspective — session_id filter blocks it
        boolean deleted = repo.delete(TENANT_A, SESSION_B, id);
        assertThat(deleted).as("cross-session delete must return false (session_id filter)").isFalse();

        // Entry still visible to SESSION_A
        Map<String, Object> still = repo.get(TENANT_A, SESSION_A, id);
        assertThat(still).as("entry must be intact after cross-session delete attempt").isNotEmpty();
    }

    // ── Test 13: closeSession — deletes all rows, returns count ──────────────

    @Test
    void closeSession_deletesAllRows_returnsCount() {
        String closeSession = "close-session-" + System.nanoTime();
        String id1 = uuid();
        String id2 = uuid();
        repo.put(TENANT_A, closeSession, id1, "entry one", null, null, false, null, null);
        repo.put(TENANT_A, closeSession, id2, "entry two", null, null, false, null, null);
        // Unrelated session — must NOT be affected
        String otherId = uuid();
        repo.put(TENANT_A, SESSION_A, otherId, "other session", null, null, false, null, null);

        int deleted = repo.closeSession(TENANT_A, closeSession);
        assertThat(deleted).as("closeSession must delete exactly 2 rows").isEqualTo(2);

        List<Map<String, Object>> after = repo.listEntries(TENANT_A, closeSession);
        assertThat(after).as("listEntries after closeSession must be empty").isEmpty();

        // Other session entry must survive
        Map<String, Object> other = repo.get(TENANT_A, SESSION_A, otherId);
        assertThat(other).as("other session entry must be unaffected by closeSession").isNotEmpty();
    }

    // ── Test 14: closeSession — idempotent (double-close returns 0) ──────────

    @Test
    void closeSession_idempotent_secondCallReturnsZero() {
        String closeSession2 = "close-session-idem-" + System.nanoTime();
        repo.put(TENANT_A, closeSession2, uuid(), "will be closed", null, null, false, null, null);

        repo.closeSession(TENANT_A, closeSession2);
        int secondDelete = repo.closeSession(TENANT_A, closeSession2);
        assertThat(secondDelete).as("second closeSession must return 0 (idempotent)").isEqualTo(0);
    }

    // ── Test 15: sweepTenant — deletes rows older than cutoff ────────────────

    @Test
    void sweepTenant_deletesByTtlCutoff() throws Exception {
        // Use a unique tenant to avoid other tests' entries polluting the swept count
        String sweepTenant = "sweep-tenant-" + System.nanoTime();
        String sweepSession = "sweep-session-" + System.nanoTime();
        String oldId = uuid();
        String newId = uuid();

        repo.put(sweepTenant, sweepSession, oldId, "old entry", null, null, false, null, null);
        Thread.sleep(50);
        repo.put(sweepTenant, sweepSession, newId, "new entry", null, null, false, null, null);

        // Sweep with cutoff = now + 2s: both entries are "old" relative to cutoff
        OffsetDateTime futureCutoff = OffsetDateTime.now(ZoneOffset.UTC).plusSeconds(2);
        int swept = repo.sweepTenant(sweepTenant, futureCutoff);
        assertThat(swept).as("sweepTenant must delete rows older than cutoff").isEqualTo(2);

        List<Map<String, Object>> after = repo.listEntries(sweepTenant, sweepSession);
        assertThat(after).as("all entries must be gone after sweep").isEmpty();
    }

    // ── Test 16: sweepTenant does NOT affect other tenants ───────────────────

    @Test
    void sweepTenant_doesNotAffectOtherTenants() {
        // Use unique tenants per-test so the sweep count stays predictable
        String isoTenantA = "sweep-iso-a-" + System.nanoTime();
        String isoTenantB = "sweep-iso-b-" + System.nanoTime();
        String sweepSess = "sweep-iso-sess-" + System.nanoTime();
        String aId = uuid();
        String bId = uuid();
        repo.put(isoTenantA, sweepSess, aId, "tenant-A old entry", null, null, false, null, null);
        repo.put(isoTenantB, sweepSess, bId, "tenant-B old entry", null, null, false, null, null);

        // Sweep isoTenantA with cutoff = now + 2s
        OffsetDateTime futureCutoff = OffsetDateTime.now(ZoneOffset.UTC).plusSeconds(2);
        repo.sweepTenant(isoTenantA, futureCutoff);

        // isoTenantA entry gone
        Map<String, Object> aGone = repo.get(isoTenantA, sweepSess, aId);
        assertThat(aGone).as("isoTenantA entry must be swept").isEmpty();

        // isoTenantB entry untouched (RLS isolation: sweepTenant only sees isoTenantA rows)
        Map<String, Object> bStays = repo.get(isoTenantB, sweepSess, bId);
        assertThat(bStays).as("isoTenantB entry must survive isoTenantA sweep").isNotEmpty();
    }

    // ── Test 17: resolvePrefix — finds full UUID by prefix ───────────────────

    @Test
    void resolvePrefix_findsByUuidPrefix() {
        String id = uuid();
        String prefix = id.substring(0, 8);
        repo.put(TENANT_A, SESSION_A, id, "prefix resolution content", null, null, false, null, null);

        List<String> resolved = repo.resolvePrefix(TENANT_A, SESSION_A, prefix);
        assertThat(resolved).as("resolvePrefix must find the entry by prefix").contains(id);
    }

    // ── Test 18: resolvePrefix returns empty for absent prefix ───────────────

    @Test
    void resolvePrefix_absent_returnsEmpty() {
        List<String> resolved = repo.resolvePrefix(TENANT_A, SESSION_A, "00000000-ffff");
        assertThat(resolved).as("resolvePrefix for absent prefix must return empty list").isEmpty();
    }

    // ── Test 19: resolvePrefix is session-scoped ─────────────────────────────

    @Test
    void resolvePrefix_sessionScoped() {
        String id = uuid();
        repo.put(TENANT_A, SESSION_B, id, "session B entry for prefix test", null, null, false, null, null);

        // Resolve from SESSION_A — must not find SESSION_B's entry
        String prefix = id.substring(0, 8);
        List<String> resolvedFromA = repo.resolvePrefix(TENANT_A, SESSION_A, prefix);
        assertThat(resolvedFromA).as("resolvePrefix from wrong session must not find entry").doesNotContain(id);
    }

    // ── Test 20: access_count increments on repeated get ─────────────────────

    @Test
    void get_accessCount_incrementsOnRepeat() {
        String id = uuid();
        repo.put(TENANT_A, SESSION_A, id, "access count test content", null, null, false, null, null);

        Map<String, Object> first  = repo.get(TENANT_A, SESSION_A, id);
        Map<String, Object> second = repo.get(TENANT_A, SESSION_A, id);

        int count1 = ((Number) first.get("access_count")).intValue();
        int count2 = ((Number) second.get("access_count")).intValue();
        assertThat(count2).as("access_count must increment on each get").isGreaterThan(count1);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private com.zaxxer.hikari.HikariDataSource buildSvcDs() {
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(5);
        config.setAutoCommit(true);
        return new com.zaxxer.hikari.HikariDataSource(config);
    }

    private static String uuid() {
        return UUID.randomUUID().toString();
    }
}
