// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantConstants;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.stream.IntStream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-ocf52 / nexus-uu4b9 / nexus-b9puj — HTTP envelope coverage for
 * {@code POST /v1/catalog/manifest/docs_for_chashes} and
 * {@code POST /v1/catalog/manifest/get_many}
 * ({@link dev.nexus.service.http.CatalogHandler#handleDocsForChashes},
 * {@link dev.nexus.service.http.CatalogHandler#handleManifestGetMany}).
 *
 * <p>Both endpoints previously emitted a single-key envelope
 * ({@code {"tumblers": [...]}} / {@code {"manifests": {...}}}) with no
 * accompanying {@code count}. Per the nexus-ir6eh precedent
 * ({@code handleManifestChashes}'s {@code chashes}/{@code count} pair), the
 * count is a TRUNCATION DEFENCE, not a convenience: the client reconciles
 * {@code len(payload) == count} before trusting a page as complete — a
 * partially-delivered {@code docs_for_chashes} list can cause the
 * superseded-vector sweep to hard-delete a still-referenced T3 row
 * (nexus-ocf52), and a partially-delivered {@code get_many} page feeds that
 * same reconciliation one hop upstream (nexus-b9puj). nexus-uu4b9
 * additionally closes a missing batch cap on {@code docs_for_chashes} —
 * mirrors {@code handleManifestGetMany}'s existing {@code MAX_BATCH_DOC_IDS}
 * guard and 400 error shape.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerManifestEnvelopeTest {

    private static final String TOKEN    = "manifest-envelope-test-token-xyz456";
    private static final String SVC_ROLE = "svc_manifest_envelope_test";
    private static final String SVC_PASS = "svc_manifest_envelope_test_pass";
    private static final String TENANT   = TenantConstants.DEFAULT_TENANT;
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
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su)));
            lb.update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES ('"
                + dev.nexus.service.db.TokenHashing.sha256Hex(TOKEN)
                + "', '" + TENANT + "', 'test-bound') ON CONFLICT (token_hash) DO NOTHING");
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
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
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    private String registerDoc(String ownerPrefix, String title, String sourceUri) throws Exception {
        // nexus-7nrvr: real collection — ghost-ness was incidental (both
        // callers of this helper are about envelope count-reconciliation,
        // not about the ghost/no-collection path).
        var resp = post("/v1/catalog/doc/register",
            mapper.writeValueAsString(Map.of(
                "owner_prefix", ownerPrefix, "title", title,
                "content_type", "prose", "source_uri", sourceUri,
                "physical_collection", "knowledge__" + ownerPrefix.replace('.', '-') + "__v1")));
        assertThat(resp.statusCode()).as("doc registration must succeed: " + resp.body()).isEqualTo(200);
        return (String) mapper.readValue(resp.body(), MAP_T).get("tumbler");
    }

    private void writeManifestRow(String docId, String collection, String chash) throws Exception {
        // RDR-191 (Hal ruling 2026-08-12): 'collection' is required, caller-
        // supplied, and stamped verbatim -- pass the SAME value the owning
        // doc was registered under (registerDoc's physical_collection), no
        // inference happens engine-side any more.
        var resp = post("/v1/catalog/manifest/write",
            mapper.writeValueAsString(Map.of(
                "doc_id", docId,
                "collection", collection,
                "rows", List.of(Map.of("position", 0, "chash", chash)))));
        assertThat(resp.statusCode()).as("manifest write must succeed: " + resp.body()).isEqualTo(200);
    }

    // ── nexus-ocf52: docs_for_chashes carries count ────────────────────────────

    @Test
    void docsForChashes_multiDoc_countMatchesTumblersLength() throws Exception {
        var c1 = ch("ocf52-doc1-chunk");
        var c2 = ch("ocf52-doc2-chunk");
        var t1 = registerDoc("ocf52.owner", "ocf52 doc 1", "file:///ocf52/doc1.md");
        var t2 = registerDoc("ocf52.owner", "ocf52 doc 2", "file:///ocf52/doc2.md");
        writeManifestRow(t1, "knowledge__ocf52-owner__v1", c1);
        writeManifestRow(t2, "knowledge__ocf52-owner__v1", c2);

        var resp = post("/v1/catalog/manifest/docs_for_chashes",
            mapper.writeValueAsString(Map.of("chashes", List.of(c1, c2))));
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsKeys("tumblers", "count");
        @SuppressWarnings("unchecked")
        var tumblers = (List<String>) body.get("tumblers");
        assertThat(tumblers).containsExactlyInAnyOrder(t1, t2);
        assertThat(((Number) body.get("count")).intValue())
            .as("count must reconcile with the delivered tumblers length")
            .isEqualTo(tumblers.size());
    }

    @Test
    void docsForChashes_emptyResult_countIsZero() throws Exception {
        var resp = post("/v1/catalog/manifest/docs_for_chashes",
            mapper.writeValueAsString(Map.of("chashes", List.of(ch("ocf52-no-such-chunk")))));
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsKeys("tumblers", "count");
        @SuppressWarnings("unchecked")
        var tumblers = (List<String>) body.get("tumblers");
        assertThat(tumblers).isEmpty();
        assertThat(((Number) body.get("count")).intValue()).isEqualTo(0);
    }

    // ── nexus-uu4b9: docs_for_chashes gains the sibling batch cap ──────────────

    @Test
    void docsForChashes_overBatchCap_returns400WithSiblingErrorShape() throws Exception {
        var tooMany = IntStream.range(0, 1001).mapToObj(i -> ch("uu4b9-chash-" + i)).toList();
        var resp = post("/v1/catalog/manifest/docs_for_chashes",
            mapper.writeValueAsString(Map.of("chashes", tooMany)));
        assertThat(resp.statusCode()).isEqualTo(400);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat((String) body.get("error"))
            .as("mirrors handleManifestGetMany's 400 error shape")
            .isEqualTo("too many chashes (max 1000)");
    }

    // ── nexus-b9puj: get_many carries count ────────────────────────────────────

    @Test
    void getMany_multiDoc_countMatchesManifestsSize() throws Exception {
        var t1 = registerDoc("b9puj.owner", "b9puj doc 1", "file:///b9puj/doc1.md");
        var t2 = registerDoc("b9puj.owner", "b9puj doc 2", "file:///b9puj/doc2.md");
        writeManifestRow(t1, "knowledge__b9puj-owner__v1", ch("b9puj-doc1-chunk"));
        writeManifestRow(t2, "knowledge__b9puj-owner__v1", ch("b9puj-doc2-chunk"));

        var resp = post("/v1/catalog/manifest/get_many",
            mapper.writeValueAsString(Map.of("doc_ids", List.of(t1, t2, "b9puj.owner.999999"))));
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsKeys("manifests", "count");
        @SuppressWarnings("unchecked")
        var manifests = (Map<String, Object>) body.get("manifests");
        assertThat(manifests).containsKeys(t1, t2);
        assertThat(((Number) body.get("count")).intValue())
            .as("count must reconcile with the delivered manifests map size")
            .isEqualTo(manifests.size());
    }

    // ── nexus-kzso5: manifest rows carry their own `collection` on the wire ────

    @Test
    void getMany_rowsCarryOwnCollection_independentOfDocPhysicalCollection() throws Exception {
        // RDR-191: the manifest row's stamped collection is caller-supplied,
        // NOT NULL truth -- decoupled from the owning doc's
        // physical_collection. Register the doc under one collection, write
        // the manifest row under a DIFFERENT one, and assert the wire row
        // reports the row's own value, not the doc's.
        var t1 = registerDoc("kzso5.owner", "kzso5 doc 1", "file:///kzso5/doc1.md");
        writeManifestRow(t1, "knowledge__kzso5-explicit__v1", ch("kzso5-doc1-chunk"));

        var resp = post("/v1/catalog/manifest/get_many",
            mapper.writeValueAsString(Map.of("doc_ids", List.of(t1))));
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        @SuppressWarnings("unchecked")
        var manifests = (Map<String, Object>) body.get("manifests");
        @SuppressWarnings("unchecked")
        var rows = (List<Map<String, Object>>) manifests.get(t1);
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).get("collection"))
            .as("get_many rows must carry the row's own collection, additive field")
            .isEqualTo("knowledge__kzso5-explicit__v1");
    }

    @Test
    void get_singleDoc_rowCarriesOwnCollection() throws Exception {
        var t1 = registerDoc("kzso5.owner", "kzso5 doc 2", "file:///kzso5/doc2.md");
        writeManifestRow(t1, "knowledge__kzso5-single__v1", ch("kzso5-doc2-chunk"));

        var resp = get("/v1/catalog/manifest/get?doc_id=" + t1);
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        @SuppressWarnings("unchecked")
        var rows = (List<Map<String, Object>>) body.get("rows");
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).get("collection"))
            .as("single-doc get must carry the row's own collection, additive field")
            .isEqualTo("knowledge__kzso5-single__v1");
    }

    @Test
    void getMany_emptyInput_returns200WithZeroCount() throws Exception {
        var resp = post("/v1/catalog/manifest/get_many",
            mapper.writeValueAsString(Map.of("doc_ids", List.of())));
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsKeys("manifests", "count");
        assertThat(((Number) body.get("count")).intValue()).isEqualTo(0);
    }

    private HttpResponse<String> post(String path, String body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET()
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
