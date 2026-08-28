// SPDX-License-Identifier: AGPL-3.0-or-later
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

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.ResultSet;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * nexus-v3w9n Amendment 2 — catalog-034-tumbler-grammar.xml now carries
 * ONLY the data changeset (034-0) that tombstones sub-3-segment LIVE
 * catalog_documents rows. The two schema CHECK constraints
 * (catalog_owners_prefix_grammar_ck / catalog_documents_tumbler_grammar_ck)
 * are DEFERRED to nexus-ia69x — the grammar itself is enforced instead at
 * the HTTP boundary (see {@code TumblerGrammar} /
 * {@code CatalogHandlerTumblerGrammarTest}). This class therefore asserts
 * only the data step, not any constraint existence or DB-level refusal.
 *
 * <p>DEVIATION (same shape as the original Catalog034 round): {@link
 * PgContainerHelper#start()} returns a database already migrated by the
 * per-fork shared cluster (nexus-yhmav) — 034-0 already ran once, against
 * an empty table, before this class's own {@code lb.update()} replay (a
 * no-op for already-applied changesets). There is no pre-migration window
 * left in which to seed a violating row and let the REAL changeset walk
 * tombstone it. Per Amendment 2's own accepted fallback, this test instead
 * (a) inserts a violating 2-segment LIVE document directly (this now
 * SUCCEEDS — no schema CHECK exists to block it) and (b) replays the
 * changeset's own {@code <sql>} text verbatim (extracted from the XML, the
 * same technique {@code Catalog016SourceUriUniqueTest} uses for its own
 * already-migrated container) to prove the tombstone LOGIC itself, rather
 * than re-triggering Liquibase.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Catalog034TumblerGrammarTest {

    private static final String TENANT   = "cat034-tenant";
    private static final String SVC_ROLE = "svc_cat034_test";
    private static final String SVC_PASS = "svc_cat034_pass";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su)));
            lb.update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.grantServiceSchemaAccess(su, SVC_ROLE);
        }
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void twoSegmentLiveDocumentInsertNowSucceeds() throws Exception {
        // No schema CHECK exists any more (Amendment 2) -- this shape is
        // only refused at the HTTP boundary, never by the repository layer
        // or a raw INSERT.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) "
                + "VALUES ('" + TENANT + "', '9999.1', 'raw-two-segment-doc')");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT deleted_at FROM nexus.catalog_documents "
                + "WHERE tenant_id = '" + TENANT + "' AND tumbler = '9999.1'");
            assertTrue(rs.next());
            assertEquals(null, rs.getTimestamp(1));
        }
    }

    @Test
    void dataStepSqlTombstonesSubThreeSegmentLiveDocuments() throws Exception {
        // Fidelity test of the SHIPPED 034-0 SQL: seed a violating 2-segment
        // LIVE row directly (succeeds -- no CHECK), replay the changeset's
        // own <sql> text verbatim, and assert it gets tombstoned. A sibling
        // conforming (3-segment) row must survive untouched.
        String xml = new String(
            getClass().getClassLoader()
                .getResourceAsStream("db/changelog/catalog-034-tumbler-grammar.xml")
                .readAllBytes(),
            StandardCharsets.UTF_8);
        String dataStepSql = extractSql(xml, 0);
        assertTrue(dataStepSql.contains("NO FORCE ROW LEVEL SECURITY"),
            "034-0 must carry the catalog-013-1b FORCE-RLS toggle");
        assertTrue(dataStepSql.contains("tumbler !~"),
            "034-0's shipped <sql> no longer contains the expected grammar predicate -- "
            + "re-derive this test against the current changeset text");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) VALUES "
                + "('" + TENANT + "', '8888.1', 'violator'),"
                + "('" + TENANT + "', '8888.1.1', 'conforming-sibling')");

            // splitStatements="false" in the shipped changeset -- this is a single
            // DO $$ ... $$ block whose body contains internal semicolons, so it
            // must be sent as ONE statement, not split on ';' (the PostgreSQL
            // JDBC driver's simple-query protocol accepts multiple ;-separated
            // top-level statements in one execute() call).
            su.createStatement().execute(dataStepSql);

            ResultSet violator = su.createStatement().executeQuery(
                "SELECT deleted_at FROM nexus.catalog_documents "
                + "WHERE tenant_id = '" + TENANT + "' AND tumbler = '8888.1'");
            assertTrue(violator.next());
            assertTrue(violator.getTimestamp(1) != null, "the 2-segment row must be tombstoned");

            ResultSet conforming = su.createStatement().executeQuery(
                "SELECT deleted_at FROM nexus.catalog_documents "
                + "WHERE tenant_id = '" + TENANT + "' AND tumbler = '8888.1.1'");
            assertTrue(conforming.next());
            assertEquals(null, conforming.getTimestamp(1), "the 3-segment sibling must survive untouched");
        }
    }

    /** Extract the Nth &lt;sql&gt; block's text content from changeset XML. */
    private static String extractSql(String xml, int n) {
        int idx = -1;
        for (int i = 0; i <= n; i++) idx = xml.indexOf("<sql", idx + 1);
        assertTrue(idx >= 0, "changeset <sql> block " + n + " not found");
        int open = xml.indexOf('>', idx) + 1;
        int close = xml.indexOf("</sql>", open);
        return xml.substring(open, close)
                  .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&");
    }
}
