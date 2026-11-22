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
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;


/**
 * nexus-f3yqd: the shared fixture of the manifest-fillable route tests (the route, guard-kill and plan tests): a real
 * PostgreSQL, the real engine, and builders for the one situation a guard exists for. Moved out of
 * {@link ManifestFillableRouteTest} so each test class stays under the test-file size cap.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
abstract class ManifestFillableTestBase {

    protected static final String TOKEN    = "manifest-fillable-test-token-f3yqd";
    protected static final String TOKEN_B  = "manifest-fillable-test-token-f3yqd-b";
    protected static final String SVC_ROLE = "svc_manifest_fillable_test";
    protected static final String SVC_PASS = "svc_manifest_fillable_test_pass";
    protected static final String TENANT   = TenantConstants.DEFAULT_TENANT;
    protected static final String TENANT_B = "tenant-b-f3yqd-fill";
    protected static final String COLL     = "knowledge__f3yqd-fill-owner__v1";
    protected static final String DOC_AT   = "2026-10-01T12:00:00+00:00";
    protected static final String OK_AT    = "2026-10-01T11:59:00+00:00";
    protected static final List<String> KINDS = List.of("labels", "spans");
    protected static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    protected PostgreSQLContainer<?> pg;
    protected NexusService service;
    protected HttpClient http;
    protected com.zaxxer.hikari.HikariDataSource svcDs;
    protected ObjectMapper mapper;
    protected int seq;
    /** The indexed_at each document should carry: the engine stamps "now" on every manifest write, so it is put back after one. */
    protected final Map<String, String> wantedStamp = new java.util.HashMap<>();

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
        }
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    protected String doc(String token, String tenant, String title, String filePath, String indexedAt, String meta) throws Exception {
        String owner = "f3yqd.fill" + (seq++);
        var resp = post(token, tenant, "/v1/catalog/doc/register", mapper.writeValueAsString(Map.of(
            "owner_prefix", owner, "title", title, "content_type", "prose",
            "source_uri", "file:///f3yqd/" + owner + ".md", "physical_collection", COLL)));
        assertThat(resp.statusCode()).as("register: " + resp.body()).isEqualTo(200);
        String tumbler = (String) mapper.readValue(resp.body(), MAP_T).get("tumbler");
        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.FILE_PATH, filePath)
                .set(CATALOG_DOCUMENTS.INDEXED_AT, indexedAt == null ? null : OffsetDateTime.parse(indexedAt))
                .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf(meta))
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
                .execute();
            assertThat(n).isEqualTo(1);
        }
        wantedStamp.put(tumbler, indexedAt);
        return tumbler;
    }

    protected String doc(String title) throws Exception {
        return doc(TOKEN, TENANT, title, "", DOC_AT, "{}");
    }

    protected void restamp(String tenant, String docId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            String at = wantedStamp.get(docId);
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.INDEXED_AT, at == null ? null : OffsetDateTime.parse(at))
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(docId)))
                .execute();
            assertThat(n).isEqualTo(1);
        }
    }

    protected void chunk(String tenant, String chash, String metadataJson) throws Exception {
        chunkIn(tenant, COLL, chash, metadataJson);
    }

    /** A chunk row of {@code collection} (registered here if it is not), with {@code metadataJson}; the vector is a stub. */
    protected void chunkIn(String tenant, String collection, String chash, String metadataJson) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, tenant, collection);
            String model = PgContainerHelper.collectionModel(dsl, tenant, collection);
            dsl.insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, tenant)
                .set(CHUNKS.COLLECTION, collection)
                .set(CHUNKS.CHASH, HexFormat.of().parseHex(chash))
                .set(CHUNKS.EMBEDDING_MODEL, model)
                .set(CHUNKS.CHUNK_TEXT, "stub")
                .set(CHUNKS.EMBEDDING_768, VectorSeeds.vec(768, 0.1f))
                .onConflict(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL).doNothing()
                .execute();
            int n = dsl.update(CHUNKS).set(CHUNKS.METADATA, JSONB.valueOf(metadataJson))
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                    .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(chash))))
                .execute();
            assertThat(n).isEqualTo(1);
        }
    }

    /** The manifest rows of a document, one per chash (positions 0..n-1), with no label key and NULL spans: legacy rows. */
    protected void manifestAs(String token, String tenant, String docId, String... chashes) throws Exception {
        var rows = new java.util.ArrayList<Map<String, Object>>();
        for (int i = 0; i < chashes.length; i++) rows.add(Map.of("position", i, "chash", chashes[i]));
        var resp = post(token, tenant, "/v1/catalog/manifest/write",
            mapper.writeValueAsString(Map.of("doc_id", docId, "collection", COLL, "rows", rows)));
        assertThat(resp.statusCode()).as("manifest write: " + resp.body()).isEqualTo(200);
        restamp(tenant, docId);
    }

    protected void manifest(String docId, String... chashes) throws Exception {
        manifestAs(TOKEN, TENANT, docId, chashes);
    }

    /** Sets the manifest span columns of one row (null leaves a column NULL). */
    protected void spans(String docId, int position, Integer lineStart, Integer lineEnd, Integer charStart, Integer charEnd) throws Exception {
        try (Connection su = pg.createConnection("")) {
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.LINE_START, lineStart).set(CATALOG_DOCUMENT_CHUNKS.LINE_END, lineEnd)
                .set(CATALOG_DOCUMENT_CHUNKS.CHAR_START, charStart).set(CATALOG_DOCUMENT_CHUNKS.CHAR_END, charEnd)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))
                    .and(CATALOG_DOCUMENT_CHUNKS.POSITION.eq(position)))
                .execute();
            assertThat(n).isEqualTo(1);
        }
        restamp(TENANT, docId);
    }

    protected void tombstone(String docId) throws Exception {
        var del = post(TOKEN, TENANT, "/v1/catalog/delete", mapper.writeValueAsString(Map.of("tumbler", docId)));
        assertThat(del.statusCode()).as(del.body()).isEqualTo(200);
    }

    protected static String h(int n) {
        return String.format("%02x", n).repeat(32);
    }

    /** A chunk row that both rungs could fill from: a label and a line span, stamped at {@code stamp}. */
    protected static String both(String stamp) {
        return "{\"section_title\":\"Intro\",\"line_start\":5,\"indexed_at\":\"" + stamp + "\"}";
    }

    protected static String both(String stamp, String title) {
        return "{\"section_title\":\"Intro\",\"line_start\":5,\"indexed_at\":\"" + stamp + "\",\"title\":\"" + title + "\"}";
    }

    protected static String labelsOnly(String stamp, String extraKeys) {
        return "{" + extraKeys + ",\"indexed_at\":\"" + stamp + "\"}";
    }

    protected Map<String, Object> fillable(String kind, int limit) throws Exception {
        var resp = get(TOKEN, TENANT, "/v1/catalog/manifest/fillable?kind=" + kind + "&limit=" + limit);
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }

    protected int fillable(String kind) throws Exception {
        return ((Number) fillable(kind, 1000).get("fillable")).intValue();
    }

    protected int oldLiveCount() throws Exception {
        var resp = get(TOKEN, TENANT, "/v1/catalog/manifest/section_label_fillable");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return ((Number) mapper.readValue(resp.body(), MAP_T).get("fillable")).intValue();
    }

    /**
     * Adds the control, a document whose single row IS fillable by both kinds, and returns the count the situation
     * alone adds to {@code kind} (the total minus the control's 1).
     */
    protected int beside(String kind) throws Exception {
        String d = doc("control doc");
        chunk(TENANT, h(0xc0), both(OK_AT));
        manifest(d, h(0xc0));
        return fillable(kind) - 1;
    }

    /** The shared guard: with the fixture {@code setup} builds, neither kind counts anything beyond its control. */
    protected void assertNotCountedByEitherKind(String why) throws Exception {
        for (String kind : KINDS) {
            assertThat(fillable(kind)).as(why + " (" + kind + ", no control yet)").isEqualTo(0);
        }
        String d = doc("control doc");
        chunk(TENANT, h(0xc0), both(OK_AT));
        manifest(d, h(0xc0));
        for (String kind : KINDS) {
            assertThat(fillable(kind)).as(why + " (" + kind + ", beside a control)").isEqualTo(1);
        }
    }

    // ── http ────────────────────────────────────────────────────────────────

    protected HttpResponse<String> post(String token, String tenant, String path, String body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    protected HttpResponse<String> get(String token, String tenant, String path) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .GET()
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
