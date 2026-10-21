// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantKeyedState;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-0bl77 (rtg20 phase A, fix round): the evictor-failure path of {@code POST /v1/_test/tenant-reset}.
 * No database and no engine: the handler is driven with fixed evictors. The gates (operator only, PCI, bad request,
 * default tenant) and the evictors themselves are covered in {@code TenantResetRouteTest} and
 * {@code TenantKeyedEvictorsTest}; what is pinned here is that an evictor that throws makes the whole answer a 500
 * naming it, that every evictor after it still runs, and that the evictors before and after it report their counts,
 * because a harness reads that 500 as "this tenant is not clean, retire it".
 */
class TenantResetHandlerTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TENANT = "reset-handler-tenant";

    private static CapturingExchange reset(TenantResetHandler handler, String tenant) throws Exception {
        CapturingExchange ex = new CapturingExchange("POST", URI.create(TenantResetHandler.PATH),
            "{\"tenant\":\"" + tenant + "\"}");
        RequestContext.set(new RequestContext.Principal("default", null, false, true, "root", "root-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        return ex;
    }

    @Test
    void anEvictorThatThrowsMakesItA500NamingItAndTheOthersStillRunAndReport() throws Exception {
        List<String> ran = new ArrayList<>();
        var handler = new TenantResetHandler(List.of(
            TenantKeyedState.of("First", t -> { ran.add("First"); return 3; }),
            TenantKeyedState.of("TupleWaitRegistry", t -> {
                ran.add("TupleWaitRegistry");
                throw new IllegalStateException("2 tuple waiter(s) of tenant '" + t + "' were still registered; "
                    + "the tenant is not quiesced, retire it");
            }),
            TenantKeyedState.of("Last", t -> { ran.add("Last"); return 5; })), () -> false);

        CapturingExchange ex = reset(handler, TENANT);

        assertThat(ex.status).as(ex.bodyString()).isEqualTo(500);
        assertThat(ran).as("an evictor after the failing one still ran").containsExactly("First", "TupleWaitRegistry", "Last");
        JsonNode body = MAPPER.readTree(ex.bodyString());
        assertThat(body.get("tenant").asText()).isEqualTo(TENANT);
        assertThat(body.get("evicted").get("First").asInt()).isEqualTo(3);
        assertThat(body.get("evicted").get("Last").asInt()).isEqualTo(5);
        assertThat(body.get("evicted").has("TupleWaitRegistry")).as("the failed evictor has no count").isFalse();
        assertThat(body.get("failed").get("TupleWaitRegistry").asText())
            .contains("not quiesced").contains(TENANT);
        assertThat(body.get("failed").has("First")).isFalse();
    }

    @Test
    void whenNoEvictorThrowsTheAnswerIs200WithNoFailedObject() throws Exception {
        var handler = new TenantResetHandler(List.of(
            TenantKeyedState.of("A", t -> 1), TenantKeyedState.of("B", t -> 0)), () -> false);

        CapturingExchange ex = reset(handler, TENANT);

        assertThat(ex.status).isEqualTo(200);
        JsonNode body = MAPPER.readTree(ex.bodyString());
        assertThat(body.has("failed")).isFalse();
        assertThat(body.get("evicted").get("A").asInt()).isEqualTo(1);
        assertThat(body.get("evicted").get("B").asInt()).isZero();
    }
}
