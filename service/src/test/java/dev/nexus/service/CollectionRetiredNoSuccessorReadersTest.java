// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteCensus.Route;
import dev.nexus.service.CollectionRouteCensus.Slot;
import dev.nexus.service.CollectionRouteCensus.Where;
import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
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
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Stream;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5c (bead nexus-p2m1x.25), the per-reader tests that Sam's "Split" put in this cut ("the guard message, the re-embed target
 * check refusing, and per-reader tests over today's 409", T2 {@code nexus_rdr/229-research-328}, corrected there) and that shipped only for
 * the re-embed readers ({@code ReembedRetiredNoSuccessorTest}); the RDR's P3.5c landing line says the rest are carried on this bead.
 *
 * <p>The state is the R7 one: a tombstone that holds data and names no successor (its successor was deleted, so its {@code
 * superseded_by_id} is its own id, and its stored {@code superseded_by} text names a dead label). Today's 409 by id answers {@code
 * collection_superseded} with the retired collection's label and id and NO {@code superseded_by} or {@code successor_collection_id}; the
 * successor fields appear only when a live successor exists ({@code CollectionSupersededBody}). This class sends that id to every READ
 * route of every store the census lists, on the real engine, and asserts the body each reader class answers: the catalog, vector,
 * taxonomy, aspect, chash, remap, telemetry and pipeline readers, one dynamic test per route, with a live id as each route's control (it is
 * not refused as superseded, so the refusal is the tombstone's and the route could have answered otherwise).
 *
 * <p>Two more reader classes answer the same 409 and are pinned below with the same tombstone: the delete's verdict ({@code
 * CollectionDeleteRoute}, by id) and the handler's own read of the retired row inside its transaction, reached when the row is retired in
 * the gap between the route boundary and the handler ({@code CollectionRetiredIdException}, answered by {@code HttpUtil}).
 *
 * <p>The label-addressed read of such a tombstone is not here: it answers as it did (200), and its refusal is the next paired cut's
 * ({@code RetiredLabelReadGuard}, bead nexus-p2m1x.25, comments of 2026-11-24 10:52 and 10:53).
 *
 * <p>Each route runs with gate 3 not held and held: the route boundary does not read the gate, and the two states pin that it does not
 * start to.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRetiredNoSuccessorReadersTest {

    /** The POST routes of the census that only read (every GET route of the census is a read). */
    private static final Set<String> POST_READS = Set.of(
        "/v1/vectors/search", "/v1/vectors/query", "/v1/vectors/search-per-collection", "/v1/vectors/hybrid-search",
        "/v1/vectors/search-metadata-scoped", "/v1/vectors/search-aspect-scoped", "/v1/vectors/get",
        "/v1/vectors/get-all-metadata", "/v1/vectors/store-get", "/v1/vectors/get-embeddings", "/v1/vectors/store-list",
        "/v1/vectors/manifest-less-census", "/v1/vectors/reapable",
        "/v1/taxonomy/assignments/cross-preview", "/v1/taxonomy/assignments/unassigned", "/v1/taxonomy/meta/last_discover_batch");

    /** The census rows that are readers, in census order. */
    private static List<Route> readers() {
        List<Route> out = new ArrayList<>();
        for (Route r : CollectionRouteCensus.ROUTES) {
            boolean read = "GET".equals(r.method()) || POST_READS.contains(r.path());
            if (read && r.slots().size() == 1 && !r.slots().get(0).nested() && r.engine() == CollectionRouteCensus.Engine.STANDARD) {
                out.add(r);
            }
        }
        return out;
    }

    CollectionRouteFixture fx;
    String live;
    String tombstone;
    long liveId;
    long tombstoneId;
    final AtomicInteger examined = new AtomicInteger();
    final Map<String, Integer> examinedByStore = new TreeMap<>();

    @BeforeAll
    void start() throws Exception {
        fx = new CollectionRouteFixture();
        live = label("rnr-live");
        tombstone = label("rnr-tomb");
        String successor = label("rnr-succ");
        fx.register(live, tombstone, successor);
        liveId = fx.idOf(live);
        tombstoneId = fx.idOf(tombstone);
        // the tombstone holds data, so it outlives its successor
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant, tombstone, List.of(Chash.ofText("rnr/chunk").toHex()),
                List.of("retired text"), List.of(new float[384]), List.of(Map.<String, Object>of()));
            return null;
        });
        assertThat(fx.post("/v1/catalog/collections/supersede",
            "{\"name\":" + json(tombstone) + ",\"superseded_by\":" + json(successor) + "}").status()).isEqualTo(200);
        assertThat(fx.post("/v1/catalog/collections/delete", "{\"name\":" + json(successor) + "}").status()).isEqualTo(200);
        assertThat(fx.exists(tombstone)).as("setup: the tombstone outlives its successor").isTrue();
        // the R7 shape, read from the row and not from the wire the tests below check: the tombstone's successor id is its own id
        Long successorId = fx.asSuperuser(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(tombstoneId))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID));
        assertThat(successorId).as("setup: a tombstone that names no successor has its own id as its successor's").isEqualTo(tombstoneId);
    }

    @AfterAll
    void floor() {
        try {
            assertThat(readers().size()).as("non-vacuity: the census has its reader routes").isGreaterThanOrEqualTo(40);
            assertThat(examined.get()).as("every reader route ran in both gate states, by store " + examinedByStore)
                .isEqualTo(2 * readers().size());
            for (String store : List.of("CatalogHandler.java", "VectorHandler.java", "TaxonomyHandler.java", "AspectHandler.java",
                    "ChashHandler.java", "RemapHandler.java", "TelemetryHandler.java", "PipelineHandler.java")) {
                assertThat(examinedByStore.getOrDefault(store, 0)).as("non-vacuity: the " + store + " readers").isPositive();
            }
        } finally {
            System.err.printf("retired_no_successor_readers examined=%d by_store=%s%n", examined.get(), examinedByStore);
            if (fx != null) fx.close();
        }
    }

    private static String request(Route route, long id) {
        Slot slot = route.slots().get(0);
        if (route.where() == Where.QUERY) {
            return route.path() + "?" + slot.idField() + "=" + id + route.extras();
        }
        String field = "\"" + slot.idField() + "\":" + (slot.list() ? "[" + id + "]" : String.valueOf(id));
        return field + (route.extras().isBlank() ? "" : "," + route.extras());
    }

    private Resp send(Route route, long id) {
        String r = request(route, id);
        return route.where() == Where.QUERY ? fx.send(route.method(), r, null) : fx.send(route.method(), route.path(), "{" + r + "}");
    }

    @TestFactory
    Stream<DynamicNode> everyReaderRouteAnswersATombstoneWithNoSuccessorTheSameBareMarker() {
        List<DynamicNode> gates = new ArrayList<>();
        for (boolean gate3 : new boolean[] {false, true}) {
            Map<String, List<DynamicNode>> byStore = new TreeMap<>();
            for (Route route : readers()) {
                byStore.computeIfAbsent(route.handler(), k -> new ArrayList<>()).add(DynamicTest.dynamicTest(route.label(), () -> {
                    fx.gate3.set(gate3);
                    Resp control = send(route, liveId);
                    assertThat(control.status() == 409 && "collection_superseded".equals(control.reason()))
                        .as("control: a live id is not refused as superseded: " + control.body()).isFalse();

                    Resp r = send(route, tombstoneId);

                    assertThat(r.status()).as("%s by the id of a tombstone with no successor: %s", route.label(), r.body()).isEqualTo(409);
                    assertThat(r.reason()).isEqualTo("collection_superseded");
                    assertThat(r.json().path("error").asText()).isEqualTo("collection_superseded");
                    assertThat(r.json().path("collection").asText()).as(r.body()).isEqualTo(tombstone);
                    assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(tombstoneId);
                    assertThat(r.json().has("superseded_by")).as("no successor, no superseded_by: %s", r.body()).isFalse();
                    assertThat(r.json().has("successor_collection_id")).as("no successor_collection_id: %s", r.body()).isFalse();
                    assertThat(r.json().path("message").asText()).as("the message names none: %s", r.body())
                        .contains("names no live successor");
                    examined.incrementAndGet();
                    synchronized (examinedByStore) {
                        examinedByStore.merge(route.handler(), 1, Integer::sum);
                    }
                }));
            }
            List<DynamicNode> stores = new ArrayList<>();
            byStore.forEach((store, tests) -> stores.add(DynamicContainer.dynamicContainer(store, tests)));
            gates.add(DynamicContainer.dynamicContainer(gate3 ? "gate 3 holds" : "gate 3 does not hold", stores));
        }
        return gates.stream();
    }

    // ---- the other two reader classes that answer today's 409 -------------------------------------------------------------------------

    private static final String DELETE = "/v1/catalog/collections/delete";
    private int n;

    private String registered(String stem) {
        String l = label("rnr-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    private void seedChunk(String collection) {
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(Chash.ofText(collection + "/chunk").toHex()),
                List.of("retired text"), List.of(new float[384]), List.of(Map.<String, Object>of()));
            return null;
        });
    }

    /** A delete by the id of a tombstone: refused at the boundary, nothing is deleted. */
    @Test
    void theDeleteByTheIdOfATombstoneWithNoSuccessorIs409WithNoSuccessorFields_andDeletesNothing() {
        String x = registered("del-x");
        String y = registered("del-y");
        seedChunk(x);
        long xId = fx.idOf(x);
        assertThat(fx.post("/v1/catalog/collections/supersede",
            "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}").status()).isEqualTo(200);
        assertThat(fx.post(DELETE, "{\"name\":" + json(y) + "}").status()).isEqualTo(200);
        assertThat(fx.exists(x)).as("setup: the tombstone outlives its successor").isTrue();

        Resp r = fx.post(DELETE, "{\"collection_id\":" + xId + "}");

        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(xId);
        assertThat(r.json().has("superseded_by")).as("no successor: %s", r.body()).isFalse();
        assertThat(r.json().has("successor_collection_id")).isFalse();
        assertThat(fx.exists(x)).as("the refused delete removed nothing").isTrue();
        assertThat((int) (Integer) fx.asSuperuser(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(fx.tenant).and(CHUNKS.COLLECTION.eq(x)))))
            .as("and its chunk is still there").isEqualTo(1);
    }

    /**
     * The same verb when the row is retired between the boundary and the verdict: the boundary found it live, so the 409 is the delete's own
     * ({@code CollectionDeleteRoute}'s verdict branch, {@code CollectionDeletes}), not the filter's.
     */
    @Test
    void aDeleteByAnIdRetiredWithNoSuccessorBetweenTheBoundaryAndTheVerdictAnswers409NamingNoSuccessor() {
        String x = registered("delgap-x");
        String y = registered("delgap-y");
        seedChunk(x);
        long xId = fx.idOf(x);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger supersedeStatus = new AtomicInteger();
        AtomicInteger successorDeleteStatus = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(() -> {
            if (fired.getAndIncrement() == 0) {
                supersedeStatus.set(fx.post("/v1/catalog/collections/supersede",
                    "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}").status());
                successorDeleteStatus.set(fx.post(DELETE, "{\"name\":" + json(y) + "}").status());
            }
        }, null, null, null);
        Resp r;
        try {
            r = fx.post(DELETE, "{\"collection_id\":" + xId + "}");
        } finally {
            fx.service.collectionSelectorFilter().testHooks(null, null, null, null);
        }

        assertThat(supersedeStatus.get()).as("setup: the hook retired the row").isEqualTo(200);
        assertThat(successorDeleteStatus.get()).as("setup: and deleted its successor").isEqualTo(200);
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(xId);
        assertThat(r.json().has("superseded_by")).as("the delete's own verdict: no successor: %s", r.body()).isFalse();
        assertThat(r.json().has("successor_collection_id")).isFalse();
        assertThat(fx.exists(x)).as("the refused delete removed nothing").isTrue();
    }

    /**
     * The handler's own read of the retired row, inside its transaction: a hook retires the row (a supersede, then the deletion of its
     * successor) between the boundary, which found it live, and the handler, so the 409 is the transaction's own, not the boundary's.
     */
    @Test
    void aWriteByAnIdRetiredWithNoSuccessorBetweenTheBoundaryAndTheHandlerAnswers409NamingNoSuccessor() {
        String a = registered("gap-a");
        String b = registered("gap-b");
        seedChunk(a);   // the tombstone holds data, so it outlives the successor the hook deletes
        long x = fx.idOf(a);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger supersedeStatus = new AtomicInteger();
        AtomicInteger deleteStatus = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(() -> {
            if (fired.getAndIncrement() == 0) {   // the calls below pass through the boundary too: only the first acts
                supersedeStatus.set(fx.post("/v1/catalog/collections/supersede",
                    "{\"name\":" + json(a) + ",\"superseded_by\":" + json(b) + "}").status());
                deleteStatus.set(fx.post(DELETE, "{\"name\":" + json(b) + "}").status());
            }
        }, null, null, null);
        Resp r;
        try {
            r = fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + x + ",\"label\":\"gap-topic\",\"doc_count\":1}");
        } finally {
            fx.service.collectionSelectorFilter().testHooks(null, null, null, null);
        }
        assertThat(supersedeStatus.get()).as("setup: the hook retired the row").isEqualTo(200);
        assertThat(deleteStatus.get()).as("setup: and deleted its successor").isEqualTo(200);
        assertThat(fx.exists(a)).as("setup: the tombstone is still there").isTrue();
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        assertThat(r.json().path("collection").asText()).isEqualTo(a);
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(x);
        assertThat(r.json().has("superseded_by")).as("the handler's own read of the retired row: no successor: %s", r.body()).isFalse();
        assertThat(r.json().has("successor_collection_id")).isFalse();
        assertThat((int) (Integer) fx.asSuperuser(ctx -> ctx.fetchCount(TOPICS, TOPICS.COLLECTION.eq(a)))).isZero();
    }
}
