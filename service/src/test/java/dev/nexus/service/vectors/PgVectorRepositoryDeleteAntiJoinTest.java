// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-191 P2 (bead nexus-o8dil.5): TDD coverage for finding F10c — {@link
 * PgVectorRepository#delete} deletes chunks by id set with no manifest leg
 * at all, so it currently destroys a chunk another document's manifest
 * still references. By RDR-108 design, identical chunk text in a collection
 * collapses to ONE row shared by every document that contains it — so
 * "delete document A's chunks" (A's manifest chash set) can and does destroy
 * a chunk document B's manifest still points at, silently, with no error.
 *
 * <p>The fix is anti-join-scoped deletion: {@code delete} must skip any id
 * still referenced by a live {@code catalog_document_chunks} row in this
 * collection, deleting only the ids that are NOT so referenced. This suite
 * proves both directions: a shared chash survives a delete call that names
 * it (the bug this bead closes), and a genuinely-unreferenced chash in the
 * same call is still removed (the fix must not degrade into a no-op).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorRepositoryDeleteAntiJoinTest {

    private static final String TENANT_A = "delaj-tenant-a";
    private static final String TENANT_B = "delaj-tenant-b";
    private static final String SVC_ROLE = "svc_delaj_test";
    private static final String SVC_PASS = "svc_delaj_test_pass";

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    private static String col(String testCase) {
        return "code__delaj-" + testCase + "__voyage-code-3__v1";
    }

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository catalogRepo;
    PgVectorRepository vectorRepo;
    HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("GRANT USAGE ON SEQUENCE nexus.catalog_links_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
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
        var embedder = new ConstantEmbedder(1024);
        vectorRepo = new PgVectorRepository(tenantScope, embedder, embedder);
    }

    /** Minimal {@link Embedder}: every text embeds to the same fixed unit vector. */
    private static final class ConstantEmbedder implements Embedder {
        private final int dim;
        ConstantEmbedder(int dim) { this.dim = dim; }

        @Override
        public List<float[]> embed(List<String> texts) {
            float[] v = new float[dim];
            v[0] = 1.0f;
            return texts.stream().map(t -> v.clone()).toList();
        }

        @Override
        public void close() { }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void seedDocWithManifest(String tenant, String docId, String collection,
                                      List<String> chashesInOrder) {
        catalogRepo.upsertDocument(tenant, Map.of(
            "tumbler", docId,
            "title", "delete-anti-join fixture " + docId,
            "content_type", "code",
            "corpus", "code",
            "physical_collection", collection
        ));
        List<Map<String, Object>> rows = new java.util.ArrayList<>();
        for (int i = 0; i < chashesInOrder.size(); i++) {
            rows.add(Map.<String, Object>of("position", i, "chash", chashesInOrder.get(i), "chunk_index", i));
        }
        catalogRepo.writeManifest(tenant, docId, rows);
    }

    private void seedChunk(String tenant, String collection, String chash, String text, String title) {
        vectorRepo.upsertChunks(tenant, collection,
            List.of(chash), List.of(text), List.of(Map.of("title", title)));
    }

    private String chunkText(String collection, String chash) throws SQLException {
        try (var conn = pg.createConnection(""); var st = conn.createStatement()) {
            var rs = st.executeQuery(
                "SELECT chunk_text FROM nexus.chunks_1024 WHERE collection = '" + collection
                + "' AND chash = decode('" + chash + "', 'hex')");
            return rs.next() ? rs.getString(1) : null;
        }
    }

    /**
     * Tenant-scoped variant of {@link #chunkText} — chunks_1024's PK is
     * {@code (tenant_id, collection, chash)}, so two different tenants can
     * legitimately hold a row with the identical (collection, chash) pair.
     * The unscoped query (a raw superuser connection, RLS bypassed) cannot
     * tell those rows apart; use this whenever a test seeds the same
     * (collection, chash) under more than one tenant.
     */
    private String chunkTextForTenant(String tenant, String collection, String chash) throws SQLException {
        try (var conn = pg.createConnection(""); var st = conn.createStatement()) {
            var rs = st.executeQuery(
                "SELECT chunk_text FROM nexus.chunks_1024 WHERE tenant_id = '" + tenant
                + "' AND collection = '" + collection
                + "' AND chash = decode('" + chash + "', 'hex')");
            return rs.next() ? rs.getString(1) : null;
        }
    }

    // ── F10c: the live silent-data-loss bug ──────────────────────────────────

    @Test
    void delete_sharedChunkSurvivesAndOtherDocumentStillReads_whileUnreferencedChunkIsRemoved() throws Exception {
        String collection = col("case1");
        String shared = ch("delaj-shared-1");
        String docAOnly = ch("delaj-a-only-1");

        // Two documents, A and B, both reference `shared` (identical chunk text —
        // RDR-108 collapse). A additionally has its own unreferenced-by-B chash.
        seedChunk(TENANT_A, collection, shared, "shared text", "Shared");
        seedChunk(TENANT_A, collection, docAOnly, "a-only text", "A only");

        seedDocWithManifest(TENANT_A, "delaj.doc.a", collection, List.of(shared, docAOnly));
        seedDocWithManifest(TENANT_A, "delaj.doc.b", collection, List.of(shared));

        // Realistic ordering (matches every one of the nine funnel callers except
        // the F8c misclassified-prune case): document A's OWN manifest is already
        // dealt with — cleared/rewritten — before the physical chunk delete call.
        // atomic_manifest_replace with an empty row list is exactly what a document
        // deletion / re-index-to-zero-chunks does.
        catalogRepo.writeManifest(TENANT_A, "delaj.doc.a", List.of());

        // Caller deletes "document A's chunks" — its former manifest chash set,
        // including the chash it happens to share with document B. The caller
        // has no way to know the sharing; that is exactly the F10c premise.
        vectorRepo.delete(TENANT_A, collection, List.of(shared, docAOnly));

        // The shared chunk MUST survive: document B's manifest still references it.
        assertThat(chunkText(collection, shared))
            .as("F10c: a chunk another document's manifest still references must survive delete")
            .isEqualTo("shared text");

        // The genuinely A-only chunk MUST actually be deleted — anti-join scoping
        // must not degrade into a no-op that protects everything.
        assertThat(chunkText(collection, docAOnly))
            .as("an unreferenced-by-any-other-document chunk must still be deleted")
            .isNull();

        // Document B must still read intact end-to-end (the failure mode F10c
        // describes: fetchDocumentChunks' IllegalStateException on a dangling
        // manifest reference).
        var bChunks = vectorRepo.fetchDocumentChunks(TENANT_A, "delaj.doc.b");
        assertThat(bChunks).hasSize(1);
        assertThat(bChunks.get(0).get("chash")).isEqualTo(shared);
        assertThat(bChunks.get(0).get("chunk_text")).isEqualTo("shared text");
    }

    @Test
    void delete_chunkWithNoManifestReferenceAtAll_isStillDeleted() throws Exception {
        String collection = col("case2");
        String orphan = ch("delaj-orphan-1");
        seedChunk(TENANT_A, collection, orphan, "orphan text", "Orphan");
        // No manifest row at all for this chash.

        vectorRepo.delete(TENANT_A, collection, List.of(orphan));

        assertThat(chunkText(collection, orphan))
            .as("a chunk with zero manifest references anywhere must still be deletable")
            .isNull();
    }

    // ── Review findings (code-review-expert, T2 nexus/review-o8dil5-delete-anti-join-2026-08-11) ──

    @Test
    void delete_returnValue_reflectsAntiJoinSkips() throws Exception {
        // The method's contract changed: it can now return FEWER than
        // ids.size() even with no cross-tenant ids present. Nothing pinned
        // that in the original suite (case1 never checked the return int).
        String collection = col("case3");
        String blocked = ch("delaj-retval-blocked");
        String removable = ch("delaj-retval-removable");

        seedChunk(TENANT_A, collection, blocked, "blocked text", "Blocked");
        seedChunk(TENANT_A, collection, removable, "removable text", "Removable");
        seedDocWithManifest(TENANT_A, "delaj.doc.retval", collection, List.of(blocked));

        int deleted = vectorRepo.delete(TENANT_A, collection, List.of(blocked, removable));

        assertThat(deleted)
            .as("only the genuinely unreferenced id was actually deleted — the manifest-blocked "
                + "id must not be counted even though it was named in the request")
            .isEqualTo(1);
        assertThat(chunkText(collection, blocked)).isEqualTo("blocked text");
        assertThat(chunkText(collection, removable)).isNull();
    }

    @Test
    void delete_sameChashDifferentCollection_manifestInOtherCollectionDoesNotBlock() throws Exception {
        // Same chash value, seeded as two physically distinct chunk rows in
        // two different collections (chunks_1024's PK is (tenant, collection,
        // chash)). A manifest row scoped to collection X must not block a
        // delete of the SAME chash in collection Y — proves the .collection()
        // filter on both the anti-join subquery and the DELETE's own WHERE
        // are both load-bearing, not redundant.
        String collectionX = col("case4x");
        String collectionY = col("case4y");
        String shared = ch("delaj-cross-collection");

        seedChunk(TENANT_A, collectionX, shared, "text in X", "In X");
        seedChunk(TENANT_A, collectionY, shared, "text in Y", "In Y");
        seedDocWithManifest(TENANT_A, "delaj.doc.crosscol", collectionX, List.of(shared));

        int deleted = vectorRepo.delete(TENANT_A, collectionY, List.of(shared));

        assertThat(deleted)
            .as("a manifest reference scoped to a DIFFERENT collection must not block this collection's delete")
            .isEqualTo(1);
        assertThat(chunkText(collectionY, shared)).isNull();
        assertThat(chunkText(collectionX, shared))
            .as("the same chash in the manifest-referencing collection must be untouched")
            .isEqualTo("text in X");
    }

    @Test
    void delete_tombstonedOwner_doesNotBlockDeletion() throws Exception {
        // deleteDocument is a deliberate SOFT tombstone (CatalogRepository
        // javadoc): it sets catalog_documents.deleted_at but leaves the
        // catalog_document_chunks manifest row in place until purge_trash's
        // cascade. A manifest row belonging ONLY to a tombstoned document
        // must not count as "still referenced" -- otherwise the
        // retract-manifest-then-delete-chunk ordering every Python caller
        // uses (tombstone the document, then delete its chunks) would be
        // permanently self-blocking. See PgVectorRepository#delete javadoc,
        // "Live excludes tombstoned owners".
        String collection = col("case5");
        String chash = ch("delaj-tombstoned-owner");

        seedChunk(TENANT_A, collection, chash, "tombstoned-owner text", "Tombstoned");
        seedDocWithManifest(TENANT_A, "delaj.doc.tombstoned", collection, List.of(chash));

        int tombstoned = catalogRepo.deleteDocument(TENANT_A, "delaj.doc.tombstoned");
        assertThat(tombstoned).as("control: the document must actually be tombstoned").isEqualTo(1);

        int deleted = vectorRepo.delete(TENANT_A, collection, List.of(chash));

        assertThat(deleted)
            .as("a manifest row whose only owner is tombstoned must not block deletion")
            .isEqualTo(1);
        assertThat(chunkText(collection, chash)).isNull();
    }

    @Test
    void delete_crossTenant_otherTenantsManifestDoesNotBlockOwnChunk() throws Exception {
        // TENANT_B has a live document manifest referencing `chash` in this
        // collection name; TENANT_A independently owns a physically distinct
        // chunk row for the SAME chash + collection (chunks_1024's PK
        // includes tenant_id, so this is a legitimate distinct row, not a
        // collision). TENANT_A's delete must not be blocked by TENANT_B's
        // manifest -- the anti-join subquery correlates on tenant_id, and
        // RLS additionally scopes the whole call to TENANT_A.
        String collection = col("case6");
        String chash = ch("delaj-cross-tenant-manifest");

        seedChunk(TENANT_B, collection, chash, "tenant b text", "Tenant B");
        seedDocWithManifest(TENANT_B, "delaj.doc.tenantb", collection, List.of(chash));

        seedChunk(TENANT_A, collection, chash, "tenant a text", "Tenant A");
        // TENANT_A has no manifest row for this chash at all.

        int deleted = vectorRepo.delete(TENANT_A, collection, List.of(chash));

        assertThat(deleted)
            .as("tenant B's manifest reference must not block tenant A's own delete")
            .isEqualTo(1);
        assertThat(chunkTextForTenant(TENANT_A, collection, chash))
            .as("tenant A's chunk must be gone")
            .isNull();

        // Control: tenant B's own row (referenced by its own live manifest)
        // must remain fully intact and blocked from deletion by its own tenant.
        int blockedForB = vectorRepo.delete(TENANT_B, collection, List.of(chash));
        assertThat(blockedForB)
            .as("tenant B's still-referenced chunk must remain protected")
            .isEqualTo(0);
        assertThat(chunkTextForTenant(TENANT_B, collection, chash))
            .as("tenant B's own row must have survived untouched throughout")
            .isEqualTo("tenant b text");
    }
}
