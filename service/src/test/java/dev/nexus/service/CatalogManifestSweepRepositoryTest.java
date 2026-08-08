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
import java.sql.SQLException;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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
    void sweepGate_crossTransactionWriteSkew_isClosed_nexus11gh6() throws Exception {
        // nexus-11gh6 rev 2 §7a: this test PREVIOUSLY documented an open
        // write-skew gap (two raw JDBC connections under manual transaction
        // control reproducing "A's DELETE decides before B's INSERT commits,
        // A commits after, B ends up dangling"). The per-(tenant, collection)
        // advisory gate (acquireSweepGateShared / acquireSweepGateExclusive)
        // now makes that exact interleaving impossible: either the sweeper's
        // EXCLUSIVE acquire is refused while a writer holds SHARED (sub-case
        // 1), or the writer commits first and the sweeper's guard, evaluated
        // fresh under the gate, correctly sees it (sub-case 2). Both leave
        // the chunk intact — the opposite of the anomaly this test used to
        // pin. DETERMINISTIC by construction (no threads, no sleep/latch
        // luck): manual transaction control dictates the exact ordering in
        // both sub-cases, on every run, every machine.
        //
        // Note: this drives the gate SQL directly on two hand-held JDBC
        // connections (mirroring the original test's structure) rather than
        // through the production writeManifestMany path — Order(20)-(25)
        // below cover the SAME closure behaviourally, through the real
        // public entry points.

        // ── Sub-case 1: writer B takes the gate SHARED first and holds it;
        //    sweeper A's EXCLUSIVE acquire is refused (55P03) and never even
        //    reaches the DELETE. ──
        String col1 = "code__swp8a__minilm-l6-v2-384__v1";
        String shared1 = ch("swp8a-writeskew");
        seedChunk384(TENANT_A, col1, shared1);
        registerDoc(TENANT_A, "swp.8a-a", col1);
        registerDoc(TENANT_A, "swp.8a-b", col1);
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.8a-a", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", shared1, "chunk_index", 0)))));
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.8a-a", "rows", List.<Map<String, Object>>of())));
        assertThat(chunk384Exists(TENANT_A, col1, shared1))
            .as("precondition: shared still physically present, nothing manifests it yet").isTrue();

        try (Connection connA = dsConnection(); Connection connB = dsConnection()) {
            connA.setAutoCommit(false);
            connB.setAutoCommit(false);

            // Writer B: take the gate SHARED exactly as writeManifestRows
            // now does, and hold it open (uncommitted).
            try (var stB = connB.prepareStatement("SET nexus.tenant = '" + TENANT_A + "'")) {
                stB.execute();
            }
            acquireGateShared(connB, TENANT_A, col1);

            // Sweeper A: short lock_timeout, attempt the EXCLUSIVE gate —
            // must be refused (55P03) while B holds SHARED, BEFORE any
            // DELETE statement runs at all.
            try (var stA = connA.prepareStatement("SET nexus.tenant = '" + TENANT_A + "'")) {
                stA.execute();
            }
            assertThatThrownBy(() -> acquireGateExclusive(connA, TENANT_A, col1, 1000))
                .as("A's exclusive acquire must be refused, not silently granted, while B holds SHARED")
                .isInstanceOf(SQLException.class)
                .satisfies(e -> assertThat(((SQLException) e).getSQLState()).isEqualTo("55P03"));
            connA.rollback();

            // B now completes its manifest insert normally and commits.
            try (var psB = connB.prepareStatement(
                    "INSERT INTO nexus.catalog_document_chunks "
                    + "(tenant_id, doc_id, position, chash, chunk_index, collection) "
                    + "VALUES (?, ?, 0, decode(?, 'hex'), 0, ?)")) {
                psB.setString(1, TENANT_A);
                psB.setString(2, "swp.8a-b");
                psB.setString(3, shared1);
                psB.setString(4, col1);
                psB.executeUpdate();
            }
            connB.commit();
        }

        assertThat(repo.getManifest(TENANT_A, "swp.8a-b").stream()
                .anyMatch(r -> shared1.equals(r.get("chash"))))
            .as("B's manifest reference committed and is live").isTrue();
        assertThat(chunk384Exists(TENANT_A, col1, shared1))
            .as("nexus-11gh6 CLOSED: A's exclusive acquire was refused while B held the gate — "
                + "the chunk was never even a delete candidate")
            .isTrue();

        // ── Sub-case 2: writer B commits FIRST; sweeper A's guard, evaluated
        //    fresh under the gate, correctly sees B's committed reference. ──
        String col2 = "code__swp8b__minilm-l6-v2-384__v1";
        String shared2 = ch("swp8b-writeskew");
        seedChunk384(TENANT_A, col2, shared2);
        registerDoc(TENANT_A, "swp.8b-a", col2);
        registerDoc(TENANT_A, "swp.8b-b", col2);
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.8b-a", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", shared2, "chunk_index", 0)))));
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.8b-a", "rows", List.<Map<String, Object>>of())));

        try (Connection connA = dsConnection(); Connection connB = dsConnection()) {
            connA.setAutoCommit(false);
            connB.setAutoCommit(false);

            try (var stB = connB.prepareStatement("SET nexus.tenant = '" + TENANT_A + "'")) {
                stB.execute();
            }
            acquireGateShared(connB, TENANT_A, col2);
            try (var psB = connB.prepareStatement(
                    "INSERT INTO nexus.catalog_document_chunks "
                    + "(tenant_id, doc_id, position, chash, chunk_index, collection) "
                    + "VALUES (?, ?, 0, decode(?, 'hex'), 0, ?)")) {
                psB.setString(1, TENANT_A);
                psB.setString(2, "swp.8b-b");
                psB.setString(3, shared2);
                psB.setString(4, col2);
                psB.executeUpdate();
            }
            connB.commit();   // B fully committed before A even starts.

            try (var stA = connA.prepareStatement("SET nexus.tenant = '" + TENANT_A + "'")) {
                stA.execute();
            }
            acquireGateExclusive(connA, TENANT_A, col2, 2000);
            int deletedByA;
            try (var psA = connA.prepareStatement(SWEEP_GUARD_DELETE_SQL)) {
                psA.setString(1, TENANT_A);
                psA.setString(2, col2);
                psA.setString(3, shared2);
                psA.setString(4, TENANT_A);
                psA.setString(5, col2);
                psA.setString(6, shared2);
                deletedByA = psA.executeUpdate();
            }
            assertThat(deletedByA)
                .as("A's guard, evaluated under the gate, sees B's now-committed reference")
                .isEqualTo(0);
            connA.commit();
        }

        assertThat(chunk384Exists(TENANT_A, col2, shared2))
            .as("nexus-11gh6 CLOSED: the guard correctly retained the chunk").isTrue();
    }

    /** The EXACT guard shape {@code sweepChunks384} uses (both {@code NOT
     *  EXISTS} clauses) — driven manually so hand-written tests can control
     *  commit order around it. */
    private static final String SWEEP_GUARD_DELETE_SQL =
        "DELETE FROM nexus.chunks_384 c "
        + "WHERE c.tenant_id = ? AND c.collection = ? AND c.chash = decode(?, 'hex') "
        + "AND NOT EXISTS (SELECT 1 FROM nexus.catalog_document_chunks m "
        + "  JOIN nexus.catalog_documents d ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id "
        + "  WHERE m.tenant_id = c.tenant_id AND m.chash = c.chash AND d.deleted_at IS NULL) "
        + "AND NOT EXISTS (SELECT 1 FROM nexus.catalog_documents d2 "
        + "  WHERE d2.tenant_id = ? AND d2.physical_collection = ? AND d2.deleted_at IS NULL "
        + "  AND (d2.file_path IS NULL OR d2.file_path = '') "
        + "  AND (d2.metadata ->> 'doc_id') = ?)";

    /** Hand-drives {@code acquireSweepGateShared}'s exact SQL shape on a raw
     *  connection, for tests that need manual transaction control around it. */
    private static void acquireGateShared(Connection conn, String tenant, String collection) throws SQLException {
        try (var ps = conn.prepareStatement("SELECT pg_advisory_xact_lock_shared(hashtext(?))")) {
            ps.setString(1, "sweepgate:" + tenant + "/" + collection);
            ps.execute();
        }
    }

    /** Hand-drives {@code acquireSweepGateExclusive}'s exact SQL shape
     *  (lock_timeout + statement_timeout, then the exclusive acquire) on a
     *  raw connection. */
    private static void acquireGateExclusive(Connection conn, String tenant, String collection, int lockTimeoutMs)
            throws SQLException {
        try (var ps = conn.prepareStatement("SELECT set_config('lock_timeout', ?, true)")) {
            ps.setString(1, String.valueOf(lockTimeoutMs));
            ps.execute();
        }
        try (var ps = conn.prepareStatement("SELECT pg_advisory_xact_lock(hashtext(?))")) {
            ps.setString(1, "sweepgate:" + tenant + "/" + collection);
            ps.execute();
        }
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

    // ── nexus-11gh6 rev 2 §7b: sweeper blocked by an externally-held SHARED gate ──

    @Test @Order(20)
    void writeManifestMany_sweepTrue_sweeperBlockedByExternalSharedGate_failsOpen_thenSweepsOnRetry() throws Exception {
        String col = "code__swp10__minilm-l6-v2-384__v1";
        String x = ch("swp10-x");
        seedChunk384(TENANT_A, col, x);
        registerDoc(TENANT_A, "swp.10", col);
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.10", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", x, "chunk_index", 0)))));

        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            try (var st = external.prepareStatement("SET nexus.tenant = '" + TENANT_A + "'")) {
                st.execute();
            }
            acquireGateShared(external, TENANT_A, col);

            // Replace with a manifest that drops x, sweep=true. The sweeper's
            // OWN transaction (fired AFTER the manifest transaction below
            // commits — nexus-11gh6 rev 2 §2.3) must fail to acquire the
            // EXCLUSIVE gate while `external` holds SHARED, and fail OPEN:
            // the manifest write itself must be entirely unaffected.
            var result = repo.writeManifestMany(TENANT_A, List.of(
                Map.<String, Object>of("doc_id", "swp.10", "rows", List.<Map<String, Object>>of(
                    Map.<String, Object>of("position", 0, "chash", ch("swp10-y"), "chunk_index", 0)))),
                null, true);

            assertThat(result.get("docs"))
                .as("the manifest write must commit regardless of sweep gate contention").isEqualTo(1);
            assertThat((List<?>) result.get("failed_doc_ids")).isEmpty();
            assertThat(result.get("sweep_skipped"))
                .as("the sweep could not acquire its gate in time -- fail-open skip, not a failure")
                .isEqualTo(1);
            assertThat(result.get("swept")).isEqualTo(0);
            assertThat(chunk384Exists(TENANT_A, col, x))
                .as("chunk survives while the gate is externally held").isTrue();

            external.rollback();
        }

        // Retry with the gate free: x itself was already dropped from
        // swp.10's manifest by the write above (only ITS sweep attempt was
        // skipped -- the manifest write succeeded), so a fresh drop is
        // needed to exercise "the gate is free, the sweep now completes"
        // rather than re-deciding a doc/chash pair that no longer relates
        // (x is no longer in swp.10's manifest to drop again). Seed a NEW
        // chash z, reference it, then drop it with the gate uncontended.
        String z = ch("swp10-z");
        seedChunk384(TENANT_A, col, z);
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.10", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", z, "chunk_index", 0)))));
        var result2 = repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "swp.10", "rows", List.<Map<String, Object>>of())),
            null, true);
        assertThat(result2.get("sweep_skipped"))
            .as("gate is free now -- no skip this time").isEqualTo(0);
        assertThat(result2.get("swept")).as("gate now free -- the sweep completes").isEqualTo(1);
        assertThat(chunk384Exists(TENANT_A, col, z)).isFalse();
    }

    // ── nexus-11gh6 rev 2 §7c: every public writer entry point BLOCKS on an
    //    externally-held EXCLUSIVE gate, and proceeds once it releases. ──

    /**
     * Common shape: hold the sweep gate EXCLUSIVE on a raw connection, submit
     * {@code writerCall} to a background thread, assert it does NOT complete
     * within a short window (proving it genuinely BLOCKS rather than racing
     * past), release the gate, then assert it completes promptly. Non-vacuous
     * in the direction that matters: a missing/broken gate call makes {@code
     * writerCall} return immediately and the first assertion fails.
     */
    private void assertWriterBlocksOnExternalExclusiveGate(String collection, Runnable writerCall) throws Exception {
        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            try (var st = external.prepareStatement("SET nexus.tenant = '" + TENANT_A + "'")) {
                st.execute();
            }
            // Generous lock_timeout on the EXTERNAL holder's own acquire —
            // it is uncontended, so this returns immediately; it only bounds
            // this helper's own acquisition, never the writer's (writers take
            // the gate SHARED with no timeout at all, by design).
            acquireGateExclusive(external, TENANT_A, collection, 60_000);

            ExecutorService executor = Executors.newSingleThreadExecutor();
            try {
                Future<?> future = executor.submit(writerCall);
                assertThatThrownBy(() -> future.get(750, TimeUnit.MILLISECONDS))
                    .as("writer must BLOCK while the gate is held EXCLUSIVE externally")
                    .isInstanceOf(TimeoutException.class);

                external.rollback();

                future.get(15, TimeUnit.SECONDS);
            } finally {
                executor.shutdownNow();
            }
        }
    }

    @Test @Order(21)
    void writeManifest_blocksOnExternalExclusiveGate_thenProceeds() throws Exception {
        String col = "code__swp11a__minilm-l6-v2-384__v1";
        registerDoc(TENANT_A, "swp.11a", col);
        assertWriterBlocksOnExternalExclusiveGate(col, () ->
            repo.writeManifest(TENANT_A, "swp.11a", List.of(
                Map.<String, Object>of("position", 0, "chash", ch("swp11a-x"), "chunk_index", 0))));
        assertThat(repo.getManifest(TENANT_A, "swp.11a")).hasSize(1);
    }

    @Test @Order(22)
    void writeManifestMany_blocksOnExternalExclusiveGate_thenProceeds() throws Exception {
        String col = "code__swp11b__minilm-l6-v2-384__v1";
        registerDoc(TENANT_A, "swp.11b", col);
        assertWriterBlocksOnExternalExclusiveGate(col, () ->
            repo.writeManifestMany(TENANT_A, List.of(
                Map.<String, Object>of("doc_id", "swp.11b", "rows", List.<Map<String, Object>>of(
                    Map.<String, Object>of("position", 0, "chash", ch("swp11b-x"), "chunk_index", 0))))));
        assertThat(repo.getManifest(TENANT_A, "swp.11b")).hasSize(1);
    }

    @Test @Order(23)
    void appendManifestChunks_blocksOnExternalExclusiveGate_thenProceeds() throws Exception {
        String col = "code__swp11c__minilm-l6-v2-384__v1";
        registerDoc(TENANT_A, "swp.11c", col);
        assertWriterBlocksOnExternalExclusiveGate(col, () ->
            repo.appendManifestChunks(TENANT_A, "swp.11c", List.of(
                Map.<String, Object>of("position", 0, "chash", ch("swp11c-x"), "chunk_index", 0))));
        assertThat(repo.getManifest(TENANT_A, "swp.11c")).hasSize(1);
    }

    @Test @Order(24)
    void importChunk_blocksOnExternalExclusiveGate_thenProceeds() throws Exception {
        String col = "code__swp11d__minilm-l6-v2-384__v1";
        registerDoc(TENANT_A, "swp.11d", col);
        assertWriterBlocksOnExternalExclusiveGate(col, () ->
            repo.importChunk(TENANT_A, "swp.11d",
                Map.<String, Object>of("position", 0, "chash", ch("swp11d-x"), "chunk_index", 0)));
        assertThat(repo.getManifest(TENANT_A, "swp.11d")).hasSize(1);
    }

    @Test @Order(25)
    void importChunksBatch_blocksOnExternalExclusiveGate_thenProceeds() throws Exception {
        String col = "code__swp11e__minilm-l6-v2-384__v1";
        registerDoc(TENANT_A, "swp.11e", col);
        assertWriterBlocksOnExternalExclusiveGate(col, () ->
            repo.importChunksBatch(TENANT_A, "swp.11e", List.of(
                Map.<String, Object>of("position", 0, "chash", ch("swp11e-x"), "chunk_index", 0))));
        assertThat(repo.getManifest(TENANT_A, "swp.11e")).hasSize(1);
    }

    /** Raw connection to the service role's own database (test-controlled transaction). */
    private Connection dsConnection() throws java.sql.SQLException {
        return svcDs.getConnection();
    }
}
