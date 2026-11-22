// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-uwggq.2.4 (RDR-241 Phase 2): {@code GET /v1/catalog/docs/by-meta} answers the live documents whose own metadata
 * satisfies every {@code meta.}, {@code meta_default.} and {@code meta_min.} clause, in tumbler order, a page at a time,
 * with the exact count. The reading rules are those of {@code catalog/meta_filter.py}. Real PostgreSQL, the real route;
 * every case sits beside control rows that ARE returned, so an empty answer cannot come from an empty fixture.
 */
class CatalogDocsByMetaRouteTest extends Rdr241RouteTestBase {

    private static final String SVC_ROLE = "svc_rdr241_bymeta_test";
    private static final String SVC_PASS = "svc_rdr241_bymeta_test_pass";
    private static final String PATH = "/v1/catalog/docs/by-meta";

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

    /** One row per metadata document, tumblers 5.1.1, 5.1.2, ... in the order given. */
    private List<String> seed(String... metadata) throws Exception {
        List<String> tumblers = new ArrayList<>();
        Row[] rows = new Row[metadata.length];
        for (int i = 0; i < metadata.length; i++) {
            String t = "5.1." + (i + 1);
            tumblers.add(t);
            rows[i] = Row.of(t).meta(metadata[i]);
        }
        insert(rows);
        return tumblers;
    }

    private List<Object> hits(String query) throws Exception {
        return tumblers(ok(PATH + "?" + query));
    }

    // ── meta.<key>=<value> ──────────────────────────────────────────────────

    @Test
    void textEquality_matchesTheStoredValueAsText_andABooleanReadsTrueOrFalse() throws Exception {
        seed("{\"k\":\"v\"}", "{\"k\":\"w\"}", "{\"b\":true}", "{\"b\":false}", "{\"b\":\"true\"}", "{\"n\":3}", "{\"n\":\"3\"}", "{\"n\":3.5}");
        assertThat(hits("meta.k=v")).containsExactly("5.1.1");
        assertThat(hits("meta.b=true")).containsExactly("5.1.3", "5.1.5");     // a JSON true and the text "true" read alike
        assertThat(hits("meta.b=false")).containsExactly("5.1.4");
        assertThat(hits("meta.n=3")).containsExactly("5.1.6", "5.1.7");
        assertThat(hits("meta.n=3.5")).containsExactly("5.1.8");
    }

    @Test
    void anAbsentKeyAndAJsonNullMatchNothing_andAnEmptyValueMatchesTheEmptyString() throws Exception {
        seed("{\"k\":\"v\"}", "{}", "{\"k\":null}", "{\"k\":\"\"}", "{\"other\":\"v\"}");
        assertThat(hits("meta.k=v")).containsExactly("5.1.1");
        assertThat(hits("meta.k=")).containsExactly("5.1.4");
        assertThat(hits("meta.k=null")).isEmpty();
    }

    @Test
    void aDefaultMakesAbsentNullAndEmptyReadAsTheDefaultText() throws Exception {
        seed("{}", "{\"k\":null}", "{\"k\":\"\"}", "{\"k\":\"file\"}", "{\"k\":\"mineru\"}", "{\"other\":1}");
        assertThat(hits("meta.k=file&meta_default.k=file")).containsExactly("5.1.1", "5.1.2", "5.1.3", "5.1.4", "5.1.6");
        assertThat(hits("meta.k=mineru&meta_default.k=file")).containsExactly("5.1.5");
        // without the default, absent and null stay unknown
        assertThat(hits("meta.k=file")).containsExactly("5.1.4");
        // the default is the text a missing key reads as, nothing else
        assertThat(hits("meta.k=other&meta_default.k=file")).isEmpty();
    }

