// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.http.StatusHandler;
import dev.nexus.service.vectors.PciBuilderSession.BuilderState;
import dev.nexus.service.vectors.PciIndexSweep;
import dev.nexus.service.vectors.PciReconciler.DdlStatus;
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
import java.time.Instant;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-227 Step 2 (bead nexus-43ulx.23): {@code per_collection_indexes} reaches the wire through the REAL
 * {@code NexusService} status route. {@code StatusHandlerTest} proves the handler renders a supplied object; this
 * proves the service hands its handler the late-bound source, omits the key until {@code Main} binds one, and serves
 * a re-bound value (not a copy taken at binding time) on the next request.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NexusServicePciStatusWiringTest {

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
        service = new NexusService(0, "pci-status-wiring-token", ds);
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
    void theKeyIsOmittedUntilMainBindsASource_thenServedFromTheBoundSupplierOnEveryRequest() throws Exception {
        assertThat(status().has("per_collection_indexes")).as("unbound: cannot tell, not zero indexes").isFalse();

        var sweep = new PciIndexSweep.Status(true, 2, 0, 1, Instant.parse("2026-10-10T08:00:00Z"), null, 0, false);
        var state = new java.util.concurrent.atomic.AtomicReference<>(
            new DdlStatus(BuilderState.STANDBY, null, null, null));
        service.perCollectionIndexes(() -> StatusHandler.PerCollectionIndexes.of(sweep, state.get()));

        JsonNode standby = status().get("per_collection_indexes");
        assertThat(standby.get("valid").asInt()).isEqualTo(2);
        assertThat(standby.get("unparsed").asInt()).isEqualTo(1);
        assertThat(standby.at("/this_engine/builder_state").asText()).isEqualTo("standby");
        assertThat(standby.at("/this_engine/building").isNull()).isTrue();

        state.set(new DdlStatus(BuilderState.OK, 1, 0, Instant.parse("2026-10-10T07:30:00Z")));
        JsonNode holder = status().get("per_collection_indexes");
        assertThat(holder.at("/this_engine/builder_state").asText()).isEqualTo("ok");
        assertThat(holder.at("/this_engine/building").asInt()).as("read per request, not captured at binding")
            .isEqualTo(1);
        assertThat(holder.at("/this_engine/last_ddl_pass_at").asText()).isEqualTo("2026-10-10T07:30:00Z");
    }
}
