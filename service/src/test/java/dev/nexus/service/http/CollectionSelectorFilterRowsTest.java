// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Filter;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.TestHttp;
import dev.nexus.service.db.CollectionRow;
import dev.nexus.service.db.UnknownCollectionIdException;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.OptionalLong;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the route boundary ({@link CollectionSelectorFilter}) over a registry stub, so the answers that
 * need a row the registry never hands out (F5) or a state a database makes awkward (a retired row whose successor is gone) are
 * pinned without a database, and the F13 edge cases and F8 are pinned beside them. The engine-backed twins are
 * {@code CollectionRetiredIdRoutesTest} and {@code CollectionRoutesByIdTest}.
 *
 * <p>The filter runs in front of a handler that echoes what reached it, so each test says both the status the client sees and
 * what the handler was (not) given.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionSelectorFilterRowsTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TENANT = "rows-test";

    /** The rows the stub knows, by id; a lying entry maps an id to a row of another id. */
    private final Map<Long, CollectionRow> byId = new HashMap<>();
    private final Map<String, Long> liveByLabel = new HashMap<>();

    private HttpServer server;
    private HttpClient http;
    private String base;
    private volatile int handlerCalls;

    private static CollectionRow row(long id, String label, String supersededBy) {
        return new CollectionRow("docs", "owner", "minilm-l6-v2-384", 384, "live", supersededBy, id, label);
    }

    @BeforeAll
    void start() throws IOException {
        byId.put(5L, row(5, "live-five", ""));
        byId.put(7L, row(7, "retired-seven", "successor-nine"));
        byId.put(8L, row(8, "retired-eight", "successor-gone"));
        byId.put(6L, row(99, "liar", ""));       // the registry answers id 6 with the row of id 99
        byId.put(10L, row(10, "", ""));           // a row without a label
        liveByLabel.put("successor-nine", 9L);

        CollectionSelectorFilter.Rows rows = new CollectionSelectorFilter.Rows() {
            @Override public CollectionRow byId(String tenant, long id) {
                CollectionRow r = CollectionSelectorFilterRowsTest.this.byId.get(id);
                if (r == null) {
                    throw new UnknownCollectionIdException(tenant, id);
                }
                return r;
            }

            @Override public OptionalLong liveIdOf(String tenant, String label) {
                Long id = liveByLabel.get(label);
                return id == null ? OptionalLong.empty() : OptionalLong.of(id);
            }
        };
        server = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
        for (String path : new String[] {"/v1/catalog/collections/get", "/v1/catalog/collections/delete", "/v1/vectors/search",
                "/v1/taxonomy/centroids/upsert", "/v1/vectors/store-get"}) {
            var context = server.createContext(path, exchange -> {
                handlerCalls++;
                byte[] in = exchange.getRequestBody().readAllBytes();
                byte[] out = (exchange.getRequestURI().getRawQuery() + "|" + new String(in, StandardCharsets.UTF_8))
                    .getBytes(StandardCharsets.UTF_8);
                exchange.sendResponseHeaders(200, out.length);
                exchange.getResponseBody().write(out);
                exchange.close();
            });
            context.getFilters().add(new Filter() {
                @Override public void doFilter(HttpExchange exchange, Chain chain) throws IOException {
                    RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "data", "h"));
                    try {
                        chain.doFilter(exchange);
                    } finally {
                        RequestContext.clear();
                    }
                }

                @Override public String description() {
                    return "sets the tenant";
                }
            });
            context.getFilters().add(new CollectionSelectorFilter(rows));
        }
        server.start();
        http = TestHttp.client();
        base = "http://127.0.0.1:" + server.getAddress().getPort();
    }

    @AfterAll
    void stop() {
        if (server != null) server.stop(0);
    }

    private record Resp(int status, String body) {
        JsonNode json() {
            try {
                return MAPPER.readTree(body);
            } catch (IOException e) {
                throw new IllegalStateException(body, e);
            }
        }
    }

    private Resp get(String pathAndQuery) throws Exception {
        HttpResponse<String> r = http.send(TestHttp.request(base + pathAndQuery).GET().build(), HttpResponse.BodyHandlers.ofString());
        return new Resp(r.statusCode(), r.body());
    }

    private Resp post(String path, String json) throws Exception {
        HttpResponse<String> r = http.send(TestHttp.request(base + path).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json)).build(), HttpResponse.BodyHandlers.ofString());
        return new Resp(r.statusCode(), r.body());
    }

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    // ---- a live id resolves; the handler is given the label -----------------------------------------------------------------

    @Test
    void aLiveIdIsReplacedByItsLabel_inAQueryAndInABody() throws Exception {
        Resp q = get("/v1/catalog/collections/get?collection_id=5&x=1");
        assertThat(q.status()).as(q.body()).isEqualTo(200);
        assertThat(q.body()).contains("name=live-five").contains("x=1").doesNotContain("collection_id");

        Resp b = post("/v1/vectors/store-get", "{\"collection_id\":5,\"ids\":[\"a\"]}");
        assertThat(b.status()).as(b.body()).isEqualTo(200);
        assertThat(b.body()).contains("\"collection\":\"live-five\"").doesNotContain("collection_id");
    }

    // ---- F8: every body is parsed, so an escaped key is still read ------------------------------------------------------------

    @Test
    void f8_anEscapedIdKeyOnAnyRouteIsStillRead() throws Exception {
        Resp b = post("/v1/vectors/store-get", "{\"collection\\u005fid\":5}");
        assertThat(b.status()).as("the escaped key is the id field: %s", b.body()).isEqualTo(200);
        assertThat(b.body()).contains("\"collection\":\"live-five\"");
        Resp unknown = post("/v1/vectors/store-get", "{\"collection\\u005fid\":424242}");
        assertThat(unknown.status()).as(unknown.body()).isEqualTo(404);
    }

    // ---- F5: the registry's row must be the row of the id asked for ------------------------------------------------------

    @Test
    void f5_aRowOfAnotherIdIsRefusedWithATyped500_andTheHandlerNeverRuns() throws Exception {
        int before = handlerCalls;
        Resp r = post("/v1/vectors/store-get", "{\"collection_id\":6}");
        assertThat(r.status()).as(r.body()).isEqualTo(500);
        assertThat(r.json().path("reason").asText()).isEqualTo("collection_id_mismatch");
        assertThat(r.json().path("collection_id").asLong()).isEqualTo(6);
        assertThat(r.json().path("resolved_collection_id").asLong()).isEqualTo(99);
        assertThat(handlerCalls).as("nothing was written to the other collection").isEqualTo(before);

        Resp q = get("/v1/catalog/collections/get?collection_id=6");
        assertThat(q.status()).isEqualTo(500);
        assertThat(handlerCalls).isEqualTo(before);
    }

    @Test
    void f5_aRowWithNoLabelIsRefusedToo() throws Exception {
        int before = handlerCalls;
        Resp r = post("/v1/vectors/store-get", "{\"collection_id\":10}");
        assertThat(r.status()).as(r.body()).isEqualTo(500);
        assertThat(r.json().path("reason").asText()).isEqualTo("collection_id_mismatch");
        assertThat(handlerCalls).isEqualTo(before);
    }

    // ---- D3: a retired id is a 409 naming the successor, on every shape of route ------------------------------------------------

    private void assertSupersededNamingSuccessor(Resp r) {
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        JsonNode j = r.json();
        assertThat(j.path("reason").asText()).isEqualTo("collection_superseded");
        assertThat(j.path("error").asText()).isEqualTo("collection_superseded");
        assertThat(j.path("collection").asText()).isEqualTo("retired-seven");
        assertThat(j.path("collection_id").asLong()).isEqualTo(7);
        assertThat(j.path("superseded_by").asText()).as("the key the client's superseded_write_error_from_body reads")
            .isEqualTo("successor-nine");
        assertThat(j.path("successor_collection_id").asLong()).isEqualTo(9);
    }

    @Test
    void d3_aRetiredIdAnswers409OnAQueryRoute_aBodyRoute_aListRoute_aNestedRoute_andTheDelete() throws Exception {
        int before = handlerCalls;
        assertSupersededNamingSuccessor(get("/v1/catalog/collections/get?collection_id=7"));
        assertSupersededNamingSuccessor(post("/v1/vectors/store-get", "{\"collection_id\":7}"));
        assertSupersededNamingSuccessor(post("/v1/vectors/search", "{\"collection_ids\":[5,7]}"));
        assertSupersededNamingSuccessor(post("/v1/taxonomy/centroids/upsert", "{\"records\":[{\"collection_id\":7}]}"));
        assertSupersededNamingSuccessor(post("/v1/catalog/collections/delete", "{\"collection_id\":7}"));
        assertThat(handlerCalls).as("a refused request reaches no handler").isEqualTo(before);
    }

    @Test
    void d3_aRetiredLabelIsNotTheFilters_itReachesTheHandler() throws Exception {
        Resp r = post("/v1/catalog/collections/delete", "{\"name\":\"retired-seven\"}");
        assertThat(r.status()).as("a label is the handler's: the delete keeps its 200 for a retired label: %s", r.body()).isEqualTo(200);
    }

    @Test
    void d3_aRetiredRowWhoseSuccessorHasNoLiveRowIs409WithNoSuccessorFields() throws Exception {
        Resp r = get("/v1/catalog/collections/get?collection_id=8");
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        JsonNode j = r.json();
        assertThat(j.path("reason").asText()).isEqualTo("collection_superseded");
        assertThat(j.path("collection").asText()).isEqualTo("retired-eight");
        assertThat(j.path("collection_id").asLong()).isEqualTo(8);
        assertThat(j.has("superseded_by")).as("no successor, no successor fields: %s", r.body()).isFalse();
        assertThat(j.has("successor_collection_id")).isFalse();
    }

    @Test
    void d3_404IsOnlyForAnIdWithNoRowAtAll() throws Exception {
        Resp r = get("/v1/catalog/collections/get?collection_id=424242");
        assertThat(r.status()).isEqualTo(404);
        assertThat(r.json().path("reason").asText()).isEqualTo("unknown_collection_id");
        assertThat(r.json().path("collection_id").asLong()).isEqualTo(424242);
    }

    // ---- F13 edges -----------------------------------------------------------------------------------------------------------

    @Test
    void f13_anEmptyLabelPlusAnIdIsBothInAQueryAsInABody() throws Exception {
        assertThat(get("/v1/catalog/collections/get?name=&collection_id=5").status()).isEqualTo(400);
        assertThat(get("/v1/catalog/collections/get?name=" + enc("x") + "&collection_id=5").status()).isEqualTo(400);
        assertThat(post("/v1/vectors/store-get", "{\"collection\":\"\",\"collection_id\":5}").status()).isEqualTo(400);
    }

    @Test
    void f13_anExplicitNullIdIs400_inEveryShape() throws Exception {
        int before = handlerCalls;
        assertThat(post("/v1/vectors/store-get", "{\"collection_id\":null}").status()).isEqualTo(400);
        assertThat(post("/v1/vectors/store-get", "{\"collection\":\"live-five\",\"collection_id\":null}").status()).isEqualTo(400);
        assertThat(post("/v1/vectors/search", "{\"collection_ids\":null}").status()).isEqualTo(400);
        assertThat(post("/v1/taxonomy/centroids/upsert", "{\"records\":[{\"collection_id\":null}]}").status()).isEqualTo(400);
        assertThat(post("/v1/catalog/collections/delete", "{\"collection_id\":null}").status()).isEqualTo(400);
        assertThat(handlerCalls).isEqualTo(before);
    }

    @Test
    void f13_aNineteenDigitIdIs400_inAQueryAndInABody() throws Exception {
        String id = "1234567890123456789";   // fits a long, and has 19 digits
        Resp q = get("/v1/catalog/collections/get?collection_id=" + id);
        Resp b = post("/v1/vectors/store-get", "{\"collection_id\":" + id + "}");
        assertThat(q.status()).as(q.body()).isEqualTo(400);
        assertThat(b.status()).as(b.body()).isEqualTo(400);
        // the largest 18-digit id is read as an id, so it is a 404 (no such row), the same in both
        assertThat(get("/v1/catalog/collections/get?collection_id=999999999999999999").status()).isEqualTo(404);
        assertThat(post("/v1/vectors/store-get", "{\"collection_id\":999999999999999999}").status()).isEqualTo(404);
    }

    @Test
    void f13_aDuplicateQueryIdIs400() throws Exception {
        int before = handlerCalls;
        assertThat(get("/v1/catalog/collections/get?collection_id=5&collection_id=5").status()).isEqualTo(400);
        assertThat(get("/v1/catalog/collections/get?collection_id=5&collection_id=424242").status()).isEqualTo(400);
        assertThat(handlerCalls).isEqualTo(before);
    }
}
