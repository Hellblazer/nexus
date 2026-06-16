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
 * RDR-154 P2 (bead nexus-2zv75) — updated_at BEFORE UPDATE triggers.
 *
 * <p>Asserts: updated_at exists on EXACTLY document_aspects + topics; the shared
 * stamp function is SECURITY INVOKER; a partial UPDATE moves updated_at forward;
 * and the four append-only logs provably have NO updated_at column.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class UpdatedAtTriggerTest {

    private static final String TENANT = "uat-tenant";
    private static final String OLD_TS = "2020-01-01T00:00:00+00";

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su))).update(new Contexts());
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    @Test
    void updatedAt_presentOnExactlyTheTwoTables() throws Exception {
        try (Connection c = pg.createConnection("")) {
            assertThat(hasColumn(c, "document_aspects", "updated_at"))
                .as("document_aspects must have updated_at").isTrue();
            assertThat(hasColumn(c, "topics", "updated_at"))
                .as("topics must have updated_at").isTrue();
        }
    }

    @Test
    void appendOnlyLogs_haveNoUpdatedAtColumn() throws Exception {
        try (Connection c = pg.createConnection("")) {
            for (String log : List.of("chash_index", "nx_answer_runs",
                                      "hook_failures", "aspect_promotion_log")) {
                assertThat(hasColumn(c, log, "updated_at"))
                    .as("append-only log nexus.%s must NOT have updated_at", log)
                    .isFalse();
            }
        }
    }

    @Test
    void stampFunction_isSecurityInvoker() throws Exception {
        try (Connection c = pg.createConnection("")) {
            ResultSet rs = c.createStatement().executeQuery(
                "SELECT prosecdef FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                + "WHERE n.nspname = 'nexus' AND p.proname = 'stamp_updated_at'");
            assertThat(rs.next()).as("stamp_updated_at must exist").isTrue();
            assertThat(rs.getBoolean("prosecdef"))
                .as("stamp_updated_at must be SECURITY INVOKER (prosecdef=false)").isFalse();
        }
    }

    @Test
    void topics_partialUpdate_movesUpdatedAt() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // Insert with a deliberately OLD updated_at (INSERT does not fire the
            // BEFORE UPDATE trigger), then UPDATE a single column.
            su.createStatement().execute(
                "INSERT INTO nexus.topics (tenant_id, label, collection, doc_count, created_at, review_status, updated_at) "
                + "VALUES ('" + TENANT + "', 'uat-topic', 'c', 0, now(), 'pending', '" + OLD_TS + "')");
            su.createStatement().execute(
                "UPDATE nexus.topics SET label = 'uat-topic-renamed' "
                + "WHERE tenant_id = '" + TENANT + "' AND label = 'uat-topic'");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT updated_at FROM nexus.topics WHERE tenant_id = '" + TENANT + "' "
                + "AND label = 'uat-topic-renamed'");
            assertThat(rs.next()).isTrue();
            var updatedAt = rs.getObject("updated_at", java.time.OffsetDateTime.class).toInstant();
            assertThat(updatedAt)
                .as("BEFORE UPDATE trigger must move updated_at off the seeded 2020 value")
                .isAfter(java.time.OffsetDateTime.parse("2020-01-02T00:00:00+00:00").toInstant());
        }
    }

    @Test
    void documentAspects_partialUpdate_movesUpdatedAt() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.document_aspects "
                + "(tenant_id, collection, source_path, extracted_at, model_version, extractor_name, salient_sentences, updated_at) "
                + "VALUES ('" + TENANT + "', 'c', 'p1', now(), 'v1', 'ex', 'before', '" + OLD_TS + "')");
            // Partial UPDATE to salient_sentences (the path that bypasses extracted_at).
            su.createStatement().execute(
                "UPDATE nexus.document_aspects SET salient_sentences = 'after' "
                + "WHERE tenant_id = '" + TENANT + "' AND collection = 'c' AND source_path = 'p1'");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT updated_at FROM nexus.document_aspects "
                + "WHERE tenant_id = '" + TENANT + "' AND collection = 'c' AND source_path = 'p1'");
            assertThat(rs.next()).isTrue();
            var updatedAt = rs.getObject("updated_at", java.time.OffsetDateTime.class).toInstant();
            assertThat(updatedAt)
                .as("partial UPDATE to salient_sentences must move updated_at")
                .isAfter(java.time.OffsetDateTime.parse("2020-01-02T00:00:00+00:00").toInstant());
        }
    }

    private static boolean hasColumn(Connection c, String table, String col) throws Exception {
        ResultSet rs = c.createStatement().executeQuery(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'nexus' "
            + "AND table_name = '" + table + "' AND column_name = '" + col + "'");
        return rs.next();
    }
}
