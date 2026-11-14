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
import dev.nexus.service.db.ManifestCheck;
import dev.nexus.service.db.ManifestCheckAdmin;
import dev.nexus.service.db.TenantConstants;
import org.jooq.DSLContext;
import org.jooq.Field;
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
 * nexus-6pfgb.42: one manifest CHECK's three routes ({@code census}, {@code add}, {@code validate} under
 * {@code ManifestCheck#path()}) through the real service and a real PostgreSQL, in the order an operator runs them. One
 * container per concrete class, so the steps are ordered. The predicate, the live constraint and the run guards are
 * {@code ManifestCheckAdminIntegrationBase}'s; this class is the routes' decisions: which tenant they refuse for, that
 * they count every tenant, and that they are idempotent. A subclass says which check it is and how a clean row, a
 * violating row and the operator's fix look for that check.
 *
 * <p>Three tenants. {@code mc-a} holds two clean manifest rows and two violators; {@code mc-b} holds only clean rows;
 * {@code mc-ghost} holds a violator and NO {@code service_tokens} row (the tenant a token-list walk never sees). Rows
 * are written with no chunk behind them and the foreign keys off for the writing session
 * ({@code session_replication_role = replica}).
 */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
abstract class ManifestCheckRoutesIntegrationBase extends ChunkKeysTestBase {

    private static final String A = "mc-a";
    private static final String B = "mc-b";
    private static final String GHOST = "mc-ghost";
    private static final String CHECK_VIOLATION = "23514";

    private final AtomicInteger seq = new AtomicInteger();
    private String tokenA;

    /** The check under test. */
    abstract ManifestCheck check();

    /** A word the stored constraint definition must contain. */
    abstract String definitionWord();

    /** The column assignments of a row that satisfies the CHECK, for the manifest row {@code position}. */
    abstract Map<Field<?>, Object> cleanColumns(int position);

    /** The column assignments of a row the CHECK refuses, for the manifest row {@code position}. */
    abstract Map<Field<?>, Object> violatingColumns(int position);

    /** The fix an operator applies: every violator made clean. */
    abstract void fixEveryViolator(DSLContext ctx);

    private String base() {
        return check().path();
    }

    private ManifestCheckAdmin admin() {
        return new ManifestCheckAdmin(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()), check());
    }

    private void bind(ManifestCheckAdmin admin) {
        service.manifestChecks().admin(check(), admin);
    }

    /** One manifest row of {@code tenant}, written as the superuser with the foreign keys off. */
    private void row(String tenant, int position, boolean violating) throws Exception {
        String doc = "doc-" + seq.incrementAndGet();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            var insert = ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant)
                .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
                .set(CATALOG_DOCUMENT_CHUNKS.CHASH,
                    MessageDigest.getInstance("SHA-256").digest(doc.getBytes(StandardCharsets.UTF_8)))
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, "mci-collection")
                .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
                // NOT NULL, and the BEFORE INSERT default trigger is off under replica: a check that is not about
                // chunk_index leaves it equal to position, one that is overrides it below
                .set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, position);
            for (var column : (violating ? violatingColumns(position) : cleanColumns(position)).entrySet()) {
                @SuppressWarnings("unchecked")
                Field<Object> f = (Field<Object>) column.getKey();
                insert.set(f, column.getValue());
            }
            insert.execute();
        }
    }

    @BeforeAll
    void seedTenantsAndBind() throws Exception {
        bind(admin());
        tokenA = tenantToken(A);
        tenantToken(B);
        row(A, 0, false);
        row(A, 1, false);
        row(A, 2, true);
        row(A, 3, true);
        row(B, 0, false);
        row(B, 7, false);
        row(GHOST, 4, false);
        row(GHOST, 5, true);
    }

    private HttpResponse<String> census(String bearer) throws Exception {
        return post(base() + "/census", bearer, Map.of());
    }

    private HttpResponse<String> add(String bearer) throws Exception {
        return post(base() + "/add", bearer, Map.of());
    }

    private HttpResponse<String> validate(String bearer) throws Exception {
        return post(base() + "/validate", bearer, Map.of());
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

    private void fixAll() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            fixEveryViolator(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    private void dropConstraintIfPresent() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).alterTable(CATALOG_DOCUMENT_CHUNKS)
                .dropConstraintIfExists(DSL.name(check().constraint())).execute();
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
            assertThat(post(base() + call, tokenA, Map.of()).statusCode()).as("tenant token " + call).isEqualTo(403);
            assertThat(post(base() + call, null, Map.of()).statusCode()).as("no token " + call).isEqualTo(401);
            assertThat(get(base() + call, ROOT).statusCode()).as("GET " + call).isEqualTo(405);
        }
        assertThat(post(base() + "/nope", ROOT, Map.of()).statusCode()).isEqualTo(404);
        bind(null);
        try {
            assertThat(census(ROOT).statusCode()).as("no admin connection bound").isEqualTo(503);
        } finally {
            bind(admin());
        }
    }

    // ---- 3. the count must be every tenant's --------------------------------------------------------------------

    /**
     * Roles are cluster-wide and the engine tests share one cluster, so each concrete class names its own fixture
     * roles and creates them from literals of its own file, where the role census can read them.
     */
    abstract String blindRole();

    /** {@code PgContainerHelper.createLoginRole(su, <the literal>, BLIND_PASS, false)}. */
    abstract void createBlindLoginRole(Connection su) throws Exception;

    abstract String ownerRole();

    /** {@code PgContainerHelper.createLoginRole(su, <the literal>, OWNER_PASS, false)}. */
    abstract void createOwnerLoginRole(Connection su) throws Exception;

    static final String BLIND_PASS = "mcblindadminpw";

    private ManifestCheckAdmin blindAdmin() {
        return new ManifestCheckAdmin(new AdminConnection(pg.getJdbcUrl(), blindRole(), BLIND_PASS), check());
    }

    private void createBlindRole() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            createBlindLoginRole(su);
            PgContainerHelper.grantSchemaUsage(su, "nexus", blindRole());
            DSL.using(su, SQLDialect.POSTGRES).grant(DSL.privilege("SELECT")).on(CATALOG_DOCUMENT_CHUNKS)
                .to(DSL.role(blindRole())).execute();
        }
    }

    /** An admin whose census drops {@code dropTenant} entirely, or one row of {@code shortTenant}. */
    private ManifestCheckAdmin lyingAdmin(String dropTenant, String shortTenant) {
        return new ManifestCheckAdmin(
                new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()), check()) {
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
        try (Connection blind = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), blindRole(), BLIND_PASS)) {
            assertThat(DSL.using(blind, SQLDialect.POSTGRES).fetchCount(CATALOG_DOCUMENT_CHUNKS))
                .as("fixture: under FORCE RLS this role reads ZERO rows, which an unchecked census takes for clean")
                .isZero();
        }
        bind(blindAdmin());
        try {
            for (HttpResponse<String> r : List.of(census(ROOT), add(ROOT))) {
                assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
                assertThat(json(r).path("reason").asText()).isEqualTo("admin_cannot_see_all_tenants");
                assertThat(json(r).path("error").asText()).contains(blindRole());
            }
            assertThat(admin().state().present()).as("nothing was added").isFalse();
        } finally {
            bind(admin());
        }
    }

    @Test
    @Order(31)
    void aCensusBelowATenantScopedCountIsRefused_evenWhenTheRoleLooksUnrestricted() throws Exception {
        for (var lying : List.of(lyingAdmin(B, null), lyingAdmin(null, A))) {
            bind(lying);
            try {
                for (HttpResponse<String> r : List.of(census(ROOT), add(ROOT))) {
                    assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
                    assertThat(json(r).path("reason").asText()).isEqualTo("admin_probe_undercount");
                }
            } finally {
                bind(admin());
            }
        }
        assertThat(admin().state().present()).as("nothing was added").isFalse();
    }

    // ---- 3b. the table's owner is enough: the managed service's nexus_admin ----------------------------------------

    static final String OWNER_PASS = "mcownerroutepw";

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
            createOwnerLoginRole(su);
            PgContainerHelper.grantSchemaUsage(su, "nexus", ownerRole());
            PgContainerHelper.grantSchemaCreate(su, "nexus", ownerRole());
            PgContainerHelper.alterTableOwner(su, CATALOG_DOCUMENT_CHUNKS, ownerRole());
        }
        bind(
            new ManifestCheckAdmin(new AdminConnection(pg.getJdbcUrl(), ownerRole(), OWNER_PASS), check()));
        try {
            try (Connection owner = java.sql.DriverManager.getConnection(pg.getJdbcUrl(), ownerRole(), OWNER_PASS)) {
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
            bind(admin());
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
        bind(new ManifestCheckAdmin(
                new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()), check()) {
            @Override
            public Action add() {
                try {
                    row(A, 90, true);   // a writer that sets the columns apart, after the count and before the add
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
            bind(admin());
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                ctx.alterTable(CATALOG_DOCUMENT_CHUNKS)
                    .dropConstraintIfExists(DSL.name(check().constraint())).execute();
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
            bind(new ManifestCheckAdmin(
                    new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()), check()) {
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
                bind(admin());
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
        assertThat(c.path("definition").asText()).contains(definitionWord()).endsWith("NOT VALID");
        assertThat(json(second).path("constraint").path("action").asText()).isEqualTo("present");
        assertThat(json(first).path("tenants_checked").size()).isGreaterThanOrEqualTo(4);
        assertThat(json(first).path("violating_after_add").asLong()).as("the count after the add: nothing raced").isZero();
        assertThat(json(first).path("violators_after_add").size()).isZero();
        assertThat(json(first).has("warning")).isFalse();
    }

    @Test
    @Order(41)
    void aNewRowShapedLikeTheViolatorsIsRefusedEvenBeforeValidate() {
        assertThatThrownBy(() -> row(A, 50, true)).isInstanceOf(DataAccessException.class)
            .satisfies(e -> assertThat(((DataAccessException) e).sqlState()).isEqualTo(CHECK_VIOLATION));
    }

    @Test
    @Order(42)
    void addOnAnExistingConstraintAnswersPresentWithoutASurvey_evenForARoleThatCannotCountOrACountThatIsShort()
            throws Exception {
        for (ManifestCheckAdmin bound : List.of(blindAdmin(), lyingAdmin(B, null))) {
            bind(bound);
            try {
                HttpResponse<String> r = add(ROOT);

                assertThat(r.statusCode()).as("a survey would be a 503 here: " + r.body()).isEqualTo(200);
                assertThat(json(r).path("constraint").path("action").asText()).isEqualTo("present");
                assertThat(json(r).path("census_skipped").asText()).isNotEmpty();
                assertThat(json(r).has("tenants")).as("no count was taken").isFalse();
            } finally {
                bind(admin());
            }
        }
    }

    @Test
    @Order(50)
    void validateIsRefusedWhileATenantHoldsAViolator_namingEach_andLeavesTheCheckNotValid() throws Exception {
        // A constraint an operator added by hand over rows that break it (the add route would have refused): drop
        // ours, write the violators while nothing checks, and put the same CHECK back through the admin class.
        dropConstraintIfPresent();
        row(A, 60, true);
        row(GHOST, 62, true);
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
        bind(blindAdmin());
        try {
            HttpResponse<String> blind = validate(ROOT);
            assertThat(blind.statusCode()).as(blind.body()).isEqualTo(503);
            assertThat(json(blind).path("reason").asText()).isEqualTo("admin_cannot_see_all_tenants");
        } finally {
            bind(admin());
        }
        bind(lyingAdmin(B, null));
        try {
            HttpResponse<String> shortCount = validate(ROOT);
            assertThat(shortCount.statusCode()).as(shortCount.body()).isEqualTo(503);
            assertThat(json(shortCount).path("reason").asText()).isEqualTo("admin_probe_undercount");
        } finally {
            bind(admin());
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
        assertThatThrownBy(() -> row(B, 70, true)).isInstanceOf(DataAccessException.class)
            .satisfies(e -> assertThat(((DataAccessException) e).sqlState()).isEqualTo(CHECK_VIOLATION));
        row(B, 72, false);
        assertThat(json(census(ROOT)).path("violating_total").asLong()).isZero();
    }
}
