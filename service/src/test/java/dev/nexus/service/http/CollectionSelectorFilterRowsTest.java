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
import dev.nexus.service.db.CollectionSuccessor;
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
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the route boundary ({@link CollectionSelectorFilter}) over a registry stub, so the answers that
 * need a state a database makes awkward (a retired row whose successor is gone) are pinned without a database, a retired id is
 * swept over EVERY route of the table, and the F13 edge cases and F8 are pinned beside them. The registry-inconsistency 500 is
 * pinned on the one arm a stub can reach; the real gap between the boundary and the handler (an id freed and its label taken
 * again) is {@code CollectionRetiredIdRoutesTest}'s, on the engine. That class and {@code CollectionRoutesByIdTest} are the
 * engine-backed twins.
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
    private final Map<Long, CollectionSuccessor.Successor> successors = new HashMap<>();

    private CollectionSelectorFilter filter;
    private final java.util.concurrent.ConcurrentLinkedQueue<String> reports = new java.util.concurrent.ConcurrentLinkedQueue<>();
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
        successors.put(7L, new CollectionSuccessor.Successor(9, "successor-nine"));

        CollectionSelectorFilter.Rows rows = new CollectionSelectorFilter.Rows() {
            @Override public CollectionRow byId(String tenant, long id) {
                CollectionRow r = CollectionSelectorFilterRowsTest.this.byId.get(id);
                if (r == null) {
                    throw new UnknownCollectionIdException(tenant, id);
                }
                return r;
            }

            @Override public Optional<CollectionSuccessor.Successor> successorOf(String tenant, long retiredId) {
                return Optional.ofNullable(successors.get(retiredId));
            }
        };
        filter = new CollectionSelectorFilter(rows);
        filter.testHooks(null, null, null, reports::add);
        server = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
        for (String path : CollectionSelectors.ROUTES.keySet()) {
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
            context.getFilters().add(filter);
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

    // ---- the registry-inconsistency 500 -------------------------------------------------------------------------------------------

    /**
     * The one arm a stub can reach: a registry that answers an id with a row of another id is an engine defect, answered with a
     * typed 500 and never a 409; the handler does not run. (The real registry cannot emit such a row, since a read by id selects on the
     * key; the gap between the boundary and the handler is the engine test's.)
     */
    @Test
    void aRegistryRowOfAnotherIdIsATyped500_andTheHandlerNeverRuns() throws Exception {
        int before = handlerCalls;
        Resp r = post("/v1/vectors/store-get", "{\"collection_id\":6}");
        assertThat(r.status()).as(r.body()).isEqualTo(500);
        assertThat(r.json().path("reason").asText()).isEqualTo("collection_id_mismatch");
        assertThat(r.json().path("collection_id").asLong()).isEqualTo(6);
        assertThat(r.json().path("resolved_collection_id").asLong()).isEqualTo(99);
        assertThat(handlerCalls).isEqualTo(before);

        Resp q = get("/v1/catalog/collections/get?collection_id=6");
        assertThat(q.status()).isEqualTo(500);
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

    // ---- D3 over the whole table --------------------------------------------------------------------------------------------------

    /** Every slot of every route of {@link CollectionSelectors#ROUTES}, alone, with the retired id: 409, and no handler runs. */
    @Test
    void d3_aRetiredIdIs409OnEveryRouteAndSlotOfTheTable_andNoHandlerRuns() throws Exception {
        int before = handlerCalls;
        int slots = 0;
        for (Map.Entry<String, CollectionSelectors.Route> e : CollectionSelectors.ROUTES.entrySet()) {
            for (CollectionSelectors.Slot slot : e.getValue().slots()) {
                slots++;
                Resp r;
                if (e.getValue().where() == CollectionSelectors.Where.QUERY) {
                    r = get(e.getKey() + "?" + slot.idField() + "=7");
                } else if (slot.nested()) {
                    r = post(e.getKey(), "{\"" + slot.nestedIn() + "\":[{\"" + slot.idField() + "\":7}]}");
                } else if (slot.list()) {
                    r = post(e.getKey(), "{\"" + slot.idField() + "\":[7]}");
                } else {
                    r = post(e.getKey(), "{\"" + slot.idField() + "\":7}");
                }
                assertThat(r.status()).as("%s slot %s: %s", e.getKey(), slot.idField(), r.body()).isEqualTo(409);
                assertThat(r.json().path("reason").asText()).as(e.getKey()).isEqualTo("collection_superseded");
                assertThat(r.json().path("superseded_by").asText()).as(e.getKey()).isEqualTo("successor-nine");
            }
        }
        assertThat(slots).as("non-vacuity: the table has its routes").isGreaterThanOrEqualTo(86);
        assertThat(handlerCalls).as("a refused request reaches no handler").isEqualTo(before);
    }

    // ---- F13 parity between a query and a body --------------------------------------------------------------------------------------

    @Test
    void f13_theSmallestLongIsRefusedAsALongIdIs_inAQueryAndInABody() throws Exception {
        // Math.abs(Long.MIN_VALUE) is negative, so a bound written with abs() let this through to a 404
        String min = "-9223372036854775808";
        assertThat(get("/v1/catalog/collections/get?collection_id=" + min).status()).isEqualTo(400);
        Resp b = post("/v1/vectors/store-get", "{\"collection_id\":" + min + "}");
        assertThat(b.status()).as(b.body()).isEqualTo(400);
        assertThat(post("/v1/vectors/store-get", "{\"collection_id\":-999999999999999999}").status()).as("the bound itself is an id").isEqualTo(404);
    }

    @Test
    void f13_anIdKeyGivenTwiceInABodyIs400_asInAQuery_andIsNotSwallowedByThePassThroughOfAnInvalidBody() throws Exception {
        int before = handlerCalls;
        assertThat(post("/v1/vectors/store-get", "{\"collection_id\":5,\"collection_id\":5}").status()).isEqualTo(400);
        assertThat(post("/v1/vectors/store-get", "{\"collection_id\":5,\"collection_id\":424242}").status()).isEqualTo(400);
        // the second key is the same key once decoded
        assertThat(post("/v1/vectors/store-get", "{\"collection_id\":5,\"collection\\u005fid\":5}").status()).isEqualTo(400);
        // in a nested record, and the duplicate is seen even though the body is cut off after it
        assertThat(post("/v1/taxonomy/centroids/upsert", "{\"records\":[{\"collection_id\":5,\"collection_id\":6}]}").status())
            .isEqualTo(400);
        assertThat(post("/v1/vectors/store-get", "{\"collection_id\":5,\"collection_id\":5,\"x\":").status()).isEqualTo(400);
        assertThat(handlerCalls).isEqualTo(before);

        // a body that is not JSON still goes to the handler, which refuses it in its own words
        Resp notJson = post("/v1/vectors/store-get", "not json");
        assertThat(notJson.status()).isEqualTo(200);
        assertThat(handlerCalls).isEqualTo(before + 1);
    }

    // ---- F8: a body with no id field is not turned into a tree ---------------------------------------------------------------------

    @Test
    void aBodyThatNamesNoIdReachesTheHandlerAsTheBytesThatCame() throws Exception {
        String body = "{ \"collection\" : \"live-five\", \"big\" : [1, 2, {\"collection_id\": 5}] }";   // an id key one level down is not an id field
        Resp r = post("/v1/vectors/store-get", body);
        assertThat(r.status()).isEqualTo(200);
        assertThat(r.body()).as("the bytes untouched, spacing included").endsWith(body);
    }

    // ---- the consumption report ----------------------------------------------------------------------------------------------------

    /**
     * A write route that answers 2xx for an id without any in-transaction read of it is reported (the stub handler reads nothing, as a
     * handler that went on by label would); a route that is not a write route is not. A request by label names no id, so there is
     * nothing to consume and nothing to report.
     */
    /**
     * The report runs on the server thread AFTER the stub handler has closed the exchange, so a client that has its 200 can look at
     * {@code reports} before the filter's {@code finally} has run (a loaded machine shows it). The positive case therefore waits for
     * the report, and the negative case gives a late one a moment to arrive, so "nothing reported" is read after the server threads have
     * had their turn.
     */
    private void awaitReports(int count) throws InterruptedException {
        long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(10);
        while (reports.size() < count && System.nanoTime() < deadline) {
            Thread.sleep(5);
        }
    }

    @Test
    void aWriteRouteThatAnswers2xxWithoutReadingTheIdIsReported_otherRoutesAreNot() throws Exception {
        reports.clear();
        assertThat(post("/v1/taxonomy/topics/insert", "{\"collection_id\":5,\"label\":\"x\"}").status()).isEqualTo(200);
        awaitReports(1);
        assertThat(reports).as("a write route, 2xx, id never read in a transaction").hasSize(1);
        assertThat(reports.peek()).contains("/v1/taxonomy/topics/insert").contains("live-five");

        // the request-level report does not depend on the per-transaction census: with it off the same request is reported
        dev.nexus.service.db.ExpectedCollectionIds.setCensus(false);
        try {
            reports.clear();
            assertThat(post("/v1/taxonomy/topics/insert", "{\"collection_id\":5,\"label\":\"x\"}").status()).isEqualTo(200);
            awaitReports(1);
            assertThat(reports).as("the same write route with the per-transaction census off").hasSize(1);
        } finally {
            dev.nexus.service.db.ExpectedCollectionIds.setCensus(true);
        }

        reports.clear();
        assertThat(post("/v1/vectors/store-get", "{\"collection_id\":5}").status()).isEqualTo(200);
        assertThat(get("/v1/catalog/collections/get?collection_id=5").status()).isEqualTo(200);
        assertThat(post("/v1/taxonomy/topics/insert", "{\"collection\":\"live-five\",\"label\":\"x\"}").status()).isEqualTo(200);
        Thread.sleep(300);   // a late report of the three requests above would have arrived by now
        assertThat(reports).as("a read route, and a request by label").isEmpty();
    }
}
