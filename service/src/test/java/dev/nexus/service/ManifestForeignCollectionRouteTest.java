// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.ManifestForeignCollectionReads;
import dev.nexus.service.db.TenantConstants;
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
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * GH #1611: {@code GET /v1/catalog/manifest/foreign_collection?limit=N} lists the tenant's live documents whose
 * manifest names chunks outside the document's own physical collection, with the true total, in one bounded
 * tenant-scoped read. Real PostgreSQL, the real route. Each exclusion (a document whose manifest is all at home,
 * tombstoned, another tenant) sits beside a control document that IS listed in the same fixture, so an empty answer
 * cannot come from a fixture that lists nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestForeignCollectionRouteTest {

    private static final String TOKEN    = "foreign-coll-test-token-gh1611";
    private static final String TOKEN_B  = "foreign-coll-test-token-gh1611-b";
    private static final String SVC_ROLE = "svc_foreign_coll_test";
    private static final String SVC_PASS = "svc_foreign_coll_test_pass";
    private static final String TENANT   = TenantConstants.DEFAULT_TENANT;
    private static final String TENANT_B = "tenant-b-gh1611-foreign";
    private static final String COLL_A   = "knowledge__gh1611-own__minilm-l6-v2-384__v1";
    private static final String COLL_C   = "docs__gh1611-other__minilm-l6-v2-384__v1";
    private static final String COLL_B   = "docs__gh1611-repo__minilm-l6-v2-384__v1";
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;
    int seq;
    long chashSeq;
    final Map<String, Integer> nextPosition = new HashMap<>();

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
        get(TOKEN, TENANT, "/v1/catalog/collections/list");
        get(TOKEN_B, TENANT_B, "/v1/catalog/collections/list");
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void wipe() throws Exception {
        nextPosition.clear();
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            dsl.deleteFrom(CATALOG_DOCUMENT_CHUNKS).execute();
            dsl.deleteFrom(CATALOG_DOCUMENTS).execute();
        }
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    /** A registered document whose physical collection is {@code collection}: its tumbler. */
    private String doc(String token, String tenant, String collection) throws Exception {
        String owner = "gh1611.fc" + (seq++);
        var resp = post(token, tenant, "/v1/catalog/doc/register", mapper.writeValueAsString(Map.of(
            "owner_prefix", owner, "title", "doc " + owner, "content_type", "prose",
            "source_uri", "file:///gh1611/" + owner + ".md", "physical_collection", collection)));
        assertThat(resp.statusCode()).as("register: " + resp.body()).isEqualTo(200);
        return (String) mapper.readValue(resp.body(), MAP_T).get("tumbler");
    }

    /** {@code n} manifest rows of {@code tumbler}, each naming a chunk that really exists in {@code collection}. */
    private void chunks(String tenant, String tumbler, String collection, int n) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, collection);
            var hexes = new ArrayList<String>();
            for (int i = 0; i < n; i++) hexes.add(String.format("%064x", ++chashSeq));
            PgContainerHelper.insertChunks(ctx, tenant, collection, hexes,
                hexes.stream().map(h -> "text " + h.substring(48)).toList(),
                hexes.stream().map(h -> new float[384]).toList(),
                hexes.stream().map(h -> Map.<String, Object>of()).toList());
            String model = PgContainerHelper.collectionModel(ctx, tenant, collection);
            for (String hex : hexes) {
                int pos = nextPosition.merge(tenant + "/" + tumbler, 1, Integer::sum) - 1;
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
                    .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant)
                    .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, tumbler)
                    .set(CATALOG_DOCUMENT_CHUNKS.POSITION, pos)
                    .set(CATALOG_DOCUMENT_CHUNKS.CHASH, dev.nexus.service.db.Chash.fromHex(hex).toBytes())
                    .set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, pos)
                    .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
                    .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model)
                    .execute();
            }
        }
    }

    /** A document at home (physical collection COLL_A, three rows there): never a finding. */
    private String homeDoc() throws Exception {
        String t = doc(TOKEN, TENANT, COLL_A);
        chunks(TENANT, t, COLL_A, 3);
        return t;
    }

    /** The GH #1611 shape: physical collection COLL_A, manifest rows in COLL_B (the repo's collection). */
    private String strayDoc(int stray) throws Exception {
        String t = doc(TOKEN, TENANT, COLL_A);
        chunks(TENANT, t, COLL_B, stray);
        return t;
    }

    private void tombstone(String tenant, String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.parse("2026-11-01T00:00:00+00:00"))
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
                .execute();
            assertThat(n).isEqualTo(1);
        }
    }

    private Map<String, Object> list(String query) throws Exception {
        var resp = get(TOKEN, TENANT, "/v1/catalog/manifest/foreign_collection" + query);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }

    private static List<String> collections(Map<String, Object> doc) {
        return ((List<?>) doc.get("collections")).stream().map(String::valueOf).toList();
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> documents(Map<String, Object> body) {
        return (List<Map<String, Object>>) body.get("documents");
    }

    // ── the answer ──────────────────────────────────────────────────────────

    @Test
    void anEmptyTenantAnswersNoDocuments() throws Exception {
        var body = list("");
        assertThat(body).containsEntry("count", 0).containsEntry("limit", 1000).containsEntry("capped", false);
        assertThat(documents(body)).isEmpty();
        assertThat(body.keySet()).containsExactlyInAnyOrder("count", "limit", "capped", "documents");
    }

    @Test
    void aDocumentWhoseManifestNamesAnotherCollectionIsListed_withTheCollectionsAndTheStrayChunkCount() throws Exception {
        String home = homeDoc();
        String stray = strayDoc(4);
        var body = list("");
        assertThat(body).containsEntry("count", 1).containsEntry("capped", false);
        var docs = documents(body);
        assertThat(docs).extracting(d -> d.get("tumbler")).containsExactly(stray).doesNotContain(home);
        var d = docs.get(0);
        assertThat(d.keySet()).containsExactlyInAnyOrder("tumbler", "physical_collection", "collections", "chunks");
        assertThat(d).containsEntry("physical_collection", COLL_A).containsEntry("chunks", 4);
        assertThat(collections(d)).containsExactly(COLL_B);
    }

    @Test
    void aMixedManifestCountsOnlyTheRowsOutsideItsOwnCollection_andNamesEveryOtherCollectionSorted() throws Exception {
        String t = doc(TOKEN, TENANT, COLL_A);
        chunks(TENANT, t, COLL_A, 2);   // at home: not counted
        chunks(TENANT, t, COLL_C, 3);
        chunks(TENANT, t, COLL_B, 1);
        var docs = documents(list(""));
        assertThat(docs).hasSize(1);
        assertThat(docs.get(0)).containsEntry("tumbler", t).containsEntry("chunks", 4);
        assertThat(collections(docs.get(0))).containsExactly(COLL_C, COLL_B).doesNotContain(COLL_A);
    }

    @Test
    void aTombstonedDocumentIsNeitherListedNorCounted() throws Exception {
        String control = strayDoc(1);
        String gone = strayDoc(2);
        tombstone(TENANT, gone);
        var body = list("");
        assertThat(body).containsEntry("count", 1).containsEntry("capped", false);
        assertThat(documents(body)).extracting(d -> d.get("tumbler")).containsExactly(control);
    }

    @Test
    void aDocumentWithNoPhysicalCollectionIsNotAFinding() throws Exception {
        String control = strayDoc(1);
        String ghost = doc(TOKEN, TENANT, COLL_A);
        chunks(TENANT, ghost, COLL_B, 2);
        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, "")
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(ghost))).execute();
            assertThat(n).isEqualTo(1);
        }
        assertThat(documents(list(""))).extracting(d -> d.get("tumbler")).containsExactly(control);
    }

    @Test
    void anotherTenantsDocumentsAreInvisible_andSoAreOurs() throws Exception {
        String ours = strayDoc(1);
        String theirs = doc(TOKEN_B, TENANT_B, COLL_A);
        chunks(TENANT_B, theirs, COLL_B, 1);
        assertThat(documents(list(""))).extracting(d -> d.get("tumbler")).containsExactly(ours).doesNotContain(theirs);
        var resp = get(TOKEN_B, TENANT_B, "/v1/catalog/manifest/foreign_collection");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        var theirBody = mapper.readValue(resp.body(), MAP_T);
        assertThat(theirBody).containsEntry("count", 1);
        assertThat(documents(theirBody)).extracting(d -> d.get("tumbler")).containsExactly(theirs);
    }

    // ── the bound ───────────────────────────────────────────────────────────

    @Test
    void theListStopsAtTheLimit_theCountDoesNot_andCappedSaysSo() throws Exception {
        List<String> made = new ArrayList<>();
        for (int i = 0; i < 3; i++) made.add(strayDoc(1));

        var one = list("?limit=1");
        assertThat(one).containsEntry("count", 3).containsEntry("limit", 1).containsEntry("capped", true);
        assertThat(documents(one)).extracting(d -> d.get("tumbler")).containsExactly(made.get(0));

        var three = list("?limit=3");
        assertThat(three).containsEntry("count", 3).containsEntry("capped", false);
        assertThat(documents(three)).hasSize(3).extracting(d -> d.get("tumbler")).isSorted();

        var four = list("?limit=4");
        assertThat(four).containsEntry("count", 3).containsEntry("capped", false);
        assertThat(documents(four)).hasSize(3);
    }

    @Test
    void theDefaultLimitIsOneThousand_andTheMaxIsTenThousand() throws Exception {
        assertThat(list("")).containsEntry("limit", 1000);
        assertThat(list("?limit=10000")).containsEntry("limit", 10000);
    }

    @Test
    void aBadLimitIsA400_andOnlyGetIsAllowed() throws Exception {
        for (String bad : List.of("0", "-1", "10001", "abc", "1.5")) {
            var resp = get(TOKEN, TENANT, "/v1/catalog/manifest/foreign_collection?limit=" + bad);
            assertThat(resp.statusCode()).as("limit=" + bad).isEqualTo(400);
        }
        assertThat(post(TOKEN, TENANT, "/v1/catalog/manifest/foreign_collection", "{}").statusCode()).isEqualTo(405);
    }

    @Test
    void aReadThatOutlastsItsStatementBoundAnswers503WithNoPartialBody_andTheNextReadAnswers() throws Exception {
        // Another session holds ACCESS EXCLUSIVE on the manifest table, so the statement waits on the lock until its
        // statement_timeout fires; nothing is answered in part.
        String d = strayDoc(1);
        System.setProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_DOCUMENT_CHUNKS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long t0 = System.nanoTime();
            var resp = get(TOKEN, TENANT, "/v1/catalog/manifest/foreign_collection");
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
        assertThat(documents(list(""))).extracting(x -> x.get("tumbler")).containsExactly(d);
    }

    /** The window count is taken before the LIMIT: a page of 2 of 5 still reports 5. */
    @Test
    void theTotalIsTheWholeSetNotThePage() throws Exception {
        for (int i = 0; i < 5; i++) strayDoc(2);
        var page = list("?limit=2");
        assertThat(page).containsEntry("count", 5).containsEntry("capped", true);
        assertThat(documents(page)).hasSize(2);
    }

    // ── the plan ────────────────────────────────────────────────────────────

    /**
     * The route is ONE statement (the total is a window count beside each grouped row). The predicate compares two
     * columns across a join, so there is no index that answers it (see {@link ManifestForeignCollectionReads}); this
     * pins the statement's shape and prints its plan so the cost class is on the record: a join of the tenant's live
     * documents to the manifest, aggregated, with the timeout as the bound.
     */
    @Test
    void theOneStatementIsAJoinGroupedByDocumentWithTheTotalAsAWindowCount() throws Exception {
        for (int i = 0; i < 6; i++) strayDoc(2);
        for (int i = 0; i < 20; i++) homeDoc();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            var statement = ManifestForeignCollectionReads.listQuery(ctx, TENANT, 11);
            String plan = PgContainerHelper.explainPlanInlined(ctx, statement);
            System.out.println("EXPLAIN foreign_collection (one statement):\n" + plan);
            assertThat(plan).contains("WindowAgg").containsPattern("(?i)(Group|Hash)Aggregate");
            String sql = ctx.renderInlined(statement).toLowerCase();
            assertThat(sql).contains("count(*) over ()").contains("group by").contains("<>");
            assertThat(ctx.fetch(statement)).hasSize(6);
        }
    }

    // ── http ────────────────────────────────────────────────────────────────

    private HttpResponse<String> post(String token, String tenant, String path, String body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String token, String tenant, String path) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .GET()
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
