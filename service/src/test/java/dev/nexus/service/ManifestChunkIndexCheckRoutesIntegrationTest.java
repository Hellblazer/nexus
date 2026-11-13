// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.db.ManifestChunkIndexCheckAdmin;
import dev.nexus.service.db.TenantConstants;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;

import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/**
 * nexus-6pfgb.42: the manifest chunk-index CHECK's three routes ({@code census}, {@code add}, {@code validate} under
 * {@code /v1/admin/manifest-chunk-index-check}) through the real service and a real PostgreSQL, in the order an
 * operator runs them. One container, so the steps are ordered. The predicate, the live constraint and the run guards
 * are {@code ManifestChunkIndexCheckAdminIntegrationTest}'s; this class is the routes' decisions: which tenant they
 * refuse for, that they count every tenant, and that they are idempotent.
 *
 * <p>Three tenants. {@code mc-a} holds two clean manifest rows and two whose {@code chunk_index} differs from their
 * {@code position}; {@code mc-b} holds only clean rows; {@code mc-ghost} holds a violator and NO {@code service_tokens}
 * row (the tenant a token-list walk never sees). Rows are written with no chunk behind them and the foreign keys off
 * for the writing session ({@code session_replication_role = replica}).
 */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ManifestChunkIndexCheckRoutesIntegrationTest extends ChunkKeysTestBase {

    private static final String A = "mc-a";
    private static final String B = "mc-b";
    private static final String GHOST = "mc-ghost";
    private static final String BASE = "/v1/admin/manifest-chunk-index-check";
    private static final String CHECK_VIOLATION = "23514";

    private final AtomicInteger seq = new AtomicInteger();
    private String tokenA;

    private ManifestChunkIndexCheckAdmin admin() {
        return new ManifestChunkIndexCheckAdmin(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()));
    }

    /** One manifest row of {@code tenant}, written as the superuser with the foreign keys off. */
    private void row(String tenant, int position, int chunkIndex) throws Exception {
        String doc = "doc-" + seq.incrementAndGet();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant)
                .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
                .set(CATALOG_DOCUMENT_CHUNKS.CHASH,
                    MessageDigest.getInstance("SHA-256").digest(doc.getBytes(StandardCharsets.UTF_8)))
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, "mci-collection")
                .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
                .set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, chunkIndex)
                .execute();
        }
    }

    @BeforeAll
    void seedTenantsAndBind() throws Exception {
        service.manifestChunkIndexCheckAdmin(admin());
        tokenA = tenantToken(A);
        tenantToken(B);
        row(A, 0, 0);
        row(A, 1, 1);
        row(A, 2, 5);    // violator
        row(A, 3, 0);    // violator
        row(B, 0, 0);
        row(B, 7, 7);
        row(GHOST, 4, 4);
        row(GHOST, 5, 1);   // violator
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

    private static JsonNode tenantRow(JsonNode body, String tenant) {
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

    /** The fix an operator applies: chunk_index back to position, on every row. */
    private void fixAll() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, CATALOG_DOCUMENT_CHUNKS.POSITION)
                .where(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX.ne(CATALOG_DOCUMENT_CHUNKS.POSITION)).execute();
        }
    }

    private void dropConstraintIfPresent() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).alterTable(CATALOG_DOCUMENT_CHUNKS)
                .dropConstraintIfExists(DSL.name(ManifestChunkIndexCheckAdmin.CONSTRAINT)).execute();
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
        assertThat(tenantNames(body)).containsExactlyInAnyOrder(A, B, GHOST);
        assertThat(tenantRow(body, A).path("rows").asLong()).isEqualTo(4);
        assertThat(tenantRow(body, A).path("violating_rows").asLong()).isEqualTo(2);
        assertThat(tenantRow(body, B).path("rows").asLong()).isEqualTo(2);
        assertThat(tenantRow(body, B).path("violating_rows").asLong()).isZero();
        assertThat(tenantRow(body, GHOST).path("rows").asLong()).as("a tenant with no token row is counted").isEqualTo(2);
        assertThat(tenantRow(body, GHOST).path("violating_rows").asLong()).isEqualTo(1);
        assertThat(body.path("violating_total").asLong()).isEqualTo(3);
        assertThat(body.path("table_rows_examined").asLong()).isEqualTo(8);
        List<String> checked = new ArrayList<>();
        body.path("tenants_checked").forEach(t -> checked.add(t.asText()));
        assertThat(checked).contains(TenantConstants.DEFAULT_TENANT, A, B, GHOST);
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
        assertThat(tenantNames(body)).as("the tenants with a violator, the tokenless one included; not the clean one")
            .containsExactlyInAnyOrder(A, GHOST);
        assertThat(tenantRow(body, A).path("violating_rows").asLong()).isEqualTo(2);
        assertThat(body.path("error").asText()).contains(A, GHOST).doesNotContain(B);
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
        service.manifestChunkIndexCheckAdmin(null);
        try {
            assertThat(census(ROOT).statusCode()).as("no admin connection bound").isEqualTo(503);
        } finally {
            service.manifestChunkIndexCheckAdmin(admin());
        }
    }

    // ---- 3. the count must be every tenant's --------------------------------------------------------------------

    private static final String BLIND = "mc_blind_admin";
    private static final String BLIND_PASS = "mcblindadminpw";

    private ManifestChunkIndexCheckAdmin blindAdmin() {
        return new ManifestChunkIndexCheckAdmin(new AdminConnection(pg.getJdbcUrl(), BLIND, BLIND_PASS));
    }

    private void createBlindRole() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.createLoginRole(su, BLIND, BLIND_PASS, false);
            PgContainerHelper.grantSchemaUsage(su, "nexus", BLIND);
            DSL.using(su, SQLDialect.POSTGRES).grant(DSL.privilege("SELECT")).on(CATALOG_DOCUMENT_CHUNKS)
                .to(DSL.role(BLIND)).execute();
        }
    }

    /** An admin whose census drops {@code dropTenant} entirely, or one row of {@code shortTenant}. */
    private ManifestChunkIndexCheckAdmin lyingAdmin(String dropTenant, String shortTenant) {
        return new ManifestChunkIndexCheckAdmin(
                new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword())) {
            @Override
            public List<TenantCensus> census() {
                return super.census().stream().filter(t -> !t.tenant().equals(dropTenant))
                    .map(t -> t.tenant().equals(shortTenant)
                        ? new TenantCensus(t.tenant(), t.rows() - 1, t.violating())
                        : t).toList();
            }
        };
    }

    @Test
    @Order(30)
    void anAdminRoleThatCannotSeeEveryRowIsRefused_notTrustedToReadZero() throws Exception {
        createBlindRole();
        try (Connection blind = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), BLIND, BLIND_PASS)) {
            assertThat(DSL.using(blind, SQLDialect.POSTGRES).fetchCount(CATALOG_DOCUMENT_CHUNKS))
                .as("fixture: under FORCE RLS this role reads ZERO rows, which an unchecked census takes for clean")
                .isZero();
        }
        service.manifestChunkIndexCheckAdmin(blindAdmin());
        try {
            for (HttpResponse<String> r : List.of(census(ROOT), add(ROOT))) {
                assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
                assertThat(json(r).path("reason").asText()).isEqualTo("admin_cannot_see_all_tenants");
                assertThat(json(r).path("error").asText()).contains(BLIND);
            }
            assertThat(admin().state().present()).as("nothing was added").isFalse();
        } finally {
            service.manifestChunkIndexCheckAdmin(admin());
        }
    }

    @Test
    @Order(31)
    void aCensusBelowATenantScopedCountIsRefused_evenWhenTheRoleLooksUnrestricted() throws Exception {
        for (var lying : List.of(lyingAdmin(B, null), lyingAdmin(null, A))) {
            service.manifestChunkIndexCheckAdmin(lying);
            try {
                for (HttpResponse<String> r : List.of(census(ROOT), add(ROOT))) {
                    assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
                    assertThat(json(r).path("reason").asText()).isEqualTo("admin_probe_undercount");
                }
            } finally {
                service.manifestChunkIndexCheckAdmin(admin());
            }
        }
        assertThat(admin().state().present()).as("nothing was added").isFalse();
    }

    // ---- 3b. the table's owner is enough: the managed service's nexus_admin ----------------------------------------

    private static final String OWNER = "mc_owner_route";
    private static final String OWNER_PASS = "mcownerroutepw";

    private boolean forceRowSecurity() throws Exception {
        try (Connection su = pg.createConnection("")) {
            return Boolean.TRUE.equals(DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.field(DSL.name("c", "relforcerowsecurity"), Boolean.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("c", "relname"), String.class).eq("catalog_document_chunks"))
                .fetchOne(0, Boolean.class));
        }
    }

    @Test
    @Order(35)
    void theTableOwnerWithoutBypassRlsCountsEveryTenant_theTokenlessOneIncluded_andPutsForceBack() throws Exception {
        String originalOwner;
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            originalOwner = DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.field(DSL.name("tableowner"), String.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_tables")))
                .where(DSL.field(DSL.name("schemaname"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("tablename"), String.class).eq("catalog_document_chunks"))
                .fetchOne(0, String.class);
            PgContainerHelper.createLoginRole(su, OWNER, OWNER_PASS, false);
            PgContainerHelper.grantSchemaUsage(su, "nexus", OWNER);
            PgContainerHelper.grantSchemaCreate(su, "nexus", OWNER);
            PgContainerHelper.alterTableOwner(su, CATALOG_DOCUMENT_CHUNKS, OWNER);
        }
        service.manifestChunkIndexCheckAdmin(
            new ManifestChunkIndexCheckAdmin(new AdminConnection(pg.getJdbcUrl(), OWNER, OWNER_PASS)));
        try {
            try (Connection owner = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), OWNER, OWNER_PASS)) {
                assertThat(DSL.using(owner, SQLDialect.POSTGRES).fetchCount(CATALOG_DOCUMENT_CHUNKS))
                    .as("fixture: FORCE hides every row from the table's owner").isZero();
            }

            HttpResponse<String> counted = census(ROOT);
            HttpResponse<String> refused = add(ROOT);

            assertThat(counted.statusCode()).as(counted.body()).isEqualTo(200);
            JsonNode body = json(counted);
            assertThat(tenantNames(body)).containsExactlyInAnyOrder(A, B, GHOST);
            assertThat(tenantRow(body, GHOST).path("violating_rows").asLong()).as("the tenant with no token row")
                .isEqualTo(1);
            assertThat(body.path("violating_total").asLong()).isEqualTo(3);
            assertThat(body.path("table_rows_examined").asLong()).isEqualTo(8);
            assertThat(refused.statusCode()).as(refused.body()).isEqualTo(409);
            assertThat(json(refused).path("reason").asText()).isEqualTo("tenant_violates_constraint");
            assertThat(tenantNames(json(refused))).containsExactlyInAnyOrder(A, GHOST);
            assertThat(forceRowSecurity()).as("FORCE ROW LEVEL SECURITY is back after both calls").isTrue();
        } finally {
            service.manifestChunkIndexCheckAdmin(admin());
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                PgContainerHelper.alterTableOwner(su, CATALOG_DOCUMENT_CHUNKS, originalOwner);
            }
        }
        assertThat(admin().state().present()).as("nothing was added").isFalse();
    }

    // ---- 4. the order an operator runs: fix, add, validate ------------------------------------------------------

    @Test
    @Order(38)
    void aViolatorWrittenBetweenTheCountAndTheAddIsReportedAfterTheAdd_withTheConstraintInPlace() throws Exception {
        fixAll();
        AtomicReference<Boolean> raced = new AtomicReference<>(false);
        service.manifestChunkIndexCheckAdmin(new ManifestChunkIndexCheckAdmin(
                new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword())) {
            @Override
            public Action add() {
                try {
                    row(A, 90, 91);   // a writer that sets the columns apart, after the count and before the add
                    raced.set(true);
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
                return super.add();
            }
        });
        try {
            HttpResponse<String> r = add(ROOT);

            assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
            assertThat(raced.get()).isTrue();
            JsonNode body = json(r);
            assertThat(body.path("violating_total").asLong()).as("the count before the add read zero").isZero();
            assertThat(body.path("constraint").path("action").asText()).isEqualTo("added");
            assertThat(body.path("violating_after_add").asLong()).as("the count after it names the racer").isEqualTo(1);
            assertThat(body.path("violators_after_add").size()).isEqualTo(1);
            assertThat(body.path("violators_after_add").get(0).path("tenant").asText()).isEqualTo(A);
            assertThat(body.path("warning").asText()).contains("frozen").contains("violators_after_add");
        } finally {
            service.manifestChunkIndexCheckAdmin(admin());
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                ctx.alterTable(CATALOG_DOCUMENT_CHUNKS)
                    .dropConstraintIfExists(DSL.name(ManifestChunkIndexCheckAdmin.CONSTRAINT)).execute();
                ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.POSITION.eq(90)).execute();
            }
        }
    }

    @Test
    @Order(39)
    void aCountAfterTheAddThatCannotBeTakenDoesNotHideTheAdd_whateverTheFailure() throws Exception {
        // a refusal, and a bare database error: the ADD landed either way, so both answer 200 with the constraint in
        // place and violating_after_add unknown
        for (RuntimeException failure : List.of(
                new AdminDdlSession.Refused(503, "admin_probe_undercount", "the second count is refused"),
                new DataAccessException("the second count failed: lock timeout"))) {
            AtomicInteger counts = new AtomicInteger();
            service.manifestChunkIndexCheckAdmin(new ManifestChunkIndexCheckAdmin(
                    new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword())) {
                @Override
                public List<TenantCensus> census() {
                    if (counts.incrementAndGet() > 1) {
                        throw failure;
                    }
                    return super.census();
                }
            });
            try {
                HttpResponse<String> r = add(ROOT);

                assertThat(r.statusCode()).as("the add landed, so the answer is 200, not a 5xx: " + r.body())
                    .isEqualTo(200);
                JsonNode body = json(r);
                assertThat(body.path("constraint").path("action").asText()).isEqualTo("added");
                assertThat(body.has("violating_after_add")).isTrue();
                assertThat(body.path("violating_after_add").isNull()).isTrue();
                assertThat(body.path("warning").asText()).contains("the constraint was added").contains("census");
                if (failure instanceof AdminDdlSession.Refused) {
                    assertThat(body.path("warning").asText()).contains("admin_probe_undercount");
                } else {
                    assertThat(body.path("warning").asText()).contains("engine log");
                }
                assertThat(admin().state().present()).isTrue();
            } finally {
                service.manifestChunkIndexCheckAdmin(admin());
                dropConstraintIfPresent();
            }
        }
    }

    @Test
    @Order(40)
    void afterTheFixTheCensusReadsZero_addPutsTheCheckOnNotValid_andARerunIsPresent() throws Exception {
        fixAll();

        JsonNode zero = json(census(ROOT));
        assertThat(zero.path("violating_total").asLong()).isZero();
        HttpResponse<String> first = add(ROOT);
        HttpResponse<String> second = add(ROOT);

        assertThat(first.statusCode()).as(first.body()).isEqualTo(200);
        JsonNode c = json(first).path("constraint");
        assertThat(c.path("action").asText()).isEqualTo("added");
        assertThat(c.path("validated").asBoolean()).as("NOT VALID").isFalse();
        assertThat(c.path("definition").asText()).contains("chunk_index").endsWith("NOT VALID");
        assertThat(json(second).path("constraint").path("action").asText()).isEqualTo("present");
        assertThat(json(first).path("tenants_checked").size()).isGreaterThanOrEqualTo(4);
        assertThat(json(first).path("violating_after_add").asLong()).as("the count after the add: nothing raced").isZero();
        assertThat(json(first).path("violators_after_add").size()).isZero();
        assertThat(json(first).has("warning")).isFalse();
    }

    @Test
    @Order(41)
    void aNewRowShapedLikeTheViolatorsIsRefusedEvenBeforeValidate() {
        assertThatThrownBy(() -> row(A, 50, 51)).isInstanceOf(DataAccessException.class)
            .satisfies(e -> assertThat(((DataAccessException) e).sqlState()).isEqualTo(CHECK_VIOLATION));
    }

    @Test
    @Order(42)
    void addOnAnExistingConstraintAnswersPresentWithoutASurvey_evenForARoleThatCannotCountOrACountThatIsShort()
            throws Exception {
        for (ManifestChunkIndexCheckAdmin bound : List.of(blindAdmin(), lyingAdmin(B, null))) {
            service.manifestChunkIndexCheckAdmin(bound);
            try {
                HttpResponse<String> r = add(ROOT);

                assertThat(r.statusCode()).as("a survey would be a 503 here: " + r.body()).isEqualTo(200);
                assertThat(json(r).path("constraint").path("action").asText()).isEqualTo("present");
                assertThat(json(r).path("census_skipped").asText()).isNotEmpty();
                assertThat(json(r).has("tenants")).as("no count was taken").isFalse();
            } finally {
                service.manifestChunkIndexCheckAdmin(admin());
            }
        }
    }

    @Test
    @Order(50)
    void validateIsRefusedWhileATenantHoldsAViolator_namingEach_andLeavesTheCheckNotValid() throws Exception {
        // A constraint an operator added by hand over rows that break it (the add route would have refused): drop
        // ours, write the violators while nothing checks, and put the same CHECK back through the admin class.
        dropConstraintIfPresent();
        row(A, 60, 61);
        row(GHOST, 62, 63);
        assertThat(admin().add().after().validated()).as("fixture: present and NOT VALID").isFalse();

        HttpResponse<String> r = validate(ROOT);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        JsonNode body = json(r);
        assertThat(body.path("reason").asText()).as("the count's refusal, not the database's check_violation")
            .isEqualTo("tenant_violates_constraint");
        assertThat(tenantNames(body)).containsExactlyInAnyOrder(A, GHOST);
        assertThat(tenantRow(body, A).path("violating_rows").asLong()).isEqualTo(1);
        assertThat(admin().state().validated()).as("still NOT VALID").isFalse();
    }

    @Test
    @Order(51)
    void validateCountsEveryTenantToo_aBlindRoleAndAShortCensusAreRefused() throws Exception {
        service.manifestChunkIndexCheckAdmin(blindAdmin());
        try {
            HttpResponse<String> blind = validate(ROOT);
            assertThat(blind.statusCode()).as(blind.body()).isEqualTo(503);
            assertThat(json(blind).path("reason").asText()).isEqualTo("admin_cannot_see_all_tenants");
        } finally {
            service.manifestChunkIndexCheckAdmin(admin());
        }
        service.manifestChunkIndexCheckAdmin(lyingAdmin(B, null));
        try {
            HttpResponse<String> shortCount = validate(ROOT);
            assertThat(shortCount.statusCode()).as(shortCount.body()).isEqualTo(503);
            assertThat(json(shortCount).path("reason").asText()).isEqualTo("admin_probe_undercount");
        } finally {
            service.manifestChunkIndexCheckAdmin(admin());
        }
        assertThat(admin().state().validated()).isFalse();
    }

    @Test
    @Order(60)
    void validatePassesOnceTheCountReadsZero_andARerunIsAlreadyValidWithoutACount() throws Exception {
        fixAll();

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
    void afterValidateTheDatabaseRefusesAViolatingRow_andAcceptsTheEquality() throws Exception {
        assertThatThrownBy(() -> row(B, 70, 71)).isInstanceOf(DataAccessException.class)
            .satisfies(e -> assertThat(((DataAccessException) e).sqlState()).isEqualTo(CHECK_VIOLATION));
        row(B, 72, 72);
        assertThat(json(census(ROOT)).path("violating_total").asLong()).isZero();
    }
}