    @Test
    void twoPairsAreAnded_andContentTypeNarrowsExactly() throws Exception {
        insert(Row.of("5.1.1").type("paper").meta("{\"a\":\"1\",\"b\":\"2\"}"),
            Row.of("5.1.2").type("paper").meta("{\"a\":\"1\",\"b\":\"3\"}"),
            Row.of("5.1.3").type("prose").meta("{\"a\":\"1\",\"b\":\"2\"}"));
        assertThat(hits("meta.a=1&meta.b=2")).containsExactly("5.1.1", "5.1.3");
        assertThat(hits("meta.a=1&meta.b=2&content_type=paper")).containsExactly("5.1.1");
        assertThat(hits("meta.a=1&content_type=paper")).containsExactly("5.1.1", "5.1.2");
        assertThat(hits("meta.a=1&content_type=pape")).isEmpty();          // exact, not a prefix
        assertThat(hits("meta.a=1&meta.a=2")).isEmpty();                     // the same key twice is two ANDed clauses
    }

    @Test
    void aKeyIsBoundNeverSpliced_soQuotesAndSpacesAreJustKeyCharacters() throws Exception {
        seed("{\"it's\":\"x\"}", "{\"a b\":\"x\"}", "{\"k\":\"x\"}");
        assertThat(hits("meta.it%27s=x")).containsExactly("5.1.1");
        assertThat(hits("meta.a%20b=x")).containsExactly("5.1.2");
        assertThat(hits("meta.k%27%29%20OR%20%28%271%27%3D%271=x")).isEmpty();    // k') OR ('1'='1 is a key, not SQL
    }

    // ── meta_min.<key>=<n> ──────────────────────────────────────────────────

    @Test
    void anIntegerBoundReadsOnlyIntegerText_andNeverRaisesOnAnythingElse() throws Exception {
        // 1:int 3  2:"3"  3:3.0  4:2.9  5:"abc"  6:true  7:null  8:" 3"  9:"+3"  10:-1  11:2  12:10  13:absent
        // (the answer's order is tumbler TEXT order, 5.1.12 before 5.1.2; the order is checked by the paging tests, not here)
        // 14: a 23-digit string  15: 1e30 as a jsonb number (printed as a plain integer)  16: ""
        seed("{\"m\":3}", "{\"m\":\"3\"}", "{\"m\":3.0}", "{\"m\":2.9}", "{\"m\":\"abc\"}", "{\"m\":true}", "{\"m\":null}",
            "{\"m\":\" 3\"}", "{\"m\":\"+3\"}", "{\"m\":-1}", "{\"m\":2}", "{\"m\":10}", "{}",
            "{\"m\":\"99999999999999999999999\"}", "{\"m\":1e30}", "{\"m\":\"\"}");
        assertThat(hits("meta_min.m=3")).containsExactlyInAnyOrder("5.1.1", "5.1.2", "5.1.12", "5.1.14", "5.1.15");
        assertThat(hits("meta_min.m=-1")).containsExactlyInAnyOrder("5.1.1", "5.1.2", "5.1.10", "5.1.11", "5.1.12", "5.1.14", "5.1.15");
        assertThat(hits("meta_min.m=11")).containsExactlyInAnyOrder("5.1.14", "5.1.15");
        assertThat(hits("meta_min.m=0")).containsExactlyInAnyOrder("5.1.1", "5.1.2", "5.1.11", "5.1.12", "5.1.14", "5.1.15");
    }

    @Test
    void aBoundIgnoresTheDefault_andComposesWithEqualityAndContentType() throws Exception {
        insert(Row.of("5.1.1").type("paper").meta("{\"m\":5,\"k\":\"a\"}"),
            Row.of("5.1.2").type("paper").meta("{\"m\":1,\"k\":\"a\"}"),
            Row.of("5.1.3").type("prose").meta("{\"m\":5,\"k\":\"a\"}"),
            Row.of("5.1.4").type("paper").meta("{\"k\":\"a\"}"));
        assertThat(hits("meta_min.m=2&meta.k=a")).containsExactly("5.1.1", "5.1.3");
        assertThat(hits("meta_min.m=2&meta.k=a&content_type=paper")).containsExactly("5.1.1");
        assertThat(hits("meta_min.m=2&meta.k=a&meta_default.k=a&content_type=paper")).containsExactly("5.1.1");
        assertThat(hits("meta_min.m=0&meta.k=a")).containsExactly("5.1.1", "5.1.2", "5.1.3");   // an absent m is no integer
    }

