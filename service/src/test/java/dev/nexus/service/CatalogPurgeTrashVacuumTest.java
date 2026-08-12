// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
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
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-0ys55 / nexus-tyxnh — {@code purge-trash} execute's post-commit {@code
 * VACUUM (ANALYZE)} step, exercised at the {@link CatalogRepository#purgeTrash}
 * layer against a REAL PostgreSQL 17 testcontainer (house rule: prefer a real round
 * trip over a mock).
 *
 * <p><strong>ASYNC CONTRACT (nexus-tyxnh).</strong> {@code purgeTrash} returns as
 * soon as the DELETE transaction commits — it never waits for the VACUUM sweep. The
 * envelope's {@code vacuum} key is one of {@code "scheduled"} (threshold cleared,
 * submitted to the executor — completion, including any permission skip, is NOT
 * knowable synchronously and lands only in the {@code event=purge_trash_vacuum_*}
 * structured log lines), {@code "already-running"} (single-flight guard rejected a
 * concurrent submission), or {@code "skipped:below-threshold"} (never submitted at
 * all). Tests that need to observe the ACTUAL vacuum outcome (real vs. permission-
 * skipped) poll {@link CatalogRepository#isVacuumInProgressForTests()} — bounded,
 * never a fixed sleep — then assert on {@code pg_stat_user_tables}.
 *
 * <p>Three roles wired against the SAME schema:
 * <ul>
 *   <li>{@code SVC_ROLE} — base purge-trash DML + {@code EXECUTE} on {@code
 *       nexus.purge_trash(interval)}, hand-granted (a throwaway role, not {@code
 *       nexus_svc}). Used for the dry-run-untouched, below-threshold, and
 *       single-flight tests, where VACUUM is either never attempted or the
 *       observable outcome does not depend on MAINTAIN.</li>
 *   <li>{@code nexus_svc} — the REAL production role name, provisioned entirely by
 *       the schema migration itself (this class's {@code @BeforeAll} only CREATEs the
 *       login role; every grant — base DML, {@code EXECUTE} on {@code purge_trash},
 *       and now {@code MAINTAIN} on the five purge-vacuum tables — comes from {@code
 *       grants-nexus-svc.xml}'s {@code grants-nexus-svc-1} and {@code
 *       grants-003-purge-vacuum-maintain} changesets, exactly as it would in a real
 *       deploy). Proves the DEFAULT provisioning path actually vacuums: after the
 *       async sweep completes, PostgreSQL shows a real vacuum (verified
 *       independently via {@code pg_stat_user_tables.last_vacuum}).</li>
 *   <li>{@code SVC_ROLE_NO_MAINTAIN} — same hand-granted base DML as {@code SVC_ROLE}
 *       but deliberately WITHOUT {@code MAINTAIN} — the negative control, standing in
 *       for a substrate that predates the {@code grants-003-purge-vacuum-maintain}
 *       changeset (pre-PG17, or a deploy that has not yet re-run migrations). Proves
 *       {@link TenantScope#vacuumAnalyze} correctly detects PostgreSQL's
 *       warn-and-skip-without-error semantics for a non-owner lacking MAINTAIN: the
 *       sweep is still SCHEDULED (submission does not know about the permission gap
 *       up front) but, after it completes, no real vacuum happened.</li>
 * </ul>
 *
 * <p>Each test uses its own tenant string so the four scenarios (and their differing
 * document counts, chosen to land on either side of {@code VACUUM_THRESHOLD_ROWS=10})
 * never interact.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogPurgeTrashVacuumTest {

    private static final String SVC_ROLE               = "svc_purge_vacuum";
    private static final String SVC_ROLE_NO_MAINTAIN   = "svc_purge_vacuum_nomaint";
    private static final String SVC_PASS               = "svc_purge_vacuum_pass";
    private static final String NEXUS_SVC              = "nexus_svc";
    private static final String NEXUS_SVC_PASS         = "nexus_svc_pass";

    PostgreSQLContainer<?> pg;
    HikariDataSource dsPlain;
    HikariDataSource dsNexusSvc;
    HikariDataSource dsNoMaintain;
    CatalogRepository repoPlain;
    CatalogRepository repoNexusSvc;
    CatalogRepository repoNoMaintain;
    PgVectorRepository vecRepoPlain;
    PgVectorRepository vecRepoNexusSvc;
    PgVectorRepository vecRepoNoMaintain;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String role : List.of(SVC_ROLE, SVC_ROLE_NO_MAINTAIN)) {
                su.createStatement().execute(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + role + "') THEN "
                    + "CREATE ROLE " + role + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            }
            // nexus_svc: ONLY created here, deliberately NOT granted anything by hand —
            // grants-nexus-svc.xml's own changesets provision it below, exactly like a
            // real deploy (that changelog REQUIRES the role to pre-exist and fails loud
            // otherwise; it does not create it).
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + NEXUS_SVC + "') THEN "
                + "CREATE ROLE " + NEXUS_SVC + " LOGIN PASSWORD '" + NEXUS_SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml", new ClassLoaderResourceAccessor(), db);
            liquibase.update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String role : List.of(SVC_ROLE, SVC_ROLE_NO_MAINTAIN)) {
                su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + role);
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + role);
                su.createStatement().execute(
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + role);
                su.createStatement().execute(
                    "GRANT EXECUTE ON FUNCTION nexus.purge_trash(interval) TO " + role);
                su.createStatement().execute("ALTER ROLE " + role + " SET search_path TO nexus, public");
            }
            // nexus_svc gets NO manual grants here — grants-nexus-svc-1 (base DML,
            // EXECUTE on purge_trash) and grants-003-purge-vacuum-maintain (MAINTAIN on
            // the five purge-vacuum tables) already ran as part of the migration above,
            // exactly as they would against a real deploy. Only search_path, the one
            // thing every other test class's connecting role also sets and which no
            // changeset can set for a role it does not create.
            su.createStatement().execute("ALTER ROLE " + NEXUS_SVC + " SET search_path TO nexus, public");
        }

        dsPlain      = pooledDataSource(SVC_ROLE, SVC_PASS);
        dsNexusSvc   = pooledDataSource(NEXUS_SVC, NEXUS_SVC_PASS);
        dsNoMaintain = pooledDataSource(SVC_ROLE_NO_MAINTAIN, SVC_PASS);

        var tenantScopePlain      = new TenantScope(dsPlain);
        var tenantScopeNexusSvc   = new TenantScope(dsNexusSvc);
        var tenantScopeNoMaintain = new TenantScope(dsNoMaintain);

        repoPlain      = new CatalogRepository(tenantScopePlain);
        repoNexusSvc   = new CatalogRepository(tenantScopeNexusSvc);
        repoNoMaintain = new CatalogRepository(tenantScopeNoMaintain);

        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        vecRepoPlain      = new PgVectorRepository(tenantScopePlain, embedder, embedder);
        vecRepoNexusSvc   = new PgVectorRepository(tenantScopeNexusSvc, embedder, embedder);
        vecRepoNoMaintain = new PgVectorRepository(tenantScopeNoMaintain, embedder, embedder);
    }

    @AfterAll
    void stopAll() {
        if (repoPlain != null) repoPlain.close();
        if (repoNexusSvc != null) repoNexusSvc.close();
        if (repoNoMaintain != null) repoNoMaintain.close();
        if (dsPlain != null) dsPlain.close();
        if (dsNexusSvc != null) dsNexusSvc.close();
        if (dsNoMaintain != null) dsNoMaintain.close();
        if (pg != null) pg.stop();
    }

    private HikariDataSource pooledDataSource(String role, String password) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(role);
        cfg.setPassword(password);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    /**
     * Seeds {@code count} aged (60-day-old) tombstoned documents, each with one
     * manifest row and one {@code chunks_384} row, under {@code tenant}/{@code
     * collection}. Returns the resulting rows-affected total a purge-trash execute
     * call would report (count docs purged + count chunks stranded — both dims other
     * than 384 stay at zero for this fixture).
     */
    private long seedAgedTombstones(CatalogRepository repo, PgVectorRepository vecRepo,
                                     String tenant, String collection, int count) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + tenant + "', '"
                + collection + "') ON CONFLICT DO NOTHING");
        }

        List<String> docIds = new ArrayList<>();
        List<String> chashes = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < count; i++) {
            String docId = tenant + "-doc-" + i;
            String chash = Chash.ofText(tenant + "-chunk-" + i).toHex();
            docIds.add(docId);
            chashes.add(chash);
            texts.add("text " + i);
            metas.add(Map.of());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String docId : docIds) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) VALUES ('"
                    + tenant + "', '" + docId + "', '" + docId + "', '" + collection + "')");
            }
            // nexus-7nrvr: catalog_document_chunks.collection is NOT NULL
            // (catalog-025-collection-not-null.xml) — the document already
            // carries a real physical_collection above, so stamp the
            // manifest row with the SAME collection rather than relying on
            // the no-longer-existent NULL default.
            for (int i = 0; i < count; i++) {
                try (PreparedStatement ps = su.prepareStatement(
                        "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
                        + "VALUES (?, ?, 0, decode(?, 'hex'), ?)")) {
                    ps.setString(1, tenant);
                    ps.setString(2, docIds.get(i));
                    ps.setString(3, chashes.get(i));
                    ps.setString(4, collection);
                    ps.execute();
                }
            }
        }

        vecRepo.upsertChunks(tenant, collection, chashes, texts, metas);

        for (String docId : docIds) {
            assertThat(repo.deleteDocument(tenant, docId)).isEqualTo(1);
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = NOW() - interval '60 days' "
                + "WHERE tenant_id = '" + tenant + "'");
        }

        // documents_purged == count, chunks_384_stranded == count (each doc's one chunk
        // has no other live manifest reference) -> total rows-affected == 2*count.
        return 2L * count;
    }

    // ── dry-run must be COMPLETELY untouched by this bead ───────────────────────────

    @Test
    void dryRunPreview_carriesNoVacuumFields() {
        Map<String, Object> result = repoPlain.purgeTrashPreview("vacuum-dryrun-tenant", 30);

        assertThat(result.keySet())
            .as("purge-trash preview's envelope shape must be identical to before nexus-0ys55")
            .containsExactlyInAnyOrder(
                "dry_run", "documents_purged",
                "chunks_384_stranded", "chunks_768_stranded", "chunks_1024_stranded");
        assertThat(result).doesNotContainKey("vacuum");
        assertThat(result).doesNotContainKey("vacuumed");
        assertThat(result).doesNotContainKey("skipped_reason");
    }

    // ── below threshold: VACUUM never submitted, no async work at all ──────────────

    @Test
    void execute_belowThreshold_skipsVacuum_reportsReason() throws Exception {
        String tenant = "vacuum-below-threshold";
        String collection = "knowledge__" + tenant + "__minilm-l6-v2-384__v1";
        // 2 docs -> 2 purged + 2 stranded chunks = 4 rows-affected, well under the 10-row gate.
        seedAgedTombstones(repoPlain, vecRepoPlain, tenant, collection, 2);

        Map<String, Object> result = repoPlain.purgeTrash(tenant, 30);

        assertThat(((Number) result.get("documents_purged")).longValue()).isEqualTo(2L);
        assertThat(result.get("vacuum")).isEqualTo("skipped:below-threshold");
        assertThat((String) result.get("skipped_reason"))
            .as("must name the threshold gate, not a permission or execution failure")
            .contains("below threshold");
        assertThat(repoPlain.isVacuumInProgressForTests())
            .as("a below-threshold call must never submit anything to the vacuum executor")
            .isFalse();
    }

    // ── above threshold, DEFAULT nexus_svc provisioning: real VACUUM round trip ────

    @Test
    void execute_aboveThreshold_defaultNexusSvcRole_reallyVacuums() throws Exception {
        String tenant = "vacuum-above-threshold-nexus-svc";
        String collection = "knowledge__" + tenant + "__minilm-l6-v2-384__v1";
        // 6 docs -> 6 purged + 6 stranded chunks = 12 rows-affected, clears the 10-row gate.
        long expectedRows = seedAgedTombstones(repoNexusSvc, vecRepoNexusSvc, tenant, collection, 6);
        assertThat(expectedRows).isEqualTo(12L);

        resetVacuumStats("nexus.chunks_384");

        Map<String, Object> result = repoNexusSvc.purgeTrash(tenant, 30);

        // The synchronous response: the request thread must never block on the vacuum.
        assertThat(((Number) result.get("documents_purged")).longValue()).isEqualTo(6L);
        assertThat(result.get("vacuum"))
            .as("threshold cleared, no vacuum already in flight on this instance -> submitted, "
                + "reported honestly as scheduled rather than a synchronous vacuumed=true the "
                + "request thread cannot actually know yet")
            .isEqualTo("scheduled");
        assertThat(result.get("skipped_reason")).isNull();

        awaitVacuumIdle(repoNexusSvc);

        // Independent verification via PostgreSQL's own stats, not just our own return
        // value: last_vacuum (VACUUM ANALYZE bumps both last_vacuum and last_analyze)
        // must now be populated for chunks_384, the table this fixture actually wrote to.
        // nexus_svc, provisioned ONLY by grants-nexus-svc.xml's own changesets
        // (grants-003-purge-vacuum-maintain grants MAINTAIN on all five tables), so
        // once the async sweep completes it must be a real vacuum, not a skip -- this
        // is the DEFAULT production path, not a hand-crafted test-only role.
        assertThat(lastVacuumIsRecent("nexus.chunks_384"))
            .as("pg_stat_user_tables.last_vacuum must reflect a REAL vacuum ran on the "
                + "vacuum executor, not merely that submission returned without throwing")
            .isTrue();
    }

    // ── above threshold, MAINTAIN NOT granted: scheduled, but never actually vacuums ─

    @Test
    void execute_aboveThreshold_withoutMaintainGrant_schedulesButNeverActuallyVacuums() throws Exception {
        String tenant = "vacuum-above-threshold-nomaint";
        String collection = "knowledge__" + tenant + "__minilm-l6-v2-384__v1";
        long expectedRows = seedAgedTombstones(repoNoMaintain, vecRepoNoMaintain, tenant, collection, 6);
        assertThat(expectedRows).isEqualTo(12L);

        resetVacuumStats("nexus.chunks_384");

        Map<String, Object> result = repoNoMaintain.purgeTrash(tenant, 30);

        // Submission itself cannot know about the permission gap -- it only shows up
        // once the sweep actually runs on the executor -- so the synchronous response
        // is IDENTICAL to the happy path: scheduled, not a synchronous skip.
        assertThat(((Number) result.get("documents_purged")).longValue()).isEqualTo(6L);
        assertThat(result.get("vacuum")).isEqualTo("scheduled");
        assertThat(result.get("skipped_reason")).isNull();

        awaitVacuumIdle(repoNoMaintain);

        // SVC_ROLE_NO_MAINTAIN owns none of the five tables and holds no MAINTAIN grant
        // -- PostgreSQL warns-and-skips every VACUUM statement without throwing, so
        // after the async sweep completes, last_vacuum must still be absent. The
        // permission-skip detail itself (TenantScope.TableVacuumResult#detail(), the
        // getWarnings() text) is NOT re-asserted here -- it is only ever visible in the
        // event=purge_trash_vacuum_complete log line the executor writes, which
        // TenantScopeVacuumAllowlistTest / the vacuumAnalyze javadoc already cover at
        // the primitive level; this test's job is proving the ENVELOPE never lies about
        // a silent no-op, which the last_vacuum absence below does directly.
        assertThat(lastVacuumIsRecent("nexus.chunks_384"))
            .as("no MAINTAIN grant -> PostgreSQL's warn-and-skip must leave last_vacuum "
                + "absent even though the envelope said 'scheduled' -- the envelope must "
                + "never claim a vacuum happened that did not")
            .isFalse();
    }

    // ── single-flight: a second call while a vacuum is in flight must not enqueue ──

    @Test
    void execute_whileVacuumAlreadyInProgress_reportsAlreadyRunning_doesNotSubmitSecondSweep()
            throws Exception {
        String tenant = "vacuum-already-running";
        String collection = "knowledge__" + tenant + "__minilm-l6-v2-384__v1";
        long expectedRows = seedAgedTombstones(repoPlain, vecRepoPlain, tenant, collection, 6);
        assertThat(expectedRows).isEqualTo(12L);

        // Force the single-flight guard's state directly (deterministic -- see
        // CatalogRepository#setVacuumInProgressForTests javadoc for why this beats a
        // genuinely slow VACUUM here) rather than racing a real in-flight sweep.
        repoPlain.setVacuumInProgressForTests(true);
        try {
            Map<String, Object> result = repoPlain.purgeTrash(tenant, 30);

            // The underlying purge itself is unaffected by the vacuum guard -- only the
            // post-commit maintenance step is gated.
            assertThat(((Number) result.get("documents_purged")).longValue()).isEqualTo(6L);
            assertThat(result.get("vacuum"))
                .as("a vacuum already in flight on this instance must reject the second "
                    + "call's submission immediately, not queue behind the first")
                .isEqualTo("already-running");
            assertThat((String) result.get("skipped_reason"))
                .contains("already in progress");
        } finally {
            // Reset -- forcing this true must not wedge every subsequent test in this
            // class into reporting already-running forever.
            repoPlain.setVacuumInProgressForTests(false);
        }
    }

    /**
     * Bounded poll for {@link CatalogRepository#isVacuumInProgressForTests()} to go
     * false — never a fixed sleep. The vacuum executor is single-threaded and this
     * fixture's VACUUM targets are tiny, so completion is normally sub-second; 10s
     * gives ample headroom without risking a flaky false-negative under load.
     */
    private void awaitVacuumIdle(CatalogRepository repo) throws InterruptedException {
        Instant deadline = Instant.now().plus(Duration.ofSeconds(10));
        while (repo.isVacuumInProgressForTests()) {
            if (Instant.now().isAfter(deadline)) {
                throw new AssertionError("vacuum did not complete within the 10s test budget");
            }
            Thread.sleep(20);
        }
    }

    private void resetVacuumStats(String qualifiedTable) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            String[] parts = qualifiedTable.split("\\.", 2);
            su.createStatement().execute(
                "SELECT pg_stat_reset_single_table_counters(c.oid) FROM pg_class c "
                + "JOIN pg_namespace n ON n.oid = c.relnamespace "
                + "WHERE n.nspname = '" + parts[0] + "' AND c.relname = '" + parts[1] + "'");
        }
    }

    private boolean lastVacuumIsRecent(String qualifiedTable) throws Exception {
        try (Connection su = pg.createConnection("")) {
            String[] parts = qualifiedTable.split("\\.", 2);
            PreparedStatement ps = su.prepareStatement(
                "SELECT last_vacuum, last_analyze FROM pg_stat_user_tables "
                + "WHERE schemaname = ? AND relname = ?");
            ps.setString(1, parts[0]);
            ps.setString(2, parts[1]);
            var rs = ps.executeQuery();
            if (!rs.next()) return false;
            return rs.getTimestamp("last_vacuum") != null && rs.getTimestamp("last_analyze") != null;
        }
    }
}
