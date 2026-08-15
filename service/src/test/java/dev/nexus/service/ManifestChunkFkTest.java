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
import java.sql.ResultSet;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-191 Phase 5 (bead nexus-o8dil.29) — {@code fk_catalog_chunks_chunk}: the
 * manifest FK from {@code nexus.catalog_document_chunks (tenant_id, collection,
 * chash)} to {@code nexus.chunks (tenant_id, collection, chash)}.
 *
 * <p>Covers the FK's live shape (exists, validated, deferrable, ON UPDATE CASCADE,
 * NOT a plain ON DELETE RESTRICT — F10/F10a/F10b/F8a), the accept/reject boundary
 * for manifest writes, and the two class-B deferred-constraint call sites
 * ({@code nexus.purge_trash} and {@code CatalogRepository.deleteCollectionTxn})
 * that delete FROM {@code nexus.chunks} (the FK's parent) while the manifest rows
 * that reference those chunks still exist, inside the SAME transaction.
 *
 * <p>The late-upgrading-deployment boot test (a pre-existing dangling population
 * surviving the full ADD-NOT-VALID -> remediate -> VALIDATE walk) lives in {@code
 * SchemaMigratorIntegrationTest} — it needs a dedicated, partially-migrated
 * container (only this class's sibling can seed data BEFORE the FK exists), which
 * this class's {@code @BeforeAll} (a single, fully-migrated container) cannot do.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ManifestChunkFkTest {

    private static final String FK_NAME = "fk_catalog_chunks_chunk";
    private static final String SVC_ROLE = "svc_manifest_fk_test";
    private static final String SVC_PASS = "svc_manifest_fk_test_pass";
    private static final String TENANT = "manifest-fk-tenant";
    private static final String COLLECTION = "knowledge__manifest-fk__minilm-l6-v2-384__v1";

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
            var lb = new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su)));
            lb.update(new Contexts());
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
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 1 — constraint shape (F10/F10a/F10b/F8a)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    @Order(10)
    void fk_existsValidatedDeferrableOnUpdateCascade_notPlainOnDeleteRestrict() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT convalidated, condeferrable, condeferred, confupdtype, confdeltype "
                + "FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
                + "WHERE c.contype = 'f' AND c.conname = '" + FK_NAME + "' AND n.nspname = 'nexus'");
            assertThat(rs.next()).as(FK_NAME + " must exist in pg_constraint").isTrue();
            assertThat(rs.getBoolean("convalidated"))
                .as(FK_NAME + " must be VALIDATED (catalog-029-2 ran)").isTrue();
            assertThat(rs.getBoolean("condeferrable"))
                .as(FK_NAME + " must be DEFERRABLE (F8a/F10b — plain RESTRICT cannot be)").isTrue();
            assertThat(rs.getBoolean("condeferred"))
                .as(FK_NAME + " must be INITIALLY IMMEDIATE, not INITIALLY DEFERRED by default (F10b: "
                    + "statement-local error locality outside the two class-B sites)").isFalse();
            assertThat(rs.getString("confupdtype"))
                .as("ON UPDATE must be CASCADE ('c') — mandatory per F10a").isEqualTo("c");
            assertThat(rs.getString("confdeltype"))
                .as("ON DELETE must be NO ACTION ('a'), never RESTRICT ('r') — RESTRICT can never be "
                    + "deferred in PostgreSQL (F8a), so a deferrable FK cannot carry it").isEqualTo("a");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 2 — accept / reject boundary
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    @Order(20)
    void manifestWrite_chashWithNoMatchingChunk_isRejected() throws Exception {
        String doc = "fk-reject-doc";
        String chash = Chash.ofText("fk-reject-no-chunk").toHex();
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", doc, "title", "FK reject doc", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", COLLECTION));
        // Deliberately NO nexus.chunks row for this chash.
        assertThatThrownBy(() -> catalogRepo.writeManifest(TENANT, doc, COLLECTION,
                List.of(Map.of("position", 0, "chash", chash))))
            .as("a manifest row naming a chash with no nexus.chunks row must be refused by " + FK_NAME)
            .hasMessageContaining(FK_NAME);
    }

    @Test
    @Order(21)
    void manifestWrite_chashWithMatchingChunk_succeeds() throws Exception {
        String doc = "fk-accept-doc";
        String chash = Chash.ofText("fk-accept-with-chunk").toHex();
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", doc, "title", "FK accept doc", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", COLLECTION));
        vecRepo.upsertChunks(TENANT, COLLECTION, List.of(chash), List.of("accept text"), List.of(Map.of()));
        assertThatCode(() -> catalogRepo.writeManifest(TENANT, doc, COLLECTION,
                List.of(Map.of("position", 0, "chash", chash))))
            .as("a manifest row whose chash IS present in nexus.chunks must be accepted")
            .doesNotThrowAnyException();
        assertThat(catalogRepo.getManifest(TENANT, doc)).hasSize(1);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 3 — ON UPDATE CASCADE (F10a): a parent-key UPDATE on nexus.chunks
    // propagates to the manifest instead of being rejected.
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    @Order(30)
    void chunkCollectionRename_cascadesToManifestCollection() throws Exception {
        String doc = "fk-cascade-doc";
        String chash = Chash.ofText("fk-cascade-chunk").toHex();
        String oldCollection = "knowledge__fk-cascade-old__minilm-l6-v2-384__v1";
        String newCollection = "knowledge__fk-cascade-new__minilm-l6-v2-384__v1";
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", doc, "title", "FK cascade doc", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", oldCollection));
        vecRepo.upsertChunks(TENANT, oldCollection, List.of(chash), List.of("cascade text"), List.of(Map.of()));
        catalogRepo.writeManifest(TENANT, doc, oldCollection,
            List.of(Map.of("position", 0, "chash", chash)));

        // Parent-key UPDATE: rename the chunk's collection (the (tenant_id,
        // collection, chash) triple's middle component, part of the FK's key).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            int updated = su.createStatement().executeUpdate(
                "UPDATE nexus.chunks SET collection = '" + newCollection + "' "
                + "WHERE tenant_id = '" + TENANT + "' AND collection = '" + oldCollection + "' "
                + "AND chash = decode('" + chash + "', 'hex')");
            assertThat(updated).as("the chunk row must exist to rename").isEqualTo(1);

            ResultSet rs = su.createStatement().executeQuery(
                "SELECT collection FROM nexus.catalog_document_chunks "
                + "WHERE tenant_id = '" + TENANT + "' AND doc_id = '" + doc + "' AND position = 0");
            assertThat(rs.next()).as("manifest row must still exist after cascade").isTrue();
            assertThat(rs.getString("collection"))
                .as("ON UPDATE CASCADE must propagate the chunk's collection rename to the manifest row "
                    + "(F10a: chunks is the FK's parent)")
                .isEqualTo(newCollection);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 4 — class-B deferred-constraint coverage: chunk-before-manifest
    // ordering, same transaction, must survive with the deferrable FK active
    // (not just "ordering characterized" — the txn must actually complete).
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    @Order(40)
    void purgeTrash_deletesChunkBeforeManifestInSameCall_survivesUnderDeferredFk() throws Exception {
        // nexus.purge_trash Step 1 deletes FROM nexus.chunks (the FK parent) while
        // the referencing catalog_document_chunks row STILL EXISTS -- it is only
        // removed later, in Step 4 of the SAME function call, via fk-001's CASCADE
        // off catalog_documents. Without catalog-029-3's SET CONSTRAINTS ...
        // DEFERRED fix, Step 1's DELETE would raise fk_catalog_chunks_chunk
        // immediately (F8a/F10b, site L1-L3).
        String collection = "knowledge__fk-purge__minilm-l6-v2-384__v1";
        String doc = "fk-purge-aged-tomb";
        String chash = Chash.ofText("fk-purge-chunk").toHex();

        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", doc, "title", "FK purge doc", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", collection));
        vecRepo.upsertChunks(TENANT, collection, List.of(chash), List.of("purge text"), List.of(Map.of()));
        catalogRepo.writeManifest(TENANT, doc, collection,
            List.of(Map.of("position", 0, "chash", chash)));

        // Tombstone, then backdate past a 30-day older_than threshold (same
        // technique as CatalogPurgeTrashTest).
        assertThat(catalogRepo.deleteDocument(TENANT, doc)).isEqualTo(1);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = NOW() - interval '60 days' "
                + "WHERE tenant_id = '" + TENANT + "' AND tumbler = '" + doc + "'");
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertThatCode(() -> {
                var ps = su.prepareStatement("SELECT set_config('nexus.tenant', ?, false)");
                ps.setString(1, TENANT);
                ps.executeQuery();
                su.createStatement().execute("SELECT nexus.purge_trash(interval '30 days')");
            })
                .as("purge_trash's chunk-before-manifest deletion must survive under the deferred FK "
                    + "(catalog-029-3's SET CONSTRAINTS fix) -- must NOT raise " + FK_NAME)
                .doesNotThrowAnyException();

            ResultSet chunkRs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.chunks WHERE tenant_id = '" + TENANT
                + "' AND collection = '" + collection + "' AND chash = decode('" + chash + "', 'hex')");
            chunkRs.next();
            assertThat(chunkRs.getInt(1)).as("the aged tombstoned chunk must be swept").isEqualTo(0);

            ResultSet manifestRs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.catalog_document_chunks "
                + "WHERE tenant_id = '" + TENANT + "' AND doc_id = '" + doc + "'");
            manifestRs.next();
            assertThat(manifestRs.getInt(1))
                .as("the manifest row must be gone too (fk-001 CASCADE off the deleted document)")
                .isEqualTo(0);
        }
    }

    @Test
    @Order(41)
    void deleteCollectionTxn_deletesChunkBeforeManifestInSameTxn_survivesUnderDeferredFk() throws Exception {
        // CatalogRepository.deleteCollectionTxn step 1 deletes FROM nexus.chunks
        // (the FK parent) BEFORE step 1b deletes catalog_document_chunks, in the
        // SAME transaction (tenantScope.withTenant). Without the companion
        // SET CONSTRAINTS fk_catalog_chunks_chunk DEFERRED fix added to
        // deleteCollectionTxn (nexus-o8dil.29), step 1 would raise immediately.
        String collection = "knowledge__fk-delcoll__minilm-l6-v2-384__v1";
        String doc = "fk-delcoll-doc";
        String chash = Chash.ofText("fk-delcoll-chunk").toHex();

        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", doc, "title", "FK delete-collection doc", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", collection));
        vecRepo.upsertChunks(TENANT, collection, List.of(chash), List.of("delcoll text"), List.of(Map.of()));
        catalogRepo.writeManifest(TENANT, doc, collection,
            List.of(Map.of("position", 0, "chash", chash)));

        assertThatCode(() -> catalogRepo.deleteCollection(TENANT, collection))
            .as("deleteCollectionTxn's chunk-before-manifest deletion must survive under the deferred "
                + "FK (the Java-side SET CONSTRAINTS fix) -- must NOT raise " + FK_NAME)
            .doesNotThrowAnyException();

        try (Connection su = pg.createConnection("")) {
            ResultSet chunkRs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.chunks WHERE tenant_id = '" + TENANT
                + "' AND collection = '" + collection + "' AND chash = decode('" + chash + "', 'hex')");
            chunkRs.next();
            assertThat(chunkRs.getInt(1)).as("the collection's chunk must be gone").isEqualTo(0);

            ResultSet manifestRs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.catalog_document_chunks "
                + "WHERE tenant_id = '" + TENANT + "' AND doc_id = '" + doc + "'");
            manifestRs.next();
            assertThat(manifestRs.getInt(1)).as("the manifest row must be gone too").isEqualTo(0);
        }
    }
}