    @Test
    void aBoundOfFiveThousandDigitsIsComparedExactly_asTheOracleTakesAnyLength() throws Exception {
        String bound = "1" + "0".repeat(4999);               // 10^4999, 5,000 digits
        seed("{\"m\":\"" + bound + "\"}", "{\"m\":\"" + "9".repeat(5000) + "\"}", "{\"m\":\"" + "9".repeat(4999) + "\"}",
            "{\"m\":5}", "{\"m\":\"" + "-" + bound + "\"}");
        assertThat(hits("meta_min.m=" + bound)).containsExactlyInAnyOrder("5.1.1", "5.1.2");
        assertThat(hits("meta_min.m=-" + bound)).containsExactlyInAnyOrder("5.1.1", "5.1.2", "5.1.3", "5.1.4", "5.1.5");
    }

    @Test
    void aStoredIntegerTextTooLongForNumericIsReadAsNoInteger_andNeverFailsTheRead() throws Exception {
        // 131,073 digits: one more than PostgreSQL's numeric holds. The cast would raise 22003 and fail the whole read.
        seed("{\"m\":\"" + "7".repeat(131_073) + "\"}", "{\"m\":9}");
        var body = ok(PATH + "?meta_min.m=0");
        assertThat(tumblers(body)).containsExactly("5.1.2");
        assertThat(body).containsEntry("count", 1);
    }

    // ── sensitive=true ──────────────────────────────────────────────────────

    @Test
    void sensitiveTrueKeepsOnlyLiveSensitiveRows_composedWithEveryOtherClause() throws Exception {
        insert(Row.of("5.1.1").flagged().meta("{\"k\":\"v\",\"m\":5}"), Row.of("5.1.2").meta("{\"k\":\"v\",\"m\":5}"),
            Row.of("5.1.3").flagged().meta("{\"k\":\"w\"}"), Row.of("5.1.4").flagged().tombstoned().meta("{\"k\":\"v\"}"),
            Row.of("5.1.5").flagged().type("paper").meta("{\"k\":\"v\"}"),
            Row.of("5.1.6").tenant(TENANT_B).flagged().meta("{\"k\":\"v\"}"));
        assertThat(hits("meta.k=v")).containsExactly("5.1.1", "5.1.2", "5.1.5");
        var body = ok(PATH + "?meta.k=v&sensitive=true");
        assertThat(tumblers(body)).containsExactly("5.1.1", "5.1.5");
        assertThat(body).containsEntry("count", 2);
        assertThat(hits("meta.k=v&sensitive=true&content_type=paper")).containsExactly("5.1.5");
        assertThat(hits("meta_min.m=1&sensitive=true")).containsExactly("5.1.1");
        assertThat(documents(body).get(0)).containsEntry("sensitive", true);
        var theirs = ok(TOKEN_B, TENANT_B, PATH + "?meta.k=v&sensitive=true");
        assertThat(tumblers(theirs)).containsExactly("5.1.6");
    }

    @Test
    void sensitiveTakesOnlyTrue() throws Exception {
        seed("{\"k\":\"v\"}");
        for (String q : List.of("meta.k=v&sensitive=false", "meta.k=v&sensitive=1", "meta.k=v&sensitive=", "meta.k=v&sensitive=TRUE",
            "sensitive=false", "sensitive=", "sensitive=true&sensitive=maybe", "meta.k=v&sensitive=true&sensitive=maybe")) {
            assertThat(status(PATH + "?" + q)).as("query: " + q).isEqualTo(400);
        }
    }

