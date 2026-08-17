// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;

import javax.xml.parsers.DocumentBuilderFactory;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-194 critical fix round 2 (2026-08-17, bead nexus-7ec4i, P0) — the test
 * the substantive-critic's delta verification named as missing.
 *
 * <p>{@link GrantsSvcForeignOwnedRelationTest} only reconstructs a
 * foreign-owned {@code nexus.diag_chash_conformance} AFTER the full
 * changelog has already run once (so {@code taxonomy-011-1}'s own
 * {@code DROP VIEW} was a no-op the first time — the view did not exist
 * yet). This class reconstructs the missing scenario directly: it replays
 * {@code taxonomy-011-1}'s OWN {@code <sql>} text (via
 * {@link #extractChangesetSql}, the same technique
 * {@link Taxonomy010BackfillDirectIntegrationTest} already established for
 * an otherwise-hard-to-stage changeset) against a connection that owns
 * {@code nexus.topic_assignments} but does NOT own a pre-existing,
 * foreign-owned {@code nexus.diag_chash_conformance} — bypassing
 * Liquibase's own changeset ordering entirely so the precondition (foreign
 * view already present, {@code taxonomy-011-1} not yet applied) can be
 * staged directly rather than fought via partial-update machinery
 * (empirically unreliable above a {@code changesToApply} of 1 on this
 * Liquibase version — the only value the codebase's own existing
 * precedent, {@code SchemaRollbackRoundTripIntegrationTest#reapplyForward},
 * ever actually uses).
 *
 * <p>Each {@code @Test} gets its OWN dedicated container and runs the full
 * migration independently (mirroring {@code GrantsSvcForeignOwnedRelationTest}
 * / {@code Catalog013RlsReplayTest}'s own per-test-container shape) —
 * deliberately NOT a shared {@code @BeforeAll}: an earlier revision of this
 * class shared one container across both tests and both failed with
 * confusing, order-dependent errors (a stale doc_id column type left by
 * whichever test happened to run first) — proof this isolation is required,
 * not merely stylistic.
 *
 * <p><strong>Two properties, not one.</strong> The critique's own item 3
 * asked for "the walk COMPLETES with the wrapped skips firing their
 * NOTICEs and the conversion applied" — VERIFIED FALSE by direct
 * reproduction against a real PostgreSQL 17 instance during this fix round
 * (a table-owning-but-not-view-owning role's {@code ALTER COLUMN TYPE} hits
 * "cannot alter type of a column used by a view or rule" REGARDLESS of who
 * owns the dependent view — a structural dependency restriction, not a
 * permissions check). A graceful skip-and-continue on {@code
 * taxonomy-011-1}'s own {@code DROP VIEW} is therefore NOT achievable: the
 * very next statement (the {@code ALTER}) would hit the identical block
 * anyway, just with a worse, unnamed error. So what this class actually
 * proves is the CORRECT, achievable pair:
 * <ol>
 *   <li>{@link #foreignOwnedView_failsLoudlyWithNamedRemedy_notSilently}
 *       — the replay FAILS (it must — the conversion genuinely cannot
 *       proceed while any view depends on {@code doc_id}), but with the
 *       NAMED, actionable {@code nexus-7ec4i} message instead of a raw,
 *       unnamed Postgres error, and {@code doc_id} is verifiably left
 *       untouched (still TEXT) — the critique's own item 2 "stale TEXT-era
 *       view sitting over a bytea column" scenario is UNREACHABLE by
 *       construction: the ALTER never runs while a foreign-owned view
 *       survives.</li>
 *   <li>{@link #foreignOwnedViewDropped_beforeRetry_letsTheConversionApply}
 *       — confirms the documented remedy (a superuser drops the
 *       foreign-owned view, then the migration is retried) actually works:
 *       once the view is gone, the SAME extracted SQL applies cleanly and
 *       {@code doc_id} converts to bytea.</li>
 * </ol>
 */
class Taxonomy011ForeignOwnedDiagViewTest {

    private static final String CHANGELOG_FILE = "db/changelog/taxonomy-011-doc-id-bytea.xml";
    private static final String CHANGESET_ID = "taxonomy-011-1";
    private static final String ADMIN_ROLE = "nexus_admin_diagview_replay";
    private static final String ADMIN_PASS = "nexus_admin_diagview_replay_pw";

    @Test
    void foreignOwnedView_failsLoudlyWithNamedRemedy_notSilently() throws Exception {
        try (var pg = PgContainerHelper.startDedicated();
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            fullMigrationThenOwnershipReassign(pg, su);
            resetToTextDocIdWithForeignOwnedView(su);

            String sql = extractChangesetSql(CHANGELOG_FILE, CHANGESET_ID);
            try (Connection admin = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)) {
                admin.setAutoCommit(true);
                assertThatThrownBy(() -> admin.createStatement().execute(sql))
                    .as("taxonomy-011-1's DROP VIEW must fail LOUD with the NAMED nexus-7ec4i "
                        + "remedy when the view is foreign-owned -- not a silent skip "
                        + "(structurally impossible for the ALTER to survive a surviving "
                        + "dependent view, verified empirically this fix round against a real "
                        + "PostgreSQL 17 instance) and not a raw, unnamed PostgreSQL error")
                    .isInstanceOf(Exception.class)
                    .hasMessageContaining("nexus-7ec4i")
                    .hasMessageContaining("does not own it and cannot drop it")
                    .hasMessageContaining("DROP VIEW IF EXISTS nexus.diag_chash_conformance")
                    .hasMessageContaining("taxonomy-011-8");
            }

            assertThat(columnUdtName(su))
                .as("the doc_id ALTER must NEVER have run -- the transaction rolled back at "
                    + "the DROP VIEW guard, before the ALTER statement -- proving the "
                    + "critique's 'stale TEXT-era view over a bytea column' scenario is "
                    + "UNREACHABLE, not merely undesirable")
                .isEqualTo("text");
        }
    }

    @Test
    void foreignOwnedViewDropped_beforeRetry_letsTheConversionApply() throws Exception {
        try (var pg = PgContainerHelper.startDedicated();
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            fullMigrationThenOwnershipReassign(pg, su);
            resetToTextDocIdWithForeignOwnedView(su);

            String sql = extractChangesetSql(CHANGELOG_FILE, CHANGESET_ID);
            try (Connection admin = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)) {
                admin.setAutoCommit(true);
                assertThatThrownBy(() -> admin.createStatement().execute(sql))
                    .as("ground truth: the same first failure as the sibling test, before "
                        + "the deploy-window remedy is applied")
                    .isInstanceOf(Exception.class);
            }

            // THE DEPLOY-WINDOW REMEDY (bead nexus-k1dgb): a superuser drops the
            // foreign-owned view immediately before retrying the migration.
            su.createStatement().execute("DROP VIEW nexus.diag_chash_conformance");

            try (Connection admin = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)) {
                admin.setAutoCommit(true);
                assertThatCode(() -> admin.createStatement().execute(sql))
                    .as("once the foreign-owned view is gone, the SAME extracted SQL must "
                        + "apply cleanly -- taxonomy-011-1's DROP VIEW IF EXISTS is now a "
                        + "true no-op (absent), and the ALTER converts doc_id to bytea")
                    .doesNotThrowAnyException();
            }

            assertThat(columnUdtName(su))
                .as("doc_id must be bytea after the clean retry")
                .isEqualTo("bytea");
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────

    /**
     * Full migration as the container superuser, then production-shaped
     * ownership reassignment (same technique as {@code Catalog013RlsReplayTest}):
     * every nexus/t1 table + sequence -> {@link #ADMIN_ROLE}. Deliberately
     * NOT the diag view -- this loop structurally cannot touch it
     * (pg_tables/pg_sequences only) -- leaving it superuser-owned: exactly
     * the "table owner != view owner" shape this bug depends on.
     */
    private static void fullMigrationThenOwnershipReassign(
            org.testcontainers.containers.PostgreSQLContainer<?> pg, Connection su) throws Exception {
        // DEDICATED connection for the Liquibase run -- Catalog013RlsReplayTest's
        // own javadoc documents this exact trap: Liquibase flips its connection
        // to autoCommit=false and leaves that state behind, so reusing `su`
        // directly here would make the role-creation DDL below "an uncommitted
        // CREATE ROLE failing password auth" for every later connection.
        try (Connection migrationConn = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(migrationConn));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }

        su.createStatement().execute(
            "CREATE ROLE " + ADMIN_ROLE + " LOGIN PASSWORD '" + ADMIN_PASS
            + "' NOSUPERUSER NOBYPASSRLS");
        su.createStatement().execute("GRANT USAGE, CREATE ON SCHEMA nexus, t1, public TO " + ADMIN_ROLE);
        su.createStatement().execute(
            "DO $$ DECLARE r record; BEGIN "
            + "  FOR r IN SELECT schemaname, tablename FROM pg_tables "
            + "           WHERE schemaname IN ('nexus', 't1') LOOP "
            + "    EXECUTE format('ALTER TABLE %I.%I OWNER TO " + ADMIN_ROLE + "', "
            + "                   r.schemaname, r.tablename); "
            + "  END LOOP; "
            + "  FOR r IN SELECT schemaname, sequencename FROM pg_sequences "
            + "           WHERE schemaname IN ('nexus', 't1') LOOP "
            + "    EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO " + ADMIN_ROLE + "', "
            + "                   r.schemaname, r.sequencename); "
            + "  END LOOP; "
            + "END $$");
    }

    /**
     * Test-only relaxation (same technique as {@code
     * Taxonomy010BackfillDirectIntegrationTest}): drop whatever diag view
     * exists (from the full migration above, superuser-owned since the
     * superuser ran taxonomy-011-8 too), revert doc_id back to TEXT so
     * {@code taxonomy-011-1}'s OWN TEXT-to-bytea SQL can be replayed
     * exactly as authored, then recreate the diag view as a FOREIGN-owned
     * stub (same simplification {@code GrantsSvcForeignOwnedRelationTest}
     * already established: the ownership-check mechanism under test does
     * not depend on the view's real body, only on it existing and being
     * owned by a role other than {@link #ADMIN_ROLE}).
     */
    private static void resetToTextDocIdWithForeignOwnedView(Connection su) throws Exception {
        su.createStatement().execute("DROP VIEW IF EXISTS nexus.diag_chash_conformance");
        // RDR-194 P3d (nexus-tk070.p3d): the full migration above now ALSO runs
        // taxonomy-012, which ADDs topic_assignments_chunk_fk -- a composite FK
        // whose (source_collection, doc_id) leg targets nexus.chunks(collection,
        // chash), bytea. The ALTER below would now hit "foreign key constraint
        // ... cannot be implemented" (a bytea-vs-TEXT type mismatch on the
        // referencing column) before ever reaching the foreign-owned-view
        // scenario this class exists to prove -- same test-only-relaxation
        // shape as the view drop directly above, and the identical fix
        // Taxonomy010BackfillDirectIntegrationTest applies for the same
        // reason: this dedicated, throwaway container never re-adds the
        // constraint.
        su.createStatement().execute(
            "ALTER TABLE nexus.topic_assignments DROP CONSTRAINT IF EXISTS topic_assignments_chunk_fk");
        su.createStatement().execute(
            "ALTER TABLE nexus.topic_assignments ALTER COLUMN doc_id TYPE TEXT USING encode(doc_id, 'hex')");
        su.createStatement().execute(
            "CREATE VIEW nexus.diag_chash_conformance AS SELECT 1 AS n");
        assertThat(count(su,
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
            + "ON n.oid = c.relnamespace WHERE n.nspname = 'nexus' "
            + "AND c.relname = 'diag_chash_conformance' "
            + "AND pg_get_userbyid(c.relowner) <> '" + ADMIN_ROLE + "'"))
            .as("sanity: the seeded view must be foreign-owned relative to the replay role")
            .isEqualTo(1);
    }

    /**
     * Read the direct {@code <sql>} children of {@code <changeSet id="changesetId">}
     * out of {@code changelogFile} (classpath resource), concatenated in document
     * order. Same technique as
     * {@code Taxonomy010BackfillDirectIntegrationTest#extractChangesetSql} /
     * {@code SchemaRollbackRoundTripIntegrationTest#extractChangesetSql}
     * (duplicated locally rather than shared — no common test-utility class
     * exists for this yet). Deliberately NOT recursive — only direct
     * children are the changeset's FORWARD SQL, not the nested
     * {@code <rollback>}'s.
     */
    private static String extractChangesetSql(String changelogFile, String changesetId) throws Exception {
        Document doc;
        try (var in = Taxonomy011ForeignOwnedDiagViewTest.class.getClassLoader()
                .getResourceAsStream(changelogFile)) {
            if (in == null) {
                throw new IllegalStateException("changelog not found on classpath: " + changelogFile);
            }
            var factory = DocumentBuilderFactory.newInstance();
            factory.setNamespaceAware(true);
            doc = factory.newDocumentBuilder().parse(in);
        }
        NodeList changeSets = doc.getElementsByTagNameNS(
            "http://www.liquibase.org/xml/ns/dbchangelog", "changeSet");
        for (int i = 0; i < changeSets.getLength(); i++) {
            Element cs = (Element) changeSets.item(i);
            if (changesetId.equals(cs.getAttribute("id"))) {
                StringBuilder sb = new StringBuilder();
                NodeList children = cs.getChildNodes();
                for (int j = 0; j < children.getLength(); j++) {
                    Node n = children.item(j);
                    if (n.getNodeType() == Node.ELEMENT_NODE && "sql".equals(n.getLocalName())) {
                        sb.append(n.getTextContent()).append('\n');
                    }
                }
                return sb.toString();
            }
        }
        throw new IllegalStateException("changeset not found: " + changesetId + " in " + changelogFile);
    }

    private static String columnUdtName(Connection c) throws Exception {
        try (var ps = c.prepareStatement(
            "SELECT udt_name FROM information_schema.columns "
            + "WHERE table_schema = 'nexus' AND table_name = 'topic_assignments' "
            + "AND column_name = 'doc_id'")) {
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getString(1);
            }
        }
    }

    private static int count(Connection c, String sql) throws Exception {
        try (Statement st = c.createStatement(); ResultSet rs = st.executeQuery(sql)) {
            rs.next();
            return rs.getInt(1);
        }
    }
}
