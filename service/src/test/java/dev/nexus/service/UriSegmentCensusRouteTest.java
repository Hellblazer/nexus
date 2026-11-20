// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.UriSegmentCensusReads;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P1.2 (nexus-p2m1x.2): {@code GET /v1/catalog/uri_segment_census?limit=N} groups the tenant's
 * {@code chroma://} and {@code nx-note://} source-URI rows by (table, scheme, segment, collection), each with its row
 * count and its collection's registry content type, in one bounded tenant-scoped read. Real PostgreSQL, the real
 * route. Each exclusion (a {@code file://} row, another tenant) sits beside rows that ARE listed in the same fixture,
 * so an empty answer cannot come from a fixture that lists nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class UriSegmentCensusRouteTest {

    private static final String TOKEN    = "uri-seg-census-test-token-rdr229";
    private static final String TOKEN_B  = "uri-seg-census-test-token-rdr229-b";
    private static final String SVC_ROLE = "svc_uri_seg_census_test";
    private static final String SVC_PASS = "svc_uri_seg_census_test_pass";
    private static final String TENANT   = TenantConstants.DEFAULT_TENANT;
    private static final String TENANT_B = "tenant-b-rdr229-uri-segments";
    /** A registry row whose name carries no prefix, content type docs (a label a rename put there). */
    private static final String LABEL    = "mylabel";
    private static final String DOCS     = "docs__rdr229-seg__minilm-l6-v2-384__v1";
    private static final String KNOW     = "knowledge__rdr229-seg__minilm-l6-v2-384__v1";
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;
    int seq;

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, TOKEN, TENANT, "test-bound");
            PgContainerHelper.seedServiceToken(dsl, TOKEN_B, TENANT_B, "test-bound-b");
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = TestHttp.client();
        // Burn the per-tenant ghost sweep (nexus-ft04v.3) on a request that registers nothing.
        get(TOKEN, "/v1/catalog/collections/list");
        get(TOKEN_B, "/v1/catalog/collections/list");
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void wipe() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            dsl.deleteFrom(DOCUMENT_ASPECTS).execute();
            dsl.deleteFrom(DOCUMENT_HIGHLIGHTS).execute();
            dsl.deleteFrom(CATALOG_DOCUMENTS).execute();
        }
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    /** Register {@code collection} for {@code tenant} under {@code contentType} (an unconformant name gets 'unknown'). */
    private void registry(String tenant, String collection, String contentType) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, collection);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.CONTENT_TYPE, contentType)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.NAME.eq(collection))).execute();
        }
    }

    private void seedRegistry() throws Exception {
        registry(TENANT, LABEL, "docs");
        registry(TENANT, DOCS, "docs");
        registry(TENANT, KNOW, "knowledge");
    }

    /** {@code n} catalog_documents rows filed under {@code collection}, each with its own URI under {@code uriBase}. */
    private void docs(String tenant, String collection, String uriBase, int n, boolean deleted) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int i = 0; i < n; i++) {
                String tumbler = "rdr229." + (seq++);
                PgContainerHelper.insertCatalogDocument(ctx, tenant, tumbler);
                var upd = ctx.update(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.SOURCE_URI, uriBase + "/doc" + seq)
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection);
                var q = deleted ? upd.set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.parse("2026-11-01T00:00:00+00:00")) : upd;
                assertThat(q.where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).execute()).isEqualTo(1);
            }
        }
    }

    private void aspects(String tenant, String collection, String uriBase, int n) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int i = 0; i < n; i++) {
                String tumbler = "rdr229.a" + (seq++);
                PgContainerHelper.insertCatalogDocument(ctx, tenant, tumbler);
                ctx.insertInto(DOCUMENT_ASPECTS)
                    .set(DOCUMENT_ASPECTS.DOC_ID, tumbler)
                    .set(DOCUMENT_ASPECTS.TENANT_ID, tenant)
                    .set(DOCUMENT_ASPECTS.COLLECTION, collection)
                    .set(DOCUMENT_ASPECTS.SOURCE_PATH, "p" + (seq++) + ".md")
                    .set(DOCUMENT_ASPECTS.EXTRACTED_AT, OffsetDateTime.now())
                    .set(DOCUMENT_ASPECTS.MODEL_VERSION, "test-model")
                    .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "test-extractor")
                    .set(DOCUMENT_ASPECTS.SOURCE_URI, uriBase + "/a" + seq)
                    .execute();
            }
        }
    }

    private void highlights(String tenant, String collection, String uriBase, int n) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int i = 0; i < n; i++) {
                String tumbler = "rdr229.h" + (seq++);
                PgContainerHelper.insertCatalogDocument(ctx, tenant, tumbler);
                ctx.insertInto(DOCUMENT_HIGHLIGHTS)
                    .set(DOCUMENT_HIGHLIGHTS.TENANT_ID, tenant)
                    .set(DOCUMENT_HIGHLIGHTS.DOC_ID, tumbler)
                    .set(DOCUMENT_HIGHLIGHTS.SOURCE_URI, uriBase + "/h" + seq)
                    .set(DOCUMENT_HIGHLIGHTS.COLLECTION, collection)
                    .set(DOCUMENT_HIGHLIGHTS.INGESTED_AT, OffsetDateTime.now())
                    .execute();
            }
        }
    }

    /** One row of every shape RDR-229 P1.2 names, with a row count per group that no two groups share. */
    private void shapes() throws Exception {
        seedRegistry();
        docs(TENANT, LABEL, "chroma://" + LABEL, 5, false);          // prefix-free, file-typed, segment IS its collection
        docs(TENANT, LABEL, "chroma://oldlabel", 2, false);          // prefix-free, file-typed, segment is not its collection
        docs(TENANT, KNOW, "chroma://" + KNOW, 7, false);            // a legacy-spelled note in a knowledge collection
        docs(TENANT, DOCS, "chroma://" + DOCS, 11, false);           // file-routed segment: the backfill-source-uri residue
        docs(TENANT, LABEL, "nx-note://" + LABEL, 4, false);         // nx-note:// with a prefix-free segment, file-typed collection
        docs(TENANT, KNOW, "nx-note://" + KNOW, 13, false);          // an ordinary note
        docs(TENANT, DOCS, "file:///repo/a", 17, false);             // not a scheme the route groups
        aspects(TENANT, LABEL, "chroma://" + LABEL, 3);
        highlights(TENANT, LABEL, "chroma://" + LABEL, 1);
    }

    private Map<String, Object> census(String query) throws Exception {
        var resp = get(TOKEN, "/v1/catalog/uri_segment_census" + query);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> groups(Map<String, Object> body) {
        return (List<Map<String, Object>>) body.get("groups");
    }

    private static Map<String, Object> find(List<Map<String, Object>> groups, String table, String scheme,
                                            String segment, String collection) {
        return groups.stream().filter(g -> g.get("table").equals(table) && g.get("scheme").equals(scheme)
                && g.get("segment").equals(segment) && g.get("collection").equals(collection)).findFirst().orElse(null);
    }

    // ── the answer ──────────────────────────────────────────────────────────

    @Test
    void anEmptyTenantAnswersNoGroups() throws Exception {
        var body = census("");
        assertThat(body).containsEntry("count", 0).containsEntry("limit", 10_000).containsEntry("capped", false);
        assertThat(groups(body)).isEmpty();
        assertThat(body.keySet()).containsExactlyInAnyOrder("count", "limit", "capped", "groups");
    }

    @Test
    void everyShapeIsCountedPerTableSchemeSegmentAndCollection_withTheRegistryContentType() throws Exception {
        shapes();
        var body = census("");
        var gs = groups(body);
        assertThat(gs.get(0).keySet()).containsExactlyInAnyOrder("table", "scheme", "segment", "collection", "content_type", "rows");

        var residueSame = find(gs, "catalog_documents", "chroma", LABEL, LABEL);
        assertThat(residueSame).containsEntry("rows", 5).containsEntry("content_type", "docs");
        var residueMoved = find(gs, "catalog_documents", "chroma", "oldlabel", LABEL);
        assertThat(residueMoved).containsEntry("rows", 2).containsEntry("content_type", "docs");
        assertThat(find(gs, "catalog_documents", "chroma", KNOW, KNOW)).containsEntry("rows", 7).containsEntry("content_type", "knowledge");
        assertThat(find(gs, "catalog_documents", "chroma", DOCS, DOCS)).containsEntry("rows", 11);
        assertThat(find(gs, "catalog_documents", "nx-note", LABEL, LABEL)).containsEntry("rows", 4).containsEntry("content_type", "docs");
        assertThat(find(gs, "catalog_documents", "nx-note", KNOW, KNOW)).containsEntry("rows", 13);
        assertThat(find(gs, "document_aspects", "chroma", LABEL, LABEL)).containsEntry("rows", 3).containsEntry("content_type", "docs");
        assertThat(find(gs, "document_highlights", "chroma", LABEL, LABEL)).containsEntry("rows", 1).containsEntry("content_type", "docs");
        assertThat(gs).as("a file:// row is not grouped, and no other group exists").hasSize(8)
            .noneMatch(g -> String.valueOf(g.get("segment")).contains("repo"));
        assertThat(body).containsEntry("count", 8).containsEntry("capped", false);
    }

    @Test
    void theSegmentIsTheTextBetweenTheSchemeAndTheFirstSlash() throws Exception {
        registry(TENANT, LABEL, "docs");
        docs(TENANT, LABEL, "chroma://" + LABEL, 1, false);
        // a URI whose rest holds "://" later on, and one with no path at all
        docs(TENANT, LABEL, "chroma://seg/http://elsewhere", 1, false);
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            String t = "rdr229.bare";
            PgContainerHelper.insertCatalogDocument(ctx, TENANT, t);
            ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, "chroma://bare")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, LABEL)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(t))).execute();
        }
        var gs = groups(census(""));
        assertThat(find(gs, "catalog_documents", "chroma", LABEL, LABEL)).containsEntry("rows", 1);
        assertThat(find(gs, "catalog_documents", "chroma", "seg", LABEL)).containsEntry("rows", 1);
        assertThat(find(gs, "catalog_documents", "chroma", "bare", LABEL)).containsEntry("rows", 1);
    }

    @Test
    void aDocumentWhoseCollectionHasNoRegistryRowCarriesAnEmptyContentType() throws Exception {
        seedRegistry();
        docs(TENANT, LABEL, "chroma://" + LABEL, 1, false);
        docs(TENANT, "unregistered-coll", "chroma://unregistered-coll", 2, false);
        var gs = groups(census(""));
        assertThat(find(gs, "catalog_documents", "chroma", "unregistered-coll", "unregistered-coll"))
            .containsEntry("rows", 2).containsEntry("content_type", "");
        assertThat(find(gs, "catalog_documents", "chroma", LABEL, LABEL)).containsEntry("content_type", "docs");
    }

    @Test
    void aTombstonedDocumentIsCounted_becauseP210sMoveRepairsEveryRowOfTheTable() throws Exception {
        seedRegistry();
        docs(TENANT, LABEL, "chroma://" + LABEL, 2, false);
        docs(TENANT, LABEL, "chroma://" + LABEL + "x", 3, true);
        var gs = groups(census(""));
        assertThat(find(gs, "catalog_documents", "chroma", LABEL, LABEL)).containsEntry("rows", 2);
        assertThat(find(gs, "catalog_documents", "chroma", LABEL + "x", LABEL)).containsEntry("rows", 3);
    }

    @Test
    void anotherTenantsRowsAreInvisible_andSoAreOurs() throws Exception {
        seedRegistry();
        docs(TENANT, LABEL, "chroma://" + LABEL, 2, false);
        registry(TENANT_B, LABEL, "docs");
        docs(TENANT_B, LABEL, "chroma://" + LABEL, 9, false);
        assertThat(find(groups(census("")), "catalog_documents", "chroma", LABEL, LABEL)).containsEntry("rows", 2);
        var resp = get(TOKEN_B, "/v1/catalog/uri_segment_census");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        var theirs = groups(mapper.readValue(resp.body(), MAP_T));
        assertThat(theirs).hasSize(1);
        assertThat(theirs.get(0)).containsEntry("rows", 9);
    }

    // ── the bound ───────────────────────────────────────────────────────────

    @Test
    void theListStopsAtTheLimit_theCountDoesNot_andCappedSaysSo() throws Exception {
        shapes();
        var one = census("?limit=1");
        assertThat(one).containsEntry("count", 8).containsEntry("limit", 1).containsEntry("capped", true);
        assertThat(groups(one)).hasSize(1);
        var eight = census("?limit=8");
        assertThat(eight).containsEntry("count", 8).containsEntry("capped", false);
        assertThat(groups(eight)).hasSize(8);
        var nine = census("?limit=9");
        assertThat(nine).containsEntry("count", 8).containsEntry("capped", false);
        assertThat(groups(nine)).hasSize(8);
    }

    @Test
    void theDefaultLimitIsTheMaximum_aBadLimitIsA400_andOnlyGetIsAllowed() throws Exception {
        assertThat(census("")).containsEntry("limit", UriSegmentCensusReads.MAX_LIMIT);
        assertThat(census("?limit=10000")).containsEntry("limit", 10_000);
        for (String bad : List.of("0", "-1", "10001", "abc", "1.5")) {
            assertThat(get(TOKEN, "/v1/catalog/uri_segment_census?limit=" + bad).statusCode()).as("limit=" + bad).isEqualTo(400);
        }
        assertThat(post(TOKEN, "/v1/catalog/uri_segment_census", "{}").statusCode()).isEqualTo(405);
    }

    @Test
    void aReadThatOutlastsItsStatementBoundAnswers503WithNoPartialBody_andTheNextReadAnswers() throws Exception {
        // Another session holds ACCESS EXCLUSIVE on catalog_documents, so the statement waits on the lock until its
        // statement_timeout fires; nothing is answered in part.
        seedRegistry();
        docs(TENANT, LABEL, "chroma://" + LABEL, 1, false);
        System.setProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_DOCUMENTS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long t0 = System.nanoTime();
            var resp = get(TOKEN, "/v1/catalog/uri_segment_census");
            long ms = (System.nanoTime() - t0) / 1_000_000;
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(503);
            var body = mapper.readValue(resp.body(), MAP_T);
            assertThat(body).containsEntry("reason", "statement_timeout");
            assertThat(body).doesNotContainKeys("count", "groups", "capped");
            assertThat(ms).as("answered at the bound, not after the lock was released").isLessThan(5_000);
            holder.rollback();
        } finally {
            System.clearProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY);
        }
        assertThat(groups(census(""))).hasSize(1);
    }

    /** The window count is taken before the LIMIT, over the union: a page of 2 of 8 still reports 8. */
    @Test
    void theTotalIsTheWholeSetNotThePage() throws Exception {
        shapes();
        var page = census("?limit=2");
        assertThat(page).containsEntry("count", 8).containsEntry("capped", true);
        assertThat(groups(page)).hasSize(2);
    }

    // ── the statement ───────────────────────────────────────────────────────

    /** The route is ONE statement: three grouped selects under a UNION ALL, the total a window count beside each row. */
    @Test
    void theOneStatementIsAUnionOfThreeGroupedReadsWithTheTotalAsAWindowCount() throws Exception {
        shapes();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var statement = UriSegmentCensusReads.listQuery(ctx, TENANT, 11);
            String plan = PgContainerHelper.explainPlanInlined(ctx, statement);
            System.out.println("EXPLAIN uri_segment_census (one statement):\n" + plan);
            assertThat(plan).contains("WindowAgg").containsPattern("(?i)(Group|Hash)Aggregate");
            String sql = ctx.renderInlined(statement).toLowerCase();
            assertThat(sql).contains("count(*) over ()").contains("union all").contains("group by");
            assertThat(ctx.fetch(statement)).hasSize(8);
        }
    }

    // ── http ────────────────────────────────────────────────────────────────

    private HttpResponse<String> post(String token, String path, String body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", token.equals(TOKEN) ? TENANT : TENANT_B)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String token, String path) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", token.equals(TOKEN) ? TENANT : TENANT_B)
            .GET()
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
