package dev.nexus.service;

import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.SchemaMigrator.MigrationException;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.exception.LiquibaseException;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.*;

import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.sql.Statement;
import java.nio.charset.StandardCharsets;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * RDR-191 Phase 4 repoint batch, Step A (bead nexus-o8dil.18) —
 * {@code vectors-005-repoint-functions-views.xml}. Chains THREE staged
 * changesets in sequence (mirrors {@link VectorsUnifyChunksIntegrationTest}
 * and {@link VectorsUnifyCentroidsIntegrationTest}'s own isolation idiom,
 * extended to a three-file chain): real unmodified master to head, then
 * {@code vectors-004-unify-chunks.xml}, then
 * {@code taxonomy-007-unify-centroids.xml}, then THIS changeset — all as
 * standalone {@link Liquibase} applications pointed directly at their
 * staged classpath paths. Liquibase tracks {@code DATABASECHANGELOG} rows by
 * {@code (id, author, filename)}, not by which changelog reached them, so
 * three sequential {@code update()} calls produce byte-identical end state
 * to what a real {@code <include>} chain would.
 *
 * <p><strong>R8 discipline (plan of record [22445]):</strong> a green
 * {@code CREATE OR REPLACE FUNCTION} proves nothing for a plpgsql body —
 * PostgreSQL does not validate references inside a plpgsql body at CREATE
 * time, only at first CALL. Every one of the 24 objects this changeset
 * touches (2 views + 9 typed combined-query facades + document_text +
 * remap_membership + manifest_verify + manifest_verify_all +
 * manifest_orphans + chash_conformance_report + gc_quarantine_orphans +
 * gc_restore_rereferenced + gc_expire_quarantine + purge_trash +
 * assign_from_chashes_384/768/1024) is therefore CALLED at least once
 * below, not merely proven to compile.
 */
class VectorsRepointFunctionsIntegrationTest {

    private static final String SVC_ROLE = "nexus_svc";
    private static final String SVC_PASS = "nexus_svc_pass";
    private static final String VECTORS_004 = "db/changelog-staged/vectors-004-unify-chunks.xml";
    private static final String TAXONOMY_007 = "db/changelog-staged/taxonomy-007-unify-centroids.xml";
    private static final String VECTORS_005 = "db/changelog-staged/vectors-005-repoint-functions-views.xml";

    private static final String TENANT = "t1";

    // ── Shared aged-box scaffold (mirrors the sibling unify tests) ──────────

    private record Rig(PostgreSQLContainer<?> pg, com.zaxxer.hikari.HikariDataSource adminDs) {
        void close() {
            adminDs.close();
            pg.stop();
        }
    }

