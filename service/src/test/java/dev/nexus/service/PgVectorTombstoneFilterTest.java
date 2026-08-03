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
import java.util.Set;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-3ck2g (RDR-156 Decision 6, never implemented until this bead): {@code
 * PgVectorRepository#searchWithTokens} and {@code #hybridSearch} read {@code
 * nexus.chunks_&lt;dim&gt;} directly with NO tombstone predicate — a document
 * tombstoned via {@link CatalogRepository#deleteDocument} stayed fully searchable
 * through the plain-search and hybrid-search paths forever (the manifest join used
 * everywhere ELSE in the catalog read surface, e.g. {@code catalog-006}'s combined-query
 * functions and {@code CatalogRepository#liveParentDoc}, was simply absent here).
 *
 * <p>Fixture: two documents in the same collection, each with one manifest-backed
 * chunk in {@code chunks_384}, plus one THIRD manifest-less chunk (no {@code
 * catalog_document_chunks} row at all — the RDR-145 MCP/{@code store_put} note-chunk
 * shape {@code SoftDeleteTest}:814-867 pins as "never swept"). One document is
 * tombstoned via the real production path ({@link CatalogRepository#deleteDocument}) —
 * not a raw SQL UPDATE — so this suite proves the actual caller-visible behavior, not
 * just the SQL predicate in isolation.
 *
 * <p>Embedding: the shared {@link PgVectorRepositoryContractTest.FakeEmbedder}, left
 * unregistered for every text in this suite so every text (and the query) embeds to
 * the same default unit vector (1, 0, 0, ...) — distance is uniformly 0 across the
 * fixture, so ordering is irrelevant here; only PRESENCE/ABSENCE of a chash in the
 * result set is asserted.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class PgVectorTombstoneFilterTest {

    private static final String SVC_ROLE = "svc_tomb_search";
    private static final String SVC_PASS = "svc_tomb_search_pass";

    private static final String TENANT = "tomb-search";
    private static final String COLLECTION = "knowledge__tomb-search__minilm-l6-v2-384__v1";

    private static final String DOC_LIVE = "tomb-doc-live";
    private static final String DOC_DEAD = "tomb-doc-dead";

    // A shared lexeme ("tombstone") in every chunk's text so a hybridSearch FTS gate
    // matches all three regardless of tombstone state — the predicate under test is
    // the LIVE_CHUNKS filter, not the text gate.
    private static final String TEXT_LIVE   = "tombstone probe chunk for the live document";
    private static final String TEXT_DEAD   = "tombstone probe chunk for the tombstoned document";
    private static final String TEXT_ORPHAN = "tombstone probe chunk with no manifest row at all";
    private static final String QUERY_TEXT  = "tombstone probe chunk";

    private static final String CHASH_LIVE   = Chash.ofText("tomb-search-chunk-live").toHex();
    private static final String CHASH_DEAD   = Chash.ofText("tomb-search-chunk-dead").toHex();
    private static final String CHASH_ORPHAN = Chash.ofText("tomb-search-chunk-orphan").toHex();

    // ── get-family fixture additions (nexus-8j1zx) ──────────────────────────────
    // A second live document + a chunk with TWO manifest rows (one on DOC_DEAD, one
    // on DOC_LIVE2) to prove the OR-semantics of liveChunksCondition: a chash stays
    // visible as long as AT LEAST ONE of its manifest rows points at a live document,
    // even though one of its OTHER manifest rows points at a tombstoned one. Seeded by
    // a dedicated @Test @Order(45) step BETWEEN the searchWithTokens/hybridSearch tests
    // above and the get-family tests below — NOT in startAll() — because
    // searchWithTokens/hybridSearch here run as unfiltered top-N KNN scans of the whole
    // collection against the shared FakeEmbedder's single default vector (every
    // unregistered text embeds identically, per the class javadoc): seeding it upfront
    // would have made CHASH_SHARED show up in the Order(10)/(20)/(30)/(40) result sets
    // above and broken their exact-set assertions, even though its TEXT_SHARED content
    // shares no FTS lexeme with QUERY_TEXT (search()/hybridSearch() rank ALL chunks
    // present, not just lexeme matches, under this fixture's embedder).
    private static final String DOC_LIVE2 = "tomb-doc-live-2";
    private static final String TEXT_SHARED = "shared chash chunk referencing a dead and a live document";
    private static final String CHASH_SHARED = Chash.ofText("tomb-search-chunk-shared").toHex();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository catalogRepo;
    PgVectorRepository vecRepo;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;

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
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);

        // --- Fixture: two live documents + their manifest rows (superuser, bypasses RLS
        // so both tenants/rows land regardless of GUC — same convention as
        // CatalogDocumentCascadeTest/SoftDeleteTest's seedDocument helpers).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT + "', '"
                + COLLECTION + "') ON CONFLICT DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) VALUES "
                + "('" + TENANT + "', '" + DOC_LIVE + "', 'Live Doc', '" + COLLECTION + "'), "
                + "('" + TENANT + "', '" + DOC_DEAD + "', 'Dead Doc', '" + COLLECTION + "')");
            insertManifestRow(su, DOC_LIVE, CHASH_LIVE);
            insertManifestRow(su, DOC_DEAD, CHASH_DEAD);
            // CHASH_ORPHAN deliberately gets NO catalog_document_chunks row (manifest-less
            // RDR-145 note-chunk shape).
        }

        // Chunk storage rows (chunks_384) for all three chashes, via the repository under
        // test — same path a real store_put/index would use.
        vecRepo.upsertChunks(TENANT, COLLECTION,
            List.of(CHASH_LIVE, CHASH_DEAD, CHASH_ORPHAN),
            List.of(TEXT_LIVE, TEXT_DEAD, TEXT_ORPHAN),
            List.of(Map.of(), Map.of(), Map.of()));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void insertManifestRow(Connection su, String docId, String chashHex) throws Exception {
        insertManifestRow(su, docId, chashHex, 0);
    }

    /** Explicit-position sibling — needed once a document (e.g. {@code DOC_DEAD}) carries
     * more than one manifest row (nexus-8j1zx's CHASH_SHARED fixture addition): {@code
     * (tenant_id, doc_id, position)} is the manifest PK, so a second row on the same doc
     * must not collide with position 0. */
    private static void insertManifestRow(Connection su, String docId, String chashHex, int position) throws Exception {
        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) "
                + "VALUES (?, ?, ?, decode(?, 'hex'))")) {
            ps.setString(1, TENANT);
            ps.setString(2, docId);
            ps.setInt(3, position);
            ps.setString(4, chashHex);
            ps.execute();
        }
    }

    private static Set<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("id")).collect(Collectors.toSet());
    }

    // ── Plain search (searchWithTokens) ─────────────────────────────────────────

    @Test @Order(10)
    void plainSearch_beforeTombstone_allThreeChunksVisible() {
        var rows = vecRepo.search(TENANT, QUERY_TEXT, List.of(COLLECTION), 10, null);
        assertThat(ids(rows))
            .as("before any tombstone, live + dead-to-be + manifest-less chunks all searchable")
            .containsExactlyInAnyOrder(CHASH_LIVE, CHASH_DEAD, CHASH_ORPHAN);
    }

    @Test @Order(20)
    void plainSearch_afterTombstone_deadChunkInvisible_liveAndOrphanSurvive() {
        int n = catalogRepo.deleteDocument(TENANT, DOC_DEAD);
        assertThat(n).as("deleteDocument tombstoned exactly one row").isEqualTo(1);

        var rows = vecRepo.search(TENANT, QUERY_TEXT, List.of(COLLECTION), 10, null);
        assertThat(ids(rows))
            .as("nexus-3ck2g fix: the tombstoned doc's chunk must no longer be searchable "
                + "via searchWithTokens; the live doc's chunk and the manifest-less (RDR-145) "
                + "chunk must still be searchable")
            .containsExactlyInAnyOrder(CHASH_LIVE, CHASH_ORPHAN)
            .doesNotContain(CHASH_DEAD);
    }

    // ── Hybrid search (hybridSearch) — both selectivity-dispatch branches ──────

    @Test @Order(30)
    void hybridSearch_selectiveBranch_afterTombstone_deadChunkInvisible() {
        // Default selectiveGateMax (PgVectorRepository.SELECTIVE_GATE_MAX = 5000): the
        // 3-row FTS gate is far below it, so this exercises the SELECTIVE (chash-ranked)
        // branch. deleteDocument was already called in the prior @Test on the shared
        // PER_CLASS fixture — DOC_DEAD stays tombstoned for the rest of this class.
        var rows = vecRepo.hybridSearch(TENANT, QUERY_TEXT, List.of(COLLECTION), 10, null);
        assertThat(ids(rows))
            .as("hybridSearch SELECTIVE branch must exclude the tombstoned doc's chunk while "
                + "keeping the live and manifest-less chunks")
            .containsExactlyInAnyOrder(CHASH_LIVE, CHASH_ORPHAN)
            .doesNotContain(CHASH_DEAD);
    }

    @Test @Order(40)
    void hybridSearch_nonSelectiveHnswFirstBranch_afterTombstone_deadChunkInvisible() {
        // selectiveGateMax=1 forces the NON-SELECTIVE (HNSW-first) branch on this
        // fixture-scale corpus: the 3-row gate exceeds the cutoff by construction — see
        // PgVectorRepository#hybridSearch(String, String, List, int, Map, int)'s own
        // javadoc ("passing a small value forces the non-selective branch").
        var rows = vecRepo.hybridSearch(TENANT, QUERY_TEXT, List.of(COLLECTION), 10, null, 1);
        assertThat(ids(rows))
            .as("hybridSearch NON-SELECTIVE (HNSW-first) branch must ALSO exclude the "
                + "tombstoned doc's chunk while keeping the live and manifest-less chunks — "
                + "this is the third of the three raw-read sites nexus-3ck2g fixes")
            .containsExactlyInAnyOrder(CHASH_LIVE, CHASH_ORPHAN)
            .doesNotContain(CHASH_DEAD);
    }

    @Test @Order(45)
    void seedSharedChashFixture_forGetFamilyTests() throws Exception {
        // DOC_DEAD is already tombstoned (Order 20). Seeded here, AFTER the
        // searchWithTokens/hybridSearch assertions above and BEFORE the get-family
        // tests below — see the CHASH_SHARED field javadoc for why.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) VALUES "
                + "('" + TENANT + "', '" + DOC_LIVE2 + "', 'Live Doc 2', '" + COLLECTION + "')");
            // CHASH_SHARED gets TWO manifest rows: one on the already-tombstoned DOC_DEAD,
            // one on the still-live DOC_LIVE2 — proves the get-family predicate's
            // OR-semantics (live iff AT LEAST ONE manifest row's document is live).
            insertManifestRow(su, DOC_DEAD, CHASH_SHARED, 1);
            insertManifestRow(su, DOC_LIVE2, CHASH_SHARED, 0);
        }
        vecRepo.upsertChunks(TENANT, COLLECTION,
            List.of(CHASH_SHARED), List.of(TEXT_SHARED), List.of(Map.of()));
    }

    // ── Get-family (typed jOOQ reads): get, getWhere, getEmbeddings, getAllMetadata ──
    // nexus-8j1zx (found during nexus-3ck2g's round-1 substantive critique): these
    // four reads went through DimTables.ChunkTable with ZERO tombstone filtering —
    // structurally invisible to both the searchWithTokens/hybridSearch fix above and
    // its gate. DOC_DEAD is already tombstoned by this point in the shared PER_CLASS
    // fixture (Order 20 above). CHASH_SHARED additionally proves the OR-semantics: a
    // chash with one manifest row on the tombstoned DOC_DEAD and another on the still-
    // live DOC_LIVE2 must stay visible.

    @SuppressWarnings("unchecked")
    private static Set<String> idsOf(Map<String, Object> envelope) {
        return new java.util.HashSet<>((List<String>) envelope.get("ids"));
    }

    @Test @Order(50)
    void get_afterTombstone_deadChunkInvisible_liveOrphanAndSharedSurvive() {
        var envelope = vecRepo.get(TENANT, COLLECTION,
            List.of(CHASH_LIVE, CHASH_DEAD, CHASH_ORPHAN, CHASH_SHARED), 10, 0);
        assertThat(idsOf(envelope))
            .as("nexus-8j1zx fix: get() must exclude the tombstoned doc's chunk while keeping "
                + "the live, manifest-less, and shared-chash (live+dead manifest rows) chunks")
            .containsExactlyInAnyOrder(CHASH_LIVE, CHASH_ORPHAN, CHASH_SHARED)
            .doesNotContain(CHASH_DEAD);
    }

    @Test @Order(60)
    void getWhere_afterTombstone_deadChunkInvisible_liveOrphanAndSharedSurvive() {
        var envelope = vecRepo.getWhere(TENANT, COLLECTION, null, 100, 0);
        assertThat(idsOf(envelope))
            .as("nexus-8j1zx fix: getWhere() must exclude the tombstoned doc's chunk while "
                + "keeping the live, manifest-less, and shared-chash chunks")
            .containsExactlyInAnyOrder(CHASH_LIVE, CHASH_ORPHAN, CHASH_SHARED)
            .doesNotContain(CHASH_DEAD);
    }

    @Test @Order(70)
    void getEmbeddings_afterTombstone_deadChunkInvisible_liveOrphanAndSharedSurvive() {
        var envelope = vecRepo.getEmbeddings(TENANT, COLLECTION,
            List.of(CHASH_LIVE, CHASH_DEAD, CHASH_ORPHAN, CHASH_SHARED));
        assertThat(idsOf(envelope))
            .as("nexus-8j1zx fix: getEmbeddings() must exclude the tombstoned doc's chunk while "
                + "keeping the live, manifest-less, and shared-chash chunks")
            .containsExactlyInAnyOrder(CHASH_LIVE, CHASH_ORPHAN, CHASH_SHARED)
            .doesNotContain(CHASH_DEAD);
    }

    @Test @Order(80)
    void getAllMetadata_afterTombstone_deadChunkInvisible_liveOrphanAndSharedSurvive() {
        var envelope = vecRepo.getAllMetadata(TENANT, COLLECTION, null);
        assertThat(idsOf(envelope))
            .as("nexus-8j1zx fix: getAllMetadata() must exclude the tombstoned doc's chunk while "
                + "keeping the live, manifest-less, and shared-chash chunks")
            .containsExactlyInAnyOrder(CHASH_LIVE, CHASH_ORPHAN, CHASH_SHARED)
            .doesNotContain(CHASH_DEAD);
    }

    // ── list() (nexus-txcbo, round-2 critique) ───────────────────────────────────
    // Unlike the get-family, list() needs NO out-of-band chash — it backs POST
    // /v1/vectors/store-list (nx store list / MCP store_list), a plain listing that
    // surfaced tombstoned content by default before this fix.

    @Test @Order(90)
    void list_afterTombstone_deadChunkInvisible_liveOrphanAndSharedSurvive() {
        var envelope = vecRepo.list(TENANT, COLLECTION, 100, 0);
        assertThat(idsOf(envelope))
            .as("nexus-txcbo fix: list() must exclude the tombstoned doc's chunk while keeping "
                + "the live, manifest-less, and shared-chash (live+dead manifest rows) chunks")
            .containsExactlyInAnyOrder(CHASH_LIVE, CHASH_ORPHAN, CHASH_SHARED)
            .doesNotContain(CHASH_DEAD);
    }
}