    @Test
    void sensitiveTrueIsAFilterClauseOnItsOwn_alone_withContentType_andPagingPastASmallCap() throws Exception {
        // seven sensitive rows (three of them papers), two live non-sensitive rows, a tombstoned sensitive row, another tenant's
        insert(Row.of("5.1.1").flagged(), Row.of("5.1.2").flagged().type("paper"), Row.of("5.1.3").flagged(),
            Row.of("5.1.4").flagged().type("paper"), Row.of("5.1.5").flagged(), Row.of("5.1.6").flagged().type("paper"),
            Row.of("5.1.7").flagged(), Row.of("5.2.1"), Row.of("5.2.2").type("paper"),
            Row.of("5.3.1").flagged().tombstoned(), Row.of("5.4.1").tenant(TENANT_B).flagged());
        var all = ok(PATH + "?sensitive=true");
        assertThat(all).containsEntry("count", 7);
        assertThat(tumblers(all)).containsExactly("5.1.1", "5.1.2", "5.1.3", "5.1.4", "5.1.5", "5.1.6", "5.1.7");
        var papers = ok(PATH + "?sensitive=true&content_type=paper");
        assertThat(tumblers(papers)).containsExactly("5.1.2", "5.1.4", "5.1.6");
        assertThat(papers).containsEntry("count", 3);
        // a page of 3 stands in for /sensitive's 10,000 cap: every sensitive row is reached by offset, the count stays exact
        var p0 = ok(PATH + "?sensitive=true&limit=3");
        var p1 = ok(PATH + "?sensitive=true&limit=3&offset=3");
        var p2 = ok(PATH + "?sensitive=true&limit=3&offset=6");
        var past = ok(PATH + "?sensitive=true&limit=3&offset=9");
        for (var p : List.of(p0, p1, p2, past)) assertThat(p).containsEntry("count", 7);
        assertThat(tumblers(p0)).containsExactly("5.1.1", "5.1.2", "5.1.3");
        assertThat(tumblers(p2)).containsExactly("5.1.7");
        assertThat(documents(past)).isEmpty();
        assertThat(tumblers(ok(TOKEN_B, TENANT_B, PATH + "?sensitive=true"))).containsExactly("5.4.1");
    }

    @Test
    void aRequestWithNeitherAMetaClauseNorSensitiveTrueIsStillA400() throws Exception {
        seed("{\"k\":\"v\"}");
        for (String q : List.of("content_type=prose", "meta_default.k=v", "limit=5", "content_type=prose&limit=5&offset=0")) {
            assertThat(status(PATH + "?" + q)).as("query: " + q).isEqualTo(400);
        }
        assertThat(status(PATH)).isEqualTo(400);
    }

    // ── the 400s ────────────────────────────────────────────────────────────

    @Test
    void aRequestWithNoMetaClauseIsA400_soTheRouteIsNeverAnUnfilteredListing() throws Exception {
        seed("{\"k\":\"v\"}");
        for (String q : List.of("", "content_type=prose", "meta_default.k=v", "content_type=prose&meta_default.k=v", "limit=5")) {
            assertThat(status(PATH + "?" + q)).as("query: " + q).isEqualTo(400);
        }
        assertThat(status(PATH)).isEqualTo(400);
    }

    @Test
    void badParametersAreA400() throws Exception {
        seed("{\"k\":\"v\"}");
        for (String q : List.of(
            "meta.k=v&limit=0", "meta.k=v&limit=1001", "meta.k=v&limit=abc", "meta.k=v&offset=-1", "meta.k=v&offset=abc",
            "meta_min.k=abc", "meta_min.k=1.5", "meta_min.k=", "meta_min.k=%2B3",
            "meta.=v", "meta_min.=3", "meta.k=v&meta_default.=x", "meta.k=v&meta_default.j=x",
            "meta.k=v&meta_default.k=a&meta_default.k=b", "meta.k=v&surprise=1", "meta.k=v&meta_mni.k=3")) {
            assertThat(status(PATH + "?" + q)).as("query: " + q).isEqualTo(400);
        }
        assertThat(post(TOKEN, TENANT, PATH + "?meta.k=v", "{}").statusCode()).isEqualTo(405);
    }

    // ── the page ────────────────────────────────────────────────────────────

