// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-kl2z6 increment 1 — the COMBINED WRITE (T2 {@code
 * design-kl2z6-combined-write} [21804] REV 2 §0/§1). Hermetic Testcontainers
 * PG, mirroring {@code CatalogManifestSweepRepositoryTest}'s structure (the
 * design memo's own named precedent for the §6a/§6c test shapes).
 *
 * <p>Test plan coverage (memo §6): 6a (two-connection atomicity block/
 * release), 6b (a chash landed via the combined-write path still survives a
 * sibling doc's drop+sweep, proving the pre-existing union guard composes
 * with the NEW write origin), 6c (forced mid-transaction failure AFTER the
 * chunk-vector INSERT rolls back the whole per-doc transaction, chunk row
 * included, while a sibling doc in the same call succeeds), 6e (a manifest
 * chash absent from BOTH {@code chunks} and the store fails its doc loud; a
 * chash already in the store needs no re-send), plus the RDR-181
 * existence-partition hard requirement (§0: "known chashes never
 * re-embedded"). 6d (the staging guard) is increment 2's concern. 6f/6h are
 * covered by the unmodified route census / existing sweep+manifest suite
 * staying green, not by tests in this file.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CombinedWriteRepositoryTest {

    private static final String SVC_ROLE = "svc_combined_write_test";
    private static final String SVC_PASS = "svc_combined_write_test_pass";
    private static final String TENANT_A = "combined-write-tenant-a";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;
    CombinedWriteService svc;
    CountingFakeEmbedder embedder;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; END IF; END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.grantServiceSchemaAccess(su, SVC_ROLE);
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
        embedder = new CountingFakeEmbedder();
        svc = new CombinedWriteService(tenantScope, repo, new EmbedderRouter(embedder, "document"));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    private void registerDoc(String tenant, String tumbler, String collection) {
        repo.upsertDocument(tenant, Map.of(
            "tumbler", tumbler, "title", "combined-write-test-" + tumbler,
            "content_type", "code", "corpus", "code",
            "physical_collection", collection, "chunk_count", 0));
    }

    private boolean chunk384Exists(String tenant, String collection, String hexChash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ps = su.prepareStatement(
                "SELECT 1 FROM " + DimTables.CHUNKS_TABLE_NAME + " WHERE tenant_id = ? AND collection = ? AND chash = ? AND " + DimTables.embeddingColumn(384) + " IS NOT NULL");
            ps.setString(1, tenant);
            ps.setString(2, collection);
            ps.setBytes(3, java.util.HexFormat.of().parseHex(hexChash));
            return ps.executeQuery().next();
        }
    }

    private String chunk384Text(String tenant, String collection, String hexChash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ps = su.prepareStatement(
                "SELECT chunk_text FROM " + DimTables.CHUNKS_TABLE_NAME + " WHERE tenant_id = ? AND collection = ? AND chash = ? AND " + DimTables.embeddingColumn(384) + " IS NOT NULL");
            ps.setString(1, tenant);
            ps.setString(2, collection);
            ps.setBytes(3, java.util.HexFormat.of().parseHex(hexChash));
            var rs = ps.executeQuery();
            return rs.next() ? rs.getString(1) : null;
        }
    }

    /** Raw stored {@code metadata} JSONB, as text, for a nexus.chunks (dim=384) row. */
    private String chunk384MetadataJson(String tenant, String collection, String hexChash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ps = su.prepareStatement(
                "SELECT metadata::text FROM " + DimTables.CHUNKS_TABLE_NAME + " WHERE tenant_id = ? AND collection = ? AND chash = ? AND " + DimTables.embeddingColumn(384) + " IS NOT NULL");
            ps.setString(1, tenant);
            ps.setString(2, collection);
            ps.setBytes(3, java.util.HexFormat.of().parseHex(hexChash));
            var rs = ps.executeQuery();
            return rs.next() ? rs.getString(1) : null;
        }
    }

    private void seedChunk384(String tenant, String collection, String hexChash, String text) throws Exception {
        repo.upsertCollection(tenant, Map.of(
            "name", collection, "content_type", "code", "owner_id", "combined-write-owner",
            "embedding_model", "minilm-l6-v2-384", "model_version", "v1"));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("SET nexus.tenant = '" + tenant + "'");
            String zeroVec = "[" + "0,".repeat(383) + "0]";
            var ps = su.prepareStatement(
                "INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " (tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(384) + ")"
                + " VALUES (?, ?, ?, ?, ?::vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
            ps.setString(1, tenant);
            ps.setString(2, collection);
            ps.setBytes(3, java.util.HexFormat.of().parseHex(hexChash));
            ps.setString(4, text);
            ps.setString(5, zeroVec);
            ps.executeUpdate();
        }
    }

    private static Map<String, Object> chunk(String chash, String text) {
        return Map.of("chash", chash, "text", text, "metadata", Map.of());
    }

    private static Map<String, Object> chunk(String chash, String text, Map<String, Object> metadata) {
        return Map.of("chash", chash, "text", text, "metadata", metadata);
    }

    private static Map<String, Object> row(int position, String chash) {
        return Map.of("position", position, "chash", chash, "chunk_index", position);
    }

    private static Map<String, Object> doc(String docId, List<Map<String, Object>> rows) {
        return Map.of("doc_id", docId, "rows", rows);
    }

    /** Raw connection to the service role's own database (test-controlled transaction). */
    private Connection dsConnection() throws java.sql.SQLException {
        return svcDs.getConnection();
    }

    private static void acquireGateExclusive(Connection conn, String tenant, String collection, int lockTimeoutMs)
            throws java.sql.SQLException {
        try (var ps = conn.prepareStatement("SELECT set_config('lock_timeout', ?, true)")) {
            ps.setString(1, String.valueOf(lockTimeoutMs));
            ps.execute();
        }
        try (var ps = conn.prepareStatement("SELECT pg_advisory_xact_lock(hashtext(?))")) {
            ps.setString(1, "sweepgate:" + tenant + "/" + collection);
            ps.execute();
        }
    }

    // ── 6a: two-connection atomicity — block on external EXCLUSIVE gate, ─────
    //        neither row visible while blocked, both appear together after. ──

    @Test @Order(1)
    void combinedWrite_blocksOnExternalExclusiveGate_thenLandsChunkAndManifestTogether() throws Exception {
        String col = "code__cw1__minilm-l6-v2-384__v1";
        String x = ch("cw1-x");
        registerDoc(TENANT_A, "cw.1", col);

        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            try (var st = external.prepareStatement("SET nexus.tenant = '" + TENANT_A + "'")) {
                st.execute();
            }
            acquireGateExclusive(external, TENANT_A, col, 60_000);

            ExecutorService executor = Executors.newSingleThreadExecutor();
            try {
                Future<?> future = executor.submit(() ->
                    svc.writeManyCombined(TENANT_A, col,
                        List.of(chunk(x, "cw1 text x")),
                        List.of(doc("cw.1", List.of(row(0, x)))),
                        null, false, false));

                assertThatThrownBy(() -> future.get(750, TimeUnit.MILLISECONDS))
                    .as("combined write must BLOCK while the gate is held EXCLUSIVE externally")
                    .isInstanceOf(java.util.concurrent.TimeoutException.class);

                // Neither the chunk row nor the manifest row is visible while blocked.
                assertThat(chunk384Exists(TENANT_A, col, x)).isFalse();
                assertThat(repo.getManifest(TENANT_A, "cw.1")).isEmpty();

                external.rollback();

                future.get(15, TimeUnit.SECONDS);
            } finally {
                executor.shutdownNow();
            }
        }

        // Both appear together once the gate releases.
        assertThat(chunk384Exists(TENANT_A, col, x)).isTrue();
        assertThat(repo.getManifest(TENANT_A, "cw.1")).hasSize(1);
    }

    // ── 6b: a chash landed via the combined write still respects the ─────────
    //        pre-existing sweep union guard when a sibling still refers to it. ─

    @Test @Order(2)
    void combinedWrite_sharedChash_survivesSiblingDropAndSweep() throws Exception {
        String col = "code__cw2__minilm-l6-v2-384__v1";
        String shared = ch("cw2-shared");
        registerDoc(TENANT_A, "cw.2a", col);
        registerDoc(TENANT_A, "cw.2b", col);

        // Both docs land in ONE combined-write call, sharing `shared`. Design
        // memo §1.1: a shared chash is upserted in EACH referencing doc's
        // OWN per-doc transaction (idempotent ON CONFLICT DO UPDATE) -- that
        // is what makes per-doc atomicity possible without per-doc text
        // duplication on the wire, so chunks_written counts 2 here, not 1.
        var landed = svc.writeManyCombined(TENANT_A, col,
            List.of(chunk(shared, "cw2 shared text")),
            List.of(doc("cw.2a", List.of(row(0, shared))),
                    doc("cw.2b", List.of(row(0, shared)))),
            null, false, false);
        assertThat(landed.response().get("chunks_written")).isEqualTo(2);
        assertThat(chunk384Exists(TENANT_A, col, shared)).isTrue();

        // A drops `shared` (plain writeManifestMany, sweep=true); B still references it.
        var result = repo.writeManifestMany(TENANT_A, List.of(
            doc("cw.2a", List.of(row(0, ch("cw2-new"))))), col, null, true);

        assertThat(result.get("swept"))
            .as("B's live reference (landed via the combined write) must survive the union guard")
            .isEqualTo(0);
        assertThat(chunk384Exists(TENANT_A, col, shared))
            .as("the combined-write-origin chunk must still be protected by the existing sweep guard")
            .isTrue();
    }

    // ── 6c: forced mid-transaction failure AFTER the chunk-vector INSERT ─────
    //        rolls back the WHOLE per-doc transaction; a sibling still succeeds. ─

    @Test @Order(3)
    void combinedWrite_forcedMidTransactionFailure_zeroChunkRowsCommitted_siblingSucceeds() throws Exception {
        String col = "code__cw3__minilm-l6-v2-384__v1";
        String failChash = ch("cw3-fail");
        String okChash   = ch("cw3-ok");
        registerDoc(TENANT_A, "cw.3.fail", col);
        registerDoc(TENANT_A, "cw.3.ok", col);
        // Force the LAST statement of writeManifestRows (the chunk_count UPDATE,
        // gated on deleted_at IS NULL) to affect zero rows for cw.3.fail, which
        // TombstonedDocumentException then throws for — AFTER this doc's chunk
        // INSERT and manifest DELETE+INSERT have already run in the SAME,
        // still-open transaction. Propagating out of the withTenant lambda
        // rolls back the WHOLE transaction, chunk INSERT included (the exact
        // "any exception, any cause, whole-transaction rollback" property the
        // Order-17 REVOKE precedent in CatalogManifestSweepRepositoryTest
        // proves for the sweep's own DELETE; deterministic and requires no
        // GRANT/REVOKE cleanup, unlike a permission-denied SQL error).
        assertThat(repo.deleteDocument(TENANT_A, "cw.3.fail")).isEqualTo(1);

        var result = svc.writeManyCombined(TENANT_A, col,
            List.of(chunk(failChash, "cw3 fail text"), chunk(okChash, "cw3 ok text")),
            List.of(doc("cw.3.fail", List.of(row(0, failChash))),
                    doc("cw.3.ok", List.of(row(0, okChash)))),
            null, false, false);

        @SuppressWarnings("unchecked")
        var failedIds = (List<String>) result.response().get("failed_doc_ids");
        assertThat(failedIds).containsExactly("cw.3.fail");
        assertThat(result.response().get("docs")).as("the sibling must still succeed").isEqualTo(1);

        assertThat(chunk384Exists(TENANT_A, col, failChash))
            .as("the failed doc's chunk INSERT must have rolled back with everything else in its transaction")
            .isFalse();
        assertThat(chunk384Exists(TENANT_A, col, okChash))
            .as("the sibling's chunk must be committed").isTrue();
        assertThat(repo.getManifest(TENANT_A, "cw.3.ok")).hasSize(1);
    }

    // ── 6e: a manifest chash absent from BOTH `chunks` and the store fails ───
    //        its doc loud; a chash already stored needs no re-send. ──────────

    @Test @Order(4)
    void combinedWrite_manifestReferencesChashAbsentFromChunksAndStore_failsDocLoud() throws Exception {
        String col = "code__cw4__minilm-l6-v2-384__v1";
        String missing = ch("cw4-missing");
        registerDoc(TENANT_A, "cw.4", col);

        var result = svc.writeManyCombined(TENANT_A, col,
            List.of(), // no chunks sent at all
            List.of(doc("cw.4", List.of(row(0, missing)))),
            null, false, false);

        @SuppressWarnings("unchecked")
        var failedIds = (List<String>) result.response().get("failed_doc_ids");
        assertThat(failedIds).containsExactly("cw.4");
        assertThat(repo.getManifest(TENANT_A, "cw.4"))
            .as("nothing must have committed for the refused doc").isEmpty();
        assertThat(chunk384Exists(TENANT_A, col, missing)).isFalse();
    }

    @Test @Order(5)
    void combinedWrite_manifestReferencesChashAlreadyInStore_noResendNeeded() throws Exception {
        String col = "code__cw5__minilm-l6-v2-384__v1";
        String preexisting = ch("cw5-preexisting");
        seedChunk384(TENANT_A, col, preexisting, "already stored text");
        registerDoc(TENANT_A, "cw.5", col);

        var result = svc.writeManyCombined(TENANT_A, col,
            List.of(), // the client does not resend text for an unchanged chunk
            List.of(doc("cw.5", List.of(row(0, preexisting)))),
            null, false, false);

        assertThat(result.response().get("docs")).isEqualTo(1);
        assertThat((List<?>) result.response().get("failed_doc_ids")).isEmpty();
        assertThat(result.response().get("chunks_written"))
            .as("nothing new was written -- the chash already existed").isEqualTo(0);
        assertThat(repo.getManifest(TENANT_A, "cw.5")).hasSize(1);
        // Untouched: still the original seeded text, never overwritten.
        assertThat(chunk384Text(TENANT_A, col, preexisting)).isEqualTo("already stored text");
    }

    // ── §5.1: no `chunks` field is BACKWARD COMPATIBLE — byte-for-byte, ──────
    //          `chunks_written` absent entirely (not zero-valued). ───────────

    @Test @Order(6)
    void writeManifestMany_noChunksField_omitsChunksWrittenKey() {
        String col = "code__cw6__minilm-l6-v2-384__v1";
        registerDoc(TENANT_A, "cw.6", col);
        var result = repo.writeManifestMany(TENANT_A, List.of(
            doc("cw.6", List.of(row(0, ch("cw6-x"))))), col, null, true);
        assertThat(result).as("no `chunks` field -- `chunks_written` must be ABSENT, not zero")
            .doesNotContainKey("chunks_written");
    }

    // ── RDR-181 hard requirement (design memo §0): known chashes are never ───
    //    re-embedded by the combined write's existence-partition. ────────────

    @Test @Order(7)
    void combinedWrite_knownChashWithIdenticalText_isNeverReEmbedded() throws Exception {
        String col = "code__cw7__minilm-l6-v2-384__v1";
        String x = ch("cw7-x");
        registerDoc(TENANT_A, "cw.7a", col);
        registerDoc(TENANT_A, "cw.7b", col);

        int before = embedder.calls.get();
        svc.writeManyCombined(TENANT_A, col,
            List.of(chunk(x, "cw7 stable text")),
            List.of(doc("cw.7a", List.of(row(0, x)))),
            null, false, false);
        int afterFirst = embedder.calls.get();
        assertThat(afterFirst).as("first sight of a new chash must embed it").isGreaterThan(before);

        // A SECOND call resending the SAME chash with IDENTICAL text (a
        // different doc referencing the same chunk, routine in a flush) must
        // not invoke the embedder again.
        svc.writeManyCombined(TENANT_A, col,
            List.of(chunk(x, "cw7 stable text")),
            List.of(doc("cw.7b", List.of(row(0, x)))),
            null, false, false);
        int afterSecond = embedder.calls.get();
        assertThat(afterSecond)
            .as("RDR-181: a chash already stored with IDENTICAL text must never be re-embedded")
            .isEqualTo(afterFirst);
        assertThat(repo.getManifest(TENANT_A, "cw.7b")).hasSize(1);
    }

    // ── nexus-awxhm adjudication (substantive-critic Critical, 2026-08-09): ──
    //    byte-identical stored text AND metadata through the REAL combined- ──
    //    write path. Falsified against the pre-fix code before any fix ────────
    //    landed (see T2 nexus/nexus-kl2z6-engine-increment1-2026-08-09 §adjudication). ─

    @Test @Order(8)
    void combinedWrite_multiWordTextAndSpacedMetadata_storedByteIdentical() throws Exception {
        String col = "code__cw8__minilm-l6-v2-384__v1";
        String x = ch("cw8-x");
        registerDoc(TENANT_A, "cw.8", col);

        String text = "hello world this is a multi word chunk";
        Map<String, Object> metadata = Map.of("title", "a b c title with spaces",
                                               "path", "src/main/java/Foo Bar.java");
        svc.writeManyCombined(TENANT_A, col,
            List.of(chunk(x, text, metadata)),
            List.of(doc("cw.8", List.of(row(0, x)))),
            null, false, false);

        assertThat(chunk384Text(TENANT_A, col, x))
            .as("stored chunk_text must be BYTE-IDENTICAL to what was sent -- no space-stripping")
            .isEqualTo(text);
        String storedMeta = chunk384MetadataJson(TENANT_A, col, x);
        assertThat(storedMeta).contains("a b c title with spaces");
        assertThat(storedMeta).contains("src/main/java/Foo Bar.java");
    }

    // ── nexus-n7umy adjudication (code-review-expert Critical, 2026-08-09): ──
    //    the gate held during the chunk-vector upsert must be the collection ─
    //    being WRITTEN (chunkCollection), not merely the doc's OWN registered ─
    //    collection -- proven by the SAME two-connection block/release ────────
    //    technique 6a uses, this time asserting gate IDENTITY (which key ─────
    //    blocks) rather than just atomicity. ────────────────────────────────

    @Test @Order(9)
    void combinedWrite_docRegisteredUnderDifferentCollection_gatesOnTheCollectionBeingWritten() throws Exception {
        String registeredUnder = "code__cw9a__minilm-l6-v2-384__v1";
        String writtenInto     = "code__cw9b__minilm-l6-v2-384__v1";
        String x = ch("cw9-x");
        // The doc is registered under A; the combined write targets B --
        // divergence, deliberately, is what n7umy flags as unguarded.
        registerDoc(TENANT_A, "cw.9", registeredUnder);

        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            try (var st = external.prepareStatement("SET nexus.tenant = '" + TENANT_A + "'")) {
                st.execute();
            }
            // Hold EXCLUSIVE on B (writtenInto), NOT A -- if the engine only
            // ever gates on the doc's OWN collection (A), this acquire has no
            // effect on the combined write at all and it proceeds unblocked,
            // exposing the exact race n7umy describes.
            acquireGateExclusive(external, TENANT_A, writtenInto, 60_000);

            ExecutorService executor = Executors.newSingleThreadExecutor();
            try {
                Future<?> future = executor.submit(() ->
                    svc.writeManyCombined(TENANT_A, writtenInto,
                        List.of(chunk(x, "cw9 text")),
                        List.of(doc("cw.9", List.of(row(0, x)))),
                        null, false, false));

                assertThatThrownBy(() -> future.get(750, TimeUnit.MILLISECONDS))
                    .as("the combined write must BLOCK on the gate for the collection it actually "
                        + "WRITES INTO, even though the doc is registered under a different collection")
                    .isInstanceOf(java.util.concurrent.TimeoutException.class);

                assertThat(chunk384Exists(TENANT_A, writtenInto, x)).isFalse();

                external.rollback();
                future.get(15, TimeUnit.SECONDS);
            } finally {
                executor.shutdownNow();
            }
        }

        assertThat(chunk384Exists(TENANT_A, writtenInto, x)).isTrue();
        assertThat(repo.getManifest(TENANT_A, "cw.9")).hasSize(1);
    }

    @Test @Order(10)
    void combinedWrite_unregisteredGhostDoc_stillGatesOnTheCollectionBeingWritten() throws Exception {
        String col = "code__cw10__minilm-l6-v2-384__v1";
        String x = ch("cw10-x");
        // No physical_collection at all for this doc -- the PRE-n7umy-fix
        // code derived its sweep-gate collection FROM the document's own
        // physical_collection (RDR-191 removed that inference entirely;
        // writeManifestRows now takes an explicit, caller-supplied
        // collection instead), so a null physical_collection meant NO gate
        // was acquired at all on the writeManifestRows path (the `if (coll
        // != null)` guard skipped it entirely).
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "cw.10", "title", "combined-write-ghost-cw.10",
            "content_type", "code", "corpus", "code", "chunk_count", 0));

        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            try (var st = external.prepareStatement("SET nexus.tenant = '" + TENANT_A + "'")) {
                st.execute();
            }
            acquireGateExclusive(external, TENANT_A, col, 60_000);

            ExecutorService executor = Executors.newSingleThreadExecutor();
            try {
                Future<?> future = executor.submit(() ->
                    svc.writeManyCombined(TENANT_A, col,
                        List.of(chunk(x, "cw10 text")),
                        List.of(doc("cw.10", List.of(row(0, x)))),
                        null, false, false));

                assertThatThrownBy(() -> future.get(750, TimeUnit.MILLISECONDS))
                    .as("an unregistered (null physical_collection) doc must still gate on the "
                        + "collection the combined write actually targets")
                    .isInstanceOf(java.util.concurrent.TimeoutException.class);

                assertThat(chunk384Exists(TENANT_A, col, x)).isFalse();

                external.rollback();
                future.get(15, TimeUnit.SECONDS);
            } finally {
                executor.shutdownNow();
            }
        }

        assertThat(chunk384Exists(TENANT_A, col, x)).isTrue();
    }

    // ── nexus-acvi7 (W7, T2 engine-embed-path-hardening-design-v0.1.70 ───────
    //    §2.5(a)): the existence-partition's embed accounting must be ────────
    //    OBSERVABLE -- a structured log line AND response-envelope counts, ───
    //    correct for a MIXED batch (some chunks known, some new). ────────────

    @Test @Order(11)
    void combinedWrite_mixedKnownAndNewChashes_reportsAccurateEmbedPartitionCounts() throws Exception {
        String col = "code__cw11__minilm-l6-v2-384__v1";
        String known1 = ch("cw11-known-1");
        String known2 = ch("cw11-known-2");
        String new1 = ch("cw11-new-1");
        String new2 = ch("cw11-new-2");
        String new3 = ch("cw11-new-3");
        seedChunk384(TENANT_A, col, known1, "known text 1");
        seedChunk384(TENANT_A, col, known2, "known text 2");
        registerDoc(TENANT_A, "cw.11", col);

        int embedCallsBefore = embedder.calls.get();

        ch.qos.logback.classic.Logger root =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(
                org.slf4j.Logger.ROOT_LOGGER_NAME);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        root.addAppender(logs);
        var result = svc.writeManyCombined(TENANT_A, col,
            List.of(chunk(known1, "known text 1"), chunk(known2, "known text 2"),
                    chunk(new1, "new text 1"), chunk(new2, "new text 2"), chunk(new3, "new text 3")),
            List.of(doc("cw.11", List.of(
                row(0, known1), row(1, known2), row(2, new1), row(3, new2), row(4, new3)))),
            null, false, false);
        List<String> messages;
        try {
            messages = logs.list.stream()
                .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
                .toList();
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }

        assertThat(result.response().get("chunks_deduped"))
            .as("5 distinct chashes sent in `chunks`").isEqualTo(5);
        assertThat(result.response().get("embed_skipped"))
            .as("2 chashes already stored with IDENTICAL text").isEqualTo(2);
        assertThat(result.response().get("embed_embedded"))
            .as("3 chashes are new").isEqualTo(3);
        assertThat(embedder.calls.get() - embedCallsBefore)
            .as("ground truth: exactly the 3 NEW texts must have reached the embedder")
            .isEqualTo(3);
        assertThat(messages)
            .as("the existence-partition must emit ONE observable line naming the exact counts, "
                + "BEFORE the embed call")
            .anyMatch(m -> m.startsWith("event=combined_write_embed_partition")
                && m.contains("collection=" + col)
                && m.contains("deduped=5")
                && m.contains("skipped=2")
                && m.contains("embedded=3")
                && m.contains("force_re_embed=false"));
    }

    // ── non-vacuity (mandatory): force-disable the existence-partition and ───
    //    confirm the signal CHANGES -- a counter reporting the same numbers ───
    //    whether or not the skip works is worthless. Three calls over the ─────
    //    SAME chash set: all-new (embed), repeat (skip), forced-repeat ───────
    //    (embed again despite identical stored text) -- a two-directional ────
    //    detector, mirroring warm-reindex-skip-gate.sh's leg B / leg C. ──────

    @Test @Order(12)
    void combinedWrite_forceReEmbed_flipsCountsBothDirections_nonVacuityProof() throws Exception {
        String col = "code__cw12__minilm-l6-v2-384__v1";
        List<String> chashes = List.of(
            ch("cw12-a"), ch("cw12-b"), ch("cw12-c"), ch("cw12-d"), ch("cw12-e"));
        List<Map<String, Object>> chunks = new ArrayList<>();
        List<Map<String, Object>> rows = new ArrayList<>();
        for (int i = 0; i < chashes.size(); i++) {
            chunks.add(chunk(chashes.get(i), "cw12 stable text " + i));
            rows.add(row(i, chashes.get(i)));
        }
        registerDoc(TENANT_A, "cw.12a", col);
        registerDoc(TENANT_A, "cw.12b", col);
        registerDoc(TENANT_A, "cw.12c", col);

        // Call 1: every chash is brand new -- nothing to skip.
        int before1 = embedder.calls.get();
        var r1 = svc.writeManyCombined(TENANT_A, col, chunks,
            List.of(doc("cw.12a", rows)), null, false, false);
        assertThat(r1.response().get("embed_skipped")).as("call 1: all new").isEqualTo(0);
        assertThat(r1.response().get("embed_embedded")).as("call 1: all new").isEqualTo(5);
        assertThat(embedder.calls.get() - before1).as("call 1: embedder ground truth").isEqualTo(5);

        // Call 2: SAME chashes, SAME text, force_re_embed=false -- the
        // existence-partition must skip every one of them (RDR-181).
        int before2 = embedder.calls.get();
        var r2 = svc.writeManyCombined(TENANT_A, col, chunks,
            List.of(doc("cw.12b", rows)), null, false, false);
        assertThat(r2.response().get("embed_skipped")).as("call 2: skip fires normally").isEqualTo(5);
        assertThat(r2.response().get("embed_embedded")).as("call 2: skip fires normally").isEqualTo(0);
        assertThat(embedder.calls.get() - before2)
            .as("ground truth: the embedder must see ZERO of these texts on the skip path").isEqualTo(0);

        // Call 3: SAME chashes, SAME text, but force_re_embed=true --
        // deliberately disables the existence-partition. If the response
        // counter merely reported a constant, or the partition itself were
        // broken and the counter blindly reported skips regardless, THIS
        // assertion catches it: the numbers must flip back to all-embedded.
        int before3 = embedder.calls.get();
        var r3 = svc.writeManyCombined(TENANT_A, col, chunks,
            List.of(doc("cw.12c", rows)), null, false, true);
        assertThat(r3.response().get("embed_skipped"))
            .as("call 3: force_re_embed must disable the skip -- signal must CHANGE, not stay constant")
            .isEqualTo(0);
        assertThat(r3.response().get("embed_embedded")).as("call 3: force_re_embed").isEqualTo(5);
        assertThat(embedder.calls.get() - before3)
            .as("ground truth: force_re_embed must actually re-invoke the embedder on all 5")
            .isEqualTo(5);
    }

    /** Deterministic, dim-384 embedder that counts every text it is asked to embed. */
    static final class CountingFakeEmbedder implements Embedder {
        final AtomicInteger calls = new AtomicInteger();

        @Override
        public List<float[]> embed(List<String> texts) {
            calls.addAndGet(texts.size());
            List<float[]> out = new ArrayList<>(texts.size());
            for (String t : texts) {
                float[] v = new float[384];
                v[Math.floorMod(t.hashCode(), 384)] = 1.0f;
                out.add(v);
            }
            return out;
        }

        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            return new EmbedResult(embed(texts), texts.size());
        }

        @Override
        public String modelToken() {
            return "minilm-l6-v2-384";
        }
    }
}
