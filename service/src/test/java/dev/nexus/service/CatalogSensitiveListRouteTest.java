// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CatalogSensitiveReads;
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
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: {@code GET /v1/catalog/sensitive?limit=N} lists the tenant's live sensitive documents (tumbler and
 * physical collection) with the true total, in one bounded tenant-scoped read. Real PostgreSQL, the real route.
 * Each exclusion (not sensitive, tombstoned, another tenant) is shown beside a control row that IS listed in the
 * same fixture, so an empty answer cannot come from a fixture that lists nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogSensitiveListRouteTest {

    private static final String TOKEN    = "sensitive-list-test-token-f3yqd";
    private static final String TOKEN_B  = "sensitive-list-test-token-f3yqd-b";
    private static final String SVC_ROLE = "svc_sensitive_list_test";
    private static final String SVC_PASS = "svc_sensitive_list_test_pass";
    private static final String TENANT   = TenantConstants.DEFAULT_TENANT;
    private static final String TENANT_B = "tenant-b-f3yqd-sens";
    private static final String COLL_A   = "knowledge__f3yqd-a__v1";
    private static final String COLL_B   = "knowledge__f3yqd-b__v1";
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
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CATALOG_DOCUMENTS).execute();
        }
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    /** A registered document in {@code collection}, sensitive when asked: its tumbler. */
    private String doc(String token, String tenant, String collection, boolean sensitive) throws Exception {
        String owner = "f3yqd.sens" + (seq++);
        var resp = post(token, tenant, "/v1/catalog/doc/register", mapper.writeValueAsString(Map.of(
            "owner_prefix", owner, "title", "doc " + owner, "content_type", "prose",
            "source_uri", "file:///f3yqd/" + owner + ".md", "physical_collection", collection)));
        assertThat(resp.statusCode()).as("register: " + resp.body()).isEqualTo(200);
        String tumbler = (String) mapper.readValue(resp.body(), MAP_T).get("tumbler");
        if (sensitive) {
            try (Connection su = pg.createConnection("")) {
                int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.SENSITIVE, true)
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
                    .execute();
                assertThat(n).isEqualTo(1);
            }
        }
        return tumbler;
    }

    private String sensitiveDoc(String collection) throws Exception {
        return doc(TOKEN, TENANT, collection, true);
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
        var resp = get(TOKEN, TENANT, "/v1/catalog/sensitive" + query);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
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
    void theListNamesEachSensitiveDocumentWithItsPhysicalCollection_inTumblerOrder() throws Exception {
        String a = sensitiveDoc(COLL_A);
        String b = sensitiveDoc(COLL_B);
        String c = sensitiveDoc(COLL_A);
        var body = list("?limit=10");
        assertThat(body).containsEntry("count", 3).containsEntry("limit", 10).containsEntry("capped", false);
        var docs = documents(body);
        assertThat(docs).extracting(d -> d.get("tumbler")).containsExactly(a, b, c).isSorted();
        assertThat(docs).extracting(d -> d.get("physical_collection")).containsExactly(COLL_A, COLL_B, COLL_A);
        assertThat(docs.get(0).keySet()).containsExactlyInAnyOrder("tumbler", "physical_collection");
    }

    @Test
    void aDocumentThatIsNotSensitiveIsNotListed() throws Exception {
        String control = sensitiveDoc(COLL_A);
        doc(TOKEN, TENANT, COLL_A, false);
        var body = list("");
        assertThat(body).containsEntry("count", 1);
        assertThat(documents(body)).extracting(d -> d.get("tumbler")).containsExactly(control);
    }

    @Test
    void aTombstonedSensitiveDocumentIsNeitherListedNorCounted() throws Exception {
        String control = sensitiveDoc(COLL_A);
        String gone = sensitiveDoc(COLL_A);
        tombstone(TENANT, gone);
        var body = list("");
        assertThat(body).containsEntry("count", 1).containsEntry("capped", false);
        assertThat(documents(body)).extracting(d -> d.get("tumbler")).containsExactly(control);
    }

    @Test
    void anotherTenantsSensitiveDocumentsAreInvisible_andSoAreOurs() throws Exception {
        String ours = sensitiveDoc(COLL_A);
        String theirs = doc(TOKEN_B, TENANT_B, COLL_A, true);
        assertThat(documents(list("")))
            .extracting(d -> d.get("tumbler")).containsExactly(ours).doesNotContain(theirs);
        var resp = get(TOKEN_B, TENANT_B, "/v1/catalog/sensitive");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        var theirBody = mapper.readValue(resp.body(), MAP_T);
        assertThat(theirBody).containsEntry("count", 1);
        assertThat(documents(theirBody)).extracting(d -> d.get("tumbler")).containsExactly(theirs);
    }

    // ── the bound ───────────────────────────────────────────────────────────

    @Test
    void theListStopsAtTheLimit_theCountDoesNot_andCappedSaysSo() throws Exception {
        List<String> made = new ArrayList<>();
        for (int i = 0; i < 3; i++) made.add(sensitiveDoc(COLL_A));

        var one = list("?limit=1");
        assertThat(one).containsEntry("count", 3).containsEntry("limit", 1).containsEntry("capped", true);
        assertThat(documents(one)).extracting(d -> d.get("tumbler")).containsExactly(made.get(0));

        var two = list("?limit=2");
        assertThat(two).containsEntry("count", 3).containsEntry("capped", true);
        assertThat(documents(two)).hasSize(2);

        // exactly as many as the limit: not capped
        var three = list("?limit=3");
        assertThat(three).containsEntry("count", 3).containsEntry("capped", false);
        assertThat(documents(three)).hasSize(3);

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
            var resp = get(TOKEN, TENANT, "/v1/catalog/sensitive?limit=" + bad);
            assertThat(resp.statusCode()).as("limit=" + bad).isEqualTo(400);
        }
        assertThat(post(TOKEN, TENANT, "/v1/catalog/sensitive", "{}").statusCode()).isEqualTo(405);
    }

    @Test
    void aReadThatOutlastsItsStatementBoundAnswers503WithNoPartialBody_andTheNextReadAnswers() throws Exception {
        // Deterministic: another session holds ACCESS EXCLUSIVE on the documents table, so the statement waits on
        // the lock until its statement_timeout fires (a lock wait is bounded by statement_timeout too); nothing is
        // answered in part.
        String d = sensitiveDoc(COLL_A);
        System.setProperty(CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, "300");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_DOCUMENTS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            long t0 = System.nanoTime();
            var resp = get(TOKEN, TENANT, "/v1/catalog/sensitive");
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

    // ── the plan ────────────────────────────────────────────────────────────

    /**
     * The route is ONE statement: the list and the total come from the same snapshot (count(*) OVER () beside each
     * row), so a document flagged or tombstoned between two statements cannot make count and documents disagree
     * under READ COMMITTED. The statement is eligible for the partial index idx_catalog_documents_sensitive_live
     * (catalog-047-1): its predicate (sensitive AND deleted_at IS NULL) is implied by the route's WHERE, and its leading
     * column is the tenant. On a fixture this small the planner prefers a sequential scan, so that is switched off to
     * show the index is USABLE by this exact statement; the production plan on a large table is the planner's choice.
     */
    @Test
    void theOneStatementCanUseThePartialSensitiveIndex_andCarriesTheTotalAsAWindowCount() throws Exception {
        for (int i = 0; i < 12; i++) sensitiveDoc(i % 2 == 0 ? COLL_A : COLL_B);
        for (int i = 0; i < 40; i++) doc(TOKEN, TENANT, COLL_A, false);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
            PgContainerHelper.setPlannerGuc(su, "enable_seqscan", "off", false);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            var statement = CatalogSensitiveReads.listQuery(ctx, TENANT, 11);
            String plan = PgContainerHelper.explainPlanInlined(ctx, statement);
            System.out.println("EXPLAIN list (one statement):\n" + plan);
            assertThat(plan).contains("idx_catalog_documents_sensitive_live").contains("WindowAgg");
            assertThat(ctx.renderInlined(statement).toLowerCase()).contains("count(*) over ()");
        }
    }

    /** The window count is taken before the LIMIT: a page of 2 of 12 still reports 12, and an empty tenant reports 0. */
    @Test
    void theTotalIsTheWholeSetNotThePage() throws Exception {
        for (int i = 0; i < 12; i++) sensitiveDoc(COLL_A);
        var page = list("?limit=2");
        assertThat(page).containsEntry("count", 12).containsEntry("capped", true);
        assertThat(documents(page)).hasSize(2);
        for (var d : documents(page)) assertThat(d.get("physical_collection")).isEqualTo(COLL_A);
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
