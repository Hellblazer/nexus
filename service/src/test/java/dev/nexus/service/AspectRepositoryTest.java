package dev.nexus.service;

import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.UnregisteredCollectionException;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.*;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.*;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
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

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    AspectRepository repo;
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

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // nexus-b7v6i: document_aspects.doc_id and document_highlights.doc_id now enforce
            // a FK to catalog_documents(tenant_id, tumbler). Seed all tumbler values used
            // as doc_ids in this test class so FK checks pass.
            // document_aspects doc_ids: "1.2.3" (makeAspect default), "2.4.6", "3.1.4"
            // document_highlights doc_ids: "highlight-1", "by-uri-doc", "list-hl-0", "list-hl-1",
            //   "del-highlight", "import-hl-1", "rls-hl-private", "hl-rename-doc-1", "hl-rename-doc-2", "hl-rls-doc"
            for (String tumbler : List.of(
                    "1.2.3", "2.4.6", "3.1.4",
                    "highlight-1", "by-uri-doc", "list-hl-0", "list-hl-1",
                    "del-highlight", "import-hl-1", "rls-hl-private",
                    "hl-rename-doc-1", "hl-rename-doc-2", "hl-rls-doc",
                    // nexus-1usso importHighlightsBatch multi-row conversion tests
                    "batch-hl-1", "batch-hl-2",
                    // nexus-nyout doc_id COALESCE-on-NULL tests
                    "coalesce-doc-1", "coalesce-doc-2")) {
                DSL.using(su, SQLDialect.POSTGRES).insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT_A)
                    .set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
                    .set(CATALOG_DOCUMENTS.TITLE, "Test fixture: " + tumbler)
                    .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER).doNothing()
                    .execute();
            }

            // RDR-204 Phase 1 (bead nexus-ft04v.7): document_aspects_collection_fk /
            // document_highlights_collection_fk
            // are REAL, always-enforced FKs to catalog_collections — AspectRepository's
            // stub-insert is retired, so a real row (not just a CollectionRegistry cache
            // entry) must exist before any write, or the FK itself rejects the insert
            // regardless of what the in-process cache believes. This suite's tests
            // predate that requirement and use a small, distinct, per-test collection
            // name purely for row isolation, not to exercise registration behavior
            // itself (CollectionRegistryTest owns the fail-loud contract). Seeded here,
            // once, for BOTH tenants (RLS isolation is a DATA guarantee enforced on the
            // rows themselves, never on collection registration) — including "", the
            // document_highlights nne()-default some tests never override.
            for (String collection : List.of(
                    "", "coll-a", "coll-coalesce", "coll-coalesce-explicit", "coll-nodocid",
                    "coll-nodocid2", "coll-lowconf", "coll-nullconf", "coll-nullconf-batch",
                    "coll-overwrite", "coll-tumbler", "list-coll", "bykey-coll", "del-coll",
                    "rename-src", "rename-dst", "import-coll", "importlc-coll", "rls-coll",
                    "failclosed-coll", "extras-wire-coll", "extras-null-coll",
                    "extras-blank-coll", "ev-coll", "batch-asp-coll", "salient-coll",
                    "dt-papers", "hl-src", "hl-dst", "hl-ghost-src", "hl-ghost-dst",
                    "hl-rls-src", "hl-rls-dst", "queue-coll", "reenqueue-coll",
                    "coalesce-coll", "enqmany-coll", "enqmany-skip-coll",
                    "enqmany-reenq-coll", "done-coll", "failretry-coll", "stale-coll",
                    "fifo-coll", "qrename-src", "qrename-dst", "etl-queue-coll",
                    "greatest-coll", "least-coll", "rls-queue-coll", "batch-coll",
                    "nra-coll", "mr-coll", "rb-coll", "lf-a", "concurrent-coll",
                    "qbatch-coll-a", "qbatch-coll-b", "qbatch-dup", "qbatch-fid",
                    "qbatch-skip")) {
                for (String tenant : List.of(TENANT_A, TENANT_B)) {
                    PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, collection);
                }
            }
        }

        svcDs = buildSvcDataSource();
        tenantScope = new TenantScope(svcDs);
        repo = new AspectRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
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
    void upsertAspect_reupsertWithBlankDocId_rejected() {
        // hygiene-001 (nexus-tk070.p6a follow-on, Sam decision) SUPERSEDES
        // nexus-x1de2's original id-less-re-upsert-preserves-attribution
        // behavior this test used to pin: document_aspects.doc_id is
        // NOT NULL now, and Postgres checks NOT NULL on the PROPOSED INSERT
        // row before it even evaluates whether an ON CONFLICT arm applies —
        // so an id-less re-upsert can no longer bind NULL at all, regardless
        // of the EX_DOC_ID_COALESCE the DO UPDATE arm still carries (now
        // vacuous, since the boundary check below fires first). Every
        // upsert, first-time or re-upsert, must re-supply doc_id.
        var attributed = makeAspect("coll-coalesce", "keep.pdf");
        assertThat(attributed.get("doc_id")).isEqualTo("1.2.3");
        repo.upsertAspect(TENANT_A, attributed);

        var idless = new java.util.HashMap<String, Object>(attributed);
        idless.put("doc_id", "");
        idless.put("model_version", "v3");
        assertThatThrownBy(() -> repo.upsertAspect(TENANT_A, idless))
            .as("doc_id is required on every upsert, including a re-upsert of an existing row")
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessage("doc_id required");

        Optional<Map<String, Object>> rec = repo.getAspect(TENANT_A, "coll-coalesce", "keep.pdf");
        assertThat(rec).isPresent();
        assertThat(rec.get().get("doc_id"))
            .as("the rejected call must not have touched the row at all")
            .isEqualTo("1.2.3");
        assertThat(rec.get().get("model_version"))
            .as("the rejected call's OTHER field changes must not land either -- "
                + "the whole write is refused, not partially applied")
            .isNotEqualTo("v3");
    }

    @Test @Order(2)
    void upsertAspect_reupsertWithExplicitDocId_stillUpdatesOtherFields() {
        // Companion to the rejection test above: a re-upsert that RE-SUPPLIES
        // the doc_id it already has (rather than omitting it) still works
        // exactly as before -- hygiene-001 only closes the omission path.
        var attributed = makeAspect("coll-coalesce-explicit", "keep2.pdf");
        repo.upsertAspect(TENANT_A, attributed);

        var reupserted = new java.util.HashMap<String, Object>(attributed);
        reupserted.put("model_version", "v3");
        repo.upsertAspect(TENANT_A, reupserted);

        Optional<Map<String, Object>> rec = repo.getAspect(TENANT_A, "coll-coalesce-explicit", "keep2.pdf");
        assertThat(rec).isPresent();
        assertThat(rec.get().get("doc_id")).isEqualTo(attributed.get("doc_id"));
        assertThat(rec.get().get("model_version")).isEqualTo("v3");
    }

    @Test @Order(2)
    void upsertAspect_blankDocId_rejected() {
        var body = makeAspect("coll-nodocid", "nodocid.pdf");
        body.put("doc_id", "");
        assertThatThrownBy(() -> repo.upsertAspect(TENANT_A, body))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessage("doc_id required");
    }

    @Test @Order(2)
    void upsertAspect_missingDocId_rejected() {
        var body = makeAspect("coll-nodocid2", "nodocid2.pdf");
        body.remove("doc_id");
        assertThatThrownBy(() -> repo.upsertAspect(TENANT_A, body))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessage("doc_id required");
    }

    @Test @Order(2)
    void upsertAspect_lowConfidence_rejected() {
        var body = makeAspect("coll-lowconf", "lowconf.pdf", 0.1, "v1", "bad-extractor");
        long id = repo.upsertAspect(TENANT_A, body);
        assertThat(id).as("low-confidence upsert must return -1").isEqualTo(-1L);

        Optional<Map<String, Object>> rec = repo.getAspect(TENANT_A, "coll-lowconf", "lowconf.pdf");
        assertThat(rec).as("low-confidence row must NOT be stored").isEmpty();
    }

    @Test @Order(2)
    void upsertAspect_nullConfidence_droppedNotNpe() {
        // nexus-j0nec: JSON body carrying "confidence": null used to NPE
        // (containsKey true -> ((Number) null).doubleValue()) and surface as
        // HTTP 500. Mirror the Python gate (document_aspects._MIN_CONFIDENCE:
        // confidence None is dropped with a structured warning, no row).
        var body = makeAspect("coll-nullconf", "nullconf.pdf");
        body.put("confidence", null);
        long id = repo.upsertAspect(TENANT_A, body);
        assertThat(id).as("null-confidence upsert must drop (-1), not NPE").isEqualTo(-1L);

        Optional<Map<String, Object>> rec = repo.getAspect(TENANT_A, "coll-nullconf", "nullconf.pdf");
        assertThat(rec).as("null-confidence row must NOT be stored").isEmpty();
    }

    @Test @Order(2)
    void importAspectsBatch_nullConfidence_droppedNotNpe() {
        // Batch variant of the null-confidence gate: the kept-filter treats
        // null as -1.0 (drop) and the write loop must never see a null.
        var ok = makeAspect("coll-nullconf-batch", "ok.pdf", 0.9, "v1", "ext");
        var nullConf = makeAspect("coll-nullconf-batch", "null.pdf");
        nullConf.put("confidence", null);
        int written = repo.importAspectsBatch(TENANT_A, List.of(ok, nullConf));
        assertThat(written).as("only the non-null-confidence row is written").isEqualTo(1);
        assertThat(repo.getAspect(TENANT_A, "coll-nullconf-batch", "null.pdf")).isEmpty();
        assertThat(repo.getAspect(TENANT_A, "coll-nullconf-batch", "ok.pdf")).isPresent();
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
        // nexus-cefa1.4: salient_sentences is jsonb now — PostgreSQL's jsonb
        // canonical text output inserts a space after each array-element comma
        // (matching Python's json.dumps default separators exactly), so the
        // multi-element array written without a space no longer round-trips
        // byte-identical; assert the canonical shape instead.
        assertThat(val).as("getSalientSentences must return stored JSON")
            .isEqualTo("[\"sentence one\", \"sentence two\"]");
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
    void rls_withCheck_crossTenantInsert_rejected() throws Exception {
        // document_aspects.collection FK's (tenant_id, collection) to
        // catalog_collections(tenant_id, name) (fk-003-1) — seed both stub rows via
        // superuser (bypasses RLS) so the positive- and negative-control INSERTs
        // below fail (or succeed) on the tenant_id mismatch alone, never on a
        // missing collection registration.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext suCtx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(suCtx, TENANT_A, "good-coll");
            PgContainerHelper.insertCollection(suCtx, TENANT_B, "bad-coll");
        }

        // isLocal=true (SET LOCAL) needs an explicit transaction to survive to the
        // guarded INSERT — autoCommit=true (the prior shape here) discards the GUC
        // before the INSERT runs, so the test would exercise RLS default-deny (no
        // tenant context at all) rather than the claimed TENANT_A-vs-TENANT_B
        // mismatch (nexus-gcbp4). The INSERT is expected to throw, so rollback()
        // runs in a finally — this pooled connection must not return to svcDs with
        // an open transaction and a stale GUC still stamped.
        try (var conn = svcDs.getConnection()) {
            conn.setAutoCommit(false);
            try {
                PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, true);
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);

                // Positive control (nexus-492qf): a matching-tenant INSERT under the
                // SAME GUC must succeed — proves the WITH CHECK rejection asserted
                // below is driven by the tenant_id/GUC mismatch, not by the GUC having
                // silently gone missing (an unset GUC would ALSO reject this insert
                // under FORCE RLS, matching-tenant included, making the negative
                // assertion pass vacuously). Runs BEFORE the failing statement: once a
                // statement inside a transaction errors, Postgres aborts the whole
                // transaction and rejects every later command until rollback.
                // doc_id/source_uri are NOT NULL here (hygiene-001-1) and doc_id is
                // FK'd to catalog_documents(tenant_id, tumbler) — "1.2.3" is the
                // TENANT_A tumbler this class's @BeforeAll already seeds for exactly
                // this purpose. Discovered while adding this control: the ORIGINAL
                // negative INSERT below (both before and after nexus-gcbp4) omitted
                // both columns, so it was throwing on a NOT NULL violation, not the
                // claimed RLS WITH CHECK mismatch — a second, independent vacuity bug
                // this control also fixes by giving both statements the same column
                // set, differing only in tenant_id.
                ctx.insertInto(DOCUMENT_ASPECTS,
                        DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.SOURCE_PATH,
                        DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                        DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                    .values(TENANT_A, "good-coll", "good.pdf", OffsetDateTime.now(), "v1", "good",
                            "1.2.3", "file:///good.pdf")
                    .execute();

                assertThatThrownBy(() ->
                    ctx.insertInto(DOCUMENT_ASPECTS,
                            DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.SOURCE_PATH,
                            DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION,
                            DOCUMENT_ASPECTS.EXTRACTOR_NAME, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                        .values(TENANT_B, "bad-coll", "bad.pdf", OffsetDateTime.now(), "v1", "bad",
                                "1.2.3", "file:///bad.pdf")
                        .execute())
                .as("RLS WITH CHECK must reject aspect INSERT where tenant_id != nexus.tenant GUC")
                .isInstanceOf(org.jooq.exception.DataAccessException.class);
            } finally {
                conn.rollback();
            }
        }
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
                int cnt = DSL.using(conn, SQLDialect.POSTGRES)
                    .fetchCount(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.COLLECTION.eq("failclosed-coll"));
                // If RLS fail-closed: count is 0 (empty result). If broken: count > 0.
                assertThat(cnt)
                    .as("unset GUC must result in 0 visible rows (fail-closed RLS)")
                    .isEqualTo(0);
            }
        }).doesNotThrowAnyException();
    }

    // ── document_aspects type-hygiene (nexus-cefa1.4) ──────────────────────────

    @Test @Order(16)
    void upsertAspect_extras_wireRoundTrip_preservesJsonObjectShape() {
        // nexus-cefa1.4: extras TEXT -> jsonb. The real write path (upsertAspect)
        // with the real production shape -- a JSON-encoded object string -- must
        // read back with the same keys/values (not byte-identical text, since
        // PostgreSQL's jsonb canonical output can reorder/respace).
        var body = makeAspect("extras-wire-coll", "extras-wire.pdf");
        body.put("extras", "{\"venue\": \"VLDB\", \"year\": \"2023\"}");
        repo.upsertAspect(TENANT_A, body);

        Optional<Map<String, Object>> rec = repo.getAspect(TENANT_A, "extras-wire-coll", "extras-wire.pdf");
        assertThat(rec).isPresent();
        Object raw = rec.get().get("extras");
        assertThat(raw).isInstanceOf(String.class);
        try {
            var mapper = new com.fasterxml.jackson.databind.ObjectMapper();
            @SuppressWarnings("unchecked")
            Map<String, Object> parsed = mapper.readValue((String) raw, Map.class);
            assertThat(parsed).containsEntry("venue", "VLDB").containsEntry("year", "2023");
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new AssertionError("extras must remain parseable JSON after the wire round trip: " + raw, e);
        }
    }

    @Test @Order(17)
    void upsertAspect_extras_nullAndBlank_bothRoundTripAsNull() {
        // The migration's own USING NULLIF(extras, '')::jsonb clause maps '' to
        // NULL; the live write path must keep doing that going forward too (a
        // blank string is not valid jsonb input).
        var nullBody = makeAspect("extras-null-coll", "extras-null.pdf");
        repo.upsertAspect(TENANT_A, nullBody);
        var blankBody = makeAspect("extras-blank-coll", "extras-blank.pdf");
        blankBody.put("extras", "");
        repo.upsertAspect(TENANT_A, blankBody);

        assertThat(repo.getAspect(TENANT_A, "extras-null-coll", "extras-null.pdf").get().get("extras"))
            .as("absent extras must round-trip as null").isNull();
        assertThat(repo.getAspect(TENANT_A, "extras-blank-coll", "extras-blank.pdf").get().get("extras"))
            .as("blank-string extras must round-trip as null (NULLIF semantics)").isNull();
    }

    @Test @Order(18)
    void recordPromotion_columnAddedAndPruned_wireRoundTrip_booleanIdentityPreserved() {
        // nexus-cefa1.4: column_added/pruned INTEGER -> boolean. The wire was
        // already boolean (AspectHandler passes body values straight through);
        // this pins that the live write path (recordPromotion) still round-trips
        // true/false as real Java Booleans, not truthy ints/strings, through the
        // read path (listPromotions).
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("field_name",      "th-column-added-true");
        body.put("sql_type",        "TEXT");
        body.put("column_added",    true);
        body.put("pruned",          true);
        body.put("promoted_at",     "2026-08-15T09:00:00.000000Z");
        repo.recordPromotion(TENANT_A, body);

        var body2 = new java.util.LinkedHashMap<String, Object>();
        body2.put("field_name",      "th-column-added-false");
        body2.put("sql_type",        "TEXT");
        body2.put("column_added",    false);
        body2.put("pruned",          false);
        body2.put("promoted_at",     "2026-08-15T09:01:00.000000Z");
        repo.recordPromotion(TENANT_A, body2);

        List<Map<String, Object>> promotions = repo.listPromotions(TENANT_A);
        var trueRow = promotions.stream()
            .filter(p -> "th-column-added-true".equals(p.get("field_name"))).findFirst().orElseThrow();
        var falseRow = promotions.stream()
            .filter(p -> "th-column-added-false".equals(p.get("field_name"))).findFirst().orElseThrow();

        assertThat(trueRow.get("column_added")).isInstanceOf(Boolean.class).isEqualTo(Boolean.TRUE);
        assertThat(trueRow.get("pruned")).isInstanceOf(Boolean.class).isEqualTo(Boolean.TRUE);
        assertThat(falseRow.get("column_added")).isInstanceOf(Boolean.class).isEqualTo(Boolean.FALSE);
        assertThat(falseRow.get("pruned")).isInstanceOf(Boolean.class).isEqualTo(Boolean.FALSE);
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

    // ── importXBatch: ONE multi-row INSERT per method (nexus-1usso) ─────────────
    // Plan-audit correction: these endpoints already existed (RDR-176 P3) but
    // their repository implementations still looped per-row .execute() inside
    // one tenant transaction (N round-trips). These tests exercise the
    // multi-row conversion.

    @Test @Order(60)
    void importAspectsBatch_multiRow_insertsAll_lowConfidenceSkipped_intraBatchDedupe() {
        var kept1 = makeAspect("batch-asp-coll", "batch1.pdf", 0.75, "v1", "ext-1");
        var kept2 = makeAspect("batch-asp-coll", "batch2.pdf", 0.80, "v1", "ext-1");
        var lowConf = makeAspect("batch-asp-coll", "lowconf.pdf", 0.05, "v1", "ext-1");
        // Intra-batch duplicate on (collection, source_path) — last wins.
        var dup = makeAspect("batch-asp-coll", "batch1.pdf", 0.95, "v2", "ext-2");

        int n = repo.importAspectsBatch(TENANT_A, List.of(kept1, kept2, lowConf, dup));
        assertThat(n).as("low-confidence row must count 0; 3 rows written").isEqualTo(3);

        var got1 = repo.getAspect(TENANT_A, "batch-asp-coll", "batch1.pdf");
        assertThat(got1).isPresent();
        assertThat(got1.get().get("model_version")).as("intra-batch dedupe: last wins").isEqualTo("v2");
        assertThat(repo.getAspect(TENANT_A, "batch-asp-coll", "batch2.pdf")).isPresent();
        assertThat(repo.getAspect(TENANT_A, "batch-asp-coll", "lowconf.pdf"))
            .as("sub-confidence row must not land").isEmpty();
    }

    @Test @Order(61)
    void importAspectsBatch_emptyAndNull_returnZero() {
        assertThat(repo.importAspectsBatch(TENANT_A, List.of())).isZero();
        assertThat(repo.importAspectsBatch(TENANT_A, null)).isZero();
    }

    @Test @Order(62)
    void importHighlightsBatch_multiRow_insertsAll_blankDocIdSkipped_intraBatchDedupe() {
        var h1 = new java.util.LinkedHashMap<String, Object>();
        h1.put("doc_id", "batch-hl-1");
        h1.put("highlights_md", "v1 content");
        h1.put("ingested_at", "2025-01-01T00:00:00.000000Z");

        var h2 = new java.util.LinkedHashMap<String, Object>();
        h2.put("doc_id", "batch-hl-2");
        h2.put("highlights_md", "other content");
        h2.put("ingested_at", "2025-01-01T00:00:00.000000Z");

        var blank = new java.util.LinkedHashMap<String, Object>();
        blank.put("doc_id", "");
        blank.put("highlights_md", "must not land");
        blank.put("ingested_at", "2025-01-01T00:00:00.000000Z");

        // Intra-batch duplicate on doc_id "batch-hl-1" — last wins.
        var dup = new java.util.LinkedHashMap<String, Object>();
        dup.put("doc_id", "batch-hl-1");
        dup.put("highlights_md", "v2 content updated");
        dup.put("ingested_at", "2026-01-01T00:00:00.000000Z");

        int n = repo.importHighlightsBatch(TENANT_A, List.of(h1, h2, blank, dup));
        assertThat(n).as("blank doc_id row must count 0; 3 rows written").isEqualTo(3);

        var got1 = repo.getHighlight(TENANT_A, "batch-hl-1");
        assertThat(got1).isPresent();
        assertThat(got1.get().get("highlights_md")).asString()
            .as("intra-batch dedupe: last wins").contains("v2 content updated");
        assertThat(repo.getHighlight(TENANT_A, "batch-hl-2")).isPresent();
    }

    @Test @Order(63)
    void importHighlightsBatch_emptyAndNull_returnZero() {
        assertThat(repo.importHighlightsBatch(TENANT_A, List.of())).isZero();
        assertThat(repo.importHighlightsBatch(TENANT_A, null)).isZero();
    }

    @Test @Order(64)
    void importPromotionBatch_multiRow_doNothingOnReimport_invalidRowsSkipped() {
        var p1 = new java.util.LinkedHashMap<String, Object>();
        p1.put("field_name",  "batch_field_1");
        p1.put("sql_type",    "TEXT");
        p1.put("promoted_at", "2026-06-02T10:00:00.000000Z");

        var p2 = new java.util.LinkedHashMap<String, Object>();
        p2.put("field_name",  "batch_field_2");
        p2.put("sql_type",    "TEXT");
        p2.put("promoted_at", "2026-06-02T11:00:00.000000Z");

        var invalid = new java.util.LinkedHashMap<String, Object>();
        invalid.put("sql_type", "TEXT"); // missing field_name — must be skipped

        int n = repo.importPromotionBatch(TENANT_A, List.of(p1, p2, invalid));
        assertThat(n).as("invalid row skipped; 2 rows newly inserted").isEqualTo(2);

        // Re-import the same 2 valid rows — DO NOTHING must report 0 newly written.
        int n2 = repo.importPromotionBatch(TENANT_A, List.of(p1, p2));
        assertThat(n2).as("re-import: DO NOTHING reports 0 newly inserted").isZero();

        List<Map<String, Object>> all = repo.listPromotions(TENANT_A);
        long count1 = all.stream().filter(p -> "batch_field_1".equals(p.get("field_name"))).count();
        long count2 = all.stream().filter(p -> "batch_field_2".equals(p.get("field_name"))).count();
        assertThat(count1).as("exactly 1 row for batch_field_1 (no duplicate)").isEqualTo(1);
        assertThat(count2).as("exactly 1 row for batch_field_2 (no duplicate)").isEqualTo(1);
    }

    @Test @Order(65)
    void importPromotionBatch_emptyAndNull_returnZero() {
        assertThat(repo.importPromotionBatch(TENANT_A, List.of())).isZero();
        assertThat(repo.importPromotionBatch(TENANT_A, null)).isZero();
    }

    @Test @Order(70)
    void upsertAspect_unregisteredCollection_throwsAndWritesNoRow() {
        // RDR-204 Phase 1 (nexus-ft04v.7 gap 3, closed by nexus-ft04v.8's test
        // addendum): AspectRepository's stub-insert retirement means a write
        // against a collection with no catalog_collections row must fail loud
        // via CollectionRegistry.requireRegistered, and create NO row anywhere --
        // neither the document_aspects row nor a catalog_collections stub.
        String collection = "unreg-aspect-coll";
        var body = makeAspect(collection, "unreg.pdf");

        assertThatThrownBy(() -> repo.upsertAspect(TENANT_A, body))
            .isInstanceOf(UnregisteredCollectionException.class)
            .hasMessageContaining(collection)
            .hasMessageContaining("POST /v1/catalog/collections/upsert");

        assertThat(repo.getAspect(TENANT_A, collection, "unreg.pdf"))
            .as("a write against an unregistered collection must create no document_aspects row")
            .isEmpty();
        boolean stubRowExists = tenantScope.withTenant(TENANT_A, ctx -> ctx.fetchExists(
                ctx.selectOne().from(CATALOG_COLLECTIONS)
                   .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT_A))
                   .and(CATALOG_COLLECTIONS.NAME.eq(collection))));
        assertThat(stubRowExists)
            .as("the rejected write must not have created a catalog_collections stub row either")
            .isFalse();
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(10);   // higher for concurrency test
        cfg.setAutoCommit(true);
        return new com.zaxxer.hikari.HikariDataSource(cfg);
    }
}
