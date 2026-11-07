// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.InputStream;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * nexus-6pfgb.44 on a real PostgreSQL: the catalog TTL CHECK's predicate, its census and its add / validate steps
 * ({@link CatalogTtlCheckAdmin}). The cases are {@code catalog-ttl-check-shapes.json}, which
 * {@code tests/test_catalog_ttl_check_shapes.py} holds against the Python reader ({@code note_ttl_days}), so the
 * census, the CHECK and the reader are one fact checked three ways.
 *
 * <p>One database, ordered steps: the predicate and the census are read before the constraint exists; the
 * constraint then goes on NOT VALID, is VALIDATEd, and refuses.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogTtlCheckAdminIntegrationTest {

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final String T = "ttl-pred";
    private static final String STAMP = "2026-01-01T00:00:00Z";
    /** SQLSTATE: check_violation. */
    private static final String CHECK_VIOLATION = "23514";

    private PostgreSQLContainer<?> pg;
    private final AtomicInteger seq = new AtomicInteger();

    /** One shape of {@code metadata}: its raw JSON and whether the predicate reads it as carrying a TTL. */
    private record Shape(String name, String metaJson, boolean carriesTtl) {}

    private static List<Shape> shapes() throws Exception {
        try (InputStream in = CatalogTtlCheckAdminIntegrationTest.class.getResourceAsStream(
                "/catalog-ttl-check-shapes.json")) {
            JsonNode all = JSON.readTree(in);
            List<Shape> out = new ArrayList<>();
            for (JsonNode n : all) {
                out.add(new Shape(n.path("name").asText(), n.path("meta_json").asText(), n.path("carries_ttl").asBoolean()));
            }
            return out;
        }
    }

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    private CatalogTtlCheckAdmin admin() {
        return new CatalogTtlCheckAdmin(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()));
    }

    private String insert(String tenant, String metaJson) throws Exception {
        String tumbler = "ttl." + seq.incrementAndGet();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES)
                .insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.METADATA)
                .values(tenant, tumbler, "Doc " + tumbler, metaJson == null ? null : JSONB.jsonb(metaJson))
                .execute();
        }
        return tumbler;
    }

    private void deleteAll() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CATALOG_DOCUMENTS).execute();
        }
    }

    /** The predicate's value for each tumbler, as the server computes it over the stored row. */
    private Map<String, Boolean> predicateOf(String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            Map<String, Boolean> out = new LinkedHashMap<>();
            DSL.using(su, SQLDialect.POSTGRES)
                .select(CATALOG_DOCUMENTS.TUMBLER, DSL.field(CatalogTtlCheckAdmin.ttlRequiresPutAt()))
                .from(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant))
                .fetch().forEach(r -> out.put(r.value1(), r.value2()));
            return out;
        }
    }

    private static String withPutAt(String metaJson, String putAtJson) throws Exception {
        var node = (com.fasterxml.jackson.databind.node.ObjectNode) JSON.readTree(metaJson);
        node.set("put_at", JSON.readTree(putAtJson));
        return JSON.writeValueAsString(node);
    }

    /** True when the insert is refused by the CHECK (and by nothing else: any other failure rethrows). */
    private boolean refusedByCheck(String tenant, String metaJson) throws Exception {
        try {
            insert(tenant, metaJson);
            return false;
        } catch (DataAccessException e) {
            assertThat(e.sqlState()).as("a refusal is the CHECK's (23514), never a cast error: %s", e.getMessage())
                .isEqualTo(CHECK_VIOLATION);
            return true;
        }
    }

    // ---- 0. the predicate, before any constraint exists -------------------------------------------------------

    @Test
    @Order(0)
    void everyShapeHasAPredicateValueThatIsNeverNull_andNeverACastError() throws Exception {
        Map<String, Shape> byTumbler = new LinkedHashMap<>();
        for (Shape s : shapes()) {
            byTumbler.put(insert(T, s.metaJson()), s);
        }
        byTumbler.put(insert(T, null), new Shape("metadata-is-SQL-NULL", null, false));
        long sizeBefore = byTumbler.size();
        assertThat(sizeBefore).as("non-vacuity: the corpus is not empty").isGreaterThan(30);

        Map<String, Boolean> got = predicateOf(T);

        assertThat(got).hasSize((int) sizeBefore);
        got.forEach((tumbler, value) -> {
            Shape s = byTumbler.get(tumbler);
            assertThat(value).as("shape %s: a NULL would let the row through the CHECK", s.name()).isNotNull();
            // no put_at in any of these: the predicate is FALSE exactly when the shape carries a TTL
            assertThat(value).as("shape %s", s.name()).isEqualTo(!s.carriesTtl());
        });
    }

    @Test
    @Order(1)
    void aHugeStringOfDigitsIsNotACastTarget() throws Exception {
        // 200000 digits would overflow a numeric (131072 digits before the point) if the string were ever cast.
        String t = "ttl-huge";
        String tumbler = insert(t, "{\"ttl_days\":\"" + "9".repeat(200_000) + "\"}");

        assertThat(predicateOf(t)).containsEntry(tumbler, true);
    }

    @Test
    @Order(2)
    void putAtIsAStampWhenItIsANonEmptyString_andEmptyOrAnyOtherTypeIsAbsent() throws Exception {
        String t = "ttl-putat";
        Map<String, Boolean> expectedByPutAt = new LinkedHashMap<>();
        expectedByPutAt.put("\"" + STAMP + "\"", true);
        expectedByPutAt.put("\"not an instant\"", true);   // SQL cannot mirror parse_iso: documented
        expectedByPutAt.put("\"\"", false);                 // "" means absent
        expectedByPutAt.put("null", false);
        expectedByPutAt.put("5", false);
        expectedByPutAt.put("true", false);
        expectedByPutAt.put("{}", false);
        expectedByPutAt.put("[]", false);
        Map<String, String> tumblerByPutAt = new LinkedHashMap<>();
        for (String putAt : expectedByPutAt.keySet()) {
            tumblerByPutAt.put(putAt, insert(t, withPutAt("{\"ttl_days\":7}", putAt)));
        }
        String absent = insert(t, "{\"ttl_days\":7}");
        String retraction = insert(t, "{\"ttl_days\":0,\"put_at\":\"\"}");
        String stringTtl = insert(t, withPutAt("{\"ttl_days\":\"7\"}", "\"" + STAMP + "\""));

        Map<String, Boolean> got = predicateOf(t);

        expectedByPutAt.forEach((putAt, ok) -> assertThat(got.get(tumblerByPutAt.get(putAt)))
            .as("ttl_days 7 with put_at %s", putAt).isEqualTo(ok));
        assertThat(got.get(absent)).as("ttl_days 7 with no put_at").isFalse();
        assertThat(got.get(retraction)).as("_NOTE_META_UNSET: ttl_days 0 with put_at \"\"").isTrue();
        assertThat(got.get(stringTtl)).as("a digit-string TTL with a stamp").isTrue();
    }

    // ---- 1. the census is the CHECK's own predicate ----------------------------------------------------------

    @Test
    @Order(10)
    void theCensusCountsExactlyTheRowsThePredicateRefuses_perTenant_tombstonesIncluded() throws Exception {
        deleteAll();
        String a = "ttl-census-a";
        String b = "ttl-census-b";
        int expectedA = 0;
        long carriesA = 0;
        for (Shape s : shapes()) {
            insert(a, s.metaJson());
            if (s.carriesTtl()) {
                expectedA++;
                carriesA++;
            }
        }
        String tomb = insert(a, "{\"ttl_days\":7}");   // violator, then tombstoned
        expectedA++;
        carriesA++;
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.parse(STAMP))
                .where(CATALOG_DOCUMENTS.TUMBLER.eq(tomb)).execute();
        }
        insert(b, withPutAt("{\"ttl_days\":7}", "\"" + STAMP + "\""));   // carries a TTL, stamped: not a violator
        insert(b, "{\"ttl_days\":0,\"put_at\":\"\"}");
        long rowsA = shapes().size() + 1;

        List<CatalogTtlCheckAdmin.TenantCensus> census = admin().census();

        assertThat(census).extracting(CatalogTtlCheckAdmin.TenantCensus::tenant).containsExactly(a, b);
        var ca = census.get(0);
        assertThat(ca.rows()).isEqualTo(rowsA);
        assertThat(ca.violating()).as("the shapes that carry a TTL have no put_at, plus the tombstoned one")
            .isEqualTo(expectedA);
        assertThat(ca.ttlRows()).isEqualTo(carriesA);
        assertThat(ca.violatingTombstoned()).as("a tombstoned entry is a row the CHECK evaluates too").isEqualTo(1);
        var cb = census.get(1);
        assertThat(cb.rows()).isEqualTo(2);
        assertThat(cb.ttlRows()).isEqualTo(1);
        assertThat(cb.violating()).isZero();
        deleteAll();
    }

    // ---- 2. add NOT VALID, then every shape against the live constraint ---------------------------------------

    @Test
    @Order(20)
    void addPutsTheCheckOnNotValid_andARerunIsPresent() throws Exception {
        assertThat(admin().state().present()).isFalse();

        var first = admin().add();
        var second = admin().add();

        assertThat(first.action()).isEqualTo("added");
        assertThat(first.after().present()).isTrue();
        assertThat(first.after().validated()).as("added NOT VALID").isFalse();
        assertThat(first.after().definition()).as("the stored definition carries the type guard")
            .contains("jsonb_typeof").endsWith("NOT VALID");
        assertThat(second.action()).isEqualTo("present");
    }

    @Test
    @Order(21)
    void everyShapeIsAcceptedOrRefusedByTheLiveCheckAsThePredicateSays_withNoCastError() throws Exception {
        String t = "ttl-live";
        int refused = 0;
        int accepted = 0;
        for (Shape s : shapes()) {
            boolean r = refusedByCheck(t, s.metaJson());
            assertThat(r).as("shape %s with no put_at", s.name()).isEqualTo(s.carriesTtl());
            refused += r ? 1 : 0;
            accepted += r ? 0 : 1;
            // with a stamp (an object shape only) everything is accepted
            if (s.metaJson().startsWith("{")) {
                assertThat(refusedByCheck(t, withPutAt(s.metaJson(), "\"" + STAMP + "\"")))
                    .as("shape %s with put_at", s.name()).isFalse();
            }
        }
        assertThat(refusedByCheck(t, null)).as("NULL metadata passes").isFalse();
        assertThat(refusedByCheck(t, "{\"ttl_days\":0,\"put_at\":\"\"}")).as("the retraction pair passes").isFalse();
        assertThat(refusedByCheck(t, "{\"ttl_days\":7,\"put_at\":\"\"}")).as("put_at \"\" is absent").isTrue();
        assertThat(refused).as("non-vacuity: the corpus exercises refusals").isGreaterThanOrEqualTo(8);
        assertThat(accepted).as("non-vacuity: the corpus exercises acceptances").isGreaterThanOrEqualTo(20);
    }

    @Test
    @Order(22)
    void anUpdateThatRemovesTheStampFromATtlEntryIsRefused_andOneThatKeepsItIsNot() throws Exception {
        String t = "ttl-update";
        String ok = insert(t, withPutAt("{\"ttl_days\":7}", "\"" + STAMP + "\""));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThatThrownBy(() -> ctx.update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.METADATA, JSONB.jsonb("{\"ttl_days\":7}"))
                .where(CATALOG_DOCUMENTS.TUMBLER.eq(ok)).execute())
                .isInstanceOf(DataAccessException.class)
                .satisfies(e -> assertThat(((DataAccessException) e).sqlState()).isEqualTo(CHECK_VIOLATION));
            assertThat(ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.TITLE, "renamed")
                .where(CATALOG_DOCUMENTS.TUMBLER.eq(ok)).execute()).as("a stamped entry updates").isEqualTo(1);
        }
    }

    // ---- 3. validate ------------------------------------------------------------------------------------------

    @Test
    @Order(30)
    void validateReadsConvalidatedFirst_soARerunDoesNothing() throws Exception {
        var first = admin().validate();
        var second = admin().validate();

        assertThat(first.action()).isEqualTo("validated");
        assertThat(first.after().validated()).isTrue();
        assertThat(second.action()).as("convalidated is true, so no VALIDATE is sent").isEqualTo("already_valid");
        assertThat(admin().add().action()).as("add on a valid constraint leaves it").isEqualTo("present");
        assertThat(admin().state().validated()).isTrue();
    }

    @Test
    @Order(31)
    void afterValidateTheCheckRefusesTtl7WithNoPutAt_andAcceptsTheRest() throws Exception {
        String t = "ttl-valid";

        assertThat(refusedByCheck(t, "{\"ttl_days\":7}")).as("ttl_days 7, no put_at").isTrue();
        assertThat(refusedByCheck(t, "{\"ttl_days\":\"7\"}")).as("a digit-string TTL, no put_at").isTrue();
        assertThat(refusedByCheck(t, "{\"ttl_days\":0,\"put_at\":\"\"}")).isFalse();
        assertThat(refusedByCheck(t, "{}")).isFalse();
        for (String malformed : List.of("\"abc\"", "true", "null", "1e400", "{\"a\":1}", "[1]")) {
            assertThat(refusedByCheck(t, "{\"ttl_days\":" + malformed + "}")).as("ttl_days %s", malformed).isFalse();
        }
    }

    // ---- 4. a NOT VALID check with a violator: the freeze, and the refusal of VALIDATE -------------------------

    @Test
    @Order(40)
    void validateOfAConstraintWithAViolatorFailsCheckViolation_leavingItNotValid_andAViolatorCannotBeUpdated()
            throws Exception {
        String t = "ttl-freeze";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).alterTable(CATALOG_DOCUMENTS)
                .dropConstraint(DSL.name(CatalogTtlCheckAdmin.CONSTRAINT)).execute();
        }
        String violator = insert(t, "{\"ttl_days\":7}");   // an entry written before the constraint existed
        // add() itself does not look for violators (the route does): a constraint an operator added by hand
        assertThat(admin().add().after().validated()).isFalse();

        // DATA EFFECT of add: the violator can no longer be updated, a tombstone included
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThatThrownBy(() -> ctx.update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.parse(STAMP))
                .where(CATALOG_DOCUMENTS.TUMBLER.eq(violator)).execute())
                .isInstanceOf(DataAccessException.class)
                .satisfies(e -> assertThat(((DataAccessException) e).sqlState()).isEqualTo(CHECK_VIOLATION));
        }

        assertThatThrownBy(() -> admin().validate()).isInstanceOf(AdminDdlSession.Refused.class)
            .satisfies(e -> {
                var r = (AdminDdlSession.Refused) e;
                assertThat(r.status()).isEqualTo(409);
                assertThat(r.reason()).isEqualTo("check_violation");
            });
        assertThat(admin().state().validated()).as("it stays NOT VALID").isFalse();

        // fixing the entry (stamp it) lets VALIDATE through
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.METADATA, JSONB.jsonb(withPutAt("{\"ttl_days\":7}", "\"" + STAMP + "\"")))
                .where(CATALOG_DOCUMENTS.TUMBLER.eq(violator)).execute();
        }
        assertThat(admin().validate().action()).isEqualTo("validated");
    }

    @Test
    @Order(41)
    void validateOfAnAbsentConstraintIsReportedNotCreated() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).alterTable(CATALOG_DOCUMENTS)
                .dropConstraint(DSL.name(CatalogTtlCheckAdmin.CONSTRAINT)).execute();
        }

        assertThat(admin().validate().action()).isEqualTo("absent");
        assertThat(admin().state().present()).isFalse();
    }

    @Test
    @Order(50)
    void theCheckExpressionIsTheCensusPredicateRenderedOnce() {
        String expr = CatalogTtlCheckAdmin.checkExpression();

        assertThat(expr).containsIgnoringCase("jsonb_typeof").containsIgnoringCase("coalesce")
            .containsIgnoringCase("case when").as("format() would read a percent sign").doesNotContain("%");
        assertThat(CatalogTtlCheckAdmin.addStatement()).as("the expression is the template's one argument")
            .contains("%s").doesNotContain("jsonb_typeof");
    }
}
