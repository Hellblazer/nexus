// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-sybbh (P1) — substrate proof that {@code nexus.gc_audit} actually
 * gets written by the two engine-side reap paths this bead wired: the SQL-side
 * {@code nexus.purge_trash} routine (catalog-033-1) and the Java-side manifest
 * sweep, {@link CatalogRepository#writeManifestMany(String, List, String, Map,
 * boolean)}'s {@code sweep=true} path -> {@link CatalogRepository#insertGcAuditRow}
 * (catalog-033 header, item 4 / {@code runSweepTransaction}).
 *
 * <p>Before this bead, {@code nexus.gc_audit} had a write surface
 * ({@code recordGcAudit}) and ZERO producers — every reap ran with no forensic
 * trail (the ~233 lost {@code store_put} chunks in nexus-3n7pr were the first
 * casualty, found retroactively with nothing to consult). This suite pins that
 * a real reap on real PG now leaves a row behind, not merely that the
 * client-facing recorder can be called directly ({@code
 * CatalogEngineDefects70Test} already covers that half).
 *
 * <p>Deliberately narrow: this does NOT re-prove {@code purge_trash}'s or the
 * sweep's own delete semantics (grace-window scoping, union-guard sharing,
 * etc.) — {@link CatalogPurgeTrashTest} and {@link
 * CatalogManifestSweepRepositoryTest} already own that. This suite's only job
 * is "did the delete leave an audit row, with the right shape, in the SAME
 * transaction."
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogGcAuditProducersTest {

    private static final String SVC_ROLE = "svc_gc_audit_producers";
    private static final String SVC_PASS = "svc_gc_audit_producers_pass";
    private static final String TENANT = "gc-audit-producers";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;
    PgVectorRepository vecRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            // purge_trash / gc_quarantine_orphans EXECUTE are not part of
            // bootstrapServiceRole's fixed grant set (nexus-cbo4a batch 1b) --
            // kept as explicit grants.
            su.createStatement().execute("GRANT EXECUTE ON FUNCTION nexus.purge_trash(interval) TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.gc_quarantine_orphans(int, text, text, text, text, int) TO " + SVC_ROLE);
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
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    private void insertManifestRow(Connection su, String tenant, String docId, String chashHex, String collection)
            throws Exception {
        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
                + "VALUES (?, ?, 0, decode(?, 'hex'), ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, docId);
            ps.setString(3, chashHex);
            ps.setString(4, collection);
            ps.execute();
        }
    }

    // ── purge_trash: SQL-side producer (catalog-033-1) ─────────────────────────

    @Test @Order(10)
    void purgeTrash_agedTombstoneReap_insertsGcAuditRow() throws Exception {
        String collection = "knowledge__gcaudit-purge__minilm-l6-v2-384__v1";
        String docId = "gc-audit-purge-doc";
        String chash = ch("gc-audit-purge-chunk");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT + "', '"
                + collection + "') ON CONFLICT DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) VALUES ('"
                + TENANT + "', '" + docId + "', 'GC Audit Purge Doc', '" + collection + "')");
        }
        vecRepo.upsertChunks(TENANT, collection, List.of(chash), List.of("gc audit purge text"), List.of(Map.of()));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertManifestRow(su, TENANT, docId, chash, collection);
        }

        assertThat(repo.deleteDocument(TENANT, docId)).isEqualTo(1);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = NOW() - interval '60 days' "
                + "WHERE tenant_id = '" + TENANT + "' AND tumbler = '" + docId + "'");
        }

        // Nothing recorded for this operation before the purge runs.
        assertThat(repo.listGcAudit(TENANT, null, "purge_trash", 100, 0)).isEmpty();

        Map<String, Object> result = repo.purgeTrash(TENANT, 30);
        assertThat(((Number) result.get("documents_purged")).longValue()).isEqualTo(1L);

        var rows = repo.listGcAudit(TENANT, null, "purge_trash", 100, 0);
        assertThat(rows)
            .as("nexus.purge_trash must leave a gc_audit row behind IN THE SAME TRANSACTION as its "
                + "delete -- this is the exact gap nexus-sybbh exists to close")
            .hasSize(1);
        Map<String, Object> row = rows.get(0);
        assertThat(row.get("actor")).as("server-driven producer, not a client-attributed one").isEqualTo("engine");
        assertThat(row.get("dry_run")).isEqualTo(false);
        assertThat(((Number) row.get("chash_count")).intValue())
            .as("exactly the one chunk purge_trash actually swept").isEqualTo(1);
        @SuppressWarnings("unchecked")
        var chashes = (List<Object>) row.get("chashes");
        assertThat(chashes).contains(chash);
        @SuppressWarnings("unchecked")
        var details = (Map<String, Object>) row.get("details");
        assertThat(((Number) details.get("documents_purged")).longValue()).isEqualTo(1L);
    }

    // ── manifest sweep: Java-side producer (runSweepTransaction / insertGcAuditRow) ──

    @Test @Order(20)
    void manifestSweep_sweepTrue_dropsUnreferencedChash_insertsGcAuditRow() throws Exception {
        String collection = "code__gcaudit-sweep__minilm-l6-v2-384__v1";
        String docId = "gc-audit-sweep-doc";
        String dropped = ch("gc-audit-sweep-dropped");
        String kept = ch("gc-audit-sweep-kept");

        repo.upsertCollection(TENANT, Map.of(
            "name", collection, "content_type", "code", "owner_id", "gc-audit-sweep-owner",
            "embedding_model", "minilm-l6-v2-384", "model_version", "v1"));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            String zeroVec = "[" + "0,".repeat(383) + "0]";
            for (String c : List.of(dropped)) {
                var ps = su.prepareStatement(
                    "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384)"
                    + " VALUES (?, ?, decode(?, 'hex'), ?, ?::vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
                ps.setString(1, TENANT);
                ps.setString(2, collection);
                ps.setString(3, c);
                ps.setString(4, "seed text " + c);
                ps.setString(5, zeroVec);
                ps.executeUpdate();
            }
        }
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", docId, "title", "gc-audit-sweep-" + docId,
            "content_type", "code", "corpus", "code",
            "physical_collection", collection, "chunk_count", 0));

        // Seed the manifest referencing `dropped` (no sweep on this first write).
        repo.writeManifestMany(TENANT, List.of(
            Map.<String, Object>of("doc_id", docId, "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", dropped, "chunk_index", 0)))), collection);

        assertThat(repo.listGcAudit(TENANT, collection, "sweep_superseded_chunks", 100, 0)).isEmpty();

        // Stub `kept`'s nexus.chunks row too (fk_catalog_chunks_chunk needs it before the
        // manifest INSERT below references it).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            String zeroVec = "[" + "0,".repeat(383) + "0]";
            var ps = su.prepareStatement(
                "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384)"
                + " VALUES (?, ?, decode(?, 'hex'), ?, ?::vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
            ps.setString(1, TENANT);
            ps.setString(2, collection);
            ps.setString(3, kept);
            ps.setString(4, "seed text " + kept);
            ps.setString(5, zeroVec);
            ps.executeUpdate();
        }

        // Replace with a manifest dropping `dropped` (unreferenced elsewhere) -- sweep=true.
        Map<String, Object> result = repo.writeManifestMany(TENANT, List.of(
            Map.<String, Object>of("doc_id", docId, "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", kept, "chunk_index", 0)))), collection, null, true);

        assertThat(result.get("swept")).as("dropped was unreferenced elsewhere -- must sweep").isEqualTo(1);

        var rows = repo.listGcAudit(TENANT, collection, "sweep_superseded_chunks", 100, 0);
        assertThat(rows)
            .as("the manifest-write sweep path (runSweepTransaction) must leave a gc_audit row "
                + "in the SAME transaction as its DELETE -- this is the exact codepath the 233 "
                + "lost store_put chunks (nexus-3n7pr) went through with zero forensic trace")
            .hasSize(1);
        Map<String, Object> row = rows.get(0);
        assertThat(row.get("actor")).isEqualTo("engine");
        assertThat(row.get("dry_run")).isEqualTo(false);
        assertThat(((Number) row.get("chash_count")).intValue()).isEqualTo(1);
        @SuppressWarnings("unchecked")
        var chashes = (List<Object>) row.get("chashes");
        assertThat(chashes).containsExactly(dropped);
        @SuppressWarnings("unchecked")
        var details = (Map<String, Object>) row.get("details");
        assertThat(details.get("doc_id")).isEqualTo(docId);
        assertThat(((Number) details.get("dropped")).intValue()).isEqualTo(1);
    }

    // ── gc_quarantine_orphans: SQL-side producer (catalog-033-2) — sample_limit cap ──

    /**
     * code-review-expert crit-fix critique 2026-08-19: unlike purge_trash /
     * gc_expire_quarantine (both hard-capped in-function), {@code
     * gc_quarantine_orphans}'s gc_audit row previously trusted the caller-
     * supplied {@code sample_limit} (HTTP {@code /gc/quarantine-orphans},
     * {@code VectorHandler}'s {@code optInt} default 20, no upper bound) --
     * a caller could request an unbounded sample and get an unbounded
     * {@code gc_audit.chashes} array. Fixed both ends: {@code VectorHandler}
     * now clamps server-side, and the SQL function (catalog-033-2) enforces
     * the SAME {@link CatalogRepository#GC_AUDIT_MAX_CHASHES} cap
     * independently (defense in depth -- a direct SQL/repository caller
     * bypassing the HTTP handler is still bounded).
     *
     * <p>Pins the cap WITHOUT seeding 5000+ real orphan chunks (prohibitively
     * slow): the clamp fires on the REQUESTED {@code p_sample_limit} value
     * itself, so a single real orphan plus a wildly oversized request is
     * enough to prove the server-side cap actually engaged.
     */
    @Test @Order(30)
    void quarantineOrphans_oversizedSampleLimitRequest_clampsAndFlagsTruncation() throws Exception {
        String collection = "code__gcaudit-quarantine__minilm-l6-v2-384__v1";
        String quarantineCollection = "quarantine-code__gcaudit-quarantine__minilm-l6-v2-384__v1";
        String orphan = ch("gc-audit-quarantine-orphan");

        repo.upsertCollection(TENANT, Map.of(
            "name", collection, "content_type", "code", "owner_id", "gc-audit-quarantine-owner",
            "embedding_model", "minilm-l6-v2-384", "model_version", "v1"));

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            String zeroVec = "[" + "0,".repeat(383) + "0]";
            var ps = su.prepareStatement(
                "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384)"
                + " VALUES (?, ?, decode(?, 'hex'), ?, ?::vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
            ps.setString(1, TENANT);
            ps.setString(2, collection);
            ps.setString(3, orphan);
            ps.setString(4, "seed text " + orphan);
            ps.setString(5, zeroVec);
            ps.executeUpdate();
        }
        // No manifest row for `orphan` -- it is unreferenced, so quarantineOrphans moves it.

        assertThat(repo.listGcAudit(TENANT, collection, "gc_quarantine_orphans", 100, 0)).isEmpty();

        var outcome = vecRepo.quarantineOrphans(
            TENANT, collection, quarantineCollection, "2026-08-19T00:00:00Z", 999_999);
        assertThat(outcome.moved()).isEqualTo(1L);

        var rows = repo.listGcAudit(TENANT, collection, "gc_quarantine_orphans", 100, 0);
        assertThat(rows).hasSize(1);
        Map<String, Object> row = rows.get(0);
        @SuppressWarnings("unchecked")
        var details = (Map<String, Object>) row.get("details");
        assertThat(((Number) details.get("sample_limit")).intValue())
            .as("the EFFECTIVE (clamped) sample_limit is recorded, not the caller's raw oversized request")
            .isEqualTo(CatalogRepository.GC_AUDIT_MAX_CHASHES);
        assertThat(details.get("chashes_truncated"))
            .as("GC_AUDIT_MAX_CHASHES cap must fire server-side even when the caller requests "
                + "an unbounded sample_limit")
            .isEqualTo(true);
        assertThat(((Number) details.get("chashes_stored")).intValue())
            .isEqualTo(CatalogRepository.GC_AUDIT_MAX_CHASHES);
    }
}
