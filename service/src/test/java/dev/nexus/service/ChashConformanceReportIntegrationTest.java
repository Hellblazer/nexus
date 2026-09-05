// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.Statement;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * substantive-critic CRITICAL (nexus-5nrzk, T2 [21458]): the "seeding a
 * non-conformant chash row is impossible by construction" claim in the
 * Python test suite (tests/db/test_du2dw_chash_conformance_report_engine.py,
 * tests/test_health_service_checks.py::TestCheckChashConformanceReport) was
 * FALSE as originally written — {@link RekeyOpsIntegrationTest}'s {@code
 * withChecksDropped} helper already does exactly this, at the Java layer,
 * via a superuser CHECK-constraint-drop maneuver. THIS class is that
 * missing proof: {@code nexus.chash_conformance_report(dim)} counting a
 * REAL poisoned row and returning its hex in {@code sample_chashes} — the
 * function's {@code non_conformant > 0} branch executing against real
 * poison for the first time (RDR-180, bead nexus-du2dw).
 *
 * <p>Self-contained (own Postgres container via {@link PgContainerHelper},
 * own service role, own Liquibase run) rather than added to {@code
 * CatalogRepositoryTest} — that file is a large {@code @Order}-dependent,
 * shared-fixture class, and a poisoning maneuver (dropping/re-adding CHECK
 * constraints mid-suite) is exactly the kind of cross-test side effect its
 * ordering discipline is fragile against. Mirrors {@link
 * RekeyOpsIntegrationTest}'s fixture shape (own container, own role, own
 * {@code withChecksDropped}) rather than reusing its private helpers
 * directly (package-private-but-different-class visibility, and this
 * class's seeding needs are narrower — one poisoned row, not a full rekey
 * scenario).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ChashConformanceReportIntegrationTest {

    private static final String SVC_ROLE = "svc_chashconf_test";
    private static final String SVC_PASS = "svc_chashconf_pw";
    private static final String TENANT_A = "t-chashconf-a";
    private static final String TENANT_B = "t-chashconf-b";
    // A routable (bge-768) collection name, so catalog_document_chunks rows
    // for it are ALSO counted (matches nexus.chash_conformance_report's
    // dim=768 IN-list, the same routing the now-retired manifest_orphans
    // used, RDR-191 Phase 6 nexus-o8dil.33).
    private static final String COLLECTION = "knowledge__chashconf__bge-base-en-v15-768__v1";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;

    private static byte[] sha256(String text) {
        try {
            return MessageDigest.getInstance("SHA-256")
                .digest(text.getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static String vec(int dim) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < dim; i++) {
            if (i > 0) sb.append(',');
            sb.append('0');
        }
        return sb.append(']').toString();
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(3);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── seeding (superuser: same reconstruction RekeyOpsIntegrationTest's
    //    withChecksDropped uses — drop the width CHECKs, seed the poisoned
    //    row(s), re-add NOT VALID; the client-facing write path enforces the
    //    CHECK even NOT VALID, so this maneuver is the ONLY way to get a
    //    non-conformant row into a real Postgres instance) ─────────────────

    // RDR-191 Phase 4 (repoint-batch lane F1): nexus.chunks_768 unified into
    // nexus.chunks -- the octet-width CHECK is now ONE constraint shared
    // across all three embedding_<dim> columns (chunks_chash_octet_check),
    // not a per-dim constraint. Dropping it opens the poisoning window for
    // whichever dim column this test's insert populates.
    private void withChecksDropped(Connection su, Runnable seed) throws Exception {
        su.createStatement().execute(
            "ALTER TABLE nexus.chunks DROP CONSTRAINT chunks_chash_octet_check");
        su.createStatement().execute(
            "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT catalog_document_chunks_chash_octet_check");
        try {
            seed.run();
        } finally {
            su.createStatement().execute(
                "ALTER TABLE nexus.chunks ADD CONSTRAINT chunks_chash_octet_check "
                + "CHECK (octet_length(chash) = 32) NOT VALID");
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks ADD CONSTRAINT catalog_document_chunks_chash_octet_check "
                + "CHECK (octet_length(chash) = 32) NOT VALID");
        }
    }

    private static void insertChunk768(Connection su, String tenant, byte[] chash, String text) {
        try (PreparedStatement ps = su.prepareStatement(
            "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_768) "
            + "VALUES (?, ?, ?, ?, ?::vector)")) {
            ps.setString(1, tenant);
            ps.setString(2, COLLECTION);
            ps.setBytes(3, chash);
            ps.setString(4, text);
            ps.setString(5, vec(768));
            ps.executeUpdate();
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    private static void exec(Connection c, String sql) throws Exception {
        try (Statement st = c.createStatement()) {
            st.execute(sql);
        }
    }

    private static Map<String, Object> tableRow(List<Map<String, Object>> report, String tableName) {
        return report.stream()
            .filter(r -> tableName.equals(r.get("table_name")))
            .findFirst()
            .orElseThrow(() -> new AssertionError(
                "no row for table_name=" + tableName + " in report=" + report));
    }

    // ── Test 1: the real poison, counted and sampled ────────────────────────

    @Test
    @Order(1)
    void poisoned_row_is_counted_and_sampled_by_chash_conformance_report() throws Exception {
        // 16 bytes -- the pre-RDR-180 legacy width, width-non-conformant
        // under the current (post-rekey) 32-byte-bytea schema.
        byte[] legacyChash = HexFormat.of().parseHex("a".repeat(32));
        String legacyHex = HexFormat.of().formatHex(legacyChash);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            exec(su, "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                + TENANT_A + "', '" + COLLECTION + "') ON CONFLICT DO NOTHING");
            exec(su, "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) VALUES ('"
                + TENANT_A + "', '1.1', 'doc') ON CONFLICT DO NOTHING");
            withChecksDropped(su, () -> {
                // a conformant sibling row too, so total > non_conformant is
                // asserted meaningfully (not just "1 row total, 1 poisoned").
                insertChunk768(su, TENANT_A, sha256("chashconf clean sibling"), "clean sibling");
                insertChunk768(su, TENANT_A, legacyChash, "poisoned legacy row");
                try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_document_chunks "
                    + "(tenant_id, doc_id, position, chash, collection) VALUES ('"
                    + TENANT_A + "', '1.1', 0, ?, '" + COLLECTION + "')")) {
                    ps.setBytes(1, legacyChash);
                    ps.executeUpdate();
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            });
        }

        List<Map<String, Object>> report = repo.chashConformanceReport(TENANT_A, 768);
        assertThat(report).hasSize(2);

        Map<String, Object> chunksRow = tableRow(report, "nexus.chunks_768");
        assertThat(((Number) chunksRow.get("total")).longValue()).isEqualTo(2L);
        assertThat(((Number) chunksRow.get("non_conformant")).longValue()).isEqualTo(1L);
        // jOOQ maps the PG text[] column to a Java String[] (not List) via
        // Record::intoMap.
        assertThat((String[]) chunksRow.get("sample_chashes")).containsExactly(legacyHex);

        Map<String, Object> manifestRow = tableRow(report, "nexus.catalog_document_chunks");
        assertThat(((Number) manifestRow.get("total")).longValue()).isEqualTo(1L);
        assertThat(((Number) manifestRow.get("non_conformant")).longValue()).isEqualTo(1L);
        assertThat((String[]) manifestRow.get("sample_chashes")).containsExactly(legacyHex);
    }

    // ── Test 2: the poison is invisible to another tenant (RLS) ─────────────

    @Test
    @Order(2)
    void poison_is_tenant_isolated_via_rls() throws Exception {
        // TENANT_B has seeded NOTHING -- SECURITY INVOKER + FORCE RLS scopes
        // the function to the request tenant GUC (refutes the 'cross-tenant
        // scan' reading; the SAME class of falsification the now-retired
        // manifest_orphans coverage used to demonstrate, RDR-191 Phase 6
        // nexus-o8dil.33).
        List<Map<String, Object>> report = repo.chashConformanceReport(TENANT_B, 768);
        Map<String, Object> chunksRow = tableRow(report, "nexus.chunks_768");
        assertThat(((Number) chunksRow.get("total")).longValue()).isZero();
        assertThat(((Number) chunksRow.get("non_conformant")).longValue()).isZero();
        assertThat((String[]) chunksRow.get("sample_chashes")).isEmpty();
    }

    // ── Test 3: dim validation ────────────────────────────────────────────

    @Test
    @Order(3)
    void rejects_unsupported_dim() {
        assertThatThrownBy(() -> repo.chashConformanceReport(TENANT_A, 999))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("unsupported dim");
    }
}