    @Test
    void theCountIsTheSameOnEveryPage_includingAPagePastTheEnd() throws Exception {
        String[] docs = new String[7];
        for (int i = 0; i < 7; i++) docs[i] = "{\"k\":\"v\"}";
        seed(docs);
        insert(Row.of("5.2.1").meta("{\"k\":\"other\"}"));   // not a match
        var p0 = ok(PATH + "?meta.k=v&limit=3&offset=0");
        var p1 = ok(PATH + "?meta.k=v&limit=3&offset=3");
        var p2 = ok(PATH + "?meta.k=v&limit=3&offset=6");
        var past = ok(PATH + "?meta.k=v&limit=3&offset=9");
        var wayPast = ok(PATH + "?meta.k=v&limit=3&offset=1000");
        for (var p : List.of(p0, p1, p2, past, wayPast)) assertThat(p).containsEntry("count", 7).containsEntry("limit", 3);
        assertThat(p0).containsEntry("offset", 0);
        assertThat(p2).containsEntry("offset", 6);
        assertThat(past).containsEntry("offset", 9);
        assertThat(tumblers(p0)).containsExactly("5.1.1", "5.1.2", "5.1.3");
        assertThat(tumblers(p1)).containsExactly("5.1.4", "5.1.5", "5.1.6");
        assertThat(tumblers(p2)).containsExactly("5.1.7");
        assertThat(documents(past)).isEmpty();
        assertThat(documents(wayPast)).isEmpty();
        assertThat(p0.keySet()).containsExactlyInAnyOrder("count", "limit", "offset", "documents");   // no capped
    }

    @Test
    void noMatchAnswersACountOfZero_atEveryOffset() throws Exception {
        seed("{\"k\":\"v\"}");
        assertThat(ok(PATH + "?meta.k=zzz")).containsEntry("count", 0).containsEntry("offset", 0);
        assertThat(ok(PATH + "?meta.k=zzz&offset=5")).containsEntry("count", 0);
    }

    @Test
    void theDefaultLimitIsOneThousand() throws Exception {
        seed("{\"k\":\"v\"}");
        assertThat(ok(PATH + "?meta.k=v")).containsEntry("limit", 1000);
        assertThat(ok(PATH + "?meta.k=v&limit=1000")).containsEntry("limit", 1000);
    }

    @Test
    void aRowCarriesTheDocumentShapeOfList_itsMetadataIncluded() throws Exception {
        insert(Row.of("5.1.1").author("A. Author").meta("{\"k\":\"v\",\"n\":2}"));
        var d = documents(ok(PATH + "?meta.k=v")).get(0);
        assertThat(d).containsEntry("tumbler", "5.1.1").containsEntry("author", "A. Author").containsEntry("content_type", "prose");
        assertThat(d.get("metadata")).isEqualTo(java.util.Map.of("k", "v", "n", 2));
        var list = documents(ok("/v1/catalog/list?limit=10")).get(0);
        assertThat(d.keySet()).isEqualTo(list.keySet());
    }

    // ── isolation ───────────────────────────────────────────────────────────

    @Test
    void aTombstonedRowIsExcluded_andAnotherTenantsRowsAreInvisible() throws Exception {
        insert(Row.of("5.1.1").meta("{\"k\":\"v\"}"), Row.of("5.1.2").tombstoned().meta("{\"k\":\"v\"}"),
            Row.of("5.1.3").tenant(TENANT_B).meta("{\"k\":\"v\"}"));
        var ours = ok(PATH + "?meta.k=v");
        assertThat(tumblers(ours)).containsExactly("5.1.1");
        assertThat(ours).containsEntry("count", 1);
        var theirs = ok(TOKEN_B, TENANT_B, PATH + "?meta.k=v");
        assertThat(tumblers(theirs)).containsExactly("5.1.3");
        assertThat(theirs).containsEntry("count", 1);
    }

    // ── the bound ───────────────────────────────────────────────────────────

    @Test
    void aReadThatOutlastsItsStatementBoundAnswers503WithNoPartialBody_andTheNextReadAnswers() throws Exception {
        seed("{\"k\":\"v\"}");
        System.setProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_DOCUMENTS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long t0 = System.nanoTime();
            var resp = get(TOKEN, TENANT, PATH + "?meta.k=v");
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
        assertThat(hits("meta.k=v")).containsExactly("5.1.1");
    }
}
