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

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-191 Phase 4, centroid family (bead nexus-jv3ue, Hal ruling o8dil.47
 * "unify the centroids too, one era") — the SIBLING of {@link
 * VectorsUnifyChunksIntegrationTest}. Collapses {@code
 * nexus.taxonomy_centroids_384/768/1024} into ONE unified {@code
 * nexus.taxonomy_centroids} table ({@code taxonomy-007-unify-centroids.xml},
 * changeset {@code taxonomy-007-1}).
 *
 * <p><strong>Isolation strategy — UPDATED at Step G (RDR-191 repoint batch,
 * bead nexus-o8dil.48)</strong> — identical shape to {@link
 * VectorsUnifyChunksIntegrationTest}: this changeset IS NOW registered in
 * {@code db.changelog-master.xml} (Step B, plan T2 [22445]). Tests that need
 * the SOURCE (pre-unify) shape run the real master changelog only UP TO (not
 * including) {@code taxonomy-007-1} via {@link #migrateUpTo}, seed their
 * fixture against the per-dim tables, then apply {@code
 * taxonomy-007-unify-centroids.xml} as its own standalone root changelog via
 * a second {@link Liquibase} instance pointed directly at that file's
 * classpath-relative path — byte-identical end state to a real {@code
 * <include>}, since Liquibase tracks {@code DATABASECHANGELOG} rows by
 * {@code (id, author, filename)}, not by which changelog reached them. Tests
 * that only assert against the FINAL unified state run {@link
 * SchemaMigrator#migrate} to real head and treat a subsequent {@code
 * applyUnifyChangeset} call as the idempotent no-op it now is.
 *
 * <p>UNLIKE the chunks test, this class has NO analog of {@code
 * unRekeyedLegacyChash_bootSurvives_octetCheckStaysNotValid} — centroid
 * tables carry no chash column and no octet-CHECK family at all (verified
 * by direct read of taxonomy-002-centroids.xml; see
 * taxonomy-007-unify-centroids.xml's own header, divergence 1). Seeding a
 * centroid row also needs NO fk-002-style stub registration into {@code
 * catalog_collections} — taxonomy-002's own header states explicitly there
 * is no FK to {@code nexus.topics(id)} either, and a repo-wide grep confirms
 * no {@code fk-NNN-*.xml} changelog references a centroid table.
 */
class VectorsUnifyCentroidsIntegrationTest {

    private static final String SVC_ROLE = "nexus_svc";
    private static final String SVC_PASS = "nexus_svc_pass";
    // Staged OUTSIDE db/changelog/ — see taxonomy-007-unify-centroids.xml's
    // own header and db.changelog-master.xml's comment for the full
    // changelog-parity drift-lint rationale (identical to the chunks side).
    private static final String UNIFY_CHANGELOG = "db/changelog/taxonomy-007-unify-centroids.xml";

    // ── Shared aged-box scaffold (mirrors VectorsUnifyChunksIntegrationTest) ──

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
            // grants-004-monitor-wal-visibility needs the migration role to hold
            // pg_monitor WITH ADMIN OPTION before it can grant it onward.
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

    /**
     * Runs the master changelog up to (but NOT including) {@code changesetId},
     * mirroring {@code SchemaMigratorIntegrationTest}'s aged-box idiom and
     * {@link VectorsUnifyChunksIntegrationTest#migrateUpTo}'s identical
     * sibling helper (Step G, RDR-191 repoint batch cluster A).
     */
    private static void migrateUpTo(com.zaxxer.hikari.HikariDataSource ds, String changesetId) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    "db/changelog/db.changelog-master.xml",
                    new ClassLoaderResourceAccessor(),
                    database)) {
                var unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (changesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx)
                    .as("%s must be present in the master changelog", changesetId)
                    .isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }

    private static void applyUnifyChangeset(com.zaxxer.hikari.HikariDataSource ds) {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    UNIFY_CHANGELOG,
                    new ClassLoaderResourceAccessor(),
                    database)) {
                liquibase.update(new Contexts(), new LabelExpression());
            }
        } catch (SQLException e) {
            throw new MigrationException("Failed to obtain DB connection for migration", e);
        } catch (LiquibaseException e) {
            throw new MigrationException("Liquibase migration failed", e);
        }
    }

    /**
     * Seeds one centroid row via a SUPERUSER connection (bypasses the FORCE
     * RLS trap). UNLIKE {@code seedChunk} in the chunks test, no stub
     * registration is needed first: taxonomy_centroids_&lt;dim&gt; carries
     * no FK to catalog_collections or to nexus.topics (verified by direct
     * read of taxonomy-002-centroids.xml's own header).
     */
    private static void seedCentroid(PostgreSQLContainer<?> pg, int dim, String tenant, String collection,
                                      long topicId, String label) throws Exception {
        try (Connection su = pg.createConnection("")) {
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.taxonomy_centroids_" + dim
                        + " (tenant_id, collection, topic_id, embedding, label, doc_count) "
                        + "VALUES (?, ?, ?, ?::vector, ?, ?)")) {
                ps.setString(1, tenant);
                ps.setString(2, collection);
                ps.setLong(3, topicId);
                ps.setString(4, "[" + "0.01,".repeat(dim - 1) + "0.01]");
                ps.setString(5, label);
                ps.setInt(6, 3);
                ps.executeUpdate();
            }
        }
    }

    private static long rowCount(Connection conn, String qualifiedTable) throws Exception {
        try (var rs = conn.createStatement().executeQuery("SELECT COUNT(*) FROM " + qualifiedTable)) {
            rs.next();
            return rs.getLong(1);
        }
    }

    private static boolean tableExists(Connection conn, String schema, String table) throws Exception {
        try (var ps = conn.prepareStatement(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?")) {
            ps.setString(1, schema);
            ps.setString(2, table);
            var rs = ps.executeQuery();
            return rs.next();
        }
    }

    private static boolean constraintExists(Connection conn, String conname) throws Exception {
        try (var ps = conn.prepareStatement("SELECT 1 FROM pg_constraint WHERE conname = ?")) {
            ps.setString(1, conname);
            return ps.executeQuery().next();
        }
    }

    // ── Test 1: fresh install, full replay from genesis ─────────────────────

    @Test
    void freshInstall_replaySafe_unifiesStructureCorrectly() throws Exception {
        Rig rig = newRig("fresh");
        try {
            SchemaMigrator.migrate(rig.adminDs());

            assertThatCode(() -> applyUnifyChangeset(rig.adminDs()))
                .as("the unify changeset must not throw against a freshly-migrated head")
                .doesNotThrowAnyException();

            try (Connection conn = rig.pg().createConnection("")) {
                assertThat(tableExists(conn, "nexus", "taxonomy_centroids"))
                    .as("nexus.taxonomy_centroids must exist post-migration")
                    .isTrue();
                for (String dim : new String[] {
                        "taxonomy_centroids_384", "taxonomy_centroids_768", "taxonomy_centroids_1024"}) {
                    assertThat(tableExists(conn, "nexus", dim))
                        .as("%s must be gone post-migration", dim)
                        .isFalse();
                }

                assertThat(constraintExists(conn, "taxonomy_centroids_pk")).isTrue();
                assertThat(constraintExists(conn, "taxonomy_centroids_exactly_one_embedding")).isTrue();

                // RLS.
                try (var rs = conn.createStatement().executeQuery(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                            + "WHERE relnamespace = 'nexus'::regnamespace AND relname = 'taxonomy_centroids'")) {
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getBoolean("relrowsecurity")).isTrue();
                    assertThat(rs.getBoolean("relforcerowsecurity")).isTrue();
                }
                try (var rs = conn.createStatement().executeQuery(
                        "SELECT 1 FROM pg_policies WHERE schemaname = 'nexus' AND tablename = 'taxonomy_centroids' "
                            + "AND policyname = 'tenant_isolation'")) {
                    assertThat(rs.next()).as("tenant_isolation policy must exist on nexus.taxonomy_centroids").isTrue();
                }

                // Idempotency: a second apply of the SAME changeset must be a no-op.
                assertThatCode(() -> applyUnifyChangeset(rig.adminDs()))
                    .as("a second apply must be a clean no-op (Liquibase's own idempotency)")
                    .doesNotThrowAnyException();
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 2: straddling distribution ──────────────────────────────────────

    @Test
    void straddlingDistribution_rowsLandInCorrectTypedColumn() throws Exception {
        Rig rig = newRig("straddle");
        try {
            // Stop BEFORE taxonomy-007-1 (now real head, Step B registration)
            // so taxonomy_centroids_384/768/1024 still exist to seed pre-unify.
            migrateUpTo(rig.adminDs(), "taxonomy-007-1");

            seedCentroid(rig.pg(), 384, "t1", "code__demo__minilm__v1", 1L, "alpha");
            seedCentroid(rig.pg(), 768, "t1", "docs__demo__bge__v1", 2L, "beta");
            seedCentroid(rig.pg(), 1024, "t1", "knowledge__demo__voyage__v1", 3L, "gamma");
            seedCentroid(rig.pg(), 1024, "t1", "knowledge__demo__voyage__v1", 4L, "delta");

            assertThatCode(() -> applyUnifyChangeset(rig.adminDs()))
                .as("straddling-distribution migration must not throw")
                .doesNotThrowAnyException();

            try (Connection conn = rig.pg().createConnection("")) {
                assertThat(rowCount(conn, "nexus.taxonomy_centroids")).isEqualTo(4L);

                try (var ps = conn.prepareStatement(
                        "SELECT embedding_384 IS NOT NULL, embedding_768 IS NOT NULL, "
                            + "embedding_1024 IS NOT NULL FROM nexus.taxonomy_centroids "
                            + "WHERE collection = ? AND topic_id = ?")) {
                    ps.setString(1, "code__demo__minilm__v1");
                    ps.setLong(2, 1L);
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getBoolean(1)).as("topic 1 came from taxonomy_centroids_384").isTrue();
                    assertThat(rs.getBoolean(2)).isFalse();
                    assertThat(rs.getBoolean(3)).isFalse();
                }
                try (var ps = conn.prepareStatement(
                        "SELECT embedding_384 IS NOT NULL, embedding_768 IS NOT NULL, "
                            + "embedding_1024 IS NOT NULL FROM nexus.taxonomy_centroids "
                            + "WHERE collection = ? AND topic_id = ?")) {
                    ps.setString(1, "knowledge__demo__voyage__v1");
                    ps.setLong(2, 3L);
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getBoolean(1)).isFalse();
                    assertThat(rs.getBoolean(2)).isFalse();
                    assertThat(rs.getBoolean(3)).as("topic 3 came from taxonomy_centroids_1024").isTrue();
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 3: degenerate single-row distribution ───────────────────────────

    @Test
    void degenerateSingleRow_migratesCleanly() throws Exception {
        Rig rig = newRig("degenerate");
        try {
            // Stop BEFORE taxonomy-007-1 -- taxonomy_centroids_384/768/1024
            // must still exist to seed pre-unify (Step G).
            migrateUpTo(rig.adminDs(), "taxonomy-007-1");
            seedCentroid(rig.pg(), 384, "t1", "code__demo__minilm__v1", 7L, "solo");
            // taxonomy_centroids_768 and _1024 stay EMPTY.

            assertThatCode(() -> applyUnifyChangeset(rig.adminDs()))
                .as("degenerate single-row migration must not throw")
                .doesNotThrowAnyException();

            try (Connection conn = rig.pg().createConnection("")) {
                assertThat(rowCount(conn, "nexus.taxonomy_centroids")).isEqualTo(1L);
                // The 768/1024 HNSW indexes must still be built UNCONDITIONALLY
                // even though those shards contributed zero rows.
                for (String idx : new String[] {
                        "idx_taxonomy_centroids_embedding_384",
                        "idx_taxonomy_centroids_embedding_768",
                        "idx_taxonomy_centroids_embedding_1024"}) {
                    try (var ps = conn.prepareStatement(
                            "SELECT 1 FROM pg_indexes WHERE schemaname = 'nexus' AND indexname = ?")) {
                        ps.setString(1, idx);
                        assertThat(ps.executeQuery().next())
                            .as("%s must exist unconditionally even at zero population", idx)
                            .isTrue();
                    }
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 4: cross-shard PK collision guard ───────────────────────────────

    @Test
    void crossShardPkCollision_haltsMigrationLoudly() throws Exception {
        Rig rig = newRig("collision");
        try {
            // Stop BEFORE taxonomy-007-1 -- taxonomy_centroids_384/768/1024
            // must still exist to seed pre-unify (Step G).
            migrateUpTo(rig.adminDs(), "taxonomy-007-1");
            seedCentroid(rig.pg(), 384, "t1", "same__collection__v1", 9L, "one");
            seedCentroid(rig.pg(), 768, "t1", "same__collection__v1", 9L, "two");

            assertThatThrownBy(() -> applyUnifyChangeset(rig.adminDs()))
                .as("a cross-shard (tenant_id, collection, topic_id) collision must HALT the "
                    + "migration loudly rather than corrupt the copy or silently drop a row")
                .isInstanceOf(MigrationException.class)
                .hasStackTraceContaining("cross-shard");

            try (Connection conn = rig.pg().createConnection("")) {
                assertThat(tableExists(conn, "nexus", "taxonomy_centroids"))
                    .as("a HALTed changeset must leave nexus.taxonomy_centroids absent -- the "
                        + "whole changeset is one transaction, so a mid-way RAISE rolls back "
                        + "the CREATE TABLE too")
                    .isFalse();
                assertThat(tableExists(conn, "nexus", "taxonomy_centroids_384"))
                    .as("the sources must survive a HALTed migration")
                    .isTrue();
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 5: exactly_one_embedding CHECK enforcement ──────────────────────

    @Test
    void exactlyOneEmbedding_rejectsZeroAndTwo_acceptsOnePerDim() throws Exception {
        Rig rig = newRig("checkenforce");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyUnifyChangeset(rig.adminDs());

            try (Connection su = rig.pg().createConnection("")) {
                su.setAutoCommit(true);

                // Zero embeddings -> rejected.
                assertThatThrownBy(() -> {
                    try (var ps = su.prepareStatement(
                            "INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id) "
                                + "VALUES ('t1', 'c', 100)")) {
                        ps.executeUpdate();
                    }
                }).as("zero-embedding row must violate taxonomy_centroids_exactly_one_embedding")
                  .hasMessageContaining("exactly_one_embedding");

                // Two embeddings -> rejected.
                assertThatThrownBy(() -> {
                    try (var ps = su.prepareStatement(
                            "INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id, "
                                + "embedding_384, embedding_768) VALUES ('t1', 'c', 101, ?::vector, ?::vector)")) {
                        String v384 = "[" + "0.01,".repeat(383) + "0.01]";
                        String v768 = "[" + "0.01,".repeat(767) + "0.01]";
                        ps.setString(1, v384);
                        ps.setString(2, v768);
                        ps.executeUpdate();
                    }
                }).as("two-embedding row must violate taxonomy_centroids_exactly_one_embedding")
                  .hasMessageContaining("exactly_one_embedding");

                // Exactly one, per dim -> accepted.
                int[] dims = {384, 768, 1024};
                for (int i = 0; i < dims.length; i++) {
                    int dim = dims[i];
                    String vec = "[" + "0.01,".repeat(dim - 1) + "0.01]";
                    try (var ps = su.prepareStatement(
                            "INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id, "
                                + "embedding_" + dim + ") VALUES ('t1', 'c', ?, ?::vector)")) {
                        ps.setLong(1, 200 + i);
                        ps.setString(2, vec);
                        assertThatCode(ps::executeUpdate)
                            .as("single embedding_%d row must be accepted", dim)
                            .doesNotThrowAnyException();
                    }
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 6: all three HNSW indexes are FULL, no partial predicate ────────

    @Test
    void allThreeHnswIndexes_areFull_noPartialPredicate() throws Exception {
        Rig rig = newRig("hnswfull");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyUnifyChangeset(rig.adminDs());
            try (Connection conn = rig.pg().createConnection("")) {
                for (String idx : new String[] {
                        "idx_taxonomy_centroids_embedding_384",
                        "idx_taxonomy_centroids_embedding_768",
                        "idx_taxonomy_centroids_embedding_1024"}) {
                    try (var ps = conn.prepareStatement(
                            "SELECT pg_get_indexdef(indexrelid), amname FROM pg_index i "
                                + "JOIN pg_class c ON c.oid = i.indexrelid "
                                + "JOIN pg_am am ON am.oid = c.relam "
                                + "WHERE c.relname = ?")) {
                        ps.setString(1, idx);
                        var rs = ps.executeQuery();
                        assertThat(rs.next()).as("%s must exist", idx).isTrue();
                        String def = rs.getString(1);
                        assertThat(rs.getString("amname")).isEqualTo("hnsw");
                        assertThat(def.toUpperCase())
                            .as("%s must carry NO WHERE ... IS NOT NULL predicate", idx)
                            .doesNotContain("WHERE");
                    }
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 7: rollback round trip, ALL THREE shards ────────────────────────

    /**
     * Covers all three shards from the start (learning from the chunks
     * test's own S4 finding — its first version only seeded/asserted two of
     * three shards' rollback fidelity, silently leaving the third
     * unverified despite the test's own "faithfully" claim; fixed in round
     * 2 there, done correctly here from the outset).
     */
    @Test
    void rollback_restoresSourcesFaithfully() throws Exception {
        Rig rig = newRig("rollback");
        try {
            // Stop BEFORE taxonomy-007-1 -- taxonomy_centroids_384/768/1024
            // must still exist to seed pre-unify (Step G).
            migrateUpTo(rig.adminDs(), "taxonomy-007-1");
            seedCentroid(rig.pg(), 384, "t1", "code__demo__minilm__v1", 50L, "r1");
            seedCentroid(rig.pg(), 768, "t1", "docs__demo__bge__v1", 52L, "r3");
            seedCentroid(rig.pg(), 1024, "t1", "knowledge__demo__voyage__v1", 51L, "r2");
            applyUnifyChangeset(rig.adminDs());

            // MUST run as the migration role (nexus_admin_*), not superuser: the
            // recreated taxonomy_centroids_384/768/1024 must be owned by the SAME
            // role that will LOCK TABLE them on any subsequent apply, exactly
            // matching a real Liquibase rollback.
            try (Connection conn = rig.adminDs().getConnection()) {
                Database database = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(conn));
                try (Liquibase liquibase = new Liquibase(
                        UNIFY_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                    liquibase.rollback(1, new Contexts(), new LabelExpression());
                }
            }

            try (Connection conn = rig.pg().createConnection("")) {
                assertThat(tableExists(conn, "nexus", "taxonomy_centroids")).isFalse();
                assertThat(tableExists(conn, "nexus", "taxonomy_centroids_384")).isTrue();
                assertThat(tableExists(conn, "nexus", "taxonomy_centroids_768")).isTrue();
                assertThat(tableExists(conn, "nexus", "taxonomy_centroids_1024")).isTrue();
                assertThat(rowCount(conn, "nexus.taxonomy_centroids_384")).isEqualTo(1L);
                assertThat(rowCount(conn, "nexus.taxonomy_centroids_768")).isEqualTo(1L);
                assertThat(rowCount(conn, "nexus.taxonomy_centroids_1024")).isEqualTo(1L);

                // All three shards: table existence, RLS, HNSW index. No
                // octet CHECK / FK analog to verify here (neither exists on
                // this family — see file header divergences 1/2).
                for (String dim : new String[] {"384", "768", "1024"}) {
                    try (var ps = conn.prepareStatement(
                            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                                + "WHERE relnamespace = 'nexus'::regnamespace AND relname = ?")) {
                        ps.setString(1, "taxonomy_centroids_" + dim);
                        var rs = ps.executeQuery();
                        assertThat(rs.next()).isTrue();
                        assertThat(rs.getBoolean("relrowsecurity")).isTrue();
                        assertThat(rs.getBoolean("relforcerowsecurity")).isTrue();
                    }
                    try (var ps = conn.prepareStatement(
                            "SELECT 1 FROM pg_indexes WHERE schemaname = 'nexus' AND indexname = ?")) {
                        ps.setString(1, "idx_taxonomy_centroids_" + dim + "_embedding");
                        assertThat(ps.executeQuery().next())
                            .as("taxonomy_centroids_%s's HNSW index must be restored", dim)
                            .isTrue();
                    }
                }

                // Re-apply must succeed cleanly (the round-trip's other half).
                assertThatCode(() -> applyUnifyChangeset(rig.adminDs()))
                    .as("re-applying after rollback must succeed cleanly")
                    .doesNotThrowAnyException();
                assertThat(rowCount(conn, "nexus.taxonomy_centroids")).isEqualTo(3L);
            }
        } finally {
            rig.close();
        }
    }
}
