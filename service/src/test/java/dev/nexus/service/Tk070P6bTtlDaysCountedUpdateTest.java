// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;

import javax.xml.parsers.DocumentBuilderFactory;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLWarning;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-tk070.p6b (RDR-194 § D5, § P6b) — direct proof of
 * {@code telemetry-006-frecency-ttl-null.xml}'s counted-UPDATE RAISE
 * NOTICE (both {@code nexus.frecency} and {@code staging.frecency}), and
 * the surviving-row shape after the CHECK lands on {@code nexus.frecency}.
 *
 * <p><strong>Technique.</strong> Same test-only-relaxation shape as
 * {@link Tk070P6aTtlDaysCountedDeleteTest} / {@link
 * Taxonomy010BackfillDirectIntegrationTest}: {@code @BeforeAll} runs a full
 * HEAD migration (so {@code telemetry-006-1}/{@code telemetry-006-2} have
 * already applied once, over zero rows per nexus-tk070.cc6). Each test then
 * relaxes the post-migration schema back toward the pre-migration shape
 * (drop the CHECK / restore NOT NULL DEFAULT 0 — a dedicated, throwaway
 * container, never shared with another test class), seeds rows including
 * TWO with {@code ttl_days = 0} plus decoy rows ({@code ttl_days IS NULL},
 * {@code ttl_days = 30}) that must survive untouched, then re-executes the
 * changeset's OWN {@code <sql>} text (extracted from the changelog XML,
 * never duplicated inline) via a plain JDBC {@link Statement#execute
 * (String)} — which lets the test capture the RAISE NOTICE off the
 * driver's {@link SQLWarning} chain, unavailable to Liquibase's own
 * internal migration connection.
 *
 * <p>Runs as the container superuser (implicit BYPASSRLS) — proving the
 * changeset's own {@code NO FORCE}/{@code FORCE ROW LEVEL SECURITY} toggle
 * actually reaches every tenant's rows is this test's OWN job: the seeded
 * rows below span TWO tenants, and the NOTICE-reported count must cover
 * both.
 *
 * <p>Unlike {@code memory-003-ttl-days.xml}/{@code plans-003-ttl-days.xml}
 * (a counted DELETE — the rows are UNREPRESENTABLE and discarded), this
 * changeset is a counted UPDATE (the rows are RECOVERED by converting the
 * sentinel) — the ground-truth assertions below check the converted rows'
 * NEW value (NULL), not their absence.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Tk070P6bTtlDaysCountedUpdateTest {

    private static final String NEXUS_CHANGELOG = "db/changelog/telemetry-006-frecency-ttl-null.xml";
    private static final String NEXUS_CHANGESET_ID = "telemetry-006-1";
    private static final String STAGING_CHANGELOG = "db/changelog/telemetry-006-frecency-ttl-null.xml";
    private static final String STAGING_CHANGESET_ID = "telemetry-006-2";
    private static final String TENANT_A = "p6b-direct-a";
    private static final String TENANT_B = "p6b-direct-b";

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        // Dedicated, not shared: this test DROPs a CHECK constraint and
        // restores NOT NULL DEFAULT 0, a global schema mutation that must
        // not leak into any other test class's shared-cluster assumptions.
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void nexusFrecencyCountedUpdate_reportsExactRowCountAcrossTenants() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // Test-only relaxation: undo telemetry-006-1 back to a shape
            // that accepts ttl_days=0 again (the CHECK now forbids it).
            su.createStatement().execute(
                "ALTER TABLE nexus.frecency DROP CONSTRAINT frecency_ttl_days_positive_chk");
            su.createStatement().execute(
                "ALTER TABLE nexus.frecency ALTER COLUMN ttl_days SET DEFAULT 0");

            // Two ttl_days=0 rows, spanning two tenants (proves the RLS
            // toggle reaches every tenant, not just one).
            seedFrecencyRow(su, TENANT_A, "1".repeat(64), 0);
            seedFrecencyRow(su, TENANT_B, "2".repeat(64), 0);
            // Decoys that must survive untouched: NULL (already permanent)
            // and a genuine positive ttl_days.
            seedFrecencyRow(su, TENANT_A, "3".repeat(64), null);
            seedFrecencyRow(su, TENANT_A, "4".repeat(64), 30);

            assertThat(countFrecencyRows(su, "nexus", "ttl_days = 0"))
                .as("ground truth before re-running telemetry-006-1's SQL")
                .isEqualTo(2);

            String sql = extractChangesetSql(NEXUS_CHANGELOG, NEXUS_CHANGESET_ID);
            List<String> notices;
            try (Statement st = su.createStatement()) {
                st.execute(sql);
                notices = collectNotices(st.getWarnings());
            }

            assertThat(notices)
                .as("the counted-UPDATE RAISE NOTICE must be captured")
                .anyMatch(n -> n.contains("nexus.frecency") && n.contains("2"));
            assertThat(notices)
                .as("the NOTICE must report the exact row count, not a vague message")
                .anyMatch(n -> n.contains("converted 2 nexus.frecency row(s)"));

            // ── Converted-row ground truth: NULL, not gone ──
            assertThat(countFrecencyRows(su, "nexus", "chunk_id = '" + "1".repeat(64) + "' AND ttl_days IS NULL"))
                .as("a converted row must now read NULL, not be deleted")
                .isEqualTo(1);
            assertThat(countFrecencyRows(su, "nexus", "chunk_id = '" + "2".repeat(64) + "' AND ttl_days IS NULL"))
                .isEqualTo(1);
            assertThat(countFrecencyRows(su, "nexus", "chunk_id = '" + "3".repeat(64) + "' AND ttl_days IS NULL"))
                .as("a NULL-ttl_days (already permanent) decoy must survive untouched")
                .isEqualTo(1);
            assertThat(countFrecencyRows(su, "nexus", "chunk_id = '" + "4".repeat(64) + "' AND ttl_days = 30"))
                .as("a positive-ttl_days decoy must survive untouched")
                .isEqualTo(1);

            // ── Schema ground truth: CHECK re-applied ──
            assertThat(checkConstraintExists(su, "frecency_ttl_days_positive_chk")).isTrue();
        }
    }

    @Test
    void stagingFrecencyCountedUpdate_reportsExactRowCountAcrossTenants() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // staging.frecency never had a CHECK — only NOT NULL DEFAULT 0
            // needs restoring to seed ttl_days=0 rows again via the default.
            su.createStatement().execute(
                "ALTER TABLE staging.frecency ALTER COLUMN ttl_days SET DEFAULT 0");

            seedStagingFrecencyRow(su, TENANT_A, "5".repeat(64), 0);
            seedStagingFrecencyRow(su, TENANT_B, "6".repeat(64), 0);
            seedStagingFrecencyRow(su, TENANT_A, "7".repeat(64), null);
            seedStagingFrecencyRow(su, TENANT_A, "8".repeat(64), 30);

            assertThat(countFrecencyRows(su, "staging", "ttl_days = 0"))
                .as("ground truth before re-running telemetry-006-2's SQL")
                .isEqualTo(2);

            String sql = extractChangesetSql(STAGING_CHANGELOG, STAGING_CHANGESET_ID);
            List<String> notices;
            try (Statement st = su.createStatement()) {
                st.execute(sql);
                notices = collectNotices(st.getWarnings());
            }

            assertThat(notices)
                .as("the counted-UPDATE RAISE NOTICE must be captured")
                .anyMatch(n -> n.contains("staging.frecency") && n.contains("2"));
            assertThat(notices)
                .as("the NOTICE must report the exact row count")
                .anyMatch(n -> n.contains("converted 2 staging.frecency row(s)"));

            assertThat(countFrecencyRows(su, "staging", "chunk_id = '" + "5".repeat(64) + "' AND ttl_days IS NULL"))
                .isEqualTo(1);
            assertThat(countFrecencyRows(su, "staging", "chunk_id = '" + "6".repeat(64) + "' AND ttl_days IS NULL"))
                .isEqualTo(1);
            assertThat(countFrecencyRows(su, "staging", "chunk_id = '" + "7".repeat(64) + "' AND ttl_days IS NULL"))
                .as("a NULL-ttl_days decoy must survive untouched")
                .isEqualTo(1);
            assertThat(countFrecencyRows(su, "staging", "chunk_id = '" + "8".repeat(64) + "' AND ttl_days = 30"))
                .as("a positive-ttl_days decoy must survive untouched")
                .isEqualTo(1);

            // staging never gets the CHECK (typeless landing, by design).
            assertThat(checkConstraintExists(su, "staging_frecency_ttl_days_positive_chk")).isFalse();
        }
    }

    // ── Seeding helpers ───────────────────────────────────────────────────

    private static void seedFrecencyRow(Connection c, String tenant, String chunkId, Integer ttlDays)
            throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.frecency (tenant_id, chunk_id, ttl_days) VALUES (?, ?, ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, chunkId);
            if (ttlDays == null) {
                ps.setNull(3, java.sql.Types.INTEGER);
            } else {
                ps.setInt(3, ttlDays);
            }
            ps.executeUpdate();
        }
    }

    private static void seedStagingFrecencyRow(Connection c, String tenant, String chunkId, Integer ttlDays)
            throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO staging.frecency (tenant_id, chunk_id, ttl_days) VALUES (?, ?, ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, chunkId);
            if (ttlDays == null) {
                ps.setNull(3, java.sql.Types.INTEGER);
            } else {
                ps.setInt(3, ttlDays);
            }
            ps.executeUpdate();
        }
    }

    private static int countFrecencyRows(Connection c, String schema, String whereClause) throws Exception {
        return count(c, "SELECT count(*) FROM " + schema + ".frecency WHERE " + whereClause);
    }

    private static int count(Connection c, String sql) throws Exception {
        try (Statement st = c.createStatement(); ResultSet rs = st.executeQuery(sql)) {
            rs.next();
            return rs.getInt(1);
        }
    }

    private static boolean checkConstraintExists(Connection c, String constraintName) throws Exception {
        return count(c, "SELECT count(*) FROM pg_constraint WHERE conname = '"
            + constraintName + "' AND contype = 'c'") == 1;
    }

    // ── Shared extraction/notice helpers (mirrors
    //    Tk070P6aTtlDaysCountedDeleteTest's own private copies — duplicated
    //    per this project's established per-test-class convention rather
    //    than a shared utility class) ─────────────────────────────────────

    private static String extractChangesetSql(String changelogFile, String changesetId) throws Exception {
        Document doc;
        try (var in = Tk070P6bTtlDaysCountedUpdateTest.class.getClassLoader()
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

    private static List<String> collectNotices(SQLWarning first) {
        List<String> out = new ArrayList<>();
        SQLWarning w = first;
        while (w != null) {
            out.add(w.getMessage());
            w = w.getNextWarning();
        }
        return out;
    }
}
