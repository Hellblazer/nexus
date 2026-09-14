// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TupleLimits;
import dev.nexus.service.tuples.TemplateRegistry;
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
import java.sql.Connection;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-205 amendment (bead nexus-r7xao) — the whole-request 8192-byte cap and
 * one field-level {@code TooLarge} refusal, proven over a REAL HTTP round
 * trip against a real {@code NexusService} (the {@code TupleHandlerWiringTest}
 * idiom), so the wire shape ({@code {"error":"TooLarge","detail":"..."}} at
 * HTTP 413) is pinned end to end rather than only at the repository layer
 * ({@code TupleSizeLimitsTest} covers every per-field boundary there).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleSizeLimitsWireTest {

    private static final String TOKEN = "tuple-size-wire-test-token-xyz123";
    private static final String SVC_ROLE = "svc_tuple_size_wire_test";
    private static final String SVC_PASS = "svc_tuple_size_wire_test_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;

    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {
    };

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    NexusService svc;
    HttpClient http;
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
            PgContainerHelper.seedServiceToken(
                    DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "test-bound");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(10);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        TemplateRegistry registry = TemplateRegistry.loadAtBoot(
                null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);

        svc = new NexusService(0, TOKEN, svcDs, null, null, null, null, registry);
        svc.start();

        http = TestHttp.client();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svc != null) {
            svc.stop();
        }
        if (svcDs != null) {
            svcDs.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void wholeRequestOverEightKilobytes_refused413_beforeAnySchemaCheck() throws Exception {
        // A body this large has no valid subspace/keys shape either -- proving the
        // 413 fires means the bounded read refused it BEFORE the JSON was ever
        // parsed into a Map, not that schema validation happened to reject it first.
        String oversizedField = "x".repeat(TupleLimits.MAX_REQUEST_BODY_BYTES + 100);
        var resp = post(Map.of("subspace", "mailbox/wire-oversize", "keys", Map.of("to", oversizedField)));
        assertThat(resp.statusCode()).isEqualTo(413);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.get("error")).isEqualTo("TooLarge");
        assertThat((String) body.get("detail")).contains("request body");
    }

    /**
     * CRE minor: the whole-request cap's own boundary (8192 succeeds past the
     * bounded read / 8193 is refused there) was previously only proven "+100
     * over" and "well under the cap" -- not the exact off-by-one edge the way
     * every per-field cap already is in {@code TupleSizeLimitsTest}. Uses the
     * SAME map instance for the measurement and the actual send (never two
     * separate {@code Map.of} calls) so there is no risk of Jackson
     * serialising two structurally-identical-but-distinct map instances to
     * different byte lengths.
     */
    @Test
    void wholeRequestBoundary_exactlyAtCapPassesThroughToAFieldCheck_oneByteOverIsRefusedByTheWholeRequestGuard()
            throws Exception {
        java.util.LinkedHashMap<String, Object> payload = new java.util.LinkedHashMap<>();
        payload.put("subspace", "mailbox/wire-boundary");
        java.util.LinkedHashMap<String, String> keys = new java.util.LinkedHashMap<>();
        keys.put("to", "");
        payload.put("keys", keys);
        int base = mapper.writeValueAsBytes(payload).length;

        // Exactly at the whole-request cap: the bounded read must NOT refuse this
        // one. "to" is then, by construction, far over its OWN 256-byte field cap
        // (base is a handful of bytes, so the padded value is over 8000 bytes) --
        // refused AFTER parsing, with a field-specific detail, proving the two
        // guards are independent and this one passed the whole-request guard.
        keys.put("to", "x".repeat(TupleLimits.MAX_REQUEST_BODY_BYTES - base));
        byte[] atCapBytes = mapper.writeValueAsBytes(payload);
        assertThat(atCapBytes.length).isEqualTo(TupleLimits.MAX_REQUEST_BODY_BYTES);
        var atCapResp = post(payload);
        assertThat(atCapResp.statusCode()).isEqualTo(413);
        var atCapBody = mapper.readValue(atCapResp.body(), MAP_T);
        assertThat(atCapBody.get("error")).isEqualTo("TooLarge");
        assertThat((String) atCapBody.get("detail")).contains("keys.to");
        assertThat((String) atCapBody.get("detail")).doesNotContain("request body");

        // One byte over: the bounded read refuses THIS one directly, before any
        // JSON parsing -- detail names "request body", never the field.
        keys.put("to", "x".repeat(TupleLimits.MAX_REQUEST_BODY_BYTES - base + 1));
        byte[] overBytes = mapper.writeValueAsBytes(payload);
        assertThat(overBytes.length).isEqualTo(TupleLimits.MAX_REQUEST_BODY_BYTES + 1);
        var overResp = post(payload);
        assertThat(overResp.statusCode()).isEqualTo(413);
        var overBody = mapper.readValue(overResp.body(), MAP_T);
        assertThat(overBody.get("error")).isEqualTo("TooLarge");
        assertThat((String) overBody.get("detail")).contains("request body");
    }

    @Test
    void everyFieldAtItsOwnCap_sumsWellUnderTheWholeRequestCap_succeeds() throws Exception {
        // body at its 4096-byte cap, "to" and "from" at the 256-byte field cap, the
        // nonce at its 128-byte cap: every field individually at its own ceiling,
        // summing to well under the 8192-byte whole-request cap -- proving the two
        // guards are independent and a request built entirely from maximal fields
        // still succeeds.
        var resp = post(Map.of(
                "subspace", "mailbox/wire-all-fields-at-cap",
                "keys", Map.of("to", "t".repeat(TupleLimits.MAX_FIELD_VALUE_BYTES)),
                "dims", Map.of("from", "f".repeat(TupleLimits.MAX_FIELD_VALUE_BYTES)),
                "body", "b".repeat(TupleLimits.MAX_BODY_BYTES),
                "nonce", "n".repeat(TupleLimits.MAX_NONCE_BYTES)));
        assertThat(resp.statusCode()).isEqualTo(200);
    }

    @Test
    void oversizedBodyField_refused413_withTooLargeCode() throws Exception {
        String oversizedBody = "x".repeat(TupleLimits.MAX_BODY_BYTES + 1);
        var resp = post(Map.of(
                "subspace", "mailbox/wire-body-over",
                "keys", Map.of("to", "wire-body-over"),
                "dims", Map.of("from", "sender"),
                "body", oversizedBody,
                "nonce", "n-wire-body-over"));
        assertThat(resp.statusCode()).isEqualTo(413);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.get("error")).isEqualTo("TooLarge");
        assertThat((String) body.get("detail")).contains("body");
        assertThat((String) body.get("detail")).doesNotContain("xxxx");
    }

    private HttpResponse<String> post(Object body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + svc.getPort() + "/v1/tuples/out")
                .header("Authorization", "Bearer " + TOKEN)
                .header("X-Nexus-Tenant", TENANT)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body)))
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
