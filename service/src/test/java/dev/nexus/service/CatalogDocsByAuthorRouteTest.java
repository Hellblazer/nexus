// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import org.junit.jupiter.api.Test;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-uwggq.2.3 (RDR-241 Phase 2): {@code GET /v1/catalog/docs/by-author?author=TEXT&limit=&offset=} answers the live
 * documents whose author contains the text, case-insensitively and literally, in tumbler order, a page at a time, with
 * the exact count. Real PostgreSQL, the real route; each exclusion sits beside a control row that IS returned.
 */
class CatalogDocsByAuthorRouteTest extends Rdr241RouteTestBase {

    private static final String SVC_ROLE = "svc_rdr241_byauthor_test";
    private static final String SVC_PASS = "svc_rdr241_byauthor_test_pass";
    private static final String PATH = "/v1/catalog/docs/by-author";

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

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    private List<Object> hits(String author) throws Exception {
        return tumblers(ok(PATH + "?author=" + enc(author)));
    }

    @Test
    void theAuthorIsContainedCaseInsensitively_inTumblerOrder() throws Exception {
        insert(Row.of("6.1.3").author("LAMPORT, L."), Row.of("6.1.1").author("Leslie Lamport"),
            Row.of("6.1.2").author("Someone Else"), Row.of("6.1.4").author("lamport & Lynch"), Row.of("6.1.5"));   // 6.1.5: no author
        var body = ok(PATH + "?author=Lamport");
        assertThat(tumblers(body)).containsExactly("6.1.1", "6.1.3", "6.1.4");
        assertThat(body).containsEntry("count", 3).containsEntry("limit", 1000).containsEntry("offset", 0);
        assertThat(body.keySet()).containsExactlyInAnyOrder("count", "limit", "offset", "documents");   // no capped
        assertThat(hits("LAMPORT")).containsExactly("6.1.1", "6.1.3", "6.1.4");
        assertThat(hits("l. ")).isEmpty();      // "LAMPORT, L." ends at "L.": no trailing space
        assertThat(hits(", l")).containsExactly("6.1.3");
    }

    @Test
    void aPercentAndAnUnderscoreInTheTextAreLiteral() throws Exception {
        insert(Row.of("6.1.1").author("100% Author_Fan"), Row.of("6.1.2").author("100 Authors Fan"),
            Row.of("6.1.3").author("Plain Name"), Row.of("6.1.4").author("back\\slash"));
        assertThat(hits("0% author_f")).containsExactly("6.1.1");
        assertThat(hits("%")).containsExactly("6.1.1");            // unescaped, % would match every author
        assertThat(hits("_")).containsExactly("6.1.1");            // unescaped, _ would match every author
        assertThat(hits("s_f")).isEmpty();          // unescaped, s_f would match "Authors Fan"
        assertThat(hits("\\")).containsExactly("6.1.4");
        assertThat(hits("k\\s")).containsExactly("6.1.4");
    }

    @Test
    void theClusterThisClassRunsOnHasAUnicodeCtype_soTheFoldTestBelowIsNotTheCCtypeCase() throws Exception {
        // The C-ctype cluster the local bundle runs is CatalogDocsByAuthorFoldCtypeCTest's; this class is the other locale.
        assertThat(databaseCtype()).isNotIn("C", "POSIX");
    }

    @Test
    void nonAsciiLettersFoldAsUnicodeSimpleLowercase_withNoNormalization() throws Exception {
        String composed = "Müller";            // u with diaeresis, one code point
        String decomposed = "Müller";         // u + combining diaeresis
        insert(Row.of("6.1.1").author("José MÜLLER"), Row.of("6.1.2").author(composed),
            Row.of("6.1.3").author(decomposed), Row.of("6.1.4").author("Muller"), Row.of("6.1.5").author("Łukasz Żółć"));
        assertThat(hits("müller")).containsExactly("6.1.1", "6.1.2");          // composed needle: composed authors only
        assertThat(hits("MÜLLER")).containsExactly("6.1.1", "6.1.2");
        assertThat(hits("müller")).containsExactly("6.1.3");                  // decomposed needle: decomposed author only
        assertThat(hits("muller")).containsExactly("6.1.4");                         // "o" or "u" alone never matches the accented form
        assertThat(hits("żółć")).containsExactly("6.1.5");       // folds Z-dot, o-acute, L-stroke and c-acute
    }

