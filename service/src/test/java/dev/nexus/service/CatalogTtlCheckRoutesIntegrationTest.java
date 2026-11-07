// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.CatalogTtlCheckAdmin;
import dev.nexus.service.db.TenantConstants;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;

import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * nexus-6pfgb.44: the catalog TTL CHECK's three routes ({@code census}, {@code add}, {@code validate} under
 * {@code /v1/admin/catalog-ttl-check}) through the real service and a real PostgreSQL, in the order an operator runs
 * them. One container, so the steps are ordered. The predicate and the live constraint's behaviour on every shape are
 * {@code CatalogTtlCheckAdminIntegrationTest}'s; this class is the routes' decisions: which tenant they refuse for,
 * that they count every tenant, and that they are idempotent.
 *
 * <p>Four tenants. {@code tt-a} holds two entries with a TTL and no {@code put_at} (one an integer TTL, one the digit
 * string {@code "7"}) and a clean one; {@code tt-b} holds only clean entries; {@code tt-ghost} holds a violator and NO
 * {@code service_tokens} row (the tenant a token-list walk never sees); {@code tt-tomb} holds a violator that was
 * tombstoned while no CHECK existed.
 */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogTtlCheckRoutesIntegrationTest extends ChunkKeysTestBase {

    private static final String A = "tt-a";
    private static final String B = "tt-b";
    private static final String GHOST = "tt-ghost";
    private static final String TOMB = "tt-tomb";
    private static final String BASE = "/v1/admin/catalog-ttl-check";
    private static final String STAMPED = "2026-01-01T00:00:00Z";
    private static final String CHECK_VIOLATION = "23514";

    private final AtomicInteger seq = new AtomicInteger();
    private String tokenA;
    private final List<String> violators = new ArrayList<>();

    private CatalogTtlCheckAdmin admin() {
        return new CatalogTtlCheckAdmin(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()));
    }

    private String doc(String tenant, String metaJson) throws Exception {
        String tumbler = "tt." + seq.incrementAndGet();
        String collection = conformant(tenant);   // physical_collection is NOT NULL and names a registered collection
        register(tenant, collection);
        document(tenant, tumbler, "knowledge", collection, metaJson);
        return tumbler;
    }

    @BeforeAll
    void seedTenantsAndBind() throws Exception {
        service.catalogTtlCheckAdmin(admin());
        tokenA = tenantToken(A);
        tenantToken(B);
        tenantToken(TOMB);
        violators.add(doc(A, "{\"ttl_days\":7}"));
        violators.add(doc(A, "{\"ttl_days\":\"7\"}"));
        doc(A, "{\"ttl_days\":0,\"put_at\":\"\"}");
        doc(B, "{\"ttl_days\":7,\"put_at\":\"" + STAMPED + "\"}");
        doc(B, "{}");
        violators.add(doc(GHOST, "{\"ttl_days\":30}"));
        String tomb = doc(TOMB, "{\"ttl_days\":7}");
        // no CHECK exists yet: an entry with a TTL and no put_at is tombstoned like any other (the expire path)
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertThat(DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.parse(STAMPED))
                .where(CATALOG_DOCUMENTS.TUMBLER.eq(tomb)).execute()).as("fixture: the tombstone landed").isEqualTo(1);
        }
        violators.add(tomb);
    }

    private HttpResponse<String> census(String bearer) throws Exception {
        return post(BASE + "/census", bearer, Map.of());
    }

    private HttpResponse<String> add(String bearer) throws Exception {
        return post(BASE + "/add", bearer, Map.of());
    }

    private HttpResponse<String> validate(String bearer) throws Exception {
        return post(BASE + "/validate", bearer, Map.of());
    }

    private static JsonNode row(JsonNode body, String tenant) {
        for (JsonNode t : body.path("tenants")) {
            if (tenant.equals(t.path("tenant").asText())) {
                return t;
            }
        }
        throw new AssertionError("tenant " + tenant + " is not named in " + body);
    }

    private static List<String> tenantNames(JsonNode body) {
        List<String> out = new ArrayList<>();
        body.path("tenants").forEach(t -> out.add(t.path("tenant").asText()));
        return out;
    }

    private void stampAll() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String tumbler : violators) {
                ctx.update(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.METADATA, JSONB.jsonb("{\"ttl_days\":7,\"put_at\":\"" + STAMPED + "\"}"))
                    .where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)).execute();
            }
        }
    }

    // ---- 0. the census --------------------------------------------------------------------------------------

    @Test
    @Order(0)
    void theCensusIsPerTenant_countsATenantWithNoTokenRow_andAddsNothing() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchCount(SERVICE_TOKENS, SERVICE_TOKENS.TENANT_ID.eq(GHOST)))
                .as("fixture: no token row, so a token-list walk cannot see this tenant").isZero();
        }

        HttpResponse<String> r = census(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(tenantNames(body)).containsExactlyInAnyOrder(A, B, GHOST, TOMB);
        assertThat(row(body, A).path("rows").asLong()).isEqualTo(3);
        assertThat(row(body, A).path("ttl_rows").asLong()).as("both TTL forms count, the retraction value does not")
            .isEqualTo(2);
        assertThat(row(body, A).path("violating_rows").asLong()).isEqualTo(2);
        assertThat(row(body, B).path("ttl_rows").asLong()).isEqualTo(1);
        assertThat(row(body, B).path("violating_rows").asLong()).isZero();
        assertThat(row(body, GHOST).path("violating_rows").asLong()).as("a tenant with no token row is counted").isEqualTo(1);
        assertThat(row(body, TOMB).path("violating_rows").asLong()).isEqualTo(1);
        assertThat(row(body, TOMB).path("violating_tombstoned_rows").asLong()).isEqualTo(1);
        assertThat(body.path("violating_total").asLong()).isEqualTo(4);
        assertThat(body.path("table_rows_examined").asLong()).isEqualTo(7);
        List<String> checked = new ArrayList<>();
        body.path("tenants_checked").forEach(t -> checked.add(t.asText()));
        assertThat(checked).contains(TenantConstants.DEFAULT_TENANT, A, B, GHOST, TOMB);
        assertThat(body.path("constraint").path("present").asBoolean()).isFalse();
        assertThat(admin().state().present()).as("a census changes nothing").isFalse();
    }

    // ---- 1. add and validate refuse, naming the tenant ---------------------------------------------------------

    @Test
    @Order(10)
    void addIsRefusedWhileAnyTenantHoldsAViolator_namingEach_andAddsNothing() throws Exception {
        HttpResponse<String> r = add(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        JsonNode body = json(r);
        assertThat(body.path("reason").asText()).isEqualTo("tenant_violates_constraint");
        assertThat(tenantNames(body)).as("the tenants with a violator, the tombstoned one and the tokenless one "
            + "included; not the clean one").containsExactlyInAnyOrder(A, GHOST, TOMB);
        assertThat(row(body, A).path("violating_rows").asLong()).isEqualTo(2);
        assertThat(body.path("error").asText()).contains(A, GHOST, TOMB).doesNotContain(B);
        assertThat(admin().state().present()).isFalse();
    }

    @Test
    @Order(11)
    void validateOfAnAbsentConstraintIsReported_withoutACountOrACreate() throws Exception {
        HttpResponse<String> r = validate(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(json(r).path("constraint").path("action").asText()).isEqualTo("absent");
        assertThat(json(r).has("census_skipped")).isTrue();
        assertThat(admin().state().present()).isFalse();
    }

    // ---- 2. access --------------------------------------------------------------------------------------------

    @Test
    @Order(20)
    void theRoutesNeedTheRootToken_andPostOnly_andKnowAnUnboundEngine() throws Exception {
        for (var call : List.of("/census", "/add", "/validate")) {
            assertThat(post(BASE + call, tokenA, Map.of()).statusCode()).as("tenant token " + call).isEqualTo(403);
            assertThat(post(BASE + call, null, Map.of()).statusCode()).as("no token " + call).isEqualTo(401);
            assertThat(get(BASE + call, ROOT).statusCode()).as("GET " + call).isEqualTo(405);
        }
        assertThat(post(BASE + "/nope", ROOT, Map.of()).statusCode()).isEqualTo(404);
        service.catalogTtlCheckAdmin(null);
        try {
            assertThat(census(ROOT).statusCode()).as("no admin connection bound").isEqualTo(503);
        } finally {
            service.catalogTtlCheckAdmin(admin());
        }
    }

    // ---- 3. the count must be every tenant's --------------------------------------------------------------------

    private static final String BLIND = "tt_blind_admin";
    private static final String BLIND_PASS = "ttblindadminpw";

    private CatalogTtlCheckAdmin blindAdmin() {
        return new CatalogTtlCheckAdmin(new AdminConnection(pg.getJdbcUrl(), BLIND, BLIND_PASS));
    }

    private void createBlindRole() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.createLoginRole(su, BLIND, BLIND_PASS, false);
            PgContainerHelper.grantSchemaUsage(su, "nexus", BLIND);
            DSL.using(su, SQLDialect.POSTGRES).grant(DSL.privilege("SELECT")).on(CATALOG_DOCUMENTS).to(DSL.role(BLIND))
                .execute();
        }
    }

    /** An admin whose census drops {@code dropTenant} entirely, or one row of {@code shortTenant}. */
    private CatalogTtlCheckAdmin lyingAdmin(String dropTenant, String shortTenant) {
        return new CatalogTtlCheckAdmin(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword())) {
            @Override
            public List<TenantCensus> census() {
                return super.census().stream().filter(t -> !t.tenant().equals(dropTenant))
                    .map(t -> t.tenant().equals(shortTenant)
                        ? new TenantCensus(t.tenant(), t.rows() - 1, t.ttlRows(), t.violating(), t.violatingTombstoned())
                        : t).toList();
            }
        };
    }

    @Test
    @Order(30)
    void anAdminRoleThatCannotSeeEveryRowIsRefused_notTrustedToReadZero() throws Exception {
        createBlindRole();
        try (Connection blind = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), BLIND, BLIND_PASS)) {
            assertThat(DSL.using(blind, SQLDialect.POSTGRES).fetchCount(CATALOG_DOCUMENTS))
                .as("fixture: under FORCE RLS this role reads ZERO entries, which an unchecked census takes for clean")
                .isZero();
        }
        service.catalogTtlCheckAdmin(blindAdmin());
        try {
            for (HttpResponse<String> r : List.of(census(ROOT), add(ROOT))) {
                assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
                assertThat(json(r).path("reason").asText()).isEqualTo("admin_cannot_see_all_tenants");
                assertThat(json(r).path("error").asText()).contains(BLIND);
            }
            assertThat(admin().state().present()).as("nothing was added").isFalse();
        } finally {
            service.catalogTtlCheckAdmin(admin());
        }
    }

    @Test
    @Order(31)
    void aCensusBelowATenantScopedCountIsRefused_evenWhenTheRoleLooksUnrestricted() throws Exception {
        for (var lying : List.of(lyingAdmin(B, null), lyingAdmin(null, A))) {
            service.catalogTtlCheckAdmin(lying);
            try {
                for (HttpResponse<String> r : List.of(census(ROOT), add(ROOT))) {
                    assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
                    assertThat(json(r).path("reason").asText()).isEqualTo("admin_probe_undercount");
                }
            } finally {
                service.catalogTtlCheckAdmin(admin());
            }
        }
        assertThat(admin().state().present()).as("nothing was added").isFalse();
    }

    // ---- 4. the order an operator runs: stamp, add, validate -----------------------------------------------------

    @Test
    @Order(40)
    void afterTheBackfillTheCensusReadsZero_addPutsTheCheckOnNotValid_andARerunIsPresent() throws Exception {
        stampAll();

        JsonNode zero = json(census(ROOT));
        assertThat(zero.path("violating_total").asLong()).isZero();
        HttpResponse<String> first = add(ROOT);
        HttpResponse<String> second = add(ROOT);

        assertThat(first.statusCode()).as(first.body()).isEqualTo(200);
        JsonNode c = json(first).path("constraint");
        assertThat(c.path("action").asText()).isEqualTo("added");
        assertThat(c.path("validated").asBoolean()).as("NOT VALID").isFalse();
        assertThat(c.path("definition").asText()).contains("jsonb_typeof").endsWith("NOT VALID");
        assertThat(json(second).path("constraint").path("action").asText()).isEqualTo("present");
        assertThat(json(first).path("tenants_checked").size()).isGreaterThanOrEqualTo(5);
    }

    @Test
    @Order(41)
    void aNewEntryShapedLikeTheViolatorsIsRefusedEvenBeforeValidate() throws Exception {
        assertThatThrownBy(() -> doc(A, "{\"ttl_days\":7}")).isInstanceOf(DataAccessException.class)
            .satisfies(e -> assertThat(((DataAccessException) e).sqlState()).isEqualTo(CHECK_VIOLATION));
    }

    @Test
    @Order(50)
    void validateIsRefusedWhileATenantHoldsAViolator_namingEach_andLeavesTheCheckNotValid() throws Exception {
        // A constraint an operator added by hand over entries that break it (the add route would have refused):
        // drop ours, write the violators while nothing checks, and put the same CHECK back through the admin class.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).alterTable(CATALOG_DOCUMENTS)
                .dropConstraint(DSL.name(CatalogTtlCheckAdmin.CONSTRAINT)).execute();
        }
        violators.clear();
        violators.add(doc(A, "{\"ttl_days\":7}"));
        violators.add(doc(GHOST, "{\"ttl_days\":\"30\"}"));
        assertThat(admin().add().after().validated()).as("fixture: present and NOT VALID").isFalse();

        HttpResponse<String> r = validate(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        JsonNode body = json(r);
        assertThat(body.path("reason").asText()).as("the count's refusal, not the database's check_violation")
            .isEqualTo("tenant_violates_constraint");
        assertThat(tenantNames(body)).containsExactlyInAnyOrder(A, GHOST);
        assertThat(row(body, A).path("violating_rows").asLong()).isEqualTo(1);
        assertThat(admin().state().validated()).as("still NOT VALID").isFalse();
    }

    @Test
    @Order(51)
    void validateCountsEveryTenantToo_aBlindRoleAndAShortCensusAreRefused() throws Exception {
        service.catalogTtlCheckAdmin(blindAdmin());
        try {
            HttpResponse<String> blind = validate(ROOT);
            assertThat(blind.statusCode()).as(blind.body()).isEqualTo(503);
            assertThat(json(blind).path("reason").asText()).isEqualTo("admin_cannot_see_all_tenants");
        } finally {
            service.catalogTtlCheckAdmin(admin());
        }
        service.catalogTtlCheckAdmin(lyingAdmin(B, null));
        try {
            HttpResponse<String> shortCount = validate(ROOT);
            assertThat(shortCount.statusCode()).as(shortCount.body()).isEqualTo(503);
            assertThat(json(shortCount).path("reason").asText()).isEqualTo("admin_probe_undercount");
        } finally {
            service.catalogTtlCheckAdmin(admin());
        }
        assertThat(admin().state().validated()).isFalse();
    }

    @Test
    @Order(60)
    void validatePassesOnceTheCountReadsZero_andARerunIsAlreadyValidWithoutACount() throws Exception {
        stampAll();

        HttpResponse<String> first = validate(ROOT);
        HttpResponse<String> second = validate(ROOT);

        assertThat(first.statusCode()).as(first.body()).isEqualTo(200);
        assertThat(json(first).path("constraint").path("action").asText()).isEqualTo("validated");
        assertThat(json(first).path("constraint").path("validated").asBoolean()).isTrue();
        assertThat(json(first).path("violating_total").asLong()).isZero();
        assertThat(json(second).path("constraint").path("action").asText()).isEqualTo("already_valid");
        assertThat(json(second).path("census_skipped").asText()).as("a VALID check has no violator to count").isNotEmpty();
        assertThat(json(second).has("tenants")).isFalse();
    }

    @Test
    @Order(61)
    void afterValidateTheDatabaseRefusesTtl7WithNoPutAt_andAcceptsTheRest() throws Exception {
        assertThatThrownBy(() -> doc(B, "{\"ttl_days\":7}")).isInstanceOf(DataAccessException.class)
            .satisfies(e -> assertThat(((DataAccessException) e).sqlState()).isEqualTo(CHECK_VIOLATION));
        doc(B, "{\"ttl_days\":0,\"put_at\":\"\"}");
        doc(B, "{}");
        doc(B, "{\"ttl_days\":\"abc\"}");
        doc(B, "{\"ttl_days\":true}");
        doc(B, "{\"ttl_days\":1e400}");
        doc(B, "{\"ttl_days\":null}");
        doc(B, "{\"ttl_days\":{\"a\":1}}");
        assertThat(json(census(ROOT)).path("violating_total").asLong()).isZero();
    }
}
