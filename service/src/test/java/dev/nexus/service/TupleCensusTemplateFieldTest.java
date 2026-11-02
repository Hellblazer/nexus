// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.jooq.nexus.Tables;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Map;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-mdbds.2.13 (RDR-239, decision Q2=A on nexus-mdbds.2.4): every census row, from both
 * {@code GET /v1/tuples/subspace_list} and {@code GET /v1/tuples/subspace_stats}, names the template
 * {@code TemplateRegistry.resolve} returns for its subspace in an additive {@code template} field, JSON
 * null when none resolves. {@code subspace_stats} refuses an unresolvable subspace (nexus-mvfm9), so the
 * null case is reachable through {@code subspace_list} only, for a row that outlived its template.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleCensusTemplateFieldTest {

    private static final String BOOT = "boot-census-template-test";
    private static final String TENANT_TOKEN = "tenant-census-template-test";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    NexusService service;
    int port;
    final HttpClient http = TestHttp.client();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, BOOT, TENANT, TokenStore.ROOT_TOKEN_LABEL,
                    TokenStore.SCOPE_ROOT, null, null);
            PgContainerHelper.seedServiceToken(dsl, TENANT_TOKEN, TENANT, "tenant-bound");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(
                null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        service = new NexusService(0, BOOT, ds, null, null, null, null, registry);
        service.start();
        port = service.getPort();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private HttpResponse<String> getRaw(String path) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + port + path)
                .header("Authorization", "Bearer " + TENANT_TOKEN);
        return http.send(b.GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode get(String path) throws Exception {
        var r = getRaw(path);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return MAPPER.readTree(r.body());
    }

    private void out(Map<String, Object> body) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + port + "/v1/tuples/out")
                .header("Authorization", "Bearer " + TENANT_TOKEN)
                .header("Content-Type", "application/json");
        var r = http.send(b.POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
                HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
    }

    private static String uniq() {
        return "ct" + UUID.randomUUID().toString().substring(0, 8);
    }

    /** One row on a board-shaped template: keys {topic}, dims {from}. */
    private void outBoard(String subspace, String topic) throws Exception {
        out(Map.of("subspace", subspace, "keys", Map.of("topic", topic),
                "dims", Map.of("from", "producer"), "body", "hello", "nonce", "n-1"));
    }

    private static JsonNode findIn(JsonNode subspaces, String name) {
        for (JsonNode s : subspaces) {
            if (name.equals(s.get("subspace").asText())) return s;
        }
        throw new AssertionError("subspace_list did not return " + name);
    }

    /** Both routes' row for {@code subspace}: subspace_stats, then the subspace_list entry. */
    private JsonNode[] rows(String subspace) throws Exception {
        return new JsonNode[] {
            get("/v1/tuples/subspace_stats?subspace=" + subspace),
            findIn(get("/v1/tuples/subspace_list?prefix=" + subspace).get("subspaces"), subspace)};
    }

    @Test
    void boardCiSubspace_namesTheBoardCiTemplate() throws Exception {
        String topic = uniq();
        outBoard("board/ci/" + topic, topic);
        for (JsonNode row : rows("board/ci/" + topic)) {
            assertThat(row.get("template").asText()).isEqualTo("board/ci/<topic>");
        }
    }

    @Test
    void boardSubspace_namesTheBoardTemplate() throws Exception {
        String topic = uniq();
        outBoard("board/" + topic, topic);
        for (JsonNode row : rows("board/" + topic)) {
            assertThat(row.get("template").asText()).isEqualTo("board/<topic>");
        }
    }

    /** Two segments: board/ci is board/<topic> with topic=ci, never the three-segment board/ci/<topic>. */
    @Test
    void boardCiTwoSegments_namesTheBoardTemplate_notBoardCi() throws Exception {
        outBoard("board/ci", "ci");
        for (JsonNode row : rows("board/ci")) {
            assertThat(row.get("template").asText()).isEqualTo("board/<topic>");
        }
    }

    @Test
    void aspectsSubspace_namesTheAspectsTemplate() throws Exception {
        out(Map.of("subspace", "aspects", "keys", Map.of("queue", "aspects"),
                "body", "task", "nonce", "n-" + uniq()));
        for (JsonNode row : rows("aspects")) {
            assertThat(row.get("template").asText()).isEqualTo("aspects");
        }
    }

    /** A row that outlived its template: subspace_list still lists it, with "template": null (key present). */
    @Test
    void unresolvableSubspace_reportsNullTemplate_onSubspaceList() throws Exception {
        String sub = "retired/" + uniq();
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES)
                    .insertInto(Tables.TUPLES, Tables.TUPLES.ID, Tables.TUPLES.TENANT_ID, Tables.TUPLES.SUBSPACE,
                            Tables.TUPLES.TEMPLATE, Tables.TUPLES.KEYS, Tables.TUPLES.DIMS, Tables.TUPLES.BODY,
                            Tables.TUPLES.ATTEMPTS, Tables.TUPLES.EXPIRES_AT, Tables.TUPLES.CREATED_AT)
                    .values(MessageDigest.getInstance("SHA-256").digest(sub.getBytes(StandardCharsets.UTF_8)),
                            TENANT, sub, "retired/<x>", JSONB.valueOf("{\"x\": \"1\"}"), JSONB.valueOf("{}"),
                            "orphan", 0, OffsetDateTime.now(ZoneOffset.UTC).plusHours(1),
                            OffsetDateTime.now(ZoneOffset.UTC))
                    .execute();
        }
        JsonNode row = findIn(get("/v1/tuples/subspace_list?prefix=" + sub).get("subspaces"), sub);
        assertThat(row.has("template")).as("the key is always written").isTrue();
        assertThat(row.get("template").isNull()).isTrue();
        assertThat(row.get("total").asInt()).isEqualTo(1);
    }

    /** subspace_stats refuses a subspace that resolves to no template, so it never answers a null template. */
    @Test
    void unresolvableSubspace_isRefusedBySubspaceStats() throws Exception {
        var r = getRaw("/v1/tuples/subspace_stats?subspace=retired/x");
        assertThat(r.statusCode()).as(r.body()).isEqualTo(404);
        assertThat(MAPPER.readTree(r.body()).get("error").asText()).as(r.body()).isEqualTo("UnknownSubspace");
    }

    /** The additive field changes no existing field: names, order (template last) and values. */
    @Test
    void otherCensusFields_areUnchanged_andTemplateIsAppendedLast() throws Exception {
        String topic = uniq();
        outBoard("board/" + topic, topic);
        for (JsonNode row : rows("board/" + topic)) {
            var names = new ArrayList<String>();
            row.fieldNames().forEachRemaining(names::add);
            assertThat(names).containsExactly("subspace", "total", "available", "claimed", "dead", "consumed",
                    "expired_unpurged", "oldest_created_at", "newest_created_at", "lapsed", "delayed",
                    "oldest_claimable_created_at", "template");
            assertThat(row.get("subspace").asText()).isEqualTo("board/" + topic);
            assertThat(row.get("total").asInt()).isEqualTo(1);
            assertThat(row.get("available").asInt()).isEqualTo(1);
            assertThat(row.get("claimed").asInt()).isZero();
            assertThat(row.get("dead").asInt()).isZero();
            assertThat(row.get("consumed").asInt()).isZero();
            assertThat(row.get("lapsed").asInt()).isZero();
            assertThat(row.get("delayed").asInt()).isZero();
            assertThat(row.get("oldest_claimable_created_at").isNull()).isFalse();
        }
    }
}
