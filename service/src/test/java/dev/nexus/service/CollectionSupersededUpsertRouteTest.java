// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.OwnerlessWriteActivity;
import dev.nexus.service.vectors.OwnerlessWriteMode;
import dev.nexus.service.vectors.PgVectorRepository;
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
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-mp8ys: {@code POST /v1/vectors/upsert-chunks} to a collection whose catalog row carries
 * {@code superseded_by} (a rename or supersede left the name as a tombstone) is refused with a
 * typed 409 {@code collection_superseded} naming the successor, before the ownership check, the
 * embedder and every write.
 *
 * <p>Not the "not registered" 422: a client's register-and-retry recognises that one and would
 * re-register, and so revive, the retired name. Each test below can fail on its own defect: the
 * refusal itself, its status (409, not 422), its order ahead of the ownerless 422, the stale
 * live-row cache entry a supersede must evict, and the control that a live sibling still accepts
 * the same request shape.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionSupersededUpsertRouteTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN = "tok-msup-0123456789abcdef0123456789abcdef";
    private static final String SVC_ROLE = "svc_msup";
    private static final String SVC_PASS = "svc_msup_pass";
    private static final String TENANT = "msup-tenant";

    private static final String LIVE = "knowledge__msup-live__voyage-context-3__v1";
    private static final String DEAD = "knowledge__msup-dead__voyage-context-3__v1";
    private static final String SUCCESSOR = "knowledge__msup-dead__voyage-context-3__v2";
    private static final String WARM = "knowledge__msup-warm__voyage-context-3__v1";
    private static final String WARM_SUCCESSOR = "knowledge__msup-warm__voyage-context-3__v2";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;
    PgVectorRepository repo;
    CatalogRepository catalog;
    ProbeEmbedder embedder;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "msup-test");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        embedder = new ProbeEmbedder();
        var scope = new TenantScope(svcDs);
        repo = new PgVectorRepository(scope, embedder, embedder);
        catalog = new CatalogRepository(scope);
        service = new NexusService(0, TOKEN, svcDs, null, repo);
        service.start();
        http = TestHttp.client();

        // Burn the per-tenant ghost sweep before registering collections (see
        // OwnerlessWriteRefusalTest for the measured ordering trap).
        http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
            .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, LIVE);
            PgContainerHelper.insertCollection(dsl, TENANT, DEAD);
            PgContainerHelper.insertCollection(dsl, TENANT, WARM);
            // DEAD is retired by a direct row write: no process cache ever held it live.
            dsl.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, SUCCESSOR)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(DEAD))
                .execute();
        }
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void reset() {
        service.ownerlessWritePolicy().set(OwnerlessWriteMode.ENFORCE);
        OwnerlessWriteActivity.resetForTests();
    }

    private HttpResponse<String> upsert(String collection, String chash, String text) throws Exception {
        var body = new LinkedHashMap<String, Object>();
        body.put("collection", collection);
        body.put("ids", List.of(chash));
        body.put("documents", List.of(text));
        body.put("metadatas", List.of(Map.of()));
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/upsert-chunks")
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> json(HttpResponse<String> resp) throws Exception {
        return MAPPER.readValue(resp.body(), Map.class);
    }

    private long storedCount(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).selectCount().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(collection))
                .fetchOne(0, Long.class);
        }
    }

    private void assertSupersededRefusal(HttpResponse<String> resp, String collection, String successor)
            throws Exception {
        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(409);
        Map<String, Object> body = json(resp);
        assertThat(body.get("error")).isEqualTo("collection_superseded");
        assertThat(body.get("reason")).isEqualTo("collection_superseded");
        assertThat(body.get("collection")).isEqualTo(collection);
        assertThat(body.get("superseded_by")).isEqualTo(successor);
        assertThat((String) body.get("message")).contains(collection).contains(successor)
            .doesNotContain("is not registered").doesNotContain("register it first");
    }

    @Test
    void aWriteToARetiredName_is409NamingTheSuccessor_andStrandsNothingUnderTheTombstone() throws Exception {
        var resp = upsert(DEAD, Chash.ofText("msup-dead-1").toHex(), "text for a retired name");

        assertSupersededRefusal(resp, DEAD, SUCCESSOR);
        assertThat(storedCount(DEAD)).as("nothing strands under the tombstone").isZero();
    }

    @Test
    void theSupersededRefusal_comesAheadOfTheOwnerless422() throws Exception {
        // The chash has no manifest row, which on a live collection is the ownerless 422. On a
        // retired name the caller must learn the name is retired, not be sent to the combined routes.
        var resp = upsert(DEAD, Chash.ofText("msup-dead-ownerless").toHex(), "ownerless text");

        assertSupersededRefusal(resp, DEAD, SUCCESSOR);
        assertThat(OwnerlessWriteActivity.refusedTotal()).isZero();
    }

    @Test
    void aSupersedeCommittedAfterTheRegistryWarmedTheLiveRow_isRefusedOnTheNextWrite() throws Exception {
        // The race the refusal exists for: this process already cached WARM as live (an earlier
        // write), then the name is retired. Served from the cache, the next write would pass and
        // strand its chunk. Fails if supersedeCollection stops evicting the cached row.
        String owned = Chash.ofText("msup-warm-owned").toHex();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertOwnedChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, WARM, 1024, owned);
        }
        var warm = upsert(WARM, owned, "refreshed text");
        assertThat(warm.statusCode()).as("the live write that warms the cache, body: %s", warm.body()).isEqualTo(200);

        catalog.supersedeCollection(TENANT, WARM, WARM_SUCCESSOR, "");

        assertSupersededRefusal(upsert(WARM, owned, "text after the retire"), WARM, WARM_SUCCESSOR);
    }

    @Test
    void control_aLiveSiblingStillAcceptsTheSameRequestShape() throws Exception {
        String owned = Chash.ofText("msup-live-owned").toHex();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertOwnedChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, LIVE, 1024, owned);
        }

        var resp = upsert(LIVE, owned, "refreshed text");

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(200);
    }

    private static final class ProbeEmbedder implements Embedder {

        @Override
        public String modelToken() {
            return "voyage-context-3";
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>(texts.size());
            for (String ignored : texts) {
                float[] v = new float[1024];
                v[0] = 1f;
                out.add(v);
            }
            return out;
        }

        @Override
        public void close() {
        }
    }
}
