// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.12 (nexus-p2m1x.32): {@code live_label_index_present} reaches the wire through the REAL {@code NexusService}
 * status route. {@code StatusHandlerTest} proves the handler renders a supplied boolean and
 * {@code LiveLabelIndexProbeTest} proves the probe; this proves the service hands its handler the probe over its own data
 * source. A service that never called {@code .liveLabelIndex(...)} omits the key (the first test fails); one wired to a
 * constant answers the same on both stores (one of the two fails). Each test takes its own store, because the probe's
 * answer is cached for a few seconds and the index is dropped before the service starts.
 */
class NexusServiceLiveLabelIndexWiringTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String KEY = "live_label_index_present";
    private static final String INDEX = "idx_catalog_collections_live_label";

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource ds;
    private NexusService service;
    private final HttpClient http = TestHttp.client();

    private void startService(boolean dropIndex) throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
            if (dropIndex) {
                // the shared helper qualifies the name: jOOQ renders a PostgreSQL DROP INDEX without the schema
                PgContainerHelper.dropIndex(su, DSL.name("nexus", INDEX));
            }
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new com.zaxxer.hikari.HikariDataSource(cfg);
        service = new NexusService(0, "live-label-index-wiring-token", ds);
        service.start();
    }

    @AfterEach
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
    void aMigratedStore_servesTheKeyAsTrueFromTheProbeOverTheServicesDataSource() throws Exception {
        startService(false);
        JsonNode key = status().get(KEY);
        assertThat(key).as("the service wires .liveLabelIndex(...): an unwired route omits the key").isNotNull();
        assertThat(key.isBoolean()).isTrue();
        assertThat(key.asBoolean()).as("catalog-053-1 built on a migrated store").isTrue();
    }

    @Test
    void aStoreWhoseIndexNeverBuilt_servesFalse_neverAnAbsentKey() throws Exception {
        startService(true);
        JsonNode key = status().get(KEY);
        assertThat(key).as("false is an answer, absent means cannot tell").isNotNull();
        assertThat(key.asBoolean()).as("the index is not there: read from the store, not a constant").isFalse();
    }
}
