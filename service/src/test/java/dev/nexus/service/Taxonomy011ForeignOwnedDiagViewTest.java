// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.impl.DSL;
import org.jooq.SQLDialect;
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
import java.util.Map;

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
 * <p>Each {@code @Test} gets its OWN empty database (it declares the replay role exclusive: a bare CREATE ROLE on the shared cluster) and runs the full
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
        try (var pg = PgContainerHelper.startEmptyDatabase(ADMIN_ROLE);
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            migrateThenSeedForeignOwnedView(pg, su);

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
        try (var pg = PgContainerHelper.startEmptyDatabase(ADMIN_ROLE);
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            migrateThenSeedForeignOwnedView(pg, su);

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
            DSL.using(su, SQLDialect.POSTGRES).dropView(DSL.name("nexus", "diag_chash_conformance")).execute();

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
     * Full migration as the container superuser, then {@code db.changelog-test-diag-view-foreign-owned.xml}
     * (nexus-cbo4a): production-shaped ownership reassignment of every nexus/t1 table and sequence to
     * {@link #ADMIN_ROLE}, and the test-only relaxation that reverts {@code topic_assignments.doc_id} to
     * TEXT so {@code taxonomy-011-1}'s own SQL replays as authored, with the diag view recreated as a
     * superuser-owned (so foreign-owned) stub. See that file's header for each step's reason.
     */
    private static void migrateThenSeedForeignOwnedView(
            org.testcontainers.containers.PostgreSQLContainer<?> pg, Connection su) throws Exception {
        // DEDICATED connection for the master-changelog run -- Catalog013RlsReplayTest's
        // own javadoc documents this exact trap: Liquibase flips its connection
        // to autoCommit=false and leaves that state behind, so `su` would be left
        // mid-transaction for the checks below. (runSuperuserTestChangelog restores
        // autoCommit itself, so the fixture changelog can run on `su`.)
        try (Connection migrationConn = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(migrationConn));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }

        PgContainerHelper.runSuperuserTestChangelog(su,
            "db/changelog-test/db.changelog-test-diag-view-foreign-owned.xml",
            "databasechangelog_test_diag_view", Map.of("adminRole", ADMIN_ROLE, "adminPass", ADMIN_PASS));
        String owner = PgCatalogProbes.relationOwner(
            DSL.using(su, SQLDialect.POSTGRES), "nexus", "diag_chash_conformance");
        assertThat(owner)
            .as("sanity: the seeded view must be foreign-owned relative to the replay role")
            .isNotNull()
            .isNotEqualTo(ADMIN_ROLE);
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
        PgCatalogProbes.ColumnInfo col = PgCatalogProbes.columnInfo(
            DSL.using(c, SQLDialect.POSTGRES), "nexus", "topic_assignments", "doc_id");
        assertThat(col).as("nexus.topic_assignments.doc_id must exist").isNotNull();
        return col.udtName();
    }

}