    @Test
    void aTombstonedRowIsExcluded_andAnotherTenantsRowsAreInvisible() throws Exception {
        insert(Row.of("6.1.1").author("Needle One"), Row.of("6.1.2").author("Needle Two").tombstoned(),
            Row.of("6.1.3").tenant(TENANT_B).author("Needle Three"));
        var ours = ok(PATH + "?author=needle");
        assertThat(tumblers(ours)).containsExactly("6.1.1");
        assertThat(ours).containsEntry("count", 1);
        var theirs = ok(TOKEN_B, TENANT_B, PATH + "?author=needle");
        assertThat(tumblers(theirs)).containsExactly("6.1.3");
        assertThat(theirs).containsEntry("count", 1);
    }

    @Test
    void theCountIsTheSameOnEveryPage_includingAPagePastTheEnd() throws Exception {
        for (int i = 1; i <= 7; i++) insert(Row.of("6.1." + i).author("Page Author " + i));
        insert(Row.of("6.2.1").author("Somebody Else"));
        var p0 = ok(PATH + "?author=page&limit=3");
        var p1 = ok(PATH + "?author=page&limit=3&offset=3");
        var p2 = ok(PATH + "?author=page&limit=3&offset=6");
        var past = ok(PATH + "?author=page&limit=3&offset=9");
        var wayPast = ok(PATH + "?author=page&limit=3&offset=1000");
        for (var p : List.of(p0, p1, p2, past, wayPast)) assertThat(p).containsEntry("count", 7).containsEntry("limit", 3);
        assertThat(p1).containsEntry("offset", 3);
        assertThat(past).containsEntry("offset", 9);
        assertThat(tumblers(p0)).containsExactly("6.1.1", "6.1.2", "6.1.3");
        assertThat(tumblers(p1)).containsExactly("6.1.4", "6.1.5", "6.1.6");
        assertThat(tumblers(p2)).containsExactly("6.1.7");
        assertThat(documents(past)).isEmpty();
        assertThat(documents(wayPast)).isEmpty();
    }

    @Test
    void noMatchAnswersACountOfZero_atEveryOffset() throws Exception {
        insert(Row.of("6.1.1").author("Somebody"));
        assertThat(ok(PATH + "?author=zzz")).containsEntry("count", 0);
        assertThat(ok(PATH + "?author=zzz&offset=5")).containsEntry("count", 0);
    }

    @Test
    void aMissingOrEmptyAuthorAndBadBoundsAreA400_andOnlyGetIsAllowed() throws Exception {
        insert(Row.of("6.1.1").author("Somebody"));
        for (String q : List.of("", "?author=", "?limit=5", "?author=x&limit=0", "?author=x&limit=1001", "?author=x&limit=abc",
            "?author=x&offset=-1", "?author=x&offset=abc")) {
            assertThat(status(PATH + q)).as("query: " + q).isEqualTo(400);
        }
        assertThat(ok(PATH + "?author=x&limit=1000")).containsEntry("limit", 1000);
        assertThat(post(TOKEN, TENANT, PATH + "?author=x", "{}").statusCode()).isEqualTo(405);
    }

    @Test
    void aRowCarriesTheDocumentShapeOfList() throws Exception {
        insert(Row.of("6.1.1").author("Shape Author").meta("{\"k\":\"v\"}"));
        var d = documents(ok(PATH + "?author=shape")).get(0);
        assertThat(d).containsEntry("tumbler", "6.1.1").containsEntry("author", "Shape Author");
        assertThat(d.get("metadata")).isEqualTo(java.util.Map.of("k", "v"));
        assertThat(d.keySet()).isEqualTo(documents(ok("/v1/catalog/list?limit=10")).get(0).keySet());
    }

    @Test
    void aReadThatOutlastsItsStatementBoundAnswers503WithNoPartialBody_andTheNextReadAnswers() throws Exception {
        insert(Row.of("6.1.1").author("Somebody"));
        System.setProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_DOCUMENTS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long t0 = System.nanoTime();
            var resp = get(TOKEN, TENANT, PATH + "?author=some");
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
        assertThat(hits("some")).containsExactly("6.1.1");
    }
}
