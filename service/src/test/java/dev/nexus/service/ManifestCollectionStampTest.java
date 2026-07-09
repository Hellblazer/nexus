package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
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
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-x6kdz — the 6.5.0 live-shakeout finding: NO writer populated
 * {@code catalog_document_chunks.collection}, the combined-query join key
 * (catalog-006/-008/-012: {@code m.collection = c.collection}). The only
 * stamper was the migration-leg {@code manifest_backfill()}, and the REPLACE
 * manifest writers wiped its work on every re-index — so on any live tenant
 * every post-migration manifest row was invisible to the combined queries
 * (silent-empty; the app-side {@code query()} fallback masked it). The seeded
 * parity tests never caught it because they INSERT manifest rows with
 * {@code collection} set directly, bypassing the writers.
 *
 * <p>These tests walk the REAL writers — {@code writeManifest} (REPLACE),
 * {@code appendManifestChunks}, {@code importChunksBatch} (the catalog-ETL
 * path) — and assert both the stamped column AND the end-to-end property the
 * shakeout found broken: {@code search_metadata_scoped_1024} returns the row.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestCollectionStampTest {

    private static final String TENANT = "mcs-tenant";
    private static final String COLL   = "knowledge__mcs__voyage-context-3__v1";
    private static final String CH_A   = "a".repeat(32);
    private static final String CH_B   = "b".repeat(32);

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource ds;
    CatalogRepository repo;
    String docTumbler;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
                + "  END IF; "
                + "END $$");
        }
        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }
        ds = PgContainerHelper.superuserDataSource(pg);
        repo = new CatalogRepository(new TenantScope(ds));

        // One registered document with a physical_collection, plus a matching
        // 1024-dim chunk row (the combined query joins chunks ⋈ manifest ⋈ docs).
        docTumbler = repo.registerDocument(TENANT, "9.1", Map.of(
            "title", "stamp doc", "content_type", "knowledge",
            "physical_collection", COLL));
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
            st.execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT + "', '" + COLL + "') ON CONFLICT DO NOTHING");
            st.execute("INSERT INTO nexus.chunks_1024 (tenant_id, collection, chash, chunk_text, embedding) "
                + "VALUES ('" + TENANT + "', '" + COLL + "', '" + CH_A + "', 'alpha text', "
                + "('[' || repeat('0.1,', 1023) || '0.1]')::vector)");
        }
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private String collectionOf(String chash) throws Exception {
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT collection FROM nexus.catalog_document_chunks "
                 + "WHERE tenant_id = '" + TENANT + "' AND chash = '" + chash + "'")) {
            return rs.next() ? rs.getString(1) : null;
        }
    }

    private int combinedQueryHits() throws Exception {
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT count(*) FROM nexus.search_metadata_scoped_1024("
                 + "('[' || repeat('0.1,', 1023) || '0.1]')::vector, "
                 + "ARRAY['" + COLL + "'], NULL::text, NULL::text, NULL::int, "
                 + "NULL::text, NULL::text, NULL::jsonb, 10)")) {
            rs.next();
            return rs.getInt(1);
        }
    }

    @Test
    void writeManifest_stampsCollection_andCombinedQuerySeesTheRow() throws Exception {
        repo.writeManifest(TENANT, docTumbler, List.of(
            Map.of("position", 0, "chash", CH_A)));
        assertThat(collectionOf(CH_A))
            .as("REPLACE writer stamps the doc's physical_collection")
            .isEqualTo(COLL);
        assertThat(combinedQueryHits())
            .as("the exact end-to-end property the live shakeout found broken: "
                + "a serving-written manifest row is visible to the combined query")
            .isEqualTo(1);

        // REPLACE again (re-index) — the stamp must SURVIVE, not be wiped
        // (pre-fix, re-indexing was what erased manifest_backfill's work).
        repo.writeManifest(TENANT, docTumbler, List.of(
            Map.of("position", 0, "chash", CH_A)));
        assertThat(collectionOf(CH_A)).isEqualTo(COLL);
        assertThat(combinedQueryHits()).isEqualTo(1);
    }

    @Test
    void appendAndImport_stampCollection() throws Exception {
        repo.appendManifestChunks(TENANT, docTumbler, List.of(
            Map.of("position", 1, "chash", CH_B)));
        assertThat(collectionOf(CH_B))
            .as("append writer stamps too").isEqualTo(COLL);

        // The catalog-ETL path (the shape every migrated tenant's rows took).
        repo.importChunksBatch(TENANT, docTumbler, List.of(
            Map.of("position", 2, "chash", "c".repeat(32))));
        assertThat(collectionOf("c".repeat(32)))
            .as("ETL import stamps too — migrated tenants stop regressing")
            .isEqualTo(COLL);
    }

    @Test
    void renameCollection_reHomesManifestRows() throws Exception {
        // The second door back into the silently-empty state (critique Q2):
        // RDR-164 rename re-pointed docs + chunks + chash_index but NOT the
        // manifest's denormalized collection — post-rename the combined
        // queries would go empty again for that collection.
        String renamed = "knowledge__mcs-renamed__voyage-context-3__v1";
        repo.writeManifest(TENANT, docTumbler, List.of(
            Map.of("position", 0, "chash", CH_A)));
        Map<String, Integer> counts = repo.renameCollection(TENANT, COLL, renamed);
        assertThat(counts.get("catalog_document_chunks"))
            .as("rename re-homes the manifest join key").isEqualTo(1);
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT count(*) FROM nexus.search_metadata_scoped_1024("
                 + "('[' || repeat('0.1,', 1023) || '0.1]')::vector, "
                 + "ARRAY['" + renamed + "'], NULL::text, NULL::text, NULL::int, "
                 + "NULL::text, NULL::text, NULL::jsonb, 10)")) {
            rs.next();
            assertThat(rs.getInt(1))
                .as("combined query follows the rename end-to-end").isEqualTo(1);
        }
        // restore for sibling tests (PER_CLASS lifecycle, shared fixture)
        repo.renameCollection(TENANT, renamed, COLL);
    }

    @Test
    void catalog014_repairsNullRows_asNonBypassRlsOwner() throws Exception {
        // Reconstruct the live-tenant state: a NULL-collection manifest row
        // (written by a pre-fix writer), then replay catalog-014 as a
        // production-shaped NOSUPERUSER NOBYPASSRLS owner — the nexus-1wjmq
        // class demands the changeset's NO FORCE toggle actually works.
        String chNull = "d".repeat(32);
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement()) {
            st.execute("ALTER TABLE nexus.catalog_document_chunks NO FORCE ROW LEVEL SECURITY");
            st.execute("INSERT INTO nexus.catalog_document_chunks "
                + "(tenant_id, doc_id, position, chash, collection) "
                + "VALUES ('" + TENANT + "', '" + docTumbler + "', 99, '" + chNull + "', NULL)");
            st.execute("ALTER TABLE nexus.catalog_document_chunks FORCE ROW LEVEL SECURITY");
            st.execute("DELETE FROM public.databasechangelog WHERE id = 'catalog-014-0'");
            st.execute("DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcs_admin') THEN "
                + "    CREATE ROLE mcs_admin LOGIN PASSWORD 'mcs_admin_pw' NOSUPERUSER NOBYPASSRLS; "
                + "  END IF; "
                + "END $$");
            st.execute("GRANT USAGE, CREATE ON SCHEMA nexus, t1, public TO mcs_admin");
            // The runAlways grants changeset GRANTs on ALL tables/sequences —
            // the replay role must own them (same recipe as Catalog013RlsReplayTest).
            st.execute("DO $$ DECLARE r record; BEGIN "
                + "  FOR r IN SELECT schemaname, tablename FROM pg_tables "
                + "           WHERE schemaname IN ('nexus', 't1') LOOP "
                + "    EXECUTE format('ALTER TABLE %I.%I OWNER TO mcs_admin', "
                + "                   r.schemaname, r.tablename); "
                + "  END LOOP; "
                + "  FOR r IN SELECT schemaname, sequencename FROM pg_sequences "
                + "           WHERE schemaname IN ('nexus', 't1') LOOP "
                + "    EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO mcs_admin', "
                + "                   r.schemaname, r.sequencename); "
                + "  END LOOP; "
                + "END $$");
            st.execute("GRANT ALL ON TABLE public.databasechangelog, "
                + "public.databasechangeloglock TO mcs_admin");
        }
        try (Connection admin = java.sql.DriverManager.getConnection(
                pg.getJdbcUrl(), "mcs_admin", "mcs_admin_pw")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(admin)));
            lb.update(new Contexts());
        }
        assertThat(collectionOf(chNull))
            .as("catalog-014 stamps NULL rows despite FORCE RLS (toggle pattern)")
            .isEqualTo(COLL);
        // FORCE restored by the changeset itself.
        try (Connection su = pg.createConnection(""); Statement st = su.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT relforcerowsecurity FROM pg_class "
                 + "WHERE oid = 'nexus.catalog_document_chunks'::regclass")) {
            rs.next();
            assertThat(rs.getBoolean(1)).isTrue();
        }
    }
}
