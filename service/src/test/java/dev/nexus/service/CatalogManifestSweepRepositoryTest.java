// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
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
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-eslkl / T2 nexus/design-eslkl-hook-lock-narrowing §8.1 (engine half) —
 * {@code CatalogRepository#getChunkChashesMany} and the {@code sweep}-flagged
 * overload of {@code writeManifestMany} (the superseded-vector sweep folded
 * server-side into the per-doc write transaction).
 *
 * <p>Hermetic Testcontainers PG, mirroring {@code TaxonomyAssignFromChashesRepositoryTest}'s
 * structure (this is the fresh precedent named by the dispatch brief).
 *
 * <p>Route/JSON-contract coverage lives in {@code CatalogHandlerSweepAndChashesManyTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogManifestSweepRepositoryTest {

    private static final String SVC_ROLE = "svc_sweep_test";
    private static final String SVC_PASS = "svc_sweep_test_pass";
    private static final String TENANT_A = "sweep-tenant-a";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;

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
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
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
            "tumbler", tumbler, "title", "sweep-test-" + tumbler,
            "content_type", "code", "corpus", "code",
            "physical_collection", collection, "chunk_count", 0));
    }

    /** Registers the collection FK target, then inserts a zero-vector 384-dim chunk row. */
    private void seedChunk384(String tenant, String collection, String hexChash) throws Exception {
        repo.upsertCollection(tenant, Map.of(
            "name", collection, "content_type", "code", "owner_id", "sweep-owner",
            "embedding_model", "minilm-l6-v2-384", "model_version", "v1"));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("SET nexus.tenant = '" + tenant + "'");
            String zeroVec = "[" + "0,".repeat(383) + "0]";
            var ps = su.prepareStatement(
                "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding)"
                + " VALUES (?, ?, ?, ?, ?::vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
            ps.setString(1, tenant);
            ps.setString(2, collection);
            ps.setBytes(3, java.util.HexFormat.of().parseHex(hexChash));
            ps.setString(4, "seed text " + hexChash);
            ps.setString(5, zeroVec);
            ps.executeUpdate();
        }
    }

    private boolean chunk384Exists(String tenant, String collection, String hexChash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ps = su.prepareStatement(
                "SELECT 1 FROM nexus.chunks_384 WHERE tenant_id = ? AND collection = ? AND chash = ?");
            ps.setString(1, tenant);
            ps.setString(2, collection);
            ps.setBytes(3, java.util.HexFormat.of().parseHex(hexChash));
            return ps.executeQuery().next();
        }
    }

    // ── getChunkChashesMany (nexus-eslkl §8.1 read side) ──────────────────────────

    @Test @Order(1)
    void getChunkChashesMany_multiDoc_orderedByPosition_missingDocsAbsent() {
        String col = "code__gccm1__minilm-l6-v2-384__v1";
        registerDoc(TENANT_A, "gccm.1", col);
        registerDoc(TENANT_A, "gccm.2", col);
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "gccm.1", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 1, "chash", ch("gccm1b"), "chunk_index", 1),
                Map.<String, Object>of("position", 0, "chash", ch("gccm1a"), "chunk_index", 0))),
            Map.<String, Object>of("doc_id", "gccm.2", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("gccm2a"), "chunk_index", 0)))));

        var result = repo.getChunkChashesMany(TENANT_A,
            List.of("gccm.1", "gccm.2", "gccm.never-registered"));

        assertThat(result).containsOnlyKeys("gccm.1", "gccm.2");
        assertThat(result.get("gccm.1"))
            .as("ordered by position, not insertion order")
            .containsExactly(ch("gccm1a"), ch("gccm1b"));
        assertThat(result.get("gccm.2")).containsExactly(ch("gccm2a"));
    }

    @Test @Order(2)
    void getChunkChashesMany_tombstonedDoc_excluded() {
        String col = "code__gccm2__minilm-l6-v2-384__v1";
        registerDoc(TENANT_A, "gccm.tomb", col);
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "gccm.tomb", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("gccmtomb"), "chunk_index", 0)))));
        assertThat(repo.getChunkChashesMany(TENANT_A, List.of("gccm.tomb"))).containsKey("gccm.tomb");

        repo.deleteDocument(TENANT_A, "gccm.tomb");

        assertThat(repo.getChunkChashesMany(TENANT_A, List.of("gccm.tomb")))
            .as("a tombstoned doc's manifest rows must not surface as a live before-read")
            .doesNotContainKey("gccm.tomb");
    }

    @Test @Order(3)
    void getChunkChashesMany_emptyInput_returnsEmptyMap() {
        assertThat(repo.getChunkChashesMany(TENANT_A, List.of())).isEmpty();
        assertThat(repo.getChunkChashesMany(TENANT_A, null)).isEmpty();
    }

    // ── writeManifestMany sweep fold (nexus-eslkl §8.1 write side) ────────────────

    @Test @Order(10)
    void writeManifestMany_sweepTrue_dropsUnreferencedChash_sweepsFromChunks384() throws Exception {
        String col = "code__swp1__minilm-l6-v2-384__v1";
        String x = ch("swp1-x");
        seedChunk384(TENANT_A, col, x);
        registerDoc(TENANT_A, "swp.1", col);
        // Seed A's manifest referencing x (no sweep needed on the seed write).
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.1", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", x, "chunk_index", 0)))));
        assertThat(chunk384Exists(TENANT_A, col, x)).isTrue();

        // Replace with a manifest that drops x — nothing else references it — sweep=true.
        var result = repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.1", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("swp1-y"), "chunk_index", 0)))),
            null, true);

        assertThat(result.get("docs")).isEqualTo(1);
        assertThat(result.get("swept")).as("x was dropped and unreferenced elsewhere").isEqualTo(1);
        assertThat(result.get("sweep_skipped")).isEqualTo(0);
        @SuppressWarnings("unchecked")
        var detail = (List<Map<String, Object>>) result.get("sweep_detail");
        assertThat(detail).singleElement().satisfies(d -> {
            assertThat(d.get("doc_id")).isEqualTo("swp.1");
            assertThat(d.get("dropped")).isEqualTo(1);
            assertThat(d.get("swept")).isEqualTo(1);
            assertThat(d.get("kept")).isEqualTo(0);
        });
        assertThat(chunk384Exists(TENANT_A, col, x))
            .as("orphaned chunk actually removed from chunks_384").isFalse();
    }

    @Test @Order(11)
    void writeManifestMany_sweepTrue_sharedChash_notSwept() throws Exception {
        String col = "code__swp2__minilm-l6-v2-384__v1";
        String shared = ch("swp2-shared");
        seedChunk384(TENANT_A, col, shared);
        registerDoc(TENANT_A, "swp.2a", col);
        registerDoc(TENANT_A, "swp.2b", col);
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.2a", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", shared, "chunk_index", 0))),
            Map.<String, Object>of("doc_id", "swp.2b", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", shared, "chunk_index", 0)))));

        // A drops `shared`; B STILL references it — the union guard must keep it.
        var result = repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.2a", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("swp2-new"), "chunk_index", 0)))),
            null, true);

        assertThat(result.get("swept")).as("shared chash must survive the union guard").isEqualTo(0);
        @SuppressWarnings("unchecked")
        var detail = (List<Map<String, Object>>) result.get("sweep_detail");
        assertThat(detail).singleElement().satisfies(d -> {
            assertThat(d.get("dropped")).isEqualTo(1);
            assertThat(d.get("swept")).isEqualTo(0);
            assertThat(d.get("kept")).isEqualTo(1);
        });
        assertThat(chunk384Exists(TENANT_A, col, shared))
            .as("B's live reference must keep the shared chunk").isTrue();
    }

    @Test @Order(12)
    void writeManifestMany_sweepTrue_genuineManifestLessNote_notSwept() throws Exception {
        // nexus-nl3fn (substantive-critic round 2 Critical): the PRIOR version
        // of this test never built a real manifest-less note — it built a
        // chash that WAS manifested (by swp.3 itself) and proved the SHARED-
        // CHASH guard, not the NOTES guard. A genuine note (indexer_utils.py
        // is_note_shaped: a live catalog_documents row with file_path empty
        // AND metadata->>'doc_id' == its own identity chash) mirrors
        // store_hook.py's catalog_store_hook_tracked — writer.register(...,
        // meta={"doc_id": doc_id}) — and CRITICALLY carries ZERO
        // catalog_document_chunks rows anywhere, ever (the legacy pre-
        // nexus-vw594 shape, still live in production today; current
        // store_put ALSO writes a manifest row for the note's own document
        // via store_put_manifest_direct, but that fix does not retroactively
        // manifest pre-existing legacy notes).
        String col = "code__swp3__minilm-l6-v2-384__v1";
        String noteChash = ch("swp3-genuine-note");
        seedChunk384(TENANT_A, col, noteChash);
        // The note's OWN catalog_documents row — deliberately NO
        // writeManifestMany call for this tumbler, so it never gets a
        // catalog_document_chunks row (the defining "manifest-less" shape).
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "swp.3note", "title", "genuine manifest-less note",
            "content_type", "knowledge", "corpus", "knowledge",
            "physical_collection", col, "chunk_count", 0,
            "metadata", Map.of("doc_id", noteChash)));
        assertThat(repo.getManifest(TENANT_A, "swp.3note"))
            .as("sanity: the note itself carries NO manifest rows anywhere").isEmpty();

        // An UNRELATED indexed document happens to contain byte-identical
        // text (content addressing collapses them to the SAME T3 row) and
        // later drops its own reference — the classic superseded-vector
        // trigger. Once dropped, NOTHING manifests noteChash (the note
        // never did, and swp.3doc just stopped) — the shared-chash union
        // guard ALONE would read this as "unreferenced" and delete it.
        registerDoc(TENANT_A, "swp.3doc", col);
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.3doc", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", noteChash, "chunk_index", 0)))));
        var result = repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.3doc", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("swp3-doc-new"), "chunk_index", 0)))),
            null, true);

        assertThat(result.get("swept"))
            .as("a genuine manifest-less note must survive its shared T3 row even though "
                + "nothing currently manifests it -- the notes guard, not the union guard, "
                + "is what protects it here").isEqualTo(0);
        assertThat(chunk384Exists(TENANT_A, col, noteChash))
            .as("note's T3 row must be untouched").isTrue();
    }

    @Test @Order(13)
    void writeManifestMany_sweepFalse_backwardCompatible_noSweepSideEffects() throws Exception {
        String col = "code__swp4__minilm-l6-v2-384__v1";
        String x = ch("swp4-x");
        seedChunk384(TENANT_A, col, x);
        registerDoc(TENANT_A, "swp.4", col);
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.4", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", x, "chunk_index", 0)))));

        // sweep OMITTED (3-arg and 2-arg overloads) — must be byte-for-byte
        // identical to the pre-eslkl behaviour: x survives even though it is
        // dropped, and the envelope's sweep fields are all zero/empty.
        var result = repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.4", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("swp4-y"), "chunk_index", 0)))));

        assertThat(result.get("swept")).isEqualTo(0);
        assertThat(result.get("sweep_skipped")).isEqualTo(0);
        assertThat((List<?>) result.get("sweep_detail")).isEmpty();
        assertThat(chunk384Exists(TENANT_A, col, x))
            .as("sweep=false: dropped chash left untouched, exactly like before this feature").isTrue();
    }

    @Test @Order(14)
    void writeManifestMany_sweepTrue_noDroppedChashes_emptySweepDetail() {
        String col = "code__swp5__minilm-l6-v2-384__v1";
        registerDoc(TENANT_A, "swp.5", col);
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.5", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("swp5-a"), "chunk_index", 0)))));

        // Replace keeps the SAME chash at the same position — nothing dropped.
        var result = repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.5", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("swp5-a"), "chunk_index", 0)))),
            null, true);

        assertThat(result.get("swept")).isEqualTo(0);
        assertThat((List<?>) result.get("sweep_detail"))
            .as("no chash dropped -> no sweep attempt at all, not a zero-swept entry")
            .isEmpty();
    }

    @Test @Order(15)
    void writeManifestMany_sweepTrue_deletePermissionDenied_failsOpen_manifestWriteStillCommits() throws Exception {
        String col = "code__swp6__minilm-l6-v2-384__v1";
        String x = ch("swp6-x");
        seedChunk384(TENANT_A, col, x);
        registerDoc(TENANT_A, "swp.6", col);
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.6", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", x, "chunk_index", 0)))));

        // Force a REAL SQL error at the sweep's DELETE statement (permission
        // denied), without touching the connection's health, so the txn-wide
        // fail-open mechanism (SAVEPOINT + ROLLBACK TO SAVEPOINT) is what's
        // under test — not a fabricated Java exception.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("REVOKE DELETE ON nexus.chunks_384 FROM " + SVC_ROLE);
            su.createStatement().execute("REVOKE DELETE ON nexus.chunks_768 FROM " + SVC_ROLE);
            su.createStatement().execute("REVOKE DELETE ON nexus.chunks_1024 FROM " + SVC_ROLE);
        }
        try {
            var result = repo.writeManifestMany(TENANT_A, List.of(
                Map.<String, Object>of("doc_id", "swp.6", "rows", List.<Map<String, Object>>of(
                    Map.<String, Object>of("position", 0, "chash", ch("swp6-y"), "chunk_index", 0)))),
                null, true);

            assertThat(result.get("docs"))
                .as("the manifest write itself must NOT fail because the sweep couldn't run")
                .isEqualTo(1);
            assertThat((List<?>) result.get("failed_doc_ids")).isEmpty();
            assertThat(result.get("sweep_skipped"))
                .as("the sweep attempt is recorded as skipped, never silent").isEqualTo(1);
            assertThat(result.get("swept")).isEqualTo(0);
            // The manifest replace committed: swp.6 now has the NEW row, not the old.
            assertThat(repo.getManifest(TENANT_A, "swp.6"))
                .singleElement()
                .satisfies(r -> assertThat(r.get("chash")).isEqualTo(ch("swp6-y")));
            // The old chunk row is untouched (delete was refused, not partially applied).
            assertThat(chunk384Exists(TENANT_A, col, x))
                .as("permission-denied sweep must leave the chunk exactly as it was").isTrue();
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute("GRANT DELETE ON nexus.chunks_384 TO " + SVC_ROLE);
                su.createStatement().execute("GRANT DELETE ON nexus.chunks_768 TO " + SVC_ROLE);
                su.createStatement().execute("GRANT DELETE ON nexus.chunks_1024 TO " + SVC_ROLE);
            }
        }
    }

    @Test @Order(16)
    void sweepGuardSql_crossTransactionWriteSkew_isTheDocumentedNexus11gh6Gap() throws Exception {
        // nexus-11gh6 (substantive-critic + code-review-expert, round 2,
        // independent convergence): DELIBERATELY NOT fixed (accept-and-
        // downgrade decision, recorded on the bead and in nexus-wxjr6). This
        // test PROVES the residual gap exists and characterizes it exactly,
        // rather than either (a) silently claiming full closure, or (b)
        // asserting something that "passes regardless of the race" the way
        // the CountDownLatch-based version of this test did (code-review-
        // expert Important-1: thread-start synchronization does not force
        // the actual interleaving point, so that test could never reliably
        // demonstrate anything either way).
        //
        // DETERMINISTIC by construction — no threads, no timing, no
        // sleep/latch luck: two raw JDBC connections under explicit manual
        // transaction control let this test dictate the exact COMMIT order,
        // which is what PostgreSQL's READ COMMITTED snapshot-per-statement
        // semantics actually key on. This reproduces the gap on every run,
        // every machine, unconditionally — the opposite of a flaky test.
        //
        // Scenario: writer A's sweep DELETE (with its NOT EXISTS guard) is
        // issued and computes its result while still UNCOMMITTED. Writer B
        // then independently INSERTs and COMMITS a brand-new manifest
        // reference to the SAME chash. Writer A commits afterward. A's
        // DELETE decision was correct as of ITS OWN statement's snapshot —
        // nothing violates any single transaction's ACID guarantees — but
        // the FINAL committed state is inconsistent: B's manifest now
        // references a chash whose T3 row A already removed. This is the
        // textbook write-skew anomaly SERIALIZABLE isolation exists to
        // prevent; READ COMMITTED (PostgreSQL's default, and what this
        // engine uses throughout) does not.
        String col = "code__swp8__minilm-l6-v2-384__v1";
        String shared = ch("swp8-writeskew");
        seedChunk384(TENANT_A, col, shared);
        registerDoc(TENANT_A, "swp.8a", col);
        registerDoc(TENANT_A, "swp.8b", col);
        // A references `shared`, then drops it (sweep=false — this just
        // establishes "nothing currently manifests `shared`", the
        // precondition a real sweep's guard would also observe).
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.8a", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", shared, "chunk_index", 0)))));
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.8a", "rows", List.<Map<String, Object>>of())));
        assertThat(chunk384Exists(TENANT_A, col, shared))
            .as("precondition: shared still physically present, nothing manifests it yet").isTrue();

        // The EXACT guard shape sweepChunks384 uses (both NOT EXISTS
        // clauses) — driven manually so this test controls commit order.
        String deleteSql =
            "DELETE FROM nexus.chunks_384 c "
            + "WHERE c.tenant_id = ? AND c.collection = ? AND c.chash = decode(?, 'hex') "
            + "AND NOT EXISTS (SELECT 1 FROM nexus.catalog_document_chunks m "
            + "  JOIN nexus.catalog_documents d ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id "
            + "  WHERE m.tenant_id = c.tenant_id AND m.chash = c.chash AND d.deleted_at IS NULL) "
            + "AND NOT EXISTS (SELECT 1 FROM nexus.catalog_documents d2 "
            + "  WHERE d2.tenant_id = ? AND d2.physical_collection = ? AND d2.deleted_at IS NULL "
            + "  AND (d2.file_path IS NULL OR d2.file_path = '') "
            + "  AND (d2.metadata ->> 'doc_id') = ?)";

        try (Connection connA = dsConnection(); Connection connB = dsConnection()) {
            connA.setAutoCommit(false);
            connB.setAutoCommit(false);

            // Writer A: compute + apply the delete decision, do NOT commit yet.
            try (var stA = connA.prepareStatement("SET nexus.tenant = '" + TENANT_A + "'")) {
                stA.execute();
            }
            int deletedByA;
            try (var psA = connA.prepareStatement(deleteSql)) {
                psA.setString(1, TENANT_A);
                psA.setString(2, col);
                psA.setString(3, shared);
                psA.setString(4, TENANT_A);
                psA.setString(5, col);
                psA.setString(6, shared);
                deletedByA = psA.executeUpdate();
            }
            assertThat(deletedByA)
                .as("A's guard is satisfied at ITS statement's snapshot: nothing (yet) "
                    + "references `shared`, so A decides to delete it").isEqualTo(1);

            // Writer B: publish a FRESH manifest reference to the SAME chash,
            // in a fully separate, independently-committed transaction.
            try (var stB = connB.prepareStatement("SET nexus.tenant = '" + TENANT_A + "'")) {
                stB.execute();
            }
            try (var psB = connB.prepareStatement(
                    "INSERT INTO nexus.catalog_document_chunks "
                    + "(tenant_id, doc_id, position, chash, chunk_index, collection) "
                    + "VALUES (?, ?, 0, decode(?, 'hex'), 0, ?)")) {
                psB.setString(1, TENANT_A);
                psB.setString(2, "swp.8b");
                psB.setString(3, shared);
                psB.setString(4, col);
                psB.executeUpdate();
            }
            connB.commit();

            // A commits AFTER B — A's already-computed DELETE stands.
            connA.commit();
        }

        // The documented nexus-11gh6 outcome: B's manifest reference exists
        // (repo-level read, fresh connection, sees both connA's and connB's
        // committed work) but the T3 row it points at is gone. THIS is the
        // inconsistency the accept-and-downgrade decision accepts as a
        // residual risk, mitigated in production ONLY by the client's own
        // sweep remaining live (nexus-wxjr6) until this is closed for real
        // (SERIALIZABLE + retry, or an advisory lock keyed on chash with a
        // deadlock-safe acquisition order across a batch).
        boolean bReferencesShared = repo.getManifest(TENANT_A, "swp.8b").stream()
            .anyMatch(r -> shared.equals(r.get("chash")));
        assertThat(bReferencesShared)
            .as("B's manifest reference committed and is live").isTrue();
        assertThat(chunk384Exists(TENANT_A, col, shared))
            .as("nexus-11gh6: A's earlier, now-committed DELETE removed the T3 row B's "
                + "manifest now points at -- the write-skew this test documents, not fixes")
            .isFalse();
    }

    @Test @Order(17)
    void writeManifestMany_sweepTrue_beforeReadFails_countsAsSweepSkipped_manifestWriteStillCommits() throws Exception {
        // code-review-expert Important-2: a FAILED before-read (currentManifestChashes)
        // was falling back to Set.of() indistinguishably from a legitimately
        // EMPTY manifest (a brand-new doc with nothing to drop) -- so a failed
        // read was silently absorbed: not in sweep_skipped, not in
        // sweep_detail, nowhere. Forced here via a column-level REVOKE
        // (REVOKE-style, matching the delete-side fail-open test) that blocks
        // JUST the chash column read -- writeManifestRows's own DELETE/INSERT
        // on this table never SELECT chash, so the manifest write itself is
        // unaffected; only currentManifestChashes's `SELECT chash ...` breaks.
        String col = "code__swp9__minilm-l6-v2-384__v1";
        registerDoc(TENANT_A, "swp.9", col);
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.9", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("swp9-x"), "chunk_index", 0)))));

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("REVOKE SELECT ON nexus.catalog_document_chunks FROM " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT (tenant_id, doc_id, \"position\", chunk_index, line_start, "
                + "line_end, char_start, char_end, collection) ON nexus.catalog_document_chunks TO "
                + SVC_ROLE);
        }
        // Only the operation under test runs with the column revoked — every
        // verification read below (including this test's own getManifest
        // assertion, which SELECTs chash) needs the grant restored FIRST, or
        // it fails for the SAME reason the before-read does, masking what
        // this test is actually proving.
        Map<String, Object> result;
        try {
            result = repo.writeManifestMany(TENANT_A, List.of(
                Map.<String, Object>of("doc_id", "swp.9", "rows", List.<Map<String, Object>>of(
                    Map.<String, Object>of("position", 0, "chash", ch("swp9-y"), "chunk_index", 0)))),
                null, true);
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "GRANT SELECT ON nexus.catalog_document_chunks TO " + SVC_ROLE);
            }
        }

        assertThat(result.get("docs"))
            .as("the manifest write must NOT fail because the before-read couldn't run")
            .isEqualTo(1);
        assertThat((List<?>) result.get("failed_doc_ids")).isEmpty();
        assertThat(result.get("sweep_skipped"))
            .as("a before-read failure must be counted, exactly like a delete-side failure")
            .isEqualTo(1);
        assertThat(result.get("swept")).isEqualTo(0);
        @SuppressWarnings("unchecked")
        var detail = (List<Map<String, Object>>) result.get("sweep_detail");
        assertThat(detail).as("the failure must be visible in sweep_detail, not silently absorbed")
            .singleElement()
            .satisfies(d -> {
                assertThat(d.get("doc_id")).isEqualTo("swp.9");
                assertThat(d.get("errored")).isEqualTo(true);
            });
        assertThat(repo.getManifest(TENANT_A, "swp.9"))
            .singleElement()
            .satisfies(r -> assertThat(r.get("chash")).isEqualTo(ch("swp9-y")));
    }

    /** Raw connection to the service role's own database (test-controlled transaction). */
    private Connection dsConnection() throws java.sql.SQLException {
        return svcDs.getConnection();
    }
}
