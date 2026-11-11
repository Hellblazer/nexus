// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
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
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd: the shared fixture of the stub-metadata route tests: a real PostgreSQL, the real engine, and direct jOOQ
 * seeds of the four tables the rule reads (collection registry, documents, manifest, chunk rows), so each test states
 * exactly the content type, path, URI, document metadata and chunk metadata it is about.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
abstract class StubMetadataTestBase {

    protected static final String TOKEN    = "stub-metadata-test-token-f3yqd";
    protected static final String TOKEN_B  = "stub-metadata-test-token-f3yqd-b";
    protected static final String SVC_ROLE = "svc_stub_metadata_test";
    protected static final String SVC_PASS = "svc_stub_metadata_test_pass";
    protected static final String TENANT   = TenantConstants.DEFAULT_TENANT;
    protected static final String TENANT_B = "tenant-b-f3yqd-stub";
    protected static final String MODEL    = "bge-base-en-v15-768";
    protected static final String KNOW_A   = "knowledge__stubs-a__" + MODEL + "__v1";
    protected static final String KNOW_B   = "knowledge__stubs-b__" + MODEL + "__v1";
    protected static final String DOCS_A   = "docs__stubs-a__" + MODEL + "__v1";
    protected static final String CODE_A   = "code__stubs-a__" + MODEL + "__v1";
    protected static final String RDR_A    = "rdr__stubs-a__" + MODEL + "__v1";
    protected static final String STUB     = "{\"title\":\"\",\"content_hash\":\"deadbeefcafe0123\"}";
    protected static final String HEALTHY  = "{\"title\":\"A Paper\",\"content_hash\":\"deadbeefcafe0123\"}";
    protected static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    protected PostgreSQLContainer<?> pg;
    protected NexusService service;
    protected HttpClient http;
    protected com.zaxxer.hikari.HikariDataSource svcDs;
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
            dsl.deleteFrom(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS).execute();
        }
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    protected void collection(String tenant, String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, name);
        }
    }

    /** A 32-byte chash that is unique per call. */
    protected byte[] nextChash() {
        int n = ++seq;
        byte[] b = new byte[32];
        b[0] = (byte) (n >> 8);
        b[1] = (byte) n;
        b[31] = 1;
        return b;
    }

    /** A chunk row of {@code collection} with {@code metadataJson} (the collection must be registered). */
    protected void chunk(String tenant, String collection, byte[] chash, String metadataJson) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(dsl, tenant, collection);
            dsl.insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, collection).set(CHUNKS.CHASH, chash)
                .set(CHUNKS.EMBEDDING_MODEL, model).set(CHUNKS.CHUNK_TEXT, "stub")
                .set(CHUNKS.EMBEDDING_768, VectorSeeds.vec(768, 0.1f))
                .set(CHUNKS.METADATA, JSONB.valueOf(metadataJson))
                .execute();
        }
    }

    /** A catalog document row; {@code deleted} makes it a tombstone. */
    protected void document(String tenant, String tumbler, String collection, String contentType, String filePath,
                            String sourceUri, String metadataJson, boolean deleted) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
                .set(CATALOG_DOCUMENTS.TITLE, "doc " + tumbler).set(CATALOG_DOCUMENTS.CONTENT_TYPE, contentType)
                .set(CATALOG_DOCUMENTS.FILE_PATH, filePath).set(CATALOG_DOCUMENTS.SOURCE_URI, sourceUri)
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
                .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf(metadataJson))
                .set(CATALOG_DOCUMENTS.DELETED_AT, deleted ? OffsetDateTime.parse("2026-11-01T00:00:00+00:00") : null)
                .execute();
        }
    }

    /** One manifest row of {@code tumbler} at {@code position}, naming {@code chash} in {@code collection}. */
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

    private String nextTumbler() {
        return "7." + (++seq) + ".1";
    }

    /**
     * A PDF document {@code name}.pdf (no extraction_method) in {@code collection} holding one chunk with
     * {@code chunkMetadata}; its tumbler. The collection is registered here.
     */
    protected String pdfWithChunk(String tenant, String collection, String name, String chunkMetadata) throws Exception {
        collection(tenant, collection);
        byte[] chash = nextChash();
        chunk(tenant, collection, chash, chunkMetadata);
        String tumbler = nextTumbler();
        document(tenant, tumbler, collection, "pdf", "/stubs/" + name + ".pdf", "file:///stubs/" + name + ".pdf", "{}", false);
        manifest(tenant, tumbler, 0, collection, chash);
        return tumbler;
    }

    protected String stubPdf(String collection, String name) throws Exception {
        return pdfWithChunk(TENANT, collection, name, STUB);
    }

    // ── http ────────────────────────────────────────────────────────────────

    protected HttpResponse<String> get(String token, String tenant, String path) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + service.getPort() + path).header("X-Nexus-Tenant", tenant);
        if (token != null) b.header("Authorization", "Bearer " + token);
        return http.send(b.GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    /** The 200 body of {@code GET /v1/catalog/stub-metadata + query}. */
    protected Map<String, Object> stubs(String query) throws Exception {
        var resp = get(TOKEN, TENANT, "/v1/catalog/stub-metadata" + query);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }

    @SuppressWarnings("unchecked")
    protected static List<Map<String, Object>> collections(Map<String, Object> body) {
        return (List<Map<String, Object>>) body.get("collections");
    }

    /** The entry of {@code collection}, which must be listed. */
    protected static Map<String, Object> entry(Map<String, Object> body, String collection) {
        return collections(body).stream().filter(c -> collection.equals(c.get("collection"))).findFirst()
            .orElseThrow(() -> new AssertionError("no entry for " + collection + " in " + body));
    }
}
