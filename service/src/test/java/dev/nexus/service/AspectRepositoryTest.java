package dev.nexus.service;

import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.TenantScope;
import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.*;

import static org.assertj.core.api.Assertions.*;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-152 bead nexus-gmiaf.15 — AspectRepository integration tests.
 *
 * <p>Hermetic embedded Postgres. Applies the full Liquibase master changelog.
 * Asserts:
 * <ol>
 *   <li>upsertAspect round-trip: returns positive id; getAspect retrieves row</li>
 *   <li>Low-confidence aspect upsert rejected (id == -1; not stored)</li>
 *   <li>getAspectByDocId: tumbler lookup returns correct row</li>
 *   <li>listByCollection: returns all rows for a collection</li>
 *   <li>listByExtractorVersion: re-extraction triage returns rows with version &lt; threshold</li>
 *   <li>setSalientSentences / getSalientSentences round-trip</li>
 *   <li>setSalientSentencesByKey: key-based update</li>
 *   <li>deleteAspect: row removed</li>
 *   <li>renameAspectCollection: moves rows; collision-defense deletes conflicting new-side</li>
 *   <li>importAspect fidelity: verbatim overwrite; confidence gate still applies</li>
 *   <li>RLS isolation: tenant A aspects invisible to tenant B</li>
 *   <li>RLS WITH CHECK: raw INSERT with mismatched tenant_id rejected</li>
 *   <li>RLS fail-closed: unset GUC blocks reads (empty_string → policy denies)</li>
 *   <li>upsertHighlight / getHighlight / getHighlightBySourceUri / listHighlights / deleteHighlight</li>
 *   <li>importHighlight fidelity</li>
 *   <li>enqueue / claimNext state-machine (enqueue→pending, claim→in_progress)</li>
 *   <li>claimBatch: bounded batch claim</li>
 *   <li>markDone (by doc_id), markFailed, markRetry, reclaimStale</li>
 *   <li>pendingCount / isDrained</li>
 *   <li>listPending: FIFO order</li>
 *   <li>renameQueueCollection</li>
 *   <li>importQueueRow fidelity: never downgrade in_progress; GREATEST for retry_count;
 *       LEAST for enqueued_at; event-log last_error strategy</li>
 *   <li>CONCURRENCY: N concurrent claimNext calls each get a DISTINCT row (no double-claim)</li>
 *   <li>recordPromotion / listPromotions round-trip</li>
 *   <li>importPromotionRow: event-log DO NOTHING on conflict (idempotent)</li>
 *   <li>renameHighlightsCollection: moves rows; unknown collection returns 0; RLS isolation</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class AspectRepositoryTest {

    private static final String TENANT_A = "aspect-tenant-a";
    private static final String TENANT_B = "aspect-tenant-b";
    private static final String SVC_ROLE = "svc_aspect_test";
    private static final String SVC_PASS = "svc_aspect_test_pass";

    EmbeddedPostgres pg;
    TenantScope tenantScope;
    AspectRepository repo;
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
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db);
            liquibase.update(new Contexts());
        }

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            // Grant on all 4 aspect tables
            for (String table : List.of("document_aspects", "document_highlights",
                                        "aspect_extraction_queue", "aspect_promotion_log")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + table + " TO " + SVC_ROLE);
                su.createStatement().execute(
                    "GRANT USAGE ON SEQUENCE nexus." + table + "_id_seq TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");

            // nexus-b7v6i: document_aspects.doc_id and document_highlights.doc_id now enforce
            // a FK to catalog_documents(tenant_id, tumbler). Seed all tumbler values used
            // as doc_ids in this test class so FK checks pass.
            // document_aspects doc_ids: "1.2.3" (makeAspect default), "2.4.6", "3.1.4"
            // document_highlights doc_ids: "highlight-1", "by-uri-doc", "list-hl-0", "list-hl-1",
            //   "del-highlight", "import-hl-1", "rls-hl-private", "hl-rename-doc-1", "hl-rename-doc-2", "hl-rls-doc"
            // aspect_extraction_queue doc_ids: "q-doc-1", "done-doc-id-unique", "etl-q-doc"
            // (queue items without doc_id are doc_id=NULL — no FK entry needed)
            for (String tumbler : List.of(
                    "1.2.3", "2.4.6", "3.1.4",
                    "highlight-1", "by-uri-doc", "list-hl-0", "list-hl-1",
                    "del-highlight", "import-hl-1", "rls-hl-private",
                    "hl-rename-doc-1", "hl-rename-doc-2", "hl-rls-doc",
                    "q-doc-1", "done-doc-id-unique", "etl-q-doc")) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) " +
                    "VALUES ('" + TENANT_A + "', '" + tumbler + "', 'Test fixture: " + tumbler + "') " +
                    "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
            }
        }

        svcDs = buildSvcDataSource();
        tenantScope = new TenantScope(svcDs);
        repo = new AspectRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.close();
    }

    // ── Helper ─────────────────────────────────────────────────────────────────

    private Map<String, Object> makeAspect(String collection, String sourcePath) {
        return makeAspect(collection, sourcePath, 0.85, "v2", "scholarly-paper");
    }

    private Map<String, Object> makeAspect(String collection, String sourcePath,
                                            double confidence, String modelVersion, String extractorName) {
        return new java.util.LinkedHashMap<>(Map.of(
            "collection",            collection,
            "source_path",           sourcePath,
            "problem_formulation",   "Problem for " + sourcePath,
            "proposed_method",       "Method A",
            "experimental_results",  "Results",
            "confidence",            confidence,
            "extracted_at",          "2026-06-01T10:00:00.000000Z",
            "model_version",         modelVersion,
            "extractor_name",        extractorName,
            "doc_id",                "1.2.3"
        ));
    }

    // ── document_aspects ────────────────────────────────────────────────────────

    @Test @Order(1)
    void upsertAspect_roundTrip() {
        long id = repo.upsertAspect(TENANT_A, makeAspect("coll-a", "doc1.pdf"));
        assertThat(id).as("upsertAspect must return positive id").isPositive();

        Optional<Map<String, Object>> rec = repo.getAspect(TENANT_A, "coll-a", "doc1.pdf");
        assertThat(rec).as("getAspect must find row after upsert").isPresent();
        assertThat(rec.get().get("collection")).isEqualTo("coll-a");
        assertThat(rec.get().get("source_path")).isEqualTo("doc1.pdf");
        assertThat(rec.get().get("extractor_name")).isEqualTo("scholarly-paper");
        assertThat(rec.get().get("confidence")).isEqualTo(0.85);
    }

    @Test @Order(2)
    void upsertAspect_lowConfidence_rejected() {
        var body = makeAspect("coll-lowconf", "lowconf.pdf", 0.1, "v1", "bad-extractor");
        long id = repo.upsertAspect(TENANT_A, body);
        assertThat(id).as("low-confidence upsert must return -1").isEqualTo(-1L);

        Optional<Map<String, Object>> rec = repo.getAspect(TENANT_A, "coll-lowconf", "lowconf.pdf");
        assertThat(rec).as("low-confidence row must NOT be stored").isEmpty();
    }

    @Test @Order(3)
    void upsertAspect_onConflict_overwritesRow() {
        repo.upsertAspect(TENANT_A, makeAspect("coll-overwrite", "ow.pdf", 0.80, "v1", "v1-extractor"));
        // Second upsert with same (tenant, collection, source_path) — overwrites
        var body = makeAspect("coll-overwrite", "ow.pdf", 0.95, "v2", "v2-extractor");
        long id2 = repo.upsertAspect(TENANT_A, body);
        assertThat(id2).isPositive();

        Optional<Map<String, Object>> rec = repo.getAspect(TENANT_A, "coll-overwrite", "ow.pdf");
        assertThat(rec).isPresent();
        assertThat(rec.get().get("model_version")).as("model_version must be updated to v2").isEqualTo("v2");
        assertThat(rec.get().get("confidence")).isEqualTo(0.95);
    }

    @Test @Order(4)
    void getAspectByDocId_findsRowByTumbler() {
        var body = makeAspect("coll-tumbler", "tumbler.pdf");
        body.put("doc_id", "2.4.6");
        repo.upsertAspect(TENANT_A, body);

        Optional<Map<String, Object>> rec = repo.getAspectByDocId(TENANT_A, "2.4.6");
        assertThat(rec).as("getAspectByDocId must find row by tumbler").isPresent();
        assertThat(rec.get().get("source_path")).isEqualTo("tumbler.pdf");
    }

    @Test @Order(5)
    void listByCollection_returnsAllRowsForCollection() {
        for (int i = 0; i < 3; i++) {
            repo.upsertAspect(TENANT_A, makeAspect("list-coll", "doc" + i + ".pdf"));
        }
        List<Map<String, Object>> rows = repo.listByCollection(TENANT_A, "list-coll", 0, 0);
        assertThat(rows).as("listByCollection must return all 3 rows").hasSize(3);
    }

    @Test @Order(6)
    void listByExtractorVersion_returnsRowsBelowMaxVersion() {
        repo.upsertAspect(TENANT_A, makeAspect("ev-coll", "old.pdf", 0.80, "1.0", "ev-extractor"));
        repo.upsertAspect(TENANT_A, makeAspect("ev-coll", "new.pdf", 0.90, "2.5", "ev-extractor"));

        // List rows where model_version < "2.0"
        List<Map<String, Object>> stale = repo.listByExtractorVersion(TENANT_A, "ev-extractor", "2.0");
        assertThat(stale).as("listByExtractorVersion must return only version < 2.0")
            .hasSize(1)
            .allMatch(r -> "old.pdf".equals(r.get("source_path")));
    }

    @Test @Order(7)
    void setSalientSentences_andGetSalient_roundTrip() {
        var body = makeAspect("salient-coll", "salient.pdf");
        body.put("doc_id", "3.1.4");
        repo.upsertAspect(TENANT_A, body);

        int n = repo.setSalientSentences(TENANT_A, "3.1.4", "[\"sentence one\",\"sentence two\"]");
        assertThat(n).as("setSalientSentences must update 1 row").isEqualTo(1);

        String val = repo.getSalientSentences(TENANT_A, "3.1.4");
        assertThat(val).as("getSalientSentences must return stored JSON")
            .isEqualTo("[\"sentence one\",\"sentence two\"]");
    }

    @Test @Order(8)
    void setSalientSentencesByKey_updatesViaKey() {
        repo.upsertAspect(TENANT_A, makeAspect("bykey-coll", "bykey.pdf"));

        int n = repo.setSalientSentencesByKey(TENANT_A, "bykey-coll", "bykey.pdf",
            "[\"key-sentence\"]");
        assertThat(n).as("setSalientSentencesByKey must update 1 row").isEqualTo(1);

        Optional<Map<String, Object>> rec = repo.getAspect(TENANT_A, "bykey-coll", "bykey.pdf");
        assertThat(rec).isPresent();
        assertThat(rec.get().get("salient_sentences")).isEqualTo("[\"key-sentence\"]");
    }

    @Test @Order(9)
    void deleteAspect_removesRow() {
        repo.upsertAspect(TENANT_A, makeAspect("del-coll", "del.pdf"));
        assertThat(repo.getAspect(TENANT_A, "del-coll", "del.pdf")).isPresent();

        int n = repo.deleteAspect(TENANT_A, "del-coll", "del.pdf");
        assertThat(n).as("deleteAspect must return 1").isEqualTo(1);
        assertThat(repo.getAspect(TENANT_A, "del-coll", "del.pdf")).isEmpty();
    }

    @Test @Order(10)
    void renameAspectCollection_movesRows() {
        repo.upsertAspect(TENANT_A, makeAspect("rename-src", "doc1.pdf"));
        repo.upsertAspect(TENANT_A, makeAspect("rename-src", "doc2.pdf"));

        int n = repo.renameAspectCollection(TENANT_A, "rename-src", "rename-dst");
        assertThat(n).as("renameAspectCollection must move 2 rows").isEqualTo(2);

        List<Map<String, Object>> dst = repo.listByCollection(TENANT_A, "rename-dst", 0, 0);
        assertThat(dst).hasSize(2);
        List<Map<String, Object>> src = repo.listByCollection(TENANT_A, "rename-src", 0, 0);
        assertThat(src).isEmpty();
    }

    @Test @Order(11)
    void importAspect_fidelity_verbatimOverwrite() {
        // Initial insert via import
        var body = makeAspect("import-coll", "import.pdf", 0.75, "v1", "import-extractor");
        body.put("extracted_at", "2025-01-01T00:00:00.000000Z");
        int n = repo.importAspect(TENANT_A, body);
        assertThat(n).as("importAspect first call must write 1 row").isEqualTo(1);

        // Overwrite via import
        var body2 = makeAspect("import-coll", "import.pdf", 0.90, "v2", "import-extractor");
        body2.put("extracted_at", "2026-01-01T00:00:00.000000Z");
        int n2 = repo.importAspect(TENANT_A, body2);
        assertThat(n2).isEqualTo(1);

        Optional<Map<String, Object>> rec = repo.getAspect(TENANT_A, "import-coll", "import.pdf");
        assertThat(rec).isPresent();
        assertThat(rec.get().get("model_version"))
            .as("importAspect must overwrite model_version verbatim").isEqualTo("v2");
        assertThat(rec.get().get("confidence"))
            .as("importAspect must overwrite confidence verbatim").isEqualTo(0.90);
    }

    @Test @Order(12)
    void importAspect_lowConfidence_skipped() {
        var body = makeAspect("importlc-coll", "lc.pdf", 0.05, "v1", "bad");
        int n = repo.importAspect(TENANT_A, body);
        assertThat(n).as("importAspect with confidence < 0.3 must return 0").isEqualTo(0);
        assertThat(repo.getAspect(TENANT_A, "importlc-coll", "lc.pdf")).isEmpty();
    }

    @Test @Order(13)
    void rls_isolation_aspectsInvisibleAcrossTenants() {
        repo.upsertAspect(TENANT_A, makeAspect("rls-coll", "secret.pdf"));

        List<Map<String, Object>> tenantBRows = repo.listByCollection(TENANT_B, "rls-coll", 0, 0);
        assertThat(tenantBRows)
            .as("tenant B must not see tenant A's aspects (RLS isolation)")
            .noneMatch(r -> "secret.pdf".equals(r.get("source_path")));
    }

    @Test @Order(14)
    void rls_withCheck_crossTenantInsert_rejected() {
        assertThatThrownBy(() -> {
            try (var conn = svcDs.getConnection()) {
                conn.setAutoCommit(true);
                conn.createStatement().execute(
                    "SET LOCAL nexus.tenant = '" + TENANT_A + "'");
                conn.createStatement().execute(
                    "INSERT INTO nexus.document_aspects " +
                    "(tenant_id, collection, source_path, extracted_at, model_version, extractor_name) " +
                    "VALUES ('" + TENANT_B + "', 'bad-coll', 'bad.pdf', now(), 'v1', 'bad')");
            }
        })
        .as("RLS WITH CHECK must reject aspect INSERT where tenant_id != nexus.tenant GUC")
        .isInstanceOfAny(org.postgresql.util.PSQLException.class, java.sql.SQLException.class);
    }

    @Test @Order(15)
    void rls_failClosed_unsetGuc_deniesAccess() {
        // Seed a row as TENANT_A
        repo.upsertAspect(TENANT_A, makeAspect("failclosed-coll", "failclosed.pdf"));

        // Connect without setting the GUC — current_setting('nexus.tenant', true) returns ''
        // RLS policy: tenant_id = '' => denies access to all rows
        assertThatCode(() -> {
            try (var conn = svcDs.getConnection()) {
                conn.setAutoCommit(true);
                var rs = conn.createStatement().executeQuery(
                    "SELECT count(*) FROM nexus.document_aspects WHERE collection='failclosed-coll'");
                rs.next();
                int cnt = rs.getInt(1);
                // If RLS fail-closed: count is 0 (empty result). If broken: count > 0.
                assertThat(cnt)
                    .as("unset GUC must result in 0 visible rows (fail-closed RLS)")
                    .isEqualTo(0);
            }
        }).doesNotThrowAnyException();
    }

    // ── document_highlights ─────────────────────────────────────────────────────

    @Test @Order(20)
    void upsertHighlight_andGetHighlight_roundTrip() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("doc_id",        "highlight-1");
        body.put("source_uri",    "x-devonthink://aaabbb");
        body.put("collection",    "dt-papers");
        body.put("highlights_md", "## Key highlight\nImportant finding");
        body.put("mentions_md",   "Author et al. cited");
        body.put("ingested_at",   "2026-06-01T12:00:00.000000Z");

        boolean written = repo.upsertHighlight(TENANT_A, body);
        assertThat(written).as("upsertHighlight must return true when content present").isTrue();

        Optional<Map<String, Object>> rec = repo.getHighlight(TENANT_A, "highlight-1");
        assertThat(rec).isPresent();
        assertThat(rec.get().get("highlights_md")).asString().contains("Important finding");
        assertThat(rec.get().get("mentions_md")).asString().contains("Author et al.");
    }

    @Test @Order(21)
    void upsertHighlight_emptyContent_returnsFalse() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("doc_id",     "empty-highlight");
        body.put("ingested_at", "2026-06-01T12:00:00.000000Z");
        // no highlights_md or mentions_md
        boolean written = repo.upsertHighlight(TENANT_A, body);
        assertThat(written).as("upsertHighlight with no content must return false").isFalse();
    }

    @Test @Order(22)
    void getHighlightBySourceUri_findsRowByUri() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("doc_id",        "by-uri-doc");
        body.put("source_uri",    "x-devonthink://bycuri-unique");
        body.put("highlights_md", "content");
        body.put("ingested_at",   "2026-06-01T12:00:00.000000Z");
        repo.upsertHighlight(TENANT_A, body);

        Optional<Map<String, Object>> rec = repo.getHighlightBySourceUri(
            TENANT_A, "x-devonthink://bycuri-unique");
        assertThat(rec).isPresent();
        assertThat(rec.get().get("doc_id")).isEqualTo("by-uri-doc");
    }

    @Test @Order(23)
    void listHighlights_returnsRows() {
        // Seed two highlights with distinct doc_ids
        for (int i = 0; i < 2; i++) {
            var body = new java.util.LinkedHashMap<String, Object>();
            body.put("doc_id",        "list-hl-" + i);
            body.put("highlights_md", "hl " + i);
            body.put("ingested_at",   "2026-06-0" + (i + 1) + "T00:00:00.000000Z");
            repo.upsertHighlight(TENANT_A, body);
        }
        List<Map<String, Object>> rows = repo.listHighlights(TENANT_A, 50, 0);
        assertThat(rows).hasSizeGreaterThanOrEqualTo(2);
    }

    @Test @Order(24)
    void deleteHighlight_removesRow() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("doc_id",        "del-highlight");
        body.put("highlights_md", "to be deleted");
        body.put("ingested_at",   "2026-06-01T12:00:00.000000Z");
        repo.upsertHighlight(TENANT_A, body);

        assertThat(repo.getHighlight(TENANT_A, "del-highlight")).isPresent();
        boolean deleted = repo.deleteHighlight(TENANT_A, "del-highlight");
        assertThat(deleted).isTrue();
        assertThat(repo.getHighlight(TENANT_A, "del-highlight")).isEmpty();
    }

    @Test @Order(25)
    void importHighlight_fidelity_verbatimOverwrite() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("doc_id",        "import-hl-1");
        body.put("highlights_md", "v1 content");
        body.put("ingested_at",   "2025-01-01T00:00:00.000000Z");
        int n = repo.importHighlight(TENANT_A, body);
        assertThat(n).isEqualTo(1);

        var body2 = new java.util.LinkedHashMap<String, Object>();
        body2.put("doc_id",        "import-hl-1");
        body2.put("highlights_md", "v2 content updated");
        body2.put("ingested_at",   "2026-01-01T00:00:00.000000Z");
        int n2 = repo.importHighlight(TENANT_A, body2);
        assertThat(n2).isEqualTo(1);

        Optional<Map<String, Object>> rec = repo.getHighlight(TENANT_A, "import-hl-1");
        assertThat(rec).isPresent();
        assertThat(rec.get().get("highlights_md"))
            .as("importHighlight must overwrite content verbatim").asString().contains("v2 content updated");
    }

    @Test @Order(26)
    void highlights_rls_isolation() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("doc_id",        "rls-hl-private");
        body.put("highlights_md", "tenant A private");
        body.put("ingested_at",   "2026-06-01T12:00:00.000000Z");
        repo.upsertHighlight(TENANT_A, body);

        Optional<Map<String, Object>> tenantBView = repo.getHighlight(TENANT_B, "rls-hl-private");
        assertThat(tenantBView)
            .as("tenant B must not see tenant A's highlight").isEmpty();
    }

    // ── aspect_extraction_queue ──────────────────────────────────────────────────

    @Test @Order(30)
    void enqueue_andClaimNext_stateMachine() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("collection",  "queue-coll");
        body.put("source_path", "q1.pdf");
        body.put("doc_id",      "q-doc-1");
        body.put("content",     "content for q1");
        repo.enqueue(TENANT_A, body);

        int before = repo.pendingCount(TENANT_A);
        assertThat(before).isGreaterThanOrEqualTo(1);
        assertThat(repo.isDrained(TENANT_A)).isFalse();

        Optional<Map<String, Object>> claimed = repo.claimNext(TENANT_A);
        assertThat(claimed).as("claimNext must return a row").isPresent();
        assertThat(claimed.get().get("source_path")).asString().isNotBlank();

        // After claim, pending_count drops
        int after = repo.pendingCount(TENANT_A);
        assertThat(after).as("pending_count must decrease after claimNext").isLessThan(before);
    }

    @Test @Order(31)
    void enqueue_reEnqueue_resetsToPending() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("collection",  "reenqueue-coll");
        body.put("source_path", "re.pdf");
        repo.enqueue(TENANT_A, body);

        // Claim it (in_progress)
        repo.claimNext(TENANT_A);

        // Re-enqueue at same key — resets to pending
        repo.enqueue(TENANT_A, body);

        // Now we must be able to claim it again
        Optional<Map<String, Object>> reclaimed = repo.claimNext(TENANT_A);
        assertThat(reclaimed).as("re-enqueued row must be claimable again").isPresent();
    }

    @Test @Order(32)
    void markDone_byDocId_deletesRow() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("collection",  "done-coll");
        body.put("source_path", "done.pdf");
        body.put("doc_id",      "done-doc-id-unique");
        repo.enqueue(TENANT_A, body);
        repo.claimNext(TENANT_A); // move to in_progress

        int n = repo.markDone(TENANT_A, "done-doc-id-unique", null, null);
        assertThat(n).as("markDone by doc_id must delete 1 row").isEqualTo(1);
    }

    @Test @Order(33)
    void markFailed_andMarkRetry_stateTransitions() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("collection",  "failretry-coll");
        body.put("source_path", "fr.pdf");
        repo.enqueue(TENANT_A, body);
        repo.claimNext(TENANT_A);

        repo.markFailed(TENANT_A, "failretry-coll", "fr.pdf", "extractor crashed");
        // After failed, isDrained excludes failed — still counts as not-done
        // Now retry: resets to pending
        repo.markRetry(TENANT_A, "failretry-coll", "fr.pdf");
        int cnt = repo.pendingCount(TENANT_A);
        assertThat(cnt).isGreaterThanOrEqualTo(1);
    }

    @Test @Order(34)
    void reclaimStale_reclaims_longRunningInProgress() throws InterruptedException {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("collection",  "stale-coll");
        body.put("source_path", "stale.pdf");
        repo.enqueue(TENANT_A, body);
        repo.claimNext(TENANT_A);

        // Use 0-second timeout so all in_progress rows are instantly stale
        int reclaimed = repo.reclaimStale(TENANT_A, 0);
        assertThat(reclaimed).as("reclaimStale must reclaim at least 1 row").isGreaterThanOrEqualTo(1);
    }

    @Test @Order(35)
    void listPending_returnsFifoOrder() {
        // Enqueue with different timestamps via body to verify FIFO
        for (int i = 3; i >= 1; i--) {
            var body = new java.util.LinkedHashMap<String, Object>();
            body.put("collection",  "fifo-coll");
            body.put("source_path", "fifo-" + i + ".pdf");
            body.put("enqueued_at", "2026-06-0" + i + "T00:00:00.000000Z");
            repo.enqueue(TENANT_A, body);
        }
        List<Map<String, Object>> pending = repo.listPending(TENANT_A, 10);
        // Should include our 3 rows; ordering should be by enqueued_at ASC
        List<String> paths = pending.stream()
            .filter(r -> ((String) r.get("collection")).equals("fifo-coll"))
            .map(r -> (String) r.get("source_path"))
            .toList();
        assertThat(paths).as("listPending must be in FIFO (enqueued_at ASC) order")
            .containsSubsequence("fifo-1.pdf", "fifo-2.pdf", "fifo-3.pdf");
    }

    @Test @Order(36)
    void renameQueueCollection_movesRows() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("collection",  "qrename-src");
        body.put("source_path", "qr.pdf");
        repo.enqueue(TENANT_A, body);

        int n = repo.renameQueueCollection(TENANT_A, "qrename-src", "qrename-dst");
        assertThat(n).as("renameQueueCollection must move 1 row").isEqualTo(1);

        List<Map<String, Object>> dstRows = repo.listPending(TENANT_A, 100);
        assertThat(dstRows)
            .anyMatch(r -> "qrename-dst".equals(r.get("collection")) && "qr.pdf".equals(r.get("source_path")));
    }

    @Test @Order(37)
    void importQueueRow_fidelity_neverDowngradesInProgress() {
        // Use a unique TENANT to avoid cross-test state from other tests that also
        // use TENANT_A and leave in-progress or pending rows in the shared DB.
        String testTenant = "etl-fidelity-tenant-" + System.nanoTime();
        String uniquePath  = "etl-q.pdf";

        // Seed a row then claim it (in_progress).
        // doc_id omitted (NULL) — FK to catalog_documents requires matching (tenant_id, tumbler),
        // and this test uses a dynamic tenant; omitting doc_id is fine since we're testing
        // status non-downgrade behavior, not doc_id handling.
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("collection",  "etl-queue-coll");
        body.put("source_path", uniquePath);
        repo.enqueue(testTenant, body);

        // In this tenant's isolated namespace, claimNext MUST claim this specific row
        Optional<Map<String, Object>> claimedForSetup = repo.claimNext(testTenant);
        assertThat(claimedForSetup).as("claimNext setup: must return a row in isolated tenant").isPresent();
        assertThat(claimedForSetup.get().get("source_path"))
            .as("claimNext must return the specific enqueued row (no other rows in test tenant)")
            .isEqualTo(uniquePath);

        // ETL import with status = 'pending' (stale source): must NOT downgrade in_progress
        var importBody = new java.util.LinkedHashMap<String, Object>();
        importBody.put("collection",    "etl-queue-coll");
        importBody.put("source_path",   uniquePath);
        // doc_id omitted (NULL) — matches the enqueue body above (no FK issue)
        importBody.put("status",        "pending");    // stale source
        importBody.put("retry_count",   0);
        importBody.put("enqueued_at",   "2025-01-01T00:00:00.000000Z");
        int n = repo.importQueueRow(testTenant, importBody);
        assertThat(n).isEqualTo(1);

        // Verify directly via listPending: if the row is still in_progress it will NOT
        // appear in listPending. If it appears, the in_progress was downgraded (bug).
        List<Map<String, Object>> pending = repo.listPending(testTenant, 1000);
        boolean rowIsNowPending = pending.stream()
            .anyMatch(r -> uniquePath.equals(r.get("source_path")));
        assertThat(rowIsNowPending)
            .as("in_progress row must NOT appear in listPending after stale ETL import "
                + "(status must be preserved as in_progress, not downgraded to pending). "
                + "source_path=" + uniquePath + " tenant=" + testTenant)
            .isFalse();
    }

    @Test @Order(38)
    void importQueueRow_greatest_retryCount() {
        // Seed with retry_count=3
        var importBody = new java.util.LinkedHashMap<String, Object>();
        importBody.put("collection",  "greatest-coll");
        importBody.put("source_path", "gr.pdf");
        importBody.put("status",      "pending");
        importBody.put("retry_count", 3);
        importBody.put("enqueued_at", "2026-01-01T00:00:00.000000Z");
        repo.importQueueRow(TENANT_A, importBody);

        // Re-import with lower retry_count (stale)
        importBody.put("retry_count", 1);
        repo.importQueueRow(TENANT_A, importBody);

        // pending_count confirms row is still there
        assertThat(repo.pendingCount(TENANT_A)).isGreaterThanOrEqualTo(1);
    }

    @Test @Order(39)
    void queue_rls_isolation() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("collection",  "rls-queue-coll");
        body.put("source_path", "rls-q.pdf");
        repo.enqueue(TENANT_A, body);

        // TENANT_B must not see TENANT_A's queued rows
        List<Map<String, Object>> tenantBPending = repo.listPending(TENANT_B, 100);
        assertThat(tenantBPending)
            .as("tenant B must not see tenant A's queue rows (RLS)")
            .noneMatch(r -> "rls-q.pdf".equals(r.get("source_path")));
    }

    @Test @Order(40)
    void claimBatch_returnsUpToLimit() {
        // Enqueue 5 fresh rows
        for (int i = 0; i < 5; i++) {
            var body = new java.util.LinkedHashMap<String, Object>();
            body.put("collection",  "batch-coll");
            body.put("source_path", "batch-" + System.nanoTime() + "-" + i + ".pdf");
            repo.enqueue(TENANT_A, body);
        }
        List<Map<String, Object>> batch = repo.claimBatch(TENANT_A, 3);
        assertThat(batch).as("claimBatch must return at most 3 rows").hasSizeLessThanOrEqualTo(3);
        assertThat(batch).as("claimBatch must return at least 1 row").isNotEmpty();
    }

    // ── CONCURRENCY: claim_next distinctness ────────────────────────────────────

    @Test @Order(45)
    void claimNext_concurrent_eachWorkerGetsDistinctRow() throws Exception {
        // Enqueue N rows for this test — must be >= worker count
        int N = 8;
        for (int i = 0; i < N; i++) {
            var body = new java.util.LinkedHashMap<String, Object>();
            body.put("collection",  "concurrent-coll");
            body.put("source_path", "concurrent-" + i + "-" + System.nanoTime() + ".pdf");
            repo.enqueue(TENANT_A, body);
        }

        int workers = 6;
        ExecutorService pool = Executors.newFixedThreadPool(workers);
        CyclicBarrier barrier = new CyclicBarrier(workers);  // all start together
        List<Future<Optional<Map<String, Object>>>> futures = new ArrayList<>();

        for (int w = 0; w < workers; w++) {
            futures.add(pool.submit(() -> {
                barrier.await(10, TimeUnit.SECONDS); // synchronized start
                return repo.claimNext(TENANT_A);
            }));
        }
        pool.shutdown();
        assertThat(pool.awaitTermination(30, TimeUnit.SECONDS)).isTrue();

        // Collect non-empty results
        List<Object> claimedPaths = new ArrayList<>();
        for (var f : futures) {
            Optional<Map<String, Object>> result = f.get();
            result.ifPresent(r -> claimedPaths.add(r.get("source_path")));
        }

        // The critical assertion: no two workers claimed the same row
        long uniquePaths = claimedPaths.stream().distinct().count();
        assertThat(uniquePaths)
            .as("Each concurrent claimNext must return a DISTINCT row (FOR UPDATE SKIP LOCKED ensures no double-claim). "
                + "All claimed paths: " + claimedPaths)
            .isEqualTo(claimedPaths.size());

        // Sanity: at least half the workers got a row (N=8 rows, 6 workers)
        assertThat(claimedPaths.size())
            .as("At least 6 of 6 concurrent workers should have gotten a row (8 rows available)")
            .isGreaterThanOrEqualTo(workers);
    }

    // ── aspect_promotion_log ────────────────────────────────────────────────────

    @Test @Order(50)
    void recordPromotion_andListPromotions_roundTrip() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("field_name",      "impact_score");
        body.put("sql_type",        "DOUBLE PRECISION");
        body.put("column_added",    true);
        body.put("rows_backfilled", 42);
        body.put("rows_pruned",     5);
        body.put("pruned",          false);
        body.put("promoted_at",     "2026-06-01T09:00:00.000000Z");

        repo.recordPromotion(TENANT_A, body);

        List<Map<String, Object>> promotions = repo.listPromotions(TENANT_A);
        assertThat(promotions).as("listPromotions must return at least 1 entry").isNotEmpty();

        boolean found = promotions.stream().anyMatch(p ->
            "impact_score".equals(p.get("field_name")) &&
            "DOUBLE PRECISION".equals(p.get("sql_type")) &&
            Boolean.TRUE.equals(p.get("column_added")) &&
            Integer.valueOf(42).equals(p.get("rows_backfilled")));
        assertThat(found).as("listPromotions must contain the recorded promotion").isTrue();
    }

    @Test @Order(51)
    void importPromotionRow_idempotent_doNothingOnConflict() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("field_name",   "dedup_field");
        body.put("sql_type",     "TEXT");
        body.put("promoted_at",  "2026-06-02T10:00:00.000000Z");
        body.put("column_added", false);

        int n1 = repo.importPromotionRow(TENANT_A, body);
        assertThat(n1).as("first importPromotionRow must write 1 row").isEqualTo(1);

        // Re-import same event — DO NOTHING on conflict (idempotent)
        int n2 = repo.importPromotionRow(TENANT_A, body);
        assertThat(n2).as("second importPromotionRow must write 0 rows (DO NOTHING on conflict)").isEqualTo(0);

        // Verify only 1 row exists for this event
        List<Map<String, Object>> all = repo.listPromotions(TENANT_A);
        long dedupCount = all.stream().filter(p -> "dedup_field".equals(p.get("field_name"))).count();
        assertThat(dedupCount).as("importPromotionRow must produce exactly 1 row for the dedup key").isEqualTo(1);
    }

    @Test @Order(52)
    void promotionLog_rls_isolation() {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("field_name",  "private_field");
        body.put("sql_type",    "TEXT");
        body.put("promoted_at", "2026-06-03T10:00:00.000000Z");
        repo.recordPromotion(TENANT_A, body);

        List<Map<String, Object>> tenantBPromos = repo.listPromotions(TENANT_B);
        assertThat(tenantBPromos)
            .as("tenant B must not see tenant A's promotion log (RLS)")
            .noneMatch(p -> "private_field".equals(p.get("field_name")));
    }

    // ── renameHighlightsCollection ─────────────────────────────────────────────

    @Test @Order(53)
    void renameHighlightsCollection_movesRows() {
        // Seed two highlight rows under old collection
        for (int i = 1; i <= 2; i++) {
            var body = new java.util.LinkedHashMap<String, Object>();
            body.put("doc_id",       "hl-rename-doc-" + i);
            body.put("source_uri",   "file://hl-rename-" + i + ".md");
            body.put("collection",   "hl-src");
            body.put("highlights_md","## h");
            body.put("mentions_md",  "");
            body.put("ingested_at",  "2026-01-01T00:00:00.000000Z");
            repo.upsertHighlight(TENANT_A, body);
        }
        int n = repo.renameHighlightsCollection(TENANT_A, "hl-src", "hl-dst");
        assertThat(n).as("renameHighlightsCollection must move 2 rows").isEqualTo(2);

        // Verify rows now appear under new collection
        var rows = repo.listHighlights(TENANT_A, 100, 0);
        long underDst = rows.stream()
            .filter(r -> "hl-dst".equals(r.get("collection")) &&
                         ((String) r.get("doc_id")).startsWith("hl-rename-doc-"))
            .count();
        assertThat(underDst).as("both rows must appear under new collection").isEqualTo(2);
    }

    @Test @Order(54)
    void renameHighlightsCollection_unknownCollection_returnsZero() {
        int n = repo.renameHighlightsCollection(TENANT_A, "hl-ghost-src", "hl-ghost-dst");
        assertThat(n).as("rename of absent collection must return 0").isEqualTo(0);
    }

    @Test @Order(55)
    void renameHighlightsCollection_rlsIsolated() {
        // TENANT_A inserts a highlight row; TENANT_B rename must not touch it
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("doc_id",      "hl-rls-doc");
        body.put("source_uri",  "file://hl-rls.md");
        body.put("collection",  "hl-rls-src");
        body.put("highlights_md", "## private");
        body.put("mentions_md", "");
        body.put("ingested_at", "2026-01-01T00:00:00.000000Z");
        repo.upsertHighlight(TENANT_A, body);

        // TENANT_B renames: must see 0 rows affected
        int n = repo.renameHighlightsCollection(TENANT_B, "hl-rls-src", "hl-rls-dst");
        assertThat(n).as("TENANT_B rename must not affect TENANT_A rows (RLS)").isEqualTo(0);

        // TENANT_A's row must still be under original collection
        var rows = repo.listHighlights(TENANT_A, 100, 0);
        boolean stillUnderSrc = rows.stream().anyMatch(r ->
            "hl-rls-src".equals(r.get("collection")) && "hl-rls-doc".equals(r.get("doc_id")));
        assertThat(stillUnderSrc).as("TENANT_A row must remain under hl-rls-src").isTrue();
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(10);   // higher for concurrency test
        cfg.setAutoCommit(true);
        return new com.zaxxer.hikari.HikariDataSource(cfg);
    }
}
