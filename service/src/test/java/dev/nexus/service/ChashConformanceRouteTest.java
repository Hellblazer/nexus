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
 * nexus-f3yqd: {@code GET /v1/schema/chash-conformance} through the real {@link NexusService} and its {@code AuthFilter}
 * (any authenticated token reads it; only the root token sees the partition counts):
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
    private static final String DATA_TOKEN = "tok-data-chash-conf-0123456789abcdefgh";
    private static final String MINT_LOCKED_TOKEN = "tok-mintlocked-chash-conf-0123456789ab";
    private static final String BOARD_CI_TOKEN = "tok-boardci-chash-conf-0123456789abcd";
    private static final String OTHER_TENANT_TOKEN = "tok-other-chash-conf-0123456789abcdef";
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
            var later = java.time.OffsetDateTime.now().plusHours(1);
            PgContainerHelper.seedServiceToken(dsl, DATA_TOKEN, "chash-conf-tenant", "data-token", TokenStore.SCOPE_DATA, later, null);
            PgContainerHelper.seedServiceToken(dsl, MINT_LOCKED_TOKEN, "chash-conf-tenant", "mint-locked-token",
                TokenStore.SCOPE_MINT_LOCKED, null, null);
            PgContainerHelper.seedServiceToken(dsl, BOARD_CI_TOKEN, "chash-conf-tenant", "board-ci-token",
                TokenStore.SCOPE_BOARD_CI, null, null);
            PgContainerHelper.seedServiceToken(dsl, OTHER_TENANT_TOKEN, "chash-conf-other-tenant", "other-tenant-token");
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
        var b = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/schema/chash-conformance");
        if (bearer != null) b.header("Authorization", "Bearer " + bearer);
        return http.send(b.GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    /** A GET that also sends the client-claimed tenant header, which the engine must ignore. */
    private HttpResponse<String> getClaiming(String bearer, String claimedTenant) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/schema/chash-conformance")
            .header("Authorization", "Bearer " + bearer).header("X-Nexus-Tenant", claimedTenant);
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
    void noCredentialIsA401_aMintTokenIsRefusedByTheFilter_andEveryOtherAuthenticatedTokenReads() throws Exception {
        assertThat(get(null).statusCode()).isEqualTo(401);
        assertThat(get("a-token-the-registry-does-not-know").statusCode()).isEqualTo(401);
        // A mint-scoped credential exists to call the data-token mint and nothing else: AuthFilter refuses it on every other
        // path, this one included (unchanged by the route being open to tenants), and the body carries nothing.
        var mint = get(MINT_TOKEN);
        assertThat(mint.statusCode()).isEqualTo(403);
        assertThat(mint.body()).doesNotContain("constraints");
        assertThat(get(TENANT_TOKEN).statusCode()).isEqualTo(200);
        assertThat(get(ROOT).statusCode()).isEqualTo(200);
    }

    // ── what each caller sees ───────────────────────────────────────────────

    @Test
    @Order(12)
    void aTenantTokenGetsExactlyTheSchemaFacts_noPartitionCount_inAnyState() throws Exception {
        for (int state = 0; state < 3; state++) {
            // fresh-like (both NOT VALID), then one valid, then both valid
            if (state == 0) resetBothNotValid();
            if (state == 1) validateChunks();
            if (state == 2) validateManifest();
            var resp = get(TENANT_TOKEN);
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
            var body = MAPPER.readTree(resp.body());
            // the key sets are asserted whole, so a field added later cannot leak to a tenant unnoticed
            assertThat(body.fieldNames()).toIterable().containsExactlyInAnyOrder("constraints", "all_validated");
            assertThat(body.get("constraints")).hasSize(2);
            for (JsonNode c : body.get("constraints")) {
                assertThat(c.fieldNames()).toIterable().containsExactlyInAnyOrder("table", "name", "present", "validated", "definition_matches");
            }
            assertThat(resp.body()).doesNotContain("partitions");
            // and it agrees with what the root token sees of the same facts
            var root = report();
            assertThat(body.get("all_validated")).isEqualTo(root.get("all_validated"));
            for (JsonNode c : body.get("constraints")) {
                var r = row(root, c.get("table").asText());
                assertThat(c.get("validated")).isEqualTo(r.get("validated"));
                assertThat(c.get("present")).isEqualTo(r.get("present"));
                assertThat(c.get("name")).isEqualTo(r.get("name"));
            }
        }
    }

    @Test
    @Order(13)
    void theRootTokenKeepsThePartitionCounts_andTheyAreNotInATenantsBody() throws Exception {
        resetBothNotValid();
        var root = report();
        for (JsonNode c : root.get("constraints")) {
            assertThat(c.fieldNames()).toIterable()
                .containsExactlyInAnyOrder("table", "name", "present", "validated", "definition_matches", "partitions", "partitions_not_validated");
        }
        assertThat(row(root, "nexus.chunks").get("partitions").asInt()).isPositive();
        assertThat(get(TENANT_TOKEN).body()).doesNotContain("partitions");
    }

    @Test
    @Order(14)
    void aTenantsBodyIsByteIdenticalForTwoDifferentTenants_soItCarriesNothingTenantSpecific() throws Exception {
        for (int state = 0; state < 2; state++) {
            if (state == 0) resetBothNotValid(); else validateBoth();
            var first = get(TENANT_TOKEN);
            var second = get(OTHER_TENANT_TOKEN);
            assertThat(first.statusCode()).isEqualTo(200);
            assertThat(second.statusCode()).isEqualTo(200);
            assertThat(first.body()).isEqualTo(second.body());
        }
    }

    @Test
    @Order(15)
    void aDataTokenTheManagedDoctorsRealCredentialGetsExactlyTheTenantKeySets() throws Exception {
        resetBothNotValid();
        var resp = get(DATA_TOKEN);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        var body = MAPPER.readTree(resp.body());
        assertThat(body.fieldNames()).toIterable().containsExactlyInAnyOrder("constraints", "all_validated");
        assertThat(body.get("constraints")).hasSize(2);
        for (JsonNode c : body.get("constraints")) {
            assertThat(c.fieldNames()).toIterable().containsExactlyInAnyOrder("table", "name", "present", "validated", "definition_matches");
        }
        assertThat(resp.body()).as("the same bytes a tenant token gets").isEqualTo(get(TENANT_TOKEN).body());
    }

    @Test
    @Order(16)
    void mintLockedAndBoardCiScopesAreRefusedOnThisPath_withNoBody() throws Exception {
        for (String token : new String[]{MINT_LOCKED_TOKEN, BOARD_CI_TOKEN}) {
            var resp = get(token);
            assertThat(resp.statusCode()).isEqualTo(403);
            assertThat(resp.body()).doesNotContain("constraints").doesNotContain("partitions");
        }
    }

    @Test
    @Order(17)
    void aTenantTokenClaimingAnotherTenantByHeaderStillGetsNoPartitionCounts_andTheSameBody() throws Exception {
        var plain = get(TENANT_TOKEN);
        assertThat(plain.statusCode()).isEqualTo(200);
        for (String claimed : new String[]{TenantConstants.DEFAULT_TENANT, "chash-conf-other-tenant", "*"}) {
            var resp = getClaiming(TENANT_TOKEN, claimed);
            assertThat(resp.statusCode()).as("claiming " + claimed + ": " + resp.body()).isEqualTo(200);
            assertThat(resp.body()).as("claiming " + claimed).doesNotContain("partitions").isEqualTo(plain.body());
        }
        // and a data token the same
        assertThat(getClaiming(DATA_TOKEN, TenantConstants.DEFAULT_TENANT).body()).isEqualTo(plain.body());
    }

    // ── a name is not a definition ──────────────────────────────────────────

    /**
     * The expected definition is what PostgreSQL itself renders, not a literal trusted on faith: the migrations' constraint
     * on a fresh database (the table's own row and every partition copy, NOT VALID or valid) and a constraint made from the
     * same expression on a scratch table by the jOOQ DDL both deparse to {@link ChashConformanceConstraints#WIDTH_CHECK_DEFINITION}.
     */
    @Test
    @Order(18)
    void theExpectedDefinitionIsWhatPostgresqlRendersForTheMigrationsCheck() throws Exception {
        resetBothNotValid();
        var scratch = DSL.name("nexus", "scratch_width_f3yqd");
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int pass = 0; pass < 2; pass++) {
                if (pass == 1) validateBoth();
                var rows = ChashConformanceConstraints.query(ctx).fetch();
                var genuine = rows.stream().filter(r -> !r.value3().equals("chunks_retired_225")).toList();
                assertThat(genuine).as("the migrations' constraints and copies, pass " + pass).hasSizeGreaterThan(10);
                for (var r : genuine) {
                    String def = r.value6().replace(" NOT VALID", "");
                    assertThat(def).as(r.value3() + " pass " + pass).isEqualTo(ChashConformanceConstraints.WIDTH_CHECK_DEFINITION);
                }
            }
            var chash = DSL.field(DSL.name("chash"), byte[].class);
            ctx.createTable(scratch).column(chash).constraint(DSL.constraint("scratch_width_check")
                .check(DSL.function("octet_length", org.jooq.impl.SQLDataType.INTEGER, chash).eq(32))).execute();
            try {
                String rendered = ctx.select(DSL.function("pg_get_constraintdef", org.jooq.impl.SQLDataType.VARCHAR,
                        DSL.field(DSL.name("con", "oid"))))
                    .from(DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("con"))
                    .where(DSL.field(DSL.name("con", "conname"), String.class).eq("scratch_width_check"))
                    .fetchOne(0, String.class);
                assertThat(rendered).isEqualTo(ChashConformanceConstraints.WIDTH_CHECK_DEFINITION);
            } finally {
                ctx.dropTable(scratch).execute();
            }
        }
        resetBothNotValid();
    }

    /** Drops the genuine constraint of {@code table} and plants a WEAKER one of the same name (it is valid: the tables are empty). */
    private void plantWeaker(org.jooq.Table<?> table, String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            var chash = DSL.field(DSL.name("chash"), byte[].class);
            ctx.alterTable(table).dropConstraint(name).execute();
            ctx.alterTable(table).add(DSL.constraint(name)
                .check(DSL.function("octet_length", org.jooq.impl.SQLDataType.INTEGER, chash).ge(1))).execute();
        }
    }

    @Test
    @Order(19)
    void aWeakerConstraintOfTheSameName_evenValid_readsPresentNotValidatedAndNotTheCheck() throws Exception {
        resetBothNotValid();
        validateBoth();
        plantWeaker(CATALOG_DOCUMENT_CHUNKS, MANIFEST_CHECK);
        try {
            var report = report();
            var manifest = row(report, "nexus.catalog_document_chunks");
            assertThat(manifest.get("present").asBoolean()).isTrue();
            assertThat(manifest.get("definition_matches").asBoolean()).isFalse();
            assertThat(manifest.get("validated").asBoolean()).as("a weaker same-named check is not the check").isFalse();
            var chunks = row(report, "nexus.chunks");
            assertThat(chunks.get("definition_matches").asBoolean()).as("the control: the genuine one").isTrue();
            assertThat(chunks.get("validated").asBoolean()).isTrue();
            assertThat(report.get("all_validated").asBoolean()).isFalse();

            // a tenant sees the same facts, and never the definition text
            var tenant = get(TENANT_TOKEN);
            assertThat(tenant.statusCode()).isEqualTo(200);
            var body = MAPPER.readTree(tenant.body());
            assertThat(row(body, "nexus.catalog_document_chunks").get("definition_matches").asBoolean()).isFalse();
            assertThat(row(body, "nexus.catalog_document_chunks").get("validated").asBoolean()).isFalse();
            assertThat(body.get("all_validated").asBoolean()).isFalse();
            assertThat(tenant.body()).doesNotContain("octet_length").doesNotContain("CHECK").doesNotContain(">=");
            assertThat(get(ROOT).body()).doesNotContain("octet_length").doesNotContain("CHECK");
        } finally {
            resetBothNotValid();
        }
    }

    @Test
    @Order(20)
    void aWeakerConstraintOnThePartitionedParent_reachesEveryCopy_andReadsNotTheCheck() throws Exception {
        resetBothNotValid();
        validateBoth();
        plantWeaker(CHUNKS, CHUNKS_CHECK);
        try {
            var chunks = row(report(), "nexus.chunks");
            assertThat(chunks.get("present").asBoolean()).isTrue();
            assertThat(chunks.get("partitions").asInt()).as("the weaker constraint reached the copies").isPositive();
            assertThat(chunks.get("partitions_not_validated").asInt()).isZero();
            assertThat(chunks.get("definition_matches").asBoolean()).isFalse();
            assertThat(chunks.get("validated").asBoolean()).isFalse();
        } finally {
            resetBothNotValid();
        }
    }

    @Test
    @Order(9)
    void onlyGetIsAllowed() throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/schema/chash-conformance")
            .header("Authorization", "Bearer " + ROOT).POST(HttpRequest.BodyPublishers.ofString("{}")).build();
        assertThat(http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(405);
    }

    @Test
    @Order(10)
    void theReadIsOneCatalogStatement_everyRelationItNamesIsInPgCatalog() {
        var ctx = DSL.using(SQLDialect.POSTGRES);
        String sql = ctx.renderInlined(ChashConformanceConstraints.query(ctx));
        // Every relation after FROM or JOIN, schema-qualified as jOOQ renders it: "schema"."name".
        var relation = java.util.regex.Pattern.compile("(?i)\\b(?:from|join)\\s+(\"[^\"]+\"|\\w+)\\s*\\.\\s*(\"[^\"]+\"|\\w+)");
        var m = relation.matcher(sql);
        var seen = new java.util.ArrayList<String>();
        while (m.find()) {
            seen.add(m.group(1) + "." + m.group(2));
            assertThat(m.group(1)).as("relation " + m.group(0) + " in:\n" + sql).isEqualTo("\"pg_catalog\"");
        }
        assertThat(seen).as("the relations found (a scan that found none proved nothing) in:\n" + sql)
            .containsExactlyInAnyOrder("\"pg_catalog\".\"pg_constraint\"", "\"pg_catalog\".\"pg_class\"",
                "\"pg_catalog\".\"pg_namespace\"", "\"pg_catalog\".\"pg_inherits\"");
        assertThat(sql.toLowerCase()).doesNotContain("tenant_id");
    }

    /**
     * The fold must not hide a real copy: the copies it counts for nexus.chunks equal the descendants {@code pg_inherits}
     * holds below it, at every depth, on the real partition tree (a copy whose chain the fold could not reach would be left
     * out of the count and, if it were not valid, out of {@code validated}).
     */
    @Test
    @Order(11)
    void theCopiesTheFoldCounts_equalTheDescendantsPgInheritsHolds() throws Exception {
        resetBothNotValid();
        int descendants;
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            var inhrelid = DSL.field(DSL.name("inhrelid")).cast(org.jooq.impl.SQLDataType.BIGINT);
            var inhparent = DSL.field(DSL.name("inhparent")).cast(org.jooq.impl.SQLDataType.BIGINT);
            var children = new java.util.HashMap<Long, java.util.List<Long>>();
            for (var r : ctx.select(inhparent, inhrelid).from(DSL.table(DSL.name("pg_catalog", "pg_inherits"))).fetch()) {
                children.computeIfAbsent(r.value1(), k -> new java.util.ArrayList<>()).add(r.value2());
            }
            Long chunksOid = ctx.select(DSL.field(DSL.name("c", "oid")).cast(org.jooq.impl.SQLDataType.BIGINT))
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("c", "relname"), String.class).eq("chunks")).fetchOne(0, Long.class);
            var stack = new java.util.ArrayDeque<>(children.getOrDefault(chunksOid, java.util.List.of()));
            int count = 0;
            while (!stack.isEmpty()) {
                count++;
                stack.addAll(children.getOrDefault(stack.pop(), java.util.List.of()));
            }
            descendants = count;
        }
        var chunks = row(report(), "nexus.chunks");
        assertThat(descendants).as("the partition tree has descendants (a tree of none proves nothing)").isGreaterThan(4);
        assertThat(chunks.get("partitions").asInt()).isEqualTo(descendants);
        assertThat(chunks.get("partitions_not_validated").asInt()).isEqualTo(descendants);
        assertThat(row(report(), "nexus.catalog_document_chunks").get("partitions").asInt()).isZero();
    }
}
