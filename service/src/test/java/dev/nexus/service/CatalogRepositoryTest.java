package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
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

    EmbeddedPostgres pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = EmbeddedPostgres.builder().start();

        // Phase 1: role creation (autoCommit=true; CREATE ROLE cannot run in txn).
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
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
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        // Phase 3: grant svc role access to all catalog tables (separate connection, all Liquibase DDL visible).
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
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
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        // HikariCP as svc role (bare JDBC URL + setUsername, NOT superuser, to enforce RLS).
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
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
        if (pg != null) pg.close();
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

        repo.upsertLink(TENANT_A, Map.of(
            "from_tumbler", "lnk.1",
            "to_tumbler", "lnk.2",
            "link_type", "cites",
            "from_span", "",
            "to_span", "",
            "created_by", "user",
            "created_at", "2026-06-01T00:00:00Z"
        ));
        var links = repo.linksFrom(TENANT_A, "lnk.1", null);
        assertThat(links).hasSize(1);
        assertThat(links.get(0).get("to_tumbler")).isEqualTo("lnk.2");
        assertThat(links.get(0).get("link_type")).isEqualTo("cites");
    }

    @Test @Order(31)
    void link_linksTo() {
        var links = repo.linksTo(TENANT_A, "lnk.2", null);
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
        var citesLinks = repo.linksFrom(TENANT_A, "lnk.1", "cites");
        assertThat(citesLinks).hasSize(1);
        assertThat(citesLinks.get(0).get("link_type")).isEqualTo("cites");

        var implLinks = repo.linksFrom(TENANT_A, "lnk.1", "implements");
        assertThat(implLinks).hasSize(1);
        assertThat(implLinks.get(0).get("link_type")).isEqualTo("implements");
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
        assertThat(repo.linksFrom(TENANT_A, "del.1", null)).hasSize(1);
        int deleted = repo.deleteLink(TENANT_A, "del.1", "del.2", "cites");
        assertThat(deleted).isEqualTo(1);
        assertThat(repo.linksFrom(TENANT_A, "del.1", null)).isEmpty();
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
            Map.<String, Object>of("position", 0, "chash", "aaaa0000", "chunk_index", 0,
                "line_start", 1, "line_end", 10, "char_start", 0, "char_end", 100),
            Map.<String, Object>of("position", 1, "chash", "bbbb1111", "chunk_index", 1,
                "line_start", 11, "line_end", 20, "char_start", 100, "char_end", 200)
        );
        repo.writeManifest(TENANT_A, "mfst.1", rows);

        var got = repo.getManifest(TENANT_A, "mfst.1");
        assertThat(got).hasSize(2);
        assertThat(got.get(0).get("chash")).isEqualTo("aaaa0000");
        assertThat(got.get(1).get("chash")).isEqualTo("bbbb1111");
    }

    @Test @Order(51)
    void manifest_writeIsAtomic_replacesExisting() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.2", "title", "Replace Doc",
            "content_type", "paper", "corpus", "knowledge"));
        // Write initial
        repo.writeManifest(TENANT_A, "mfst.2", List.of(
            Map.<String, Object>of("position", 0, "chash", "old0000", "chunk_index", 0)
        ));
        // Replace with new set
        repo.writeManifest(TENANT_A, "mfst.2", List.of(
            Map.<String, Object>of("position", 0, "chash", "new0000", "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", "new1111", "chunk_index", 1)
        ));
        var got = repo.getManifest(TENANT_A, "mfst.2");
        assertThat(got).hasSize(2);
        assertThat(got.stream().map(r -> (String) r.get("chash")).toList())
            .containsExactlyInAnyOrder("new0000", "new1111");
    }

    @Test @Order(52)
    void manifest_purge_removesAll() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.3", "title", "Purge Doc",
            "content_type", "paper", "corpus", "knowledge"));
        repo.writeManifest(TENANT_A, "mfst.3", List.of(
            Map.<String, Object>of("position", 0, "chash", "purge0000", "chunk_index", 0)
        ));
        assertThat(repo.getManifest(TENANT_A, "mfst.3")).hasSize(1);
        int deleted = repo.purgeManifest(TENANT_A, "mfst.3");
        assertThat(deleted).isEqualTo(1);
        assertThat(repo.getManifest(TENANT_A, "mfst.3")).isEmpty();
    }

    @Test @Order(53)
    void manifest_chashesForCollection() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.4", "title", "Chash For Collection",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__chash_test"));
        repo.writeManifest(TENANT_A, "mfst.4", List.of(
            Map.<String, Object>of("position", 0, "chash", "cfccc000", "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", "cfccc111", "chunk_index", 1)
        ));
        Set<String> chashes = repo.chashesForCollection(TENANT_A, "knowledge__chash_test");
        assertThat(chashes).containsExactlyInAnyOrder("cfccc000", "cfccc111");
    }

    @Test @Order(54)
    void manifest_resyncChunkCount() {
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "mfst.5", "title", "Resync Doc",
            "content_type", "paper", "corpus", "knowledge", "chunk_count", 0));
        repo.writeManifest(TENANT_A, "mfst.5", List.of(
            Map.<String, Object>of("position", 0, "chash", "rsync000", "chunk_index", 0),
            Map.<String, Object>of("position", 1, "chash", "rsync111", "chunk_index", 1),
            Map.<String, Object>of("position", 2, "chash", "rsync222", "chunk_index", 2)
        ));
        repo.resyncChunkCount(TENANT_A, "mfst.5");
        var doc = repo.getDocument(TENANT_A, "mfst.5");
        assertThat(doc.get("chunk_count")).isEqualTo(3);
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
    void collection_rename_cascadesToDocuments() {
        repo.upsertCollection(TENANT_A, Map.of(
            "name", "knowledge__old__v1",
            "content_type", "knowledge",
            "owner_id", "nexus-1-1"
        ));
        repo.upsertDocument(TENANT_A, Map.of("tumbler", "rn.1", "title", "Rename Test",
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "knowledge__old__v1"));
        int updated = repo.renameCollection(TENANT_A, "knowledge__old__v1", "knowledge__new__v1");
        assertThat(updated).isEqualTo(1); // 1 document updated
        var doc = repo.getDocument(TENANT_A, "rn.1");
        assertThat(doc.get("physical_collection")).isEqualTo("knowledge__new__v1");
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
        var links = repo.linksFrom(etlTenant, "elA", null);
        assertThat(links).hasSize(1); // exactly one, not two
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
}
