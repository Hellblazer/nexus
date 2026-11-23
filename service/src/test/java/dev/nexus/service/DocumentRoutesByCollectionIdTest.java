// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.CollectionRouteCensus.Route;
import dev.nexus.service.CollectionRouteFixture.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DynamicContainer;
import org.junit.jupiter.api.DynamicNode;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import org.junit.jupiter.api.TestInstance;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Stream;

import static dev.nexus.service.CollectionRouteCensus.UNKNOWN_ID;
import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), linda's ruling D1: the document routes are collection routes. Each route that writes or moves a
 * document ({@link CollectionRouteCensus#DOCUMENT_ROUTES}: register, doc/register, doc/register_many, import/document,
 * doc/resolve_or_register, update, update_many) accepts {@code physical_collection_id} beside {@code physical_collection}.
 *
 * <p>Additive on the wire: a label alone is answered as before. For each route:
 * <ul>
 *   <li>an id alone resolves, and the stored document carries that row's label and id;</li>
 *   <li>a label alone is stored with the label and the id of the row it names, and the id request is answered like it;</li>
 *   <li>both is a 400, an id that is not a JSON integer (a string, a fraction, an explicit null) is a 400, an id no row has is the typed
 *       404 {@code unknown_collection_id}, a label no row has keeps the typed 422 {@code unregistered_collection}, a retired row's id
 *       is the typed 409 {@code collection_superseded}; every refusal writes nothing.</li>
 * </ul>
 *
 * <p>The id reaches the write as the resolved row, in the write's own transaction ({@code DocumentCollectionRef}): the last test
 * here is the case the review named (F4), an id whose label was freed and taken again, which must never reach the new holder.
 * Requests carry the tokens {@link CollectionRouteCensus#TOKEN_SEQ}, {@link CollectionRouteCensus#TOKEN_TITLE} and
 * {@link CollectionRouteCensus#TOKEN_DOC}; a stored document is found by the title of its request.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class DocumentRoutesByCollectionIdTest {

    CollectionRouteFixture fx;

    private final AtomicInteger seq = new AtomicInteger(1000);

    @BeforeAll
    void start() throws Exception {
        fx = new CollectionRouteFixture();
        fx.gate3.set(false);
    }

    @AfterAll
    void stop() {
        if (fx != null) fx.close();
    }

    /** A stored document's collection, as the table holds it. */
    private record Stored(String label, Long id) { }

    private static String titleOf(int n) {
        return "p35a-doc-" + n;
    }

    private List<Stored> stored(String title) {
        return fx.asSuperuser(ctx -> ctx.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
            .from(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(fx.tenant)).and(CATALOG_DOCUMENTS.TITLE.eq(title))
            .fetch(r -> new Stored(r.value1(), r.value2())));
    }

    private String expand(String text, int n, String doc) {
        return text.replace(CollectionRouteCensus.TOKEN_SEQ, String.valueOf(n))
            .replace(CollectionRouteCensus.TOKEN_TITLE, titleOf(n))
            .replace(CollectionRouteCensus.TOKEN_DOC, doc == null ? "" : doc);
    }

    /** A document registered in a collection of its own, for the update routes to move; its tumbler. Null for a route that needs none. */
    private String seedDocument(Route route, int n) {
        if (!(route.extras() + route.elementExtras()).contains(CollectionRouteCensus.TOKEN_DOC)) {
            return null;
        }
        String home = CollectionRouteFixture.label("seed" + n);
        fx.register(home);
        String tumbler = "p35a." + n + ".9";
        Resp r = fx.post("/v1/catalog/register", "{\"tumbler\":\"" + tumbler + "\",\"title\":" + json(titleOf(n) + "-seed")
            + ",\"content_type\":\"knowledge\",\"corpus\":\"knowledge\",\"physical_collection\":" + json(home) + "}");
        assertThat(r.status()).as("setup: the document the update moves: %s", r.body()).isEqualTo(200);
        return tumbler;
    }

    /** The route's request with {@code selector} (the collection field or fields, as JSON members) in it. */
    private Resp send(Route route, String selector, int n, String doc) {
        String extras = expand(route.extras(), n, doc);
        List<String> parts = new ArrayList<>();
        if (route.slots().get(0).nested()) {
            if (!extras.isBlank()) {
                parts.add(extras);
            }
            String element = expand(route.elementExtras(), n, doc);
            parts.add("\"" + route.slots().get(0).nestedIn() + "\":[{" + selector + (element.isBlank() ? "" : "," + element) + "}]");
        } else {
            parts.add(selector);
            if (!extras.isBlank()) {
                parts.add(extras);
            }
        }
        return fx.post(route.path(), "{" + String.join(",", parts) + "}");
    }

    private static String byLabel(String label) {
        return "\"physical_collection\":" + json(label);
    }

    private static String byId(Object id) {
        return "\"physical_collection_id\":" + id;
    }

    private record Sides(String a, long idA, String b, long idB) { }

    private Sides sides(String stem) {
        String a = CollectionRouteFixture.label(stem + "a");
        String b = CollectionRouteFixture.label(stem + "b");
        fx.register(a, b);
        return new Sides(a, fx.idOf(a), b, fx.idOf(b));
    }

    // ---- the contract, one container per route -------------------------------------------------------------------------

    @TestFactory
    Stream<DynamicNode> everyDocumentRouteAcceptsAnIdOrALabel() {
        List<DynamicNode> routes = new ArrayList<>();
        int idx = 0;
        assertThat(CollectionRouteCensus.DOCUMENT_ROUTES).as("non-vacuity: the document routes").hasSize(7);
        for (Route route : CollectionRouteCensus.DOCUMENT_ROUTES) {
            String stem = "doc" + idx++;
            routes.add(DynamicContainer.dynamicContainer(route.label(), List.of(
                DynamicTest.dynamicTest("labelAlone", () -> labelAlone(route, stem + "l")),
                DynamicTest.dynamicTest("idAlone", () -> idAlone(route, stem + "i")),
                DynamicTest.dynamicTest("bothGiven", () -> bothGiven(route, stem + "b")),
                DynamicTest.dynamicTest("unknownId", () -> unknownId(route, stem + "u")),
                DynamicTest.dynamicTest("anIdThatIsNotAnInteger", () -> notAnInteger(route, stem + "n")),
                DynamicTest.dynamicTest("anUnregisteredLabel", () -> unregisteredLabel(route, stem + "r")),
                DynamicTest.dynamicTest("aRetiredId", () -> retiredId(route, stem + "t")))));
        }
        return routes.stream();
    }

    private void labelAlone(Route route, String stem) {
        Sides s = sides(stem);
        int n = seq.incrementAndGet();
        Resp r = send(route, byLabel(s.a()), n, seedDocument(route, n));
        assertThat(r.status()).as("%s by label: %s", route.label(), r.body()).isEqualTo(200);
        assertThat(stored(titleOf(n))).as("%s: stored under the label and the id of its row", route.label())
            .containsExactly(new Stored(s.a(), s.idA()));
    }

    private void idAlone(Route route, String stem) {
        Sides s = sides(stem);
        int byLabelN = seq.incrementAndGet();
        Resp byLabel = send(route, byLabel(s.a()), byLabelN, seedDocument(route, byLabelN));
        int n = seq.incrementAndGet();
        Resp r = send(route, byId(s.idB()), n, seedDocument(route, n));
        assertThat(r.status()).as("%s by id: %s", route.label(), r.body()).isEqualTo(200);
        assertThat(stored(titleOf(n))).as("%s: the id names the row, so its label and id are stored", route.label())
            .containsExactly(new Stored(s.b(), s.idB()));
        assertThat(r.status()).as("the id request is answered like the label request").isEqualTo(byLabel.status());
        JsonNode a = byLabel.json();
        JsonNode b = r.json();
        if (a.isObject() && b.isObject()) {
            List<String> ka = new ArrayList<>();
            List<String> kb = new ArrayList<>();
            a.fieldNames().forEachRemaining(ka::add);
            b.fieldNames().forEachRemaining(kb::add);
            assertThat(kb).as("%s: the same response shape by id as by label", route.label()).containsExactlyInAnyOrderElementsOf(ka);
        }
    }

    private void bothGiven(Route route, String stem) {
        Sides s = sides(stem);
        int n = seq.incrementAndGet();
        Resp r = send(route, byLabel(s.a()) + "," + byId(s.idB()), n, seedDocument(route, n));
        assertThat(r.status()).as("%s with a label and an id: %s", route.label(), r.body()).isEqualTo(400);
        assertThat(stored(titleOf(n))).as("nothing is written").isEmpty();
    }

    private void unknownId(Route route, String stem) {
        sides(stem);
        int n = seq.incrementAndGet();
        Resp r = send(route, byId(UNKNOWN_ID), n, seedDocument(route, n));
        assertThat(r.status()).as("%s with an id no row has: %s", route.label(), r.body()).isEqualTo(404);
        assertThat(r.reason()).as("typed: %s", r.body()).isEqualTo("unknown_collection_id");
        assertThat(r.json().path("collection_id").asLong(-1)).as("names the id: %s", r.body()).isEqualTo(UNKNOWN_ID);
        assertThat(stored(titleOf(n))).as("nothing is written").isEmpty();
    }

    private void notAnInteger(Route route, String stem) {
        sides(stem);
        for (String bad : new String[] {"\"12\"", "1.5", "null", "true"}) {
            int n = seq.incrementAndGet();
            Resp r = send(route, byId(bad), n, seedDocument(route, n));
            assertThat(r.status()).as("%s with physical_collection_id %s: %s", route.label(), bad, r.body()).isEqualTo(400);
            assertThat(stored(titleOf(n))).as("nothing is written").isEmpty();
        }
    }

    private void unregisteredLabel(Route route, String stem) {
        String never = CollectionRouteFixture.label(stem + "never");
        int n = seq.incrementAndGet();
        Resp r = send(route, byLabel(never), n, seedDocument(route, n));
        assertThat(r.status()).as("%s with a label no row has: %s", route.label(), r.body()).isEqualTo(422);
        assertThat(r.reason()).as("typed: %s", r.body()).isEqualTo("unregistered_collection");
        assertThat(r.json().path("collection").asText()).as("names the label: %s", r.body()).isEqualTo(never);
        assertThat(stored(titleOf(n))).as("nothing is written").isEmpty();
        assertThat(fx.exists(never)).as("a refused write registers nothing").isFalse();
    }

    private void retiredId(Route route, String stem) {
        Sides s = sides(stem);
        Resp superseded = fx.post("/v1/catalog/collections/supersede",
            "{\"name\":" + json(s.a()) + ",\"superseded_by\":" + json(s.b()) + "}");
        assertThat(superseded.status()).as("setup: retire the first collection: %s", superseded.body()).isEqualTo(200);
        int n = seq.incrementAndGet();
        Resp r = send(route, byId(s.idA()), n, seedDocument(route, n));
        assertThat(r.status()).as("%s with the id of a retired row: %s", route.label(), r.body()).isEqualTo(409);
        assertThat(r.reason()).as("typed: %s", r.body()).isEqualTo("collection_superseded");
        assertThat(r.json().path("collection").asText()).as("names the retired label: %s", r.body()).isEqualTo(s.a());
        assertThat(r.json().path("superseded_by").asText()).as("and its successor: %s", r.body()).isEqualTo(s.b());
        assertThat(stored(titleOf(n))).as("nothing is written").isEmpty();
    }

    // ---- the id reaches the write as the resolved row -------------------------------------------------------------------

    /**
     * Review F4: an id resolved to a label and looked up by name again lets a label that was freed and taken by another collection send
     * the write to the wrong one. The old id of a deleted collection, whose label a new collection now holds, never reaches the new
     * holder: the id names no row, and nothing is written under the label.
     */
    @Test
    void anIdWhoseLabelWasFreedAndTakenAgainNeverReachesTheNewHolder() {
        String label = CollectionRouteFixture.label("recycled");
        fx.register(label);
        long oldId = fx.idOf(label);
        Resp deleted = fx.post("/v1/catalog/collections/delete", "{\"name\":" + json(label) + "}");
        assertThat(deleted.status()).as("setup: free the label: %s", deleted.body()).isEqualTo(200);
        fx.register(label);
        long newId = fx.idOf(label);
        assertThat(newId).as("setup: the label is held by a new row").isNotEqualTo(oldId);

        for (Route route : CollectionRouteCensus.DOCUMENT_ROUTES) {
            int n = seq.incrementAndGet();
            Resp r = send(route, byId(oldId), n, seedDocument(route, n));
            assertThat(r.status()).as("%s by the old id: %s", route.label(), r.body()).isEqualTo(404);
            assertThat(r.reason()).isEqualTo("unknown_collection_id");
            assertThat(stored(titleOf(n))).as("%s: nothing reached the label's new holder", route.label()).isEmpty();
        }
        int n = seq.incrementAndGet();
        Resp byNewId = send(CollectionRouteCensus.DOCUMENT_ROUTES.get(1), byId(newId), n, null);
        assertThat(byNewId.status()).as(byNewId.body()).isEqualTo(200);
        assertThat(stored(titleOf(n))).containsExactly(new Stored(label, newId));
    }

    // ---- batches name each document's collection separately -------------------------------------------------------------

    @Test
    void aRegisterManyBatchMixesALabelAndAnId() {
        Sides s = sides("many");
        int n = seq.incrementAndGet();
        Resp r = fx.post("/v1/catalog/doc/register_many", "{\"owner_prefix\":\"p35a." + n + "\",\"docs\":["
            + "{" + byLabel(s.a()) + ",\"title\":" + json(titleOf(n)) + ",\"file_path\":\"m-" + n + "-1.md\"},"
            + "{" + byId(s.idB()) + ",\"title\":" + json(titleOf(n + 5000)) + ",\"file_path\":\"m-" + n + "-2.md\"}]}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("tumblers")).hasSize(2);
        assertThat(stored(titleOf(n))).containsExactly(new Stored(s.a(), s.idA()));
        assertThat(stored(titleOf(n + 5000))).containsExactly(new Stored(s.b(), s.idB()));
    }

    @Test
    void anImportBatchMixesALabelAndAnId() {
        Sides s = sides("import");
        int n = seq.incrementAndGet();
        Resp r = fx.post("/v1/catalog/import/document", "{\"rows\":["
            + "{\"tumbler\":\"p35a." + n + ".1\"," + byLabel(s.a()) + ",\"title\":" + json(titleOf(n)) + "},"
            + "{\"tumbler\":\"p35a." + n + ".2\"," + byId(s.idB()) + ",\"title\":" + json(titleOf(n + 5000)) + "}]}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(stored(titleOf(n))).containsExactly(new Stored(s.a(), s.idA()));
        assertThat(stored(titleOf(n + 5000))).containsExactly(new Stored(s.b(), s.idB()));
    }

    @Test
    void anUpdateManyBatchMixesALabelAndAnId() {
        Sides s = sides("updmany");
        Route route = CollectionRouteCensus.DOCUMENT_ROUTES.get(6);
        int n1 = seq.incrementAndGet();
        int n2 = seq.incrementAndGet();
        String d1 = seedDocument(route, n1);
        String d2 = seedDocument(route, n2);
        Resp r = fx.post("/v1/catalog/update_many", "{\"updates\":["
            + "{\"tumbler\":\"" + d1 + "\"," + byLabel(s.a()) + ",\"title\":" + json(titleOf(n1)) + "},"
            + "{\"tumbler\":\"" + d2 + "\"," + byId(s.idB()) + ",\"title\":" + json(titleOf(n2)) + "}]}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("updated")).hasSize(2);
        assertThat(r.json().path("updated").get(0).asInt()).isEqualTo(1);
        assertThat(r.json().path("updated").get(1).asInt()).isEqualTo(1);
        assertThat(stored(titleOf(n1))).containsExactly(new Stored(s.a(), s.idA()));
        assertThat(stored(titleOf(n2))).containsExactly(new Stored(s.b(), s.idB()));
    }

    /**
     * The legacy register writes the owner row, then the document, in two transactions. A label and an id together are refused before
     * the owner write, so a refused request leaves no owner behind.
     */
    @Test
    void theLegacyRegisterRefusesALabelAndAnIdBeforeItWritesTheOwner() {
        Sides s = sides("legacy");
        int n = seq.incrementAndGet();
        String prefix = "pown." + n;
        Resp r = fx.post("/v1/catalog/register", "{\"tumbler_prefix\":\"" + prefix + "\",\"name\":\"p35a-owner-" + n
            + "\",\"owner_type\":\"curator\",\"tumbler\":\"" + prefix + ".1\",\"title\":" + json(titleOf(n)) + ","
            + byLabel(s.a()) + "," + byId(s.idB()) + "}");
        assertThat(r.status()).as(r.body()).isEqualTo(400);
        boolean ownerWritten = fx.asSuperuser(ctx -> ctx.fetchExists(CATALOG_OWNERS,
            CATALOG_OWNERS.TENANT_ID.eq(fx.tenant).and(CATALOG_OWNERS.TUMBLER_PREFIX.eq(prefix))));
        assertThat(ownerWritten).as("no owner row for a refused request").isFalse();
        assertThat(stored(titleOf(n))).isEmpty();
    }
}
