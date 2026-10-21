// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.vectors.LeafBloatCatalog.Leaf;
import dev.nexus.service.vectors.LeafBloatSweep.LeafStatus;
import dev.nexus.service.vectors.LeafBloatSweep.Status;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-dxsjc (nexus-4lnn1 R4): {@code GET /v1/status}'s {@code chunks_leaf_bloat}. Hermetic, as
 * {@link StatusHandlerTest} is: a bare server bound to the handler, fed a supplied value, no database.
 */
class StatusHandlerChunksLeafBloatTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Instant READ_AT = Instant.parse("2026-10-10T08:00:00Z");
    private static final Instant VACUUMED = Instant.parse("2026-10-10T07:12:00.750Z");

    private HttpServer server;
    private String baseUrl;
    private final HttpClient http = dev.nexus.service.TestHttp.client();

    private void start(Supplier<StatusHandler.ChunksLeafBloat> supplier) throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/status",
            new StatusHandler(null, null, 0L, null, null, null, null, supplier));
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

    private static Leaf leaf(String name, long live, long dead, Instant vacuumed, boolean policySet,
                             boolean enabled) {
        return leaf(name, live, dead, vacuumed, 0, policySet, enabled);
    }

    private static Leaf leaf(String name, long live, long dead, Instant vacuumed, long vacuums, boolean policySet,
                             boolean enabled) {
        return new Leaf(name, live, dead, 0, vacuumed, vacuums, policySet, enabled);
    }

    /** Not overdue. */
    private static LeafStatus clean(Leaf leaf) {
        return LeafStatus.clean(leaf);
    }

    /** Overdue for {@code seconds} with no vacuum finished since it became so (waiting, or starved past 30 minutes). */
    private static LeafStatus overdue(Leaf leaf, long seconds) {
        return new LeafStatus(leaf, READ_AT.minusSeconds(seconds), leaf.vacuumCount(), seconds);
    }

    /** Overdue for {@code seconds}, and {@code vacuums} vacuums have finished since it became so. */
    private static LeafStatus pinned(Leaf leaf, long seconds, long vacuums) {
        return new LeafStatus(leaf, READ_AT.minusSeconds(seconds), leaf.vacuumCount() - vacuums, seconds);
    }

    private static Status status(boolean expired, LeafStatus... leaves) {
        return new Status(true, READ_AT, null, 0, expired, List.of(leaves));
    }

    private static StatusHandler.ChunksLeafBloat bloat(Status s) {
        return StatusHandler.ChunksLeafBloat.of(s, 600);
    }

    // -- the key is optional --------------------------------------------------------------------------

    @Test
    void noSupplier_omitsTheKey_andASupplierReturningNullOmitsItToo() throws Exception {
        start(null);
        assertThat(get().has("chunks_leaf_bloat")).as("a wiring that predates the field").isFalse();
        stop();
        start(() -> null);
        assertThat(get().has("chunks_leaf_bloat")).as("a sweep that is not built yet").isFalse();
    }

    @Test
    void theObjectIsServedFromTheSuppliedValue_oneCallPerRequest() throws Exception {
        var calls = new java.util.concurrent.atomic.AtomicInteger();
        var v = bloat(status(false, clean(leaf("chunks_a", 100, 0, null, true, true))));
        start(() -> {
            calls.incrementAndGet();
            return v;
        });
        get();
        get();
        assertThat(calls.get()).isEqualTo(2);
    }

    // -- shape ----------------------------------------------------------------------------------------

    @Test
    void hasExactlyTheDocumentedKeysInOrder() throws Exception {
        start(() -> bloat(status(false, clean(leaf("chunks_a", 100, 5, VACUUMED, 3, true, true)))));

        JsonNode o = get().get("chunks_leaf_bloat");
        assertThat(names(o)).containsExactly("last_read_at", "expired", "sweep_seconds", "ever_read",
            "consecutive_failures", "overdue_base_rows", "overdue_scale_factor", "overdue_warn_after_seconds",
            "leaves_total", "policy_unset", "autovacuum_disabled", "overdue", "pinned", "starved",
            "max_overdue_for_seconds");
        assertThat(o.get("overdue_base_rows").asLong()).isEqualTo(1000);
        assertThat(o.get("overdue_scale_factor").asDouble()).isEqualTo(0.10);
        assertThat(o.get("overdue_warn_after_seconds").asLong()).isEqualTo(1800);
        assertThat(o.get("ever_read").asBoolean()).isTrue();
        assertThat(o.get("leaves_total").asInt()).isEqualTo(1);
        assertThat(o.get("max_overdue_for_seconds").asLong()).isZero();
    }

    private static List<String> names(JsonNode o) {
        var out = new ArrayList<String>();
        o.fieldNames().forEachRemaining(out::add);
        return out;
    }

    // -- aggregates only (Sam's ruling 2026-10-19: the route is unauthenticated) ------------------------

    @Test
    void theAggregatesCountEveryLeaf_andNoListIsReported() throws Exception {
        var leaves = new ArrayList<LeafStatus>();
        for (int i = 0; i < 80; i++) {
            String name = String.format("chunks_l%02d", i);
            leaves.add(switch (i) {
                case 7 -> clean(leaf(name, 100, 0, null, false, true));                                 // policy unset
                case 3 -> clean(leaf(name, 100, 0, null, true, false));                                 // autovacuum off
                case 79 -> pinned(leaf(name, 88_000, 12_000, null, 5, true, true), 600, 1);             // pinned, 12%
                case 78 -> overdue(leaf(name, 88_000, 12_000, null, true, true), 3600);                 // starved, 12%
                case 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33,
                     34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49 ->
                    overdue(leaf(name, 50_000, 50_000, null, true, true), 60);                          // waiting
                default -> clean(leaf(name, 100, 1, null, true, true));                                 // fine
            });
        }
        var all = bloat(new Status(true, READ_AT, null, 0, false, leaves));
        start(() -> all);

        JsonNode o = get().get("chunks_leaf_bloat");
        assertThat(o.get("leaves_total").asInt()).isEqualTo(80);
        assertThat(o.get("policy_unset").asInt()).isEqualTo(1);
        assertThat(o.get("autovacuum_disabled").asInt()).isEqualTo(1);
        assertThat(o.get("overdue").asInt()).isEqualTo(42);
        assertThat(o.get("pinned").asInt()).isEqualTo(1);
        assertThat(o.get("starved").asInt()).isEqualTo(1);
        assertThat(o.get("max_overdue_for_seconds").asLong()).as("the starved leaf, overdue the longest")
            .isEqualTo(3600);
        assertThat(o.has("leaves")).as("no per-leaf list: the route is unauthenticated").isFalse();
    }

    /**
     * Sam's ruling of 2026-10-19: no body of this route names a leaf, a tenant hash or a per-leaf datum. A leaf name is
     * {@code chunks_<model>_<tenant hash>}; the leaves here carry that shape and an index, in every state, and nothing
     * of them may appear anywhere under {@code chunks_leaf_bloat}: not as a key, not as a value. Goes red if a list of
     * leaves, or one named leaf, is put back.
     */
    @Test
    void noBodyNamesALeafOrCarriesAPerLeafDatum() throws Exception {
        var cases = new ArrayList<StatusHandler.ChunksLeafBloat>();
        cases.add(bloat(status(false,
            clean(leaf("chunks_minilm_aaaa", 1000, 20, VACUUMED, 7, true, true)),
            overdue(leaf("chunks_minilm_bbbb", 88_000, 12_000, VACUUMED, 4, true, true), 60),
            pinned(leaf("chunks_minilm_cccc", 80_000, 20_000, VACUUMED, 12, true, true), 600, 1),
            overdue(leaf("chunks_minilm_dddd", 80_000, 20_000, null, 0, true, true), 3600),
            clean(leaf("chunks_minilm_eeee", 10, 0, null, false, true)),
            clean(leaf("chunks_minilm_ffff", 10, 0, null, true, false)))));
        cases.add(bloat(status(true, clean(leaf("chunks_minilm_gggg", 10, 0, null, true, true)))));
        cases.add(StatusHandler.ChunksLeafBloat.of(new Status(false, null, null, 0, false, List.of()), 600));
        var forbiddenKeys = java.util.Set.of("leaves", "leaf", "name", "pci_indexes", "pci_indexes_total", "bytes",
            "live_rows", "dead_rows", "dead_fraction", "last_autovacuum", "vacuum_count", "overdue_state");
        for (var supplied : cases) {
            start(() -> supplied);
            JsonNode o = get().get("chunks_leaf_bloat");
            var seen = new ArrayList<String>();
            o.fields().forEachRemaining(e -> {
                seen.add(e.getKey());
                assertThat(e.getValue().isContainerNode()).as("%s is a scalar: nothing nested to hold a leaf",
                    e.getKey()).isFalse();
            });
            assertThat(seen).as("no per-leaf key").doesNotContainAnyElementsOf(forbiddenKeys);
            String text = o.toString();
            assertThat(text).as("no leaf name anywhere in the object").doesNotContain("chunks_").doesNotContain("pci_");
            stop();
        }
    }

    @Test
    void readsThatFailedFromBoot_areOnTheWire() throws Exception {
        start(() -> bloat(new Status(false, null, Instant.parse("2026-10-10T08:00:00Z"), 3, false, List.of())));
        JsonNode o = get().get("chunks_leaf_bloat");
        assertThat(o.get("ever_read").asBoolean()).isFalse();
        assertThat(o.get("consecutive_failures").asInt()).isEqualTo(3);
        assertThat(o.get("last_read_at").isNull()).isTrue();
    }

    @Test
    void aVirginView_countsZero() throws Exception {
        start(() -> StatusHandler.ChunksLeafBloat.of(new Status(false, null, null, 0, false, List.of()), 600));
        JsonNode o = get().get("chunks_leaf_bloat");
        assertThat(o.get("leaves_total").asInt()).isZero();
        assertThat(o.get("last_read_at").isNull()).isTrue();
    }

    // -- the golden fixture ---------------------------------------------------------------------------

    /**
     * The golden fixture the Python doctor row also reads ({@code tests/fixtures/leaf_bloat_status_bodies.json}): the
     * {@code chunks_leaf_bloat} objects the engine emits for a healthy engine, a small leaf at 30 percent dead, an overdue leaf
     * waiting, pinned and starved, a leaf missing its autovacuum parameters, a leaf with autovacuum off, an expired view, a view not yet read
     * and reads failing since boot, rendered through the real handler. A field renamed or retyped here fails this test until the
     * fixture is updated, and the doctor row's test then reads the updated bodies.
     */
    @Test
    void bodiesMatchTheGoldenFixtureThePythonDoctorRowReads() throws Exception {
        JsonNode golden = MAPPER.readTree(java.nio.file.Files.readString(
            java.nio.file.Path.of("..", "tests", "fixtures", "leaf_bloat_status_bodies.json"))).get("cases");
        var cases = new LinkedHashMap<String, StatusHandler.ChunksLeafBloat>();
        cases.put("healthy", bloat(status(false,
            clean(leaf("chunks_minilm_aaaa", 1000, 20, VACUUMED, 7, true, true)),
            clean(leaf("chunks_minilm_bbbb", 0, 0, null, true, true)))));
        cases.put("small_leaf_thirty_percent_dead", bloat(status(false,
            clean(leaf("chunks_minilm_iiii", 70, 30, null, true, true)))));
        cases.put("overdue_waiting", bloat(status(false,
            overdue(leaf("chunks_minilm_eeee", 88_000, 12_000, VACUUMED, 4, true, true), 0))));
        cases.put("overdue_pinned", bloat(status(false,
            pinned(leaf("chunks_minilm_cccc", 80_000, 20_000, VACUUMED, 12, true, true), 600, 1),
            clean(leaf("chunks_minilm_dddd", 100, 0, null, true, true)))));
        cases.put("overdue_starved", bloat(status(false,
            overdue(leaf("chunks_minilm_jjjj", 80_000, 20_000, null, 0, true, true), 3600))));
        cases.put("policy_unset", bloat(status(false,
            clean(leaf("chunks_minilm_ffff", 10, 0, null, false, true)),
            clean(leaf("chunks_minilm_aaaa", 1000, 20, VACUUMED, 7, true, true)))));
        cases.put("autovacuum_disabled", bloat(status(false,
            clean(leaf("chunks_minilm_gggg", 10, 0, null, true, false)))));
        cases.put("expired", bloat(status(true,
            overdue(leaf("chunks_minilm_hhhh", 80_000, 20_000, VACUUMED, 4, true, true), 7200))));
        cases.put("not_read_yet", StatusHandler.ChunksLeafBloat.of(
            new Status(false, null, null, 0, false, List.of()), 600));
        cases.put("reads_failing_since_boot", StatusHandler.ChunksLeafBloat.of(
            new Status(false, null, Instant.parse("2026-10-10T08:00:00Z"), 3, false, List.of()), 600));

        var goldenNames = new ArrayList<String>();
        golden.fieldNames().forEachRemaining(goldenNames::add);
        assertThat(goldenNames).as("the fixture holds exactly the cases rendered here")
            .containsExactlyElementsOf(cases.keySet());
        for (Map.Entry<String, StatusHandler.ChunksLeafBloat> entry : cases.entrySet()) {
            var supplied = entry.getValue();
            start(() -> supplied);
            assertThat(get().get("chunks_leaf_bloat")).as(entry.getKey()).isEqualTo(golden.get(entry.getKey()));
            stop();
        }
    }
}
