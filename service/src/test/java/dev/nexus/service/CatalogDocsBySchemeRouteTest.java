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
 * nexus-uwggq.1.5 (RDR-241 Phase 2): the contract of GET /v1/catalog/docs/by-scheme?scheme=&limit=&offset= on the test
 * cluster's locale: {count, limit, offset, documents}, the exact count on every page, tumbler order, a 400 for a scheme that
 * is not an RFC 3986 scheme, 405, 503 on a statement timeout. The urlparse matrix is {@link CatalogDocsBySchemeBase}'s.
 */
class CatalogDocsBySchemeRouteTest extends CatalogDocsBySchemeBase {

    private static final String SVC_ROLE = "svc_rdr241_scheme_test";
    private static final String SVC_PASS = "svc_rdr241_scheme_test_pass";

    @Override String svcRole() { return SVC_ROLE; }
    @Override String svcPass() { return SVC_PASS; }
    @Override void bootstrapRole(Connection su) throws Exception {
        PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
    }

    @Test
    void theClusterIsNotCtypeC() throws Exception {
        assertThat(databaseCtype()).isNotIn("C", "POSIX");
    }

    @Test
    void theAnswerIsTheListShapeWithOffset_andTheRequestedSchemeIsReadWithoutRegardToCase() throws Exception {
        insert(Row.of("7.1.1").uri("HTTPS://example.org/x").author("A"), Row.of("7.1.2").uri("http://example.org/y"));
        var body = ok(PATH + "?scheme=Https");
        assertThat(body.keySet()).containsExactlyInAnyOrder("count", "limit", "offset", "documents");
        assertThat(body).containsEntry("count", 1).containsEntry("limit", 1000).containsEntry("offset", 0);
        assertThat(tumblers(body)).containsExactly("7.1.1");
        assertThat(documents(body).get(0)).containsEntry("source_uri", "HTTPS://example.org/x").containsEntry("author", "A");
        assertThat(documents(body).get(0).keySet()).isEqualTo(documents(ok("/v1/catalog/list?limit=10")).get(0).keySet());
    }

    @Test
    void theCountIsTheSameOnEveryPage_includingAPagePastTheEnd() throws Exception {
        for (int i = 1; i <= 7; i++) insert(Row.of("7.1." + i).uri("nx-note://n" + i));
        insert(Row.of("7.2.1").uri("https://other"));
        var p0 = ok(PATH + "?scheme=nx-note&limit=3");
        var p1 = ok(PATH + "?scheme=nx-note&limit=3&offset=3");
        var p2 = ok(PATH + "?scheme=nx-note&limit=3&offset=6");
        var past = ok(PATH + "?scheme=nx-note&limit=3&offset=9");
        for (var p : List.of(p0, p1, p2, past)) assertThat(p).containsEntry("count", 7).containsEntry("limit", 3);
        assertThat(tumblers(p0)).containsExactly("7.1.1", "7.1.2", "7.1.3");
        assertThat(tumblers(p1)).containsExactly("7.1.4", "7.1.5", "7.1.6");
        assertThat(tumblers(p2)).containsExactly("7.1.7");
        assertThat(documents(past)).isEmpty();
        assertThat(past).containsEntry("offset", 9);
        assertThat(ok(PATH + "?scheme=zzz")).containsEntry("count", 0);
        assertThat(ok(PATH + "?scheme=zzz&offset=5")).containsEntry("count", 0);
    }

    @Test
    void aTombstonedRowIsExcluded_andAnotherTenantsRowsAreInvisible() throws Exception {
        insert(Row.of("7.1.1").uri("https://ours"), Row.of("7.1.2").uri("https://gone").tombstoned(),
            Row.of("7.1.3").tenant(TENANT_B).uri("https://theirs"));
        assertThat(tumblers(ok(PATH + "?scheme=https"))).containsExactly("7.1.1");
        var theirs = ok(TOKEN_B, TENANT_B, PATH + "?scheme=https");
        assertThat(tumblers(theirs)).containsExactly("7.1.3");
        assertThat(theirs).containsEntry("count", 1);
    }

    @Test
    void aSchemeThatIsNotAnRfc3986SchemeIsA400_asAreBadBoundsAndAWrongMethod() throws Exception {
        insert(Row.of("7.1.1").uri("https://ours"));
        for (String q : List.of("", "?scheme=", "?limit=5", "?scheme=1http", "?scheme=-x", "?scheme=ht%20tp", "?scheme=https%3A", "?scheme=ht%09tp",
            "?scheme=h%C3%A9", "?scheme=a_b", "?scheme=a+b", "?scheme=https&limit=0", "?scheme=https&limit=1001", "?scheme=https&limit=abc",
            "?scheme=https&offset=-1", "?scheme=https&offset=abc", "?scheme=https%00")) {
            assertThat(status(PATH + q)).as("query: " + q).isEqualTo(400);
        }
        for (String ok : List.of("?scheme=a", "?scheme=a%2Bb", "?scheme=a.b-c9", "?scheme=X-DEVONTHINK-ITEM")) {
            assertThat(status(PATH + ok)).as("query: " + ok).isEqualTo(200);
        }
        assertThat(post(TOKEN, TENANT, PATH + "?scheme=https", "{}").statusCode()).isEqualTo(405);
    }

    @Test
    void aReadThatOutlastsItsStatementBoundAnswers503WithNoPartialBody_andTheNextReadAnswers() throws Exception {
        insert(Row.of("7.1.1").uri("https://ours"));
        System.setProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_DOCUMENTS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long t0 = System.nanoTime();
            var resp = get(TOKEN, TENANT, PATH + "?scheme=https");
            long ms = (System.nanoTime() - t0) / 1_000_000;
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(503);
            var body = mapper.readValue(resp.body(), MAP_T);
            assertThat(body).containsEntry("reason", "statement_timeout");
            assertThat(body).doesNotContainKeys("count", "documents");
            assertThat(ms).as("answered at the bound, not after the lock was released").isLessThan(5_000);
            holder.rollback();
        } finally {
            System.clearProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY);
        }
        assertThat(tumblers(ok(PATH + "?scheme=https"))).containsExactly("7.1.1");
    }
}
