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
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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
 *
 * <p><strong>RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15:</strong> {@code
 * manifest_verify_all} and {@code manifest_orphans} coverage below was
 * DELETED — both functions are DROPPED (catalog-030-retire-manifest-
 * verify.xml), which {@link SchemaMigrator#migrate} applies to HEAD before
 * {@link #applyFullBatch} re-chains vectors-004/taxonomy-007/vectors-005 as
 * a (now-no-op, already-recorded) standalone sequence — so by the time this
 * file's assertions run, both functions are already gone from the fixture
 * database. {@code manifest_verify} coverage is KEPT — that function is
 * NOT dropped (completeIndexRun depends on it).
 *
 * <p><strong>Statement-level coverage (2026-08-13 round 2, substantive-critic
 * finding, T2 nexus/rdr-191-repoint-batch-step-a-critique-2026-08-13
 * [22454]):</strong> a CALLED function is not the same as an EXECUTED
 * statement -- {@code gcWritersAndPurgeTrash_eachCallableAgainstSeededData}
 * above always seeds a manifest-referenced fixture, so every gc_* call there
 * trips its early-return guard before reaching its retargeted
 * INSERT/ON CONFLICT/DELETE. {@code gcWriters_actuallyExecuteRetargetedStatements_
 * postStateAsserted} seeds a genuinely orphaned chunk so
 * gc_quarantine_orphans, gc_restore_rereferenced, and gc_expire_quarantine's
 * retargeted statements ACTUALLY RUN, with post-state row assertions rather
 * than must-not-throw alone.
 *
 * <p><strong>Cross-dim collision guard (Critical-1 fix, same T2 entry):</strong>
 * {@code gcQuarantineOrphans_crossDimCollisionAtQuarantineTarget_
 * raisesAndPreservesBothRows} and {@code gcRestoreRereferenced_
 * crossDimCollisionAtOriginTarget_raisesAndPreservesBothRows} construct a
 * real (tenant, collection, chash) collision across two different populated
 * embedding_&lt;dim&gt; columns and assert the RAISE fires with both rows
 * surviving untouched -- proven to fail against the pre-fix body (no guard
 * existed; the ON CONFLICT ... EXCLUDED copy silently overwrote the
 * pre-existing row instead).
 *
 * <p><strong>Tombstone-relay guard (Critical-3 fix, T2 nexus/review-rdr191-
 * stepA-critical-graphhop-tombstone-regression-2026-08-13 [22455]):</strong>
 * {@code searchGraphHop_tombstonedRelay_blocksReachabilityBeyondIt} proves
 * the recursive traversal itself gates on deleted_at (not just the final
 * row) -- proven to fail against the pre-fix body, which silently dropped
 * catalog-019's fd/td JOINs during the retarget.
 */
class VectorsRepointFunctionsIntegrationTest {

    private static final String SVC_ROLE = "nexus_svc";
    private static final String SVC_PASS = "nexus_svc_pass";
    private static final String VECTORS_004 = "db/changelog/vectors-004-unify-chunks.xml";
    private static final String TAXONOMY_007 = "db/changelog/taxonomy-007-unify-centroids.xml";
    private static final String VECTORS_005 = "db/changelog/vectors-005-repoint-functions-views.xml";

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

            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk requires the
            // nexus.chunks row to exist before the manifest row referencing it is
            // inserted — chunk insert MUST precede the manifest insert below.
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
                    "INSERT INTO nexus.catalog_document_chunks "
                        + "(tenant_id, doc_id, position, chash, collection) VALUES (?, ?, 0, ?, ?)")) {
                ps.setString(1, TENANT);
                ps.setString(2, docTumbler);
                ps.setBytes(3, chash);
                ps.setString(4, collection);
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
                    "INSERT INTO nexus.topic_assignments "
                        + "(tenant_id, doc_id, topic_id, assigned_by, source_collection) "
                        + "SELECT '" + TENANT + "', '" + chashHex + "', t.id, 'centroid', '" + collection + "' "
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
                        // manifest_verify_all/manifest_orphans REMOVED here (RDR-191
                        // Phase 6, nexus-o8dil.33) — both dropped by catalog-030,
                        // already applied to HEAD by SchemaMigrator.migrate above.
                        // remap_membership REMOVED here (nexus-lgdel.l2) — dropped by
                        // legacy-002-drop-remap-membership.xml, same reason.
                        "document_text", "manifest_verify",
                        "chash_conformance_report",
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

    // ── Test 2b: the nine combined-query functions still number NINE ────────
    //
    // RDR-191 Phase 6 (nexus-o8dil.33) mechanical-exclusion pin, Decision
    // item 4 / F7 risk 4: "the 9 combined-query stored functions do not
    // disappear... write a TEST asserting they still number 9." Distinct
    // from nineTypedFacades_eachCallableAgainstSeededData above (which calls
    // each by NAME and would not itself notice a 10th appearing or one of
    // the nine vanishing) — this counts pg_proc rows matching the naming
    // pattern directly, on the SAME post-catalog-030 HEAD schema (Class B/
    // manifest_orphans/manifest_verify_all/manifest_backfill are dropped by
    // that changeset; these nine are explicitly NOT).

    @Test
    void combinedQueryFunctions_stillNumberNine() throws Exception {
        Rig rig = newRig("ninecount");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());

            try (Connection conn = rig.pg().createConnection("")) {
                try (var rs = conn.createStatement().executeQuery(
                        "SELECT p.proname FROM pg_proc p "
                            + "JOIN pg_namespace n ON n.oid = p.pronamespace "
                            + "WHERE n.nspname = 'nexus' AND ("
                            + "p.proname LIKE 'search_metadata_scoped_%' "
                            + "OR p.proname LIKE 'search_topic_scoped_%' "
                            + "OR p.proname LIKE 'search_graph_hop_%') "
                            + "ORDER BY p.proname")) {
                    var names = new java.util.ArrayList<String>();
                    while (rs.next()) names.add(rs.getString("proname"));
                    assertThat(names)
                        .as("the 9 combined-query facades (3 verbs x 3 dims) must "
                            + "still number 9 post-RDR-191-Phase-6 — the dim "
                            + "COUNT does not collapse (F7 risk 4), only "
                            + "manifest_orphans/manifest_verify_all/"
                            + "manifest_backfill are dropped by catalog-030: %s",
                            names)
                        .hasSize(9)
                        .containsExactlyInAnyOrder(
                            "search_metadata_scoped_384", "search_metadata_scoped_768",
                            "search_metadata_scoped_1024",
                            "search_topic_scoped_384", "search_topic_scoped_768",
                            "search_topic_scoped_1024",
                            "search_graph_hop_384", "search_graph_hop_768",
                            "search_graph_hop_1024");
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 3: document_text / manifest_verify /
    //    chash_conformance_report -- the "dim collapses to one reference"
    //    and "dim stays branched for routing" buckets. (manifest_orphans
    //    coverage DELETED, RDR-191 Phase 6 nexus-o8dil.33 — function
    //    dropped. remap_membership coverage DELETED, nexus-lgdel.l2 — function
    //    dropped, legacy-002-drop-remap-membership.xml.) ─────────────────

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

                // manifest_verify_all coverage DELETED (RDR-191 Phase 6,
                // nexus-o8dil.33) — the function is dropped.

                // remap_membership coverage DELETED (nexus-lgdel.l2) — the
                // function is dropped (legacy-002-drop-remap-membership.xml);
                // the orphaned GET /v1/remap/membership read surface is gone
                // with it.

                // manifest_orphans coverage DELETED (RDR-191 Phase 6,
                // nexus-o8dil.33) — the function is dropped.

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

    // ── Test 4b: the three gc_* functions' RETARGETED INSERT/ON CONFLICT/
    //    DELETE statements, ACTUALLY EXECUTED (Critical-2 fix, T2 [22454]):
    //    test 4 above only ever seeds a manifest-referenced fixture, so
    //    every gc_* call there trips its early-return guard before reaching
    //    the retargeted statements -- R8 claimed but not delivered for the
    //    highest-risk lines. This test seeds a genuinely ORPHANED chunk so
    //    gc_quarantine_orphans's copy-INSERT + DELETE run for real, then
    //    re-references it so gc_restore_rereferenced's copy-INSERT + DELETE
    //    run for real, then seeds an independent already-quarantined,
    //    unreferenced, past-cutoff row so gc_expire_quarantine's DELETE
    //    runs for real -- with post-state assertions on the underlying
    //    nexus.chunks rows, not merely "did not throw". ────────────────────

    @Test
    void gcWriters_actuallyExecuteRetargetedStatements_postStateAsserted() throws Exception {
        Rig rig = newRig("gcreal");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            String origin = fx.collection();
            String quarantineFlow = "quarantine__demo__minilm-l6-v2-384__v2";
            byte[] orphanChash = sha256("gc real-execution orphan chunk text");

            try (Connection su = rig.pg().createConnection("")) {
                su.setAutoCommit(true);
                // A genuinely orphaned chunk: present in nexus.chunks, NOT
                // referenced by any catalog_document_chunks row.
                try (PreparedStatement ps = su.prepareStatement(
                        "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384) "
                            + "VALUES (?, ?, ?, ?, ?::vector)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, origin);
                    ps.setBytes(3, orphanChash);
                    ps.setString(4, "gc real-execution orphan chunk text");
                    ps.setString(5, vectorLiteral(384, 0.10));
                    ps.executeUpdate();
                }
            }

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);

                // ── gc_quarantine_orphans: the orphan MUST move, the
                //    manifest-referenced fixture row MUST NOT. ────────────
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.gc_quarantine_orphans(384, ?, ?, ?, '2020-01-01T00:00:00Z', 20)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, origin);
                    ps.setString(3, quarantineFlow);
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getLong("moved"))
                        .as("only the genuinely orphaned chunk moves; the manifest-referenced fixture stays")
                        .isEqualTo(1L);
                }
                assertRowExists(conn, quarantineFlow, orphanChash,
                    "the orphan must actually be present in the quarantine collection after the copy-INSERT");
                assertRowAbsent(conn, origin, orphanChash,
                    "the orphan must actually be GONE from the origin collection after the DELETE");
                assertRowExists(conn, origin, fx.chash(),
                    "the manifest-referenced fixture chunk must survive untouched");

                // ── gc_restore_rereferenced: re-reference the orphan at
                //    origin, then it MUST come back. ─────────────────────
                //
                // RDR-191 Phase 5 (nexus-o8dil.29) / nexus-zlv38: at this exact
                // instant the orphan's nexus.chunks row sits ONLY in the
                // quarantine collection (moved by gc_quarantine_orphans above)
                // -- NOT at origin. fk_catalog_chunks_chunk now rejects a
                // manifest write that re-references a chash before
                // gc_restore_rereferenced has copied it back to origin; that
                // production-behavior gap is tracked by nexus-zlv38, out of
                // scope for this changeset. Bypass the FK locally for this one
                // insert so the test can keep proving gc_restore_rereferenced's
                // own copy-back + delete-from-quarantine behavior.
                try (Connection ddl = rig.pg().createConnection("")) {
                    ddl.setAutoCommit(true);
                    ddl.createStatement().execute(
                        "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk");
                }
                try (PreparedStatement ps = conn.prepareStatement(
                        "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
                            + "VALUES (?, ?, 1, ?, ?)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, fx.docTumbler());
                    ps.setBytes(3, orphanChash);
                    ps.setString(4, origin);
                    ps.executeUpdate();
                }
                try (Connection ddl = rig.pg().createConnection("")) {
                    ddl.setAutoCommit(true);
                    ddl.createStatement().execute(
                        "ALTER TABLE nexus.catalog_document_chunks "
                        + "ADD CONSTRAINT fk_catalog_chunks_chunk "
                        + "FOREIGN KEY (tenant_id, collection, chash) REFERENCES nexus.chunks (tenant_id, collection, chash) "
                        + "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID");
                }
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT nexus.gc_restore_rereferenced(384, ?, ?, ?)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, quarantineFlow);
                    ps.setString(3, origin);
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getLong(1)).as("the re-referenced chash must be restored").isEqualTo(1L);
                }
                assertRowExists(conn, origin, orphanChash,
                    "the restored chunk must actually be back in the origin collection after the copy-INSERT");
                assertRowAbsent(conn, quarantineFlow, orphanChash,
                    "the restored chunk must actually be GONE from the quarantine collection after the DELETE");

                // ── gc_expire_quarantine: an independent, already-
                //    quarantined, unreferenced, past-cutoff row MUST be
                //    hard-deleted. ────────────────────────────────────────
                byte[] expireChash = sha256("gc real-execution expire chunk text");
                try (Connection su = rig.pg().createConnection("")) {
                    su.setAutoCommit(true);
                    try (PreparedStatement ps = su.prepareStatement(
                            "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384, metadata) "
                                + "VALUES (?, ?, ?, ?, ?::vector, ?::jsonb)")) {
                        ps.setString(1, TENANT);
                        ps.setString(2, quarantineFlow);
                        ps.setBytes(3, expireChash);
                        ps.setString(4, "gc real-execution expire chunk text");
                        ps.setString(5, vectorLiteral(384, 0.11));
                        ps.setString(6, "{\"quarantined_at\":\"2020-01-01T00:00:00Z\",\"origin_collection\":\""
                            + origin + "\"}");
                        ps.executeUpdate();
                    }
                }
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.gc_expire_quarantine(384, ?, ?, ?, '2025-01-01T00:00:00Z', 0.5, 100, false)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, quarantineFlow);
                    ps.setString(3, origin);
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getLong("expired")).isEqualTo(1L);
                    assertThat(rs.getLong("refused")).isEqualTo(0L);
                }
                assertRowAbsent(conn, quarantineFlow, expireChash,
                    "the expired row must actually be hard-deleted from nexus.chunks");
            }
        } finally {
            rig.close();
        }
    }

    private static void assertRowExists(Connection conn, String collection, byte[] chash, String message)
            throws Exception {
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT count(*) FROM nexus.chunks WHERE tenant_id = ? AND collection = ? AND chash = ?")) {
            ps.setString(1, TENANT);
            ps.setString(2, collection);
            ps.setBytes(3, chash);
            var rs = ps.executeQuery();
            rs.next();
            assertThat(rs.getLong(1)).as(message).isEqualTo(1L);
        }
    }

    private static void assertRowAbsent(Connection conn, String collection, byte[] chash, String message)
            throws Exception {
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT count(*) FROM nexus.chunks WHERE tenant_id = ? AND collection = ? AND chash = ?")) {
            ps.setString(1, TENANT);
            ps.setString(2, collection);
            ps.setBytes(3, chash);
            var rs = ps.executeQuery();
            rs.next();
            assertThat(rs.getLong(1)).as(message).isEqualTo(0L);
        }
    }

    // ── Test 4c: cross-dim (tenant, collection, chash) collision guard
    //    (Critical-1 fix, T2 [22454]) -- the unified PK's shared namespace
    //    means an orphan sharing its chash with an already-quarantined row
    //    under a DIFFERENT dim must RAISE, never silently overwrite via the
    //    blind ON CONFLICT ... EXCLUDED pattern. Verified to FAIL against
    //    the pre-fix body during this fix's own development (the guard did
    //    not exist; the copy-INSERT silently clobbered the pre-existing
    //    row instead of raising). ────────────────────────────────────────

    @Test
    void gcQuarantineOrphans_crossDimCollisionAtQuarantineTarget_raisesAndPreservesBothRows() throws Exception {
        Rig rig = newRig("gccollisionq");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            byte[] collisionChash = sha256("cross-dim collision fixture chunk text");
            String quarantineShared = "quarantine__demo__shared__v1";

            try (Connection su = rig.pg().createConnection("")) {
                su.setAutoCommit(true);
                // RDR-191 Phase 5 (nexus-o8dil.49): nexus.chunks now carries
                // chunks_collection_fk (tenant_id, collection) -> catalog_collections
                // (tenant_id, name) — register the quarantine target first.
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                    + TENANT + "', '" + quarantineShared + "') ON CONFLICT (tenant_id, name) DO NOTHING");
                // Pre-existing quarantined row under dim 768, at the SHARED
                // quarantine target, same chash.
                try (PreparedStatement ps = su.prepareStatement(
                        "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_768, metadata) "
                            + "VALUES (?, ?, ?, ?, ?::vector, ?::jsonb)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, quarantineShared);
                    ps.setBytes(3, collisionChash);
                    ps.setString(4, "existing 768-dim quarantined chunk");
                    ps.setString(5, vectorLiteral(768, 0.05));
                    ps.setString(6, "{\"quarantined_at\":\"2026-01-01T00:00:00Z\",\"origin_collection\":\"some-768-collection\"}");
                    ps.executeUpdate();
                }
                // A DIFFERENT, orphaned 384-dim chunk in fx.collection() that
                // happens to share the same chash -- the shared-quarantine-
                // naming gap the critique flagged (VectorHandler derives
                // quarantine_collection by convention, not server-enforced
                // per-dim distinctness).
                try (PreparedStatement ps = su.prepareStatement(
                        "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384) "
                            + "VALUES (?, ?, ?, ?, ?::vector)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, fx.collection());
                    ps.setBytes(3, collisionChash);
                    ps.setString(4, "colliding 384-dim orphan chunk");
                    ps.setString(5, vectorLiteral(384, 0.06));
                    ps.executeUpdate();
                }
            }

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT * FROM nexus.gc_quarantine_orphans(384, ?, ?, ?, '2026-01-01T00:00:00Z', 20)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, fx.collection());
                    ps.setString(3, quarantineShared);
                    assertThatThrownBy(ps::executeQuery)
                        .as("a cross-dim (tenant,collection,chash) collision at the quarantine target must "
                            + "RAISE, never silently overwrite the pre-existing row via blind "
                            + "ON CONFLICT ... EXCLUDED (Critical-1, T2 22454)")
                        .hasMessageContaining("cross-dim");
                }

                // Post-state: the pre-existing 768-dim quarantined row is UNTOUCHED.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT chunk_text, embedding_384 IS NULL AS no384, embedding_768 IS NOT NULL AS has768 "
                            + "FROM nexus.chunks WHERE tenant_id = ? AND collection = ? AND chash = ?")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, quarantineShared);
                    ps.setBytes(3, collisionChash);
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).as("the pre-existing quarantined row must still exist").isTrue();
                    assertThat(rs.getString("chunk_text")).isEqualTo("existing 768-dim quarantined chunk");
                    assertThat(rs.getBoolean("no384")).as("embedding_384 must NOT have been set by the failed copy").isTrue();
                    assertThat(rs.getBoolean("has768")).as("embedding_768 must remain populated, untouched").isTrue();
                }
                // And the origin orphan row survives too -- the RAISE aborts
                // the whole statement/transaction before the DELETE runs.
                assertRowExists(conn, fx.collection(), collisionChash,
                    "the origin orphan row must survive -- the guard prevents the whole move, not just the copy");
            }
        } finally {
            rig.close();
        }
    }

    @Test
    void gcRestoreRereferenced_crossDimCollisionAtOriginTarget_raisesAndPreservesBothRows() throws Exception {
        Rig rig = newRig("gccollisionr");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());
            Fixture fx = seedFixture(rig.pg());

            byte[] collisionChash = sha256("restore cross-dim collision fixture chunk text");
            String quarantineCollection = "quarantine__demo__restore-collision__v1";

            try (Connection su = rig.pg().createConnection("")) {
                su.setAutoCommit(true);
                // RDR-191 Phase 5 (nexus-o8dil.49): nexus.chunks now carries
                // chunks_collection_fk (tenant_id, collection) -> catalog_collections
                // (tenant_id, name) — register the quarantine target first.
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                    + TENANT + "', '" + quarantineCollection + "') ON CONFLICT (tenant_id, name) DO NOTHING");
                // Pre-existing row ALREADY at the origin collection, under a
                // DIFFERENT dim (1024) than the row being restored (384).
                try (PreparedStatement ps = su.prepareStatement(
                        "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_1024) "
                            + "VALUES (?, ?, ?, ?, ?::vector)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, fx.collection());
                    ps.setBytes(3, collisionChash);
                    ps.setString(4, "existing 1024-dim origin row");
                    ps.setString(5, vectorLiteral(1024, 0.07));
                    ps.executeUpdate();
                }
                // The quarantined row to be restored, dim 384.
                try (PreparedStatement ps = su.prepareStatement(
                        "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384, metadata) "
                            + "VALUES (?, ?, ?, ?, ?::vector, ?::jsonb)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, quarantineCollection);
                    ps.setBytes(3, collisionChash);
                    ps.setString(4, "quarantined 384-dim chunk to restore");
                    ps.setString(5, vectorLiteral(384, 0.08));
                    ps.setString(6, "{\"quarantined_at\":\"2026-01-01T00:00:00Z\",\"origin_collection\":\""
                        + fx.collection() + "\"}");
                    ps.executeUpdate();
                }
                // Manifest row re-referencing it at the origin -- makes it a
                // genuine restore candidate.
                try (PreparedStatement ps = su.prepareStatement(
                        "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
                            + "VALUES (?, ?, 2, ?, ?)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, fx.docTumbler());
                    ps.setBytes(3, collisionChash);
                    ps.setString(4, fx.collection());
                    ps.executeUpdate();
                }
            }

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT nexus.gc_restore_rereferenced(384, ?, ?, ?)")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, quarantineCollection);
                    ps.setString(3, fx.collection());
                    assertThatThrownBy(ps::executeQuery)
                        .as("a cross-dim (tenant,collection,chash) collision at the restore target must "
                            + "RAISE, never silently overwrite the pre-existing row (Critical-1, T2 22454)")
                        .hasMessageContaining("cross-dim");
                }

                // Post-state: the pre-existing 1024-dim origin row is UNTOUCHED.
                try (PreparedStatement ps = conn.prepareStatement(
                        "SELECT chunk_text, embedding_384 IS NULL AS no384, embedding_1024 IS NOT NULL AS has1024 "
                            + "FROM nexus.chunks WHERE tenant_id = ? AND collection = ? AND chash = ?")) {
                    ps.setString(1, TENANT);
                    ps.setString(2, fx.collection());
                    ps.setBytes(3, collisionChash);
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).as("the pre-existing origin row must still exist").isTrue();
                    assertThat(rs.getString("chunk_text")).isEqualTo("existing 1024-dim origin row");
                    assertThat(rs.getBoolean("no384")).as("embedding_384 must NOT have been set by the failed copy").isTrue();
                    assertThat(rs.getBoolean("has1024")).as("embedding_1024 must remain populated, untouched").isTrue();
                }
                // And the quarantined row survives too -- the RAISE aborts
                // before the DELETE runs.
                assertRowExists(conn, quarantineCollection, collisionChash,
                    "the quarantined row must survive -- the guard prevents the whole move, not just the copy");
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 4d: search_graph_hop's tombstone-relay guard, carried forward
    //    from catalog-019 (Critical-3 fix, T2 [22455]) -- the recursive
    //    traversal itself must gate on deleted_at, not just the final row,
    //    or a tombstoned document can still act as a live relay forwarding
    //    reachability to documents beyond it. Verified to FAIL against the
    //    pre-fix body during this fix's own development (the fd/td JOINs
    //    were silently dropped in the retarget). ─────────────────────────

    @Test
    void searchGraphHop_tombstonedRelay_blocksReachabilityBeyondIt() throws Exception {
        Rig rig = newRig("graphhoptomb");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyFullBatch(rig.adminDs());

            String collection = "code__relay__minilm-l6-v2-384__v1";
            byte[] chashA = sha256("relay-a chunk text");
            byte[] chashB = sha256("relay-b chunk text");
            byte[] chashC = sha256("relay-c chunk text");

            try (Connection su = rig.pg().createConnection("")) {
                su.setAutoCommit(true);
                // RDR-191 Phase 5 (nexus-o8dil.49): nexus.chunks now carries
                // chunks_collection_fk (tenant_id, collection) -> catalog_collections
                // (tenant_id, name) — register the collection first.
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                    + TENANT + "', '" + collection + "') ON CONFLICT (tenant_id, name) DO NOTHING");
                for (var doc : new Object[][] {{"relay-a", chashA}, {"relay-b", chashB}, {"relay-c", chashC}}) {
                    String tumbler = (String) doc[0];
                    byte[] chash = (byte[]) doc[1];
                    try (PreparedStatement ps = su.prepareStatement(
                            "INSERT INTO nexus.catalog_documents "
                                + "(tenant_id, tumbler, title, content_type, corpus, physical_collection) "
                                + "VALUES (?, ?, ?, 'code', 'code', ?)")) {
                        ps.setString(1, TENANT);
                        ps.setString(2, tumbler);
                        ps.setString(3, "Relay doc " + tumbler);
                        ps.setString(4, collection);
                        ps.executeUpdate();
                    }
                    // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk
                    // requires the nexus.chunks row before the manifest row below.
                    try (PreparedStatement ps = su.prepareStatement(
                            "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384) "
                                + "VALUES (?, ?, ?, ?, ?::vector)")) {
                        ps.setString(1, TENANT);
                        ps.setString(2, collection);
                        ps.setBytes(3, chash);
                        ps.setString(4, tumbler + " chunk text");
                        ps.setString(5, vectorLiteral(384, 0.09));
                        ps.executeUpdate();
                    }
                    try (PreparedStatement ps = su.prepareStatement(
                            "INSERT INTO nexus.catalog_document_chunks "
                                + "(tenant_id, doc_id, position, chash, collection) VALUES (?, ?, 0, ?, ?)")) {
                        ps.setString(1, TENANT);
                        ps.setString(2, tumbler);
                        ps.setBytes(3, chash);
                        ps.setString(4, collection);
                        ps.executeUpdate();
                    }
                }
                for (var edge : new String[][] {{"relay-a", "relay-b"}, {"relay-b", "relay-c"}}) {
                    try (PreparedStatement ps = su.prepareStatement(
                            "INSERT INTO nexus.catalog_links "
                                + "(tenant_id, from_tumbler, to_tumbler, link_type, created_by) "
                                + "VALUES (?, ?, ?, 'cites', 'test')")) {
                        ps.setString(1, TENANT);
                        ps.setString(2, edge[0]);
                        ps.setString(3, edge[1]);
                        ps.executeUpdate();
                    }
                }
            }

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);
                assertThat(callGraphHop384(conn, collection, "relay-a", 2))
                    .as("baseline: relay-c reachable at depth 2 via live relay-b")
                    .containsExactlyInAnyOrder("relay-a", "relay-b", "relay-c");
            }

            try (Connection su = rig.pg().createConnection("")) {
                su.setAutoCommit(true);
                try (PreparedStatement ps = su.prepareStatement(
                        "UPDATE nexus.catalog_documents SET deleted_at = now() "
                            + "WHERE tenant_id = ? AND tumbler = 'relay-b'")) {
                    ps.setString(1, TENANT);
                    ps.executeUpdate();
                }
            }

            try (Connection conn = rig.pg().createConnection("")) {
                setTenantGuc(conn, TENANT);
                assertThat(callGraphHop384(conn, collection, "relay-a", 2))
                    .as("relay-c is reachable ONLY via the tombstoned relay-b -- the traversal itself "
                        + "must gate on deleted_at, not just the final row (catalog-019 regression, T2 22455)")
                    .containsExactly("relay-a");
            }
        } finally {
            rig.close();
        }
    }

    private static java.util.List<String> callGraphHop384(Connection conn, String collection, String seed, int depth)
            throws Exception {
        try (PreparedStatement ps = conn.prepareStatement(
                "SELECT id FROM nexus.search_graph_hop_384("
                    + "?::vector, ?, ?, 'cites', ?, 'out', NULL, 10)")) {
            ps.setString(1, vectorLiteral(384, 0.09));
            ps.setArray(2, conn.createArrayOf("text", new String[] {seed}));
            ps.setArray(3, conn.createArrayOf("text", new String[] {collection}));
            ps.setInt(4, depth);
            var rs = ps.executeQuery();
            java.util.List<String> ids = new java.util.ArrayList<>();
            while (rs.next()) ids.add(rs.getString("id"));
            return ids;
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
