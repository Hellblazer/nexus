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
 * nexus-3ck2g E3/E4 — {@code POST /v1/catalog/purge-trash}, exercised at the
 * {@link CatalogRepository} layer ({@code purgeTrashPreview}/{@code purgeTrash}, the
 * exact methods {@code CatalogHandler#handlePurgeTrash} calls into). The handler's
 * own body-validation branching (the {@code older_than_days}/{@code dry_run}
 * present-but-wrong-typed-value 400s added by nexus-8j1zx's fix round) is covered
 * separately by {@code dev.nexus.service.http.CatalogHandlerPurgeTrashTest}.
 *
 * <p>{@code nexus.purge_trash(interval)} (catalog-003-soft-delete.xml:200-296) existed
 * with ZERO Java callers before this bead — the stranded-chunk sweep and the physical
 * tombstone GC it implements were both dead code.
 *
 * <p>Fixture (one collection, {@code chunks_384}):
 * <ul>
 *   <li>{@code DOC_LIVE} — never tombstoned.</li>
 *   <li>{@code DOC_FRESH_TOMB} — tombstoned via {@link CatalogRepository#deleteDocument}
 *       moments ago (real production path).</li>
 *   <li>{@code DOC_AGED_TOMB} — tombstoned via {@code deleteDocument}, then its {@code
 *       deleted_at} is backdated 60 days (superuser {@code UPDATE}) to simulate an old
 *       tombstone without waiting real time.</li>
 *   <li>One manifest-less orphan chunk (RDR-145 note-chunk shape,
 *       {@code SoftDeleteTest}:814-867's pin) — never stranded, never swept.</li>
 * </ul>
 *
 * <p><strong>Non-obvious contract this suite pins</strong>: {@code nexus.purge_trash}'s
 * Steps 1-3 (the {@code chunks_&lt;dim&gt;} stranded-row sweep) carry NO age filter at
 * all — only Step 4 (the {@code catalog_documents} physical delete) is gated by {@code
 * older_than}. A FRESHLY tombstoned document's chunk is therefore swept from {@code
 * chunks_384} on the very next purge call, even though the document row itself survives
 * (not yet aged past the threshold) until a later purge. {@code
 * fresh_tombstone_docSurvives_butItsChunkIsSweptImmediately} pins this explicitly so a
 * future reader does not mistake it for a bug.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogPurgeTrashTest {

    private static final String SVC_ROLE = "svc_purge_trash";
    private static final String SVC_PASS = "svc_purge_trash_pass";

    private static final String TENANT = "purge-trash";
    private static final String COLLECTION = "knowledge__purge-trash__minilm-l6-v2-384__v1";

    private static final String DOC_LIVE        = "purge-doc-live";
    private static final String DOC_FRESH_TOMB  = "purge-doc-fresh-tomb";
    private static final String DOC_AGED_TOMB   = "purge-doc-aged-tomb";

    private static final String CHASH_LIVE   = Chash.ofText("purge-chunk-live").toHex();
    private static final String CHASH_FRESH  = Chash.ofText("purge-chunk-fresh").toHex();
    private static final String CHASH_AGED   = Chash.ofText("purge-chunk-aged").toHex();
    private static final String CHASH_ORPHAN = Chash.ofText("purge-chunk-orphan").toHex();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository catalogRepo;
    PgVectorRepository vecRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml", new ClassLoaderResourceAccessor(), db);
            liquibase.update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.purge_trash(interval) TO " + SVC_ROLE);
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        catalogRepo = new CatalogRepository(tenantScope);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT + "', '"
                + COLLECTION + "') ON CONFLICT DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) VALUES "
                + "('" + TENANT + "', '" + DOC_LIVE + "', 'Live', '" + COLLECTION + "'), "
                + "('" + TENANT + "', '" + DOC_FRESH_TOMB + "', 'Fresh Tomb', '" + COLLECTION + "'), "
                + "('" + TENANT + "', '" + DOC_AGED_TOMB + "', 'Aged Tomb', '" + COLLECTION + "')");
            insertManifestRow(su, DOC_LIVE, CHASH_LIVE);
            insertManifestRow(su, DOC_FRESH_TOMB, CHASH_FRESH);
            insertManifestRow(su, DOC_AGED_TOMB, CHASH_AGED);
            // CHASH_ORPHAN: no manifest row (RDR-145 manifest-less note chunk).
        }

        vecRepo.upsertChunks(TENANT, COLLECTION,
            List.of(CHASH_LIVE, CHASH_FRESH, CHASH_AGED, CHASH_ORPHAN),
            List.of("live text", "fresh tomb text", "aged tomb text", "orphan text"),
            List.of(Map.of(), Map.of(), Map.of(), Map.of()));

        // Tombstone FRESH and AGED via the real production path.
        assertThat(catalogRepo.deleteDocument(TENANT, DOC_FRESH_TOMB)).isEqualTo(1);
        assertThat(catalogRepo.deleteDocument(TENANT, DOC_AGED_TOMB)).isEqualTo(1);

        // Backdate AGED's tombstone 60 days so it clears a 30-day older_than threshold;
        // FRESH's tombstone stays at "just now".
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = NOW() - interval '60 days' "
                + "WHERE tenant_id = '" + TENANT + "' AND tumbler = '" + DOC_AGED_TOMB + "'");
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void insertManifestRow(Connection su, String docId, String chashHex) throws Exception {
        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) "
                + "VALUES (?, ?, 0, decode(?, 'hex'))")) {
            ps.setString(1, TENANT);
            ps.setString(2, docId);
            ps.setString(3, chashHex);
            ps.execute();
        }
    }

    private long chunks384Count(String chashHex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ps = su.prepareStatement(
                "SELECT count(*) FROM nexus.chunks_384 WHERE tenant_id = ? AND chash = decode(?, 'hex')");
            ps.setString(1, TENANT);
            ps.setString(2, chashHex);
            var rs = ps.executeQuery();
            rs.next();
            return rs.getLong(1);
        }
    }

    private boolean documentExists(String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ps = su.prepareStatement(
                "SELECT 1 FROM nexus.catalog_documents WHERE tenant_id = ? AND tumbler = ?");
            ps.setString(1, TENANT);
            ps.setString(2, tumbler);
            return ps.executeQuery().next();
        }
    }

    // ── dry_run preview: no mutation ────────────────────────────────────────────

    @Test @Order(10)
    void dryRunPreview_countsStrandedChunksAndAgedTombstones_withoutMutating() throws Exception {
        Map<String, Object> result = catalogRepo.purgeTrashPreview(TENANT, 30);

        assertThat(result.get("dry_run")).isEqualTo(true);
        // Both FRESH's and AGED's chunks are stranded (purge_trash's chunk sweep carries
        // no age filter — see class javadoc) — 2, not 1.
        assertThat(((Number) result.get("chunks_384_stranded")).longValue())
            .as("stranded chunk count is age-INDEPENDENT: both tombstoned docs' chunks count")
            .isEqualTo(2L);
        assertThat(((Number) result.get("chunks_768_stranded")).longValue()).isEqualTo(0L);
        assertThat(((Number) result.get("chunks_1024_stranded")).longValue()).isEqualTo(0L);
        // Only AGED clears the 30-day threshold.
        assertThat(((Number) result.get("documents_purged")).longValue())
            .as("preview 'documents_purged' = would-purge count under a 30-day threshold")
            .isEqualTo(1L);

        // No mutation: everything from setup must still be exactly as it was.
        assertThat(documentExists(DOC_LIVE)).isTrue();
        assertThat(documentExists(DOC_FRESH_TOMB)).isTrue();
        assertThat(documentExists(DOC_AGED_TOMB)).isTrue();
        assertThat(chunks384Count(CHASH_LIVE)).isEqualTo(1L);
        assertThat(chunks384Count(CHASH_FRESH)).isEqualTo(1L);
        assertThat(chunks384Count(CHASH_AGED)).isEqualTo(1L);
        assertThat(chunks384Count(CHASH_ORPHAN))
            .as("manifest-less orphan chunk is never counted as stranded, dry-run or not")
            .isEqualTo(1L);
    }

    @Test @Order(15)
    void dryRunPreview_withSmallerOlderThan_stillDoesNotMutate() throws Exception {
        // older_than_days=1 clears BOTH tombstones (fresh is still < 1 day old in wall-clock
        // terms, but the point here is only that dry_run never mutates regardless of the
        // threshold chosen).
        Map<String, Object> result = catalogRepo.purgeTrashPreview(TENANT, 1);
        assertThat(result.get("dry_run")).isEqualTo(true);
        assertThat(documentExists(DOC_LIVE)).isTrue();
        assertThat(documentExists(DOC_FRESH_TOMB)).isTrue();
        assertThat(documentExists(DOC_AGED_TOMB)).isTrue();
    }

    // ── execute: actually purges ────────────────────────────────────────────────

    @Test @Order(20)
    void execute_purgesAgedTombstoneAndItsChunk_sweepsFreshTombstonesChunkToo_liveAndOrphanSurvive() throws Exception {
        Map<String, Object> result = catalogRepo.purgeTrash(TENANT, 30);

        assertThat(result.get("dry_run")).isEqualTo(false);
        assertThat(((Number) result.get("documents_purged")).longValue())
            .as("nexus.purge_trash's own return value: exactly the aged doc")
            .isEqualTo(1L);

        // AGED: fully gone — catalog_documents row hard-deleted, chunk swept.
        assertThat(documentExists(DOC_AGED_TOMB)).as("aged tombstone physically purged").isFalse();
        assertThat(chunks384Count(CHASH_AGED)).as("aged doc's chunk swept").isEqualTo(0L);

        // LIVE and its chunk: untouched.
        assertThat(documentExists(DOC_LIVE)).isTrue();
        assertThat(chunks384Count(CHASH_LIVE)).isEqualTo(1L);

        // Manifest-less orphan chunk: RDR-145 pin — never swept regardless.
        assertThat(chunks384Count(CHASH_ORPHAN)).isEqualTo(1L);
    }

    @Test @Order(21)
    void fresh_tombstone_docSurvives_butItsChunkIsSweptImmediately() throws Exception {
        // Pinning the non-obvious contract from the class javadoc: FRESH's document row
        // survives (not yet aged past 30 days) but its chunk was ALREADY swept by the
        // Order(20) execute call above — the chunk-sweep predicate (purge_trash Steps 1-3)
        // carries no age filter at all, only the document DELETE (Step 4) does.
        assertThat(documentExists(DOC_FRESH_TOMB))
            .as("fresh tombstone's document row must survive — not yet aged past older_than")
            .isTrue();
        assertThat(chunks384Count(CHASH_FRESH))
            .as("fresh tombstone's chunk is swept on the very next purge call regardless of "
                + "the document's own age — this is nexus.purge_trash's existing behavior, "
                + "not something nexus-3ck2g introduces")
            .isEqualTo(0L);
    }
}
