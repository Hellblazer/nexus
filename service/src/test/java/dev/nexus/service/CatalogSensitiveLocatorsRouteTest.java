// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-uwggq.2.5 (RDR-241 Phase 2): {@code GET /v1/catalog/sensitive/locators?limit=N} lists the tenant's live
 * sensitive documents and the live documents that alias a live sensitive one, each with the five fields operator
 * dispatch reads to write a deny rule, in one bounded tenant-scoped read. Real PostgreSQL, the real route. Each
 * exclusion sits beside a control row that IS listed, so an empty answer cannot come from a fixture listing nothing.
 */
class CatalogSensitiveLocatorsRouteTest extends Rdr241RouteTestBase {

    private static final String SVC_ROLE = "svc_rdr241_locators_test";
    private static final String SVC_PASS = "svc_rdr241_locators_test_pass";
    private static final String PATH = "/v1/catalog/sensitive/locators";

    @Override String svcRole() { return SVC_ROLE; }
    @Override String svcPass() { return SVC_PASS; }
    @Override void bootstrapRole(Connection su) throws Exception {
        PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
    }

    /**
     * RDR-229 P3.1: a document names COLL by registry id, so COLL is registered for both tenants. It runs after the
     * base's startup (which burns each tenant's ghost sweep, and that sweep removes a collection nothing references).
     */
    @org.junit.jupiter.api.BeforeAll
    void registerRouteCollection() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, COLL);
            PgContainerHelper.insertCollection(dsl, TENANT_B, COLL);
        }
    }

    @Test
    void anEmptyTenantAnswersNoDocuments_inTheEnvelopeOfSensitive() throws Exception {
        var body = ok(PATH);
        assertThat(body).containsEntry("count", 0).containsEntry("limit", 1000).containsEntry("capped", false);
        assertThat(documents(body)).isEmpty();
        assertThat(body.keySet()).containsExactlyInAnyOrder("count", "limit", "capped", "documents");
    }

    @Test
    void aSensitiveRowCarriesTheFiveLocatorFieldsAndItsOwnSensitiveFlag() throws Exception {
        insert(Row.of("9.1.1").flagged().uri("file:///s/one.md").path("/s/one.md"));
        var body = ok(PATH);
        assertThat(body).containsEntry("count", 1);
        var d = documents(body).get(0);
        assertThat(d).containsEntry("tumbler", "9.1.1").containsEntry("source_uri", "file:///s/one.md")
            .containsEntry("file_path", "/s/one.md").containsEntry("alias_of", "").containsEntry("physical_collection", COLL)
            .containsEntry("sensitive", true);
        assertThat(d.keySet()).containsExactlyInAnyOrder("tumbler", "source_uri", "file_path", "alias_of", "physical_collection", "sensitive");
    }

    @Test
    void sensitiveIsTheRowsOwnFlag_anAliasReadsFalseUnlessItIsItselfFlagged() throws Exception {
        insert(Row.of("9.1.1").flagged(), Row.of("9.1.2").alias("9.1.1"), Row.of("9.1.3").alias("9.1.1").flagged());
        var docs = documents(ok(PATH));
        assertThat(docs).extracting(d -> d.get("sensitive")).containsExactly(true, false, true);
        assertThat(docs).extracting(d -> d.get("alias_of")).containsExactly("", "9.1.1", "9.1.1");
    }

    @Test
    void anAliasOfALiveSensitiveRowIsListed_withItsOwnFileAndItsAliasOf() throws Exception {
        insert(Row.of("9.1.1").flagged(),
            Row.of("9.1.2").alias("9.1.1").uri("file:///dup/copy.md").path("/dup/copy.md"));
        var body = ok(PATH);
        assertThat(body).containsEntry("count", 2);
        assertThat(tumblers(body)).containsExactly("9.1.1", "9.1.2");
        assertThat(documents(body).get(1)).containsEntry("alias_of", "9.1.1")
            .containsEntry("source_uri", "file:///dup/copy.md").containsEntry("file_path", "/dup/copy.md");
    }

    @Test
    void anAliasOfANonSensitiveRowIsExcluded() throws Exception {
        insert(Row.of("9.1.1").flagged(), Row.of("9.1.2").alias("9.1.1"),          // control pair, listed
            Row.of("9.2.1"), Row.of("9.2.2").alias("9.2.1"));                         // plain pair, not listed
        assertThat(tumblers(ok(PATH))).containsExactly("9.1.1", "9.1.2");
    }

    @Test
    void aDeletedSensitiveRowIsExcluded_andSoIsItsLiveAlias() throws Exception {
        insert(Row.of("9.1.1").flagged(), Row.of("9.1.2").alias("9.1.1"),          // control pair, listed
            Row.of("9.2.1").flagged().tombstoned(), Row.of("9.2.2").alias("9.2.1"));   // dead canonical, live alias
        var body = ok(PATH);
        assertThat(tumblers(body)).containsExactly("9.1.1", "9.1.2");
        assertThat(body).containsEntry("count", 2);
    }

    @Test
    void aDeletedAliasIsExcluded() throws Exception {
        insert(Row.of("9.1.1").flagged(), Row.of("9.1.2").alias("9.1.1"), Row.of("9.1.3").alias("9.1.1").tombstoned());
        assertThat(tumblers(ok(PATH))).containsExactly("9.1.1", "9.1.2");
    }

    @Test
    void aRowThatIsBothSensitiveAndAnAliasOfASensitiveRowAppearsOnce() throws Exception {
        insert(Row.of("9.1.1").flagged(), Row.of("9.1.2").flagged().alias("9.1.1"));
        var body = ok(PATH);
        assertThat(tumblers(body)).containsExactly("9.1.1", "9.1.2");
        assertThat(body).containsEntry("count", 2);
    }

    @Test
    void anAliasNamesOnlyASensitiveRowOfItsOwnTenant() throws Exception {
        // tenant B's row 9.1.1 is sensitive; tenant A's row 9.1.2 names "9.1.1" but A has no such sensitive row.
        insert(Row.of("9.1.1").tenant(TENANT_B).flagged(), Row.of("9.1.2").alias("9.1.1"), Row.of("9.3.1").flagged());
        assertThat(tumblers(ok(PATH))).containsExactly("9.3.1");
    }

    @Test
    void theListIsInTumblerOrder() throws Exception {
        insert(Row.of("9.1.3").flagged(), Row.of("9.1.1").flagged(), Row.of("9.1.2").flagged());
        assertThat(tumblers(ok(PATH))).containsExactly("9.1.1", "9.1.2", "9.1.3");
    }

    @Test
    void anotherTenantsRowsAreInvisible_andSoAreOurs() throws Exception {
        insert(Row.of("9.1.1").flagged(), Row.of("9.2.1").tenant(TENANT_B).flagged(),
            Row.of("9.2.2").tenant(TENANT_B).alias("9.2.1"));
        assertThat(tumblers(ok(PATH))).containsExactly("9.1.1");
        var theirs = ok(TOKEN_B, TENANT_B, PATH);
        assertThat(theirs).containsEntry("count", 2);
        assertThat(tumblers(theirs)).containsExactly("9.2.1", "9.2.2");
    }

    @Test
    void theListStopsAtTheLimit_theCountDoesNot_andCappedSaysSoOnlyAboveIt() throws Exception {
        insert(Row.of("9.1.1").flagged(), Row.of("9.1.2").flagged(), Row.of("9.1.3").alias("9.1.1"));
        var one = ok(PATH + "?limit=1");
        assertThat(one).containsEntry("count", 3).containsEntry("limit", 1).containsEntry("capped", true);
        assertThat(tumblers(one)).containsExactly("9.1.1");
        var three = ok(PATH + "?limit=3");
        assertThat(three).containsEntry("count", 3).containsEntry("capped", false);   // exactly the limit: not capped
        assertThat(documents(three)).hasSize(3);
        var four = ok(PATH + "?limit=4");
        assertThat(four).containsEntry("count", 3).containsEntry("capped", false);
    }

    @Test
    void theDefaultLimitIsOneThousandAndTheMaxTenThousand_andABadLimitIsA400() throws Exception {
        assertThat(ok(PATH)).containsEntry("limit", 1000);
        assertThat(ok(PATH + "?limit=10000")).containsEntry("limit", 10000);
        for (String bad : List.of("0", "-1", "10001", "abc", "1.5")) {
            assertThat(status(PATH + "?limit=" + bad)).as("limit=" + bad).isEqualTo(400);
        }
    }

    @Test
    void onlyGetIsAllowed() throws Exception {
        assertThat(post(TOKEN, TENANT, PATH, "{}").statusCode()).isEqualTo(405);
    }

    @Test
    void theSensitiveRouteItselfStillAnswersItsOwnShape() throws Exception {
        insert(Row.of("9.1.1").flagged());
        var body = ok("/v1/catalog/sensitive");
        assertThat(documents(body).get(0).keySet()).containsExactlyInAnyOrder("tumbler", "physical_collection");
    }

    @Test
    void aReadThatOutlastsItsStatementBoundAnswers503WithNoPartialBody_andTheNextReadAnswers() throws Exception {
        insert(Row.of("9.1.1").flagged());
        System.setProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_DOCUMENTS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long t0 = System.nanoTime();
            var resp = get(TOKEN, TENANT, PATH);
            long ms = (System.nanoTime() - t0) / 1_000_000;
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(503);
            var body = mapper.readValue(resp.body(), MAP_T);
            assertThat(body).containsEntry("reason", "statement_timeout");
            assertThat(body).doesNotContainKeys("count", "documents", "capped");
            assertThat(ms).as("answered at the bound, not after the lock was released").isLessThan(5_000);
            holder.rollback();
        } finally {
            System.clearProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY);
        }
        assertThat(tumblers(ok(PATH))).containsExactly("9.1.1");
    }
}
