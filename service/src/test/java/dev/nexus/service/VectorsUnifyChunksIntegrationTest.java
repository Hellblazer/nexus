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
 * RDR-191 Phase 4 core (beads nexus-o8dil.12 + .13 + .14) — the ALWAYS-COPY
 * migration collapsing {@code nexus.chunks_384/768/1024} into ONE unified
 * {@code nexus.chunks} table ({@code vectors-004-unify-chunks.xml}, changeset
 * {@code vectors-004-1}).
 *
 * <p><strong>Isolation strategy — UPDATED at Step G (RDR-191 repoint batch,
 * bead nexus-o8dil.48): this changeset IS NOW registered in {@code
 * db.changelog-master.xml} (Step B, plan T2 [22445]) alongside the full Java
 * repoint (o8dil.16/.17/.18/.48).</strong> Tests that need the SOURCE
 * (pre-unify) shape — chunks_384/768/1024 present, {@code nexus.chunks} not
 * yet created — therefore run the real master changelog only UP TO (not
 * including) {@code vectors-004-1} via {@link #migrateUpTo}, seed their
 * fixture against the per-dim tables, then apply {@code
 * vectors-004-unify-chunks.xml} as its own standalone root changelog via a
 * second {@link Liquibase} instance pointed directly at that file's
 * classpath-relative path (identical filename to the {@code <include>} in
 * master, so Liquibase's {@code (id, author, filename)} DATABASECHANGELOG key
 * resolves it as the SAME changeset either way). Tests that only assert
 * against the FINAL unified state run {@link SchemaMigrator#migrate} to real
 * head (the changeset already applied as part of that run) and treat a
 * subsequent {@code applyUnifyChangeset} call as the idempotent no-op it now
 * is.
 *
 * <p>Coverage against the bead acceptance criteria:
 * <ul>
 *   <li>{@link #freshInstall_replaySafe_unifiesStructureCorrectly()} — full
 *       head migration + this changeset, proving the CRITICAL REPLAY
 *       QUESTION: catalog-013-2's {@code expectedResult="5"} precondition
 *       already executed cleanly during the (unmodified) head migration,
 *       long before this changeset ever runs.</li>
 *   <li>{@link #straddlingDistribution_rowsLandInCorrectTypedColumn()} —
 *       nexus-4rbud matrix case "straddling", the load-bearing multi-dim
 *       case.</li>
 *   <li>{@link #degenerateSingleRow_migratesCleanly()} — nexus-4rbud matrix
 *       case "degenerate 1-row", the actual measured production shape
 *       (chunks_768=0, chunks_384=1, chunks_1024=384k+).</li>
 *   <li>{@link #crossShardPkCollision_haltsMigrationLoudly()} — N7.</li>
 *   <li>{@link #exactlyOneEmbedding_rejectsZeroAndTwo_acceptsOnePerDim()} —
 *       bead .12's direct acceptance line.</li>
 *   <li>{@link #unRekeyedLegacyChash_bootSurvives_octetCheckStaysNotValid()} —
 *       F14b / bead .13's core boot-brick guard.</li>
 *   <li>{@link #allThreeHnswIndexes_areFull_noPartialPredicate()} — F13/C4,
 *       queried against the LIVE database per the plan-audit finding on bead
 *       .12 (not a grep of the changelog XML).</li>
 * </ul>
 *
 * <p><strong>NOT covered here:</strong> the grants-nexus-svc.xml boot-brick
 * this changeset's own DROP causes (grants-003-purge-vacuum-maintain is
 * {@code runAlways} and unconditionally GRANTs MAINTAIN on
 * chunks_384/768/1024 by name — the very next execution after this changeset
 * drops them would crash-loop). The fix (an in-body early-RETURN existence
 * guard on grants-003 — NOT a {@code <preConditions onFail="MARK_RAN">},
 * which on a runAlways changeset appends one DATABASECHANGELOG row per unmet
 * boot, nexus-ixsxa — plus a new grants-005
 * changeset) is fully designed and written but cannot be integration-tested
 * without EITHER registering this changeset for real (which breaks
 * compilation as above) OR maintaining a throwaway duplicate of a 250+ line
 * production file as a test resource. The design is delivered as text in the
 * developer's completion report and T2 write-back, ready to apply verbatim
 * in the SAME coordinated batch that adds the {@code <include>}.
 */
class VectorsUnifyChunksIntegrationTest {

    private static final String SVC_ROLE = "nexus_svc";
    private static final String SVC_PASS = "nexus_svc_pass";
    // Staged OUTSIDE db/changelog/ (round 3, coordinator directive): the
    // changelog-parity lints (test_rehearsal_seed_coverage_lint.py,
    // test_changelog_rls_lint.py, test_changelog_validate_precondition_lint.py)
    // assert the master include list matches CHANGELOG_DIR.glob("*.xml")
    // exactly -- a file present on disk but not <include>d is correctly
    // treated as drift and fails the whole Python suite. db/changelog/
    // is a SIBLING of db/changelog/, invisible to that non-recursive glob (and
    // to jOOQ codegen, which only ever applies what master.xml <include>s), so
    // this changeset stays fully classpath-loadable (everything under
    // src/main/resources/ is a classpath root) and fully tested here while
    // dormant, without tripping the drift lints. See db.changelog-master.xml's
    // own comment and T2 nexus/rdr-191-p4-unify-changeset-2026-08-13 [22433]
    // for the registration checklist that moves it back.
    private static final String UNIFY_CHANGELOG = "db/changelog/vectors-004-unify-chunks.xml";

    // ── Shared aged-box scaffold ─────────────────────────────────────────────

    /** One dedicated container + admin pool, bootstrapped like production. */
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
     * mirroring {@code SchemaMigratorIntegrationTest}'s aged-box idiom (tests
     * 5/6/8/10).
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

    /**
     * Applies {@code vectors-004-unify-chunks.xml} as its OWN standalone root
     * changelog — see class javadoc for why. Wraps {@link LiquibaseException}
     * as {@link MigrationException} to match {@link SchemaMigrator#migrate}'s
     * production wrapping shape exactly, so assertions here transfer directly
     * once this changeset is registered for real.
     */
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
     * Seeds one row via a SUPERUSER connection (bypasses the FORCE RLS trap),
     * first stub-registering the collection into catalog_collections — the
     * fk-002 collection FK (already VALIDATEd, part of the unmodified head
     * migration) rejects an unregistered collection on ANY new insert,
     * mirroring fk-002-0-backfill-stubs' own shape.
     */
    private static void seedChunk(PostgreSQLContainer<?> pg, int dim, String tenant, String collection,
                                   byte[] chash, String text) throws Exception {
        try (Connection su = pg.createConnection("")) {
            try (PreparedStatement stub = su.prepareStatement(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?) "
                        + "ON CONFLICT (tenant_id, name) DO NOTHING")) {
                stub.setString(1, tenant);
                stub.setString(2, collection);
                stub.executeUpdate();
            }
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.chunks_" + dim
                        + " (tenant_id, collection, chash, chunk_text, embedding) "
                        + "VALUES (?, ?, ?, ?, ?::vector)")) {
                ps.setString(1, tenant);
                ps.setString(2, collection);
                ps.setBytes(3, chash);
                ps.setString(4, text);
                ps.setString(5, "[" + "0.01,".repeat(dim - 1) + "0.01]");
                ps.executeUpdate();
            }
        }
    }

    private static byte[] chash32(int fillByte) {
        byte[] b = new byte[32];
        java.util.Arrays.fill(b, (byte) fillByte);
        return b;
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

    private static boolean constraintValidated(Connection conn, String conname) throws Exception {
        try (var ps = conn.prepareStatement(
                "SELECT convalidated FROM pg_constraint WHERE conname = ?")) {
            ps.setString(1, conname);
            var rs = ps.executeQuery();
            return rs.next() && rs.getBoolean("convalidated");
        }
    }

    private static String changesetExecType(Connection conn, String id, String author, String filename)
            throws Exception {
        try (var ps = conn.prepareStatement(
                "SELECT exectype FROM databasechangelog WHERE id = ? AND author = ? AND filename = ?")) {
            ps.setString(1, id);
            ps.setString(2, author);
            ps.setString(3, filename);
            var rs = ps.executeQuery();
            return rs.next() ? rs.getString("exectype") : null;
        }
    }

    // ── Test 1: fresh install, full replay from genesis ─────────────────────

    /**
     * CRITICAL REPLAY QUESTION (from the dispatch brief): on a fresh install,
     * catalog-013-2 (position ~186 in master order) runs during the head
     * migration BEFORE this changeset is ever applied — so it always sees all
     * five len-check constraints. Verified here directly rather than by
     * inspection alone.
     */
    @Test
    void freshInstall_replaySafe_unifiesStructureCorrectly() throws Exception {
        Rig rig = newRig("fresh");
        try {
            SchemaMigrator.migrate(rig.adminDs());

            try (Connection conn = rig.pg().createConnection("")) {
                assertThat(changesetExecType(conn, "catalog-013-2", "nexus-e0hd2",
                        "db/changelog/catalog-013-chash-checks-validate.xml"))
                    .as("catalog-013-2 must EXECUTE cleanly BEFORE this changeset ever runs -- "
                        + "proving replay ordering is unaffected by this changeset's tail position")
                    .isEqualTo("EXECUTED");
            }

            assertThatCode(() -> applyUnifyChangeset(rig.adminDs()))
                .as("the unify changeset must not throw against a freshly-migrated head")
                .doesNotThrowAnyException();

            try (Connection conn = rig.pg().createConnection("")) {
                assertThat(tableExists(conn, "nexus", "chunks"))
                    .as("nexus.chunks must exist post-migration")
                    .isTrue();
                for (String dim : new String[] {"chunks_384", "chunks_768", "chunks_1024"}) {
                    assertThat(tableExists(conn, "nexus", dim))
                        .as("%s must be gone post-migration", dim)
                        .isFalse();
                }

                assertThat(constraintExists(conn, "chunks_pk")).isTrue();
                assertThat(constraintExists(conn, "exactly_one_embedding")).isTrue();
                assertThat(constraintExists(conn, "chunks_chash_octet_check")).isTrue();
                assertThat(constraintValidated(conn, "chunks_chash_octet_check"))
                    .as("octet CHECK must stay NOT VALID at boot (client rung validates, F14b)")
                    .isFalse();

                // RLS.
                try (var rs = conn.createStatement().executeQuery(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                            + "WHERE relnamespace = 'nexus'::regnamespace AND relname = 'chunks'")) {
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getBoolean("relrowsecurity")).isTrue();
                    assertThat(rs.getBoolean("relforcerowsecurity")).isTrue();
                }
                try (var rs = conn.createStatement().executeQuery(
                        "SELECT 1 FROM pg_policies WHERE schemaname = 'nexus' AND tablename = 'chunks' "
                            + "AND policyname = 'tenant_isolation'")) {
                    assertThat(rs.next()).as("tenant_isolation policy must exist on nexus.chunks").isTrue();
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

    // ── Test 2: straddling distribution (nexus-4rbud matrix) ────────────────

    @Test
    void straddlingDistribution_rowsLandInCorrectTypedColumn() throws Exception {
        Rig rig = newRig("straddle");
        try {
            // Stop BEFORE vectors-004-1 (now real head, Step B registration) so
            // chunks_384/768/1024 still exist to seed pre-unify -- see class
            // javadoc's Step G note and migrateUpTo's own javadoc.
            migrateUpTo(rig.adminDs(), "vectors-004-1");

            seedChunk(rig.pg(), 384, "t1", "code__demo__minilm__v1", chash32(1), "alpha");
            seedChunk(rig.pg(), 768, "t1", "docs__demo__bge__v1", chash32(2), "beta");
            seedChunk(rig.pg(), 1024, "t1", "knowledge__demo__voyage__v1", chash32(3), "gamma");
            seedChunk(rig.pg(), 1024, "t1", "knowledge__demo__voyage__v1", chash32(4), "delta");

            assertThatCode(() -> applyUnifyChangeset(rig.adminDs()))
                .as("straddling-distribution migration must not throw")
                .doesNotThrowAnyException();

            try (Connection conn = rig.pg().createConnection("")) {
                assertThat(rowCount(conn, "nexus.chunks")).isEqualTo(4L);

                try (var ps = conn.prepareStatement(
                        "SELECT embedding_384 IS NOT NULL, embedding_768 IS NOT NULL, "
                            + "embedding_1024 IS NOT NULL FROM nexus.chunks WHERE chash = ?")) {
                    ps.setBytes(1, chash32(1));
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getBoolean(1)).as("chash32(1) came from chunks_384").isTrue();
                    assertThat(rs.getBoolean(2)).isFalse();
                    assertThat(rs.getBoolean(3)).isFalse();
                }
                try (var ps = conn.prepareStatement(
                        "SELECT embedding_384 IS NOT NULL, embedding_768 IS NOT NULL, "
                            + "embedding_1024 IS NOT NULL FROM nexus.chunks WHERE chash = ?")) {
                    ps.setBytes(1, chash32(3));
                    var rs = ps.executeQuery();
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getBoolean(1)).isFalse();
                    assertThat(rs.getBoolean(2)).isFalse();
                    assertThat(rs.getBoolean(3)).as("chash32(3) came from chunks_1024").isTrue();
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 3: degenerate 1-row distribution (nexus-4rbud matrix) ──────────

    /** Mirrors the ACTUAL measured production distribution: chunks_768=0, chunks_384=1. */
    @Test
    void degenerateSingleRow_migratesCleanly() throws Exception {
        Rig rig = newRig("degenerate");
        try {
            // Stop BEFORE vectors-004-1 -- chunks_384/768/1024 must still exist
            // to seed pre-unify (Step G).
            migrateUpTo(rig.adminDs(), "vectors-004-1");
            seedChunk(rig.pg(), 384, "t1", "code__demo__minilm__v1", chash32(7), "solo");
            // chunks_768 and chunks_1024 stay EMPTY -- the real cloud/local shape.

            assertThatCode(() -> applyUnifyChangeset(rig.adminDs()))
                .as("degenerate single-row migration must not throw")
                .doesNotThrowAnyException();

            try (Connection conn = rig.pg().createConnection("")) {
                assertThat(rowCount(conn, "nexus.chunks")).isEqualTo(1L);
                // The 768/1024 HNSW indexes must still be built UNCONDITIONALLY (C4)
                // even though those shards contributed zero rows.
                for (String idx : new String[] {
                        "idx_chunks_embedding_384", "idx_chunks_embedding_768", "idx_chunks_embedding_1024"}) {
                    try (var ps = conn.prepareStatement(
                            "SELECT 1 FROM pg_indexes WHERE schemaname = 'nexus' AND indexname = ?")) {
                        ps.setString(1, idx);
                        assertThat(ps.executeQuery().next())
                            .as("%s must exist unconditionally even at zero population (F13/C4)", idx)
                            .isTrue();
                    }
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 4: cross-shard PK collision guard (N7) ──────────────────────────

    @Test
    void crossShardPkCollision_haltsMigrationLoudly() throws Exception {
        Rig rig = newRig("collision");
        try {
            // Stop BEFORE vectors-004-1 -- chunks_384/768/1024 must still exist
            // to seed pre-unify (Step G).
            migrateUpTo(rig.adminDs(), "vectors-004-1");
            byte[] sharedChash = chash32(9);
            seedChunk(rig.pg(), 384, "t1", "same__collection__v1", sharedChash, "one");
            seedChunk(rig.pg(), 768, "t1", "same__collection__v1", sharedChash, "two");

            // MigrationException's own message is a fixed "Liquibase migration failed"
            // (SchemaMigrator wraps every LiquibaseException identically); the RAISE
            // EXCEPTION text from the DO block below lives in the cause chain, so this
            // asserts against the full printed stack trace rather than getMessage().
            assertThatThrownBy(() -> applyUnifyChangeset(rig.adminDs()))
                .as("a cross-shard (tenant_id, collection, chash) collision must HALT the "
                    + "migration loudly (N7) rather than corrupt the copy or silently drop a row")
                .isInstanceOf(MigrationException.class)
                .hasStackTraceContaining("cross-shard");

            try (Connection conn = rig.pg().createConnection("")) {
                assertThat(tableExists(conn, "nexus", "chunks"))
                    .as("a HALTed changeset must leave nexus.chunks absent -- the whole "
                        + "changeset is one transaction (N3), so a mid-way RAISE rolls back "
                        + "the CREATE TABLE too")
                    .isFalse();
                assertThat(tableExists(conn, "nexus", "chunks_384"))
                    .as("the sources must survive a HALTed migration")
                    .isTrue();
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 5: exactly_one_embedding CHECK enforcement (bead .12) ──────────

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
                            "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text) "
                                + "VALUES ('t1', 'c', ?, 'x')")) {
                        ps.setBytes(1, chash32(20));
                        ps.executeUpdate();
                    }
                }).as("zero-embedding row must violate exactly_one_embedding")
                  .hasMessageContaining("exactly_one_embedding");

                // Two embeddings -> rejected.
                assertThatThrownBy(() -> {
                    try (var ps = su.prepareStatement(
                            "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, "
                                + "embedding_384, embedding_768) VALUES ('t1', 'c', ?, 'x', ?::vector, ?::vector)")) {
                        ps.setBytes(1, chash32(21));
                        String v384 = "[" + "0.01,".repeat(383) + "0.01]";
                        String v768 = "[" + "0.01,".repeat(767) + "0.01]";
                        ps.setString(2, v384);
                        ps.setString(3, v768);
                        ps.executeUpdate();
                    }
                }).as("two-embedding row must violate exactly_one_embedding")
                  .hasMessageContaining("exactly_one_embedding");

                // Exactly one, per dim -> accepted.
                int[] dims = {384, 768, 1024};
                for (int i = 0; i < dims.length; i++) {
                    int dim = dims[i];
                    String vec = "[" + "0.01,".repeat(dim - 1) + "0.01]";
                    try (var ps = su.prepareStatement(
                            "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, "
                                + "embedding_" + dim + ") VALUES ('t1', 'c', ?, 'x', ?::vector)")) {
                        ps.setBytes(1, chash32(30 + i));
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

    // ── Test 6: un-rekeyed legacy chash — F14b boot-brick guard ─────────────

    /**
     * A pre-rekey store carries 16-byte legacy chash values. A width CHECK
     * present VALID (or added before the copy) would reject them mid-migration
     * — the exact GH #1390 crash-loop shape. This test's premise was falsified
     * manually (not left in the suite) by temporarily reordering the changeset
     * to add the octet CHECK VALID and before the copy and confirming this
     * test goes red — see the developer's completion report for that
     * kill-control demonstration.
     *
     * <p>MUST seed BEFORE {@code rdr180-11-octet-checks-not-valid} runs:
     * {@code NOT VALID} exempts only rows that PRE-EXIST the {@code ADD
     * CONSTRAINT} — a row inserted AFTER that changeset is fully checked like
     * any other write (rdr180-001-bytea-chash.xml's own header, "MEASURED and
     * still binding"). Seeding after a full head migration (as the other
     * tests in this class do) would therefore hit chunks_1024's OWN octet
     * CHECK immediately, never reaching this changeset at all.
     */
    @Test
    void unRekeyedLegacyChash_bootSurvives_octetCheckStaysNotValid() throws Exception {
        Rig rig = newRig("legacy16");
        try {
            migrateUpTo(rig.adminDs(), "rdr180-11-octet-checks-not-valid");
            // 16-byte legacy chash, not the post-rekey 32-byte form -- PRE-EXISTS
            // the octet CHECK that is about to be added, so NOT VALID exempts it.
            byte[] legacy16 = new byte[16];
            java.util.Arrays.fill(legacy16, (byte) 0x5a);
            try (Connection su = rig.pg().createConnection("")) {
                try (PreparedStatement stub = su.prepareStatement(
                        "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?) "
                            + "ON CONFLICT (tenant_id, name) DO NOTHING")) {
                    stub.setString(1, "t1");
                    stub.setString(2, "legacy__demo__voyage__v1");
                    stub.executeUpdate();
                }
                try (PreparedStatement ps = su.prepareStatement(
                        "INSERT INTO nexus.chunks_1024 (tenant_id, collection, chash, chunk_text, embedding) "
                            + "VALUES (?, ?, ?, ?, ?::vector)")) {
                    ps.setString(1, "t1");
                    ps.setString(2, "legacy__demo__voyage__v1");
                    ps.setBytes(3, legacy16);
                    ps.setString(4, "un-rekeyed row");
                    ps.setString(5, "[" + "0.01,".repeat(1023) + "0.01]");
                    ps.executeUpdate();
                }
            }

            // Resume the rest of the head migration (rdr180-11 onward — the
            // octet CHECK is added NOT VALID and correctly exempts this
            // pre-existing row) before applying the unify changeset.
            assertThatCode(() -> SchemaMigrator.migrate(rig.adminDs()))
                .as("resuming the head migration past rdr180-11 must not throw -- the "
                    + "pre-existing 16-byte row is exempted by NOT VALID")
                .doesNotThrowAnyException();

            assertThatCode(() -> applyUnifyChangeset(rig.adminDs()))
                .as("boot against an un-rekeyed store (16-byte legacy chash) must complete — "
                    + "a VALID or pre-copy octet CHECK would crash-loop here (F14b, GH #1390 shape)")
                .doesNotThrowAnyException();

            try (Connection conn = rig.pg().createConnection("")) {
                assertThat(rowCount(conn, "nexus.chunks")).isEqualTo(1L);
                assertThat(constraintValidated(conn, "chunks_chash_octet_check"))
                    .as("octet CHECK must remain NOT VALID -- it is never validated at boot time")
                    .isFalse();
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 7: all three HNSW indexes are FULL, no partial predicate (F13/C4) ──

    /**
     * Plan-audit finding on bead .12 (2026-08-10): assert via {@code
     * pg_get_indexdef} against the LIVE database, not a grep of the changelog
     * XML — an XML grep would pass even if a partial index were added
     * elsewhere.
     */
    @Test
    void allThreeHnswIndexes_areFull_noPartialPredicate() throws Exception {
        Rig rig = newRig("hnswfull");
        try {
            SchemaMigrator.migrate(rig.adminDs());
            applyUnifyChangeset(rig.adminDs());
            try (Connection conn = rig.pg().createConnection("")) {
                for (String idx : new String[] {
                        "idx_chunks_embedding_384", "idx_chunks_embedding_768", "idx_chunks_embedding_1024"}) {
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
                            .as("%s must carry NO WHERE ... IS NOT NULL predicate (F13: partial "
                                + "buys 0.11%% size and costs a silent ~250x seq-scan)", idx)
                            .doesNotContain("WHERE");
                    }
                }
            }
        } finally {
            rig.close();
        }
    }

    // ── Test 8: rollback round trip for THIS changeset alone ────────────────

    /**
     * Narrower than {@code SchemaRollbackRoundTripIntegrationTest}'s full-chain
     * proof (which cannot cover this changeset until it is registered): applies
     * this changeset, rolls back exactly one changeset, and asserts the sources
     * are faithfully restored with data, indexes, RLS, octet CHECK, and the
     * fk-002 collection FK all present again.
     */
    @Test
    void rollback_restoresSourcesFaithfully() throws Exception {
        Rig rig = newRig("rollback");
        try {
            // Stop BEFORE vectors-004-1 -- chunks_384/768/1024 must still exist
            // to seed pre-unify (Step G).
            migrateUpTo(rig.adminDs(), "vectors-004-1");
            seedChunk(rig.pg(), 384, "t1", "code__demo__minilm__v1", chash32(50), "r1");
            seedChunk(rig.pg(), 768, "t1", "docs__demo__bge__v1", chash32(52), "r3");
            seedChunk(rig.pg(), 1024, "t1", "knowledge__demo__voyage__v1", chash32(51), "r2");
            applyUnifyChangeset(rig.adminDs());

            // MUST run as the migration role (nexus_admin_*), not superuser: the
            // recreated chunks_384/768/1024 must be owned by the SAME role that
            // will LOCK TABLE them on any subsequent apply, exactly matching a
            // real Liquibase rollback (which always runs as the migration role).
            try (Connection conn = rig.adminDs().getConnection()) {
                Database database = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(conn));
                try (Liquibase liquibase = new Liquibase(
                        UNIFY_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                    liquibase.rollback(1, new Contexts(), new LabelExpression());
                }
            }

            try (Connection conn = rig.pg().createConnection("")) {
                assertThat(tableExists(conn, "nexus", "chunks")).isFalse();
                assertThat(tableExists(conn, "nexus", "chunks_384")).isTrue();
                assertThat(tableExists(conn, "nexus", "chunks_768")).isTrue();
                assertThat(tableExists(conn, "nexus", "chunks_1024")).isTrue();
                assertThat(rowCount(conn, "nexus.chunks_384")).isEqualTo(1L);
                assertThat(rowCount(conn, "nexus.chunks_768")).isEqualTo(1L);
                assertThat(rowCount(conn, "nexus.chunks_1024")).isEqualTo(1L);

                // All three shards, not just 384/1024 (critic S4: the prior
                // version of this test never seeded or asserted chunks_768 at
                // all, so one of three shards' rollback fidelity was entirely
                // unverified despite the test's own "faithfully" claim).
                for (String dim : new String[] {"384", "768", "1024"}) {
                    assertThat(constraintExists(conn, "chunks_" + dim + "_chash_octet_check")).isTrue();
                    assertThat(constraintValidated(conn, "chunks_" + dim + "_chash_octet_check")).isFalse();
                    assertThat(constraintValidated(conn, "chunks_" + dim + "_collection_fk"))
                        .as("the fk-002 collection FK must be restored AND validated")
                        .isTrue();

                    try (var ps = conn.prepareStatement(
                            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                                + "WHERE relnamespace = 'nexus'::regnamespace AND relname = ?")) {
                        ps.setString(1, "chunks_" + dim);
                        var rs = ps.executeQuery();
                        assertThat(rs.next()).isTrue();
                        assertThat(rs.getBoolean("relrowsecurity")).isTrue();
                        assertThat(rs.getBoolean("relforcerowsecurity")).isTrue();
                    }
                }

                // Re-apply must succeed cleanly (the round-trip's other half).
                assertThatCode(() -> applyUnifyChangeset(rig.adminDs()))
                    .as("re-applying after rollback must succeed cleanly")
                    .doesNotThrowAnyException();
                assertThat(rowCount(conn, "nexus.chunks")).isEqualTo(3L);
            }
        } finally {
            rig.close();
        }
    }
}
