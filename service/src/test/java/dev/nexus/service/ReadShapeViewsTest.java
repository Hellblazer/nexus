// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-154 P1.2 (bead nexus-h9qyp) — security_invoker read-shape views.
 *
 * <p>Two guarantees, mirroring CollectionVectorStatsTest GROUP 3 + GROUP 5:
 * <ul>
 *   <li>Every one of the five views has {@code security_invoker=true} PHYSICALLY
 *       set in pg_class.reloptions (a comment / a changelog-grep is not proof
 *       that Liquibase actually applied it).</li>
 *   <li>Cross-tenant isolation under a NOSUPERUSER NOBYPASSRLS svc role + GUC:
 *       the grouped views (which carry tenant_id) leak ZERO foreign-tenant rows,
 *       and the scalar catalog_stats view scopes its counts to the GUC tenant.
 *       A superuser CONTROL proves foreign rows exist underneath (non-vacuous).</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ReadShapeViewsTest {

    private static final String TENANT_A = "rsv-tenant-a";
    private static final String TENANT_B = "rsv-tenant-b";
    private static final String SVC_ROLE = "svc_rsv_test";
    private static final String SVC_PASS = "svc_rsv_test_pass";

    private static final List<String> VIEWS = List.of(
        "catalog_stats", "collection_doc_counts", "coverage_by_content_type",
        "collection_health_meta", "topics_with_counts");

    // The four views that carry a tenant_id column (catalog_stats is scalar).
    private static final List<String> GROUPED_VIEWS = List.of(
        "collection_doc_counts", "coverage_by_content_type",
        "collection_health_meta", "topics_with_counts");

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
        }

        try (Connection su = pg.createConnection("")) {
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su))).update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (String tbl : List.of("catalog_documents", "catalog_links", "topics")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + tbl + " TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");

            // Fixtures: TENANT_A has 2 docs (one linked) + 1 topic in collection c_a;
            // TENANT_B has 1 doc + 1 topic in collection c_b.
            seedDoc(su, TENANT_A, "a.1", "paper", "c_a");
            seedDoc(su, TENANT_A, "a.2", "code",  "c_a");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_links (tenant_id, from_tumbler, to_tumbler, link_type, created_by) "
                + "VALUES ('" + TENANT_A + "', 'a.1', 'a.2', 'cites', 'test')");
            seedTopic(su, TENANT_A, "topic-a", "c_a");

            seedDoc(su, TENANT_B, "b.1", "paper", "c_b");
            seedTopic(su, TENANT_B, "topic-b", "c_b");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void seedDoc(Connection su, String tenant, String tumbler,
                                String ctype, String coll) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, content_type, physical_collection, indexed_at) "
            + "VALUES ('" + tenant + "', '" + tumbler + "', 'T', '" + ctype + "', '" + coll + "', '2026-01-01T00:00:00Z')");
    }

    private static void seedTopic(Connection su, String tenant, String label, String coll) throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.topics (tenant_id, label, collection, doc_count, created_at, review_status) "
            + "VALUES ('" + tenant + "', '" + label + "', '" + coll + "', 0, now(), 'pending')");
    }

    // ── reloption physically set on every view ──────────────────────────────────

    @Test @Order(10)
    void everyView_hasSecurityInvokerReloption() throws Exception {
        try (Connection su = pg.createConnection("")) {
            for (String view : VIEWS) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT reloptions FROM pg_class WHERE oid = 'nexus." + view + "'::regclass");
                assertThat(rs.next()).as("view nexus.%s must exist", view).isTrue();
                java.sql.Array arr = rs.getArray(1);
                assertThat(arr).as("nexus.%s must HAVE reloptions", view).isNotNull();
                assertThat((String[]) arr.getArray())
                    .as("nexus.%s must have security_invoker=true PHYSICALLY set (RDR-154 standing rule)", view)
                    .contains("security_invoker=true");
            }
        }
    }

    // ── grouped views leak zero foreign-tenant rows ─────────────────────────────

    @Test @Order(20)
    void groupedViews_gucA_seeZeroTenantBRows() throws Exception {
        // CONTROL: superuser sees BOTH tenants in each grouped view (foreign rows exist).
        try (Connection su = pg.createConnection("")) {
            for (String view : GROUPED_VIEWS) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT count(*) FROM nexus." + view + " WHERE tenant_id = '" + TENANT_B + "'");
                rs.next();
                assertThat(rs.getLong(1))
                    .as("CONTROL: superuser must see tenant-B rows in nexus.%s", view)
                    .isGreaterThanOrEqualTo(1L);
            }
        }
        // svc + GUC=A: zero tenant-B rows; at least one tenant-A row.
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute("SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            for (String view : GROUPED_VIEWS) {
                ResultSet rsB = svc.createStatement().executeQuery(
                    "SELECT count(*) FROM nexus." + view + " WHERE tenant_id = '" + TENANT_B + "'");
                rsB.next();
                assertThat(rsB.getLong(1))
                    .as("GUC=A must see ZERO tenant-B rows in nexus.%s (caller RLS via security_invoker)", view)
                    .isEqualTo(0L);
                ResultSet rsA = svc.createStatement().executeQuery(
                    "SELECT count(*) FROM nexus." + view + " WHERE tenant_id = '" + TENANT_A + "'");
                rsA.next();
                assertThat(rsA.getLong(1))
                    .as("GUC=A must see its own tenant-A rows in nexus.%s", view)
                    .isGreaterThanOrEqualTo(1L);
            }
        }
    }

    // ── scalar catalog_stats scopes counts to the GUC tenant ────────────────────

    @Test @Order(30)
    void catalogStats_scopesScalarCountsToGucTenant() throws Exception {
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute("SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            ResultSet a = svc.createStatement().executeQuery(
                "SELECT doc_count, link_count FROM nexus.catalog_stats");
            a.next();
            assertThat(a.getLong("doc_count")).as("GUC=A doc_count is exactly A's 2 docs").isEqualTo(2L);
            assertThat(a.getLong("link_count")).as("GUC=A link_count is exactly A's 1 link").isEqualTo(1L);

            svc.createStatement().execute("SELECT set_config('nexus.tenant', '" + TENANT_B + "', false)");
            ResultSet b = svc.createStatement().executeQuery(
                "SELECT doc_count, link_count FROM nexus.catalog_stats");
            b.next();
            assertThat(b.getLong("doc_count")).as("GUC=B doc_count is exactly B's 1 doc").isEqualTo(1L);
            assertThat(b.getLong("link_count")).as("GUC=B link_count is exactly B's 0 links").isEqualTo(0L);
        }
    }
}
