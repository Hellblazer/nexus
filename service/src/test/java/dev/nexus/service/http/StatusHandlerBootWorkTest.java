// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.BootWorkRegistry.Step;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd.3: {@code GET /v1/status}'s {@code boot_work}. Hermetic, as {@link StatusHandlerTest} is: a bare
 * server bound to the handler, fed a registry's snapshot, no database. The field names are the contract with the
 * client (nexus-mjmjl), so each is asserted by name.
 */
class StatusHandlerBootWorkTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Instant T0 = Instant.parse("2026-11-10T08:00:00.750Z");

    private HttpServer server;
    private String baseUrl;
    private final HttpClient http = dev.nexus.service.TestHttp.client();
    private final BootWorkRegistry registry = new BootWorkRegistry(Clock.fixed(T0, ZoneOffset.UTC));

    private void start(StatusHandler handler) throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/status", handler);
        server.start();
        baseUrl = "http://127.0.0.1:" + server.getAddress().getPort();
    }

    @AfterEach
    void stop() {
        if (server != null) server.stop(0);
    }

    private JsonNode get() throws Exception {
        HttpResponse<String> resp = http.send(
            dev.nexus.service.TestHttp.request(baseUrl + "/v1/status").GET().build(),
            HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    private static List<String> fieldNames(JsonNode node) {
        List<String> names = new ArrayList<>();
        node.fieldNames().forEachRemaining(names::add);
        return names;
    }

    @Test
    void anEngineWithNoRegistryBoundStillReportsAnEmptyList_soTheKeyIsAlwaysThere() throws Exception {
        start(new StatusHandler(null, null));

        JsonNode body = get();

        assertThat(body.has("boot_work")).isTrue();
        assertThat(body.get("boot_work").isArray()).isTrue();
        assertThat(body.get("boot_work")).isEmpty();
    }

    @Test
    void anIdleRegistryReportsAnEmptyList() throws Exception {
        start(new StatusHandler(null, null).bootWork(registry::snapshot));

        JsonNode list = get().get("boot_work");

        assertThat(list.isArray()).isTrue();
        assertThat(list).isEmpty();
    }

    @Test
    void anEntryWithoutAStepCarriesExactlyTheAgreedFields_andStepIsNull() throws Exception {
        registry.register("pci-reconcile", "building search indexes", 1L, 4L, "indexes", true,
            "the index in progress is dropped and rebuilt from the start on a later pass; finished indexes are kept");
        start(new StatusHandler(null, null).bootWork(registry::snapshot));

        JsonNode list = get().get("boot_work");

        assertThat(list).hasSize(1);
        JsonNode e = list.get(0);
        assertThat(fieldNames(e)).containsExactly("id", "what", "started_at", "done", "total", "unit",
            "safe_to_stop", "on_stop", "step");
        assertThat(e.get("id").asText()).isEqualTo("pci-reconcile");
        assertThat(e.get("what").asText()).isEqualTo("building search indexes");
        assertThat(e.get("started_at").asText()).isEqualTo("2026-11-10T08:00:00Z");
        assertThat(e.get("done").isIntegralNumber()).isTrue();
        assertThat(e.get("done").asLong()).isEqualTo(1L);
        assertThat(e.get("total").asLong()).isEqualTo(4L);
        assertThat(e.get("unit").asText()).isEqualTo("indexes");
        assertThat(e.get("safe_to_stop").isBoolean()).isTrue();
        assertThat(e.get("safe_to_stop").asBoolean()).isTrue();
        assertThat(e.get("on_stop").asText()).startsWith("the index in progress is dropped");
        assertThat(e.get("step").isNull()).isTrue();
    }

    @Test
    void anEntryWithAStepNestsExactlyTheAgreedStepFields() throws Exception {
        registry.register("pci-reconcile", "building search indexes", 0L, 2L, "indexes", true, "kept");
        registry.step("pci-reconcile", new Step("c: building index", 120L, 4000L, "blocks"));
        start(new StatusHandler(null, null).bootWork(registry::snapshot));

        JsonNode step = get().get("boot_work").get(0).get("step");

        assertThat(step.isObject()).isTrue();
        assertThat(fieldNames(step)).containsExactly("what", "done", "total", "unit");
        assertThat(step.get("what").asText()).isEqualTo("c: building index");
        assertThat(step.get("done").asLong()).isEqualTo(120L);
        assertThat(step.get("total").asLong()).isEqualTo(4000L);
        assertThat(step.get("unit").asText()).isEqualTo("blocks");
    }

    @Test
    void unknownProgressIsJsonNull_notZero() throws Exception {
        registry.register("job", "a job", null, null, null, false, "stopping loses the work");
        registry.step("job", new Step("waiting for writers", null, null, "blocks"));
        start(new StatusHandler(null, null).bootWork(registry::snapshot));

        JsonNode e = get().get("boot_work").get(0);

        assertThat(e.get("done").isNull()).isTrue();
        assertThat(e.get("total").isNull()).isTrue();
        assertThat(e.get("unit").isNull()).isTrue();
        assertThat(e.get("safe_to_stop").asBoolean()).isFalse();
        assertThat(e.get("step").get("done").isNull()).isTrue();
        assertThat(e.get("step").get("total").isNull()).isTrue();
    }

    @Test
    void textIsJsonEscaped_andEntriesComeInRegistrationOrder() throws Exception {
        registry.register("z", "say \"hi\"\n", 0L, 1L, "x", true, "back\\slash");
        registry.register("a", "second", 0L, 1L, "x", true, "y");
        start(new StatusHandler(null, null).bootWork(registry::snapshot));

        JsonNode list = get().get("boot_work");

        assertThat(list.get(0).get("id").asText()).isEqualTo("z");
        assertThat(list.get(0).get("what").asText()).isEqualTo("say \"hi\"\n");
        assertThat(list.get(0).get("on_stop").asText()).isEqualTo("back\\slash");
        assertThat(list.get(1).get("id").asText()).isEqualTo("a");
    }

    @Test
    void aFinishedEntryLeavesTheList() throws Exception {
        registry.register("a", "w", 0L, 1L, "x", true, "y");
        start(new StatusHandler(null, null).bootWork(registry::snapshot));
        assertThat(get().get("boot_work")).hasSize(1);

        registry.finish("a");

        assertThat(get().get("boot_work")).isEmpty();
    }
}
