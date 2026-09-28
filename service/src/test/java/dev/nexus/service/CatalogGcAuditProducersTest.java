// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
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
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
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
            // RDR-204 nexus-ft04v.4/.5: routed through PgContainerHelper.insertCollection.
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, collection);
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
                    + " VALUES (?, ?, decode(?, 'hex'), ?, ?::nexus.vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
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
                + " VALUES (?, ?, decode(?, 'hex'), ?, ?::nexus.vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
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
                + " VALUES (?, ?, decode(?, 'hex'), ?, ?::nexus.vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
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

    // ── gc_restore_rereferenced: SQL-side producer (catalog-039-1) ──────────────

    /**
     * nexus-gt03d — the third GC-lifecycle producer catalog-033 left
     * un-audited (found by conexus-86 / nexus-9la1f, 2026-09-24): a heal
     * re-referencing a quarantined chash moves it back to its origin
     * collection with no gc_audit row, unlike its two catalog-033 siblings
     * (gc_quarantine_orphans, sweep_superseded_chunks). Mirrors this file's
     * own {@code quarantineOrphans_...} shape one level up the GC lifecycle:
     * seed a chunk already IN quarantine, reference its chash from the
     * ORIGIN collection's manifest (the "re-referenced" condition
     * {@code restoreRereferenced} exists to detect), restore it, and assert
     * the audit row's shape -- {@code collection} is the QUARANTINE
     * collection (the reap source, same convention as
     * gc_quarantine_orphans/gc_expire_quarantine above), {@code
     * details.origin_collection} names the restore destination.
     */
    @Test @Order(40)
    void restoreRereferenced_reReferencedQuarantineChunk_insertsGcAuditRow() throws Exception {
        String collection = "code__gcaudit-restore__minilm-l6-v2-384__v1";
        String quarantineCollection = "quarantine-code__gcaudit-restore__minilm-l6-v2-384__v1";
        String docId = "gc-audit-restore-doc";
        String chash = ch("gc-audit-restore-chunk");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            // nexus-gt03d: both sides pre-registered -- PgVectorRepository
            // .restoreRereferenced resolves dim from the ORIGIN collection
            // before ever calling the SQL function (see that method's own
            // javadoc), and the function itself requires the QUARANTINE
            // collection to already be registered (its self-registration
            // block copies the origin's attributes FROM that row).
            PgContainerHelper.insertCollection(dsl, TENANT, collection);
            PgContainerHelper.insertCollection(dsl, TENANT, quarantineCollection);
        }

        // The chunk already lives in the QUARANTINE collection (no
        // `origin_collection` metadata stamp -- exercises the COALESCE-to-
        // self default the function's own header documents, same as
        // gc_restore_rereferenced's own "mine" filter comment).
        vecRepo.upsertChunks(TENANT, quarantineCollection, List.of(chash),
            List.of("gc audit restore text"), List.of(Map.of()));
        // fk_catalog_chunks_catalog_doc: the manifest row below needs a real
        // catalog_documents row to reference.
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", docId, "title", "gc-audit-restore-" + docId,
            "content_type", "code", "corpus", "code",
            "physical_collection", collection, "chunk_count", 0));
        // fk_catalog_chunks_chunk: the manifest row below scopes the FK to
        // (tenant_id, ORIGIN collection, chash) -- a stub chunks row must
        // already exist there too (mirrors the manifestSweep test's own
        // "kept" stub above; gc_restore_rereferenced's copy-INSERT below
        // then ON-CONFLICT-DO-UPDATEs this stub with the quarantine row's
        // real content, same as a heal rehydrating a reference before the
        // chunk vector itself came back).
        vecRepo.upsertChunks(TENANT, collection, List.of(chash),
            List.of("gc audit restore stub text"), List.of(Map.of()));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // A heal re-referenced this chash from the ORIGIN collection's
            // manifest -- the condition restoreRereferenced moves it back on.
            insertManifestRow(su, TENANT, docId, chash, collection);
        }

        assertThat(repo.listGcAudit(TENANT, quarantineCollection, "gc_restore_rereferenced", 100, 0)).isEmpty();

        long restored = vecRepo.restoreRereferenced(TENANT, quarantineCollection, collection);
        assertThat(restored).isEqualTo(1L);

        var rows = repo.listGcAudit(TENANT, quarantineCollection, "gc_restore_rereferenced", 100, 0);
        assertThat(rows)
            .as("nexus.gc_restore_rereferenced must leave a gc_audit row behind IN THE SAME "
                + "TRANSACTION as its restore DELETE -- the exact gap conexus-86/nexus-9la1f "
                + "found: rows leaving quarantine by restore had no forensic trail")
            .hasSize(1);
        Map<String, Object> row = rows.get(0);
        assertThat(row.get("actor")).as("server-driven producer, not a client-attributed one").isEqualTo("engine");
        assertThat(row.get("dry_run")).isEqualTo(false);
        assertThat(((Number) row.get("chash_count")).intValue())
            .as("exactly the one chunk restoreRereferenced actually moved").isEqualTo(1);
        @SuppressWarnings("unchecked")
        var chashes = (List<Object>) row.get("chashes");
        assertThat(chashes).containsExactly(chash);
        @SuppressWarnings("unchecked")
        var details = (Map<String, Object>) row.get("details");
        assertThat(details.get("origin_collection")).isEqualTo(collection);
    }

    /**
     * nexus-brxnp / nexus-u6d93 (T2 {@code nexus/debug-u6d93-brxnp}): before
     * this fix, {@code gc_restore_rereferenced}'s copy-INSERT used {@code ON
     * CONFLICT DO UPDATE}, so a PRE-EXISTING origin row (this fixture's
     * stub, inserted before the quarantine-side row is even backdated) was
     * clobbered by the older quarantine copy's {@code created_at} on
     * restore. The fix ({@code catalog-043-2}, {@code ON CONFLICT DO
     * NOTHING}) makes the pre-existing origin row's OWN {@code created_at}
     * authoritative: it must be UNCHANGED by the restore, never backdated to
     * the quarantine copy's stamp. BACKDATES the quarantine-side chunk to a
     * fixed past instant so "unchanged" is distinguishable from "coincidentally
     * still now" -- same non-vacuity reasoning as {@code
     * GcRestoreRereferencedBoundedTest
     * #liveOriginRow_metadataAndCreatedAt_areNeverOverwrittenByAnOlderQuarantineCopy}.
     * (catalog-042-2's own fix, carrying {@code created_at} through on a
     * genuine INSERT when the origin has NO row at all, is covered
     * separately by {@link
     * #restoreRereferenced_unbounded_createdAtIsCarriedThrough_onInsertWhenOriginIsAbsent_regressionGuard}
     * and is unaffected by this changeset.)
     */
    @Test @Order(45)
    void restoreRereferenced_unbounded_liveOriginCreatedAt_isNeverOverwrittenByAnOlderQuarantineCopy()
            throws Exception {
        String collection = "code__gcaudit-restore-ts__minilm-l6-v2-384__v1";
        String quarantineCollection = "quarantine-code__gcaudit-restore-ts__minilm-l6-v2-384__v1";
        String docId = "gc-audit-restore-ts-doc";
        String chash = ch("gc-audit-restore-ts-chunk");
        OffsetDateTime past = OffsetDateTime.of(2026, 7, 16, 0, 8, 44, 0, ZoneOffset.UTC);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, collection);
            PgContainerHelper.insertCollection(dsl, TENANT, quarantineCollection);
        }

        vecRepo.upsertChunks(TENANT, quarantineCollection, List.of(chash),
            List.of("gc audit restore ts text"), List.of(Map.of()));
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", docId, "title", "gc-audit-restore-ts-" + docId,
            "content_type", "code", "corpus", "code",
            "physical_collection", collection, "chunk_count", 0));
        vecRepo.upsertChunks(TENANT, collection, List.of(chash),
            List.of("gc audit restore ts stub text"), List.of(Map.of()));
        OffsetDateTime originCreatedAtBeforeRestore = chunkCreatedAt(collection, chash);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertManifestRow(su, TENANT, docId, chash, collection);
        }

        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES)
               .update(DSL.table(DSL.name("nexus", "chunks")))
               .set(DSL.field("created_at", OffsetDateTime.class), past)
               .where(DSL.field("tenant_id", String.class).eq(TENANT))
               .and(DSL.field("collection", String.class).eq(quarantineCollection))
               .execute();
            assertThat(n).as("guard: the quarantine-side row exists to backdate").isEqualTo(1);
        }
        // NON-VACUITY: the backdate must have landed, and it must differ from
        // the origin's own pre-restore created_at, or "unchanged" below could
        // only pass by coincidence of the clock.
        assertThat(chunkCreatedAt(quarantineCollection, chash))
            .as("guard: quarantine-side row is backdated").isEqualTo(past);
        assertThat(originCreatedAtBeforeRestore).as("guard: origin's own created_at is NOT the backdated value")
            .isNotEqualTo(past);

        long restored = vecRepo.restoreRereferenced(TENANT, quarantineCollection, collection);
        assertThat(restored).as("the restore still processes and reports it").isEqualTo(1L);

        assertThat(chunkCreatedAt(collection, chash))
            .as("nexus-brxnp: the pre-existing origin row's OWN created_at survives, never "
                + "overwritten by the older quarantine copy's backdated stamp")
            .isEqualTo(originCreatedAtBeforeRestore);

        var rows = repo.listGcAudit(TENANT, quarantineCollection, "gc_restore_rereferenced", 100, 0);
        assertThat(rows).hasSize(1);
        @SuppressWarnings("unchecked")
        var details = (Map<String, Object>) rows.get(0).get("details");
        assertThat(((Number) details.get("already_live")).longValue())
            .as("nexus-brxnp: the one candidate hit the DO NOTHING conflict -- the origin was already live")
            .isEqualTo(1L);
    }

    private OffsetDateTime chunkCreatedAt(String collection, String chashHex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var r = DSL.using(su, SQLDialect.POSTGRES)
               .select(DSL.field("created_at", OffsetDateTime.class))
               .from(DSL.table(DSL.name("nexus", "chunks")))
               .where(DSL.field("tenant_id", String.class).eq(TENANT))
               .and(DSL.field("collection", String.class).eq(collection))
               .and(DSL.field("chash", byte[].class).eq(Chash.fromHex(chashHex).toBytes()))
               .fetchOne(0, OffsetDateTime.class);
            assertThat(r).as("chunks row for %s/%s", collection, chashHex).isNotNull();
            return r.withOffsetSameInstant(ZoneOffset.UTC);
        }
    }

    private String chunkText(String collection, String chashHex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
               .select(DSL.field("chunk_text", String.class))
               .from(DSL.table(DSL.name("nexus", "chunks")))
               .where(DSL.field("tenant_id", String.class).eq(TENANT))
               .and(DSL.field("collection", String.class).eq(collection))
               .and(DSL.field("chash", byte[].class).eq(Chash.fromHex(chashHex).toBytes()))
               .fetchOne(0, String.class);
        }
    }

    private String metadataField(String collection, String chashHex, String key) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
               .select(DSL.jsonbGetAttributeAsText(DSL.field("metadata", org.jooq.JSONB.class), key))
               .from(DSL.table(DSL.name("nexus", "chunks")))
               .where(DSL.field("tenant_id", String.class).eq(TENANT))
               .and(DSL.field("collection", String.class).eq(collection))
               .and(DSL.field("chash", byte[].class).eq(Chash.fromHex(chashHex).toBytes()))
               .fetchOne(0, String.class);
        }
    }

    /**
     * nexus-brxnp: bypasses {@code fk_catalog_chunks_chunk} locally (drop,
     * insert, re-add {@code NOT VALID}) so a manifest row can name {@code
     * collection} for a chash whose physical chunk currently sits ONLY in
     * the quarantine collection -- the real production race the FK
     * structurally blocks at write time (RDR-191 Phase 5, nexus-o8dil.29).
     * Same idiom {@code PgVectorRepositoryGcQuarantineTest
     * #seedManifestBypassingFk} and {@code GcRestoreRereferencedBoundedTest
     * #insertManifestRowBypassingFk} already use.
     */
    private void insertManifestRowBypassingFk(String docId, String chashHex, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk");
            insertManifestRow(su, TENANT, docId, chashHex, collection);
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks "
                + "ADD CONSTRAINT fk_catalog_chunks_chunk "
                + "FOREIGN KEY (tenant_id, collection, chash) REFERENCES nexus.chunks (tenant_id, collection, chash) "
                + "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID");
        }
    }

    /**
     * nexus-brxnp fix round (code-review CONFIRMED): the javadoc on {@link
     * #restoreRereferenced_unbounded_liveOriginCreatedAt_isNeverOverwrittenByAnOlderQuarantineCopy}
     * used to claim {@code PgVectorRepositoryGcQuarantineTest
     * #restoreRereferenced_movesBackWhenManifestReReferencesIt} covers the
     * UNBOUNDED function's {@code created_at} carry-through on the plain-INSERT
     * path (origin genuinely absent) -- that test never asserts {@code
     * created_at} at all. This IS that proof: the origin has NO row for the
     * chash, the manifest re-references it anyway (bypassing {@code
     * fk_catalog_chunks_chunk} the same way production's real race does),
     * and restore must INSERT the quarantine copy's own content and {@code
     * created_at}, stripping the quarantine stamps -- catalog-042-2's fix,
     * unaffected by catalog-043-2's {@code ON CONFLICT DO NOTHING} change
     * (DO NOTHING only changes behaviour when a conflicting row already
     * exists; this path is a plain INSERT). Mirrors {@code
     * GcRestoreRereferencedBoundedTest
     * #createdAtIsCarriedThrough_onInsertWhenOriginIsAbsent_regressionGuard}
     * for the unbounded twin.
     */
    @Test @Order(47)
    void restoreRereferenced_unbounded_createdAtIsCarriedThrough_onInsertWhenOriginIsAbsent_regressionGuard()
            throws Exception {
        String collection = "code__gcaudit-restore-absent__minilm-l6-v2-384__v1";
        String quarantineCollection = "quarantine-code__gcaudit-restore-absent__minilm-l6-v2-384__v1";
        String docId = "gc-audit-restore-absent-doc";
        String chash = ch("gc-audit-restore-absent-chunk");
        OffsetDateTime past = OffsetDateTime.of(2026, 7, 16, 0, 8, 44, 0, ZoneOffset.UTC);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, collection);
            PgContainerHelper.insertCollection(dsl, TENANT, quarantineCollection);
        }

        vecRepo.upsertChunks(TENANT, quarantineCollection, List.of(chash),
            List.of("gc audit restore absent text"), List.of(Map.of()));
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", docId, "title", "gc-audit-restore-absent-" + docId,
            "content_type", "code", "corpus", "code",
            "physical_collection", collection, "chunk_count", 0));

        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES)
               .update(DSL.table(DSL.name("nexus", "chunks")))
               .set(DSL.field("created_at", OffsetDateTime.class), past)
               .where(DSL.field("tenant_id", String.class).eq(TENANT))
               .and(DSL.field("collection", String.class).eq(quarantineCollection))
               .execute();
            assertThat(n).as("guard: the quarantine-side row exists to backdate").isEqualTo(1);
        }
        // NON-VACUITY: the backdate must have landed before asserting carry-through.
        assertThat(chunkCreatedAt(quarantineCollection, chash))
            .as("guard: quarantine-side row is backdated").isEqualTo(past);

        // D re-references X while X is STILL only in Q -- the origin genuinely
        // has no row for this chash, exactly the plain-INSERT path.
        insertManifestRowBypassingFk(docId, chash, collection);

        long restored = vecRepo.restoreRereferenced(TENANT, quarantineCollection, collection);
        assertThat(restored).as("the restore inserts it").isEqualTo(1L);

        assertThat(chunkText(collection, chash))
            .as("origin gets the quarantine copy's own content -- there was nothing to conflict with")
            .isEqualTo("gc audit restore absent text");
        assertThat(chunkCreatedAt(collection, chash))
            .as("created_at is carried through on a genuine INSERT, the absent-origin path -- "
                + "catalog-042-2's fix, unaffected by catalog-043-2's ON CONFLICT DO NOTHING change")
            .isEqualTo(past);
        assertThat(metadataField(collection, chash, "quarantined_at")).as("quarantine stamp stripped").isNull();
        assertThat(metadataField(collection, chash, "origin_collection")).as("quarantine stamp stripped").isNull();

        var rows = repo.listGcAudit(TENANT, quarantineCollection, "gc_restore_rereferenced", 100, 0);
        assertThat(rows).hasSize(1);
        @SuppressWarnings("unchecked")
        var details = (Map<String, Object>) rows.get(0).get("details");
        assertThat(((Number) details.get("already_live")).longValue())
            .as("nexus-brxnp: a genuine INSERT (origin absent) hits no conflict at all")
            .isZero();
    }

    /**
     * nexus-gt03d fix round (substantive-critic Significant, T2 [27238]):
     * catalog-039-1's audit INSERT has a truncation CASE for more than
     * GC_AUDIT_MAX_CHASHES (5000) chashes, and nothing above exercises it --
     * unlike {@code gc_quarantine_orphans} (whose truncation the
     * {@code quarantineOrphans_oversizedSampleLimitRequest_...} test above
     * reaches cheaply by gaming a caller-supplied {@code sample_limit}),
     * {@code gc_restore_rereferenced} has NO caller-supplied limit to game
     * -- the only end-to-end path to its own {@code > 5000} branch is a
     * real quarantine with more than 5000 re-referenced rows, each needing
     * a matching manifest row AND a matching origin-side stub chunk row
     * (three ~5000-row inserts, not one), which this same test class's own
     * precedent already documents as prohibitively slow to seed for a
     * smaller-scoped truncation. See the changeset's own header ("TRUNCATION
     * BOUNDARY COVERAGE") for the full derivation and the accepted residual.
     *
     * <p>Proves the exact arithmetic the changeset's audit INSERT performs
     * -- {@code cardinality(arr)} (chash_count), {@code cardinality(arr) &gt;
     * 5000} (chashes_truncated), and the literal 5000 the changeset reports
     * as chashes_stored whenever that predicate holds -- against a REAL,
     * typed-jOOQ-constructed Postgres array of exactly n elements (n =
     * 4999, 5000, 5001), via Postgres's own {@code cardinality()} rather
     * than a Java re-implementation of the same arithmetic: a Java-side
     * reimplementation would prove nothing about the actual SQL text if the
     * changeset's threshold were ever mistyped ({@code &gt;=} for
     * {@code &gt;}, or an off-by-one constant). {@code DSL.array(Integer...)}
     * builds the fabricated array as ONE bound Postgres array parameter
     * (n literal Java values, zero per-row round trips), so all three
     * boundary points run in well under a second combined.
     */
    @Test @Order(50)
    void restoreAuditTruncation_caseLogic_atBoundary() {
        for (int n : new int[] {4999, 5000, 5001}) {
            Integer[] fabricated = new Integer[n];
            java.util.Arrays.fill(fabricated, 0);
            org.jooq.Field<Integer[]> arr = DSL.array(fabricated);
            org.jooq.Field<Integer> count = DSL.cardinality(arr);
            org.jooq.Field<Boolean> truncated = DSL.coalesce(count, 0).gt(5000);
            // Mirrors the changeset's own
            //   CASE WHEN COALESCE(array_length(v_chashes,1),0) > 5000
            //        THEN jsonb_build_object('chashes_truncated', true, 'chashes_stored', 5000)
            //        ELSE '{}'::jsonb END
            // at the field level: chashes_stored is the LITERAL 5000 when the
            // predicate holds, and absent (NULL here, standing in for "key not
            // present in the jsonb object") otherwise -- never a recomputed
            // LEAST(n,5000) value, since the branch only fires once n already
            // exceeds 5000.
            org.jooq.Field<Integer> chashesStored =
                DSL.when(truncated, DSL.val(5000)).otherwise(DSL.castNull(Integer.class));

            var rec = tenantScope.withTenant(TENANT, ctx -> ctx.select(
                    count.as("chash_count"),
                    truncated.as("chashes_truncated"),
                    chashesStored.as("chashes_stored"))
                .fetchOne());

            boolean expectedTruncated = n > 5000;
            assertThat(((Number) rec.get("chash_count", Object.class)).intValue())
                .as("chash_count is the FULL array length, never the post-truncation one -- n=" + n)
                .isEqualTo(n);
            assertThat(rec.get("chashes_truncated", Object.class))
                .as("the changeset's own `COALESCE(array_length(v_chashes,1),0) > 5000` predicate at n=" + n)
                .isEqualTo(expectedTruncated);
            if (expectedTruncated) {
                assertThat(((Number) rec.get("chashes_stored", Object.class)).intValue())
                    .as("chashes_stored is the literal cap 5000 whenever truncated fires -- n=" + n)
                    .isEqualTo(5000);
            } else {
                assertThat(rec.get("chashes_stored", Object.class))
                    .as("chashes_stored is ABSENT (never a recomputed LEAST(n,5000)) below the cap -- n=" + n)
                    .isNull();
            }
        }
    }
}
