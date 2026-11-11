// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.ChashConformanceConstraints;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenStore;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: {@code GET /v1/admin/chash-conformance} through the real {@link NexusService} and its {@code AuthFilter}:
 * the state of the two chash width CHECKs, read from {@code pg_constraint}. Real PostgreSQL, so the partitioned
 * {@code nexus.chunks} is the real partition tree. The classes run in order because they move one database's constraints
 * through the states a reader meets: as the migrations leave them, validated, one dropped and re-added NOT VALID,
 * validated again. The fold of a partition mix is {@link ChashConformanceConstraintsAggregateTest}'s.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ChashConformanceRouteTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String ROOT = "tok-root-chash-conf-0123456789abcdef";
    private static final String TENANT_TOKEN = "tok-tenant-chash-conf-0123456789abcd";
    private static final String MINT_TOKEN = "tok-mint-chash-conf-0123456789abcdef";
    private static final String CHUNKS_CHECK = "chunks_chash_octet_check";
    private static final String MANIFEST_CHECK = "catalog_document_chunks_chash_octet_check";

    private PostgreSQLContainer<?> pg;
    private HikariDataSource ds;
    private NexusService service;
    private final HttpClient http = TestHttp.client();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, ROOT, TenantConstants.DEFAULT_TENANT, TokenStore.ROOT_TOKEN_LABEL,
                TokenStore.SCOPE_ROOT, null, null);
            PgContainerHelper.seedServiceToken(dsl, TENANT_TOKEN, "chash-conf-tenant", "tenant-token");
            PgContainerHelper.seedServiceToken(dsl, MINT_TOKEN, "chash-conf-tenant", "mint-token",
                TokenStore.SCOPE_MINT, null, null);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        service = new NexusService(0, ROOT, ds);
        service.start();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private HttpResponse<String> get(String bearer) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/admin/chash-conformance");
        if (bearer != null) b.header("Authorization", "Bearer " + bearer);
        return http.send(b.GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode report() throws Exception {
        var resp = get(ROOT);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    private static JsonNode row(JsonNode report, String table) {
        for (JsonNode n : report.get("constraints")) {
            if (table.equals(n.get("table").asText())) return n;
        }
        throw new AssertionError("no row for " + table + " in " + report);
    }

    /** VALIDATE CONSTRAINT on nexus.chunks, as the chash re-key rung does (a test changelog: the jOOQ DDL has no VALIDATE). */
    private void validateChunks() throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-validate-chunks-octet-check.xml",
                "databasechangelog_test_validate_chunks_octet", Map.of());
        }
    }

    /** VALIDATE CONSTRAINT on nexus.catalog_document_chunks. */
    private void validateManifest() throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserTestChangelog(su, "db/changelog-test/db.changelog-test-validate-manifest-octet-check.xml",
                "databasechangelog_test_validate_manifest_octet", Map.of());
        }
    }

    private void validateBoth() throws Exception {
        validateChunks();
        validateManifest();
    }

    /** Both constraints dropped (if there) and re-added NOT VALID: the state the migrations leave. */
    private void resetBothNotValid() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.alterTable(CHUNKS).dropConstraintIfExists(CHUNKS_CHECK).execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists(MANIFEST_CHECK).execute();
            PgContainerHelper.readdChashOctetChecksNotValid(su);
        }
    }

    // ── what the migrations leave ───────────────────────────────────────────

    @Test
    @Order(1)
    void aFreshSchemaReportsBothConstraintsPresent_andNotValidated_becauseTheMigrationsLeaveThemNotValid() throws Exception {
        var report = report();
        System.out.println("fresh: " + report);
        assertThat(report.get("constraints")).hasSize(2);
        for (String table : new String[]{"nexus.chunks", "nexus.catalog_document_chunks"}) {
            var r = row(report, table);
            assertThat(r.get("present").asBoolean()).as(table).isTrue();
            assertThat(r.get("validated").asBoolean()).as(table + " is left NOT VALID until the re-key rung validates it").isFalse();
        }
        assertThat(row(report, "nexus.chunks").get("name").asText()).isEqualTo(CHUNKS_CHECK);
        assertThat(row(report, "nexus.catalog_document_chunks").get("name").asText()).isEqualTo(MANIFEST_CHECK);
        assertThat(report.get("all_validated").asBoolean()).isFalse();
        assertThat(report.fieldNames()).toIterable().containsExactlyInAnyOrder("constraints", "all_validated");
    }

    @Test
    @Order(2)
    void thePartitionedTablesCopiesAreCounted_soTheWalkIsNotVacuous_andThePlainTableHasNone() throws Exception {
        var report = report();
        assertThat(row(report, "nexus.chunks").get("partitions").asInt())
            .as("nexus.chunks is partitioned; every partition carries a copy").isPositive();
        // The parent is NOT VALID; a partition created after it (a tenant's leaf, made with its first token) copies it
        // validated, being empty, so the copies are a mix and the count says how many are not valid.
        assertThat(row(report, "nexus.chunks").get("partitions_not_validated").asInt()).isPositive();
        assertThat(row(report, "nexus.catalog_document_chunks").get("partitions").asInt()).isZero();
    }

    // ── validated ───────────────────────────────────────────────────────────

    @Test
    @Order(3)
    void afterBothAreValidated_everyEntryIsValidated_andAllValidatedIsTrue() throws Exception {
        validateBoth();
        var report = report();
        System.out.println("validated: " + report);
        for (String table : new String[]{"nexus.chunks", "nexus.catalog_document_chunks"}) {
            assertThat(row(report, table).get("validated").asBoolean()).as(table).isTrue();
        }
        assertThat(row(report, "nexus.chunks").get("partitions_not_validated").asInt()).isZero();
        assertThat(row(report, "nexus.chunks").get("partitions").asInt()).isPositive();
        assertThat(report.get("all_validated").asBoolean()).isTrue();
    }

    @Test
    @Order(4)
    void aSameNamedConstraintOnAnotherTableDoesNotStandInForAMissingOne() throws Exception {
        // A constraint counts through the table it is declared on. chunks_retired_225 already carries a constraint named
        // chunks_chash_octet_check of its own; here a stray table does too, VALID, while the real one is gone: the entry
        // of nexus.chunks must read absent, not valid.
        validateBoth();
        var stray = DSL.name("nexus", "stray_chash_f3yqd");
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.alterTable(CHUNKS).dropConstraint(CHUNKS_CHECK).execute();
            var chash = DSL.field(DSL.name("chash"), byte[].class);
            ctx.createTable(stray).column(chash)
                .constraint(DSL.constraint(CHUNKS_CHECK)
                    .check(DSL.function("octet_length", org.jooq.impl.SQLDataType.INTEGER, chash).eq(32))).execute();
            try {
                assertThat(ChashConformanceConstraints.query(ctx).fetch())
                    .as("the stray table's constraint is in the statement's rows, so only the fold can exclude it")
                    .anyMatch(r -> r.value3().equals("stray_chash_f3yqd") && r.value5());
                var report = report();
                var chunks = row(report, "nexus.chunks");
                assertThat(chunks.get("present").asBoolean()).isFalse();
                assertThat(chunks.get("validated").asBoolean()).isFalse();
                assertThat(report.get("all_validated").asBoolean()).isFalse();
            } finally {
                ctx.dropTable(stray).execute();
            }
        }
        resetBothNotValid();
    }

    // ── not validated ───────────────────────────────────────────────────────

    @Test
    @Order(5)
    void oneConstraintStillNotValid_readsNotValidated_whileTheOtherValidated() throws Exception {
        resetBothNotValid();
        validateChunks();
        var report = report();
        System.out.println("chunks validated, manifest not: " + report);
        assertThat(row(report, "nexus.catalog_document_chunks").get("present").asBoolean()).isTrue();
        assertThat(row(report, "nexus.catalog_document_chunks").get("validated").asBoolean()).isFalse();
        assertThat(row(report, "nexus.chunks").get("validated").asBoolean()).as("the control: the other is valid").isTrue();
        assertThat(report.get("all_validated").asBoolean()).isFalse();

        resetBothNotValid();
        validateManifest();
        var other = report();
        assertThat(row(other, "nexus.chunks").get("validated").asBoolean()).isFalse();
        assertThat(row(other, "nexus.catalog_document_chunks").get("validated").asBoolean()).isTrue();
        assertThat(other.get("all_validated").asBoolean()).isFalse();
    }

    @Test
    @Order(6)
    void aPartitionedParentReaddedNotValid_readsNotValidated_andCountsItsCopies() throws Exception {
        validateBoth();
        resetBothNotValid();
        var report = report();
        var chunks = row(report, "nexus.chunks");
        assertThat(chunks.get("validated").asBoolean()).isFalse();
        assertThat(chunks.get("partitions").asInt()).isPositive();
        assertThat(chunks.get("partitions_not_validated").asInt()).as("an ADD ... NOT VALID reaches every partition")
            .isEqualTo(chunks.get("partitions").asInt());
        assertThat(report.get("all_validated").asBoolean()).isFalse();
    }

    @Test
    @Order(7)
    void aMissingConstraintIsReportedAbsentAndNotValidated_neverASilentTrue() throws Exception {
        validateBoth();
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraint(MANIFEST_CHECK).execute();
        }
        try {
            var report = report();
            var manifest = row(report, "nexus.catalog_document_chunks");
            assertThat(manifest.get("present").asBoolean()).isFalse();
            assertThat(manifest.get("validated").asBoolean()).isFalse();
            assertThat(row(report, "nexus.chunks").get("validated").asBoolean()).as("the control: the other is valid").isTrue();
            assertThat(report.get("all_validated").asBoolean()).isFalse();
        } finally {
            resetBothNotValid();
        }
    }

    // ── authorization ───────────────────────────────────────────────────────

    @Test
    @Order(8)
    void noCredentialIsA401_aTenantTokenIsA403_aMintTokenIsRefused_andOnlyTheRootTokenReads() throws Exception {
        assertThat(get(null).statusCode()).isEqualTo(401);
        assertThat(get("a-token-the-registry-does-not-know").statusCode()).isEqualTo(401);
        var tenant = get(TENANT_TOKEN);
        assertThat(tenant.statusCode()).isEqualTo(403);
        assertThat(tenant.body()).doesNotContain("chunks_chash").doesNotContain("constraints");
        var mint = get(MINT_TOKEN);
        assertThat(mint.statusCode()).isEqualTo(403);
        assertThat(mint.body()).doesNotContain("constraints");
        assertThat(get(ROOT).statusCode()).isEqualTo(200);
    }

    @Test
    @Order(9)
    void onlyGetIsAllowed() throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/admin/chash-conformance")
            .header("Authorization", "Bearer " + ROOT).POST(HttpRequest.BodyPublishers.ofString("{}")).build();
        assertThat(http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(405);
    }

    @Test
    @Order(10)
    void theReadIsOneCatalogStatement_noProductTableIsNamed() {
        var ctx = DSL.using(SQLDialect.POSTGRES);
        String sql = ctx.renderInlined(ChashConformanceConstraints.query(ctx)).toLowerCase();
        assertThat(sql).contains("pg_constraint").contains("pg_inherits");
        assertThat(sql).doesNotContain("nexus.\"chunks\"").doesNotContain("catalog_document_chunks\" ")
            .doesNotContain("tenant_id");
    }
}
