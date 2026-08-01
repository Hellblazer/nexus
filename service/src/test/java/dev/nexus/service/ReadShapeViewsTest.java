// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-154 P1.2 (bead nexus-h9qyp) — security_invoker read-shape views.
 *
 * <p>Two guarantees, mirroring CollectionVectorStatsTest GROUP 3 + GROUP 5:
 * <ul>
 *   <li>Every one of the five views has {@code security_invoker=true} PHYSICALLY
 *       set in pg_class.reloptions (a comment / a changelog-grep is not proof
 *       that Liquibase actually applied it).</li>
 *   <li>Cross-tenant isolation under a NOSUPERUSER NOBYPASSRLS svc role + GUC:
 *       the grouped views (which carry tenant_id) leak ZERO foreign-tenant rows,
 *       and the scalar catalog_stats view scopes its counts to the GUC tenant.
 *       A superuser CONTROL proves foreign rows exist underneath (non-vacuous).</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ReadShapeViewsTest {

    private static final String TENANT_A = "rsv-tenant-a";
    private static final String TENANT_B = "rsv-tenant-b";
    private static final String SVC_ROLE = "svc_rsv_test";
    private static final String SVC_PASS = "svc_rsv_test_pass";

    private static final List<String> VIEWS = List.of(
        "catalog_stats", "collection_doc_counts", "coverage_by_content_type",
        "collection_health_meta", "topics_with_counts", "links_by_type_counts");

    // The views that carry a tenant_id column (catalog_stats is scalar).
    private static final List<String> GROUPED_VIEWS = List.of(
        "collection_doc_counts", "coverage_by_content_type",
        "collection_health_meta", "topics_with_counts", "links_by_type_counts");

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
        }

        try (Connection su = pg.createConnection("")) {
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su))).update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (String tbl : List.of("catalog_documents", "catalog_links", "topics",
                                       "catalog_owners", "catalog_collections",
                                       "catalog_document_chunks")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + tbl + " TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");

            // Fixtures chosen so EVERY catalog_stats scalar differs A vs B (non-vacuous
            // per-subquery RLS scoping) AND every grouped view has rows for both
            // tenants. TENANT_A: 2 docs, 1 link, 2 owners, 2 collections, 2 chunks,
            // 1 topic. TENANT_B: 1 doc, 2 links (dangling tumblers, links are not
            // FK-enforced), 1 owner, 1 collection, 1 chunk, 1 topic.
            seedDoc(su, TENANT_A, "a.1", "paper", "c_a");
            seedDoc(su, TENANT_A, "a.2", "code",  "c_a");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_links (tenant_id, from_tumbler, to_tumbler, link_type, created_by) "
                + "VALUES ('" + TENANT_A + "', 'a.1', 'a.2', 'cites', 'test')");
            seedTopic(su, TENANT_A, "topic-a", "c_a");
            seedOwner(su, TENANT_A, "a-own-1");
            seedOwner(su, TENANT_A, "a-own-2");
            seedColl(su, TENANT_A, "c_a");
            seedColl(su, TENANT_A, "c_a2");
            seedChunk(su, TENANT_A, "a.1", 0, "chash-a-0");
            seedChunk(su, TENANT_A, "a.1", 1, "chash-a-1");

            seedDoc(su, TENANT_B, "b.1", "paper", "c_b");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_links (tenant_id, from_tumbler, to_tumbler, link_type, created_by) "
                + "VALUES ('" + TENANT_B + "', 'b.1', 'b.x1', 'cites', 'test'), "
                + "       ('" + TENANT_B + "', 'b.1', 'b.x2', 'cites', 'test')");
            seedTopic(su, TENANT_B, "topic-b", "c_b");
            seedOwner(su, TENANT_B, "b-own-1");
            seedColl(su, TENANT_B, "c_b");
            seedChunk(su, TENANT_B, "b.1", 0, "chash-b-0");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void seedDoc(Connection su, String tenant, String tumbler,
                                String ctype, String coll) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, content_type, physical_collection, indexed_at) "
            + "VALUES ('" + tenant + "', '" + tumbler + "', 'T', '" + ctype + "', '" + coll + "', '2026-01-01T00:00:00Z')");
    }


    private static void seedTopic(Connection su, String tenant, String label, String coll) throws Exception {
        // RDR-164 P1a: register the collection (topics_collection_fk).
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + tenant + "', '"
            + coll + "') ON CONFLICT (tenant_id, name) DO NOTHING");
        su.createStatement().execute(
            "INSERT INTO nexus.topics (tenant_id, label, collection, doc_count, created_at, review_status) "
            + "VALUES ('" + tenant + "', '" + label + "', '" + coll + "', 0, now(), 'pending')");
    }

    private static void seedDocIndexed(Connection su, String tenant, String tumbler,
                                       String coll, String indexedAt) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection, indexed_at) "
            + "VALUES ('" + tenant + "', '" + tumbler + "', 'T', '" + coll + "', '" + indexedAt + "')");
    }

    @Test @Order(40)
    void collectionHealthMeta_staleSourceRatio_indexAge() throws Exception {
        // nexus-agsq7: stale = indexed_at more than 30 days ago. 2020 is always
        // stale, 2099 is always fresh (future) — deterministic regardless of now().
        final String col = "c_stale_age";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedDocIndexed(su, TENANT_A, "sa.1", col, "2020-01-01T00:00:00Z"); // stale
            seedDocIndexed(su, TENANT_A, "sa.2", col, "2099-01-01T00:00:00Z"); // fresh
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT stale_source_ratio FROM nexus.collection_health_meta "
                + "WHERE tenant_id = '" + TENANT_A + "' AND collection = '" + col + "'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getDouble("stale_source_ratio"))
                .as("1 of 2 dated docs is > 30 days old").isEqualTo(0.5d);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // TOMBSTONE AUDIT 2026-08-01 (nexus-se9r3, nexus-l1nre, nexus-cah9n)
    // ══════════════════════════════════════════════════════════════════════════

    private record StatsRow(long docCount, long chunkCount) {}

    private StatsRow statsFor(String tenant) throws Exception {
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute("SELECT set_config('nexus.tenant', '" + tenant + "', false)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT doc_count, chunk_count FROM nexus.catalog_stats");
            rs.next();
            return new StatsRow(rs.getLong("doc_count"), rs.getLong("chunk_count"));
        }
    }

    /**
     * nexus-se9r3: doc_count filters deleted_at IS NULL, and chunk_count
     * counts only chunks whose OWNING document is live (soft-delete does not
     * cascade to catalog_document_chunks, so the naive raw count would keep
     * counting a tombstoned doc's chunks). Superuser CONTROL at the end
     * proves the tombstoned row and its chunk exist underneath — this is a
     * real exclusion, not an artifact of the row never having existed.
     */
    @Test @Order(50)
    void catalogStats_docCountAndChunkCount_excludeTombstonedDocs() throws Exception {
        String tenant = "rsv-tomb-stats-" + System.nanoTime();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedDoc(su, tenant, "ts.live", "paper", "c_ts_tomb");
            seedDoc(su, tenant, "ts.dead", "paper", "c_ts_tomb");
            seedChunk(su, tenant, "ts.live", 0, "chash-ts-live");
            seedChunk(su, tenant, "ts.dead", 0, "chash-ts-dead");
        }

        StatsRow before = statsFor(tenant);
        assertThat(before.docCount()).as("both docs counted while live").isEqualTo(2L);
        assertThat(before.chunkCount()).as("both chunks counted while owning doc is live").isEqualTo(2L);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = now() "
                + "WHERE tenant_id = '" + tenant + "' AND tumbler = 'ts.dead'");
        }

        StatsRow after = statsFor(tenant);
        assertThat(after.docCount()).as("doc_count drops by exactly the tombstoned doc").isEqualTo(1L);
        assertThat(after.chunkCount())
            .as("chunk_count drops too — the manifest does not cascade the tombstone, "
                + "but chunk_count now counts only chunks of LIVE documents")
            .isEqualTo(1L);

        // CONTROL: the tombstoned row and its manifest chunk both still exist underneath.
        try (Connection su = pg.createConnection("")) {
            ResultSet rsDoc = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.catalog_documents WHERE tenant_id = '" + tenant
                + "' AND tumbler = 'ts.dead' AND deleted_at IS NOT NULL");
            rsDoc.next();
            assertThat(rsDoc.getLong(1)).as("CONTROL: tombstoned row exists").isEqualTo(1L);
            ResultSet rsChunk = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.catalog_document_chunks WHERE tenant_id = '" + tenant
                + "' AND doc_id = 'ts.dead'");
            rsChunk.next();
            assertThat(rsChunk.getLong(1))
                .as("CONTROL: the tombstoned doc's manifest chunk row survives (no cascade)")
                .isEqualTo(1L);
        }
    }

    private long collectionDocCount(String tenant, String coll) throws Exception {
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute("SELECT set_config('nexus.tenant', '" + tenant + "', false)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT doc_count FROM nexus.collection_doc_counts WHERE physical_collection = '" + coll + "'");
            return rs.next() ? rs.getLong(1) : 0L;
        }
    }

    /** nexus-se9r3: collection_doc_counts excludes tombstoned documents. */
    @Test @Order(51)
    void collectionDocCounts_excludesTombstonedDocs() throws Exception {
        String tenant = "rsv-tomb-cdc-" + System.nanoTime();
        String coll = "c_cdc_tomb";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedColl(su, tenant, coll);
            seedDoc(su, tenant, "cdc.live", "paper", coll);
            seedDoc(su, tenant, "cdc.dead", "paper", coll);
        }
        assertThat(collectionDocCount(tenant, coll)).isEqualTo(2L);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = now() "
                + "WHERE tenant_id = '" + tenant + "' AND tumbler = 'cdc.dead'");
        }
        assertThat(collectionDocCount(tenant, coll))
            .as("tombstoned doc no longer counted").isEqualTo(1L);

        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.catalog_documents WHERE tenant_id = '" + tenant
                + "' AND tumbler = 'cdc.dead' AND deleted_at IS NOT NULL");
            rs.next();
            assertThat(rs.getLong(1)).as("CONTROL: tombstoned row exists").isEqualTo(1L);
        }
    }

    private long coverageTotalFor(String tenant, String contentType) throws Exception {
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute("SELECT set_config('nexus.tenant', '" + tenant + "', false)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT total FROM nexus.coverage_by_content_type WHERE content_type = '" + contentType + "'");
            return rs.next() ? rs.getLong(1) : 0L;
        }
    }

    /**
     * nexus-l1nre: coverage_by_content_type excludes tombstoned documents.
     * Before this fix the view branch of coverageByContentType (empty
     * ownerPrefix) was the ONLY unfiltered branch — the owner-prefix hand
     * aggregation already filtered deleted_at IS NULL — so the same method
     * disagreed with itself depending on whether a prefix was supplied
     * (CatalogRepositoryTest pins the two branches' agreement directly).
     */
    @Test @Order(52)
    void coverageByContentType_excludesTombstonedDocs() throws Exception {
        String tenant = "rsv-tomb-cov-" + System.nanoTime();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedDoc(su, tenant, "cov.live", "paper", "c_cov_tomb");
            seedDoc(su, tenant, "cov.dead", "paper", "c_cov_tomb");
        }
        assertThat(coverageTotalFor(tenant, "paper")).isEqualTo(2L);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = now() "
                + "WHERE tenant_id = '" + tenant + "' AND tumbler = 'cov.dead'");
        }
        assertThat(coverageTotalFor(tenant, "paper"))
            .as("tombstoned doc excluded from total").isEqualTo(1L);

        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.catalog_documents WHERE tenant_id = '" + tenant
                + "' AND tumbler = 'cov.dead' AND deleted_at IS NOT NULL");
            rs.next();
            assertThat(rs.getLong(1)).as("CONTROL: tombstoned row exists").isEqualTo(1L);
        }
    }

    private long orphanCountFor(String tenant, String coll) throws Exception {
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute("SELECT set_config('nexus.tenant', '" + tenant + "', false)");
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT orphan_count FROM nexus.collection_health_meta WHERE collection = '" + coll + "'");
            return rs.next() ? rs.getLong(1) : 0L;
        }
    }

    /**
     * nexus-cah9n: a tombstoned document must not inflate orphan_count.
     * Before this fix, a tombstoned doc stayed in the GROUP BY scope (only
     * physical_collection IS NOT NULL was filtered) and — having no inbound
     * links of its own — always fell into the orphan FILTER, so deleting
     * documents RAISED the reported orphan count. After the fix, a
     * tombstoned doc leaves collection_health_meta's scope ENTIRELY: chm.a
     * starts as a genuine orphan (no inbound link) and, once tombstoned,
     * must drop OUT of the count rather than staying counted.
     */
    @Test @Order(53)
    void collectionHealthMeta_tombstonedDoc_leavesOrphanScopeEntirely() throws Exception {
        String tenant = "rsv-tomb-chm-" + System.nanoTime();
        String coll = "c_chm_tomb";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            seedDoc(su, tenant, "chm.a", "paper", coll);
            seedDoc(su, tenant, "chm.b", "paper", coll);
            // chm.a has no inbound link (orphan); chm.b is the target of one (not orphan).
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_links (tenant_id, from_tumbler, to_tumbler, link_type, created_by) "
                + "VALUES ('" + tenant + "', 'chm.a', 'chm.b', 'cites', 'test')");
        }
        assertThat(orphanCountFor(tenant, coll))
            .as("chm.a has no inbound link; chm.b does — one orphan").isEqualTo(1L);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "UPDATE nexus.catalog_documents SET deleted_at = now() "
                + "WHERE tenant_id = '" + tenant + "' AND tumbler = 'chm.a'");
        }
        assertThat(orphanCountFor(tenant, coll))
            .as("tombstoning the orphan removes it from scope — it must NOT still be "
                + "counted (the wrong-direction bug: deletion RAISING orphan_count)")
            .isEqualTo(0L);

        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.catalog_documents WHERE tenant_id = '" + tenant
                + "' AND tumbler = 'chm.a' AND deleted_at IS NOT NULL");
            rs.next();
            assertThat(rs.getLong(1)).as("CONTROL: tombstoned row exists").isEqualTo(1L);
        }
    }

    private static void seedOwner(Connection su, String tenant, String prefix) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_owners (tenant_id, tumbler_prefix, name, owner_type) "
            + "VALUES ('" + tenant + "', '" + prefix + "', '" + prefix + "', 'repo')");
    }

    private static void seedColl(Connection su, String tenant, String name) throws Exception {
        // Idempotent: seedTopic (RDR-164 P1a) may have already stub-registered this collection.
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
            + tenant + "', '" + name + "') ON CONFLICT (tenant_id, name) DO NOTHING");
    }

    private static void seedChunk(Connection su, String tenant, String docId, int pos, String chash) throws Exception {
        // chash must be exactly 32 chars (catalog_document_chunks_chash_len_check).
        String c = (chash + "00000000000000000000000000000000").substring(0, 32);
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) "
            + "VALUES ('" + tenant + "', '" + docId + "', " + pos + ", '" + c + "')");
    }

    // ── reloption physically set on every view ──────────────────────────────────

    @Test @Order(10)
    void everyView_hasSecurityInvokerReloption() throws Exception {
        try (Connection su = pg.createConnection("")) {
            for (String view : VIEWS) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT reloptions FROM pg_class WHERE oid = 'nexus." + view + "'::regclass");
                assertThat(rs.next()).as("view nexus.%s must exist", view).isTrue();
                java.sql.Array arr = rs.getArray(1);
                assertThat(arr).as("nexus.%s must HAVE reloptions", view).isNotNull();
                assertThat((String[]) arr.getArray())
                    .as("nexus.%s must have security_invoker=true PHYSICALLY set (RDR-154 standing rule)", view)
                    .contains("security_invoker=true");
            }
        }
    }

    // ── grouped views leak zero foreign-tenant rows ─────────────────────────────

    @Test @Order(20)
    void groupedViews_gucA_seeZeroTenantBRows() throws Exception {
        // CONTROL: superuser sees BOTH tenants in each grouped view (foreign rows exist).
        try (Connection su = pg.createConnection("")) {
            for (String view : GROUPED_VIEWS) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT count(*) FROM nexus." + view + " WHERE tenant_id = '" + TENANT_B + "'");
                rs.next();
                assertThat(rs.getLong(1))
                    .as("CONTROL: superuser must see tenant-B rows in nexus.%s", view)
                    .isGreaterThanOrEqualTo(1L);
            }
        }
        // svc + GUC=A: zero tenant-B rows; at least one tenant-A row.
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute("SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            for (String view : GROUPED_VIEWS) {
                ResultSet rsB = svc.createStatement().executeQuery(
                    "SELECT count(*) FROM nexus." + view + " WHERE tenant_id = '" + TENANT_B + "'");
                rsB.next();
                assertThat(rsB.getLong(1))
                    .as("GUC=A must see ZERO tenant-B rows in nexus.%s (caller RLS via security_invoker)", view)
                    .isEqualTo(0L);
                ResultSet rsA = svc.createStatement().executeQuery(
                    "SELECT count(*) FROM nexus." + view + " WHERE tenant_id = '" + TENANT_A + "'");
                rsA.next();
                assertThat(rsA.getLong(1))
                    .as("GUC=A must see its own tenant-A rows in nexus.%s", view)
                    .isGreaterThanOrEqualTo(1L);
            }
        }
    }

    // ── scalar catalog_stats scopes counts to the GUC tenant ────────────────────

    @Test @Order(30)
    void catalogStats_scopesScalarCountsToGucTenant() throws Exception {
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute("SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            ResultSet a = svc.createStatement().executeQuery(
                "SELECT doc_count, link_count, owner_count, collection_count, chunk_count "
                + "FROM nexus.catalog_stats");
            a.next();
            assertThat(a.getLong("doc_count")).as("GUC=A doc_count").isEqualTo(2L);
            assertThat(a.getLong("link_count")).as("GUC=A link_count").isEqualTo(1L);
            assertThat(a.getLong("owner_count")).as("GUC=A owner_count").isEqualTo(2L);
            assertThat(a.getLong("collection_count")).as("GUC=A collection_count").isEqualTo(2L);
            assertThat(a.getLong("chunk_count")).as("GUC=A chunk_count").isEqualTo(2L);

            svc.createStatement().execute("SELECT set_config('nexus.tenant', '" + TENANT_B + "', false)");
            ResultSet b = svc.createStatement().executeQuery(
                "SELECT doc_count, link_count, owner_count, collection_count, chunk_count "
                + "FROM nexus.catalog_stats");
            b.next();
            assertThat(b.getLong("doc_count")).as("GUC=B doc_count").isEqualTo(1L);
            assertThat(b.getLong("link_count")).as("GUC=B link_count").isEqualTo(2L);
            assertThat(b.getLong("owner_count")).as("GUC=B owner_count").isEqualTo(1L);
            assertThat(b.getLong("collection_count")).as("GUC=B collection_count").isEqualTo(1L);
            assertThat(b.getLong("chunk_count")).as("GUC=B chunk_count").isEqualTo(1L);
        }
    }
}
