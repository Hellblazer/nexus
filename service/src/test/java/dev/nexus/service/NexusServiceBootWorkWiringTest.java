// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.BootWorkRegistry.Step;
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
import java.time.Clock;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd.3: {@code boot_work} reaches the wire through the REAL {@code NexusService} {@code /v1/status} route.
 * {@code StatusHandlerBootWorkTest} proves the handler renders what it is given; this proves the service hands the
 * handler its late-bound registry, reads it per request (not a copy taken at binding time), and reports an empty list,
 * not a missing key, before {@code Main} binds one. {@code MainPciWiringTest} pins that {@code Main} makes the bind.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NexusServiceBootWorkWiringTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource ds;
    private NexusService service;
    private final HttpClient http = TestHttp.client();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new com.zaxxer.hikari.HikariDataSource(cfg);
        service = new NexusService(0, "boot-work-wiring-token", ds);
        service.start();
    }

    @AfterAll
    void stopAll() {
        if (service != null) {
            try {
                service.stop();
            } catch (Exception ignored) {
                // best effort
            }
        }
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private JsonNode status() throws Exception {
        HttpResponse<String> resp = http.send(
            HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/status")).GET().build(),
            HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    @Test
    void anEmptyListBeforeARegistryIsBound_thenTheBoundRegistrysEntriesReadPerRequest_thenEmptyAgain() throws Exception {
        JsonNode before = status();
        assertThat(before.has("boot_work")).as("present, so a client tells idle from an old engine").isTrue();
        assertThat(before.get("boot_work").isArray()).isTrue();
        assertThat(before.get("boot_work")).isEmpty();

        var registry = new BootWorkRegistry(Clock.systemUTC());
        service.bootWork(registry);
        assertThat(status().get("boot_work")).as("bound, idle").isEmpty();

        registry.register("pci-reconcile", "building search indexes", 0L, null, "indexes", true, "lost, rebuilt");
        registry.step("pci-reconcile", new Step("building index: scanning table", 5L, 50L, "blocks"));
        JsonNode work = status().get("boot_work");
        assertThat(work).as("the service reads the registry per request").hasSize(1);
        assertThat(work.get(0).get("id").asText()).isEqualTo("pci-reconcile");
        assertThat(work.get(0).get("total").isNull()).as("a total not known yet is JSON null").isTrue();
        assertThat(work.get(0).at("/step/what").asText()).isEqualTo("building index: scanning table");
        assertThat(work.get(0).at("/step/done").asLong()).isEqualTo(5L);

        registry.finish("pci-reconcile");
        assertThat(status().get("boot_work")).isEmpty();

        service.bootWork(null);
        registry.register("a", "w", 0L, 1L, "x", true, "y");
        assertThat(status().get("boot_work")).as("unbound again: nothing is read").isEmpty();
    }
}
