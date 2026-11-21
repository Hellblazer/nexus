// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.MintedLabel;
import dev.nexus.service.db.TenantConstants;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.Map;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28), the engine mint through its routes: {@code POST /v1/catalog/collections/upsert} with
 * no name mints and returns the row; no route accepts a client-supplied {@code collection_id} (the upsert and the ETL
 * import answer 400 and write nothing); an import row or a named upsert on a live tuple another collection already holds is a 409 that
 * names both collections, with the partial UNIQUE index present and absent alike; and a mint's refusals (a label of the minted form in
 * the way for every draw, an id taken even after the heal) are 409, not a 500. The restore is
 * {@link CollectionMintRestoreTest}; the mint itself is {@link CollectionMintTest}. Ordered: the last tests move the
 * {@code collection_id} sequence.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CollectionMintHttpTest {

    private static final String TOKEN = "collection-mint-http-token-p38";
    private static final String SVC_ROLE = "svc_mint_http";
    private static final String SVC_PASS = "svc_mint_http_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final String MODEL = "voyage-context-3";
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "test-bound");
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
        // the tenant's first request runs the once-per-process ghost sweep, which deletes a chunkless registered collection
        http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
            .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void indexAbsent() {
        su(ctx -> {
            CollectionMintSupport.dropIndex(ctx);
            return null;
        });
    }

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private void createIndex() {
        su(ctx -> {
            CollectionMintSupport.createIndex(ctx);
            return null;
        });
    }

    private static String attrsJson(String owner, String extra) {
        return "\"content_type\":\"knowledge\",\"owner_id\":\"" + owner + "\",\"embedding_model\":\"" + MODEL
            + "\",\"model_version\":\"v1\"" + extra;
    }

    private long rowsOwnedBy(String owner) {
        return su(ctx -> (long) ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
            .and(CATALOG_COLLECTIONS.OWNER_ID.eq(owner))));
    }

    private long nextSequenceValue() {
        return su(CollectionMintSupport::nextSequenceValue);
    }

    // ── mint through the route ──────────────────────────────────────────────────────────────────

    @Test @Order(1)
    void aNamelessUpsertMintsAndAnswersTheRow() throws Exception {
        var resp = post("/v1/catalog/collections/upsert", "{" + attrsJson("http-mint-1", "") + "}");

        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        var row = mapper.readValue(resp.body(), MAP_T);
        long id = ((Number) row.get("collection_id")).longValue();
        assertThat(row.get("name")).isEqualTo("c" + id);
        assertThat(row.get("owner_id")).isEqualTo("http-mint-1");
        assertThat(row.get("content_type")).isEqualTo("knowledge");
        assertThat(row.get("embedding_model")).isEqualTo(MODEL);
        assertThat(row.get("model_version")).isEqualTo("v1");
        assertThat(row.get("ok")).as("the old answer's key stays").isEqualTo(true);

        var again = mapper.readValue(post("/v1/catalog/collections/upsert", "{" + attrsJson("http-mint-1", "") + "}").body(), MAP_T);
        assertThat(((Number) again.get("collection_id")).longValue()).isEqualTo(id);
        assertThat(rowsOwnedBy("http-mint-1")).isEqualTo(1);
    }

    @Test @Order(2)
    void aNamedUpsertKeepsItsAnswerAndTheSequenceAssignsItsId() throws Exception {
        var resp = post("/v1/catalog/collections/upsert",
            "{\"name\":\"named-p38\"," + attrsJson("http-named-1", "") + "}");

        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        assertThat(mapper.readValue(resp.body(), MAP_T)).containsOnlyKeys("ok");
        Long id = su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq("named-p38"))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
        assertThat(id).as("BY DEFAULT identity assigned it").isNotNull();
    }

    // ── no route accepts a collection_id ────────────────────────────────────────────────────────

    @Test @Order(3)
    void theUpsertRefusesABodyCarryingACollectionId_named_and_nameless() throws Exception {
        var nameless = post("/v1/catalog/collections/upsert", "{" + attrsJson("http-refuse-1", ",\"collection_id\":777001") + "}");
        var named = post("/v1/catalog/collections/upsert",
            "{\"name\":\"refused-named-p38\"," + attrsJson("http-refuse-2", ",\"collection_id\":777002") + "}");

        assertThat(nameless.statusCode()).as(nameless.body()).isEqualTo(400);
        assertThat(nameless.body()).contains("collection_id");
        assertThat(named.statusCode()).as(named.body()).isEqualTo(400);
        assertThat(named.body()).contains("collection_id");
        assertThat(rowsOwnedBy("http-refuse-1") + rowsOwnedBy("http-refuse-2")).as("nothing was written").isZero();
    }

    @Test @Order(3)
    void aRefusedCollectionIdSeedsNoEmbeddingProfileRow() throws Exception {
        // The seed is a write (a profile row for the request's content type) that used to run before the refusal. A request the
        // route refuses for its collection_id writes nothing at all.
        String type = "p38-seed-probe-refused";
        var refused = post("/v1/catalog/collections/upsert",
            "{\"content_type\":\"" + type + "\",\"owner_id\":\"http-seed-1\",\"embedding_model\":\"" + MODEL
                + "\",\"model_version\":\"v1\",\"collection_id\":777005}");
        assertThat(refused.statusCode()).as(refused.body()).isEqualTo(400);
        assertThat(profileRows(type)).as("a refused request seeds no profile row").isZero();

        // non-vacuity: the same request without the id does reach the seed, so the zero above is the refusal's doing
        String controlType = "p38-seed-probe-control";
        post("/v1/catalog/collections/upsert",
            "{\"content_type\":\"" + controlType + "\",\"owner_id\":\"http-seed-2\",\"embedding_model\":\"" + MODEL
                + "\",\"model_version\":\"v1\"}");
        assertThat(profileRows(controlType)).as("guard: a request that is not refused seeds one").isEqualTo(1L);
    }

    private long profileRows(String contentType) {
        return su(ctx -> (long) ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE,
            dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE.TENANT_ID.eq(TENANT)
                .and(dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE.CONTENT_TYPE.eq(contentType))));
    }

    @Test @Order(4)
    void theImportRefusesARowCarryingACollectionId_single_and_batch() throws Exception {
        var single = post("/v1/catalog/import/collection",
            "{\"name\":\"imp-refused-1\"," + attrsJson("http-imp-refuse-1", ",\"collection_id\":777003") + "}");
        var batch = post("/v1/catalog/import/collection",
            "{\"rows\":[{\"name\":\"imp-refused-2\"," + attrsJson("http-imp-refuse-2", ",\"collection_id\":777004") + "}]}");

        assertThat(single.statusCode()).as(single.body()).isEqualTo(400);
        assertThat(single.body()).contains("collection_id");
        assertThat(batch.statusCode()).as(batch.body()).isEqualTo(400);
        assertThat(batch.body()).contains("collection_id");
        assertThat(rowsOwnedBy("http-imp-refuse-1") + rowsOwnedBy("http-imp-refuse-2")).as("nothing was written").isZero();
    }

    // ── an import row on a live tuple another collection holds ──────────────────────────────────

    @Test @Order(5)
    void withTheIndexPresentAnImportRowOnAHeldLiveTupleIs409NamingBothCollections() throws Exception {
        post("/v1/catalog/collections/upsert", "{\"name\":\"held-p38\"," + attrsJson("http-held-1", "") + "}");
        createIndex();

        var resp = post("/v1/catalog/import/collection", "{\"name\":\"intruder-p38\"," + attrsJson("http-held-1", "") + "}");

        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(409);
        assertThat(resp.body()).contains("held-p38").contains("intruder-p38");
        assertThat(rowsOwnedBy("http-held-1")).as("the held row only").isEqualTo(1);
    }

    @Test @Order(6)
    void withTheIndexPresentTwoNewImportRowsOnOneTupleAre409NamingBoth() throws Exception {
        createIndex();

        var resp = post("/v1/catalog/import/collection",
            "{\"rows\":[{\"name\":\"twin-a-p38\"," + attrsJson("http-twins-1", "") + "},"
                + "{\"name\":\"twin-b-p38\"," + attrsJson("http-twins-1", "") + "}]}");

        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(409);
        assertThat(resp.body()).contains("twin-a-p38").contains("twin-b-p38");
        assertThat(rowsOwnedBy("http-twins-1")).as("the batch is refused whole").isZero();
    }

    @Test @Order(7)
    void withTheIndexAbsentAnImportRowOnAHeldLiveTupleIsRefusedToo_noSecondLiveRowIsEverMade() throws Exception {
        post("/v1/catalog/collections/upsert", "{\"name\":\"held-2-p38\"," + attrsJson("http-held-2", "") + "}");

        var resp = post("/v1/catalog/import/collection", "{\"name\":\"intruder-2-p38\"," + attrsJson("http-held-2", "") + "}");

        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(409);
        assertThat(resp.body()).contains("held-2-p38").contains("intruder-2-p38");
        assertThat(rowsOwnedBy("http-held-2")).as("the held row only").isEqualTo(1);
    }

    @Test @Order(8)
    void withTheIndexPresentAnImportRowOnAFreeTupleOrAHeldNameStillLands() throws Exception {
        createIndex();
        post("/v1/catalog/collections/upsert", "{\"name\":\"held-3-p38\"," + attrsJson("http-held-3", "") + "}");

        var free = post("/v1/catalog/import/collection", "{\"name\":\"free-p38\"," + attrsJson("http-free-1", "") + "}");
        var sameName = post("/v1/catalog/import/collection", "{\"name\":\"held-3-p38\"," + attrsJson("http-held-3", "") + "}");

        assertThat(free.statusCode()).as(free.body()).isEqualTo(200);
        assertThat(sameName.statusCode()).as("an import of a name already registered is a no-op, as before").isEqualTo(200);
        assertThat(rowsOwnedBy("http-held-3")).isEqualTo(1);
    }

    @Test @Order(11)
    void withTheIndexAbsentTwoNewImportRowsOnOneTupleAre409NamingBoth() throws Exception {
        var resp = post("/v1/catalog/import/collection",
            "{\"rows\":[{\"name\":\"twin-c-p38\"," + attrsJson("http-twins-2", "") + "},"
                + "{\"name\":\"twin-d-p38\"," + attrsJson("http-twins-2", "") + "}]}");

        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(409);
        assertThat(resp.body()).contains("twin-c-p38").contains("twin-d-p38");
        assertThat(rowsOwnedBy("http-twins-2")).as("the batch is refused whole").isZero();
    }

    @Test @Order(12)
    void aBatchImportRowOnAHeldLiveTupleIs409NamingBoth_inBothIndexStates() throws Exception {
        post("/v1/catalog/collections/upsert", "{\"name\":\"held-4-p38\"," + attrsJson("http-held-4", "") + "}");
        String batch = "{\"rows\":[{\"name\":\"intruder-4-p38\"," + attrsJson("http-held-4", "") + "}]}";

        var absent = post("/v1/catalog/import/collection", batch);
        createIndex();
        var present = post("/v1/catalog/import/collection", batch);

        for (var resp : java.util.List.of(absent, present)) {
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(409);
            assertThat(resp.body()).contains("held-4-p38").contains("intruder-4-p38");
        }
        assertThat(rowsOwnedBy("http-held-4")).as("the held row only").isEqualTo(1);
    }

    // ── a named upsert onto a live tuple another collection holds ───────────────────────────────

    @Test @Order(13)
    void aNamedUpsertOntoAHeldLiveTupleIs409NamingBoth_inBothIndexStates() throws Exception {
        post("/v1/catalog/collections/upsert", "{\"name\":\"held-5-p38\"," + attrsJson("http-held-5", "") + "}");
        String body = "{\"name\":\"intruder-5-p38\"," + attrsJson("http-held-5", "") + "}";

        var absent = post("/v1/catalog/collections/upsert", body);
        createIndex();
        var present = post("/v1/catalog/collections/upsert", body);

        for (var resp : java.util.List.of(absent, present)) {
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(409);
            assertThat(resp.body()).as("the refusal names both collections, not a constraint").contains("held-5-p38").contains("intruder-5-p38");
        }
        assertThat(rowsOwnedBy("http-held-5")).as("the held row only").isEqualTo(1);
    }

    // ── a mint's refusals are 409 ───────────────────────────────────────────────────────────────

    @Test @Order(30)
    void everyDrawnLabelLiveIs409() throws Exception {
        long next = nextSequenceValue();
        int draws = MintedLabel.MAX_DRAWS;
        for (int k = 0; k < draws; k++) {
            String name = "c" + (next + draws + k);
            String owner = "http-labels-" + k;
            su(ctx -> {
                CollectionMintSupport.insertLiveRow(ctx, TENANT, name, owner, MODEL, null);
                return null;
            });
        }

        var resp = post("/v1/catalog/collections/upsert", "{" + attrsJson("http-labels-mint", "") + "}");

        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(409);
        assertThat(resp.body()).contains("c" + (next + draws));
        assertThat(rowsOwnedBy("http-labels-mint")).isZero();
    }

    @Test @Order(31)
    void aTakenIdAfterTheHealIs409() throws Exception {
        su(ctx -> {
            CollectionMintSupport.cycleOverTheRows(ctx, TENANT, "cycle-anchor-p38", MODEL);
            return null;
        });

        var resp = post("/v1/catalog/collections/upsert", "{" + attrsJson("http-cycle-mint", "") + "}");

        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(409);
        assertThat(resp.body()).as("the refusal is the mint's, not a constraint violation").contains("sequence");
        assertThat(rowsOwnedBy("http-cycle-mint")).isZero();
    }

    private HttpResponse<String> post(String path, String body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
