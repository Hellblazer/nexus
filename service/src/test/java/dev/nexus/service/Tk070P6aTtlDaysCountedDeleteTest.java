// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.Condition;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
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
import java.sql.SQLWarning;
import java.sql.Statement;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static dev.nexus.service.jooq.nexus.Tables.PLANS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-tk070.p6a (RDR-194 § D5, § P6a) — direct proof of
 * {@code memory-003-ttl-days.xml}'s and {@code plans-003-ttl-days.xml}'s
 * counted-DELETE RAISE NOTICE, and the surviving-row shape after the
 * RENAME + CHECK land.
 *
 * <p><strong>Technique.</strong> Same test-only-relaxation shape as
 * {@link Taxonomy010BackfillDirectIntegrationTest}: {@code @BeforeAll} runs
 * a full HEAD migration (so {@code memory-003-1}/{@code plans-003-1} have
 * already applied once, over zero rows). Each test then relaxes the
 * post-migration schema back to the PRE-migration shape (drop the CHECK,
 * rename {@code ttl_days} back to {@code ttl} — a dedicated, throwaway
 * container, never shared with another test class), seeds rows including
 * TWO with {@code ttl = 0} plus decoy rows ({@code ttl IS NULL},
 * {@code ttl = 30}) that must survive untouched, then re-executes the
 * changeset's OWN {@code <sql>} text (extracted from the changelog XML,
 * never duplicated inline) via a plain JDBC {@link Statement#execute
 * (String)} — which lets the test capture the RAISE NOTICE off the
 * driver's {@link SQLWarning} chain, unavailable to Liquibase's own
 * internal migration connection.
 *
 * <p>Runs as the container superuser (implicit BYPASSRLS) — proving the
 * changeset's own {@code NO FORCE}/{@code FORCE ROW LEVEL SECURITY} toggle
 * actually reaches every tenant's rows is this test's OWN job (unlike
 * {@code Taxonomy010BackfillDirectIntegrationTest}, which delegates that to
 * a sibling rehearsal leg): the seeded rows below span TWO tenants, and the
 * NOTICE-reported count must cover both.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Tk070P6aTtlDaysCountedDeleteTest {

    private static final String MEMORY_CHANGELOG = "db/changelog/memory-003-ttl-days.xml";
    private static final String MEMORY_CHANGESET_ID = "memory-003-1";
    private static final String PLANS_CHANGELOG = "db/changelog/plans-003-ttl-days.xml";
    private static final String PLANS_CHANGESET_ID = "plans-003-1";
    private static final String TENANT_A = "p6a-direct-a";
    private static final String TENANT_B = "p6a-direct-b";

    /** The renamed-back {@code ttl} column, schema-agnostic: at the point every
     *  {@code count*Rows}/seed call below touches it, the live column is named
     *  {@code ttl} (test-only relaxation), never the HEAD {@code ttl_days} the
     *  generated {@code MEMORY.TTL_DAYS}/{@code PLANS.TTL_DAYS} fields carry. */
    private static final Field<Integer> TTL = DSL.field(DSL.name("ttl"), Integer.class);

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        // Dedicated, not shared: this test DROPs a CHECK constraint and
        // renames a column back to its pre-migration name, a global schema
        // mutation that must not leak into any other test class's
        // shared-cluster assumptions.
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
            // nexus_test.* (dropConstraint below) isn't installed by this file's own
            // migration path (unlike applyProductSchema) -- install it via the SAME
            // connection/role that just ran the product changelog, so whichever role
            // owns databasechangelog stays consistent (nexus-cbo4a batch 12 finding).
            PgContainerHelper.installTestObjects(su);
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void memoryCountedDelete_reportsExactRowCountAcrossTenants() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // Test-only relaxation: undo memory-003-1 back to its pre-migration
            // shape so this test can seed ttl=0 rows again (the CHECK now
            // forbids it) under the ORIGINAL column name the changeset's own
            // SQL text expects (`RENAME COLUMN ttl TO ttl_days` requires the
            // column to currently be named `ttl`).
            PgContainerHelper.dropConstraint(su, MEMORY, "memory_ttl_days_positive_chk");
            // KEPT RAW: no typed jOOQ/nexus_test DDL form for ALTER TABLE ... RENAME
            // COLUMN (candidate for a nexus_test bootstrap function).
            su.createStatement().execute(
                "ALTER TABLE nexus.memory RENAME COLUMN ttl_days TO ttl");

            // Two ttl=0 rows, spanning two tenants (proves the RLS toggle
            // reaches every tenant, not just one).
            seedMemoryRow(su, TENANT_A, "p6a-proj", "zero-a", 0);
            seedMemoryRow(su, TENANT_B, "p6a-proj", "zero-b", 0);
            // Decoys that must survive untouched: NULL (permanent) and a
            // genuine positive ttl.
            seedMemoryRow(su, TENANT_A, "p6a-proj", "permanent-a", null);
            seedMemoryRow(su, TENANT_A, "p6a-proj", "expiring-a", 30);

            assertThat(countMemoryRows(su, TTL.eq(0)))
                .as("ground truth before re-running memory-003-1's SQL")
                .isEqualTo(2);

            String sql = extractChangesetSql(MEMORY_CHANGELOG, MEMORY_CHANGESET_ID);
            List<String> notices;
            try (Statement st = su.createStatement()) {
                st.execute(sql);
                notices = collectNotices(st.getWarnings());
            }

            assertThat(notices)
                .as("the counted-DELETE RAISE NOTICE must be captured")
                .anyMatch(n -> n.contains("nexus.memory") && n.contains("2"));
            assertThat(notices)
                .as("the NOTICE must report the exact row count, not a vague message")
                .anyMatch(n -> n.contains("deleted 2 nexus.memory row(s) with ttl = 0"));

            // ── Surviving-row ground truth ──
            assertThat(countMemoryRows(su, MEMORY.TITLE.eq("zero-a"))).isEqualTo(0);
            assertThat(countMemoryRows(su, MEMORY.TITLE.eq("zero-b"))).isEqualTo(0);
            assertThat(countMemoryRows(su, MEMORY.TITLE.eq("permanent-a")))
                .as("a NULL-ttl (permanent) decoy must survive untouched")
                .isEqualTo(1);
            assertThat(countMemoryRows(su, MEMORY.TITLE.eq("expiring-a")))
                .as("a positive-ttl decoy must survive untouched")
                .isEqualTo(1);

            // ── Schema ground truth: rename + CHECK re-applied ──
            assertThat(columnExists(su, "memory", "ttl_days")).isTrue();
            assertThat(columnExists(su, "memory", "ttl")).isFalse();
            assertThat(checkConstraintExists(su, "memory_ttl_days_positive_chk")).isTrue();
        }
    }

    @Test
    void plansCountedDelete_reportsExactRowCountAcrossTenants() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            PgContainerHelper.dropConstraint(su, PLANS, "plans_ttl_days_positive_chk");
            // KEPT RAW: no typed jOOQ/nexus_test DDL form for ALTER TABLE ... RENAME
            // COLUMN (candidate for a nexus_test bootstrap function).
            su.createStatement().execute(
                "ALTER TABLE nexus.plans RENAME COLUMN ttl_days TO ttl");

            seedPlanRow(su, TENANT_A, "p6a-proj", "zero-a query", 0);
            seedPlanRow(su, TENANT_B, "p6a-proj", "zero-b query", 0);
            seedPlanRow(su, TENANT_A, "p6a-proj", "permanent-a query", null);
            seedPlanRow(su, TENANT_A, "p6a-proj", "expiring-a query", 30);

            assertThat(countPlanRows(su, TTL.eq(0)))
                .as("ground truth before re-running plans-003-1's SQL")
                .isEqualTo(2);

            String sql = extractChangesetSql(PLANS_CHANGELOG, PLANS_CHANGESET_ID);
            List<String> notices;
            try (Statement st = su.createStatement()) {
                st.execute(sql);
                notices = collectNotices(st.getWarnings());
            }

            assertThat(notices)
                .as("the counted-DELETE RAISE NOTICE must be captured")
                .anyMatch(n -> n.contains("nexus.plans") && n.contains("2"));
            assertThat(notices)
                .as("the NOTICE must report the exact row count, not a vague message")
                .anyMatch(n -> n.contains("deleted 2 nexus.plans row(s) with ttl = 0"));

            assertThat(countPlanRows(su, PLANS.QUERY.eq("zero-a query"))).isEqualTo(0);
            assertThat(countPlanRows(su, PLANS.QUERY.eq("zero-b query"))).isEqualTo(0);
            assertThat(countPlanRows(su, PLANS.QUERY.eq("permanent-a query")))
                .as("a NULL-ttl (permanent) decoy must survive untouched")
                .isEqualTo(1);
            assertThat(countPlanRows(su, PLANS.QUERY.eq("expiring-a query")))
                .as("a positive-ttl decoy must survive untouched")
                .isEqualTo(1);

            assertThat(columnExists(su, "plans", "ttl_days")).isTrue();
            assertThat(columnExists(su, "plans", "ttl")).isFalse();
            assertThat(checkConstraintExists(su, "plans_ttl_days_positive_chk")).isTrue();
        }
    }

    // ── Seeding helpers ───────────────────────────────────────────────────

    private static void seedMemoryRow(Connection c, String tenant, String project,
                                       String title, Integer ttl) {
        // TTL, not MEMORY.TTL_DAYS: the live column is named "ttl" here (test-only
        // relaxation, see this file's own javadoc); every other column matches the
        // generated HEAD shape, so those stay typed TableFields.
        DSL.using(c, SQLDialect.POSTGRES)
            .insertInto(MEMORY, MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE, MEMORY.CONTENT,
                MEMORY.TIMESTAMP, TTL)
            .values(tenant, project, title, "content", OffsetDateTime.now(), ttl)
            .execute();
    }

    private static void seedPlanRow(Connection c, String tenant, String project,
                                     String query, Integer ttl) {
        // verb: plans.verb is NOT NULL (hygiene-001-11, nexus-tk070.p6a
        // follow-on) at the time this seed runs -- the ttl_days->ttl rename
        // and CHECK drop above are historical-shape probes, they don't touch
        // the verb column's constraint.
        DSL.using(c, SQLDialect.POSTGRES)
            .insertInto(PLANS, PLANS.TENANT_ID, PLANS.PROJECT, PLANS.QUERY, PLANS.PLAN_JSON,
                PLANS.CREATED_AT, TTL, PLANS.VERB)
            .values(tenant, project, query, JSONB.valueOf("{}"), OffsetDateTime.now(), ttl, "research")
            .execute();
    }

    private static int countMemoryRows(Connection c, Condition where) {
        return DSL.using(c, SQLDialect.POSTGRES).fetchCount(MEMORY, where);
    }

    private static int countPlanRows(Connection c, Condition where) {
        return DSL.using(c, SQLDialect.POSTGRES).fetchCount(PLANS, where);
    }

    private static boolean columnExists(Connection c, String table, String column) {
        return PgCatalogProbes.columnExists(DSL.using(c, SQLDialect.POSTGRES), "nexus", table, column);
    }

    private static boolean checkConstraintExists(Connection c, String constraintName) {
        return PgCatalogProbes.constraintExists(DSL.using(c, SQLDialect.POSTGRES), constraintName);
    }

    // ── Shared extraction/notice helpers (mirrors
    //    Taxonomy010BackfillDirectIntegrationTest's own private copies —
    //    duplicated per this project's established per-test-class
    //    convention rather than a shared utility class) ──────────────────

    private static String extractChangesetSql(String changelogFile, String changesetId) throws Exception {
        Document doc;
        try (var in = Tk070P6aTtlDaysCountedDeleteTest.class.getClassLoader()
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
