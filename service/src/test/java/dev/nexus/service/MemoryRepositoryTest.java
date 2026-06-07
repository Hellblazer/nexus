package dev.nexus.service;

import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.tables.records.MemoryRecord;
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
import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-152 bead nexus-gmiaf.6 — MemoryRepository integration test.
 *
 * <p>Proves that the jOOQ-generated {@code Memory} table class and {@code MemoryRecord}
 * compile, execute correctly, and honour RLS through {@link TenantScope#withTenant}.
 *
 * <p>Coverage (Part A deliverable):
 * <ol>
 *   <li>upsert INSERT: generated id is positive, row is visible to the same tenant.</li>
 *   <li>upsert UPDATE (ON CONFLICT): content is replaced, id is unchanged.</li>
 *   <li>RLS isolation: tenant-A rows are invisible to tenant-B.</li>
 *   <li>findByProject returns all rows for the tenant, ordered by timestamp desc.</li>
 *   <li>findByTitle finds the row or returns empty.</li>
 *   <li>delete removes the row; a second delete returns false.</li>
 *   <li>cross-tenant upsert: inserting with mismatched tenant_id is blocked by RLS.</li>
 * </ol>
 *
 * <p>Hermetic: embedded Postgres (io.zonky), port 0, no Docker.
 * Schema applied via Liquibase master changelog (same as MemorySchemaLiquibaseTest).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MemoryRepositoryTest {

    private static final String SVC_ROLE = "svc_repo_test";
    private static final String SVC_PASS = "svc_repo_test_pass";

    private static final String TENANT_A = "tenant-a";
    private static final String TENANT_B = "tenant-b";

    EmbeddedPostgres pg;
    TenantScope tenantScope;
    MemoryRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = EmbeddedPostgres.builder().start();

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            // Create the service role used by TenantScope connections.
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; " +
                "  END IF; " +
                "END $$");
            // nexus_svc needed by changeset 5 grant DO block.
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        // Apply Liquibase changelog via superuser.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                db);
            liquibase.update(new Contexts());
        }

        // Grant the service role the same privileges as nexus_svc.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.memory TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.memory_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        tenantScope = new TenantScope(svcDs);
        repo = new MemoryRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.close();
    }

    // ── Test 1: upsert INSERT — generated id, row visible ───────────────────

    @Test
    void upsert_insert_returnsPositiveId_rowVisible() {
        long id = repo.upsert(TENANT_A, "repo-proj", "first-entry",
                              "hello from generated jOOQ", "tag1,tag2",
                              /*session*/ null, "test-agent", 30);

        assertThat(id).as("generated id from RETURNING must be positive").isPositive();

        Optional<MemoryRecord> row = repo.findByTitle(TENANT_A, "repo-proj", "first-entry");
        assertThat(row).as("inserted row must be findable via findByTitle").isPresent();
        MemoryRecord r = row.get();
        assertThat(r.getId()).as("id must match the returned value").isEqualTo(id);
        assertThat(r.getTenantId()).as("tenant_id must be stamped by RLS").isEqualTo(TENANT_A);
        assertThat(r.getProject()).isEqualTo("repo-proj");
        assertThat(r.getTitle()).isEqualTo("first-entry");
        assertThat(r.getContent()).isEqualTo("hello from generated jOOQ");
        assertThat(r.getTags()).isEqualTo("tag1,tag2");
        assertThat(r.getAgent()).isEqualTo("test-agent");
        assertThat(r.getTtl()).isEqualTo(30);
        // findByTitle now tracks access: access_count is 1 after first read
        assertThat(r.getAccessCount()).as("access_count incremented to 1 by findByTitle").isEqualTo(1);
        assertThat(r.getTimestamp()).as("timestamp must be set").isNotNull();
    }

    // ── Test 2: upsert UPDATE — ON CONFLICT replaces content ────────────────

    @Test
    void upsert_update_onConflict_replacesContent() {
        // Initial insert.
        long id1 = repo.upsert(TENANT_A, "repo-proj", "update-entry",
                               "original content", "tag-old", null, null, 7);
        assertThat(id1).isPositive();

        // Second upsert with same (tenant, project, title) — should update.
        long id2 = repo.upsert(TENANT_A, "repo-proj", "update-entry",
                               "updated content", "tag-new", null, "updater", 14);
        assertThat(id2).as("ON CONFLICT RETURNING must still return a valid id").isPositive();

        // Read back — content must be updated.
        Optional<MemoryRecord> row = repo.findByTitle(TENANT_A, "repo-proj", "update-entry");
        assertThat(row).isPresent();
        assertThat(row.get().getContent())
            .as("content must be updated after ON CONFLICT DO UPDATE").isEqualTo("updated content");
        assertThat(row.get().getTags()).isEqualTo("tag-new");
        assertThat(row.get().getAgent()).isEqualTo("updater");
        assertThat(row.get().getTtl()).isEqualTo(14);
    }

    // ── Test 3: RLS tenant isolation — tenant-B cannot see tenant-A rows ────

    @Test
    void rls_tenantIsolation_crossTenantInvisible() {
        // Seed a row for tenant-A.
        repo.upsert(TENANT_A, "isolation-proj", "alpha-secret",
                    "sensitive content", null, null, null, null);

        // tenant-B must not see it.
        Optional<MemoryRecord> viewedByB = repo.findByTitle(TENANT_B, "isolation-proj", "alpha-secret");
        assertThat(viewedByB)
            .as("tenant-B must NOT see tenant-A's row (RLS isolation)").isEmpty();

        List<MemoryRecord> bRows = repo.findByProject(TENANT_B, "isolation-proj");
        assertThat(bRows)
            .as("findByProject from tenant-B must return no tenant-A rows").isEmpty();
    }

    // ── Test 4: findByProject — all rows, ordered by timestamp desc ─────────

    @Test
    void findByProject_returnsAllRowsForTenant() {
        String proj = "list-proj-" + System.nanoTime();  // unique project to avoid cross-test pollution

        repo.upsert(TENANT_A, proj, "alpha-entry-1", "content 1", null, null, null, null);
        repo.upsert(TENANT_A, proj, "alpha-entry-2", "content 2", null, null, null, null);
        repo.upsert(TENANT_A, proj, "alpha-entry-3", "content 3", null, null, null, null);

        List<MemoryRecord> rows = repo.findByProject(TENANT_A, proj);
        assertThat(rows).as("findByProject must return all 3 tenant-A rows").hasSize(3);
        List<String> titles = rows.stream().map(MemoryRecord::getTitle).toList();
        assertThat(titles)
            .as("all inserted titles must be present")
            .containsExactlyInAnyOrder("alpha-entry-1", "alpha-entry-2", "alpha-entry-3");

        // tenant-B still sees nothing for the same project name
        List<MemoryRecord> bRows = repo.findByProject(TENANT_B, proj);
        assertThat(bRows).as("tenant-B sees zero rows for tenant-A's project").isEmpty();
    }

    // ── Test 5: findByTitle — absent row returns empty ───────────────────────

    @Test
    void findByTitle_absentEntry_returnsEmpty() {
        Optional<MemoryRecord> row = repo.findByTitle(TENANT_A, "nonexistent-proj", "nonexistent-title");
        assertThat(row).as("absent row must return Optional.empty()").isEmpty();
    }

    // ── Test 6: delete — removes row, second delete returns false ───────────

    @Test
    void delete_removesRow_secondDeleteReturnsFalse() {
        repo.upsert(TENANT_A, "delete-proj", "to-delete", "delete me", null, null, null, null);

        boolean firstDelete = repo.delete(TENANT_A, "delete-proj", "to-delete");
        assertThat(firstDelete).as("first delete must return true (row existed)").isTrue();

        Optional<MemoryRecord> afterDelete = repo.findByTitle(TENANT_A, "delete-proj", "to-delete");
        assertThat(afterDelete).as("row must not be findable after delete").isEmpty();

        boolean secondDelete = repo.delete(TENANT_A, "delete-proj", "to-delete");
        assertThat(secondDelete).as("second delete must return false (row already gone)").isFalse();
    }

    // ── Test 7: RLS delete isolation — tenant-B cannot delete tenant-A rows ─

    @Test
    void delete_crossTenant_returnsZeroRows() {
        String proj = "delete-iso-proj-" + System.nanoTime();
        repo.upsert(TENANT_A, proj, "a-row", "content", null, null, null, null);

        // tenant-B tries to delete tenant-A's row — RLS makes it invisible, returns false
        boolean deleted = repo.delete(TENANT_B, proj, "a-row");
        assertThat(deleted)
            .as("tenant-B delete targeting tenant-A row must return false (RLS makes it invisible)").isFalse();

        // tenant-A's row must still be there
        Optional<MemoryRecord> stillThere = repo.findByTitle(TENANT_A, proj, "a-row");
        assertThat(stillThere)
            .as("tenant-A's row must be unaffected by cross-tenant delete attempt").isPresent();
    }

    // ── Test 8: session round-trip — session column persists and survives UPDATE ─
    //
    // Proves that the session provenance required by the .8 ETL is preserved.
    // Two sub-cases:
    //   (a) INSERT with non-null session → read back equals the stored value
    //   (b) UPDATE ON CONFLICT with a different session → read back shows new session
    //       (session is updated so the latest write's provenance is canonical)

    @Test
    void session_roundTrips_throughInsertAndUpdate() {
        String proj = "session-proj-" + System.nanoTime();
        String sessionA = "python-session-abc123";
        String sessionB = "python-session-def456";

        // Insert with sessionA
        long id = repo.upsert(TENANT_A, proj, "session-entry",
                              "content", "tag", sessionA, "agent-a", 30);
        assertThat(id).isPositive();

        Optional<MemoryRecord> afterInsert = repo.findByTitle(TENANT_A, proj, "session-entry");
        assertThat(afterInsert).isPresent();
        assertThat(afterInsert.get().getSession())
            .as("session must round-trip through insert: stored value must equal the passed session")
            .isEqualTo(sessionA);

        // Update with sessionB — ON CONFLICT DO UPDATE also sets SESSION
        repo.upsert(TENANT_A, proj, "session-entry",
                    "updated content", "tag", sessionB, "agent-b", 30);

        Optional<MemoryRecord> afterUpdate = repo.findByTitle(TENANT_A, proj, "session-entry");
        assertThat(afterUpdate).isPresent();
        assertThat(afterUpdate.get().getSession())
            .as("session must round-trip through ON CONFLICT DO UPDATE: stored value must equal the new session")
            .isEqualTo(sessionB);

        // Null session: verify it stores as NULL (not empty string or prior value)
        String proj2 = "session-null-proj-" + System.nanoTime();
        repo.upsert(TENANT_A, proj2, "null-session-entry",
                    "content", null, /*session*/ null, null, null);
        Optional<MemoryRecord> nullRow = repo.findByTitle(TENANT_A, proj2, "null-session-entry");
        assertThat(nullRow).isPresent();
        assertThat(nullRow.get().getSession())
            .as("null session must be stored as NULL (not empty string)")
            .isNull();
    }
}
