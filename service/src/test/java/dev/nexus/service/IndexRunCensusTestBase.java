// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: the shared fixture of the two census routes' tests (the index-run census and the read-only next_seq drift):
 * a real PostgreSQL, the real engine behind its real routes, and direct jOOQ seeds of the tables the routes read, so each
 * test states exactly the fence columns, manifest rows and chunk rows it is about.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
abstract class IndexRunCensusTestBase {

    protected static final String TOKEN    = "index-run-census-test-token-f3yqd";
    protected static final String TOKEN_B  = "index-run-census-test-token-f3yqd-b";
    protected static final String SVC_ROLE = "svc_index_run_census_test";
    protected static final String SVC_PASS = "svc_index_run_census_test_pass";
    protected static final String TENANT   = TenantConstants.DEFAULT_TENANT;
    protected static final String TENANT_B = "tenant-b-f3yqd-census";
    protected static final String MODEL    = "bge-base-en-v15-768";
    /** The fixture's collection names ("c1") are these. */
    protected static final String C1 = "knowledge__census-c1__" + MODEL + "__v1";
    protected static final String C2 = "knowledge__census-c2__" + MODEL + "__v1";
    protected static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    protected PostgreSQLContainer<?> pg;
    protected NexusService service;
    protected HttpClient http;
    protected com.zaxxer.hikari.HikariDataSource svcDs;
    protected CatalogRepository repo;
    protected ObjectMapper mapper;
    private int seq;

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
        repo = new CatalogRepository(new TenantScope(svcDs));
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
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            dsl.deleteFrom(CATALOG_DOCUMENT_CHUNKS).execute();
            dsl.deleteFrom(CHUNKS).execute();
            dsl.deleteFrom(CATALOG_DOCUMENTS).execute();
            dsl.deleteFrom(CATALOG_COLLECTIONS).execute();
            dsl.deleteFrom(CATALOG_OWNERS).execute();
        }
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    protected void collection(String tenant, String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, name);
        }
    }

    protected byte[] nextChash() {
        int n = ++seq;
        byte[] b = new byte[32];
        b[0] = (byte) (n >> 8);
        b[1] = (byte) n;
        b[31] = 1;
        return b;
    }

    /** A chunk row of {@code collection} (registered) with {@code metadataJson}; its chash. */
    protected byte[] chunk(String tenant, String collection, String metadataJson) throws Exception {
        byte[] chash = nextChash();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(dsl, tenant, collection);
            dsl.insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, collection).set(CHUNKS.CHASH, chash)
                .set(CHUNKS.EMBEDDING_MODEL, model).set(CHUNKS.CHUNK_TEXT, "t")
                .set(CHUNKS.METADATA, JSONB.valueOf(metadataJson))
                .set(CHUNKS.EMBEDDING_768, VectorSeeds.vec(768, 0.1f)).execute();
        }
        return chash;
    }

    protected void manifest(String tenant, String tumbler, int position, String collection, byte[] chash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            dsl.insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, tumbler)
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position).set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, position)
                .set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
                .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(dsl, tenant, collection))
                .execute();
        }
    }

    /** The columns of one catalog document a census test sets; everything else is empty. */
    protected static final class Doc {
        String tumbler;
        String contentType = "code";
        String sourceUri = "";
        String filePath = "";
        String collection = "";
        int chunkCount = 0;
        String state;               // null = never stamped
        OffsetDateTime startedAt;
        OffsetDateTime indexedAt;
        String runId = "";
        String contentHash = "";
        String metaJson = "{}";
        boolean deleted;

        static Doc of(String tumbler) {
            Doc d = new Doc();
            d.tumbler = tumbler;
            d.sourceUri = "file:///census/" + tumbler + ".py";
            return d;
        }

        Doc state(String s) { this.state = s; return this; }
        Doc started(OffsetDateTime t) { this.startedAt = t; return this; }
        Doc indexed(OffsetDateTime t) { this.indexedAt = t; return this; }
        Doc run(String r) { this.runId = r; return this; }
        Doc collection(String c) { this.collection = c; return this; }
        Doc chunks(int n) { this.chunkCount = n; return this; }
        Doc fenceHash(String h) { this.contentHash = h; return this; }
        Doc meta(String json) { this.metaJson = json; return this; }
        Doc type(String t) { this.contentType = t; return this; }
        Doc uri(String u) { this.sourceUri = u; return this; }
        Doc tombstoned() { this.deleted = true; return this; }
    }

    protected void insert(String tenant, Doc d) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, d.tumbler)
                .set(CATALOG_DOCUMENTS.TITLE, "doc " + d.tumbler).set(CATALOG_DOCUMENTS.CONTENT_TYPE, d.contentType)
                .set(CATALOG_DOCUMENTS.FILE_PATH, d.filePath).set(CATALOG_DOCUMENTS.SOURCE_URI, d.sourceUri)
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, d.collection).set(CATALOG_DOCUMENTS.CHUNK_COUNT, d.chunkCount)
                .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf(d.metaJson))
                .set(CATALOG_DOCUMENTS.INDEX_STATE, d.state).set(CATALOG_DOCUMENTS.INDEX_RUN_ID, d.runId)
                .set(CATALOG_DOCUMENTS.INDEX_CONTENT_HASH, d.contentHash)
                .set(CATALOG_DOCUMENTS.INDEX_STARTED_AT, d.startedAt).set(CATALOG_DOCUMENTS.INDEXED_AT, d.indexedAt)
                .set(CATALOG_DOCUMENTS.DELETED_AT, d.deleted ? OffsetDateTime.parse("2026-11-01T00:00:00+00:00") : null)
                .execute();
        }
    }

    protected static OffsetDateTime at(String iso) {
        return OffsetDateTime.ofInstant(Instant.parse(iso), ZoneOffset.UTC);
    }

    protected static OffsetDateTime ago(long seconds) {
        return OffsetDateTime.now(ZoneOffset.UTC).minusSeconds(seconds);
    }

    // ── http ────────────────────────────────────────────────────────────────

    protected HttpResponse<String> get(String token, String tenant, String path) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + service.getPort() + path).header("X-Nexus-Tenant", tenant);
        if (token != null) b.header("Authorization", "Bearer " + token);
        return http.send(b.GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    protected HttpResponse<String> post(String token, String tenant, String path, String body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token).header("X-Nexus-Tenant", tenant)
            .header("Content-Type", "application/json").POST(java.net.http.HttpRequest.BodyPublishers.ofString(body)).build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    /** The 200 body of {@code GET /v1/catalog/index-run/census + query}. */
    protected Map<String, Object> census(String query) throws Exception {
        var resp = get(TOKEN, TENANT, "/v1/catalog/index-run/census" + query);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }

    /** The 200 body of {@code GET /v1/catalog/owners/next_seq_drift + query}. */
    protected Map<String, Object> drift(String query) throws Exception {
        var resp = get(TOKEN, TENANT, "/v1/catalog/owners/next_seq_drift" + query);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }

    @SuppressWarnings("unchecked")
    protected static Map<String, Object> bucket(Map<String, Object> body, String name) {
        return (Map<String, Object>) body.get(name);
    }

    @SuppressWarnings("unchecked")
    protected static List<Map<String, Object>> list(Map<String, Object> body, String key) {
        return (List<Map<String, Object>>) body.get(key);
    }

    protected JsonNode json(String resource) throws Exception {
        try (var in = getClass().getResourceAsStream("/" + resource)) {
            assertThat(in).as("resource " + resource).isNotNull();
            return mapper.readTree(in);
        }
    }
}
