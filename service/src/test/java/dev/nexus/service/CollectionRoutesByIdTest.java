// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.CollectionRouteCensus.Route;
import dev.nexus.service.CollectionRouteCensus.Slot;
import dev.nexus.service.CollectionRouteCensus.Tier;
import dev.nexus.service.CollectionRouteCensus.Where;
import dev.nexus.service.CollectionRouteFixture.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DynamicContainer;
import org.junit.jupiter.api.DynamicNode;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import org.junit.jupiter.api.TestInstance;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.Stream;

import static dev.nexus.service.CollectionRouteCensus.UNKNOWN_ID;
import static dev.nexus.service.CollectionRouteFixture.json;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a write half (nexus-p2m1x.23): every engine route that takes a collection accepts {@code collection_id} or a
 * label, and resolves both to the same collection.
 *
 * <p>RDR line 2662: "every collection-taking route accepts {@code collection_id} and a label; responses carry both." Item 6:
 * "Every engine route that takes a collection accepts {@code collection_id}; a label is still accepted at the route boundary
 * and resolved to the id once, by {@code CollectionRegistry}." The routes are {@link CollectionRouteCensus#ROUTES}, one
 * dynamic container each, run under gate 3 not holding and holding: the RDR makes the registry hand readers the name until
 * gate 3 and the id from it, and says the switch needs no restart, so the route boundary must answer the same both ways. A
 * row's rename variant runs only while gate 3 does not hold; from gate 3 a rename is refused ({@link
 * CollectionRenameRefusalTest}).
 *
 * <p>Four checks per route and gate:
 * <ol>
 *   <li>{@code unknownId}: an id no row has is a typed 404 {@code unknown_collection_id} that names the id (bob's
 *       contract point 3, applied to every route: "unknown id -&gt; typed 404 naming the id").</li>
 *   <li>{@code bothGiven}: a label and an id for one slot is a 400 (contract point 1, "name AND id ... -&gt; 400").</li>
 *   <li>{@code nonIntegerId}: an id that is not a JSON integer (a query value that is not a decimal) is a 400 (point 1).</li>
 *   <li>{@code idEqualsLabel}: the same request by the label of one collection and by the id of a twin gives the same status
 *       and the same response shape, and the id one is never the unknown-id refusal. Last, because a mutating route changes
 *       what the three above see.</li>
 * </ol>
 *
 * <p>The assumption these checks make about a route's own order of work, named because the RDR does not state it: a route
 * reads its collection selector first (the route boundary resolves it, RDR item 6), so an unknown id is refused before the
 * route's other validation. {@link Tier#HEAVY} routes need a document, a chunk or a job that the fixture does not build; for
 * them the label request may be refused, and the unknown-id and both-given checks are what prove the selector is read.
 * {@link #idResolvesToItsOwnCollection} is the check that an id reaches the right collection and not just some collection.
 *
 * <p>linda's ruling 2 (2026-11-22): the 400 (both given, non-integer) and the 404 {@code unknown_collection_id} apply to id
 * input only. A label no row has keeps today's answer on the routes that refuse one, 422 {@code unregistered_collection}
 * ({@link #anUnregisteredLabelKeepsTheTyped422_whileAnUnknownIdIsTheTyped404}); nothing in this class asserts a 404 or 400 for a label.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRoutesByIdTest {

    enum Mode { LABEL, ID, UNKNOWN, BOTH, NON_INTEGER }

    /** The standard engine, which every row but three runs against. */
    CollectionRouteFixture fx;

    /**
     * The {@link CollectionRouteCensus.Engine#LOCAL_EMBED} variant: an {@code EmbedderRouter}, {@code NX_ENGINE_LOCAL=1} and a
     * recording re-embed executor. Only the census rows that name it run here (the embed and re-embed routes answer 503 before
     * they read a collection on the standard engine); the shared fixture's mode is unchanged.
     */
    CollectionRouteFixture fxLocal;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        fxLocal = new CollectionRouteFixture(true);
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
        if (fxLocal != null) fxLocal.close();
    }

    private CollectionRouteFixture fixtureFor(Route route) {
        return route.engine() == CollectionRouteCensus.Engine.LOCAL_EMBED ? fxLocal : fx;
    }

    /** What one route's two sides are: for each slot the label-side and id-side collection, registered. */
    private final class Prepared {
        final Route route;
        final String stem;
        final CollectionRouteFixture f;
        final List<String> labelSide = new ArrayList<>();
        final List<String> idSide = new ArrayList<>();
        final List<Long> idSideIds = new ArrayList<>();

        Prepared(Route route, String stem) {
            this.route = route;
            this.stem = stem;
            this.f = fixtureFor(route);
            List<String> all = new ArrayList<>();
            for (int k = 0; k < route.slots().size(); k++) {
                String q = route.slots().get(k).labelField().contains("quarantine") ? "quarantine-" : "";
                labelSide.add(q + CollectionRouteFixture.label(stem + "-s" + k + "a"));
                idSide.add(q + CollectionRouteFixture.label(stem + "-s" + k + "b"));
            }
            all.addAll(labelSide);
            all.addAll(idSide);
            f.register(all.toArray(String[]::new));
            for (String l : idSide) {
                idSideIds.add(f.idOf(l));
            }
            assertThat(idSideIds).as("setup: the id-side collections are registered with ids").doesNotContainNull();
        }

        String fresh(boolean idSideNew) {
            return CollectionRouteFixture.label(stem + (idSideNew ? "-nb" : "-na"));
        }
    }

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    /** The selector text of one slot for one mode. {@code first} is slot 0, the one the error modes act on. */
    private static String selector(Route route, Slot slot, int k, Mode mode, Prepared p) {
        String label = p.labelSide.get(k);
        long id = p.idSideIds.get(k);
        boolean first = k == 0;
        Mode m = first ? mode : (mode == Mode.LABEL ? Mode.LABEL : Mode.ID);
        boolean query = route.where() == Where.QUERY;
        String lf = slot.labelField();
        String idf = slot.idField();
        return switch (m) {
            case LABEL -> field(query, lf, query ? enc(label) : json(label), slot.list());
            case ID -> field(query, idf, String.valueOf(id), slot.list());
            case UNKNOWN -> field(query, idf, String.valueOf(UNKNOWN_ID), slot.list());
            case BOTH -> field(query, lf, query ? enc(label) : json(label), slot.list()) + (query ? "&" : ",")
                + field(query, idf, String.valueOf(id), slot.list());
            case NON_INTEGER -> field(query, idf, query ? "abc" : "\"12\"", slot.list());
        };
    }

    private static String field(boolean query, String name, String value, boolean list) {
        if (query) {
            return name + "=" + value;
        }
        return "\"" + name + "\":" + (list ? "[" + value + "]" : value);
    }

    private Resp call(Route route, Mode mode, Prepared p) {
        String fresh = p.fresh(mode != Mode.LABEL);
        String extras = route.extras().replace("{NEW}", fresh);
        if (route.where() == Where.QUERY) {
            String sel = new ArrayList<>(selectors(route, mode, p)).stream().collect(Collectors.joining("&"));
            return p.f.send(route.method(), route.path() + "?" + sel + extras, null);
        }
        // A selector is a top-level field, or a field of each element of one request array (the centroid upsert's records, a
        // remap batch's entries); a row can have both. The route's extras are top-level, its elementExtras sit beside the nested
        // selector in the one element.
        List<String> top = new ArrayList<>();
        List<String> element = new ArrayList<>();
        String array = "";
        for (int k = 0; k < route.slots().size(); k++) {
            Slot slot = route.slots().get(k);
            String sel = selector(route, slot, k, mode, p);
            if (slot.nested()) {
                element.add(sel);
                array = slot.nestedIn();
            } else {
                top.add(sel);
            }
        }
        List<String> parts = new ArrayList<>(top);
        if (!extras.isBlank()) {
            parts.add(extras);
        }
        if (!element.isEmpty()) {
            String elementExtras = route.elementExtras().replace("{NEW}", fresh);
            parts.add("\"" + array + "\":[{" + String.join(",", element) + (elementExtras.isBlank() ? "" : "," + elementExtras) + "}]");
        }
        return p.f.send(route.method(), route.path(), "{" + String.join(",", parts) + "}");
    }

    private List<String> selectors(Route route, Mode mode, Prepared p) {
        List<String> out = new ArrayList<>();
        for (int k = 0; k < route.slots().size(); k++) {
            out.add(selector(route, route.slots().get(k), k, mode, p));
        }
        return out;
    }

    // ---- the contract, one container per route and gate --------------------------------------------------------------

    @TestFactory
    Stream<DynamicNode> everyCollectionTakingRouteAcceptsAnIdOrALabel() {
        List<DynamicNode> gates = new ArrayList<>();
        for (boolean gate3 : new boolean[] {false, true}) {
            List<DynamicNode> routes = new ArrayList<>();
            int idx = 0;
            for (Route route : CollectionRouteCensus.ROUTES) {
                int n = idx++;
                if (gate3 && route.tier() == Tier.RENAME) {
                    continue;   // from gate 3 a rename is refused: CollectionRenameRefusalTest
                }
                String stem = "r" + n + (gate3 ? "g1" : "g0");
                Prepared[] holder = new Prepared[1];
                java.util.function.Supplier<Prepared> prep = () -> {
                    fixtureFor(route).gate3.set(gate3);
                    if (holder[0] == null) {
                        holder[0] = new Prepared(route, stem);
                    }
                    return holder[0];
                };
                routes.add(DynamicContainer.dynamicContainer(route.label(), List.of(
                    DynamicTest.dynamicTest("unknownId", () -> unknownId(route, prep.get())),
                    DynamicTest.dynamicTest("bothGiven", () -> bothGiven(route, prep.get())),
                    DynamicTest.dynamicTest("nonIntegerId", () -> nonIntegerId(route, prep.get())),
                    DynamicTest.dynamicTest("idEqualsLabel", () -> idEqualsLabel(route, prep.get())))));
            }
            gates.add(DynamicContainer.dynamicContainer(gate3 ? "gate 3 holds" : "gate 3 does not hold", routes));
        }
        return gates.stream();
    }

    private void unknownId(Route route, Prepared p) {
        Resp r = call(route, Mode.UNKNOWN, p);
        assertThat(r.status()).as("%s with an id no row has: %s", route.label(), r.body()).isEqualTo(404);
        JsonNode j = r.json();
        assertThat(j.path("reason").asText()).as("typed: %s", r.body()).isEqualTo("unknown_collection_id");
        assertThat(j.path("collection_id").asLong(-1)).as("names the id: %s", r.body()).isEqualTo(UNKNOWN_ID);
        assertThat(j.path("error").asText()).as("and says so in words: %s", r.body()).contains(String.valueOf(UNKNOWN_ID));
    }

    private void bothGiven(Route route, Prepared p) {
        Resp r = call(route, Mode.BOTH, p);
        assertThat(r.status()).as("%s with a label and an id for one collection: %s", route.label(), r.body()).isEqualTo(400);
    }

    /** Passes today on a route that requires its collection (the 400 is the missing label); it proves id validation only once the id is read. */
    private void nonIntegerId(Route route, Prepared p) {
        Resp r = call(route, Mode.NON_INTEGER, p);
        assertThat(r.status()).as("%s with an id that is not an integer: %s", route.label(), r.body()).isEqualTo(400);
    }

    private void idEqualsLabel(Route route, Prepared shared) {
        // Fresh collections for this check. The three checks above share one Prepared, and under today's code a check that
        // is red because the id is unimplemented still ACTS on the label it carries (bothGiven sends a valid label, so the
        // route ignores the unknown id field and renames, inserts a topic or opens a pipeline). That would leave the label
        // side in a state its own request then refuses (a retired rename source, a duplicate root topic), and this check
        // would fail in setup instead of on the id. A route that is correct refuses bothGiven before acting, so the state
        // is only ever dirty while the behaviour is missing; a clean pair makes the check independent of that.
        Prepared p = new Prepared(route, shared.stem + "q");
        Resp byLabel = call(route, Mode.LABEL, p);
        Resp byId = call(route, Mode.ID, p);
        if (route.tier() != Tier.HEAVY) {
            assertThat(byLabel.status()).as("setup: %s by its labels reaches the route: %s", route.label(), byLabel.body())
                .isIn(route.tier() == Tier.GENERIC ? List.of(200, 404) : List.of(200));
        }
        if (route.slots().get(0).nested()) {
            // A nested-selector route (centroid upsert): the label request must have WRITTEN, or this row proves nothing: a
            // handler that ignored a record it could not read would answer 200 and pass an equal-answers comparison.
            assertThat(centroidCount(p, p.labelSide.get(0))).as("%s: the label request wrote a centroid under its label: %s",
                route.label(), byLabel.body()).isPositive();
        }
        assertThat(byId.reason()).as("%s by id is never the unknown-id refusal: %s", route.label(), byId.body())
            .isNotEqualTo("unknown_collection_id");
        assertThat(byId.status()).as("%s: the id request and the label request answer alike (label: %s, id: %s)",
            route.label(), byLabel.body(), byId.body()).isEqualTo(byLabel.status());
        JsonNode a = byLabel.json();
        JsonNode b = byId.json();
        if (a.isObject() && b.isObject()) {
            List<String> ka = new ArrayList<>();
            List<String> kb = new ArrayList<>();
            a.fieldNames().forEachRemaining(ka::add);
            b.fieldNames().forEachRemaining(kb::add);
            assertThat(kb).as("%s: the same response shape by id as by label", route.label())
                .containsExactlyInAnyOrderElementsOf(ka);
        }
        if (route.slots().get(0).nested()) {
            assertThat(centroidCount(p, p.idSide.get(0))).as("%s: the id request wrote a centroid under the id's collection: %s",
                route.label(), byId.body()).isPositive();
        }
    }

    /** The centroids stored under {@code label}. The only nested-selector row is the centroid upsert; this is its read side. */
    private long centroidCount(Prepared p, String label) {
        return p.f.get("/v1/taxonomy/centroids/count?collection=" + enc(label)).json().path("count").asLong(0);
    }

    // ---- an id reaches its own collection ----------------------------------------------------------------------------

    /**
     * The id twin of a label is that collection and no other. Two collections hold different data; a request by the id of
     * one returns that one's data. {@code idEqualsLabel} compares two empty twins and cannot tell a right collection from
     * any collection; this does, on one read route per store the P3.5a routes span.
     */
    @Test
    void idResolvesToItsOwnCollection() {
        for (boolean gate3 : new boolean[] {false, true}) {
            fx.gate3.set(gate3);
            String x = CollectionRouteFixture.label("own-x" + (gate3 ? "g1" : "g0"));
            String y = CollectionRouteFixture.label("own-y" + (gate3 ? "g1" : "g0"));
            fx.register(x, y);
            long xid = fx.idOf(x);
            long yid = fx.idOf(y);
            assertThat(xid).isNotEqualTo(yid);
            assertThat(fx.post("/v1/taxonomy/topics/insert",
                "{\"collection\":" + json(x) + ",\"label\":\"x-topic\",\"doc_count\":1}").status()).isEqualTo(200);
            assertThat(fx.post("/v1/taxonomy/topics/insert",
                "{\"collection\":" + json(y) + ",\"label\":\"y-topic\",\"doc_count\":1}").status()).isEqualTo(200);
            assertThat(fx.post("/v1/taxonomy/meta/record",
                "{\"collection\":" + json(x) + ",\"doc_count\":7}").status()).isEqualTo(200);

            // GET /v1/catalog/collections/get: the row of the id is the row of its label
            JsonNode row = fx.get("/v1/catalog/collections/get?collection_id=" + xid).json();
            assertThat(row.path("name").asText()).as("gate3=%s: the row by x's id", gate3).isEqualTo(x);
            assertThat(row.path("collection_id").asLong()).isEqualTo(xid);
            assertThat(fx.get("/v1/catalog/collections/get?collection_id=" + yid).json().path("name").asText()).isEqualTo(y);

            // GET /v1/taxonomy/topics: the topics filed under the id's collection, not the other's
            var topicsX = fx.get("/v1/taxonomy/topics?collection_id=" + xid).json();
            var topicsY = fx.get("/v1/taxonomy/topics?collection_id=" + yid).json();
            assertThat(topicsX.toString()).contains("x-topic").doesNotContain("y-topic");
            assertThat(topicsY.toString()).contains("y-topic").doesNotContain("x-topic");

            // GET /v1/taxonomy/meta/last_count: x has a recorded count, y has none
            assertThat(fx.get("/v1/taxonomy/meta/last_count?collection_id=" + xid).body())
                .isEqualTo(fx.get("/v1/taxonomy/meta/last_count?collection=" + enc(x)).body());
            assertThat(fx.get("/v1/taxonomy/meta/last_count?collection_id=" + yid).body())
                .isEqualTo(fx.get("/v1/taxonomy/meta/last_count?collection=" + enc(y)).body());
            assertThat(fx.get("/v1/taxonomy/meta/last_count?collection_id=" + xid).body())
                .isNotEqualTo(fx.get("/v1/taxonomy/meta/last_count?collection_id=" + yid).body());
        }
    }

    /**
     * Residual S-7 acceptance (bead note on nexus-p2m1x.23): "Rename, then look up by the old id: not found". A rename
     * while gate 3 does not hold is the name path, which gives the collection a new id (RDR line 2662: "until then a
     * rename gives the collection a new id"), so the old id resolves to nothing.
     */
    @Test
    void afterANamePathRenameTheOldIdIsNotFound() {
        fx.gate3.set(false);
        String from = CollectionRouteFixture.label("ren-old");
        String to = CollectionRouteFixture.label("ren-new");
        fx.register(from);
        long oldId = fx.idOf(from);
        Resp renamed = fx.post("/v1/catalog/collections/rename",
            "{\"old_name\":" + json(from) + ",\"new_name\":" + json(to) + "}");
        assertThat(renamed.status()).as(renamed.body()).isEqualTo(200);
        Resp byOldId = fx.get("/v1/catalog/collections/get?collection_id=" + oldId);
        assertThat(byOldId.status()).as(byOldId.body()).isEqualTo(404);
        assertThat(byOldId.reason()).isEqualTo("unknown_collection_id");
        assertThat(fx.get("/v1/catalog/collections/get?name=" + enc(to)).status()).as("the new label resolves").isEqualTo(200);
    }

    /**
     * Linda's ruling 2026-11-20 (bead note on nexus-p2m1x.23): "no route may accept collection_id from a client" on
     * registration. The by-id contract is for routes that SELECT a collection; the routes that REGISTER one still refuse a
     * supplied id (CollectionMint.refuseSuppliedId, today on {@code /collections/upsert}; {@code CollectionMintHttpTest}
     * covers it). This pins that the by-id work did not widen it: a registration that carries an id is a 400 and registers
     * nothing, under both gate states.
     */
    @Test
    void registrationStillRefusesASuppliedId() {
        for (boolean gate3 : new boolean[] {false, true}) {
            fx.gate3.set(gate3);
            String label = CollectionRouteFixture.label("reg-id" + (gate3 ? "g1" : "g0"));
            Resp r = fx.post("/v1/catalog/collections/upsert", "{\"name\":" + json(label)
                + ",\"content_type\":\"docs\",\"owner_id\":\"p35a\",\"embedding_model\":\"minilm-l6-v2-384\","
                + "\"model_version\":\"v1\",\"collection_id\":424242}");
            assertThat(r.status()).as(r.body()).isEqualTo(400);
            assertThat(fx.exists(label)).as("a refused registration writes nothing").isFalse();
        }
    }

    /**
     * linda's ruling 2 (2026-11-22): the 400 and 404 rules apply to id input only. A LABEL that no row has keeps today's answer
     * on the routes that refuse one: 422 {@code unregistered_collection} naming the label, never a 404, and never the
     * {@code unknown_collection_id} an id gets. The by-id work does not move it, under either gate state. The route is
     * {@code /v1/taxonomy/topics/insert}, a write the fixture can drive that refuses an unregistered label today
     * ({@code TaxonomyRepository.insertTopic} requires the registry row); the same route with an id no row has is the typed
     * 404, for contrast.
     */
    @Test
    void anUnregisteredLabelKeepsTheTyped422_whileAnUnknownIdIsTheTyped404() {
        for (boolean gate3 : new boolean[] {false, true}) {
            fx.gate3.set(gate3);
            String never = CollectionRouteFixture.label("never-registered" + (gate3 ? "g1" : "g0"));
            Resp byLabel = fx.post("/v1/taxonomy/topics/insert",
                "{\"collection\":" + json(never) + ",\"label\":\"p35a-topic\",\"doc_count\":1}");
            assertThat(byLabel.status()).as("gate3=%s, an unregistered label: %s", gate3, byLabel.body()).isEqualTo(422);
            assertThat(byLabel.reason()).as("typed: %s", byLabel.body()).isEqualTo("unregistered_collection");
            assertThat(byLabel.json().path("collection").asText()).as("names the label: %s", byLabel.body()).isEqualTo(never);
            assertThat(fx.exists(never)).as("a refused write registers nothing").isFalse();

            Resp byId = fx.post("/v1/taxonomy/topics/insert",
                "{\"collection_id\":" + UNKNOWN_ID + ",\"label\":\"p35a-topic\",\"doc_count\":1}");
            assertThat(byId.status()).as("gate3=%s, an unknown id: %s", gate3, byId.body()).isEqualTo(404);
            assertThat(byId.reason()).isEqualTo("unknown_collection_id");
        }
    }

    /**
     * SB3 (review of ede4ca3c2d, F6a): {@code POST /v1/catalog/gc_audit/record} reads {@code collection} from its body (the repository,
     * {@code recordGcAudit}), and the route table and census missed it, so a client that sent {@code collection_id} got an audit row
     * with no collection. By id, the row records the collection's label, the same as by label; the list route finds it by either.
     */
    @Test
    void aGcAuditRecordByIdIsFiledUnderTheCollectionsLabel() {
        fx.gate3.set(false);
        String label = CollectionRouteFixture.label("gc-audit");
        fx.register(label);
        long id = fx.idOf(label);
        Resp byId = fx.post("/v1/catalog/gc_audit/record", "{\"operation\":\"sb3-by-id\",\"collection_id\":" + id + "}");
        assertThat(byId.status()).as(byId.body()).isEqualTo(200);
        Resp byLabel = fx.post("/v1/catalog/gc_audit/record", "{\"operation\":\"sb3-by-label\",\"collection\":" + json(label) + "}");
        assertThat(byLabel.status()).as(byLabel.body()).isEqualTo(200);

        JsonNode listed = fx.get("/v1/catalog/gc_audit/list?collection_id=" + id).json().path("entries");
        List<String> operations = new ArrayList<>();
        listed.forEach(e -> {
            assertThat(e.path("collection").asText()).as("the row records the label: %s", e).isEqualTo(label);
            operations.add(e.path("operation").asText());
        });
        assertThat(operations).containsExactlyInAnyOrder("sb3-by-id", "sb3-by-label");
        assertThat(fx.get("/v1/catalog/gc_audit/list?collection=" + enc(label)).json().path("entries")).hasSize(2);
    }
}
