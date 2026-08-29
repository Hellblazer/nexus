package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.postgresql.util.PSQLException;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-152 bead nexus-gmiaf.18 — CatalogRepository integration tests.
 *
 * <p>Hermetic embedded Postgres (zonky). Applies the full Liquibase master
 * changelog. 3-connection bootstrap follows ChashRepositoryTest pattern.
 *
 * <p>Asserts:
 * <ol>
 *   <li>Owner upsert + list + by_repo</li>
 *   <li>Document upsert + get + list + delete</li>
 *   <li>FTS: English stemming (run→running, search→searching) + simple token probe</li>
 *   <li>Link upsert + linksFrom/linksTo + link_query + deleteLink</li>
 *   <li>BFS graph traversal (depth 1 and 2)</li>
 *   <li>Manifest write + get + append + purge + chashes + resync</li>
 *   <li>Collection upsert + list + get + supersede + rename</li>
 *   <li>Stats</li>
 *   <li>ETL fidelity + idempotent re-import (document source_mtime GREATEST)</li>
 *   <li>RLS isolation: tenant A rows invisible to tenant B</li>
 *   <li>RLS WITH CHECK: cross-tenant INSERT on catalog_documents rejected</li>
 *   <li>Fail-closed: unset GUC yields 0 rows</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogRepositoryTest {

    private static final String TENANT_A  = "cat-tenant-a";
    private static final String TENANT_B  = "cat-tenant-b";
    private static final String SVC_ROLE  = "svc_catalog_test";
    private static final String SVC_PASS  = "svc_catalog_test_pass";

    /** Canonical 64-hex chash deterministically derived from a seed (RDR-180: the
     *  full sha256 digest is the canonical chash — hand-padded 32-char literals
     *  are retired since the storage column is now bytea(32)). */
    private static String ch(String seed) {
        return dev.nexus.service.db.Chash.ofText(seed).toHex();
    }

    /**
     * RDR-191 Phase 5 (nexus-o8dil.29): {@code fk_catalog_chunks_chunk} now requires
     * every {@code catalog_document_chunks} row's {@code (tenant_id, collection,
     * chash)} to have a matching {@code nexus.chunks} row. This suite exercises the
     * MANIFEST bookkeeping layer in isolation from real vector content, so every
     * manifest-write call site below is routed through one of the {@code *Seeded}
     * wrappers, which stub a minimal {@code nexus.chunks} row (single
     * {@code embedding_384} vector, arbitrary text) for each row's chash first. A
     * chash that is null, not valid hex, or not EXACTLY 64 hex chars (32 bytes) is
     * left unstubbed on purpose — several tests deliberately exercise chash-shaped
     * CHECK-constraint violations (missing chash, wrong octet length), and
     * nexus.chunks carries the SAME {@code chunks_chash_octet_check} (octet_length=32)
     * as catalog_document_chunks (vectors-004-unify-chunks.xml), so a wrong-length
     * stub would itself violate that check rather than reaching the manifest-side
     * violation the test is actually after.
     */
    private static final String STUB_VECTOR_384 =
        "[" + "0.1,".repeat(383) + "0.1]";

    private void stubChunk(String tenant, String collection, Object chashObj) {
        if (!(chashObj instanceof String chashHex) || chashHex.length() != 64) {
            return;
        }
        if (!chashHex.matches("(?i)^[0-9a-f]+$")) {
            return;
        }
        tenantScope.withTenant(tenant, ctx -> {
            // RDR-191 Phase 5 (nexus-o8dil.49): nexus.chunks now carries
            // chunks_collection_fk (tenant_id, collection) -> catalog_collections
            // (tenant_id, name) — stub-register the collection first, mirroring
            // PgVectorRepository#upsertChunks' own ensure-registered step.
            ctx.execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?) "
                + "ON CONFLICT (tenant_id, name) DO NOTHING",
                tenant, collection);
            ctx.execute(
                "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384) "
                + "VALUES (?, ?, decode(?, 'hex'), 'stub', ?::vector) "
                + "ON CONFLICT (tenant_id, collection, chash) DO NOTHING",
                tenant, collection, chashHex, STUB_VECTOR_384);
            return null;
        });
    }

    private void writeManifestSeeded(String tenant, String docId, String collection,
                                      List<Map<String, Object>> rows) {
        for (var row : rows) {
            stubChunk(tenant, collection, row.get("chash"));
        }
        repo.writeManifest(tenant, docId, collection, rows);
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> writeManifestManySeeded(String tenant, List<Map<String, Object>> docs,
                                                          String collection) {
        for (var doc : docs) {
            var rows = (List<Map<String, Object>>) doc.get("rows");
            if (rows != null) {
                for (var row : rows) {
                    stubChunk(tenant, collection, row.get("chash"));
                }
            }
        }
        return repo.writeManifestMany(tenant, docs, collection);
    }

    private void importChunkSeeded(String tenant, String docId, String collection,
                                    Map<String, Object> row) {
        stubChunk(tenant, collection, row.get("chash"));
        repo.importChunk(tenant, docId, collection, row);
    }

    private int importChunksBatchSeeded(String tenant, String docId, String collection,
                                         List<Map<String, Object>> rows) {
        if (rows != null) {
            for (var row : rows) {
                stubChunk(tenant, collection, row.get("chash"));
            }
        }
        return repo.importChunksBatch(tenant, docId, collection, rows);
    }

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // Phase 1: role creation (autoCommit=true; CREATE ROLE cannot run in txn).
        try (Connection su = pg.createConnection("")) {
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

        // Phase 2: apply Liquibase master changelog (separate connection, committed before grants).
        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        // Phase 3: grant svc role access to all catalog tables (separate connection, all Liquibase DDL visible).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (String tbl : new String[]{
                "catalog_owners", "catalog_documents", "catalog_links",
                "catalog_document_chunks", "catalog_collections", "catalog_meta"}) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + tbl + " TO " + SVC_ROLE);
            }
            // Grant sequence for catalog_links BIGSERIAL
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.catalog_links_id_seq TO " + SVC_ROLE);
            // RDR-159 P-1b GRANTs for manifest_backfill()/manifest_orphans(int) REMOVED
            // here (RDR-191 Phase 6, nexus-o8dil.33) -- both functions are DROPPED
            // (catalog-030-retire-manifest-verify.xml); granting EXECUTE on a
            // nonexistent function fails the fixture outright.
            // RDR-191 (nexus-o8dil.48): chunks_384/768/1024 unified into ONE
            // nexus.chunks -- a single GRANT now covers what three did.
            su.createStatement().execute(
                "GRANT SELECT ON nexus.chunks TO " + SVC_ROLE);
            // RDR-164 P3: renameCollection re-homes every denorm-collection table in one txn;
            // grant write broadly so the coherent rename can move children off the old name.
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        // HikariCP as svc role (bare JDBC URL + setUsername, NOT superuser, to enforce RLS).
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);

        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // OWNERS
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(1)
    void owner_upsertAndList() {
        repo.upsertOwner(TENANT_A, Map.of(
            "tumbler_prefix", "1",
            "name", "nexus",
            "owner_type", "repo",
            "repo_hash", "abc123",
            "description", "Nexus repo",
            "repo_root", "/Users/hal/git/nexus",
            "head_hash", "deadbeef"
        ));
        var owners = repo.listOwners(TENANT_A);
        assertThat(owners).isNotEmpty();
        var owner = owners.stream()
            .filter(o -> "1".equals(o.get("tumbler_prefix")))
            .findFirst();
        assertThat(owner).isPresent();
        assertThat(owner.get().get("name")).isEqualTo("nexus");
        assertThat(owner.get().get("owner_type")).isEqualTo("repo");
    }

    @Test @Order(1)
    void owner_writePathsRejectWildcardSentinel() {
        // nexus-45ykb: '*' is a reserved sentinel and can never be a registered owner.
        // Enforced independently at EVERY repository owner-write path (not merely via
        // AuthFilter): upsertOwner, importOwner, and registerDocument (which auto-creates
        // an owner row). Locks the full T_OWNERS write surface.
        assertThrows(IllegalArgumentException.class, () ->
            repo.upsertOwner("*", Map.of(
                "tumbler_prefix", "1", "name", "ghost", "owner_type", "repo")));
        assertThrows(IllegalArgumentException.class, () ->
            repo.importOwner("*", Map.of(
                "tumbler_prefix", "1", "name", "ghost", "owner_type", "repo")));
        assertThrows(IllegalArgumentException.class, () ->
            repo.registerDocument("*", "1", Map.of(
                "tumbler", "1.1", "title", "ghost-doc", "source_uri", "ghost://x")));
    }

    @Test @Order(2)
    void owner_byRepoHash_found() {
        repo.upsertOwner(TENANT_A, Map.of(
            "tumbler_prefix", "2",
            "name", "arcaneum",
            "owner_type", "repo",
            "repo_hash", "feed0000",
            "description", "Arcaneum",
            "repo_root", "/Users/hal/git/arcaneum",
            "head_hash", "cafebabe"
        ));
        var found = repo.ownerByRepoHash(TENANT_A, "feed0000");
        assertThat(found).isNotNull();
        assertThat(found.get("name")).isEqualTo("arcaneum");
    }

    @Test @Order(3)
    void owner_byRepoHash_notFound_returnsNull() {
        var found = repo.ownerByRepoHash(TENANT_A, "no-such-hash");
        assertThat(found).isNull();
    }

    @Test @Order(99)
    void owner_serverSideAllocatesPrefix_whenAbsent() {
        // nexus-0cy4b: the HTTP client (Catalog.ensure_owner_for_repo) sends NO
        // tumbler_prefix and expects the server to assign one (the column is
        // NOT NULL). Fresh tenant -> RLS-clean owner space -> deterministic
        // 1.1, 1.2, and idempotent reuse by repo_hash.
        final String T = "cat-tenant-alloc";
        repo.upsertOwner(T, Map.of(
            "name", "repoA", "owner_type", "repo", "repo_hash", "hashA",
            "repo_root", "/x/a"));
        repo.upsertOwner(T, Map.of(
            "name", "repoB", "owner_type", "repo", "repo_hash", "hashB",
            "repo_root", "/x/b"));

        var a = repo.ownerByRepoHash(T, "hashA");
        var b = repo.ownerByRepoHash(T, "hashB");
        assertThat(a).isNotNull();
        assertThat(b).isNotNull();
        assertThat(a.get("tumbler_prefix")).isEqualTo("1.1");
        assertThat(b.get("tumbler_prefix")).isEqualTo("1.2");

        // Re-register repoA with no prefix -> idempotent (reuses 1.1, not 1.3).
        repo.upsertOwner(T, Map.of(
            "name", "repoA-renamed", "owner_type", "repo", "repo_hash", "hashA",
            "repo_root", "/x/a"));
        var a2 = repo.ownerByRepoHash(T, "hashA");
        assertThat(a2.get("tumbler_prefix")).isEqualTo("1.1");
        assertThat(a2.get("name")).isEqualTo("repoA-renamed");
        assertThat(repo.listOwners(T)).hasSize(2);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // DOCUMENTS
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(10)
    void document_upsertAndGet() {
        repo.upsertDocument(TENANT_A, mapOf(
            "tumbler", "1.1",
            "title", "RDR-152 Postgres Storage Service",
            "author", "Hal Hildebrand",
            "year", 2026,
            "content_type", "rdr",
            "file_path", "docs/rdr/rdr-152.md",
            "corpus", "rdr",
            "physical_collection", "rdr__nexus__voyage-context-3__v1",
            "chunk_count", 42,
            "head_hash", "aabbccdd",
            "indexed_at", "2026-06-01T00:00:00Z",
            "source_mtime", 1.0,
            "source_uri", "file:///Users/hal/git/nexus/docs/rdr/rdr-152.md"
        ));
        var doc = repo.getDocument(TENANT_A, "1.1");
        assertThat(doc).isNotNull();
        assertThat(doc.get("title")).isEqualTo("RDR-152 Postgres Storage Service");
        assertThat(doc.get("author")).isEqualTo("Hal Hildebrand");
        assertThat(doc.get("year")).isEqualTo(2026);
        assertThat(doc.get("content_type")).isEqualTo("rdr");
        assertThat(doc.get("chunk_count")).isEqualTo(42);
    }

    @Test @Order(11)
    void document_getNotFound_returnsNull() {
        assertThat(repo.getDocument(TENANT_A, "99.99.99")).isNull();
    }

    @Test @Order(12)
    void document_listDocuments_returnsPaged() {
        // Seed a few more docs
        for (int i = 2; i <= 4; i++) {
            repo.upsertDocument(TENANT_A, Map.of(
                "tumbler", "1." + i,
                "title", "Doc " + i,
                "author", "Author " + i,
                "content_type", "paper",
                "corpus", "knowledge",
                "physical_collection", "knowledge__nexus__v1"
            ));
        }
        var all = repo.listDocuments(TENANT_A, 100, 0);
        assertThat(all.size()).isGreaterThanOrEqualTo(4); // 1.1 + 1.2-1.4
    }

    @Test @Order(13)
    void document_updateFields() {
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "2.1",
            "title", "Old Title",
            "author", "Author A",
            "content_type", "paper",
            "corpus", "knowledge"
        ));
        int updated = repo.updateDocument(TENANT_A, "2.1", Map.of("title", "New Title", "year", 2025));
        assertThat(updated).isEqualTo(1);
        var doc = repo.getDocument(TENANT_A, "2.1");
        assertThat(doc.get("title")).isEqualTo("New Title");
        assertThat(doc.get("year")).isEqualTo(2025);

        // RDR-168 nexus-njrcn.7: a "meta" object field must be JSON-encoded into the
        // jsonb metadata column, not bound as a raw Map (which threw
        // "LinkedHashMap is not supported in dialect POSTGRES" → 500).
        int metaUpdated = repo.updateDocument(
            TENANT_A, "2.1", Map.of("meta", Map.of("content_hash", "abc123")));
        assertThat(metaUpdated).isEqualTo(1);
    }

    @Test @Order(12)
    @SuppressWarnings("unchecked")
    void document_updateMeta_mergesLikeLocalCatalog() {
        // nexus-ke45f: local Catalog.update() MERGES meta (dict.update —
        // add/overwrite keys, never remove); the wire did a bare
        // SET metadata=<new>, so every service-mode writer.update(meta=...)
        // silently dropped pre-existing keys (miss_count, content_hash, ...).
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "2.15",
            "title", "Merge Target",
            "content_type", "paper",
            "metadata", Map.of("content_hash", "keepme", "miss_count", 1)
        ));

        int n = repo.updateDocument(
            TENANT_A, "2.15", Map.of("meta", Map.of("bib_checked", true)));
        assertThat(n).isEqualTo(1);

        var doc = repo.getDocument(TENANT_A, "2.15");
        Map<String, Object> meta = (Map<String, Object>) doc.get("metadata");
        assertThat(meta.get("content_hash"))
            .as("pre-existing key survives a merge update").isEqualTo("keepme");
        assertThat(meta.get("miss_count")).isEqualTo(1);
        assertThat(meta.get("bib_checked")).isEqualTo(true);

        // Overwrite semantics: an incoming key replaces the old value.
        repo.updateDocument(TENANT_A, "2.15", Map.of("meta", Map.of("miss_count", 0)));
        var doc2 = repo.getDocument(TENANT_A, "2.15");
        Map<String, Object> meta2 = (Map<String, Object>) doc2.get("metadata");
        assertThat(meta2.get("miss_count")).isEqualTo(0);
        assertThat(meta2.get("content_hash")).isEqualTo("keepme");
    }

    @Test @Order(13)
    void document_update_rejectsNonWhitelistedColumns() {
        // Wave review (SQL audit CRITICAL): request JSON keys become SET targets —
        // without the whitelist, POST /v1/catalog/update could write ANY column,
        // including tenant_id (re-homing a document across tenants). Unknown keys
        // must fail loud (IllegalArgumentException → 400), never silently apply.
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "2.9",
            "title", "Guarded",
            "content_type", "paper",
            "corpus", "knowledge"
        ));
        org.assertj.core.api.Assertions.assertThatThrownBy(() ->
                repo.updateDocument(TENANT_A, "2.9", Map.of("tenant_id", "tenant-b")))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("tenant_id");
        org.assertj.core.api.Assertions.assertThatThrownBy(() ->
                repo.updateDocument(TENANT_A, "2.9", Map.of("created_at", "2020-01-01")))
            .isInstanceOf(IllegalArgumentException.class);
        org.assertj.core.api.Assertions.assertThatThrownBy(() ->
                repo.updateDocument(TENANT_A, "2.9", Map.of("no_such_column", "x")))
            .isInstanceOf(IllegalArgumentException.class);
        // deleted_at keeps its documented silent-strip contract (trash/restore own it):
        // stripped -> no other field set -> 0 rows, no exception.
        assertThat(repo.updateDocument(TENANT_A, "2.9", Map.of("deleted_at", "now"))).isZero();
        // Document unharmed and still updatable through the whitelist.
        assertThat(repo.updateDocument(TENANT_A, "2.9", Map.of("title", "Still Guarded"))).isEqualTo(1);
        assertThat(repo.getDocument(TENANT_A, "2.9").get("title")).isEqualTo("Still Guarded");
    }

    @Test @Order(135)
    void document_updateDocumentsMany_batchesHeterogeneousUpdatesInOneRoundTrip() {
        // nexus-xedhp: replaces N serial writer.update() calls with one
        // updateDocumentsMany() batch — each entry may set DIFFERENT fields
        // (mirrors the indexer catalog hook's real per-file payload shape:
        // head_hash is repo-wide but physical_collection/meta/source_mtime
        // vary per file).
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "2.20", "title", "Many A", "content_type", "code",
            "corpus", "code", "physical_collection", "code__nexus__v1"));
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "2.21", "title", "Many B", "content_type", "docs",
            "corpus", "docs", "physical_collection", "docs__nexus__v1",
            "metadata", Map.of("content_hash", "keepme")));

        List<Integer> results = repo.updateDocumentsMany(TENANT_A, List.of(
            Map.of("tumbler", "2.20", "head_hash", "abc123", "source_mtime", 111.0),
            Map.of("tumbler", "2.21", "head_hash", "abc123",
                   "meta", Map.of("bib_checked", true))
        ));
        assertThat(results).containsExactly(1, 1);

        var doc20 = repo.getDocument(TENANT_A, "2.20");
        assertThat(doc20.get("head_hash")).isEqualTo("abc123");

        var doc21 = repo.getDocument(TENANT_A, "2.21");
        assertThat(doc21.get("head_hash")).isEqualTo("abc123");
        @SuppressWarnings("unchecked")
        Map<String, Object> meta21 = (Map<String, Object>) doc21.get("metadata");
        assertThat(meta21.get("content_hash")).as("meta merge semantics preserved in batch path").isEqualTo("keepme");
        assertThat(meta21.get("bib_checked")).isEqualTo(true);
    }

    @Test @Order(136)
    void document_updateDocumentsMany_isolatesPerEntryFailures() {
        // A malformed entry (missing tumbler, non-updatable column) must not
        // abort the rest of the batch — mirrors register_many's per-doc
        // failure isolation, which the indexer's catalog hook depends on.
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "2.22", "title", "Survivor", "content_type", "code",
            "corpus", "code"));

        List<Integer> results = repo.updateDocumentsMany(TENANT_A, java.util.Arrays.asList(
            Map.of("no_tumbler_key", "x"),
            Map.of("tumbler", "2.22", "head_hash", "def456"),
            Map.of("tumbler", "2.999-does-not-exist", "head_hash", "def456"),
            Map.of("tumbler", "2.22", "no_such_column", "y")
        ));
        assertThat(results).containsExactly(-1, 1, 0, -1);

        var doc = repo.getDocument(TENANT_A, "2.22");
        assertThat(doc.get("head_hash")).isEqualTo("def456");
    }

    @Test @Order(137)
    void document_updateDocumentsMany_emptyListReturnsEmpty() {
        assertThat(repo.updateDocumentsMany(TENANT_A, List.of())).isEmpty();
    }

    @Test @Order(138)
    void document_deleteDocumentsMany_tombstonesInOneRoundTrip() {
        // nexus-xedhp: completes the update_many/register_many/delete_many
        // batch trio — replaces N serial writer.delete_document() calls.
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "2.30", "title", "Del A", "content_type", "code", "corpus", "code"));
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "2.31", "title", "Del B", "content_type", "code", "corpus", "code"));
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "2.32", "title", "Survivor", "content_type", "code", "corpus", "code"));

        Set<String> deleted = repo.deleteDocumentsMany(
            TENANT_A, List.of("2.30", "2.31", "2.999-does-not-exist"));

        assertThat(deleted).containsExactlyInAnyOrder("2.30", "2.31");
        assertThat(repo.getDocument(TENANT_A, "2.30")).isNull();
        assertThat(repo.getDocument(TENANT_A, "2.31")).isNull();
        assertThat(repo.getDocument(TENANT_A, "2.32")).isNotNull();
    }

    @Test @Order(139)
    void document_deleteDocumentsMany_idempotentOnAlreadyTombstoned() {
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "2.33", "title", "Once", "content_type", "code", "corpus", "code"));
        repo.deleteDocument(TENANT_A, "2.33");

        Set<String> deleted = repo.deleteDocumentsMany(TENANT_A, List.of("2.33"));

        assertThat(deleted).as("already-tombstoned tumbler is not re-reported as deleted").isEmpty();
    }

    @Test @Order(140)
    void document_deleteDocumentsMany_emptyListReturnsEmpty() {
        assertThat(repo.deleteDocumentsMany(TENANT_A, List.of())).isEmpty();
    }

    @Test @Order(14)
    void document_delete() {
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "3.1",
            "title", "To Delete",
            "content_type", "paper",
            "corpus", "knowledge"
        ));
        assertThat(repo.getDocument(TENANT_A, "3.1")).isNotNull();
        int deleted = repo.deleteDocument(TENANT_A, "3.1");
        assertThat(deleted).isEqualTo(1);
        assertThat(repo.getDocument(TENANT_A, "3.1")).isNull();
    }

    @Test @Order(15)
    void document_countDocuments() {
        long count = repo.countDocuments(TENANT_A);
        assertThat(count).isGreaterThan(0);
    }

    /**
     * GH #1350 Fix B (nexus-lc8r5): documentsByOwnerAndFilePath must filter by
     * BOTH owner prefix and exact file_path. The owner-only path returns the
     * whole owner list, which drove the client's docs[0] mis-attribution
     * (silent manifest overwrite).
     */
    @Test @Order(16)
    void document_byOwnerAndFilePath_filtersByBoth() {
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "7.1", "title", "Owner7 A", "content_type", "paper",
            "corpus", "knowledge", "file_path", "owner7/a.pdf"));
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "7.2", "title", "Owner7 B", "content_type", "paper",
            "corpus", "knowledge", "file_path", "owner7/b.pdf"));

        // Exact existing path under the owner: exactly one, the right one.
        var hit = repo.documentsByOwnerAndFilePath(TENANT_A, "7", "owner7/b.pdf", 0, 0);
        assertThat(hit).hasSize(1);
        assertThat(hit.get(0).get("tumbler")).isEqualTo("7.2");

        // Brand-new path under a POPULATED owner: zero (this is what stops the
        // corruption — the client no longer receives docs[0] of the owner).
        var miss = repo.documentsByOwnerAndFilePath(TENANT_A, "7", "owner7/brand-new.pdf", 0, 0);
        assertThat(miss).isEmpty();
    }

    /**
     * RDR-159 P-1a (nexus-0wz93): relationCounts returns tenant-scoped row
     * counts for whitelisted migration-verify relations and OMITS any
     * relation outside the whitelist (no arbitrary relation counts).
     *
     * <p>Scoped to the catalog relations the svc role can SELECT
     * (catalog_owners / catalog_documents / catalog_collections /
     * catalog_document_chunks / catalog_links); other verify relations
     * (nexus.memory, …) are exercised in production where the service role
     * holds the grants.
     *
     * <p>RDR-176 Gap 1a (nexus-t9rmg.12): owners, collections, and
     * document_chunks are now in the verify whitelist (previously only
     * documents + links were counted, so a partial copy of the other three
     * reconciled GREEN). Mirrors the Python {@code _VERIFY_TABLES} extension.
     */
    @Test @Order(40)
    void migration_relationCounts_whitelisted_and_tenant_scoped() {
        var counts = repo.relationCounts(TENANT_A, List.of(
            "nexus.catalog_owners",
            "nexus.catalog_documents",
            "nexus.catalog_collections",
            "nexus.catalog_document_chunks",
            "nexus.catalog_links",
            "nexus.pg_class"             // not whitelisted → omitted
        ));
        // catalog_documents has rows for TENANT_A from earlier ordered tests
        assertThat(counts).containsKey("nexus.catalog_documents");
        assertThat(counts.get("nexus.catalog_documents")).isGreaterThan(0L);
        assertThat(counts).containsKey("nexus.catalog_links");
        // RDR-176 Gap 1a: the three formerly-unverified catalog relations are
        // now counted (presence proves whitelisting; counts are tenant-scoped).
        assertThat(counts).containsKey("nexus.catalog_owners");
        assertThat(counts).containsKey("nexus.catalog_collections");
        assertThat(counts).containsKey("nexus.catalog_document_chunks");
        // non-whitelisted relations are silently omitted
        assertThat(counts).doesNotContainKey("nexus.pg_class");
    }

    /**
     * nexus-te885.10: the four formerly count-unmapped telemetry tables are
     * now whitelisted, so verify-fill's outer count-diff (and the watermark
     * target-shrank invalidation guard) can read them. Empty tables count 0 —
     * presence in the result proves whitelisting.
     */
    @Test @Order(42)
    void migration_relationCounts_includesTelemetryTables() {
        var counts = repo.relationCounts(TENANT_A, List.of(
            "nexus.relevance_log",
            "nexus.search_telemetry",
            "nexus.tier_writes",
            "nexus.frecency"
        ));
        assertThat(counts).containsKeys(
            "nexus.relevance_log", "nexus.search_telemetry",
            "nexus.tier_writes", "nexus.frecency");
    }

    @Test @Order(41)
    void migration_relationCounts_is_tenant_isolated() {
        // TENANT_B has no catalog_documents; its count is 0, not TENANT_A's.
        var counts = repo.relationCounts(TENANT_B, List.of("nexus.catalog_documents"));
        assertThat(counts.get("nexus.catalog_documents")).isEqualTo(0L);
    }

    @Test @Order(43)
    void migration_relationCounts_droppedChashIndex_isIndeterminateNotException() {
        // nexus-20agh: nexus.chash_index was dropped (RDR-187) and is no
        // longer in VERIFY_RELATIONS. A caller (e.g. a stale relations list
        // from before the drop) requesting it must get the same silent-omit
        // treatment as any other unwhitelisted relation — an absent key the
        // caller reads as INDETERMINATE — never an unhandled SQL exception
        // from querying a table that no longer exists in this (post-drop,
        // fully-migrated) integration schema.
        var counts = repo.relationCounts(TENANT_A, List.of(
            "nexus.chash_index",
            "nexus.catalog_documents"
        ));
        assertThat(counts).doesNotContainKey("nexus.chash_index");
        assertThat(counts).containsKey("nexus.catalog_documents");
    }

    @Test @Order(16)
    void document_documentsByCollection() {
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "4.1",
            "title", "In Collection",
            "content_type", "paper",
            "corpus", "knowledge",
            "physical_collection", "knowledge__unit_test_coll"
        ));
        var docs = repo.documentsByCollection(TENANT_A, "knowledge__unit_test_coll", 0, 0);
        assertThat(docs).hasSize(1);
        assertThat(docs.get(0).get("tumbler")).isEqualTo("4.1");
    }

    /**
     * nexus-xoimv: repository-level signature check — {@code limit <= 0} is
     * unbounded, an explicit positive {@code limit} is honored, and
     * {@code limit}+{@code offset} together page without overlap or gap
     * (ORDER BY tumbler gives a stable cursor). The HTTP-layer equivalent
     * across all seven filter branches lives in
     * {@code CatalogHandlerListPaginationTest}; this is the direct repo-level
     * sanity check for the same new signature.
     */
    @Test @Order(17)
    void documentsByCollection_limitAndOffset_pageWithoutOverlap() {
        final String coll = "knowledge__xoimv_page_coll";
        for (int i = 1; i <= 5; i++) {
            repo.upsertDocument(TENANT_A, Map.of(
                "tumbler", "4.20." + i, "title", "Page Doc " + i,
                "content_type", "paper", "corpus", "knowledge",
                "physical_collection", coll));
        }
        // limit <= 0: unbounded.
        assertThat(repo.documentsByCollection(TENANT_A, coll, 0, 0)).hasSize(5);
        // explicit limit: honored.
        assertThat(repo.documentsByCollection(TENANT_A, coll, 2, 0)).hasSize(2);
        // paging reconstructs the full ordered set with no overlap/gap.
        var page1 = repo.documentsByCollection(TENANT_A, coll, 2, 0);
        var page2 = repo.documentsByCollection(TENANT_A, coll, 2, 2);
        var page3 = repo.documentsByCollection(TENANT_A, coll, 2, 4);
        var reconstructed = new java.util.ArrayList<String>();
        for (var page : List.of(page1, page2, page3)) {
            for (var d : page) reconstructed.add((String) d.get("tumbler"));
        }
        assertThat(reconstructed).containsExactly(
            "4.20.1", "4.20.2", "4.20.3", "4.20.4", "4.20.5");
    }

    /**
     * nexus-xoimv: {@code documentsBySourceUri} pagination cannot be
     * exercised through the HTTP handler ({@code catalog-016-source-uri-
     * unique.xml} enforces a partial unique index on {@code (tenant_id,
     * source_uri)} for any non-empty value, and a blank {@code source_uri}
     * query param never routes into this branch in {@code handleList}) — so
     * it is covered here, directly, using the empty-string {@code source_uri}
     * every document without one carries by default (excluded from the
     * unique index, so multiple live rows may share it).
     */
    @Test @Order(18)
    void documentsBySourceUri_limitAndOffset_pageWithoutOverlap() {
        final String tenant = "xoimv-source-uri-tenant";
        for (int i = 1; i <= 4; i++) {
            repo.upsertDocument(tenant, Map.of(
                "tumbler", "8.30." + i, "title", "No-URI Doc " + i,
                "content_type", "code", "corpus", "code"));
        }
        assertThat(repo.documentsBySourceUri(tenant, "", 0, 0)).hasSize(4);
        assertThat(repo.documentsBySourceUri(tenant, "", 2, 0)).hasSize(2);
        var page1 = repo.documentsBySourceUri(tenant, "", 2, 0);
        var page2 = repo.documentsBySourceUri(tenant, "", 2, 2);
        var reconstructed = new java.util.ArrayList<String>();
        for (var page : List.of(page1, page2)) {
            for (var d : page) reconstructed.add((String) d.get("tumbler"));
        }
        assertThat(reconstructed).containsExactly("8.30.1", "8.30.2", "8.30.3", "8.30.4");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // FTS — the key correctness gate for the OPTION B intentional-upgrade
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(20)
    void fts_englishStemming_runsRunningBothMatch() {
        // Insert a doc whose title contains "running"
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "5.1",
            "title", "Running in the Background",
            "author", "Test Author",
            "content_type", "paper",
            "corpus", "knowledge"
        ));
        // English stemming: "run" should match "running" via ts_vector('english', ...)
        var results = repo.searchDocuments(TENANT_A, "run", null, 50);
        var tumblers = results.stream().map(d -> (String) d.get("tumbler")).toList();
        assertThat(tumblers).as("English stemming: 'run' should match 'running'").contains("5.1");
    }

    @Test @Order(21)
    void fts_simpleTokenExact_corpusMatch() {
        // Insert a doc with a specific corpus that is an identifier (no stemming needed)
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "5.2",
            "title", "Some Paper",
            "author", "Some Author",
            "content_type", "paper",
            "corpus", "assetops-kg",
            "physical_collection", "knowledge__assetops"
        ));
        // Simple tokenizer should find "assetops" exactly in the corpus field
        var results = repo.searchDocuments(TENANT_A, "assetops", null, 50);
        var tumblers = results.stream().map(d -> (String) d.get("tumbler")).toList();
        assertThat(tumblers).as("Simple token: 'assetops' should match corpus field exactly").contains("5.2");
    }

    @Test @Order(22)
    void fts_contentTypeFilter_narrowsResults() {
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "5.3",
            "title", "FTS Filter Paper",
            "content_type", "paper",
            "corpus", "knowledge"
        ));
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "5.4",
            "title", "FTS Filter RDR",
            "content_type", "rdr",
            "corpus", "rdr"
        ));
        // Search without filter returns both; with filter returns only matching type
        var rdrOnly = repo.searchDocuments(TENANT_A, "FTS Filter", "rdr", 50);
        var tumblers = rdrOnly.stream().map(d -> (String) d.get("tumbler")).toList();
        assertThat(tumblers).contains("5.4");
        assertThat(tumblers).doesNotContain("5.3");
    }

    @Test @Order(23)
    void fts_emptyQuery_returnsEmpty() {
        var results = repo.searchDocuments(TENANT_A, "", null, 50);
        assertThat(results).isEmpty();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // LINKS
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(30)
    void link_upsertAndLinksFrom() {
        // Ensure referenced documents exist to avoid FK constraint (if any)
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "lnk.1", "title", "Link Source",
            "content_type", "paper", "corpus", "knowledge"));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "lnk.2", "title", "Link Target",
            "content_type", "paper", "corpus", "knowledge"));

        // RDR-168 nexus-njrcn.3: upsertLink returns true=created on first insert,
        // false=merged on the ON CONFLICT path (the created-vs-merged signal).
        boolean created = repo.upsertLink(TENANT_A, Map.of(
            "from_tumbler", "lnk.1",
            "to_tumbler", "lnk.2",
            "link_type", "cites",
            "from_span", "",
            "to_span", "",
            "created_by", "user",
            "created_at", "2026-06-01T00:00:00Z"
        ));
        assertThat(created).as("first upsert inserts → created").isTrue();
        boolean merged = repo.upsertLink(TENANT_A, Map.of(
            "from_tumbler", "lnk.1", "to_tumbler", "lnk.2", "link_type", "cites",
            "from_span", "", "to_span", "", "created_by", "user2",
            "created_at", "2026-06-02T00:00:00Z"
        ));
        assertThat(merged).as("second upsert conflicts → merged").isFalse();
        var links = repo.linksFrom(TENANT_A, "lnk.1", (java.util.List<String>) null);
        assertThat(links).hasSize(1);
        assertThat(links.get(0).get("to_tumbler")).isEqualTo("lnk.2");
        assertThat(links.get(0).get("link_type")).isEqualTo("cites");
    }

    @Test @Order(95)
    void orphanedLinks_findsDanglingEndpointsAndNamesTheSide() {
        // nexus-ysrwi (GH #1419 issue 7): Steve's backup held 5 of 52 links
        // pointing at tumblers with no document anywhere in the same pg_dump.
        // catalog_links has a PK and a UNIQUE but NO foreign key to
        // catalog_documents (catalog-001-baseline.xml), so nothing structurally
        // prevented this at the time — nexus-tk070.p1 (RDR-194 § D2) closed
        // exactly that gap with fk_catalog_links_from_document /
        // fk_catalog_links_to_document, so this test's damage-seed no longer
        // fits: a link to a tumbler with NO catalog_documents row at all now
        // 400s (SQLSTATE 23503 mapped to dangling_endpoint), even under
        // allow_dangling=true. orphanedLinks() itself is unchanged and its
        // remaining, narrower job — per its own updated javadoc — is exactly
        // the TOMBSTONED-endpoint case the FK deliberately does not cover
        // (soft delete does not fire ON DELETE CASCADE), so the seed below
        // creates real documents and TOMBSTONES them (repo.deleteDocument)
        // rather than pointing at tumblers that never existed.
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "orph.live", "title", "Live",
            "content_type", "paper", "corpus", "knowledge"));

        // A fully-resolvable link: must NOT be reported. Uses its OWN tumblers —
        // this class is @Order-ed and shares state, so borrowing lnk.1/lnk.2
        // would change the link counts that link_linksTo/link_filterByType assert.
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "orph.src", "title", "Resolvable Src",
            "content_type", "paper", "corpus", "knowledge"));
        repo.upsertLink(TENANT_A, Map.of("from_tumbler", "orph.src", "to_tumbler", "orph.live",
            "link_type", "relates", "from_span", "", "to_span", "",
            "created_by", "user", "created_at", "2026-06-01T00:00:00Z"));

        // Endpoints that WILL be tombstoned (row exists, satisfies the FK;
        // deleted_at set, so orphanedLinks' LIVE-only predicate still flags
        // them). allow_dangling=true is still required — requireLiveEndpoints
        // rejects a tombstoned target the same as a missing one.
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "orph.gone", "title", "Will be tombstoned (target)",
            "content_type", "paper", "corpus", "knowledge"));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "orph.vanished", "title", "Will be tombstoned (source)",
            "content_type", "paper", "corpus", "knowledge"));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "orph.x", "title", "Will be tombstoned (both x)",
            "content_type", "paper", "corpus", "knowledge"));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "orph.y", "title", "Will be tombstoned (both y)",
            "content_type", "paper", "corpus", "knowledge"));

        // Dangling TARGET (the document-deletion shape Steve hit).
        repo.upsertLink(TENANT_A, Map.of("from_tumbler", "orph.live", "to_tumbler", "orph.gone",
            "link_type", "cites", "from_span", "", "to_span", "",
            "created_by", "user", "created_at", "2026-06-01T00:00:00Z",
            "allow_dangling", true));
        // Dangling SOURCE.
        repo.upsertLink(TENANT_A, Map.of("from_tumbler", "orph.vanished", "to_tumbler", "orph.live",
            "link_type", "cites", "from_span", "", "to_span", "",
            "created_by", "user", "created_at", "2026-06-01T00:00:00Z",
            "allow_dangling", true));
        // BOTH endpoints eventually gone.
        repo.upsertLink(TENANT_A, Map.of("from_tumbler", "orph.x", "to_tumbler", "orph.y",
            "link_type", "cites", "from_span", "", "to_span", "",
            "created_by", "user", "created_at", "2026-06-01T00:00:00Z",
            "allow_dangling", true));

        // Tombstone the four endpoints AFTER the links are written (the FK
        // only needs the row to exist at write time; ON DELETE CASCADE does
        // not fire for a soft delete, so the links survive pointing at now-
        // tombstoned documents — exactly the case orphanedLinks still reports).
        repo.deleteDocument(TENANT_A, "orph.gone");
        repo.deleteDocument(TENANT_A, "orph.vanished");
        repo.deleteDocument(TENANT_A, "orph.x");
        repo.deleteDocument(TENANT_A, "orph.y");

        var orphans = repo.orphanedLinks(TENANT_A);

        var pairs = orphans.stream()
            .map(m -> m.get("from_tumbler") + "->" + m.get("to_tumbler") + ":" + m.get("side"))
            .toList();
        assertThat(pairs).contains(
            "orph.live->orph.gone:to",
            "orph.vanished->orph.live:from",
            "orph.x->orph.y:both");
        // The resolvable link must never appear — a check that cries wolf gets
        // ignored, which is how a real orphan gets missed.
        assertThat(pairs).noneMatch(x -> x.startsWith("orph.src->orph.live"));
    }

    @Test @Order(96)
    void orphanedLinks_isTenantScoped() {
        // RLS safety: tenant B must not see tenant A's orphans.
        var bOrphans = repo.orphanedLinks(TENANT_B);
        assertThat(bOrphans).noneMatch(m ->
            String.valueOf(m.get("from_tumbler")).startsWith("orph."));
    }

    @Test @Order(31)
    void link_linksTo() {
        var links = repo.linksTo(TENANT_A, "lnk.2", (java.util.List<String>) null);
        assertThat(links).hasSize(1);
        assertThat(links.get(0).get("from_tumbler")).isEqualTo("lnk.1");
    }

    @Test @Order(32)
    void link_filterByType() {
        // Add a second link with a different type
        repo.upsertLink(TENANT_A, Map.of(
            "from_tumbler", "lnk.1",
            "to_tumbler", "lnk.2",
            "link_type", "implements",
            "created_by", "user",
            "created_at", "2026-06-01T00:00:00Z"
        ));
        var citesLinks = repo.linksFrom(TENANT_A, "lnk.1", java.util.List.of("cites"));
        assertThat(citesLinks).hasSize(1);
        assertThat(citesLinks.get(0).get("link_type")).isEqualTo("cites");

        var implLinks = repo.linksFrom(TENANT_A, "lnk.1", java.util.List.of("implements"));
        assertThat(implLinks).hasSize(1);
        assertThat(implLinks.get(0).get("link_type")).isEqualTo("implements");

        // RDR-168 njrcn.5: server-side IN filter over a SET of link types.
        var bothTypes = repo.linksFrom(TENANT_A, "lnk.1", java.util.List.of("cites", "implements"));
        assertThat(bothTypes).hasSize(2);
        var onlyCites = repo.linksFrom(TENANT_A, "lnk.1", java.util.List.of("cites", "relates"));
        assertThat(onlyCites).hasSize(1);
        assertThat(onlyCites.get(0).get("link_type")).isEqualTo("cites");
    }

    @Test @Order(33)
    void link_deleteLink() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "del.1", "title", "Del Source",
            "content_type", "paper", "corpus", "knowledge"));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "del.2", "title", "Del Target",
            "content_type", "paper", "corpus", "knowledge"));
        repo.upsertLink(TENANT_A, Map.of(
            "from_tumbler", "del.1", "to_tumbler", "del.2",
            "link_type", "cites", "created_by", "user", "created_at", "2026-06-01T00:00:00Z"
        ));
        assertThat(repo.linksFrom(TENANT_A, "del.1", (java.util.List<String>) null)).hasSize(1);
        int deleted = repo.deleteLink(TENANT_A, "del.1", "del.2", "cites");
        assertThat(deleted).isEqualTo(1);
        assertThat(repo.linksFrom(TENANT_A, "del.1", (java.util.List<String>) null)).isEmpty();
    }

    @Test @Order(34)
    void link_queryLinks_withFilters() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "qry.1", "title", "Query Source",
            "content_type", "paper", "corpus", "knowledge"));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "qry.2", "title", "Query Target",
            "content_type", "paper", "corpus", "knowledge"));
        repo.upsertLink(TENANT_A, Map.of(
            "from_tumbler", "qry.1", "to_tumbler", "qry.2",
            "link_type", "relates", "created_by", "developer", "created_at", "2026-06-01T00:00:00Z"
        ));
        var links = repo.queryLinks(TENANT_A, "qry.1", null, null, "developer", null, 50, 0, null, null);
        assertThat(links).hasSize(1);
        assertThat(links.get(0).get("from_tumbler")).isEqualTo("qry.1");
        assertThat(links.get(0).get("created_by")).isEqualTo("developer");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // BFS GRAPH TRAVERSAL
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(40)
    void graphBFS_depth1_bothDirections() {
        // Seed: A -> B -> C (cites chain)
        for (String t : List.of("bfs.A", "bfs.B", "bfs.C")) {
            repo.upsertDocument(TENANT_A, Map.of("tumbler", t,
                "title", "BFS Node " + t, "content_type", "paper", "corpus", "knowledge"));
        }
        repo.upsertLink(TENANT_A, Map.of("from_tumbler", "bfs.A", "to_tumbler", "bfs.B",
            "link_type", "cites", "created_by", "user", "created_at", "2026-06-01T00:00:00Z"));
        repo.upsertLink(TENANT_A, Map.of("from_tumbler", "bfs.B", "to_tumbler", "bfs.C",
            "link_type", "cites", "created_by", "user", "created_at", "2026-06-01T00:00:00Z"));

        // Depth 1 from bfs.B: should see bfs.A and bfs.C (in both directions)
        var result = repo.graphBFS(TENANT_A, List.of("bfs.B"), List.of(), "both", 1);
        @SuppressWarnings("unchecked")
        var edges = (List<Map<String, Object>>) result.get("edges");
        assertThat(edges).hasSizeGreaterThanOrEqualTo(2);
    }

    @Test @Order(41)
    void graphBFS_depth2_followsChain() {
        // Depth 2 from bfs.A should reach bfs.C via bfs.B
        var result = repo.graphBFS(TENANT_A, List.of("bfs.A"), List.of("cites"), "out", 2);
        @SuppressWarnings("unchecked")
        var nodes = (List<Map<String, Object>>) result.get("nodes");
        var tumblers = nodes.stream().map(n -> (String) n.get("tumbler")).toList();
        assertThat(tumblers).contains("bfs.B", "bfs.C");
    }

    @Test @Order(42)
    void graphBFS_emptySeeds_returnsEmpty() {
        var result = repo.graphBFS(TENANT_A, List.of(), List.of(), "both", 1);
        assertThat((List<?>) result.get("nodes")).isEmpty();
        assertThat((List<?>) result.get("edges")).isEmpty();
    }

    /**
     * nexus-t7m8e leg (a)+(b): a tombstoned document must not act as a live
     * relay. A(live) --cites--> D(tombstoned) --cites--> B(live): before the
     * fix, D was added to the frontier with no liveness check, so B was
     * reachable at depth 2 despite D being invisible. After the fix, every
     * edge touching D is excluded from the traversal (both endpoints of an
     * edge must be live), so D can never forward reachability to B.
     */
    @Test
    void graphBFS_tombstonedRelay_unreachableAtDepth2() {
        String tenant = "bfs-tomb-" + System.nanoTime();
        String a = "tr.A", d = "tr.D", b = "tr.B";
        for (String t : List.of(a, d, b)) {
            repo.upsertDocument(tenant, Map.of("tumbler", t,
                "title", "Relay " + t, "content_type", "paper", "corpus", "knowledge"));
        }
        repo.upsertLink(tenant, Map.of("from_tumbler", a, "to_tumbler", d,
            "link_type", "cites", "created_by", "test"));
        repo.upsertLink(tenant, Map.of("from_tumbler", d, "to_tumbler", b,
            "link_type", "cites", "created_by", "test"));

        assertThat(repo.deleteDocument(tenant, d))
            .as("precondition: D must actually be tombstoned").isEqualTo(1);

        var result = repo.graphBFS(tenant, List.of(a), List.of("cites"), "out", 2);
        @SuppressWarnings("unchecked")
        var nodes = (List<Map<String, Object>>) result.get("nodes");
        @SuppressWarnings("unchecked")
        var edges = (List<Map<String, Object>>) result.get("edges");
        var nodeTumblers = tumblersOf(nodes);

        assertThat(nodeTumblers)
            .as("D is tombstoned — invisible as a node")
            .doesNotContain(d);
        assertThat(nodeTumblers)
            .as("B is reachable ONLY via the tombstoned relay D — must be unreachable")
            .doesNotContain(b);
        for (var e : edges) {
            assertThat(e.get("from_tumbler")).as("no edge may name the tombstoned relay").isNotEqualTo(d);
            assertThat(e.get("to_tumbler")).as("no edge may name the tombstoned relay").isNotEqualTo(d);
        }
    }

    /**
     * nexus-t7m8e leg (a): structural invariant — every edge's endpoints must
     * appear in the returned node set. A dangling reference used to mean "an
     * edge naming a tumbler that was never registered at all" (the other half
     * of the same defect class the tombstone-relay test covers), reachable
     * via the {@code import}/ETL family or legacy pre-9ssih data even after
     * {@code upsertLink} started rejecting it at write time.
     *
     * <p><strong>nexus-tk070.p1 (RDR-194 § D2) narrows what this test can even
     * seed.</strong> {@code fk_catalog_links_from_document}/{@code
     * _to_document} now make "an edge naming a tumbler with no
     * catalog_documents row at all" physically impossible in Postgres,
     * through EVERY write path (upsertLink, import*, raw SQL) — there is no
     * longer a way to construct that state to test against. What survives is
     * the TOMBSTONED case (D2: a row exists, so the FK is satisfied, but it
     * is not LIVE), which this test now seeds instead — {@link #graphBFS}'s
     * {@code deleted_at IS NULL} INNER JOIN on both endpoints (leg a/b,
     * comment at the join site) excludes a tombstoned-endpoint edge exactly
     * the same way it excluded the now-unreachable fully-missing case, so
     * the structural invariant below is still exercised against a real state.
     */
    @Test
    void graphBFS_everyEdgeEndpoint_appearsInNodes() {
        String tenant = "bfs-dangle-" + System.nanoTime();
        String live = "dg.live";
        String tombstoned = "dg.ghost";
        repo.upsertDocument(tenant, Map.of("tumbler", live,
            "title", "Live", "content_type", "paper", "corpus", "knowledge"));
        repo.upsertDocument(tenant, Map.of("tumbler", tombstoned,
            "title", "Will be tombstoned", "content_type", "paper", "corpus", "knowledge"));
        repo.upsertLink(tenant, Map.of("from_tumbler", live, "to_tumbler", tombstoned,
            "link_type", "cites", "created_by", "test"));
        // Tombstone AFTER the write — the FK only needs the row to exist at
        // write time; soft delete does not cascade (D2), so the link survives
        // pointing at a now-tombstoned document.
        assertThat(repo.deleteDocument(tenant, tombstoned)).isEqualTo(1);

        var result = repo.graphBFS(tenant, List.of(live), List.of("cites"), "out", 1);
        @SuppressWarnings("unchecked")
        var nodes = (List<Map<String, Object>>) result.get("nodes");
        @SuppressWarnings("unchecked")
        var edges = (List<Map<String, Object>>) result.get("edges");
        var nodeTumblers = new java.util.HashSet<>(tumblersOf(nodes));

        for (var e : edges) {
            assertThat(nodeTumblers)
                .as("edge from_tumbler %s must appear in nodes", e.get("from_tumbler"))
                .contains((String) e.get("from_tumbler"));
            assertThat(nodeTumblers)
                .as("edge to_tumbler %s must appear in nodes", e.get("to_tumbler"))
                .contains((String) e.get("to_tumbler"));
        }
        assertThat(edges)
            .as("the dangling reference must not appear as an edge at all")
            .noneMatch(e -> "dg.ghost".equals(e.get("to_tumbler")));
    }

    /**
     * nexus-t7m8e leg (c): the ported 500-node cap. A 501-node star (1 seed +
     * 500 direct children) truncates to EXACTLY 500 nodes, and the surviving
     * set is IDENTICAL across two independent calls (deterministic ordering
     * by (min_depth, tumbler), not database/HashSet iteration order).
     */
    @Test
    void graphBFS_501NodeFixture_truncatesToExactly500Deterministically() {
        String tenant = "bfs-cap-" + System.nanoTime();
        String seed = "cap.seed";
        repo.upsertDocument(tenant, Map.of("tumbler", seed,
            "title", "Cap Seed", "content_type", "paper", "corpus", "knowledge"));
        for (int i = 0; i < 500; i++) {
            String child = String.format("cap.child.%04d", i);
            repo.upsertDocument(tenant, Map.of("tumbler", child,
                "title", "Cap Child " + i, "content_type", "paper", "corpus", "knowledge"));
            repo.upsertLink(tenant, Map.of("from_tumbler", seed, "to_tumbler", child,
                "link_type", "cites", "created_by", "test"));
        }

        var run1 = repo.graphBFS(tenant, List.of(seed), List.of("cites"), "out", 1);
        var run2 = repo.graphBFS(tenant, List.of(seed), List.of("cites"), "out", 1);
        @SuppressWarnings("unchecked")
        var nodes1 = (List<Map<String, Object>>) run1.get("nodes");
        @SuppressWarnings("unchecked")
        var nodes2 = (List<Map<String, Object>>) run2.get("nodes");

        assertThat(nodes1).as("501 reachable nodes truncate to exactly 500").hasSize(500);
        assertThat(tumblersOf(nodes1))
            .as("truncation is deterministic across repeated calls")
            .containsExactlyInAnyOrderElementsOf(tumblersOf(nodes2));
    }

    /**
     * nexus-t7m8e leg (c): depth cap applies BEFORE the node limit. A fixture
     * with few depth-1 nodes but many depth-2 nodes (total > 500) must keep
     * EVERY depth-1 node, truncating only the depth-2 layer.
     */
    @Test
    void graphBFS_depth1Nodes_allSurvive_whenDepth2Truncated() {
        String tenant = "bfs-cap-d2-" + System.nanoTime();
        String seed = "cap2.seed";
        repo.upsertDocument(tenant, Map.of("tumbler", seed,
            "title", "Cap2 Seed", "content_type", "paper", "corpus", "knowledge"));
        List<String> depth1 = new java.util.ArrayList<>();
        for (int i = 0; i < 5; i++) {
            String t = "cap2.d1." + i;
            depth1.add(t);
            repo.upsertDocument(tenant, Map.of("tumbler", t,
                "title", "Cap2 D1 " + i, "content_type", "paper", "corpus", "knowledge"));
            repo.upsertLink(tenant, Map.of("from_tumbler", seed, "to_tumbler", t,
                "link_type", "cites", "created_by", "test"));
        }
        // 5 * 100 = 500 depth-2 nodes; total reachable = 1 + 5 + 500 = 506 > 500.
        for (String d1 : depth1) {
            for (int j = 0; j < 100; j++) {
                String t = d1 + ".d2." + j;
                repo.upsertDocument(tenant, Map.of("tumbler", t,
                    "title", "Cap2 D2", "content_type", "paper", "corpus", "knowledge"));
                repo.upsertLink(tenant, Map.of("from_tumbler", d1, "to_tumbler", t,
                    "link_type", "cites", "created_by", "test"));
            }
        }

        var result = repo.graphBFS(tenant, List.of(seed), List.of("cites"), "out", 2);
        @SuppressWarnings("unchecked")
        var nodes = (List<Map<String, Object>>) result.get("nodes");
        var nodeTumblers = tumblersOf(nodes);

        assertThat(nodeTumblers).as("total reachable set truncates to the 500 cap").hasSize(500);
        assertThat(nodeTumblers)
            .as("every depth-1 node survives truncation — only depth-2 is truncated")
            .containsAll(depth1);
    }

    /**
     * nexus-t7m8e leg (c): the graph_node_limit warning fires when the
     * reachable set hits the cap.
     */
    @Test
    void graphBFS_nodeCapWarning_fires() {
        String tenant = "bfs-cap-warn-" + System.nanoTime();
        String seed = "capw.seed";
        repo.upsertDocument(tenant, Map.of("tumbler", seed,
            "title", "Warn Seed", "content_type", "paper", "corpus", "knowledge"));
        for (int i = 0; i < 500; i++) {
            String child = String.format("capw.child.%04d", i);
            repo.upsertDocument(tenant, Map.of("tumbler", child,
                "title", "Warn Child " + i, "content_type", "paper", "corpus", "knowledge"));
            repo.upsertLink(tenant, Map.of("from_tumbler", seed, "to_tumbler", child,
                "link_type", "cites", "created_by", "test"));
        }

        ch.qos.logback.classic.Logger root =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(
                org.slf4j.Logger.ROOT_LOGGER_NAME);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            repo.graphBFS(tenant, List.of(seed), List.of("cites"), "out", 1);
            var warnings = logs.list.stream()
                .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
                .filter(m -> m.startsWith("event=graph_node_limit"))
                .toList();
            assertThat(warnings).as("graph_node_limit warning must fire at the cap threshold").hasSize(1);
            assertThat(warnings.getFirst()).contains("tenant=" + tenant);
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // MANIFEST
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(50)
    void manifest_writeAndGet() {
        // nexus-7nrvr: real collection — ghost-ness was incidental (write/get
        // round-trip behaviour is the point).
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.1", "title", "Manifest Doc",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__mfst1__v1"));

        var rows = List.of(
            Map.<String, Object>of("position", 0, "chash", ch("aaaa"), "chunk_index", 0,
                "line_start", 1, "line_end", 10, "char_start", 0, "char_end", 100),
            Map.<String, Object>of("position", 1, "chash", ch("bbbb"), "chunk_index", 1,
                "line_start", 11, "line_end", 20, "char_start", 100, "char_end", 200)
        );
        writeManifestSeeded(TENANT_A, "mfst.1", "knowledge__mfst1__v1", rows);

        var got = repo.getManifest(TENANT_A, "mfst.1");
        assertThat(got).hasSize(2);
        assertThat(got.get(0).get("chash")).isEqualTo(ch("aaaa"));
        assertThat(got.get(1).get("chash")).isEqualTo(ch("bbbb"));
        // nexus-kzso5: each row carries its own stamped collection (RDR-191
        // caller-supplied truth), additive wire field.
        assertThat(got.get(0).get("collection")).isEqualTo("knowledge__mfst1__v1");
        assertThat(got.get(1).get("collection")).isEqualTo("knowledge__mfst1__v1");
    }

    @Test @Order(51)
    void manifest_writeIsAtomic_replacesExisting() {
        // nexus-7nrvr: a real physical_collection makes this a genuine,
        // resolvable write (ghost-ness here was incidental — the test is
        // about writeManifest's REPLACE atomicity, not about the ghost/
        // no-collection path).
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.2", "title", "Replace Doc",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__mfst2__v1"));
        // Write initial
        writeManifestSeeded(TENANT_A, "mfst.2", "knowledge__mfst2__v1", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("old"), "chunk_index", 0)
        ));
        // Replace with new set
        writeManifestSeeded(TENANT_A, "mfst.2", "knowledge__mfst2__v1", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("new0"), "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", ch("new1"), "chunk_index", 1)
        ));
        var got = repo.getManifest(TENANT_A, "mfst.2");
        assertThat(got).hasSize(2);
        assertThat(got.stream().map(r -> (String) r.get("chash")).toList())
            .containsExactlyInAnyOrder(ch("new0"), ch("new1"));
    }

    @Test @Order(52)
    void manifest_purge_removesAll() {
        // nexus-7nrvr: real collection — ghost-ness was incidental (purge
        // behaviour is the point, not the ghost path).
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.3", "title", "Purge Doc",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__mfst3__v1"));
        writeManifestSeeded(TENANT_A, "mfst.3", "knowledge__mfst3__v1", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("purge"), "chunk_index", 0)
        ));
        assertThat(repo.getManifest(TENANT_A, "mfst.3")).hasSize(1);
        int deleted = repo.purgeManifest(TENANT_A, "mfst.3");
        assertThat(deleted).isEqualTo(1);
        assertThat(repo.getManifest(TENANT_A, "mfst.3")).isEmpty();
    }

    @Test @Order(52)
    void manifest_purge_zeroesChunkCountInSameTransaction() {
        // nexus-b6enc F5: purgeManifest used to delete the manifest rows but
        // leave documents.chunk_count stale — a ghost count with no rows
        // behind it. The zero must land in the SAME transaction as the purge.
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.purge2", "title", "Purge Count Doc",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 2));
        writeManifestSeeded(TENANT_A, "mfst.purge2", "knowledge__mfst-purge2__v1", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("pg0"), "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", ch("pg1"), "chunk_index", 1)
        ));
        repo.purgeManifest(TENANT_A, "mfst.purge2");
        var doc = repo.getDocument(TENANT_A, "mfst.purge2");
        assertThat(((Number) doc.get("chunk_count")).intValue())
            .as("purgeManifest must zero chunk_count with the rows")
            .isZero();
    }

    @Test @Order(52)
    void manifest_write_foldsChunkCountLikeWriteManifestMany() {
        // nexus-b6enc F5: the single-doc REPLACE must fold chunk_count the
        // same way writeManifestMany / resyncChunkCount do — a stale count
        // after a single-doc rewrite is the same ghost class as the purge.
        //
        // nexus-9kj5j DE-VACUATION: this test previously registered
        // "mfst.wcnt" with NO physical_collection (a ghost) and asserted only
        // chunk_count == rows.size() — which PASSED vacuously even while
        // insertManifestChunkRows' old coll==null SKIP guard left the
        // manifest completely EMPTY, because chunk_count was folded from the
        // caller's rows.size() rather than a re-derived COUNT(*). That was
        // defect 1 (nexus-9kj5j) itself, undetected by its own regression
        // test. Fixed: writeManifestRows folds chunk_count via
        // manifestRowCount() (an actual COUNT(*)), so a desync can no longer
        // hide. RDR-191 (Hal ruling 2026-08-12) removed the ghost/ghost-not
        // distinction entirely — writeManifest now REQUIRES an explicit
        // caller-supplied collection on every call, stamped verbatim
        // regardless of the document's own physical_collection.
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.wcnt", "title", "Write Count Doc",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));
        writeManifestSeeded(TENANT_A, "mfst.wcnt", "knowledge__mfst-wcnt__voyage-context-3__v1", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("wc0"), "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", ch("wc1"), "chunk_index", 1),
            Map.<String, Object>of("position", 2, "chash", ch("wc2"), "chunk_index", 2)
        ));
        var doc = repo.getDocument(TENANT_A, "mfst.wcnt");
        assertThat(((Number) doc.get("chunk_count")).intValue())
            .as("writeManifest must fold chunk_count = manifestRowCount(), not rows.size()")
            .isEqualTo(3);
        assertThat(repo.getManifest(TENANT_A, "mfst.wcnt"))
            .as("nexus-9kj5j: the fold must reflect a REAL, non-empty manifest — "
                + "the exact assertion the original vacuous version omitted")
            .hasSize(3);
        // And the REPLACE shrink folds too.
        writeManifestSeeded(TENANT_A, "mfst.wcnt", "knowledge__mfst-wcnt__voyage-context-3__v1", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("wc9"), "chunk_index", 0)
        ));
        assertThat(((Number) repo.getDocument(TENANT_A, "mfst.wcnt").get("chunk_count")).intValue())
            .isEqualTo(1);
        assertThat(repo.getManifest(TENANT_A, "mfst.wcnt")).hasSize(1);
    }

    @Test @Order(53)
    void manifest_chashesForCollection() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.4", "title", "Chash For Collection",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__chash_test"));
        writeManifestSeeded(TENANT_A, "mfst.4", "knowledge__chash_test", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("cfccc0"), "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", ch("cfccc1"), "chunk_index", 1)
        ));
        Set<String> chashes = repo.chashesForCollection(TENANT_A, "knowledge__chash_test");
        assertThat(chashes).containsExactlyInAnyOrder(ch("cfccc0"), ch("cfccc1"));
    }

    @Test @Order(54)
    void manifest_resyncChunkCount() {
        // nexus-7nrvr: real collection — ghost-ness was incidental (resync
        // repair behaviour is the point).
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.5", "title", "Resync Doc",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0,
            "physical_collection", "knowledge__mfst5__v1"));
        writeManifestSeeded(TENANT_A, "mfst.5", "knowledge__mfst5__v1", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("rsync0"), "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", ch("rsync1"), "chunk_index", 1),
            Map.<String, Object>of("position", 2, "chash", ch("rsync2"), "chunk_index", 2)
        ));
        // De-sync the count deliberately (writeManifest itself now folds it —
        // nexus-b6enc F5 — so force a wrong value to prove resync repairs).
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.5", "title", "Resync Doc",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 99,
            "physical_collection", "knowledge__mfst5__v1"));
        repo.resyncChunkCount(TENANT_A, "mfst.5");
        var doc = repo.getDocument(TENANT_A, "mfst.5");
        assertThat(doc.get("chunk_count")).isEqualTo(3);
    }

    // ── nexus-7lm3q: batch manifest/resolve endpoints ────────────────────────

    @Test @Order(55)
    void manifest_getManifestMany_batchFetchesAllDocs() {
        // nexus-7nrvr: real collections — ghost-ness was incidental (batch
        // fetch behaviour is the point).
        // Seed two docs each with two chunks
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "gmm.1", "title", "GMM Doc1",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__gmm1__v1"));
        writeManifestSeeded(TENANT_A, "gmm.1", "knowledge__gmm1__v1", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("gmm1aa"), "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", ch("gmm1bb"), "chunk_index", 1)
        ));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "gmm.2", "title", "GMM Doc2",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__gmm2__v1"));
        writeManifestSeeded(TENANT_A, "gmm.2", "knowledge__gmm2__v1", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("gmm2cc"), "chunk_index", 0)
        ));

        var result = repo.getManifestMany(TENANT_A, List.of("gmm.1", "gmm.2", "gmm.nonexistent"));

        // Two docs found, one absent (not keyed to empty list)
        assertThat(result).containsOnlyKeys("gmm.1", "gmm.2");
        assertThat(result.get("gmm.1")).hasSize(2);
        assertThat(result.get("gmm.2")).hasSize(1);
        // Ordered by position within each doc
        assertThat(result.get("gmm.1").get(0).get("chash")).isEqualTo(ch("gmm1aa"));
        assertThat(result.get("gmm.1").get(1).get("chash")).isEqualTo(ch("gmm1bb"));
        assertThat(result.get("gmm.2").get(0).get("chash")).isEqualTo(ch("gmm2cc"));
        // nexus-kzso5: batch rows carry their own stamped collection too.
        assertThat(result.get("gmm.1").get(0).get("collection")).isEqualTo("knowledge__gmm1__v1");
        assertThat(result.get("gmm.2").get(0).get("collection")).isEqualTo("knowledge__gmm2__v1");
    }

    @Test @Order(55)
    void manifest_getManifest_rowCollection_independentOfDocPhysicalCollection() {
        // nexus-kzso5: prove the row's collection is the CALLER-SUPPLIED
        // write-time value, decoupled from the doc's own physical_collection
        // -- the exact RDR-191 contract this wire field exposes.
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.kzso5", "title", "Kzso5 Doc",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__mfst-kzso5-doc__v1"));
        writeManifestSeeded(TENANT_A, "mfst.kzso5", "knowledge__mfst-kzso5-explicit__v1", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("kzso5row"), "chunk_index", 0)
        ));
        var got = repo.getManifest(TENANT_A, "mfst.kzso5");
        assertThat(got).hasSize(1);
        assertThat(got.get(0).get("collection"))
            .as("row collection must be the write-time value, not the doc's physical_collection")
            .isEqualTo("knowledge__mfst-kzso5-explicit__v1");
    }

    @Test @Order(56)
    void manifest_getManifestMany_emptyInput_returnsEmptyMap() {
        var result = repo.getManifestMany(TENANT_A, List.of());
        assertThat(result).isEmpty();
    }

    @Test @Order(56)
    void manifest_getManifestMany_tenantIsolation() {
        // nexus-7lm3q review (CR High-1): getManifestMany routes through
        // withTenant + RLS just like resolveMany; assert a TENANT_B manifest
        // never leaks into a TENANT_A batch query (mirrors
        // resolveMany_tenantIsolation @Order 59).
        repo.upsertDocument(TENANT_B, Map.of("tumbler", "gmmiso.1", "title", "Tenant B Doc",
            "content_type", "paper", "corpus", "knowledge"));
        writeManifestSeeded(TENANT_B, "gmmiso.1", "knowledge__gmmiso1__v1", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("gmmisob"), "chunk_index", 0)
        ));
        var result = repo.getManifestMany(TENANT_A, List.of("gmmiso.1"));
        assertThat(result).isEmpty();
    }

    @Test @Order(57)
    void resolveMany_batchFetchesDocuments() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "rmany.1", "title", "Resolve Many 1",
            "content_type", "code", "corpus", "code",
            "file_path", "/src/nexus/search_engine.py"));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "rmany.2", "title", "Resolve Many 2",
            "content_type", "paper", "corpus", "knowledge",
            "file_path", "/papers/test.pdf"));

        var result = repo.resolveMany(TENANT_A, List.of("rmany.1", "rmany.2", "rmany.absent"));

        assertThat(result).containsOnlyKeys("rmany.1", "rmany.2");
        assertThat(result.get("rmany.1").get("file_path")).isEqualTo("/src/nexus/search_engine.py");
        assertThat(result.get("rmany.1").get("content_type")).isEqualTo("code");
        assertThat(result.get("rmany.2").get("file_path")).isEqualTo("/papers/test.pdf");
    }

    @Test @Order(58)
    void resolveMany_emptyInput_returnsEmptyMap() {
        var result = repo.resolveMany(TENANT_A, List.of());
        assertThat(result).isEmpty();
    }

    @Test @Order(59)
    void resolveMany_tenantIsolation() {
        // A doc in TENANT_B must not appear when querying TENANT_A
        repo.upsertDocument(TENANT_B, Map.of("tumbler", "rmiso.1", "title", "Tenant B Doc",
            "content_type", "paper", "corpus", "knowledge"));
        var result = repo.resolveMany(TENANT_A, List.of("rmiso.1"));
        assertThat(result).isEmpty();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // COLLECTIONS
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(60)
    void collection_upsertAndGet() {
        repo.upsertCollection(TENANT_A, Map.of(
            "name", "code__nexus__voyage-code-3__v1",
            "content_type", "code",
            "owner_id", "nexus-1-1",
            "embedding_model", "voyage-code-3",
            "model_version", "v1"
        ));
        var coll = repo.getCollection(TENANT_A, "code__nexus__voyage-code-3__v1");
        assertThat(coll).isNotNull();
        assertThat(coll.get("content_type")).isEqualTo("code");
        assertThat(coll.get("embedding_model")).isEqualTo("voyage-code-3");
        // nexus-cefa1.2: legacy_grandfathered is boolean now (catalog-031-3-collections-
        // legacy-bool); the DEFAULT false round-trips as a real Boolean, not 0.
        assertThat(coll.get("legacy_grandfathered")).isEqualTo(Boolean.FALSE);
    }

    /**
     * nexus-cefa1.2: legacy_grandfathered=true (a real JSON boolean, matching the
     * only shape Python clients send — nexus-cecqy) round-trips through
     * catalog_collections.legacy_grandfathered's boolean column exactly.
     */
    @Test @Order(60)
    void collection_legacyGrandfathered_trueRoundTrips() {
        repo.upsertCollection(TENANT_A, Map.of(
            "name", "legacy__nexus__voyage-code-3__v1",
            "content_type", "code",
            "owner_id", "nexus-1-1",
            "embedding_model", "voyage-code-3",
            "model_version", "v1",
            "legacy_grandfathered", true
        ));
        var coll = repo.getCollection(TENANT_A, "legacy__nexus__voyage-code-3__v1");
        assertThat(coll).isNotNull();
        assertThat(coll.get("legacy_grandfathered")).isEqualTo(Boolean.TRUE);
    }

    @Test @Order(61)
    void collection_list() {
        var colls = repo.listCollections(TENANT_A);
        assertThat(colls).isNotEmpty();
    }

    @Test @Order(62)
    void collection_supersede() {
        repo.upsertCollection(TENANT_A, Map.of(
            "name", "code__nexus__voyage-code-3__v0",
            "content_type", "code",
            "owner_id", "nexus-1-1",
            "embedding_model", "voyage-code-3"
        ));
        int updated = repo.supersedeCollection(TENANT_A, "code__nexus__voyage-code-3__v0",
            "code__nexus__voyage-code-3__v1", "2026-06-01T00:00:00Z");
        assertThat(updated).isEqualTo(1);
        var coll = repo.getCollection(TENANT_A, "code__nexus__voyage-code-3__v0");
        assertThat(coll.get("superseded_by")).isEqualTo("code__nexus__voyage-code-3__v1");
    }

    @Test @Order(61)
    void collection_supersede_refusesToChainToADifferentTargetInTheUPDATEitself() {
        // nexus-cecqy review: guard 2 lives in the HANDLER and read-then-wrote as two
        // statements, so two concurrent supersedes of the same name to DIFFERENT targets
        // could both pass. The precondition now rides in supersedeCollection's own WHERE.
        //
        // This pin exists because 1232585d shipped that conjunct with NO test at any layer:
        // both pre-existing callers supersede a LIVE row and return 1, so deleting the
        // conjunct left the suite green. Repo-level is the correct layer here — the SUBJECT
        // is the WHERE clause, not the route.
        String name = "code__chainguard__voyage-code-3__v1";
        repo.upsertCollection(TENANT_A, Map.of(
            "name", name, "content_type", "code",
            "owner_id", "chainguard", "embedding_model", "voyage-code-3"));
        assertThat(repo.supersedeCollection(TENANT_A, name, "code__chainguard__voyage-code-3__v2", ""))
            .as("guard: the first supersession must land, or the rest proves nothing")
            .isEqualTo(1);

        // A DIFFERENT target must match zero rows — this is what the handler reports as 409.
        assertThat(repo.supersedeCollection(TENANT_A, name, "code__chainguard__voyage-code-3__v3", ""))
            .as("chaining to a different target must match no rows")
            .isEqualTo(0);
        assertThat(repo.getCollection(TENANT_A, name).get("superseded_by"))
            .as("the original supersession must survive the refused chain")
            .isEqualTo("code__chainguard__voyage-code-3__v2");

        // The SAME target stays idempotent (the .or() disjunct) — re-asserting a supersession
        // must not start failing.
        assertThat(repo.supersedeCollection(TENANT_A, name, "code__chainguard__voyage-code-3__v2", ""))
            .as("re-asserting the same target is idempotent, not a chain")
            .isEqualTo(1);
    }

    @Test @Order(61)
    void collection_supersede_sameTargetReassert_leavesSupersededAtByteIdentical() {
        // nexus-0svvu (a): the WHERE's .or() disjunct above is deliberately permissive —
        // a same-target re-assertion must MATCH (guard: the paired supersede call the
        // canonical rename issues against its own tombstone must not 409). Matching is
        // not the whole story: a bare SET would re-stamp superseded_at on every match,
        // moving the recorded supersession instant on every retry — exactly what
        // CatalogHandler's guard-2 comment says must never happen. The fix moves the
        // guard into the SET clause (CASE WHEN superseded_by = '' THEN <new> ELSE
        // superseded_at END), so a same-target re-assertion must be a true no-op on the
        // timestamp, not merely idempotent on the pointer. This is the ONLY layer that
        // proves it: the HTTP handler's OWN idempotence branch (guard 2's early return)
        // never calls supersedeCollection a second time at all, so it cannot pin the
        // SET clause — repo-level is the correct layer here, same lesson as the sibling
        // pin above.
        String name = "code__samestamp__voyage-code-3__v1";
        repo.upsertCollection(TENANT_A, Map.of(
            "name", name, "content_type", "code",
            "owner_id", "samestamp", "embedding_model", "voyage-code-3"));
        assertThat(repo.supersedeCollection(TENANT_A, name, "code__samestamp__voyage-code-3__v2", ""))
            .as("guard: the first supersession must land, or the rest proves nothing")
            .isEqualTo(1);
        String stamp = (String) repo.getCollection(TENANT_A, name).get("superseded_at");
        assertThat(stamp).as("guard: the first supersede stamped an instant").isNotBlank();

        assertThat(repo.supersedeCollection(TENANT_A, name, "code__samestamp__voyage-code-3__v2", ""))
            .as("same-target re-assertion still reports the row as marked")
            .isEqualTo(1);
        assertThat(repo.getCollection(TENANT_A, name).get("superseded_at"))
            .as("the SET clause must not move superseded_at on a same-target re-assertion")
            .isEqualTo(stamp);
    }

    @Test @Order(61)
    void collection_supersede_concurrentSameTarget_bothCallersSeeOneSurvivingStamp() throws Exception {
        // nexus-0svvu (a), concurrency form. Two concurrent supersedes of the SAME
        // old_name to the SAME target both call supersedeCollection directly here
        // (bypassing the HTTP handler's guard-2 early return, which is the point — this
        // pin is about the repo's OWN WHERE/SET, not the handler's short-circuit), and
        // the WHERE's same-target disjunct lets BOTH match. Whichever runs second must
        // take the CASE's ELSE branch and leave the instant exactly as the first left it.
        String name = "code__concstamp__voyage-code-3__v1";
        String target = "code__concstamp__voyage-code-3__v2";
        repo.upsertCollection(TENANT_A, Map.of(
            "name", name, "content_type", "code",
            "owner_id", "concstamp", "embedding_model", "voyage-code-3"));

        var pool = java.util.concurrent.Executors.newFixedThreadPool(2);
        try {
            java.util.concurrent.Callable<Integer> task = () -> repo.supersedeCollection(TENANT_A, name, target, "");
            var f1 = pool.submit(task);
            var f2 = pool.submit(task);
            int r1 = f1.get();
            int r2 = f2.get();
            assertThat(r1 + r2).as("both concurrent callers must see the row as marked (matched)")
                .isEqualTo(2);
            var row = repo.getCollection(TENANT_A, name);
            assertThat(row.get("superseded_by")).isEqualTo(target);
            String stampAfterRace = (String) row.get("superseded_at");
            assertThat(stampAfterRace).isNotBlank();

            // Whichever caller's write landed second necessarily saw superseded_by
            // already equal to the target (not '') and had to take the CASE's ELSE
            // branch, or this third, purely sequential, same-target call would move
            // the stamp — proving the loser's write did NOT re-stamp during the race.
            assertThat(repo.supersedeCollection(TENANT_A, name, target, "")).isEqualTo(1);
            assertThat(repo.getCollection(TENANT_A, name).get("superseded_at"))
                .as("the recorded supersession instant must survive both the race and a "
                    + "further same-target re-assertion")
                .isEqualTo(stampAfterRace);
        } finally {
            pool.shutdownNow();
        }
    }

    @Test @Order(62)
    void collection_upsert_revivesASupersededRow() {
        // nexus-cecqy: a rename now RETIRES the old name as a tombstone instead of
        // deleting it, so re-creating a collection under that name lands on a row that
        // is still marked superseded. Superseded rows are excluded from
        // collectionForTuple, so without this the revived collection would be
        // unreachable as a write target — and silently so, since
        // `nx catalog doctor --collections-drift` deliberately permits a superseded row
        // to have no T3 collection. /collections/upsert is the caller asserting the
        // collection is current.
        String name = "code__revive__voyage-code-3__v1";
        repo.upsertCollection(TENANT_A, Map.of(
            "name", name, "content_type", "code",
            "owner_id", "revive", "embedding_model", "voyage-code-3"));
        assertThat(repo.supersedeCollection(TENANT_A, name, "code__revive__voyage-code-3__v2", ""))
            .isEqualTo(1);
        // NON-VACUITY: the row really is tombstoned before the re-registration.
        assertThat(repo.getCollection(TENANT_A, name).get("superseded_by"))
            .as("guard: the row must be superseded before the revive is meaningful")
            .isEqualTo("code__revive__voyage-code-3__v2");

        repo.upsertCollection(TENANT_A, Map.of(
            "name", name, "content_type", "code",
            "owner_id", "revive", "embedding_model", "voyage-code-3"));

        var revived = repo.getCollection(TENANT_A, name);
        assertThat(revived.get("superseded_by")).as("tombstone pointer cleared").isEqualTo("");
        // collRow renders every null column as "" (nne), and a non-null timestamptz would
        // render as an ISO instant — so "" here means the column is genuinely NULL.
        assertThat(revived.get("superseded_at")).as("tombstone timestamp cleared").isEqualTo("");
        // And it is once again resolvable as the live collection for its tuple.
        var forTuple = repo.collectionForTuple(TENANT_A, "code", "revive", "voyage-code-3");
        assertThat(forTuple).as("a revived collection must be reachable as a write target")
            .isNotNull();
        assertThat(forTuple.get("name")).isEqualTo(name);
    }

    @Test @Order(63)
    void importCollection_overwritesStubRow() {
        // A stub row (all three discriminator columns empty) must be fully upgraded
        // by importCollection. Stubs are created by PgVectorRepository.upsertChunks
        // auto-registration and by fk-002-0-backfill-stubs (RDR-156 P0.2).
        String name = "code__nexus__voyage-code-3__v2";
        // Seed a stub via upsertCollection with no metadata — this simulates the
        // auto-registration path (content_type/owner_id/embedding_model all default to '').
        // Use a direct SQL stub to guarantee the three discriminators are all empty:
        repo.importCollection(TENANT_A, Map.of(
            "name", name,
            "content_type", "",
            "owner_id", "",
            "embedding_model", "",
            "model_version", ""
        ));
        var before = repo.getCollection(TENANT_A, name);
        assertThat(before).isNotNull();
        assertThat(before.get("content_type")).as("stub has empty content_type").isEqualTo("");

        // Now call importCollection with full metadata — the DO UPDATE WHERE-stub must fire.
        repo.importCollection(TENANT_A, Map.of(
            "name", name,
            "content_type", "code",
            "owner_id", "nexus-1-1",
            "embedding_model", "voyage-code-3",
            "model_version", "v2"
        ));
        var after = repo.getCollection(TENANT_A, name);
        assertThat(after.get("content_type")).as("importCollection must upgrade stub content_type").isEqualTo("code");
        assertThat(after.get("owner_id")).as("importCollection must upgrade stub owner_id").isEqualTo("nexus-1-1");
        assertThat(after.get("embedding_model")).as("importCollection must upgrade stub embedding_model").isEqualTo("voyage-code-3");
        assertThat(after.get("model_version")).as("importCollection must upgrade stub model_version").isEqualTo("v2");
    }

    @Test @Order(64)
    void importCollection_doesNotOverwriteLiveRow() {
        // A live row (at least one discriminator non-empty) must NOT be overwritten
        // by importCollection. The DO UPDATE WHERE-stub predicate must not fire.
        String name = "code__nexus__voyage-code-3__v3";
        // Register a live row with fully populated metadata via upsertCollection.
        repo.upsertCollection(TENANT_A, Map.of(
            "name", name,
            "content_type", "code",
            "owner_id", "live-owner",
            "embedding_model", "voyage-code-3",
            "model_version", "v3"
        ));
        var before = repo.getCollection(TENANT_A, name);
        assertThat(before.get("owner_id")).as("live row owner_id before import").isEqualTo("live-owner");

        // Call importCollection with DIFFERENT metadata — must NOT overwrite the live row.
        repo.importCollection(TENANT_A, Map.of(
            "name", name,
            "content_type", "docs",
            "owner_id", "different-owner",
            "embedding_model", "voyage-context-3",
            "model_version", "v3"
        ));
        var after = repo.getCollection(TENANT_A, name);
        assertThat(after.get("content_type")).as("importCollection must not overwrite live content_type").isEqualTo("code");
        assertThat(after.get("owner_id")).as("importCollection must not overwrite live owner_id").isEqualTo("live-owner");
        assertThat(after.get("embedding_model")).as("importCollection must not overwrite live embedding_model").isEqualTo("voyage-code-3");
    }

    @Test @Order(65)
    void collection_rename_cascadesToDocuments() {
        repo.upsertCollection(TENANT_A, Map.of(
            "name", "knowledge__old__v1",
            "content_type", "knowledge",
            "owner_id", "nexus-1-1"
        ));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "rn.1", "title", "Rename Test",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__old__v1"));
        var counts = repo.renameCollection(TENANT_A, "knowledge__old__v1", "knowledge__new__v1");
        assertThat(counts.get("catalog_documents")).as("1 document re-homed").isEqualTo(1);
        assertThat(counts.get("catalog_collections_inserted")).as("registry Y inserted").isEqualTo(1);
        assertThat(counts.get("catalog_collections_superseded"))
            .as("registry X retired as a superseded tombstone (nexus-cecqy)").isEqualTo(1);
        var doc = repo.getDocument(TENANT_A, "rn.1");
        assertThat(doc.get("physical_collection")).isEqualTo("knowledge__new__v1");
    }

    @Test @Order(66)
    void collection_rename_crossModel_targetPreRegistered_repointsDocsNoCollision() {
        // RDR-162 cross-model migrate is COPY-not-move: the bge-768 TARGET is ALREADY
        // registered in catalog_collections (the vector upsert pre-registers it so its
        // chunks' FK is satisfied). Renaming the SOURCE registry row into that name
        // would collide on the (tenant_id, name) PK -> 500 (the bug the cross-model
        // ref-remap hit). The rename must instead repoint the catalog documents only,
        // leaving the (already-correct) target registry row untouched.
        String src = "knowledge__xmrn__minilm-l6-v2-384__v1";
        String tgt = "knowledge__xmrn__bge-base-en-v15-768__v1";
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "xmrn.1", "title", "Cross-model Rename",
            "content_type", "knowledge", "corpus", "knowledge",
            "physical_collection", src));
        // The target is pre-registered (simulating the vector upsert's auto-registration).
        repo.upsertCollection(TENANT_A, Map.of(
            "name", tgt, "content_type", "knowledge", "owner_id", "nexus-1-1",
            "embedding_model", "bge-base-en-v15-768", "model_version", "v1"));

        // Pre-RDR-162 this threw a 500 (PK collision on the registry rename). The cross-model
        // COPY branch (target exists) repoints catalog_documents only and returns just that key.
        var counts = repo.renameCollection(TENANT_A, src, tgt);
        // nexus-x6kdz: the branch now ALSO re-homes the manifest join key.
        assertThat(counts).as("cross-model branch re-homes docs and manifests")
            .containsOnlyKeys("catalog_documents", "catalog_document_chunks");
        assertThat(counts.get("catalog_documents")).isEqualTo(1);
        assertThat(repo.getDocument(TENANT_A, "xmrn.1").get("physical_collection")).isEqualTo(tgt);
        // The pre-registered target row is intact (not collided, not duplicated).
        assertThat(repo.getCollection(TENANT_A, tgt)).isNotNull();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // STATS
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(70)
    void stats_returnsAllCounters() {
        var stats = repo.stats(TENANT_A);
        assertThat((Long) stats.get("doc_count")).isGreaterThan(0);
        assertThat((Long) stats.get("link_count")).isGreaterThan(0);
        assertThat((Long) stats.get("owner_count")).isGreaterThan(0);
        assertThat((Long) stats.get("collection_count")).isGreaterThan(0);
        assertThat(stats.get("links_by_type")).isNotNull();
    }

    /**
     * nexus-se9r3 closing assertion: stats().doc_count must equal
     * countDocuments() — the two surfaces disagreeing (doc_count raw,
     * countDocuments tombstone-filtered) was the bead's own repro.
     */
    @Test
    void stats_docCount_matchesCountDocuments() {
        String tenant = "stats-parity-" + System.nanoTime();
        repo.upsertDocument(tenant, mapOf("tumbler", "sp.1", "title", "Live", "content_type", "paper"));
        repo.upsertDocument(tenant, mapOf("tumbler", "sp.2", "title", "Dead", "content_type", "paper"));
        assertThat(repo.deleteDocument(tenant, "sp.2")).isEqualTo(1);

        var stats = repo.stats(tenant);
        assertThat((Long) stats.get("doc_count"))
            .as("stats().doc_count must equal countDocuments() post-tombstone")
            .isEqualTo(repo.countDocuments(tenant))
            .isEqualTo(1L);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // ETL — fidelity-preserving + idempotent
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(80)
    void etl_document_fidelityAndIdempotentReimport() {
        String etlTenant = "etl-cat-tenant";
        String tumbler   = "etl.1";

        // First import
        repo.importDocument(etlTenant, Map.of(
            "tumbler", tumbler,
            "title", "ETL Document",
            "author", "ETL Author",
            "content_type", "paper",
            "corpus", "knowledge",
            "source_mtime", 1000.0,
            "indexed_at", "2025-01-01T00:00:00Z"
        ));
        var doc = repo.getDocument(etlTenant, tumbler);
        assertThat(doc).isNotNull();
        assertThat(doc.get("title")).isEqualTo("ETL Document");

        // Second import (idempotent): same tumbler, higher source_mtime
        repo.importDocument(etlTenant, Map.of(
            "tumbler", tumbler,
            "title", "ETL Document Updated",
            "author", "ETL Author",
            "content_type", "paper",
            "corpus", "knowledge",
            "source_mtime", 2000.0,
            "indexed_at", "2025-02-01T00:00:00Z"
        ));
        var doc2 = repo.getDocument(etlTenant, tumbler);
        // GREATEST(1000.0, 2000.0) = 2000.0; title updated too (EXCLUDED)
        assertThat(doc2.get("title")).isEqualTo("ETL Document Updated");
        // source_mtime should be 2000.0 (GREATEST)
        Double mtime = (Double) doc2.get("source_mtime");
        assertThat(mtime).isEqualTo(2000.0);
    }

    @Test @Order(81)
    void etl_document_greatestSourceMtime_neverDowngrades() {
        String etlTenant = "etl-cat-mtime-tenant";
        repo.importDocument(etlTenant, Map.of(
            "tumbler", "mtime.1", "title", "High Mtime",
            "content_type", "paper", "corpus", "knowledge",
            "source_mtime", 5000.0
        ));
        // Re-import with lower mtime: should stay at 5000 (GREATEST)
        repo.importDocument(etlTenant, Map.of(
            "tumbler", "mtime.1", "title", "Low Mtime Attempt",
            "content_type", "paper", "corpus", "knowledge",
            "source_mtime", 100.0
        ));
        var doc = repo.getDocument(etlTenant, "mtime.1");
        Double mtime = (Double) doc.get("source_mtime");
        assertThat(mtime).isEqualTo(5000.0);
    }

    @Test @Order(82)
    void etl_link_idempotentOnConflictDoNothing() {
        String etlTenant = "etl-link-tenant";
        repo.importDocument(etlTenant, Map.of("tumbler", "elA", "title", "ETL Link A",
            "content_type", "paper", "corpus", "knowledge"));
        repo.importDocument(etlTenant, Map.of("tumbler", "elB", "title", "ETL Link B",
            "content_type", "paper", "corpus", "knowledge"));

        var lnk = Map.<String, Object>of(
            "from_tumbler", "elA", "to_tumbler", "elB",
            "link_type", "cites", "created_by", "user", "created_at", "2024-01-01T00:00:00Z"
        );
        repo.importLink(etlTenant, lnk);
        repo.importLink(etlTenant, lnk); // second import: no error, no duplicate
        var links = repo.linksFrom(etlTenant, "elA", (java.util.List<String>) null);
        assertThat(links).hasSize(1); // exactly one, not two
    }

    @Test @Order(83)
    void etl_chunk_convergentReimport_updatesChangedChash() {
        // nexus-9wz72: importChunk must use DO UPDATE (convergent), not DO NOTHING.
        // Re-importing the same (tenant, doc, position) with a DIFFERENT chash must
        // update the row so the manifest reflects the new content hash.
        String etlTenant = "etl-chunk-conv-tenant";
        String docId     = "conv.1";

        // Seed a parent document (FK target).
        // nexus-7nrvr: real collection — ghost-ness was incidental (convergent
        // re-import behaviour is the point).
        repo.importDocument(etlTenant, Map.of(
            "tumbler", docId, "title", "Chunk Conv Doc",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__etl-chunk-conv__v1"
        ));

        // chash must be the full 64-hex canonical digest (RDR-180: chunks_*/manifest
        // columns are bytea(32) now, CHECK octet_length=32)
        String chashV1 = ch("chashV1");
        String chashV2 = ch("chashV2"); // different

        // Initial chunk import
        importChunkSeeded(etlTenant, docId, "knowledge__etl-chunk-conv__v1", Map.of(
            "position", 0, "chash", chashV1, "chunk_index", 0,
            "line_start", 1, "line_end", 10, "char_start", 0, "char_end", 200
        ));
        var before = repo.getManifest(etlTenant, docId);
        assertThat(before).hasSize(1);
        assertThat(before.get(0).get("chash")).isEqualTo(chashV1);

        // Re-import same (tenant, doc, position) with a DIFFERENT chash — convergence
        importChunkSeeded(etlTenant, docId, "knowledge__etl-chunk-conv__v1", Map.of(
            "position", 0, "chash", chashV2, "chunk_index", 0,
            "line_start", 1, "line_end", 10, "char_start", 0, "char_end", 200
        ));
        var after = repo.getManifest(etlTenant, docId);
        assertThat(after).hasSize(1); // still exactly one row
        assertThat(after.get(0).get("chash")).isEqualTo(chashV2); // updated, not silently dropped
    }

    @Test @Order(84)
    void etl_chunk_idempotentReimport_sameValuesStable() {
        // nexus-9wz72: re-importing with identical values must be a no-op (idempotent).
        // DO UPDATE SET chash=EXCLUDED.chash, ... with the same values must not corrupt.
        String etlTenant = "etl-chunk-idem-tenant";
        String docId     = "idem.1";

        // nexus-7nrvr: real collection — ghost-ness was incidental (idempotent
        // re-import behaviour is the point).
        repo.importDocument(etlTenant, Map.of(
            "tumbler", docId, "title", "Chunk Idem Doc",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__etl-chunk-idem__v1"
        ));

        String chashStable = ch("chashStable"); // full 64-hex canonical digest
        Map<String, Object> chunk = Map.of(
            "position", 0, "chash", chashStable, "chunk_index", 0,
            "line_start", 5, "line_end", 15, "char_start", 10, "char_end", 300
        );
        importChunkSeeded(etlTenant, docId, "knowledge__etl-chunk-idem__v1", chunk);
        importChunkSeeded(etlTenant, docId, "knowledge__etl-chunk-idem__v1", chunk); // exact same values — must be stable

        var manifest = repo.getManifest(etlTenant, docId);
        assertThat(manifest).hasSize(1);
        assertThat(manifest.get(0).get("chash")).isEqualTo(chashStable);
        assertThat(manifest.get(0).get("line_start")).isEqualTo(5);
        assertThat(manifest.get(0).get("line_end")).isEqualTo(15);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // RLS
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(90)
    void rls_isolation_tenantAInvisibleToTenantB() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "rls.1", "title", "RLS Doc",
            "content_type", "paper", "corpus", "knowledge"));

        // TENANT_B cannot see TENANT_A's doc
        var doc = repo.getDocument(TENANT_B, "rls.1");
        assertThat(doc).isNull();

        // TENANT_A can see its own doc
        assertThat(repo.getDocument(TENANT_A, "rls.1")).isNotNull();
    }

    @Test @Order(91)
    void rls_withCheck_crossTenantInsertOnDocumentsRejected() throws Exception {
        // Connect as svc role, set GUC = TENANT_A, try to insert row with tenant_id = TENANT_B
        try (Connection conn = svcDs.getConnection()) {
            conn.setAutoCommit(false);
            conn.createStatement().execute("SET LOCAL nexus.tenant = '" + TENANT_A + "'");
            var ex = assertThrows(PSQLException.class, () ->
                conn.createStatement().execute(
                    "INSERT INTO nexus.catalog_documents " +
                    "(tenant_id, tumbler, title, content_type) " +
                    "VALUES ('" + TENANT_B + "', 'wc.1', 'WithCheck Test', 'paper')")
            );
            assertThat(ex.getMessage()).containsIgnoringCase("violates row-level security");
            conn.rollback();
        }
    }

    @Test @Order(92)
    void failClosed_unsetGuc_yieldsNoRows() throws Exception {
        // Seed a row under TENANT_A
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "fc.1", "title", "FailClosed",
            "content_type", "paper", "corpus", "knowledge"));

        // Connect without setting nexus.tenant GUC
        try (Connection conn = svcDs.getConnection()) {
            conn.setAutoCommit(false);
            // DO NOT set nexus.tenant
            var rs = conn.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.catalog_documents");
            rs.next();
            int count = rs.getInt(1);
            // RLS fail-closed: NULL != any tenant_id => 0 rows
            assertThat(count).isEqualTo(0);
            conn.rollback();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPER
    // ══════════════════════════════════════════════════════════════════════════

    /** Varargs map builder — avoids Map.of() 10-entry limit. */
    @SuppressWarnings("unchecked")
    private static <K, V> Map<K, V> mapOf(Object... kv) {
        if (kv.length % 2 != 0) throw new IllegalArgumentException("odd arg count");
        var m = new LinkedHashMap<K, V>(kv.length);
        for (int i = 0; i < kv.length; i += 2) m.put((K) kv[i], (V) kv[i + 1]);
        return m;
    }

    // ══════════════════════════════════════════════════════════════════════════
    // BATCH ENDPOINTS (nexus-qnp5s)
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(100)
    void ownersByType_returnsOnlyMatchingType() {
        // Seed mixed-type owners under TENANT_A (prefix "bt.*" = batch-type tests)
        repo.upsertOwner(TENANT_A, mapOf(
            "tumbler_prefix", "bt.1",
            "name", "bt-repo-owner",
            "owner_type", "repo",
            "repo_hash", "bthash1",
            "repo_root", "/bt/repo",
            "head_hash", "bthead1"
        ));
        repo.upsertOwner(TENANT_A, mapOf(
            "tumbler_prefix", "bt.2",
            "name", "bt-curator-owner",
            "owner_type", "curator",
            "repo_hash", "bthash2",
            "head_hash", "bthead2"
        ));
        repo.upsertOwner(TENANT_A, mapOf(
            "tumbler_prefix", "bt.3",
            "name", "bt-repo-owner-2",
            "owner_type", "repo",
            "repo_hash", "bthash3",
            "repo_root", "/bt/repo2",
            "head_hash", "bthead3"
        ));

        var repos = repo.ownersByType(TENANT_A, "repo");
        var repoNames = repos.stream().map(o -> (String) o.get("name")).toList();

        // Must include the two repo-type owners we seeded
        assertThat(repoNames).contains("bt-repo-owner", "bt-repo-owner-2");
        // Must NOT include the curator
        assertThat(repoNames).doesNotContain("bt-curator-owner");

        var curators = repo.ownersByType(TENANT_A, "curator");
        var curatorNames = curators.stream().map(o -> (String) o.get("name")).toList();
        assertThat(curatorNames).contains("bt-curator-owner");
        assertThat(curatorNames).doesNotContain("bt-repo-owner", "bt-repo-owner-2");
    }

    @Test @Order(101)
    void ownersByType_unknownType_returnsEmpty() {
        var none = repo.ownersByType(TENANT_A, "nonexistent-type");
        assertThat(none).isEmpty();
    }

    @Test @Order(102)
    void ownerByPrefix_found() {
        // "bt.1" was seeded in Order(100)
        var found = repo.ownerByPrefix(TENANT_A, "bt.1");
        assertThat(found).isNotNull();
        assertThat(found.get("name")).isEqualTo("bt-repo-owner");
        assertThat(found.get("owner_type")).isEqualTo("repo");
        assertThat(found.get("tumbler_prefix")).isEqualTo("bt.1");
    }

    @Test @Order(103)
    void ownerByPrefix_notFound_returnsNull() {
        var found = repo.ownerByPrefix(TENANT_A, "zz.9999");
        assertThat(found).isNull();
    }

    @Test @Order(104)
    void chunkCountsForDocs_batchCorrectness() {
        // Seed 3 documents with known chunk_counts
        repo.upsertDocument(TENANT_A, mapOf(
            "tumbler", "cc.1",
            "title", "ChunkCount Doc 1",
            "content_type", "paper",
            "corpus", "knowledge",
            "chunk_count", 10
        ));
        repo.upsertDocument(TENANT_A, mapOf(
            "tumbler", "cc.2",
            "title", "ChunkCount Doc 2",
            "content_type", "paper",
            "corpus", "knowledge",
            "chunk_count", 25
        ));
        repo.upsertDocument(TENANT_A, mapOf(
            "tumbler", "cc.3",
            "title", "ChunkCount Doc 3 (zero chunks)",
            "content_type", "paper",
            "corpus", "knowledge",
            "chunk_count", 0
            // chunk_count explicitly zero — upsertDocument stores 0 when field absent too (ni default)
        ));

        // Query batch: 2 present with counts + 1 with zero count + 1 absent
        var result = repo.chunkCountsForDocs(TENANT_A,
            List.of("cc.1", "cc.2", "cc.3", "cc.DOES-NOT-EXIST"));

        // cc.1 and cc.2 must be present with exact counts
        assertThat(result).containsKey("cc.1");
        assertThat(result.get("cc.1")).isEqualTo(10);
        assertThat(result).containsKey("cc.2");
        assertThat(result.get("cc.2")).isEqualTo(25);

        // cc.3 has chunk_count=0 (stored; 0 != null so it appears in results)
        assertThat(result).containsKey("cc.3");
        assertThat(result.get("cc.3")).isEqualTo(0);

        // cc.DOES-NOT-EXIST must be absent (not in DB)
        assertThat(result).doesNotContainKey("cc.DOES-NOT-EXIST");

        // Exactly 3 entries (cc.1, cc.2, cc.3)
        assertThat(result).hasSize(3);
    }

    @Test @Order(105)
    void chunkCountsForDocs_emptyInput_returnsEmpty() {
        var result = repo.chunkCountsForDocs(TENANT_A, List.of());
        assertThat(result).isEmpty();
    }

    @Test @Order(106)
    void chunkCountsForDocs_nullInput_returnsEmpty() {
        var result = repo.chunkCountsForDocs(TENANT_A, null);
        assertThat(result).isEmpty();
    }

    @Test @Order(107)
    void linksFromBatch_groupedByFromTumbler() {
        // Seed documents so FK constraints (if any) are satisfied
        repo.upsertDocument(TENANT_A, mapOf(
            "tumbler", "lf.1",
            "title", "Links From Doc 1",
            "content_type", "paper",
            "corpus", "knowledge"
        ));
        repo.upsertDocument(TENANT_A, mapOf(
            "tumbler", "lf.2",
            "title", "Links From Doc 2",
            "content_type", "paper",
            "corpus", "knowledge"
        ));
        repo.upsertDocument(TENANT_A, mapOf(
            "tumbler", "lf.3",
            "title", "Links From Doc 3 (target)",
            "content_type", "paper",
            "corpus", "knowledge"
        ));

        // Seed links: lf.1 → lf.3 (cites), lf.1 → lf.2 (relates), lf.2 → lf.3 (implements)
        repo.upsertLink(TENANT_A, Map.of("from_tumbler", "lf.1", "to_tumbler", "lf.3", "link_type", "cites"));
        repo.upsertLink(TENANT_A, Map.of("from_tumbler", "lf.1", "to_tumbler", "lf.2", "link_type", "relates"));
        repo.upsertLink(TENANT_A, Map.of("from_tumbler", "lf.2", "to_tumbler", "lf.3", "link_type", "implements"));

        // Query: lf.1 (2 outbound links), lf.2 (1 outbound link), lf.3 (0 outbound)
        var result = repo.linksFromBatch(TENANT_A, List.of("lf.1", "lf.2", "lf.3", "lf.ABSENT"));

        // lf.1 should have 2 link entries
        assertThat(result).containsKey("lf.1");
        var lf1Links = result.get("lf.1");
        assertThat(lf1Links).hasSize(2);
        var lf1Types = lf1Links.stream().map(m -> (String) m.get("link_type")).toList();
        assertThat(lf1Types).containsExactlyInAnyOrder("cites", "relates");
        // Each entry must have from_tumbler set correctly
        assertThat(lf1Links).allMatch(m -> "lf.1".equals(m.get("from_tumbler")));

        // lf.2 should have 1 link entry
        assertThat(result).containsKey("lf.2");
        var lf2Links = result.get("lf.2");
        assertThat(lf2Links).hasSize(1);
        assertThat(lf2Links.get(0).get("link_type")).isEqualTo("implements");
        assertThat(lf2Links.get(0).get("from_tumbler")).isEqualTo("lf.2");

        // lf.3 has NO outbound links — must be absent from result (not an empty list)
        assertThat(result).doesNotContainKey("lf.3");

        // lf.ABSENT is not in DB — must be absent
        assertThat(result).doesNotContainKey("lf.ABSENT");
    }

    @Test @Order(108)
    void linksFromBatch_emptyInput_returnsEmpty() {
        var result = repo.linksFromBatch(TENANT_A, List.of());
        assertThat(result).isEmpty();
    }

    @Test @Order(109)
    void linksFromBatch_nullInput_returnsEmpty() {
        var result = repo.linksFromBatch(TENANT_A, null);
        assertThat(result).isEmpty();
    }

    @Test @Order(110)
    void chunkCountsForDocs_tenantIsolation() {
        // Seed a doc under TENANT_B with a distinct chunk_count
        repo.upsertDocument(TENANT_B, mapOf(
            "tumbler", "cc.b1",
            "title", "Tenant B Chunk Doc",
            "content_type", "paper",
            "corpus", "knowledge",
            "chunk_count", 99
        ));

        // Query TENANT_A for TENANT_B's tumbler — must get empty (RLS isolation)
        var resultA = repo.chunkCountsForDocs(TENANT_A, List.of("cc.b1"));
        assertThat(resultA).isEmpty();

        // Query TENANT_B for its own doc — must find it
        var resultB = repo.chunkCountsForDocs(TENANT_B, List.of("cc.b1"));
        assertThat(resultB).containsKey("cc.b1");
        assertThat(resultB.get("cc.b1")).isEqualTo(99);
    }

    @Test @Order(111)
    void linksFromBatch_tenantIsolation() {
        // Seed docs and a link under TENANT_B using "lfb.*" prefix
        repo.upsertDocument(TENANT_B, mapOf(
            "tumbler", "lfb.1",
            "title", "Links From Batch Tenant B Doc 1",
            "content_type", "paper",
            "corpus", "knowledge"
        ));
        repo.upsertDocument(TENANT_B, mapOf(
            "tumbler", "lfb.2",
            "title", "Links From Batch Tenant B Doc 2",
            "content_type", "paper",
            "corpus", "knowledge"
        ));
        repo.upsertLink(TENANT_B, Map.of("from_tumbler", "lfb.1", "to_tumbler", "lfb.2", "link_type", "cites"));

        // TENANT_A must not see TENANT_B's links
        var resultA = repo.linksFromBatch(TENANT_A, List.of("lfb.1"));
        assertThat(resultA).doesNotContainKey("lfb.1");

        // TENANT_B must see its own link
        var resultB = repo.linksFromBatch(TENANT_B, List.of("lfb.1"));
        assertThat(resultB).containsKey("lfb.1");
        assertThat(resultB.get("lfb.1")).hasSize(1);
        assertThat(resultB.get("lfb.1").get(0).get("link_type")).isEqualTo("cites");
    }

    @Test @Order(112)
    void ownersByType_tenantIsolation() {
        // Seed a repo-type owner under TENANT_B using "obt.*" prefix
        repo.upsertOwner(TENANT_B, mapOf(
            "tumbler_prefix", "obt.1",
            "name", "obt-tenant-b-repo",
            "owner_type", "repo",
            "repo_hash", "obthash1",
            "repo_root", "/obt/repo",
            "head_hash", "obthead1"
        ));

        // TENANT_A must not see TENANT_B's owner in its ownersByType result
        var reposA = repo.ownersByType(TENANT_A, "repo");
        var namesA = reposA.stream().map(o -> (String) o.get("name")).toList();
        assertThat(namesA).doesNotContain("obt-tenant-b-repo");

        // TENANT_B must see its own owner
        var reposB = repo.ownersByType(TENANT_B, "repo");
        var namesB = reposB.stream().map(o -> (String) o.get("name")).toList();
        assertThat(namesB).contains("obt-tenant-b-repo");
    }

    @Test @Order(113)
    void ownerByPrefix_tenantIsolation() {
        // Seed an owner under TENANT_B using "opb.*" prefix
        repo.upsertOwner(TENANT_B, mapOf(
            "tumbler_prefix", "opb.1",
            "name", "opb-tenant-b-owner",
            "owner_type", "repo",
            "repo_hash", "opbhash1",
            "repo_root", "/opb/repo",
            "head_hash", "opbhead1"
        ));

        // TENANT_A must not see TENANT_B's owner by prefix
        var foundByA = repo.ownerByPrefix(TENANT_A, "opb.1");
        assertThat(foundByA).isNull();

        // TENANT_B must find its own owner
        var foundByB = repo.ownerByPrefix(TENANT_B, "opb.1");
        assertThat(foundByB).isNotNull();
        assertThat(foundByB.get("name")).isEqualTo("opb-tenant-b-owner");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // COLLECTION HEALTH META (nexus-dsu5z)
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(120)
    void collectionHealthMeta_exactValues() {
        // Seed 3 documents in a dedicated collection with known indexed_at values.
        String chmTenant = "chm-tenant";
        String chmColl   = "chm__test__voyage__v1";
        repo.upsertDocument(chmTenant, mapOf(
            "tumbler", "chm.1", "title", "CHM Doc A",
            "content_type", "knowledge", "physical_collection", chmColl,
            "indexed_at", "2026-01-01T08:00:00"
        ));
        repo.upsertDocument(chmTenant, mapOf(
            "tumbler", "chm.2", "title", "CHM Doc B",
            "content_type", "knowledge", "physical_collection", chmColl,
            "indexed_at", "2026-06-01T12:00:00"
        ));
        repo.upsertDocument(chmTenant, mapOf(
            "tumbler", "chm.3", "title", "CHM Doc C",
            "content_type", "knowledge", "physical_collection", chmColl,
            "indexed_at", "2026-03-15T00:00:00"
        ));

        // Add one link pointing TO chm.2 — makes it a non-orphan.
        repo.upsertLink(chmTenant, Map.of(
            "from_tumbler", "chm.1",
            "to_tumbler",   "chm.2",
            "link_type",    "cites",
            "created_by",   "test"
        ));

        var meta = repo.collectionHealthMeta(chmTenant, chmColl);

        // last_indexed = MAX("2026-01-01", "2026-06-01", "2026-03-15") = "2026-06-01...".
        // nexus-cefa1.2: indexed_at is timestamptz now (catalog-031-1-documents-temporal);
        // the wire value is CatalogRepository.utcIso's micros+offset rendering
        // (INDEXED_AT_FMT, the catalog convention — kept, not the coarser "...Z" shape, so a
        // stamp written by the published client round-trips byte-identical). The seed value
        // above has no microsecond/offset component, so it gains the accepted
        // ".000000+00:00" residual on read (see utcIso's javadoc) rather than echoing verbatim
        // the way the pre-migration TEXT column did.
        assertThat(meta.get("last_indexed")).isEqualTo("2026-06-01T12:00:00.000000+00:00");
        // orphan_count = 2 (chm.1 and chm.3 have no incoming links)
        assertThat(meta.get("orphan_count")).isEqualTo(2L);
    }

    @Test @Order(121)
    void collectionHealthMeta_crossTenantIsolation() {
        // Same physical_collection name used by two tenants — each sees only its own rows.
        String tenantX = "chm-tenant-x";
        String tenantY = "chm-tenant-y";
        String sharedColl = "shared__knowledge__v1";

        // TENANT_X: 2 docs, no links (both orphans); indexed_at = "2026-05-01"
        repo.upsertDocument(tenantX, mapOf(
            "tumbler", "chmx.1", "title", "X Doc 1",
            "content_type", "knowledge", "physical_collection", sharedColl,
            "indexed_at", "2026-05-01T00:00:00"
        ));
        repo.upsertDocument(tenantX, mapOf(
            "tumbler", "chmx.2", "title", "X Doc 2",
            "content_type", "knowledge", "physical_collection", sharedColl,
            "indexed_at", "2026-05-01T00:00:00"
        ));

        // TENANT_Y: 1 doc with a later indexed_at; no incoming link, so it is
        // itself an orphan (orphan_count == 1). RLS keeps it invisible to TENANT_X.
        repo.upsertDocument(tenantY, mapOf(
            "tumbler", "chmy.1", "title", "Y Doc 1",
            "content_type", "knowledge", "physical_collection", sharedColl,
            "indexed_at", "2026-06-07T10:00:00"
        ));

        // TENANT_X must see only its own rows: last_indexed, orphan_count=2
        // (nexus-cefa1.2: micros+offset — see collectionHealthMeta_exactValues comment above.)
        var metaX = repo.collectionHealthMeta(tenantX, sharedColl);
        assertThat(metaX.get("last_indexed")).isEqualTo("2026-05-01T00:00:00.000000+00:00");
        assertThat(metaX.get("orphan_count")).isEqualTo(2L);

        // TENANT_Y must see only its own rows: last_indexed, orphan_count=1
        var metaY = repo.collectionHealthMeta(tenantY, sharedColl);
        assertThat(metaY.get("last_indexed")).isEqualTo("2026-06-07T10:00:00.000000+00:00");
        assertThat(metaY.get("orphan_count")).isEqualTo(1L);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // ANALYTICS QUERIES — PER-ENDPOINT EXACT + RLS TESTS (nexus-xnz0o)
    // ══════════════════════════════════════════════════════════════════════════

    // ── distinctDocCollections ────────────────────────────────────────────────

    @Test @Order(130)
    void distinctDocCollections_exactValues() {
        String dTenant = "ddc-tenant";
        String cA = "ddc__knowledge__voyage__v1";
        String cB = "ddc__code__voyage__v1";

        repo.upsertDocument(dTenant, mapOf(
            "tumbler", "ddc.1", "title", "DDC Doc 1",
            "content_type", "knowledge", "physical_collection", cA
        ));
        repo.upsertDocument(dTenant, mapOf(
            "tumbler", "ddc.2", "title", "DDC Doc 2",
            "content_type", "knowledge", "physical_collection", cA
        ));
        repo.upsertDocument(dTenant, mapOf(
            "tumbler", "ddc.3", "title", "DDC Doc 3",
            "content_type", "code", "physical_collection", cB
        ));
        // Doc with empty physical_collection — must NOT appear.
        repo.upsertDocument(dTenant, mapOf(
            "tumbler", "ddc.4", "title", "DDC Doc 4", "content_type", "paper"
        ));

        var result = repo.distinctDocCollections(dTenant);

        // RLS-isolated tenant — exactly 2 distinct non-empty collections seeded.
        assertThat(result).contains(cA, cB);
        assertThat(result).doesNotContain("");
        assertThat(result).noneMatch(s -> s == null);
        assertThat(result).hasSize(2);
    }

    @Test @Order(131)
    void distinctDocCollections_crossTenantIsolation() {
        String tX = "ddc-tenant-x";
        String tY = "ddc-tenant-y";
        String shared = "shared__ddc__v1";
        String exclusive = "exclusive__ddc-x__v1";

        repo.upsertDocument(tX, mapOf(
            "tumbler", "ddcx.1", "title", "X1",
            "content_type", "knowledge", "physical_collection", shared
        ));
        repo.upsertDocument(tX, mapOf(
            "tumbler", "ddcx.2", "title", "X2",
            "content_type", "knowledge", "physical_collection", exclusive
        ));
        repo.upsertDocument(tY, mapOf(
            "tumbler", "ddcy.1", "title", "Y1",
            "content_type", "knowledge", "physical_collection", shared
        ));

        var colsX = repo.distinctDocCollections(tX);
        var colsY = repo.distinctDocCollections(tY);

        // tX sees its own collections (shared + exclusive)
        assertThat(colsX).contains(shared, exclusive);
        // tY sees only shared; exclusive is tX-only
        assertThat(colsY).contains(shared);
        assertThat(colsY).doesNotContain(exclusive);
    }

    // ── ownersWithRoots ───────────────────────────────────────────────────────

    @Test @Order(132)
    void ownersWithRoots_exactValues() {
        String owrTenant = "owr-tenant";

        repo.upsertOwner(owrTenant, mapOf(
            "tumbler_prefix", "owr.1",
            "name", "OWR Root Repo",
            "owner_type", "repo",
            "repo_root", "/projects/owr-root-repo"
        ));
        repo.upsertOwner(owrTenant, mapOf(
            "tumbler_prefix", "owr.2",
            "name", "OWR No Root",
            "owner_type", "curator"
            // repo_root absent => stored as empty string
        ));

        var result = repo.ownersWithRoots(owrTenant);

        // Only the owner with a non-empty repo_root must appear
        assertThat(result).hasSize(1);
        var m = result.get(0);
        assertThat(m).containsEntry("tumbler_prefix", "owr.1");
        assertThat(m).containsEntry("repo_root", "/projects/owr-root-repo");
        assertThat(m.get("name")).isEqualTo("OWR Root Repo");
    }

    @Test @Order(133)
    void ownersWithRoots_crossTenantIsolation() {
        String tA = "owr-tenant-a";
        String tB = "owr-tenant-b";

        repo.upsertOwner(tA, mapOf(
            "tumbler_prefix", "owra.1", "name", "A Repo",
            "owner_type", "repo", "repo_root", "/projects/a"
        ));
        repo.upsertOwner(tB, mapOf(
            "tumbler_prefix", "owrb.1", "name", "B Repo",
            "owner_type", "repo", "repo_root", "/projects/b"
        ));

        var resultA = repo.ownersWithRoots(tA);
        var resultB = repo.ownersWithRoots(tB);

        // Each tenant sees only its own owners
        var prefixesA = resultA.stream().map(m -> (String) m.get("tumbler_prefix")).toList();
        var prefixesB = resultB.stream().map(m -> (String) m.get("tumbler_prefix")).toList();
        assertThat(prefixesA).contains("owra.1").doesNotContain("owrb.1");
        assertThat(prefixesB).contains("owrb.1").doesNotContain("owra.1");
    }

    // ── orphanedDocs ──────────────────────────────────────────────────────────

    @Test @Order(134)
    void orphanedDocs_exactValues() {
        String orpTenant = "orp-tenant";

        // Three docs: A↔B linked, C is isolated (orphan)
        repo.upsertDocument(orpTenant, mapOf(
            "tumbler", "orp.1", "title", "ORP Doc A",
            "content_type", "paper", "file_path", "a.pdf"
        ));
        repo.upsertDocument(orpTenant, mapOf(
            "tumbler", "orp.2", "title", "ORP Doc B",
            "content_type", "paper", "file_path", "b.pdf"
        ));
        repo.upsertDocument(orpTenant, mapOf(
            "tumbler", "orp.3", "title", "ORP Doc C",
            "content_type", "paper", "file_path", "c.pdf"
        ));
        repo.upsertLink(orpTenant, Map.of(
            "from_tumbler", "orp.1",
            "to_tumbler",   "orp.2",
            "link_type",    "cites",
            "created_by",   "test"
        ));

        var result = repo.orphanedDocs(orpTenant);

        // Only orp.3 has no links in either direction
        var tumblers = result.stream().map(m -> (String) m.get("tumbler")).toList();
        assertThat(tumblers).contains("orp.3");
        // orp.1 (from) and orp.2 (to) are linked — must NOT appear
        assertThat(tumblers).doesNotContain("orp.1", "orp.2");
        // Response must include expected fields
        var orphan = result.stream().filter(m -> "orp.3".equals(m.get("tumbler"))).findFirst().orElseThrow();
        assertThat(orphan).containsKey("title");
        assertThat(orphan).containsKey("content_type");
        assertThat(orphan).containsKey("file_path");
    }

    @Test @Order(135)
    void orphanedDocs_crossTenantIsolation() {
        String tX = "orp-tenant-x";
        String tY = "orp-tenant-y";

        // tX: one orphan
        repo.upsertDocument(tX, mapOf(
            "tumbler", "orpx.1", "title", "X Orphan",
            "content_type", "paper", "file_path", "x.pdf"
        ));
        // tY: one linked pair (neither is an orphan)
        repo.upsertDocument(tY, mapOf(
            "tumbler", "orpy.1", "title", "Y From",
            "content_type", "paper", "file_path", "yf.pdf"
        ));
        repo.upsertDocument(tY, mapOf(
            "tumbler", "orpy.2", "title", "Y To",
            "content_type", "paper", "file_path", "yt.pdf"
        ));
        repo.upsertLink(tY, Map.of(
            "from_tumbler", "orpy.1", "to_tumbler", "orpy.2",
            "link_type", "cites", "created_by", "test"
        ));

        var orphansX = repo.orphanedDocs(tX);
        var orphansY = repo.orphanedDocs(tY);

        var tumblersX = orphansX.stream().map(m -> (String) m.get("tumbler")).toList();
        var tumblersY = orphansY.stream().map(m -> (String) m.get("tumbler")).toList();

        // tX sees its own orphan; tY docs must NOT appear
        assertThat(tumblersX).contains("orpx.1");
        assertThat(tumblersX).doesNotContain("orpy.1", "orpy.2");
        // tY has no orphans (both docs are linked)
        assertThat(tumblersY).isEmpty();
    }

    // ── docsWithAbsolutePaths ─────────────────────────────────────────────────

    @Test @Order(136)
    void docsWithAbsolutePaths_exactValues() {
        String absTenant = "abs-tenant";

        repo.upsertDocument(absTenant, mapOf(
            "tumbler", "abs.1", "title", "ABS Absolute",
            "content_type", "paper",
            "file_path", "/usr/local/data/abs.pdf",
            "physical_collection", "abs__knowledge__v1"
        ));
        repo.upsertDocument(absTenant, mapOf(
            "tumbler", "abs.2", "title", "ABS Relative",
            "content_type", "paper",
            "file_path", "relative/path.pdf",
            "physical_collection", "abs__knowledge__v1"
        ));

        var result = repo.docsWithAbsolutePaths(absTenant);

        var tumblers = result.stream().map(m -> (String) m.get("tumbler")).toList();
        assertThat(tumblers).contains("abs.1");
        assertThat(tumblers).doesNotContain("abs.2");

        var entry = result.stream()
            .filter(m -> "abs.1".equals(m.get("tumbler"))).findFirst().orElseThrow();
        assertThat(entry.get("file_path")).isEqualTo("/usr/local/data/abs.pdf");
        assertThat(entry.get("physical_collection")).isEqualTo("abs__knowledge__v1");
    }

    @Test @Order(137)
    void docsWithAbsolutePaths_crossTenantIsolation() {
        String tA = "abs-tenant-a";
        String tB = "abs-tenant-b";

        repo.upsertDocument(tA, mapOf(
            "tumbler", "absa.1", "title", "A Abs",
            "content_type", "paper", "file_path", "/a/absolute.pdf"
        ));
        repo.upsertDocument(tB, mapOf(
            "tumbler", "absb.1", "title", "B Abs",
            "content_type", "paper", "file_path", "/b/absolute.pdf"
        ));

        var resultA = repo.docsWithAbsolutePaths(tA);
        var resultB = repo.docsWithAbsolutePaths(tB);

        var tumblersA = resultA.stream().map(m -> (String) m.get("tumbler")).toList();
        var tumblersB = resultB.stream().map(m -> (String) m.get("tumbler")).toList();

        assertThat(tumblersA).contains("absa.1").doesNotContain("absb.1");
        assertThat(tumblersB).contains("absb.1").doesNotContain("absa.1");
    }

    // ── collectionOwnerRoot ───────────────────────────────────────────────────

    @Test @Order(138)
    void collectionOwnerRoot_exactValues() {
        String corTenant = "cor-tenant";
        String collName  = "cor__knowledge__voyage__v1";

        repo.upsertOwner(corTenant, mapOf(
            "tumbler_prefix", "cor.1",
            "name", "COR Owner",
            "owner_type", "repo",
            "repo_root", "/projects/cor"
        ));
        repo.upsertCollection(corTenant, Map.of(
            "name", collName,
            "content_type", "knowledge",
            "owner_id", "cor.1",
            "embedding_model", "voyage-context-3"
        ));

        var result = repo.collectionOwnerRoot(corTenant, collName);

        assertThat(result).isNotNull();
        assertThat(result.get("owner_id")).isEqualTo("cor.1");
        assertThat(result.get("repo_root")).isEqualTo("/projects/cor");
    }

    @Test @Order(139)
    void collectionOwnerRoot_absentCollectionReturnsNull() {
        var result = repo.collectionOwnerRoot(TENANT_A, "no-such-collection-xyz");
        assertThat(result).isNull();
    }

    @Test @Order(140)
    void collectionOwnerRoot_crossTenantIsolation() {
        String tA = "cor-tenant-a";
        String tB = "cor-tenant-b";
        String collA = "cor__a__voyage__v1";
        String collB = "cor__b__voyage__v1";

        repo.upsertOwner(tA, mapOf(
            "tumbler_prefix", "cora.1", "name", "A Owner",
            "owner_type", "repo", "repo_root", "/projects/a"
        ));
        repo.upsertCollection(tA, Map.of(
            "name", collA, "content_type", "knowledge",
            "owner_id", "cora.1", "embedding_model", "voyage-context-3"
        ));

        repo.upsertOwner(tB, mapOf(
            "tumbler_prefix", "corb.1", "name", "B Owner",
            "owner_type", "repo", "repo_root", "/projects/b"
        ));
        repo.upsertCollection(tB, Map.of(
            "name", collB, "content_type", "knowledge",
            "owner_id", "corb.1", "embedding_model", "voyage-context-3"
        ));

        // tA cannot see tB's collection and vice versa
        var resultAforB = repo.collectionOwnerRoot(tA, collB);
        var resultBforA = repo.collectionOwnerRoot(tB, collA);

        assertThat(resultAforB).isNull();
        assertThat(resultBforA).isNull();
    }

    // ── stats with by_content_type ────────────────────────────────────────────

    @Test @Order(141)
    void stats_byContentType_exactValues() {
        String stTenant = "st-tenant";

        repo.upsertDocument(stTenant, mapOf(
            "tumbler", "st.1", "title", "ST Paper 1", "content_type", "paper"
        ));
        repo.upsertDocument(stTenant, mapOf(
            "tumbler", "st.2", "title", "ST Paper 2", "content_type", "paper"
        ));
        repo.upsertDocument(stTenant, mapOf(
            "tumbler", "st.3", "title", "ST Code 1", "content_type", "code"
        ));
        repo.upsertLink(stTenant, Map.of(
            "from_tumbler", "st.1", "to_tumbler", "st.2",
            "link_type", "cites", "created_by", "test"
        ));
        repo.upsertLink(stTenant, Map.of(
            "from_tumbler", "st.2", "to_tumbler", "st.3",
            "link_type", "relates", "created_by", "test"
        ));

        var stats = repo.stats(stTenant);

        @SuppressWarnings("unchecked")
        var byType = (Map<String, Long>) stats.get("by_content_type");
        assertThat(byType).isNotNull();
        assertThat(byType.get("paper")).isEqualTo(2L);
        assertThat(byType.get("code")).isEqualTo(1L);

        @SuppressWarnings("unchecked")
        var byLinkType = (Map<String, Long>) stats.get("links_by_type");
        assertThat(byLinkType).isNotNull();
        assertThat(byLinkType.get("cites")).isEqualTo(1L);
        assertThat(byLinkType.get("relates")).isEqualTo(1L);
    }

    @Test @Order(142)
    void stats_byContentType_crossTenantIsolation() {
        String tA = "st-tenant-a";
        String tB = "st-tenant-b";

        repo.upsertDocument(tA, mapOf(
            "tumbler", "sta.1", "title", "STA Paper", "content_type", "paper"
        ));
        repo.upsertDocument(tA, mapOf(
            "tumbler", "sta.2", "title", "STA RDR", "content_type", "rdr"
        ));
        repo.upsertDocument(tB, mapOf(
            "tumbler", "stb.1", "title", "STB Paper", "content_type", "paper"
        ));
        repo.upsertDocument(tB, mapOf(
            "tumbler", "stb.2", "title", "STB Paper 2", "content_type", "paper"
        ));
        repo.upsertDocument(tB, mapOf(
            "tumbler", "stb.3", "title", "STB Code", "content_type", "code"
        ));

        var statsA = repo.stats(tA);
        var statsB = repo.stats(tB);

        @SuppressWarnings("unchecked")
        var byTypeA = (Map<String, Long>) statsA.get("by_content_type");
        @SuppressWarnings("unchecked")
        var byTypeB = (Map<String, Long>) statsB.get("by_content_type");

        // tA: 1 paper + 1 rdr; no code
        assertThat(byTypeA.get("paper")).isEqualTo(1L);
        assertThat(byTypeA.get("rdr")).isEqualTo(1L);
        assertThat(byTypeA.get("code")).isNull();

        // tB: 2 papers + 1 code; no rdr
        assertThat(byTypeB.get("paper")).isEqualTo(2L);
        assertThat(byTypeB.get("code")).isEqualTo(1L);
        assertThat(byTypeB.get("rdr")).isNull();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // ANALYTICS QUERIES (nexus-xnz0o)
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(122)
    void collectionDocCounts_exactValues() {
        // Seed docs in two distinct collections.
        String anTenant = "an-tenant";
        String coll1    = "an__knowledge__voyage__v1";
        String coll2    = "an__code__voyage__v1";

        repo.upsertDocument(anTenant, mapOf(
            "tumbler", "an.1", "title", "AN Doc 1",
            "content_type", "knowledge", "physical_collection", coll1
        ));
        repo.upsertDocument(anTenant, mapOf(
            "tumbler", "an.2", "title", "AN Doc 2",
            "content_type", "knowledge", "physical_collection", coll1
        ));
        repo.upsertDocument(anTenant, mapOf(
            "tumbler", "an.3", "title", "AN Doc 3",
            "content_type", "code", "physical_collection", coll2
        ));
        // One doc with no physical_collection — must NOT appear in counts.
        repo.upsertDocument(anTenant, mapOf(
            "tumbler", "an.4", "title", "AN Doc 4", "content_type", "paper"
        ));

        var counts = repo.collectionDocCounts(anTenant);

        assertThat(counts).containsEntry(coll1, 2L);
        assertThat(counts).containsEntry(coll2, 1L);
        // doc with no physical_collection must not appear
        assertThat(counts).doesNotContainKey("");
        assertThat(counts).doesNotContainKey(null);
    }

    @Test @Order(123)
    void collectionDocCounts_crossTenantIsolation() {
        // Two tenants share the same physical_collection name.
        String tenantP = "anp-tenant";
        String tenantQ = "anq-tenant";
        String shared  = "shared__analytics__v1";

        repo.upsertDocument(tenantP, mapOf(
            "tumbler", "anp.1", "title", "P1",
            "content_type", "knowledge", "physical_collection", shared
        ));
        repo.upsertDocument(tenantP, mapOf(
            "tumbler", "anp.2", "title", "P2",
            "content_type", "knowledge", "physical_collection", shared
        ));
        repo.upsertDocument(tenantP, mapOf(
            "tumbler", "anp.3", "title", "P3",
            "content_type", "knowledge", "physical_collection", shared
        ));
        repo.upsertDocument(tenantQ, mapOf(
            "tumbler", "anq.1", "title", "Q1",
            "content_type", "knowledge", "physical_collection", shared
        ));

        var countsP = repo.collectionDocCounts(tenantP);
        var countsQ = repo.collectionDocCounts(tenantQ);

        // TENANT_P sees 3 docs; TENANT_Q sees 1 doc — RLS must isolate.
        assertThat(countsP).containsEntry(shared, 3L);
        assertThat(countsQ).containsEntry(shared, 1L);
    }

    // ── nexus-8tnz2 fix-round CRITICAL 2: collectionDocCountsIncludingDeleted ──

    @Test @Order(124)
    void collectionDocCountsIncludingDeleted_matchesLiveOnlyWhenNothingTombstoned() {
        String tenant = "an-idl-tenant";
        String coll   = "an-idl__knowledge__voyage__v1";

        repo.upsertDocument(tenant, mapOf(
            "tumbler", "anidl.1", "title", "Live 1",
            "content_type", "knowledge", "physical_collection", coll
        ));
        repo.upsertDocument(tenant, mapOf(
            "tumbler", "anidl.2", "title", "Live 2",
            "content_type", "knowledge", "physical_collection", coll
        ));

        var liveOnly = repo.collectionDocCounts(tenant);
        var allRows  = repo.collectionDocCountsIncludingDeleted(tenant);

        assertThat(liveOnly).containsEntry(coll, 2L);
        assertThat(allRows).containsEntry(coll, 2L);
    }

    @Test @Order(125)
    void collectionDocCountsIncludingDeleted_countsTombstonedDocsLiveOnlyDoesNot() {
        // nexus-8tnz2 fix-round CRITICAL 2: a collection whose ONLY catalog
        // document has been soft-tombstoned must read live=0, all=1 — the
        // exact signal classify_t3_orphan_collections uses to distinguish
        // "tombstoned-only" (restorable) from "orphan" (never registered).
        String tenant = "an-idl2-tenant";
        String coll   = "an-idl2__knowledge__voyage__v1";

        repo.upsertDocument(tenant, mapOf(
            "tumbler", "anidl2.1", "title", "Soon Tombstoned",
            "content_type", "knowledge", "physical_collection", coll
        ));

        int deleted = repo.deleteDocument(tenant, "anidl2.1");
        assertThat(deleted).isEqualTo(1);

        var liveOnly = repo.collectionDocCounts(tenant);
        var allRows  = repo.collectionDocCountsIncludingDeleted(tenant);

        assertThat(liveOnly).doesNotContainKey(coll);
        assertThat(allRows).containsEntry(coll, 1L);
    }

    @Test @Order(126)
    void collectionDocCountsIncludingDeleted_crossTenantIsolation() {
        String tenantP = "anidlp-tenant";
        String tenantQ = "anidlq-tenant";
        String shared  = "shared-idl__analytics__v1";

        repo.upsertDocument(tenantP, mapOf(
            "tumbler", "anidlp.1", "title", "P1",
            "content_type", "knowledge", "physical_collection", shared
        ));
        repo.deleteDocument(tenantP, "anidlp.1");
        repo.upsertDocument(tenantQ, mapOf(
            "tumbler", "anidlq.1", "title", "Q1",
            "content_type", "knowledge", "physical_collection", shared
        ));

        var allRowsP = repo.collectionDocCountsIncludingDeleted(tenantP);
        var allRowsQ = repo.collectionDocCountsIncludingDeleted(tenantQ);

        // tenantP's tombstoned doc is counted in P's include_deleted view
        // (RLS scopes by tenant, not by liveness); it must never leak into Q.
        assertThat(allRowsP).containsEntry(shared, 1L);
        assertThat(allRowsQ).containsEntry(shared, 1L);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // COVERAGE BY CONTENT TYPE (nexus-3cwnx)
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Seed:
     *   - 3 papers (2 with links, 1 unlinked)
     *   - 2 rdrs   (1 with link, 1 unlinked)
     *   - 1 code   (0 links)
     * Expected coverage:
     *   paper  -> total=3 linked=2
     *   rdr    -> total=2 linked=1
     *   code   -> total=1 linked=0
     */
    @Test @Order(143)
    void coverageByContentType_exactValues() {
        String cov1 = "cov1-tenant";

        // Seed documents: 3 papers, 2 rdrs, 1 code
        repo.upsertDocument(cov1, mapOf("tumbler","cov1.1","title","Paper A","content_type","paper"));
        repo.upsertDocument(cov1, mapOf("tumbler","cov1.2","title","Paper B","content_type","paper"));
        repo.upsertDocument(cov1, mapOf("tumbler","cov1.3","title","Paper C (unlinked)","content_type","paper"));
        repo.upsertDocument(cov1, mapOf("tumbler","cov1.4","title","RDR A","content_type","rdr"));
        repo.upsertDocument(cov1, mapOf("tumbler","cov1.5","title","RDR B (unlinked)","content_type","rdr"));
        repo.upsertDocument(cov1, mapOf("tumbler","cov1.6","title","Code A (unlinked)","content_type","code"));

        // Links: cov1.1->cov1.2 (cites), cov1.2->cov1.4 (implements)
        // => linked papers: cov1.1, cov1.2 (two distinct); linked rdrs: cov1.4 (one distinct)
        repo.upsertLink(cov1, mapOf(
            "from_tumbler","cov1.1","to_tumbler","cov1.2","link_type","cites","created_by","test"));
        repo.upsertLink(cov1, mapOf(
            "from_tumbler","cov1.2","to_tumbler","cov1.4","link_type","implements","created_by","test"));

        var rows = repo.coverageByContentType(cov1, "");

        // Build a lookup map for easy assertion
        var byType = new java.util.HashMap<String, Map<String, Object>>();
        for (var r : rows) byType.put((String) r.get("content_type"), r);

        // paper: 3 total, 2 linked (cov1.1 from, cov1.2 from+to, cov1.3 none)
        assertThat(byType).containsKey("paper");
        assertThat(((Number) byType.get("paper").get("total")).longValue()).isEqualTo(3L);
        assertThat(((Number) byType.get("paper").get("linked")).longValue()).isEqualTo(2L);

        // rdr: 2 total, 1 linked (cov1.4 as to_tumbler)
        assertThat(byType).containsKey("rdr");
        assertThat(((Number) byType.get("rdr").get("total")).longValue()).isEqualTo(2L);
        assertThat(((Number) byType.get("rdr").get("linked")).longValue()).isEqualTo(1L);

        // code: 1 total, 0 linked
        assertThat(byType).containsKey("code");
        assertThat(((Number) byType.get("code").get("total")).longValue()).isEqualTo(1L);
        assertThat(((Number) byType.get("code").get("linked")).longValue()).isEqualTo(0L);
    }

    /**
     * Owner-prefix filter: only documents under the given prefix are counted.
     * cov2.1.X documents (prefix="cov2.1") should be isolated from cov2.2.X.
     */
    @Test @Order(144)
    void coverageByContentType_ownerPrefixFilter() {
        String cov2 = "cov2-tenant";
        // tumbler "cov2.1" itself (exercises the OR tumbler = prefix arm)
        repo.upsertDocument(cov2, mapOf("tumbler","cov2.1","title","Cov2 Owner","content_type","paper"));
        // Under owner prefix cov2.1: 2 more papers, one with link
        repo.upsertDocument(cov2, mapOf("tumbler","cov2.1.1","title","Cov2 Paper A","content_type","paper"));
        repo.upsertDocument(cov2, mapOf("tumbler","cov2.1.2","title","Cov2 Paper B","content_type","paper"));
        // Under owner prefix cov2.2: 2 papers with link (must NOT appear)
        repo.upsertDocument(cov2, mapOf("tumbler","cov2.2.1","title","Cov2 Paper C","content_type","paper"));
        repo.upsertDocument(cov2, mapOf("tumbler","cov2.2.2","title","Cov2 Paper D","content_type","paper"));

        // Link within cov2.1: cov2.1.1 -> cov2.1.2
        repo.upsertLink(cov2, mapOf(
            "from_tumbler","cov2.1.1","to_tumbler","cov2.1.2","link_type","cites","created_by","test"));
        // Link within cov2.2: cov2.2.1 -> cov2.2.2 (must NOT affect cov2.1 results)
        repo.upsertLink(cov2, mapOf(
            "from_tumbler","cov2.2.1","to_tumbler","cov2.2.2","link_type","cites","created_by","test"));

        // Query with prefix "cov2.1" — should see cov2.1 (exact) + cov2.1.X (LIKE)
        var rows = repo.coverageByContentType(cov2, "cov2.1");
        assertThat(rows).hasSize(1);
        var paperRow = rows.get(0);
        assertThat(paperRow.get("content_type")).isEqualTo("paper");
        // 3 docs: "cov2.1" (exact), "cov2.1.1", "cov2.1.2"
        assertThat(((Number) paperRow.get("total")).longValue()).isEqualTo(3L);
        // Linked: cov2.1.1 (from_tumbler), cov2.1.2 (to_tumbler) = 2 linked; "cov2.1" unlinked
        assertThat(((Number) paperRow.get("linked")).longValue()).isEqualTo(2L);
    }

    /**
     * RLS: cross-tenant isolation — coverageByContentType for tenant X must not
     * reveal tenant Y's documents or links.
     */
    @Test @Order(145)
    void coverageByContentType_crossTenantIsolation() {
        String tX = "cov-rls-x";
        String tY = "cov-rls-y";

        // Seed tX: 1 paper with link
        repo.upsertDocument(tX, mapOf("tumbler","covx.1","title","X Paper A","content_type","paper"));
        repo.upsertDocument(tX, mapOf("tumbler","covx.2","title","X Paper B","content_type","paper"));
        repo.upsertLink(tX, mapOf(
            "from_tumbler","covx.1","to_tumbler","covx.2","link_type","cites","created_by","test"));

        // Seed tY: 3 papers, 2 with links
        repo.upsertDocument(tY, mapOf("tumbler","covy.1","title","Y Paper A","content_type","paper"));
        repo.upsertDocument(tY, mapOf("tumbler","covy.2","title","Y Paper B","content_type","paper"));
        repo.upsertDocument(tY, mapOf("tumbler","covy.3","title","Y Paper C","content_type","paper"));
        repo.upsertLink(tY, mapOf(
            "from_tumbler","covy.1","to_tumbler","covy.2","link_type","cites","created_by","test"));
        repo.upsertLink(tY, mapOf(
            "from_tumbler","covy.2","to_tumbler","covy.3","link_type","cites","created_by","test"));

        var rowsX = repo.coverageByContentType(tX, "");
        var rowsY = repo.coverageByContentType(tY, "");

        // tX sees exactly its own 2 papers, 2 linked
        assertThat(rowsX).hasSize(1);
        var xPaper = rowsX.get(0);
        assertThat(((Number) xPaper.get("total")).longValue()).isEqualTo(2L);
        assertThat(((Number) xPaper.get("linked")).longValue()).isEqualTo(2L);

        // tY sees exactly its own 3 papers, 3 linked (covy.1 as from, covy.2 as from+to, covy.3 as to)
        assertThat(rowsY).hasSize(1);
        var yPaper = rowsY.get(0);
        assertThat(((Number) yPaper.get("total")).longValue()).isEqualTo(3L);
        assertThat(((Number) yPaper.get("linked")).longValue()).isEqualTo(3L);
    }

    /**
     * nexus-l1nre meta-assertion: the view branch (empty ownerPrefix) and the
     * owner-prefix hand-aggregation branch of coverageByContentType must
     * AGREE on the same fixture, tombstone included. Before this fix the view
     * branch was unfiltered while the hand-aggregation branch already
     * filtered deleted_at IS NULL — the same method disagreeing with itself
     * depending purely on whether a prefix argument was supplied.
     */
    @Test
    void coverageByContentType_viewBranchAndOwnerPrefixBranch_agree_withTombstone() {
        String tenant = "cov-agree-" + System.nanoTime();
        repo.upsertDocument(tenant, mapOf("tumbler", "covagree.1", "title", "P1", "content_type", "paper"));
        repo.upsertDocument(tenant, mapOf("tumbler", "covagree.2", "title", "P2", "content_type", "paper"));
        repo.upsertDocument(tenant, mapOf("tumbler", "covagree.3", "title", "P3 (tombstoned)", "content_type", "paper"));
        repo.upsertLink(tenant, mapOf(
            "from_tumbler", "covagree.1", "to_tumbler", "covagree.2", "link_type", "cites", "created_by", "test"));

        assertThat(repo.deleteDocument(tenant, "covagree.3")).isEqualTo(1);

        var viewBranch = repo.coverageByContentType(tenant, "");
        var prefixBranch = repo.coverageByContentType(tenant, "covagree");

        assertThat(viewBranch).as("non-vacuity: exactly one content_type in this tenant").hasSize(1);
        assertThat(prefixBranch).hasSize(1);

        var v = viewBranch.get(0);
        var p = prefixBranch.get(0);
        assertThat(v.get("content_type")).isEqualTo(p.get("content_type"));
        assertThat(((Number) v.get("total")).longValue())
            .as("the two branches must AGREE on total, tombstone included")
            .isEqualTo(((Number) p.get("total")).longValue())
            .isEqualTo(2L);
        assertThat(((Number) v.get("linked")).longValue())
            .as("the two branches must AGREE on linked, tombstone included")
            .isEqualTo(((Number) p.get("linked")).longValue())
            .isEqualTo(2L);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // TENANT B ISOLATION CHECK
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(95)
    void tenantB_ownData_isolated_from_tenantA() {
        // Seed TENANT_B data
        repo.upsertDocument(TENANT_B, Map.of("tumbler", "b.1", "title", "Tenant B Doc",
            "content_type", "paper", "corpus", "knowledge"));
        repo.upsertDocument(TENANT_B, Map.of("tumbler", "b.2", "title", "Tenant B Doc 2",
            "content_type", "paper", "corpus", "knowledge"));

        var listB = repo.listDocuments(TENANT_B, 100, 0);
        assertThat(listB).hasSizeGreaterThanOrEqualTo(2);
        // None of TENANT_B's docs should include "rls.1" (a TENANT_A doc)
        var tumblersB = listB.stream().map(d -> (String) d.get("tumbler")).toList();
        assertThat(tumblersB).doesNotContain("rls.1", "1.1", "1.2", "mfst.1");

        // countDocuments only counts TENANT_B rows
        long countB = repo.countDocuments(TENANT_B);
        long countA = repo.countDocuments(TENANT_A);
        assertThat(countB).isGreaterThanOrEqualTo(2);
        assertThat(countA).isGreaterThan(countB); // TENANT_A has more rows
    }

    // ══════════════════════════════════════════════════════════════════════════
    // SPAN / CHASH RESOLUTION  (nexus-njrcn.4)
    // ══════════════════════════════════════════════════════════════════════════

    private static final String SPAN_TENANT     = "span-tenant-a";
    // Full 64-hex canonical chash (RDR-180: chunks_*/manifest columns are bytea(32))
    private static final String SPAN_CHASH      = ch("span-chash");
    private static final String SPAN_COLLECTION = "knowledge__span__bge-768__v1";
    private static final String SPAN_DOC_ID     = "span.1";

    /**
     * Seed: register the collection FK target, then insert a chunk row via raw
     * SQL (no vector column required when using zero-fill embedding).
     *
     * <p>RDR-191 (nexus-o8dil.48): {@code nexus.chunks} (unified; formerly
     * {@code chunks_768}) has a FK to catalog_collections (COLLECTION col);
     * we must upsert the collection row BEFORE inserting the chunk.  The catalog_document_chunks
     * row links chash → doc_id for the resolveChash doc_id assertion.
     */
    @Test @Order(210)
    void resolveSpan_returnsChunkTextAndMetadata() throws Exception {
        // 1. Register the collection (FK prerequisite).
        repo.upsertCollection(SPAN_TENANT, Map.of(
            "name",            SPAN_COLLECTION,
            "content_type",    "knowledge",
            "owner_id",        "span-owner",
            "embedding_model", "bge-base-en-v15-768",
            "model_version",   "v1"
        ));

        // 2. Insert a chunk row with a zero-filled 768-dim embedding via raw SQL.
        //    The embedding column is vector(768): we cast a text literal.
        try (var su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "SET nexus.tenant = '" + SPAN_TENANT + "'"
            );
            // Build a zero-vector literal: '[0,0,...,0]' with 768 zeros.
            String zeroVec = "[" + "0,".repeat(767) + "0]";
            // RDR-191 (nexus-o8dil.48): chunks_768 unified into nexus.chunks --
            // embedding_768 replaces the bare embedding column.
            var ps = su.prepareStatement(
                "INSERT INTO nexus.chunks"
                + " (tenant_id, collection, chash, chunk_text, embedding_768, metadata)"
                + " VALUES (?, ?, ?, ?, ?::vector, ?::jsonb)"
                + " ON CONFLICT (tenant_id, collection, chash) DO NOTHING"
            );
            ps.setString(1, SPAN_TENANT);
            ps.setString(2, SPAN_COLLECTION);
            // chash column is bytea(32) now (RDR-180) — bind the decoded digest, not the hex text.
            ps.setBytes(3, java.util.HexFormat.of().parseHex(SPAN_CHASH));
            ps.setString(4, "hello span text");
            ps.setString(5, zeroVec);
            ps.setString(6, "{\"lang\":\"en\"}");
            ps.executeUpdate();
        }

        // 3. resolveSpan — keyed by (collection, chash).
        var result = repo.resolveSpan(SPAN_TENANT, SPAN_COLLECTION, SPAN_CHASH);
        assertThat(result).isNotNull();
        assertThat(result.get("chunk_text")).isEqualTo("hello span text");
        assertThat(result.get("chunk_hash")).isEqualTo(SPAN_CHASH);
        @SuppressWarnings("unchecked")
        var meta = (Map<String, Object>) result.get("metadata");
        assertThat(meta).containsEntry("lang", "en");
    }

    @Test @Order(211)
    void resolveSpan_miss_returnsNull() {
        // Query for a chash that does not exist in the collection.
        var result = repo.resolveSpan(SPAN_TENANT, SPAN_COLLECTION, "0000000000000000000000000000dead");
        assertThat(result).isNull();
    }

    @Test @Order(212)
    void resolveChash_returnsCollectionAndDocId() throws Exception {
        // Seed a catalog_document_chunks row linking SPAN_CHASH → SPAN_DOC_ID.
        repo.upsertDocument(SPAN_TENANT, Map.of(
            "tumbler",      SPAN_DOC_ID,
            "title",        "Span Test Doc",
            "content_type", "knowledge",
            "corpus",       "knowledge",
            "physical_collection", SPAN_COLLECTION
        ));
        writeManifestSeeded(SPAN_TENANT, SPAN_DOC_ID, SPAN_COLLECTION, List.of(
            Map.<String, Object>of("position", 0, "chash", SPAN_CHASH, "chunk_index", 0)
        ));

        // resolveChash — global lookup with prefer_collection hint.
        var result = repo.resolveChash(SPAN_TENANT, SPAN_CHASH, SPAN_COLLECTION);
        assertThat(result).isNotNull();
        assertThat(result.get("chash")).isEqualTo(SPAN_CHASH);
        assertThat(result.get("chunk_hash")).isEqualTo(SPAN_CHASH);
        assertThat(result.get("physical_collection")).isEqualTo(SPAN_COLLECTION);
        assertThat(result.get("chunk_text")).isEqualTo("hello span text");
        assertThat(result.get("doc_id")).isEqualTo(SPAN_DOC_ID);
        @SuppressWarnings("unchecked")
        var meta = (Map<String, Object>) result.get("metadata");
        assertThat(meta).containsEntry("lang", "en");
    }

    @Test @Order(213)
    void resolveChash_miss_returnsNull() {
        // Chash that was never inserted — must return null, not throw.
        var result = repo.resolveChash(SPAN_TENANT, "ffffffff000000000000000000000000", null);
        assertThat(result).isNull();
    }

    @Test @Order(214)
    void resolveChash_tenantIsolation() {
        // SPAN_CHASH belongs to SPAN_TENANT; querying from another tenant must return null.
        var result = repo.resolveChash(TENANT_B, SPAN_CHASH, null);
        assertThat(result).isNull();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // CHUNK RESOLUTION (nexus-gc2ze)
    // ══════════════════════════════════════════════════════════════════════════
    // Mirrors the local Catalog._DocumentOps.resolve_chunk contract
    // (catalog_docs.py): chunks are implicit addresses derived from a
    // document's chunk_count, not their own catalog rows. resolveChunk()
    // is a pure lookup + range-check over an existing document row (no new
    // SQL — it delegates to getDocument()).

    private static final String CHUNK_DOC_TUMBLER = "9.9.101";

    @Test @Order(215)
    void resolveChunk_returnsDocumentAndChunkMetadata() {
        repo.upsertDocument(TENANT_A, mapOf(
            "tumbler", CHUNK_DOC_TUMBLER,
            "title", "Chunk Resolution Doc",
            "content_type", "code",
            "corpus", "code",
            "physical_collection", "code__nexus__voyage-code-3__v1",
            "chunk_count", 3
        ));
        var result = repo.resolveChunk(TENANT_A, CHUNK_DOC_TUMBLER, 1);
        assertThat(result).isNotNull();
        assertThat(result.get("document_tumbler")).isEqualTo(CHUNK_DOC_TUMBLER);
        assertThat(result.get("chunk_index")).isEqualTo(1);
        assertThat(result.get("physical_collection")).isEqualTo("code__nexus__voyage-code-3__v1");
        assertThat(result.get("title")).isEqualTo("Chunk Resolution Doc");
        assertThat(result.get("content_type")).isEqualTo("code");
    }

    @Test @Order(216)
    void resolveChunk_outOfRangeIndex_returnsNull() {
        // chunk_count=3 seeded in Order 215 -> valid indices are 0, 1, 2.
        var result = repo.resolveChunk(TENANT_A, CHUNK_DOC_TUMBLER, 3);
        assertThat(result).isNull();
    }

    @Test @Order(217)
    void resolveChunk_missingDocument_returnsNull() {
        var result = repo.resolveChunk(TENANT_A, "9.9.999", 0);
        assertThat(result).isNull();
    }

    @Test @Order(218)
    void resolveChunk_zeroChunkCount_skipsBoundsCheck() {
        // chunk_count=0 (unset/unknown): the local Python contract skips the
        // bounds check entirely in this case (catalog_docs.py: "chunk_count
        // of 0 or None means count is not yet known") — a large chunk index
        // must still resolve rather than being rejected as out-of-range.
        final String tumbler = "9.9.102";
        repo.upsertDocument(TENANT_A, mapOf(
            "tumbler", tumbler,
            "title", "Unknown Chunk Count Doc",
            "content_type", "code",
            "corpus", "code",
            "physical_collection", "code__nexus__voyage-code-3__v1"
        ));
        var result = repo.resolveChunk(TENANT_A, tumbler, 999);
        assertThat(result).isNotNull();
        assertThat(result.get("chunk_index")).isEqualTo(999);
    }

    @Test @Order(219)
    void resolveChunk_writtenThroughNormalManifestPath_resolvesLastChunk() {
        // nexus-ojazb pin: writeManifest (the normal production write path)
        // folds documents.chunk_count = rows.size() in the SAME transaction
        // (nexus-b6enc F5), so there is no staleness window for a manifest
        // written through it — the last valid index (rows.size() - 1) must
        // resolve, and rows.size() itself must be rejected as out of range.
        // This does NOT cover the ETL importChunk/importChunksBatch legs,
        // which do not fold chunk_count — see resolveChunk's javadoc.
        final String tumbler = "9.9.103";
        repo.upsertDocument(TENANT_A, mapOf(
            "tumbler", tumbler,
            "title", "Normal Path Last Chunk Doc",
            "content_type", "code",
            "corpus", "code",
            "physical_collection", "code__nexus__voyage-code-3__v1"
        ));
        writeManifestSeeded(TENANT_A, tumbler, "code__nexus__voyage-code-3__v1", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("np0"), "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", ch("np1"), "chunk_index", 1),
            Map.<String, Object>of("position", 2, "chash", ch("np2"), "chunk_index", 2),
            Map.<String, Object>of("position", 3, "chash", ch("np3"), "chunk_index", 3),
            Map.<String, Object>of("position", 4, "chash", ch("np4"), "chunk_index", 4)
        ));
        var last = repo.resolveChunk(TENANT_A, tumbler, 4);
        assertThat(last).as("last chunk (index rows.size()-1) must resolve").isNotNull();
        assertThat(last.get("chunk_index")).isEqualTo(4);

        var outOfRange = repo.resolveChunk(TENANT_A, tumbler, 5);
        assertThat(outOfRange).as("index == rows.size() must be out of range").isNull();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // importXBatch: ONE multi-row INSERT per method (nexus-1usso)
    // ══════════════════════════════════════════════════════════════════════════
    // Plan-audit correction: these endpoints already existed but their repository
    // implementations still looped per-row .execute() inside one tenant transaction
    // (N round-trips). These tests exercise the multi-row conversion.

    @Test @Order(220)
    void importOwnersBatch_multiRow_insertsAll_greatestSeq_intraBatchDedupe() throws Exception {
        String tenant = "etl-batch-owner-tenant";
        int n = repo.importOwnersBatch(tenant, List.of(
            Map.of("tumbler_prefix", "bo1", "name", "batch-owner-1", "owner_type", "repo",
                   "next_seq", 5),
            Map.of("tumbler_prefix", "bo2", "name", "batch-owner-2", "owner_type", "repo",
                   "next_seq", 7),
            // Intra-batch duplicate on tumbler_prefix "bo1" — last occurrence wins.
            Map.of("tumbler_prefix", "bo1", "name", "batch-owner-1-updated", "owner_type", "repo",
                   "next_seq", 1)));
        assertThat(n).as("rows submitted (contract unchanged), not rows landed").isEqualTo(3);

        var bo1 = repo.ownerByPrefix(tenant, "bo1");
        assertThat(bo1).isNotNull();
        assertThat(bo1.get("name")).isEqualTo("batch-owner-1-updated");
        var bo2 = repo.ownerByPrefix(tenant, "bo2");
        assertThat(bo2).isNotNull();
        assertThat(bo2.get("name")).isEqualTo("batch-owner-2");

        // Seed a higher live seq, then re-import a lower one — GREATEST must not downgrade.
        // ownerByPrefix() does not expose next_seq — verify via raw SQL (superuser conn).
        repo.importOwnersBatch(tenant, List.of(
            Map.of("tumbler_prefix", "bo1", "name", "batch-owner-1-updated", "owner_type", "repo",
                   "next_seq", 50)));
        repo.importOwnersBatch(tenant, List.of(
            Map.of("tumbler_prefix", "bo1", "name", "batch-owner-1-updated", "owner_type", "repo",
                   "next_seq", 3)));
        try (Connection su = pg.createConnection("")) {
            var rs = su.createStatement().executeQuery(
                "SELECT next_seq FROM nexus.catalog_owners WHERE tenant_id='" + tenant
                + "' AND tumbler_prefix='bo1'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("next_seq"))
                .as("GREATEST: next_seq must never downgrade").isEqualTo(50L);
        }
    }

    @Test @Order(221)
    void importOwnersBatch_emptyAndNull_returnZero() {
        assertThat(repo.importOwnersBatch("etl-batch-owner-tenant", List.of())).isZero();
        assertThat(repo.importOwnersBatch("etl-batch-owner-tenant", null)).isZero();
    }

    @Test @Order(222)
    void importDocumentsBatch_multiRow_insertsAll_excludedAndGreatest_intraBatchDedupe() {
        String tenant = "etl-batch-doc-tenant";
        int n = repo.importDocumentsBatch(tenant, List.of(
            Map.of("tumbler", "bd.1", "title", "Batch Doc 1", "content_type", "paper",
                   "corpus", "knowledge", "source_mtime", 1000.0),
            Map.of("tumbler", "bd.2", "title", "Batch Doc 2", "content_type", "paper",
                   "corpus", "knowledge", "source_mtime", 500.0),
            // Intra-batch duplicate on tumbler "bd.1" — last occurrence wins.
            Map.of("tumbler", "bd.1", "title", "Batch Doc 1 v2", "content_type", "paper",
                   "corpus", "knowledge", "source_mtime", 1500.0)));
        assertThat(n).isEqualTo(3);

        var bd1 = repo.getDocument(tenant, "bd.1");
        assertThat(bd1.get("title")).isEqualTo("Batch Doc 1 v2");
        Double mtime1 = (Double) bd1.get("source_mtime");
        assertThat(mtime1).isEqualTo(1500.0);
        var bd2 = repo.getDocument(tenant, "bd.2");
        assertThat(bd2.get("title")).isEqualTo("Batch Doc 2");

        // Re-import bd.1 with a LOWER source_mtime — GREATEST must not downgrade.
        repo.importDocumentsBatch(tenant, List.of(
            Map.of("tumbler", "bd.1", "title", "Batch Doc 1 stale", "content_type", "paper",
                   "corpus", "knowledge", "source_mtime", 10.0)));
        var afterStale = repo.getDocument(tenant, "bd.1");
        assertThat(afterStale.get("title")).as("EXCLUDED: title still updates verbatim").isEqualTo("Batch Doc 1 stale");
        Double mtimeAfter = (Double) afterStale.get("source_mtime");
        assertThat(mtimeAfter).as("GREATEST: source_mtime must never downgrade").isEqualTo(1500.0);
    }

    @Test @Order(223)
    void importLinksBatch_multiRow_doNothingOnReimport_noError() {
        String tenant = "etl-batch-link-tenant";
        repo.importDocument(tenant, Map.of("tumbler", "bl-a", "title", "Batch Link A",
            "content_type", "paper", "corpus", "knowledge"));
        repo.importDocument(tenant, Map.of("tumbler", "bl-b", "title", "Batch Link B",
            "content_type", "paper", "corpus", "knowledge"));
        repo.importDocument(tenant, Map.of("tumbler", "bl-c", "title", "Batch Link C",
            "content_type", "paper", "corpus", "knowledge"));

        int n = repo.importLinksBatch(tenant, List.of(
            Map.of("from_tumbler", "bl-a", "to_tumbler", "bl-b", "link_type", "cites"),
            Map.of("from_tumbler", "bl-a", "to_tumbler", "bl-c", "link_type", "cites")));
        assertThat(n).isEqualTo(2);
        assertThat(repo.linksFrom(tenant, "bl-a", List.of("cites"))).hasSize(2);

        // Re-import the same batch — ON CONFLICT DO NOTHING must not error or duplicate.
        repo.importLinksBatch(tenant, List.of(
            Map.of("from_tumbler", "bl-a", "to_tumbler", "bl-b", "link_type", "cites"),
            Map.of("from_tumbler", "bl-a", "to_tumbler", "bl-b", "link_type", "cites")));
        assertThat(repo.linksFrom(tenant, "bl-a", List.of("cites"))).hasSize(2);
    }

    @Test @Order(224)
    void importChunksBatch_multiRow_convergentUpdate_intraBatchDedupe() {
        String tenant = "etl-batch-chunk-tenant";
        String docId  = "bch.1";
        // nexus-7nrvr: real collection — ghost-ness was incidental (intra-
        // batch dedupe/convergent-update behaviour is the point).
        repo.importDocument(tenant, Map.of("tumbler", docId, "title", "Batch Chunk Doc",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__etl-batch-chunk__v1"));

        String chashV1 = ch("bchV1");
        String chashV2 = ch("bchV2");
        String chashV3 = ch("bchV3");

        int n = importChunksBatchSeeded(tenant, docId, "knowledge__etl-batch-chunk__v1", List.of(
            Map.of("position", 0, "chash", chashV1, "chunk_index", 0,
                   "line_start", 1, "line_end", 5, "char_start", 0, "char_end", 100),
            Map.of("position", 1, "chash", chashV2, "chunk_index", 1,
                   "line_start", 6, "line_end", 10, "char_start", 100, "char_end", 200),
            // Intra-batch duplicate on position 0 — last occurrence wins.
            Map.of("position", 0, "chash", chashV3, "chunk_index", 0,
                   "line_start", 1, "line_end", 5, "char_start", 0, "char_end", 100)));
        assertThat(n).isEqualTo(3);

        var manifest = repo.getManifest(tenant, docId);
        assertThat(manifest).hasSize(2);
        var pos0 = manifest.stream().filter(m -> ((Number) m.get("position")).intValue() == 0).findFirst();
        assertThat(pos0).isPresent();
        assertThat(pos0.get().get("chash")).as("intra-batch dedupe: last wins").isEqualTo(chashV3);

        // Re-import position 0 with yet another chash — convergent DO UPDATE.
        importChunksBatchSeeded(tenant, docId, "knowledge__etl-batch-chunk__v1", List.of(
            Map.of("position", 0, "chash", chashV2, "chunk_index", 0,
                   "line_start", 1, "line_end", 5, "char_start", 0, "char_end", 100)));
        var afterReimport = repo.getManifest(tenant, docId).stream()
            .filter(m -> ((Number) m.get("position")).intValue() == 0).findFirst();
        assertThat(afterReimport.get().get("chash")).isEqualTo(chashV2);
    }

    @Test @Order(225)
    void importCollectionsBatch_multiRow_stubUpgrade_intraBatchDedupe() {
        String tenant = "etl-batch-coll-tenant";
        String name   = "code__batch__voyage-code-3__v1";

        // Seed a stub (all three discriminators empty).
        repo.importCollectionsBatch(tenant, List.of(
            Map.of("name", name, "content_type", "", "owner_id", "", "embedding_model", "",
                   "model_version", "")));
        var before = repo.getCollection(tenant, name);
        assertThat(before).isNotNull();
        assertThat(before.get("content_type")).isEqualTo("");

        // Batch of 2 rows for the SAME name — intra-batch dedupe, last wins — plus
        // the DO UPDATE WHERE-stub predicate must fire (upgrading the stub).
        int n = repo.importCollectionsBatch(tenant, List.of(
            Map.of("name", name, "content_type", "code", "owner_id", "nexus-1-1",
                   "embedding_model", "voyage-code-3", "model_version", "v0"),
            Map.of("name", name, "content_type", "code", "owner_id", "nexus-1-1",
                   "embedding_model", "voyage-code-3", "model_version", "v1")));
        assertThat(n).isEqualTo(2);

        var after = repo.getCollection(tenant, name);
        assertThat(after.get("content_type")).isEqualTo("code");
        assertThat(after.get("model_version")).as("intra-batch dedupe: last wins").isEqualTo("v1");

        // A second batch call must NOT overwrite the now-live row (WHERE-stub predicate).
        repo.importCollectionsBatch(tenant, List.of(
            Map.of("name", name, "content_type", "docs", "owner_id", "nexus-x",
                   "embedding_model", "voyage-context-3", "model_version", "v9")));
        var stillLive = repo.getCollection(tenant, name);
        assertThat(stillLive.get("content_type")).as("live row must not be overwritten").isEqualTo("code");
    }

    @Test @Order(226)
    void importChunksBatch_emptyAndNull_returnZero() {
        assertThat(importChunksBatchSeeded("etl-batch-chunk-tenant", "bch.1",
            "knowledge__etl-batch-chunk__v1", List.of())).isZero();
        assertThat(importChunksBatchSeeded("etl-batch-chunk-tenant", "bch.1",
            "knowledge__etl-batch-chunk__v1", null)).isZero();
    }

    // ── writeManifestMany (nexus-u2kwq) ─────────────────────────────────────────

    @Test @Order(230)
    void writeManifestMany_twoDocs_replaceAndChunkCountUpdated() {
        // RDR-191 (Hal ruling 2026-08-12): writeManifestMany's `collection`
        // is ONE value applying to every doc in the call (the caller batches
        // per collection) — no per-doc ghost/collection axis remains.
        String coll = "knowledge__wmm-batch__v1";
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "wmm.1", "title", "WMM Doc1",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "wmm.2", "title", "WMM Doc2",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));

        var result = writeManifestManySeeded(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "wmm.1", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("wmm1aa"), "chunk_index", 0),
                Map.<String, Object>of("position", 1, "chash", ch("wmm1bb"), "chunk_index", 1))),
            Map.<String, Object>of("doc_id", "wmm.2", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("wmm2aa"), "chunk_index", 0)))), coll);

        assertThat(result.get("docs")).isEqualTo(2);
        assertThat(result.get("rows")).isEqualTo(3);
        assertThat((List<?>) result.get("failed_doc_ids")).isEmpty();

        // Equal to two independent writeManifest calls: positions + chashes intact.
        var m1 = repo.getManifest(TENANT_A, "wmm.1");
        assertThat(m1).hasSize(2);
        assertThat(m1.get(0).get("chash")).isEqualTo(ch("wmm1aa"));
        assertThat(m1.get(1).get("chash")).isEqualTo(ch("wmm1bb"));
        assertThat(repo.getManifest(TENANT_A, "wmm.2")).hasSize(1);

        // chunk_count folded into the same per-doc transaction.
        assertThat(repo.getDocument(TENANT_A, "wmm.1").get("chunk_count")).isEqualTo(2);
        assertThat(repo.getDocument(TENANT_A, "wmm.2").get("chunk_count")).isEqualTo(1);
    }

    @Test @Order(231)
    void writeManifestMany_replaceShrinks_exactRowsAndChunkCount() {
        // RDR-191 (Hal ruling 2026-08-12): the caller-supplied `collection`
        // is what makes this write resolvable now — the document's own
        // physical_collection is no longer read at all.
        String coll = "knowledge__wmm3__v1";
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "wmm.3", "title", "WMM Doc3",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));
        // Seed 5 rows.
        writeManifestManySeeded(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "wmm.3", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("wmm3a"), "chunk_index", 0),
                Map.<String, Object>of("position", 1, "chash", ch("wmm3b"), "chunk_index", 1),
                Map.<String, Object>of("position", 2, "chash", ch("wmm3c"), "chunk_index", 2),
                Map.<String, Object>of("position", 3, "chash", ch("wmm3d"), "chunk_index", 3),
                Map.<String, Object>of("position", 4, "chash", ch("wmm3e"), "chunk_index", 4)))), coll);
        assertThat(repo.getManifest(TENANT_A, "wmm.3")).hasSize(5);
        assertThat(repo.getDocument(TENANT_A, "wmm.3").get("chunk_count")).isEqualTo(5);

        // Replace with only 2 rows — REPLACE shrinks; exactly 2 remain, chunk_count 2.
        writeManifestManySeeded(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "wmm.3", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("wmm3new0"), "chunk_index", 0),
                Map.<String, Object>of("position", 1, "chash", ch("wmm3new1"), "chunk_index", 1)))), coll);

        var got = repo.getManifest(TENANT_A, "wmm.3");
        assertThat(got).hasSize(2);
        assertThat(got.stream().map(r -> (String) r.get("chash")).toList())
            .containsExactlyInAnyOrder(ch("wmm3new0"), ch("wmm3new1"));
        assertThat(repo.getDocument(TENANT_A, "wmm.3").get("chunk_count")).isEqualTo(2);
    }

    @Test @Order(232)
    void writeManifestMany_violatingRow_isolatedToFailedDocIds() {
        // RDR-191 (Hal ruling 2026-08-12): writeManifestMany stamps its
        // caller-supplied `collection` on EVERY doc's rows unconditionally —
        // there is no ghost/registered-collection axis left to set up.
        // wmm.bad's missing-chash row still hits the (unrelated) NOT NULL
        // violation on `chash` and rolls back only its own per-doc
        // transaction; wmm.good is unaffected (cross-doc isolation) — the
        // actual subject of this test.
        String coll = "knowledge__wmm-isolation__voyage-context-3__v1";
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "wmm.good", "title", "WMM Good",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "wmm.bad", "title", "WMM Bad",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));

        // wmm.bad carries a row with a missing chash (NOT NULL violation) -> its own
        // transaction rolls back; wmm.good is unaffected (cross-doc isolation).
        Map<String, Object> badRow = new LinkedHashMap<>();
        badRow.put("position", 0);
        badRow.put("chunk_index", 0); // chash intentionally absent -> null

        var result = writeManifestManySeeded(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "wmm.good", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("wmmgood"), "chunk_index", 0))),
            Map.<String, Object>of("doc_id", "wmm.bad", "rows", List.<Map<String, Object>>of(badRow))), coll);

        assertThat(result.get("docs")).isEqualTo(1);
        assertThat(result.get("rows")).isEqualTo(1);
        @SuppressWarnings("unchecked")
        List<String> failed = (List<String>) result.get("failed_doc_ids");
        assertThat(failed).containsExactly("wmm.bad");

        assertThat(repo.getManifest(TENANT_A, "wmm.good")).hasSize(1);
        assertThat(repo.getDocument(TENANT_A, "wmm.good").get("chunk_count")).isEqualTo(1);
        assertThat(repo.getManifest(TENANT_A, "wmm.bad")).isEmpty();
        assertThat(repo.getDocument(TENANT_A, "wmm.bad").get("chunk_count")).isEqualTo(0);
    }

    @Test @Order(233)
    void writeManifestMany_emptyDocsList_noOp() {
        var result = writeManifestManySeeded(TENANT_A, List.of(), "knowledge__wmm-empty__v1");
        assertThat(result.get("docs")).isEqualTo(0);
        assertThat(result.get("rows")).isEqualTo(0);
        assertThat((List<?>) result.get("failed_doc_ids")).isEmpty();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // registerDocumentMany (nexus-9dvqy, duoak.11 sink #2)
    // ══════════════════════════════════════════════════════════════════════════

    private static Map<String, Object> regDoc(String title, String filePath) {
        return Map.of("title", title, "content_type", "code", "corpus", "code",
                      "file_path", filePath, "physical_collection", "code__x__v1");
    }

    @Test @Order(300)
    void registerDocumentMany_contiguousSeqBlock() {
        final String prefix = "rm-block";
        var tumblers = repo.registerDocumentMany(TENANT_A, prefix, List.of(
            regDoc("a", "a.py"), regDoc("b", "b.py"), regDoc("c", "c.py")));
        // Contiguous, input-order, starting at seq 1 for a fresh owner.
        assertThat(tumblers).containsExactly(prefix + ".1", prefix + ".2", prefix + ".3");
        // next_seq advanced by exactly 3: a following single register gets .4.
        String next = repo.registerDocument(TENANT_A, prefix, regDoc("d", "d.py"));
        assertThat(next).isEqualTo(prefix + ".4");
    }

    @Test @Order(310)
    void writeManifestMany_chashCheckViolation_reasonNamesConstraint() {
        // nexus-fhhwf acceptance: a doc violating the chash CHECK gets a
        // structured reason naming the constraint + sqlstate 23514, not a
        // bare id. Repo-level deliberately: the HTTP boundary now 400s a
        // non-canonical chash before any txn (nexus-z4skl), so the DB CHECK
        // is the belt for writers that bypass the handler.
        //
        // POLARITY NOTE (RDR-180, nexus-jxizy.2): this file used length(chash)=32
        // TEXT — "a".repeat(32) (32 hex chars) was the GOOD case and
        // "b".repeat(64) was the CHECK-violating BAD case. The column is bytea(32)
        // now with CHECK octet_length(chash)=32 (32 BYTES): "a".repeat(32) decodes
        // to 16 bytes (now the violator) and "b".repeat(64) decodes to the full
        // 32-byte canonical digest (now the passing case) — the two fixtures swap
        // roles, not just their chash literals.
        String prefix = "fhhwf-check";
        var tumblers = repo.registerDocumentMany(TENANT_A, prefix, List.of(
            regDoc("ok doc", "ok.py"), regDoc("bad doc", "bad.py")));
        var result = writeManifestManySeeded(TENANT_A, List.of(
            Map.of("doc_id", tumblers.get(0), "rows", List.of(
                Map.of("position", 0, "chash", "b".repeat(64)))),
            Map.of("doc_id", tumblers.get(1), "rows", List.of(
                Map.of("position", 0, "chash", "a".repeat(32))))), "code__fhhwf-check__voyage-code-3__v1");
        assertThat(result.get("docs")).isEqualTo(1);
        assertThat(result.get("failed_doc_ids")).isEqualTo(List.of(tumblers.get(1)));
        @SuppressWarnings("unchecked")
        var failed = (List<Map<String, Object>>) result.get("failed");
        assertThat(failed).hasSize(1);
        assertThat(failed.get(0).get("doc_id")).isEqualTo(tumblers.get(1));
        assertThat((String) failed.get(0).get("reason"))
            .contains("check constraint violation")
            .contains("chash");   // constraint name names the column/length rule
        assertThat(failed.get(0).get("sqlstate")).isEqualTo("23514");
    }

    @Test @Order(307)
    void registerDocumentMany_fullPage_profileBaseline() {
        // nexus-oub13: local SQL baseline for the live ~38s/page observation.
        // Registers a real 1000-doc page into a catalog pre-seeded with 5,000
        // rows (STORED fts tsvector + GIN + 5 btree indexes all pay per row).
        // NO wall-clock assertion (shared-runner flake class, nexus-77fqp) —
        // the register_many_timing log line this exercises IS the deliverable;
        // correctness asserts only. Local reference on a dev box: the full
        // page lands in the low hundreds of ms, ~100x under the live number,
        // which localizes the live sink OFF the SQL path (WAN/pooler/client
        // stages — see the bead).
        final String prefix = "rm-profile";
        for (int page = 0; page < 5; page++) {
            java.util.List<Map<String, Object>> seed = new java.util.ArrayList<>();
            for (int i = 0; i < 1000; i++) {
                int n = page * 1000 + i;
                seed.add(regDoc("seed doc " + n + " with a realistic title string",
                                "src/pkg" + (n % 40) + "/mod" + n + ".py"));
            }
            var got = repo.registerDocumentMany(TENANT_A, prefix, seed);
            assertThat(got).hasSize(1000);
        }
        // The measured page: 1000 fresh docs against 5k existing rows.
        java.util.List<Map<String, Object>> pageDocs = new java.util.ArrayList<>();
        for (int i = 0; i < 1000; i++) {
            pageDocs.add(regDoc("measured doc " + i,
                                "src/measured/m" + i + ".py"));
        }
        long t0 = System.nanoTime();
        var tumblers = repo.registerDocumentMany(TENANT_A, prefix, pageDocs);
        long ms = (System.nanoTime() - t0) / 1_000_000;
        assertThat(tumblers).hasSize(1000);
        assertThat(tumblers.get(0)).isEqualTo(prefix + ".5001");
        assertThat(tumblers.get(999)).isEqualTo(prefix + ".6000");
        // Idempotent re-send of the same page: no new seq consumed, same tumblers.
        var again = repo.registerDocumentMany(TENANT_A, prefix, pageDocs);
        assertThat(again).isEqualTo(tumblers);
        // The measured wall lands in register_many_timing's total_ms log line
        // (structured logging convention); `ms` is asserted only for sanity of
        // the timer plumbing, never as a perf bound (nexus-77fqp flake class).
        assertThat(ms).isNotNegative();
    }

    @Test @Order(301)
    void registerDocumentMany_mixedNewAndExisting_preservesOrderAndSkipsSeqForExisting() {
        final String prefix = "rm-mixed";
        // Pre-register one doc via the single-doc path.
        String existing = repo.registerDocument(TENANT_A, prefix, regDoc("keep", "keep.py"));
        assertThat(existing).isEqualTo(prefix + ".1");
        // Batch: [existing (same file_path), new, new] — existing returns its tumbler,
        // only the two new docs consume the block; order preserved.
        var tumblers = repo.registerDocumentMany(TENANT_A, prefix, List.of(
            regDoc("keep-again", "keep.py"), regDoc("new1", "n1.py"), regDoc("new2", "n2.py")));
        assertThat(tumblers).containsExactly(prefix + ".1", prefix + ".2", prefix + ".3");
        // No seq gap: next single register is .4, not .5.
        assertThat(repo.registerDocument(TENANT_A, prefix, regDoc("tail", "tail.py")))
            .isEqualTo(prefix + ".4");
    }

    @Test @Order(302)
    void registerDocumentMany_idempotentRebatch_returnsSameTumblers_noSeqGap() {
        final String prefix = "rm-idem";
        var first = repo.registerDocumentMany(TENANT_A, prefix, List.of(
            regDoc("x", "x.py"), regDoc("y", "y.py")));
        assertThat(first).containsExactly(prefix + ".1", prefix + ".2");
        // Re-batch the same file_paths — every doc is already LIVE, so no seq is drawn.
        var second = repo.registerDocumentMany(TENANT_A, prefix, List.of(
            regDoc("x-again", "x.py"), regDoc("y-again", "y.py")));
        assertThat(second).containsExactly(prefix + ".1", prefix + ".2");
        assertThat(repo.registerDocument(TENANT_A, prefix, regDoc("z", "z.py")))
            .isEqualTo(prefix + ".3");
    }

    @Test @Order(303)
    void registerDocumentMany_ownerAbsentBootstrap_createsOwner() {
        final String prefix = "rm-bootstrap";
        // Owner does not exist yet — the batch upserts it, then assigns from seq 1.
        var tumblers = repo.registerDocumentMany(TENANT_A, prefix, List.of(regDoc("only", "only.py")));
        assertThat(tumblers).containsExactly(prefix + ".1");
        assertThat(repo.getDocument(TENANT_A, prefix + ".1").get("title")).isEqualTo("only");
    }

    @Test @Order(304)
    void registerDocumentMany_emptyList_returnsEmpty() {
        assertThat(repo.registerDocumentMany(TENANT_A, "rm-empty", List.of())).isEmpty();
    }

    @Test @Order(306)
    void registerDocumentMany_idempotencyKeysOnSourceUriFirst() {
        // source_uri is checked BEFORE file_path (matching registerDocument);
        // a re-batch with the same source_uri returns the same tumbler and
        // consumes no sequence number, even if the file_path differs.
        final String prefix = "rm-srcuri";
        final String uri = "file:///tmp/rm-srcuri/doc.md";
        String first = repo.registerDocument(TENANT_A, prefix, Map.of(
            "title", "srcuri doc", "content_type", "rdr", "corpus", "rdr",
            "file_path", "orig.md", "source_uri", uri));
        assertThat(first).isEqualTo(prefix + ".1");
        // Batch with the SAME source_uri but a DIFFERENT file_path -> idempotent
        // on source_uri (first precedence), returns the existing tumbler.
        var tumblers = repo.registerDocumentMany(TENANT_A, prefix, List.of(
            Map.of("title", "srcuri doc renamed", "content_type", "rdr",
                   "corpus", "rdr", "file_path", "renamed.md", "source_uri", uri),
            Map.of("title", "brand new", "content_type", "rdr", "corpus", "rdr",
                   "file_path", "new.md", "source_uri", "file:///tmp/rm-srcuri/new.md")));
        assertThat(tumblers).containsExactly(prefix + ".1", prefix + ".2");
        // No seq gap: the existing source_uri consumed nothing; only "brand new" did.
        assertThat(repo.registerDocument(TENANT_A, prefix, Map.of(
            "title", "tail", "content_type", "rdr", "corpus", "rdr",
            "file_path", "tail.md", "source_uri", "file:///tmp/rm-srcuri/tail.md")))
            .isEqualTo(prefix + ".3");
    }

    @Test @Order(305)
    void registerDocumentMany_concurrentSameOwner_disjointGaplessBlocks() throws Exception {
        final String prefix = "rm-concurrent";
        final int perBatch = 5;
        // Bootstrap the owner first so both threads race only the next_seq FOR UPDATE claim.
        repo.registerDocumentMany(TENANT_A, prefix, List.of(regDoc("seed", "seed.py")));

        var pool = java.util.concurrent.Executors.newFixedThreadPool(2);
        try {
            java.util.concurrent.Callable<List<String>> task = () -> {
                long tid = Thread.currentThread().getId();
                var docs = new java.util.ArrayList<Map<String, Object>>();
                for (int i = 0; i < perBatch; i++) {
                    docs.add(regDoc("t" + tid + "-" + i, "t" + tid + "-" + i + ".py"));
                }
                return repo.registerDocumentMany(TENANT_A, prefix, docs);
            };
            var f1 = pool.submit(task);
            var f2 = pool.submit(task);
            var all = new java.util.ArrayList<String>();
            all.addAll(f1.get());
            all.addAll(f2.get());
            // 10 tumblers, all distinct (disjoint blocks) — the FOR UPDATE lock
            // serializes the two seq-block claims so neither overlaps.
            assertThat(all).hasSize(2 * perBatch);
            assertThat(new java.util.HashSet<>(all)).hasSize(2 * perBatch);
            // Gapless overall: seeds .2, both batches fill .2..(seed+10) with no hole.
            String next = repo.registerDocument(TENANT_A, prefix, regDoc("after", "after.py"));
            // seed consumed .1; 10 concurrent docs consumed .2..0.11; next is .12.
            assertThat(next).isEqualTo(prefix + ".12");
        } finally {
            pool.shutdownNow();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-vfef0 — register wire response created-vs-matched signal
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(311)
    void registerDocumentWithOutcome_freshInsert_reportsCreatedTrue() {
        final String prefix = "vfef0-fresh";
        var outcome = repo.registerDocumentWithOutcome(TENANT_A, prefix, regDoc("fresh", "fresh.py"));
        assertThat(outcome.tumbler()).isEqualTo(prefix + ".1");
        assertThat(outcome.created()).isTrue();
    }

    @Test @Order(312)
    void registerDocumentWithOutcome_sourceUriIdempotencyHit_reportsCreatedFalse() {
        final String prefix = "vfef0-srcuri";
        final String uri = "file:///tmp/vfef0-srcuri/doc.md";
        var first = repo.registerDocumentWithOutcome(TENANT_A, prefix, Map.of(
            "title", "srcuri doc", "content_type", "rdr", "corpus", "rdr",
            "file_path", "orig.md", "source_uri", uri));
        assertThat(first.created()).isTrue();
        // Re-registering the SAME source_uri (different file_path, matching the
        // leg's precedence order) must hand back the existing row untouched.
        var second = repo.registerDocumentWithOutcome(TENANT_A, prefix, Map.of(
            "title", "srcuri doc renamed", "content_type", "rdr", "corpus", "rdr",
            "file_path", "renamed.md", "source_uri", uri));
        assertThat(second.tumbler()).isEqualTo(first.tumbler());
        assertThat(second.created()).isFalse();
    }

    @Test @Order(313)
    void registerDocumentWithOutcome_filePathIdempotencyHit_reportsCreatedFalse() {
        final String prefix = "vfef0-fpath";
        var first = repo.registerDocumentWithOutcome(TENANT_A, prefix, regDoc("keep", "keep.py"));
        assertThat(first.created()).isTrue();
        // Re-registering the same file_path (no source_uri) must hit the
        // file_path idempotency leg, not mint a second row.
        var second = repo.registerDocumentWithOutcome(TENANT_A, prefix, regDoc("keep-again", "keep.py"));
        assertThat(second.tumbler()).isEqualTo(first.tumbler());
        assertThat(second.created()).isFalse();
    }

    @Test @Order(314)
    void registerDocumentWithOutcome_concurrentFirstPutRace_exactlyOneWinnerReportsCreated() throws Exception {
        // nexus-vfef0's core scenario: two callers race a genuinely NEW
        // (owner, source_uri) with no precheck between them. Pre-fix, BOTH
        // legs looked identical to the caller (a bare tumbler); the wire now
        // must tell the winner (created=true) from the loser (created=false)
        // so a caller's rollback-on-failure compensation never deletes the
        // winner's row out from under it.
        repo.upsertOwner(TENANT_A, Map.of(
            "tumbler_prefix", "vfef0-race", "name", "vfef0-race-owner", "owner_type", "repo",
            "repo_hash", "vfef0", "description", "", "repo_root", "",
            "head_hash", "", "next_seq", 0L));
        final String uri = "file:///tmp/vfef0-race/doc.md";
        var pool = java.util.concurrent.Executors.newFixedThreadPool(2);
        try {
            var start = new java.util.concurrent.CountDownLatch(1);
            java.util.concurrent.Callable<CatalogRepository.RegisterOutcome> task = () -> {
                start.await();
                return repo.registerDocumentWithOutcome(TENANT_A, "vfef0-race", Map.of(
                    "title", "race", "source_uri", uri, "file_path", "race.md"));
            };
            var f1 = pool.submit(task);
            var f2 = pool.submit(task);
            start.countDown();
            var a = f1.get();
            var b = f2.get();

            assertThat(a.tumbler()).as("both racers converge on one tumbler").isEqualTo(b.tumbler());
            assertThat(a.created() ^ b.created())
                .as("EXACTLY one of the two racers must report created=true (the winner) "
                    + "and the other created=false (the loser) — never both true (would let a "
                    + "caller's rollback delete the winner's live row) and never both false "
                    + "(the row would be unaccounted for)")
                .isTrue();
        } finally {
            pool.shutdownNow();
        }
    }

    @Test @Order(315)
    void registerDocumentManyWithOutcome_mixedNewAndExisting_perEntryCreatedFlag() {
        final String prefix = "vfef0-batch";
        // Pre-register one doc via the single-doc path.
        var pre = repo.registerDocumentWithOutcome(TENANT_A, prefix, regDoc("keep", "keep.py"));
        assertThat(pre.created()).isTrue();

        var outcomes = repo.registerDocumentManyWithOutcome(TENANT_A, prefix, List.of(
            regDoc("keep-again", "keep.py"),   // existing (file_path hit) -> created=false
            regDoc("new1", "n1.py"),            // genuinely new -> created=true
            regDoc("new2", "n2.py")));          // genuinely new -> created=true
        assertThat(outcomes).hasSize(3);
        assertThat(outcomes.get(0).tumbler()).isEqualTo(pre.tumbler());
        assertThat(outcomes.get(0).created()).isFalse();
        assertThat(outcomes.get(1).created()).isTrue();
        assertThat(outcomes.get(2).created()).isTrue();
        assertThat(outcomes.get(1).tumbler()).isNotEqualTo(outcomes.get(2).tumbler());
    }

    @Test @Order(316)
    void registerDocumentManyWithOutcome_intraBatchAlias_mirrorsFirstOccurrenceCreatedFlag() {
        final String prefix = "vfef0-batch-alias";
        var outcomes = repo.registerDocumentManyWithOutcome(TENANT_A, prefix, List.of(
            Map.of("title", "b1", "source_uri", "file:///vfef0-batch-alias/dup.md", "file_path", "dup.md"),
            Map.of("title", "b2", "source_uri", "file:///vfef0-batch-alias/dup.md", "file_path", "dup.md")));
        assertThat(outcomes).hasSize(2);
        assertThat(outcomes.get(0).tumbler()).isEqualTo(outcomes.get(1).tumbler());
        // The first occurrence's own INSERT lands (created=true); the
        // intra-batch alias never runs its own INSERT, so it mirrors that
        // same outcome rather than independently reporting false.
        assertThat(outcomes.get(0).created()).isTrue();
        assertThat(outcomes.get(1).created()).isTrue();
    }

    @Test @Order(317)
    void registerDocument_backCompatWrapper_unaffectedByOutcomeVariant() {
        // The bare-tumbler registerDocument/registerDocumentMany (the ~90
        // pre-existing call sites) must remain byte-for-byte unchanged by
        // the addition of the *WithOutcome variants.
        final String prefix = "vfef0-compat";
        String t1 = repo.registerDocument(TENANT_A, prefix, regDoc("a", "a.py"));
        String t2 = repo.registerDocument(TENANT_A, prefix, regDoc("a-again", "a.py"));
        assertThat(t1).isEqualTo(prefix + ".1");
        assertThat(t2).isEqualTo(t1);
        var many = repo.registerDocumentMany(TENANT_A, prefix, List.of(regDoc("b", "b.py")));
        assertThat(many).containsExactly(prefix + ".2");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-23wlw — tombstone visibility parity contract
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * nexus-23wlw: a tombstoned document is invisible to EVERY read.
     *
     * <p>THE DEFECT. {@code delete_document} tombstones (sets
     * {@code deleted_at}); it does not remove the row. The POINT lookups
     * filtered correctly — {@code getDocument}, {@code resolveMany}, the
     * {@code registerDocument} idempotency probes — but the LIST reads did
     * not. So {@code audit-membership --purge-non-canonical} reported
     * "Deleted 3 of 3" and {@code list_by_collection} still returned all 8:
     * a delete that reports success and appears not to have happened, which
     * reads as a broken delete rather than a broken list. Every count derived
     * from these reads was inflated by exactly the tombstone population.
     *
     * <p>SQLite has no equivalent because it HARD-deletes, so the row is
     * physically absent from {@code documents}. That is also why the fix is a
     * blanket filter rather than an {@code include_deleted} parameter: each of
     * these methods documents itself as replacing a {@code FROM documents}
     * SQLite query, and that table never contains tombstones. Matching it is
     * parity, not a new policy.
     *
     * <p>WHY NO CALLER LOSES ANYTHING, checked before filtering rather than
     * assumed: {@code nx catalog undelete} restores from a BACKUP FILE
     * (re-registering), not from live tombstones; {@code purge_trash} reads
     * {@code deleted_at IS NOT NULL} directly in PL/pgSQL; the catalog-004
     * manifest functions were already tombstone-aware in SQL. No surface in
     * this class exists to LIST the trash, so nothing here needed to see it.
     *
     * <p>The bead named nine sites. An audit of every {@code CATALOG_DOCUMENTS}
     * read found twenty-two unfiltered, including {@code countDocuments} (the
     * inflated-counts symptom itself) and
     * {@code lookupDocByCollectionAndPath}, which resolves a tumbler for
     * WRITES — a tombstone match there hands a caller a deleted document to
     * write into. Fixing the nine and leaving the rest is the recurring shape
     * of this bug: catalog-015 fixed the catalog and left memory, j0nec was
     * fixed in one file and left in twenty-three.
     *
     * <p>Exactly ONE read is deliberately unfiltered — {@code
     * requireDocumentExists} (formerly {@code physicalCollectionOf}, renamed
     * when RDR-191 removed its collection-resolution duty, now a pure
     * existence check), which is a manifest WRITE helper, not a reader.
     * Its exclusion is argued at its own declaration.
     */
    @Test
    void tombstonedDocument_isInvisibleToEveryRead() {
        String tenant = "tomb-tenant-" + System.nanoTime();
        String live = "9.1";
        String dead = "9.2";

        for (String[] d : new String[][] {
                {live, "Live Doc",  "live.py"},
                {dead, "Dead Doc",  "dead.py"}}) {
            repo.upsertDocument(tenant, Map.of(
                "tumbler",             d[0],
                "title",               d[1],
                "content_type",        "code",
                "corpus",              "tombcorpus",
                "physical_collection", "tombcoll",
                "file_path",           "/abs/" + d[2],
                "source_uri",          "file:///abs/" + d[2]));
        }
        repo.upsertLink(tenant, Map.of(
            "from_tumbler", live,
            "to_tumbler",   dead,
            "link_type",    "relates",
            "created_by",   "nexus-23wlw-test"));

        assertThat(repo.deleteDocument(tenant, dead))
            .as("precondition: the delete must actually tombstone one row")
            .isEqualTo(1);

        // Every list/lookup surface, named so a failure says WHICH one leaked.
        assertThat(tumblersOf(repo.listDocuments(tenant, 200, 0)))
            .as("listDocuments").containsExactly(live);
        assertThat(repo.countDocuments(tenant))
            .as("countDocuments — the inflated-counts symptom").isEqualTo(1);
        assertThat(tumblersOf(repo.documentsByCollection(tenant, "tombcoll", 0, 0)))
            .as("documentsByCollection — the bead's own repro").containsExactly(live);
        assertThat(tumblersOf(repo.documentsByFilePath(tenant, "/abs/dead.py", 0, 0)))
            .as("documentsByFilePath").isEmpty();
        assertThat(tumblersOf(repo.documentsBySourceUri(tenant, "file:///abs/dead.py", 0, 0)))
            .as("documentsBySourceUri").isEmpty();
        assertThat(tumblersOf(repo.documentsByOwner(tenant, "9", 0, 0)))
            .as("documentsByOwner").containsExactly(live);
        assertThat(tumblersOf(repo.documentsByOwnerAndFilePath(tenant, "9", "/abs/dead.py", 0, 0)))
            .as("documentsByOwnerAndFilePath").isEmpty();
        assertThat(tumblersOf(repo.documentsByContentType(tenant, "code", 0, 0)))
            .as("documentsByContentType").containsExactly(live);
        assertThat(tumblersOf(repo.documentsByCorpus(tenant, "tombcorpus", 0, 0)))
            .as("documentsByCorpus").containsExactly(live);
        assertThat(tumblersOf(repo.descendants(tenant, "9")))
            .as("descendants").containsExactly(live);
        assertThat(repo.lookupDocByCollectionAndPath(tenant, "tombcoll", "/abs/dead.py"))
            .as("lookupDocByCollectionAndPath — resolves a tumbler for WRITES, so a "
                + "tombstone match hands the caller a deleted document")
            .isNull();
        assertThat(tumblersOf(repo.searchDocuments(tenant, "Dead", null, 50)))
            .as("searchDocuments").isEmpty();
        assertThat(repo.chunkCountsForDocs(tenant, List.of(live, dead)))
            .as("chunkCountsForDocs").doesNotContainKey(dead);
        assertThat(tumblersOf(repo.docsWithAbsolutePaths(tenant)))
            .as("docsWithAbsolutePaths").containsExactly(live);
        assertThat(tumblersOf(repo.orphanedDocs(tenant)))
            .as("orphanedDocs — a tombstone must not be reported as an orphan")
            .doesNotContain(dead);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> nodes =
            (List<Map<String, Object>>) repo.graphBFS(
                tenant, List.of(live), List.of(), "both", 1).get("nodes");
        assertThat(tumblersOf(nodes))
            .as("graphBFS nodes — a tombstoned neighbour must not surface as a node")
            .doesNotContain(dead);

        long codeCount = repo.coverageByContentType(tenant, "9").stream()
            .filter(r -> "code".equals(r.get("content_type")))
            .mapToLong(r -> ((Number) r.get("total")).longValue())
            .sum();
        assertThat(codeCount).as("coverageByContentType").isEqualTo(1);

        // NON-VACUITY. Every assertion above is satisfied by a tenant that is
        // simply empty, so without this the whole test passes against a
        // seeding bug — and it would have passed against the ORIGINAL defect
        // too if `live` were missing for an unrelated reason.
        assertThat(tumblersOf(repo.documentsByCollection(tenant, "tombcoll", 0, 0)))
            .as("the LIVE doc is still returned — this test is not passing "
                + "because the tenant is empty")
            .containsExactly(live);
        assertThat(repo.getDocument(tenant, live))
            .as("and it is still individually resolvable").isNotNull();
    }

    /** Tumblers of a document-row list, in order, for the nexus-23wlw contract. */
    private static List<String> tumblersOf(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> String.valueOf(r.get("tumbler"))).toList();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-ybj1b — include_heuristic parity contract
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * nexus-ybj1b: graph traversal excludes {@code implements-heuristic} by
     * default and includes it on explicit opt-in.
     *
     * <p>THE DEFECT. {@code Catalog.graph}/{@code graph_many} have always
     * excluded heuristic edges by default. The HTTP client sent
     * {@code include_heuristic} with the comment "forwarded to service for
     * future support; currently informational", and the server read only
     * {@code link_types} — the string appeared NOWHERE in the Java module. So
     * BOTH directions were broken, which is why this test asserts both: the
     * default did not exclude, and the opt-in was indistinguishable from the
     * default.
     *
     * <p>Not a subtle ranking shift. The 2026-05-08 production probe measured
     * 15,490 heuristic edges out of 23,582 — 66% — with 500-660 inbound on a
     * single high-traffic RDR. That is the flood the local default exists to
     * suppress, silently reinstated for every user on the 6.0 default backend.
     *
     * <p>The third case is the one a deny-list implementation would get wrong:
     * an explicit {@code link_types} naming the heuristic type must WIN, since
     * the local contract trusts a caller who names types.
     */
    @Test
    void graphBFS_excludesHeuristicByDefault_andHonoursOptIn() {
        String tenant = "heur-tenant-" + System.nanoTime();
        String seed = "8.1", curated = "8.2", heuristic = "8.3", custom = "8.4";

        for (String t : List.of(seed, curated, heuristic, custom)) {
            repo.upsertDocument(tenant, Map.of(
                "tumbler", t, "title", "Doc " + t, "content_type", "code"));
        }
        for (String[] e : new String[][] {
                {curated,   "cites"},
                {heuristic, "implements-heuristic"},
                {custom,    "invented-by-a-user"}}) {
            repo.upsertLink(tenant, Map.of(
                "from_tumbler", seed, "to_tumbler", e[0],
                "link_type", e[1], "created_by", "nexus-ybj1b-test"));
        }

        assertThat(neighbours(repo.graphBFS(tenant, List.of(seed), List.of(), "both", 1, false)))
            .as("DEFAULT: the curated edge survives, the heuristic flood does not — "
                + "and a CUSTOM type is excluded too, because the contract is an "
                + "allow-list, not a deny-list")
            .contains(curated)
            .doesNotContain(heuristic)
            .doesNotContain(custom);

        assertThat(neighbours(repo.graphBFS(tenant, List.of(seed), List.of(), "both", 1, true)))
            .as("OPT-IN: include_heuristic reaches the query and lifts the filter "
                + "entirely — previously indistinguishable from the default")
            .contains(curated, heuristic, custom);

        assertThat(neighbours(repo.graphBFS(
                tenant, List.of(seed), List.of("implements-heuristic"), "both", 1, false)))
            .as("EXPLICIT TYPES WIN: naming the heuristic type returns it even with "
                + "includeHeuristic=false — the caller knows what they asked for")
            .containsExactly(heuristic);

        assertThat(neighbours(repo.graphBFS(tenant, List.of(seed), List.of(), "both", 1, false)))
            .as("non-vacuity: the traversal finds SOMETHING by default, so the "
                + "doesNotContain assertions above are not passing on an empty graph")
            .isNotEmpty();
    }

    /**
     * nexus-23wlw census: catalog document search folds diacritics, the half
     * catalog-015 left behind.
     *
     * <p>catalog-015 fixed this table's SEPARATOR divergence in 2026-07-13 and
     * stopped there, so {@code Godel} still did not find {@code Gödel} — the
     * third falsification of catalog-001's "PG >= FTS5 superset" claim and the
     * second on this table. Author is where it bites: academic names are where
     * diacritics live.
     *
     * <p>Measured against the LIVE catalog rather than assumed. Of 51 sampled
     * papers, five had non-ASCII authors but only ONE genuinely diverged
     * ({@code Ángel Plaza} — searching {@code Angel} found it under FTS5 and
     * returned nothing here). The other four — {@code Groß}, two
     * {@code Křížek}s, and a Cyrillic name — are unfolded by BOTH substrates
     * and already agreed. The boundary cases below pin exactly that, so the
     * fold cannot later be "improved" into unaccent and start returning rows
     * the baseline never would.
     */
    @Test
    void searchDocuments_foldsDiacritics_withinTheFts5Range() {
        String tenant = "dia-tenant-" + System.nanoTime();
        repo.upsertDocument(tenant, Map.of(
            "tumbler", "7.1", "title", "On Formally Undecidable Propositions",
            "author", "Kurt Gödel", "content_type", "paper", "corpus", "knowledge"));
        repo.upsertDocument(tenant, Map.of(
            "tumbler", "7.2", "title", "Numerical Methods",
            "author", "Sven Groß", "content_type", "paper", "corpus", "knowledge"));
        repo.upsertDocument(tenant, Map.of(
            "tumbler", "7.3", "title", "Mesh Refinement",
            "author", "Michal Křížek", "content_type", "paper", "corpus", "knowledge"));

        assertThat(tumblersOf(repo.searchDocuments(tenant, "Godel", null, 20)))
            .as("the measured divergence: an unaccented author query must hit")
            .contains("7.1");
        assertThat(tumblersOf(repo.searchDocuments(tenant, "Gödel", null, 20)))
            .as("non-vacuity: the ACCENTED spelling still matches, so folding "
                + "both sides is a superset rather than a swap")
            .contains("7.1");

        assertThat(tumblersOf(repo.searchDocuments(tenant, "Gross", null, 20)))
            .as("ß is NOT expanded — FTS5 does not expand it either, so this "
                + "MISS is parity, not a gap")
            .doesNotContain("7.2");
        assertThat(tumblersOf(repo.searchDocuments(tenant, "Krizek", null, 20)))
            .as("Latin Extended-A is NOT folded — outside FTS5's default "
                + "remove_diacritics=1 range; unaccent would fold it and "
                + "overshoot the baseline")
            .doesNotContain("7.3");

        assertThat(tumblersOf(repo.searchDocuments(tenant, "Mesh", null, 20)))
            .as("non-vacuity: plain ASCII search still works, so the MISSes "
                + "above are not a dead search path")
            .contains("7.3");
    }

    /**
     * The non-seed nodes reached by a {@link CatalogRepository#graphBFS} result.
     * Seeds are always present in {@code visited}, so comparing raw node sets
     * would report a hit for a neighbour that was never actually traversed.
     */
    private static List<String> neighbours(Map<String, Object> graph) {
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> nodes = (List<Map<String, Object>>) graph.get("nodes");
        return tumblersOf(nodes).stream().filter(t -> !"8.1".equals(t)).toList();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // nexus-cw262 — owner deactivate/reactivate (soft-delete), the 7kl32
    // dead-owner GC mutation arm's engine half.
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(320)
    void deactivateOwner_excludesFromDefaultListOwnersAndByType_visibleWithIncludeDeactivated() {
        final String T = "cat-tenant-cw262-a";
        repo.upsertOwner(T, Map.of(
            "tumbler_prefix", "1",
            "name", "dead-repo",
            "owner_type", "repo",
            "repo_root", "/tmp/dead-repo"));

        // Sanity: visible before deactivation, on both read paths this bead touches.
        assertThat(repo.listOwners(T)).extracting(o -> o.get("tumbler_prefix")).contains("1");
        assertThat(repo.ownersByType(T, "repo")).extracting(o -> o.get("tumbler_prefix")).contains("1");

        int n = repo.deactivateOwner(T, "1");
        assertThat(n).as("deactivateOwner reports the row it actually flipped").isEqualTo(1);

        // The entire point: default reads now exclude it.
        assertThat(repo.listOwners(T)).extracting(o -> o.get("tumbler_prefix")).doesNotContain("1");
        assertThat(repo.ownersByType(T, "repo")).extracting(o -> o.get("tumbler_prefix")).doesNotContain("1");

        // include_deactivated=true (the audit escape hatch) still sees it, with
        // deactivated_at populated so a caller can tell WHICH rows are dead.
        var all = repo.listOwners(T, true);
        var found = all.stream().filter(o -> "1".equals(o.get("tumbler_prefix"))).findFirst();
        assertThat(found).isPresent();
        assertThat(found.get().get("deactivated_at")).isNotNull();

        var allByType = repo.ownersByType(T, "repo", true);
        assertThat(allByType).anySatisfy(o -> {
            assertThat(o.get("tumbler_prefix")).isEqualTo("1");
            assertThat(o.get("deactivated_at")).isNotNull();
        });
    }

    @Test @Order(321)
    void deactivateOwner_isIdempotent_secondCallReturnsZero() {
        final String T = "cat-tenant-cw262-b";
        repo.upsertOwner(T, Map.of("tumbler_prefix", "1", "name", "idem-repo", "owner_type", "repo"));
        assertThat(repo.deactivateOwner(T, "1")).isEqualTo(1);
        // AND deactivated_at IS NULL guard: double-deactivate is a no-op, does
        // not reset the timestamp (same idempotency contract as deleteDocument).
        assertThat(repo.deactivateOwner(T, "1")).isEqualTo(0);
    }

    @Test @Order(322)
    void deactivateOwner_unknownPrefix_returnsZero() {
        assertThat(repo.deactivateOwner("cat-tenant-cw262-c", "999.999")).isEqualTo(0);
    }

    @Test @Order(323)
    void reactivateOwner_clearsFlag_ownerVisibleAgainInDefaultList() {
        final String T = "cat-tenant-cw262-d";
        repo.upsertOwner(T, Map.of("tumbler_prefix", "1", "name", "resurrected-repo", "owner_type", "repo"));
        assertThat(repo.deactivateOwner(T, "1")).isEqualTo(1);
        assertThat(repo.listOwners(T)).extracting(o -> o.get("tumbler_prefix")).doesNotContain("1");

        int n = repo.reactivateOwner(T, "1");
        assertThat(n).as("reactivateOwner reports the row it actually flipped").isEqualTo(1);
        assertThat(repo.listOwners(T)).extracting(o -> o.get("tumbler_prefix")).contains("1");

        // Idempotent the other direction too: already-active is a no-op.
        assertThat(repo.reactivateOwner(T, "1")).isEqualTo(0);
    }

    @Test @Order(324)
    void upsertOwner_onExistingDeactivatedOwner_reactivatesAutomatically() {
        // The self-heal contract ownerUpdateSet documents: a live re-registration
        // through the normal `nx index repo` path is affirmative evidence the
        // owner is back in use, so it clears deactivated_at with no separate
        // /owners/reactivate call needed.
        final String T = "cat-tenant-cw262-e";
        repo.upsertOwner(T, Map.of(
            "tumbler_prefix", "1", "name", "revived-repo", "owner_type", "repo",
            "repo_hash", "revived-hash"));
        assertThat(repo.deactivateOwner(T, "1")).isEqualTo(1);
        assertThat(repo.listOwners(T)).extracting(o -> o.get("tumbler_prefix")).doesNotContain("1");

        // Converge-on-identity re-registration (same name+owner_type, no
        // explicit tumbler_prefix) -- the exact path a re-cloned/remounted
        // repo takes through `nx index repo`.
        repo.upsertOwner(T, Map.of(
            "name", "revived-repo", "owner_type", "repo", "repo_hash", "revived-hash",
            "description", "back from the dead"));

        assertThat(repo.listOwners(T)).extracting(o -> o.get("tumbler_prefix")).contains("1");
        var revived = repo.listOwners(T).stream()
            .filter(o -> "1".equals(o.get("tumbler_prefix"))).findFirst();
        assertThat(revived).isPresent();
        assertThat(revived.get().get("deactivated_at")).isNull();
        assertThat(revived.get().get("description")).isEqualTo("back from the dead");
    }

    @Test @Order(325)
    void deactivateOwner_rlsIsolation_cannotDeactivateAnotherTenantsOwner() {
        final String TA = "cat-tenant-cw262-rls-a";
        final String TB = "cat-tenant-cw262-rls-b";
        repo.upsertOwner(TB, Map.of("tumbler_prefix", "1", "name", "tenant-b-repo", "owner_type", "repo"));

        // TENANT A attempting to deactivate TENANT B's owner prefix must affect
        // nothing: FORCE ROW LEVEL SECURITY scopes the UPDATE's WHERE to tenant
        // A's own rows, so the cross-tenant tumbler_prefix simply matches no row.
        int n = repo.deactivateOwner(TA, "1");
        assertThat(n).isEqualTo(0);

        // Tenant B's owner remains active and visible in its own tenant scope --
        // the no-op above did not leak a partial/cross-tenant mutation either.
        assertThat(repo.listOwners(TB)).extracting(o -> o.get("tumbler_prefix")).contains("1");
        var b = repo.listOwners(TB, true).stream()
            .filter(o -> "1".equals(o.get("tumbler_prefix"))).findFirst();
        assertThat(b).isPresent();
        assertThat(b.get().get("deactivated_at")).isNull();
    }
}
