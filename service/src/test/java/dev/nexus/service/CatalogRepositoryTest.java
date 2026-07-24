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
            // RDR-159 P-1b: manifest functions (catalog-004) + chunks_384 read so the
            // svc role can invoke manifestBackfill/manifestOrphans (SECURITY INVOKER).
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.manifest_backfill() TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.manifest_orphans(int) TO " + SVC_ROLE);
            for (String ct : new String[]{"chunks_384", "chunks_768", "chunks_1024"}) {
                su.createStatement().execute(
                    "GRANT SELECT ON nexus." + ct + " TO " + SVC_ROLE);
            }
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
        var hit = repo.documentsByOwnerAndFilePath(TENANT_A, "7", "owner7/b.pdf");
        assertThat(hit).hasSize(1);
        assertThat(hit.get(0).get("tumbler")).isEqualTo("7.2");

        // Brand-new path under a POPULATED owner: zero (this is what stops the
        // corruption — the client no longer receives docs[0] of the owner).
        var miss = repo.documentsByOwnerAndFilePath(TENANT_A, "7", "owner7/brand-new.pdf");
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

    @Test @Order(16)
    void document_documentsByCollection() {
        repo.upsertDocument(TENANT_A, Map.of(
            "tumbler", "4.1",
            "title", "In Collection",
            "content_type", "paper",
            "corpus", "knowledge",
            "physical_collection", "knowledge__unit_test_coll"
        ));
        var docs = repo.documentsByCollection(TENANT_A, "knowledge__unit_test_coll");
        assertThat(docs).hasSize(1);
        assertThat(docs.get(0).get("tumbler")).isEqualTo("4.1");
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

    // ══════════════════════════════════════════════════════════════════════════
    // MANIFEST
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(50)
    void manifest_writeAndGet() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.1", "title", "Manifest Doc",
            "content_type", "paper", "corpus", "knowledge"));

        var rows = List.of(
            Map.<String, Object>of("position", 0, "chash", ch("aaaa"), "chunk_index", 0,
                "line_start", 1, "line_end", 10, "char_start", 0, "char_end", 100),
            Map.<String, Object>of("position", 1, "chash", ch("bbbb"), "chunk_index", 1,
                "line_start", 11, "line_end", 20, "char_start", 100, "char_end", 200)
        );
        repo.writeManifest(TENANT_A, "mfst.1", rows);

        var got = repo.getManifest(TENANT_A, "mfst.1");
        assertThat(got).hasSize(2);
        assertThat(got.get(0).get("chash")).isEqualTo(ch("aaaa"));
        assertThat(got.get(1).get("chash")).isEqualTo(ch("bbbb"));
    }

    @Test @Order(51)
    void manifest_writeIsAtomic_replacesExisting() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.2", "title", "Replace Doc",
            "content_type", "paper", "corpus", "knowledge"));
        // Write initial
        repo.writeManifest(TENANT_A, "mfst.2", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("old"), "chunk_index", 0)
        ));
        // Replace with new set
        repo.writeManifest(TENANT_A, "mfst.2", List.of(
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
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.3", "title", "Purge Doc",
            "content_type", "paper", "corpus", "knowledge"));
        repo.writeManifest(TENANT_A, "mfst.3", List.of(
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
        repo.writeManifest(TENANT_A, "mfst.purge2", List.of(
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
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.wcnt", "title", "Write Count Doc",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));
        repo.writeManifest(TENANT_A, "mfst.wcnt", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("wc0"), "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", ch("wc1"), "chunk_index", 1),
            Map.<String, Object>of("position", 2, "chash", ch("wc2"), "chunk_index", 2)
        ));
        var doc = repo.getDocument(TENANT_A, "mfst.wcnt");
        assertThat(((Number) doc.get("chunk_count")).intValue())
            .as("writeManifest must fold chunk_count = rows.size()")
            .isEqualTo(3);
        // And the REPLACE shrink folds too.
        repo.writeManifest(TENANT_A, "mfst.wcnt", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("wc9"), "chunk_index", 0)
        ));
        assertThat(((Number) repo.getDocument(TENANT_A, "mfst.wcnt").get("chunk_count")).intValue())
            .isEqualTo(1);
    }

    @Test @Order(53)
    void manifest_chashesForCollection() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.4", "title", "Chash For Collection",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__chash_test"));
        repo.writeManifest(TENANT_A, "mfst.4", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("cfccc0"), "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", ch("cfccc1"), "chunk_index", 1)
        ));
        Set<String> chashes = repo.chashesForCollection(TENANT_A, "knowledge__chash_test");
        assertThat(chashes).containsExactlyInAnyOrder(ch("cfccc0"), ch("cfccc1"));
    }

    @Test @Order(54)
    void manifest_resyncChunkCount() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.5", "title", "Resync Doc",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));
        repo.writeManifest(TENANT_A, "mfst.5", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("rsync0"), "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", ch("rsync1"), "chunk_index", 1),
            Map.<String, Object>of("position", 2, "chash", ch("rsync2"), "chunk_index", 2)
        ));
        // De-sync the count deliberately (writeManifest itself now folds it —
        // nexus-b6enc F5 — so force a wrong value to prove resync repairs).
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.5", "title", "Resync Doc",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 99));
        repo.resyncChunkCount(TENANT_A, "mfst.5");
        var doc = repo.getDocument(TENANT_A, "mfst.5");
        assertThat(doc.get("chunk_count")).isEqualTo(3);
    }

    // ── nexus-7lm3q: batch manifest/resolve endpoints ────────────────────────

    @Test @Order(55)
    void manifest_getManifestMany_batchFetchesAllDocs() {
        // Seed two docs each with two chunks
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "gmm.1", "title", "GMM Doc1",
            "content_type", "paper", "corpus", "knowledge"));
        repo.writeManifest(TENANT_A, "gmm.1", List.of(
            Map.<String, Object>of("position", 0, "chash", ch("gmm1aa"), "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", ch("gmm1bb"), "chunk_index", 1)
        ));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "gmm.2", "title", "GMM Doc2",
            "content_type", "paper", "corpus", "knowledge"));
        repo.writeManifest(TENANT_A, "gmm.2", List.of(
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
        repo.writeManifest(TENANT_B, "gmmiso.1", List.of(
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
        assertThat(counts.get("catalog_collections_deleted")).as("registry X deleted").isEqualTo(1);
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

        // Seed a parent document (FK target)
        repo.importDocument(etlTenant, Map.of(
            "tumbler", docId, "title", "Chunk Conv Doc",
            "content_type", "paper", "corpus", "knowledge"
        ));

        // chash must be the full 64-hex canonical digest (RDR-180: chunks_*/manifest
        // columns are bytea(32) now, CHECK octet_length=32)
        String chashV1 = ch("chashV1");
        String chashV2 = ch("chashV2"); // different

        // Initial chunk import
        repo.importChunk(etlTenant, docId, Map.of(
            "position", 0, "chash", chashV1, "chunk_index", 0,
            "line_start", 1, "line_end", 10, "char_start", 0, "char_end", 200
        ));
        var before = repo.getManifest(etlTenant, docId);
        assertThat(before).hasSize(1);
        assertThat(before.get(0).get("chash")).isEqualTo(chashV1);

        // Re-import same (tenant, doc, position) with a DIFFERENT chash — convergence
        repo.importChunk(etlTenant, docId, Map.of(
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

        repo.importDocument(etlTenant, Map.of(
            "tumbler", docId, "title", "Chunk Idem Doc",
            "content_type", "paper", "corpus", "knowledge"
        ));

        String chashStable = ch("chashStable"); // full 64-hex canonical digest
        Map<String, Object> chunk = Map.of(
            "position", 0, "chash", chashStable, "chunk_index", 0,
            "line_start", 5, "line_end", 15, "char_start", 10, "char_end", 300
        );
        repo.importChunk(etlTenant, docId, chunk);
        repo.importChunk(etlTenant, docId, chunk); // exact same values — must be stable

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

        // last_indexed = MAX("2026-01-01", "2026-06-01", "2026-03-15") = "2026-06-01..."
        assertThat(meta.get("last_indexed")).isEqualTo("2026-06-01T12:00:00");
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

        // TENANT_X must see only its own rows: last_indexed="2026-05-01T00:00:00", orphan_count=2
        var metaX = repo.collectionHealthMeta(tenantX, sharedColl);
        assertThat(metaX.get("last_indexed")).isEqualTo("2026-05-01T00:00:00");
        assertThat(metaX.get("orphan_count")).isEqualTo(2L);

        // TENANT_Y must see only its own rows: last_indexed="2026-06-07T10:00:00", orphan_count=1
        var metaY = repo.collectionHealthMeta(tenantY, sharedColl);
        assertThat(metaY.get("last_indexed")).isEqualTo("2026-06-07T10:00:00");
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
    // RDR-159 P-1b (nexus-avjdd): manifest backfill + orphans callables
    // ══════════════════════════════════════════════════════════════════════════

    // Dedicated tenant so counts are EXACT (== N): no other test writes here.
    private static final String TENANT_MIG = "mig-tenant-iso";
    private static final String MIG_COLLECTION_384 =
        "knowledge__mig-owner__minilm-l6-v2-384__v1";

    @Test @Order(200)
    void migration_manifestBackfill_stamps_null_collection_then_orphans_detected() {
        // A 384-model doc with ONE manifest row whose collection is NULL and
        // NO chunk row in chunks_384. nexus-x6kdz: writeManifest now stamps
        // collection AT WRITE TIME, so the legacy NULL shape backfill exists
        // for must be seeded directly (the pre-fix writer's output).
        repo.upsertDocument(TENANT_MIG, Map.of(
            "tumbler", "mforph.1",
            "title", "Orphan Source",
            "content_type", "knowledge",
            "corpus", "knowledge",
            "physical_collection", MIG_COLLECTION_384
        ));
        repo.writeManifest(TENANT_MIG, "mforph.1", List.of(
            Map.<String, Object>of(
                "position", 0, "chash", ch("f00d"),
                "chunk_index", 0)
        ));
        // The write-time stamp is the new contract:
        try (var conn = pg.createConnection(""); var st = conn.createStatement()) {
            var rs = st.executeQuery(
                "SELECT collection FROM nexus.catalog_document_chunks "
                + "WHERE tenant_id = '" + TENANT_MIG + "' AND doc_id = 'mforph.1'");
            rs.next();
            assertThat(rs.getString(1))
                .as("nexus-x6kdz: writer stamps collection at write time")
                .isEqualTo(MIG_COLLECTION_384);
            // Reset to the legacy NULL shape so backfill has real work:
            st.execute("UPDATE nexus.catalog_document_chunks SET collection = NULL "
                + "WHERE tenant_id = '" + TENANT_MIG + "' AND doc_id = 'mforph.1'");
        } catch (Exception e) {
            throw new RuntimeException(e);
        }

        // EXACTLY one NULL-collection row in this tenant → backfill stamps 1.
        long stamped = repo.manifestBackfill(TENANT_MIG);
        assertThat(stamped).isEqualTo(1L);

        // the row is now a 384 orphan (no chunks_384 row for that chash):
        // count and sample come from ONE transaction and agree.
        var report = repo.manifestOrphanReport(TENANT_MIG, 384, 100);
        assertThat(report.get("count")).isEqualTo(1L);
        @SuppressWarnings("unchecked")
        var orphans = (List<Map<String, Object>>) report.get("orphans");
        assertThat(orphans).hasSize(1);
        assertThat(orphans.get(0).get("doc_id")).isEqualTo("mforph.1");

        // count-only gate form agrees with the report.
        assertThat(repo.manifestOrphanCount(TENANT_MIG, 384)).isEqualTo(1L);
    }

    @Test @Order(201)
    void migration_manifestOrphans_tenant_isolated_via_rls() {
        // The orphan seeded for TENANT_MIG must be INVISIBLE to another tenant:
        // SECURITY INVOKER + FORCE RLS scopes the function to the GUC tenant
        // (refutes the 'cross-tenant scan' reading — the tenant arg is real).
        var report = repo.manifestOrphanReport(TENANT_B, 384, 100);
        assertThat(report.get("count")).isEqualTo(0L);
        assertThat(repo.manifestOrphanCount(TENANT_B, 384)).isEqualTo(0L);
    }

    @Test @Order(202)
    void migration_manifestOrphans_rejects_unsupported_dim_and_bad_limit() {
        // dim + limit validated in the repo (clean error, not a PL/pgSQL RAISE)
        org.assertj.core.api.Assertions.assertThatThrownBy(
            () -> repo.manifestOrphanReport(TENANT_MIG, 999, 100)
        ).isInstanceOf(IllegalArgumentException.class)
         .hasMessageContaining("unsupported dim");
        org.assertj.core.api.Assertions.assertThatThrownBy(
            () -> repo.manifestOrphanCount(TENANT_MIG, 512)
        ).isInstanceOf(IllegalArgumentException.class);
        org.assertj.core.api.Assertions.assertThatThrownBy(
            () -> repo.manifestOrphanReport(TENANT_MIG, 384, 0)
        ).isInstanceOf(IllegalArgumentException.class)
         .hasMessageContaining("limit");
    }

    @Test @Order(203)
    void migration_manifestBackfill_is_idempotent() {
        // Second backfill stamps nothing new — the function's WHERE collection
        // IS NULL guard never re-stamps an already-stamped row.
        long second = repo.manifestBackfill(TENANT_MIG);
        assertThat(second).isEqualTo(0L);
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
     * <p>The chunks_768 table has a FK to catalog_collections (COLLECTION col);
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
            var ps = su.prepareStatement(
                "INSERT INTO nexus.chunks_768"
                + " (tenant_id, collection, chash, chunk_text, embedding, metadata)"
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
        repo.writeManifest(SPAN_TENANT, SPAN_DOC_ID, List.of(
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
        repo.importDocument(tenant, Map.of("tumbler", docId, "title", "Batch Chunk Doc",
            "content_type", "paper", "corpus", "knowledge"));

        String chashV1 = ch("bchV1");
        String chashV2 = ch("bchV2");
        String chashV3 = ch("bchV3");

        int n = repo.importChunksBatch(tenant, docId, List.of(
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
        repo.importChunksBatch(tenant, docId, List.of(
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
        assertThat(repo.importChunksBatch("etl-batch-chunk-tenant", "bch.1", List.of())).isZero();
        assertThat(repo.importChunksBatch("etl-batch-chunk-tenant", "bch.1", null)).isZero();
    }

    // ── writeManifestMany (nexus-u2kwq) ─────────────────────────────────────────

    @Test @Order(230)
    void writeManifestMany_twoDocs_replaceAndChunkCountUpdated() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "wmm.1", "title", "WMM Doc1",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "wmm.2", "title", "WMM Doc2",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));

        var result = repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "wmm.1", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("wmm1aa"), "chunk_index", 0),
                Map.<String, Object>of("position", 1, "chash", ch("wmm1bb"), "chunk_index", 1))),
            Map.<String, Object>of("doc_id", "wmm.2", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("wmm2aa"), "chunk_index", 0)))));

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
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "wmm.3", "title", "WMM Doc3",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));
        // Seed 5 rows.
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "wmm.3", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("wmm3a"), "chunk_index", 0),
                Map.<String, Object>of("position", 1, "chash", ch("wmm3b"), "chunk_index", 1),
                Map.<String, Object>of("position", 2, "chash", ch("wmm3c"), "chunk_index", 2),
                Map.<String, Object>of("position", 3, "chash", ch("wmm3d"), "chunk_index", 3),
                Map.<String, Object>of("position", 4, "chash", ch("wmm3e"), "chunk_index", 4)))));
        assertThat(repo.getManifest(TENANT_A, "wmm.3")).hasSize(5);
        assertThat(repo.getDocument(TENANT_A, "wmm.3").get("chunk_count")).isEqualTo(5);

        // Replace with only 2 rows — REPLACE shrinks; exactly 2 remain, chunk_count 2.
        repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "wmm.3", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("wmm3new0"), "chunk_index", 0),
                Map.<String, Object>of("position", 1, "chash", ch("wmm3new1"), "chunk_index", 1)))));

        var got = repo.getManifest(TENANT_A, "wmm.3");
        assertThat(got).hasSize(2);
        assertThat(got.stream().map(r -> (String) r.get("chash")).toList())
            .containsExactlyInAnyOrder(ch("wmm3new0"), ch("wmm3new1"));
        assertThat(repo.getDocument(TENANT_A, "wmm.3").get("chunk_count")).isEqualTo(2);
    }

    @Test @Order(232)
    void writeManifestMany_violatingRow_isolatedToFailedDocIds() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "wmm.good", "title", "WMM Good",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "wmm.bad", "title", "WMM Bad",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));

        // wmm.bad carries a row with a missing chash (NOT NULL violation) -> its own
        // transaction rolls back; wmm.good is unaffected (cross-doc isolation).
        Map<String, Object> badRow = new LinkedHashMap<>();
        badRow.put("position", 0);
        badRow.put("chunk_index", 0); // chash intentionally absent -> null

        var result = repo.writeManifestMany(TENANT_A, List.of(
            Map.<String, Object>of("doc_id", "wmm.good", "rows", List.<Map<String, Object>>of(
                Map.<String, Object>of("position", 0, "chash", ch("wmmgood"), "chunk_index", 0))),
            Map.<String, Object>of("doc_id", "wmm.bad", "rows", List.<Map<String, Object>>of(badRow))));

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
        var result = repo.writeManifestMany(TENANT_A, List.of());
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
        var result = repo.writeManifestMany(TENANT_A, List.of(
            Map.of("doc_id", tumblers.get(0), "rows", List.of(
                Map.of("position", 0, "chash", "b".repeat(64)))),
            Map.of("doc_id", tumblers.get(1), "rows", List.of(
                Map.of("position", 0, "chash", "a".repeat(32))))));
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
}
