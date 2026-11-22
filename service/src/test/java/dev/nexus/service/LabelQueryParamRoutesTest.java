// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.6 review I1 (nexus-p2m1x.26): a label may hold {@code %}, {@code +}, {@code &} and a space, so a route that
 * reads a collection from its query string decodes it exactly once. {@code TaxonomyHandler.queryParam} and
 * {@code TelemetryHandler.queryParams} read {@code getQuery()}, which already percent-decodes, and then ran
 * {@code URLDecoder} over the result: the {@code %} and {@code +} of a label were decoded again and an encoded
 * {@code &} split the pair. These tests send one label holding all four through a taxonomy GET and a telemetry
 * {@code search/stats} GET and read it back.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class LabelQueryParamRoutesTest {

    private static final String TOKEN = "label-query-param-token-p36";
    private static final String SVC_ROLE = "svc_label_query";
    private static final String SVC_PASS = "svc_label_query_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};
    private static final TypeReference<List<Map<String, Object>>> LIST_T = new TypeReference<>() {};

    /** A space, '%', '+' and '&' in one label, and a '%41' that a second decode would turn into 'A'. */
    private static final String LABEL = "notes 100%41+x&y z";

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
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private HttpRequest.Builder request(String path) {
        return TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN).header("X-Nexus-Tenant", TENANT);
    }

    private HttpResponse<String> post(String path, Map<String, Object> body) throws Exception {
        return http.send(request(path).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body))).build(),
            HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path) throws Exception {
        return http.send(request(path).GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    @Test
    void aTaxonomyGetReadsAnEncodedLabelOnce() throws Exception {
        var reg = post("/v1/catalog/collections/upsert", Map.of("name", LABEL, "content_type", "knowledge",
            "owner_id", "label-notes", "embedding_model", "voyage-context-3", "model_version", "v1",
            "legacy_grandfathered", true));
        assertThat(reg.statusCode()).as(reg.body()).isEqualTo(200);
        var ins = post("/v1/taxonomy/topics/insert", Map.of("label", "a topic", "collection", LABEL, "doc_count", 1));
        assertThat(ins.statusCode()).as(ins.body()).isEqualTo(200);

        var resp = get("/v1/taxonomy/topics?collection=" + enc(LABEL));

        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        List<Map<String, Object>> topics = mapper.readValue(resp.body(), LIST_T);
        assertThat(topics).as("the topic filed under the label comes back").hasSize(1);
        assertThat(topics.get(0)).containsEntry("collection", LABEL);
        // the %20 spelling of the space is read the same way
        var asPercent = get("/v1/taxonomy/topics?collection=" + enc(LABEL).replace("+", "%20"));
        assertThat(asPercent.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(asPercent.body(), LIST_T)).hasSize(1);
    }

    @Test
    void aTelemetrySearchStatsGetReadsAnEncodedLabelOnce() throws Exception {
        String ts = java.time.Instant.now().toString();
        var ins = post("/v1/telemetry/search/batch",
            Map.of("rows", List.of(List.of(ts, "hash-label-query", LABEL, 3, 2, 0.25, 0.5))));
        assertThat(ins.statusCode()).as(ins.body()).isEqualTo(200);

        var resp = get("/v1/telemetry/search/stats?collection=" + enc(LABEL) + "&days=30");

        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        assertThat(((Number) mapper.readValue(resp.body(), MAP_T).get("row_count")).longValue())
            .as("the one row logged under the label").isEqualTo(1L);
    }
}
