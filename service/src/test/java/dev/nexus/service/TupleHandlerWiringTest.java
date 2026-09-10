// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-205 Phase 1 Step 4 (bead nexus-em75s.4) — proves the constructor-level
 * half of "the boot check refuses to start" (the other half — {@code
 * TemplateRegistry.loadAtBoot} itself refusing on a breach — is already
 * pinned by {@code TemplateRegistryTest}'s {@code bootCheckRefusesOnEquality}
 * / {@code bootCheckRefusesWhenLogTtlIsShorter}, bead nexus-em75s.3):
 *
 * <ol>
 *   <li>{@code NexusService} constructed WITH a {@link TemplateRegistry}
 *       (the widest constructor, the one {@code Main.java} calls AFTER
 *       {@code TemplateRegistry.loadAtBoot} has already passed its own boot
 *       check) wires {@code /v1/tuples} live.</li>
 *   <li>{@code NexusService} constructed WITHOUT one (every narrower
 *       overload — what every OTHER handler test in this package already
 *       uses) leaves {@code /v1/tuples} unregistered (404), never a
 *       partially-wired NPE.</li>
 * </ol>
 *
 * <p>{@code Main.java}'s own ordering — {@code loadAtBoot} runs, and on a
 * {@code TemplateRegistryException} the process exits BEFORE {@code
 * NexusService} is ever constructed — is a straight-line boot sequence with
 * no branching to unit-test independently of spawning the real process;
 * combined with (1) above (a registry that DID pass its boot check reaches
 * this constructor and wires the route) and the existing loadAtBoot
 * boot-check tests, the two together are the proof the bead requires.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleHandlerWiringTest {

    private static final String TOKEN = "tuple-wiring-test-token-xyz123";
    private static final String SVC_ROLE = "svc_tuple_wiring_test";
    private static final String SVC_PASS = "svc_tuple_wiring_test_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;

    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {
    };

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    NexusService withRegistry;
    NexusService withoutRegistry;
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

        withRegistry = new NexusService(0, TOKEN, svcDs, null, null, null, null, registry);
        withRegistry.start();

        withoutRegistry = new NexusService(0, TOKEN, svcDs);
        withoutRegistry.start();

        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (withRegistry != null) {
            withRegistry.stop();
        }
        if (withoutRegistry != null) {
            withoutRegistry.stop();
        }
        if (svcDs != null) {
            svcDs.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void registryPresent_tuplesRouteLive() throws Exception {
        var resp = get(withRegistry, "/v1/tuples/registry");
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsKey("digest");
        assertThat((java.util.List<?>) body.get("templates")).hasSize(2);
    }

    @Test
    void registryAbsent_tuplesRouteNotRegistered() throws Exception {
        var resp = get(withoutRegistry, "/v1/tuples/registry");
        assertThat(resp.statusCode()).isEqualTo(404);
    }

    private HttpResponse<String> get(NexusService svc, String path) throws Exception {
        var req = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + svc.getPort() + path))
                .header("Authorization", "Bearer " + TOKEN)
                .header("X-Nexus-Tenant", TENANT)
                .GET()
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