    private static Rig newRig(String label) throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        String role = "nexus_admin_" + label;
        String pass = role + "_pass";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "CREATE ROLE " + role + " LOGIN PASSWORD '" + pass
                    + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
            su.createStatement().execute("GRANT CREATE ON DATABASE postgres TO " + role);
            su.createStatement().execute("GRANT CREATE ON SCHEMA public TO " + role);
            su.createStatement().execute("GRANT pg_monitor TO " + role + " WITH ADMIN OPTION");
            su.createStatement().execute(
                "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS
                    + "' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS");
            su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS vector");
            su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS pg_trgm");
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(role);
        cfg.setPassword(pass);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-admin-" + label);
        return new Rig(pg, new com.zaxxer.hikari.HikariDataSource(cfg));
    }

    private static void applyChangelog(com.zaxxer.hikari.HikariDataSource ds, String path) {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(path, new ClassLoaderResourceAccessor(), database)) {
                liquibase.update(new Contexts(), new LabelExpression());
            }
        } catch (SQLException e) {
            throw new MigrationException("Failed to obtain DB connection for migration", e);
        } catch (LiquibaseException e) {
            throw new MigrationException("Liquibase migration failed: " + path, e);
        }
    }

    /** Applies the full three-file repoint-batch chain in registration order. */
    private static void applyFullBatch(com.zaxxer.hikari.HikariDataSource ds) {
        applyChangelog(ds, VECTORS_004);
        applyChangelog(ds, TAXONOMY_007);
        applyChangelog(ds, VECTORS_005);
    }

    private static byte[] sha256(String s) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8));
    }

    private static String vectorLiteral(int dim, double fill) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < dim; i++) {
            if (i > 0) sb.append(',');
            sb.append(fill);
        }
        return sb.append(']').toString();
    }

    // ── Fixture: one document + one chunk + one manifest row + one centroid
    //    + one topic/topic_assignment + one catalog_link, all dim=384,
    //    seeded via the SUPERUSER connection (bypasses FORCE RLS entirely,
    //    matching the sibling tests' seedChunk/seedCentroid pattern). ─────────

    private record Fixture(byte[] chash, String chashHex, String collection, String docTumbler) {}

    private static Fixture seedFixture(PostgreSQLContainer<?> pg) throws Exception {
        byte[] chash = sha256("vectors-005 repoint fixture chunk text");
        String chashHex = bytesToHex(chash);
        String collection = "code__demo__minilm-l6-v2-384__v1";
        String docTumbler = "1.1";

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // nexus.topics carries an FK to catalog_collections (topics_collection_fk)
            // that neither nexus.chunks nor nexus.taxonomy_centroids requires
            // (vectors-004/taxonomy-007 headers: no collection FK on either unified
            // table). Register the collection first so the topics insert below succeeds.
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?) "
                        + "ON CONFLICT (tenant_id, name) DO NOTHING")) {
                ps.setString(1, TENANT);
                ps.setString(2, collection);
                ps.executeUpdate();
            }

            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_documents "
                        + "(tenant_id, tumbler, title, content_type, corpus, physical_collection) "
                        + "VALUES (?, ?, ?, ?, ?, ?)")) {
                ps.setString(1, TENANT);
                ps.setString(2, docTumbler);
                ps.setString(3, "Repoint fixture doc");
                ps.setString(4, "code");
                ps.setString(5, "code");
                ps.setString(6, collection);
                ps.executeUpdate();
            }

            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_document_chunks "
                        + "(tenant_id, doc_id, position, chash, collection) VALUES (?, ?, 0, ?, ?)")) {
                ps.setString(1, TENANT);
                ps.setString(2, docTumbler);
                ps.setBytes(3, chash);
                ps.setString(4, collection);
                ps.executeUpdate();
            }

            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384) "
                        + "VALUES (?, ?, ?, ?, ?::vector)")) {
                ps.setString(1, TENANT);
                ps.setString(2, collection);
                ps.setBytes(3, chash);
                ps.setString(4, "vectors-005 repoint fixture chunk text");
                ps.setString(5, vectorLiteral(384, 0.01));
                ps.executeUpdate();
            }

            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.taxonomy_centroids "
                        + "(tenant_id, collection, topic_id, embedding_384, label, doc_count) "
                        + "VALUES (?, ?, 1, ?::vector, 'alpha', 1)")) {
                ps.setString(1, TENANT);
                ps.setString(2, collection);
                ps.setString(3, vectorLiteral(384, 0.02));
                ps.executeUpdate();
            }

            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.topics (tenant_id, label, collection, doc_count, created_at) "
                        + "VALUES (?, 'alpha', ?, 1, now())")) {
                ps.setString(1, TENANT);
                ps.setString(2, collection);
                ps.executeUpdate();
            }

            try (Statement st = su.createStatement()) {
                st.execute(
                    "INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by) "
                        + "SELECT '" + TENANT + "', '" + chashHex + "', t.id, 'centroid' "
                        + "FROM nexus.topics t WHERE t.tenant_id = '" + TENANT + "' AND t.label = 'alpha'");
            }

            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_links "
                        + "(tenant_id, from_tumbler, to_tumbler, link_type, created_by) "
                        + "VALUES (?, ?, ?, 'cites', 'test')")) {
                ps.setString(1, TENANT);
                ps.setString(2, docTumbler);
                ps.setString(3, docTumbler);
                ps.executeUpdate();
            }
        }
        return new Fixture(chash, chashHex, collection, docTumbler);
    }

    private static String bytesToHex(byte[] bytes) {
        StringBuilder sb = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    private static void setTenantGuc(Connection conn, String tenant) throws SQLException {
        try (PreparedStatement ps = conn.prepareStatement("SELECT set_config('nexus.tenant', ?, false)")) {
            ps.setString(1, tenant);
            ps.executeQuery();
        }
    }

    // ── Test 1: the chain applies cleanly end to end (structural smoke) ─────

    @Test
    void chainedBatch_appliesCleanlyOverFreshInstall() throws Exception {
        Rig rig = newRig("chain");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            assertThatCode(() -> applyFullBatch(rig.adminDs()))
                .as("vectors-004 -> taxonomy-007 -> vectors-005 must chain cleanly over a fresh install")
                .doesNotThrowAnyException();

            try (Connection conn = rig.pg().createConnection("")) {
                for (String obj : new String[] {
                        "live_chunks", "collection_vector_stats"}) {
                    try (var rs = conn.createStatement().executeQuery(
                            "SELECT 1 FROM information_schema.views "
                                + "WHERE table_schema='nexus' AND table_name='" + obj + "'")) {
                        assertThat(rs.next()).as("view nexus.%s must exist", obj).isTrue();
                    }
                }
                for (String fn : new String[] {
                        "search_metadata_scoped_384", "search_metadata_scoped_768", "search_metadata_scoped_1024",
                        "search_topic_scoped_384", "search_topic_scoped_768", "search_topic_scoped_1024",
                        "search_graph_hop_384", "search_graph_hop_768", "search_graph_hop_1024",
                        "document_text", "remap_membership", "manifest_verify", "manifest_verify_all",
                        "manifest_orphans", "chash_conformance_report",
                        "gc_quarantine_orphans", "gc_restore_rereferenced", "gc_expire_quarantine",
                        "purge_trash",
                        "assign_from_chashes_384", "assign_from_chashes_768", "assign_from_chashes_1024"}) {
                    try (var rs = conn.createStatement().executeQuery(
                            "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                                + "WHERE n.nspname='nexus' AND p.proname='" + fn + "'")) {
                        assertThat(rs.next()).as("function nexus.%s must exist", fn).isTrue();
                    }
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 2: the nine typed combined-query facades, called for real ──────

    @Test
    void nineTypedFacades_eachCallableAgainstSeededData() throws Exception {
        Rig rig = newRig("facades");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            try (Connection conn = rig.pg().createConnection("")) {
                // search_metadata_scoped_384: must find the seeded chunk.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.search_metadata_scoped_384("
                            + "?::vector, ?, NULL, NULL, NULL, NULL, NULL, NULL, 10)")) {
                    ps.setString(1, vectorLiteral(384, 0.01));
                    ps.setArray(2, conn.createArrayOf("text", new String[] {fx.collection()}));
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).as("search_metadata_scoped_384 must find the seeded chunk").isTrue();
                    assertThat(rs.getString("id")).isEqualTo(fx.docTumbler());
                }
                // 768/1024: not seeded under those dims, so 0 rows is the correct
                // (not-erroring) answer -- proves the FROM/embedding_<dim> rewrite
                // does not throw on an empty match, not just on a hit.
                for (int dim : new int[] {768, 1024}) {
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT * FROM nexus.search_metadata_scoped_" + dim + "("
                                + "?::vector, ?, NULL, NULL, NULL, NULL, NULL, NULL, 10)")) {
                        ps.setString(1, vectorLiteral(dim, 0.01));
                        ps.setArray(2, conn.createArrayOf("text", new String[] {fx.collection()}));
                        assertThatCode(ps::executeQuery)
                            .as("search_metadata_scoped_%d must not throw on an empty-match query", dim)
                            .doesNotThrowAnyException();
                    }
                }

                // search_topic_scoped_384: must find the seeded chunk via topic_assignments.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.search_topic_scoped_384(?::vector, 'alpha', ?, 10)")) {
                    ps.setString(1, vectorLiteral(384, 0.01));
                    ps.setString(2, fx.collection());
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).as("search_topic_scoped_384 must find the seeded chunk").isTrue();
                }
                for (int dim : new int[] {768, 1024}) {
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT * FROM nexus.search_topic_scoped_" + dim + "(?::vector, 'alpha', ?, 10)")) {
                        ps.setString(1, vectorLiteral(dim, 0.01));
                        ps.setString(2, fx.collection());
                        assertThatCode(ps::executeQuery)
                            .as("search_topic_scoped_%d must not throw", dim)
                            .doesNotThrowAnyException();
                    }
                }

                // search_graph_hop_384: seed is its own link target (self-loop
                // 'cites'), depth 1, direction both, from itself as the seed.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.search_graph_hop_384("
                            + "?::vector, ?, ?, NULL, 1, 'both', NULL, 10)")) {
                    ps.setString(1, vectorLiteral(384, 0.01));
                    ps.setArray(2, conn.createArrayOf("text", new String[] {fx.docTumbler()}));
                    ps.setArray(3, conn.createArrayOf("text", new String[] {fx.collection()}));
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).as("search_graph_hop_384 must find the seeded chunk via the self-loop link").isTrue();
                }
                for (int dim : new int[] {768, 1024}) {
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT * FROM nexus.search_graph_hop_" + dim + "("
                                + "?::vector, ?, ?, NULL, 1, 'both', NULL, 10)")) {
                        ps.setString(1, vectorLiteral(dim, 0.01));
                        ps.setArray(2, conn.createArrayOf("text", new String[] {fx.docTumbler()}));
                        ps.setArray(3, conn.createArrayOf("text", new String[] {fx.collection()}));
                        assertThatCode(ps::executeQuery)
                            .as("search_graph_hop_%d must not throw", dim)
                            .doesNotThrowAnyException();
                    }
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 3: document_text / remap_membership / manifest_verify family /
    //    manifest_orphans / chash_conformance_report -- the "dim collapses
    //    to one reference" and "dim stays branched for routing" buckets. ────

    @Test
    void collapsedAndBranchedReaders_eachCallableAgainstSeededData() throws Exception {
        Rig rig = newRig("readers");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);

                // document_text: ordered manifest join reconstruction.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.document_text(?)")) {
                    ps.setString(1, fx.docTumbler());
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).as("document_text must resolve the seeded manifest row").isTrue();
                    assertThat(rs.getString("chunk_text")).isEqualTo("vectors-005 repoint fixture chunk text");
                }

                // manifest_verify: referenced=1, present=1, missing=0.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.manifest_verify(?)")) {
                    ps.setString(1, fx.docTumbler());
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getLong("referenced")).isEqualTo(1L);
                    assertThat(rs.getLong("present")).isEqualTo(1L);
                    assertThat(rs.getLong("missing")).isEqualTo(0L);
                }

                // manifest_verify_all: same fixture, grouped by collection.
                try (var rs = conn.createStatement().executeQuery(
                        "SELECT * FROM nexus.manifest_verify_all()")) {
                    assertThat(rs.next()).as("manifest_verify_all must return a row for the seeded collection").isTrue();
                }

                // remap_membership: no chash_remap facts seeded -> mapped_total=0,
                // present_count=0. The point is that it does not throw (the
                // three-EXISTS-collapses-to-one rewrite) not the row content.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.remap_membership(?, ?)")) {
                    ps.setString(1, "some-source-collection");
                    ps.setString(2, fx.collection());
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getLong("mapped_total")).isEqualTo(0L);
                }

                // manifest_orphans(384): seeded chunk IS referenced, so it must
                // NOT appear as an orphan for dim 384.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.manifest_orphans(384)")) {
                    var rs = ps.executeQuery();
                    boolean sawFixtureChash = false;
                    while (rs.next()) {
                        if (fx.chashHex().equals(rs.getString("chash"))) sawFixtureChash = true;
                    }
                    assertThat(sawFixtureChash)
                        .as("the seeded, manifest-and-chunk-present chash must NOT be reported as an orphan")
                        .isFalse();
                }
                for (int dim : new int[] {768, 1024}) {
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT * FROM nexus.manifest_orphans(?)")) {
                        ps.setInt(1, dim);
                        assertThatCode(ps::executeQuery).as("manifest_orphans(%d) must not throw", dim)
                            .doesNotThrowAnyException();
                    }
                }

                // chash_conformance_report(384): the seeded chunk's chash is a
                // real 32-byte sha256 digest, so non_conformant must be 0 for the
                // nexus.chunks row and total >= 1.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.chash_conformance_report(384)")) {
                    var rs = ps.executeQuery();
                    boolean sawChunksRow = false;
                    while (rs.next()) {
                        if (rs.getString("table_name").startsWith("nexus.chunks")) {
                            sawChunksRow = true;
                            assertThat(rs.getLong("total")).isGreaterThanOrEqualTo(1L);
                            assertThat(rs.getLong("non_conformant")).isEqualTo(0L);
                        }
                    }
                    assertThat(sawChunksRow).isTrue();
                }
                for (int dim : new int[] {768, 1024}) {
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT * FROM nexus.chash_conformance_report(?)")) {
                        ps.setInt(1, dim);
                        assertThatCode(ps::executeQuery).as("chash_conformance_report(%d) must not throw", dim)
                            .doesNotThrowAnyException();
                    }
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 4: the GC writers + purge_trash -- dim-agnostic collapse,
    //    called for real against the unified table. ─────────────────────────

    @Test
    void gcWritersAndPurgeTrash_eachCallableAgainstSeededData() throws Exception {
        Rig rig = newRig("gc");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);

                // gc_quarantine_orphans: the seeded chunk IS referenced by a
                // manifest row, so nothing should move (moved=0) -- proves the
                // dim-agnostic NOT EXISTS rewrite against nexus.chunks runs
                // clean, not that it moves anything.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.gc_quarantine_orphans(384, ?, ?, ?, '2026-01-01T00:00:00Z', 20)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, fx.collection());
                    ps.setString(3, "quarantine__demo__minilm-l6-v2-384__v1");
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getLong("moved")).isEqualTo(0L);
                }
                for (int dim : new int[] {768, 1024}) {
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT * FROM nexus.gc_quarantine_orphans(?, ?, ?, ?, '2026-01-01T00:00:00Z', 20)")) {
                        ps.setInt(1, dim);
                        ps.setString(2, TENANT);
                        ps.setString(3, "nonexistent-collection-" + dim);
                        ps.setString(4, "quarantine-" + dim);
                        assertThatCode(ps::executeQuery).as("gc_quarantine_orphans(%d) must not throw", dim)
                            .doesNotThrowAnyException();
                    }
                }

                // gc_restore_rereferenced: no quarantined rows exist yet -> 0.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT nexus.gc_restore_rereferenced(384, ?, ?, ?)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, "quarantine__demo__minilm-l6-v2-384__v1");
                    ps.setString(3, fx.collection());
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getLong(1)).isEqualTo(0L);
                }

                // gc_expire_quarantine: nothing quarantined -> expired=0, refused=0.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.gc_expire_quarantine(384, ?, ?, ?, '2099-01-01T00:00:00Z', 0.5, 100, false)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, "quarantine__demo__minilm-l6-v2-384__v1");
                    ps.setString(3, fx.collection());
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getLong("expired")).isEqualTo(0L);
                    assertThat(rs.getLong("refused")).isEqualTo(0L);
                }

                // purge_trash: no tombstoned documents -> 0 rows purged, and the
                // manifest-referenced seeded chunk must SURVIVE the sweep.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT nexus.purge_trash(?::interval)")) {
                    ps.setString(1, "0 seconds");
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getLong(1)).isEqualTo(0L);
                }
                try (var rs = conn.createStatement().executeQuery(
                        "SELECT count(*) FROM nexus.chunks WHERE tenant_id = '" + TENANT + "'")) {
                    rs.next();
                    assertThat(rs.getLong(1))
                        .as("purge_trash must not sweep a chunk still protected by a live manifest row")
                        .isEqualTo(1L);
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 5: assign_from_chashes_384/768/1024, retargeted to BOTH unified
    //    tables (o8dil.47 "one era" ruling). ─────────────────────────────────

    @Test
    void assignFromChashes_ownPass_persistsAssignmentAgainstBothUnifiedTables() throws Exception {
        Rig rig = newRig("assign");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);

                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.assign_from_chashes_384(?, ?, false)")) {
                    ps.setString(1, fx.collection());
                    ps.setArray(2, conn.createArrayOf("text", new String[] {fx.chashHex()}));
                    var rs = ps.executeQuery();
                    assertThat(rs.next())
                        .as("assign_from_chashes_384 must find the seeded chunk against the seeded centroid")
                        .isTrue();
                    assertThat(rs.getString("chash")).isEqualTo(fx.chashHex());
                    assertThat(rs.getLong("topic_id")).isEqualTo(1L);
                }
                for (int dim : new int[] {768, 1024}) {
                    try (PreparedStatement ps = conn.prepareStatement(
                            "SELECT * FROM nexus.assign_from_chashes_" + dim + "(?, ?, false)")) {
                        ps.setString(1, "nonexistent-collection-" + dim);
                        ps.setArray(2, conn.createArrayOf("text", new String[] {fx.chashHex()}));
                        assertThatCode(ps::executeQuery)
                            .as("assign_from_chashes_%d must not throw on an empty-match call", dim)
                            .doesNotThrowAnyException();
                    }
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 6: the two views, queried directly. ─────────────────────────────

    @Test
    void liveChunksAndCollectionVectorStats_queryableAgainstSeededData() throws Exception {
        Rig rig = newRig("views");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            try (Connection conn = rig.pg().createConnection("")) {
                try (var rs = conn.createStatement().executeQuery(
                        "SELECT * FROM nexus.live_chunks WHERE tenant_id = '" + TENANT + "'")) {
                    assertThat(rs.next()).as("live_chunks must expose the seeded, manifest-referenced chunk").isTrue();
                    assertThat(rs.getInt("dim"))
                        .as("live_chunks.dim must be DERIVED from which embedding_<dim> column is populated")
                        .isEqualTo(384);
                }
                try (var rs = conn.createStatement().executeQuery(
                        "SELECT * FROM nexus.collection_vector_stats WHERE tenant_id = '" + TENANT + "'")) {
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getInt("dim")).isEqualTo(384);
                    assertThat(rs.getLong("chunk_count")).isEqualTo(1L);
                }
            }
        } finally {
            rig.close();
        }
    }
}
