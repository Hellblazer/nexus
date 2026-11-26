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
import org.junit.jupiter.api.BeforeEach;
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
 * <p><b>What this pins, and what it does not.</b> It is the real-engine wiring complement to the stub-backed sweep in
 * {@code CollectionSelectorFilterRowsTest}: it sends the id of a no-successor tombstone to every READ route of every store and asserts the
 * route-boundary 409 each reader class answers. That is "per-reader tests over today's 409" read as the 409 a client sees from each reader,
 * which is what Sam's wording and the RDR's landing line name. It does NOT pin each reader's own id path: the boundary refuses before any
 * reader's SQL runs, so this sweep stays green if a reader reverts to name SQL (the latched-id tests of the readers are
 * {@code Rdr229P35c*ReadersByIdTest} and its siblings).
 *
 * <p>The state is the R7 one: a tombstone that holds data and names no successor (its successor was deleted, so its {@code
 * superseded_by_id} is its own id, and its stored {@code superseded_by} text names a dead label). Today's 409 by id answers {@code
 * collection_superseded} with the retired collection's label and id and NO {@code superseded_by} or {@code successor_collection_id}; the
 * successor fields appear only when a live successor exists ({@code CollectionSupersededBody}). Each census route runs with a live id as its
 * control (it is not refused as superseded and does not fail with a 5xx, so the refusal is the tombstone's), in both gate states: the route
 * boundary does not read the gate, and the two states pin that it does not start to.
 *
 * <p>Which census routes are readers is classified here, not guessed: {@link #POST_READS} and {@link #POST_WRITES} partition the census's POST
 * routes, every GET route is a reader, and {@link #theCensusIsClassified_andTheSweepCoversEveryReader} fails when a new route is in neither set.
 *
 * <p>Two more paths answer the same 409 and are pinned below with the same tombstone: the delete's verdict ({@code CollectionDeleteRoute}) and
 * the in-transaction registry read ({@code CollectionRetiredIdException}, answered by {@code HttpUtil}). The latter is a WRITE-route case
 * (a topic insert, whose handler reads the row in its own transaction), not a reader class: no read route re-reads the registry row inside
 * a transaction of its own.
 *
 * <p>The label-addressed read of such a tombstone is not here: it answers as it did (200), and its refusal is the next paired cut's
 * ({@code RetiredLabelReadGuard}, bead nexus-p2m1x.25, comments of 2026-11-24 10:52 and 10:53).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRetiredNoSuccessorReadersTest {

    /** The POST routes of the census that only read (every GET route of the census is a read). */
    static final Set<String> POST_READS = Set.of(
        "/v1/vectors/search", "/v1/vectors/query", "/v1/vectors/search-per-collection", "/v1/vectors/hybrid-search",
        "/v1/vectors/search-metadata-scoped", "/v1/vectors/search-topic-scoped", "/v1/vectors/search-graph-hop",
        "/v1/vectors/search-aspect-scoped", "/v1/vectors/get", "/v1/vectors/get-all-metadata", "/v1/vectors/store-get",
        "/v1/vectors/get-embeddings", "/v1/vectors/store-list", "/v1/vectors/manifest-less-census", "/v1/vectors/reapable",
        "/v1/taxonomy/assignments/cross-preview", "/v1/taxonomy/assignments/unassigned", "/v1/taxonomy/meta/last_discover_batch",
        "/v1/taxonomy/centroids/query", "/v1/pipeline/pages", "/v1/pipeline/chunks");

    /** The POST routes of the census that write, embed or start a job: not readers. (Re-embed's two are {@code ReembedRetiredNoSuccessorTest}'s.) */
    static final Set<String> POST_WRITES = Set.of(
        "/v1/catalog/collections/set_aspects_enabled",
        "/v1/catalog/collections/supersede",
        "/v1/catalog/collections/rehome",
        "/v1/catalog/collections/rehome/status",
        "/v1/catalog/collections/rename",
        "/v1/catalog/collections/delete",
        "/v1/catalog/gc_audit/record",
        "/v1/catalog/index-run/begin",
        "/v1/catalog/index-run/begin-many",
        "/v1/catalog/manifest/write",
        "/v1/catalog/manifest/append",
        "/v1/catalog/manifest/append_many",
        "/v1/catalog/manifest/write_many",
        "/v1/catalog/import/chunk",
        "/v1/vectors/upsert-chunks",
        "/v1/vectors/store-delete",
        "/v1/vectors/update-metadata",
        "/v1/vectors/embed",
        "/v1/vectors/gc/quarantine-orphans",
        "/v1/vectors/gc/restore-rereferenced",
        "/v1/vectors/gc/expire-quarantine",
        "/v1/vectors/gc/quarantine-restore",
        "/v1/vectors/reembed/plan",
        "/v1/vectors/reembed/jobs",
        "/v1/taxonomy/topics/insert",
        "/v1/taxonomy/assignments/assign",
        "/v1/taxonomy/assignments/assign_from_chashes",
        "/v1/taxonomy/purge_collection",
        "/v1/taxonomy/meta/record",
        "/v1/taxonomy/topics/persist_split",
        "/v1/taxonomy/topics/persist_rebuild",
        "/v1/taxonomy/topics/persist_discovered",
        "/v1/taxonomy/centroids/upsert",
        "/v1/taxonomy/centroids/delete",
        "/v1/taxonomy/centroids/purge",
        "/v1/aspects/delete",
        "/v1/aspects/salient_sentences/set_by_key",
        "/v1/aspects/upsert",
        "/v1/aspects/highlights/upsert",
        "/v1/remap/clear_leg",
        "/v1/remap/record_batch",
        "/v1/pipeline/create",
        "/v1/pipeline/delete_collection",
        "/v1/pipeline/progress",
        "/v1/pipeline/extraction_meta",
        "/v1/pipeline/complete",
        "/v1/pipeline/fail",
        "/v1/pipeline/mark_uploaded",
        "/v1/pipeline/reset_uploaded",
        "/v1/pipeline/clear_wal",
        "/v1/pipeline/delete",
        "/v1/telemetry/relevance/log",
        "/v1/telemetry/hook_failures/record");

    /** The census rows that are readers, in census order. */
    private static List<Route> readers() {
        List<Route> out = new ArrayList<>();
        for (Route r : CollectionRouteCensus.ROUTES) {
            if ("GET".equals(r.method()) || POST_READS.contains(r.path())) {
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
    void stop() {
        if (fx != null) fx.close();
    }

    /** The routes the {@code @Test} methods below send need the production regime; the sweep sets its own gate per route. */
    @BeforeEach
    void latched() {
        fx.gate3.set(true);
    }

    /**
     * The census is classified and the sweep covers all of it: every POST route is a reader or a write (a new route must be put in one), the
     * two sets name routes the census has, every reader is a one-slot STANDARD-engine row the request builder below can send, and the number
     * of readers is exactly what the sweep runs per gate state.
     */
    @Test
    void theCensusIsClassified_andTheSweepCoversEveryReader() {
        Set<String> census = new java.util.TreeSet<>();
        Set<String> posts = new java.util.TreeSet<>();
        for (Route r : CollectionRouteCensus.ROUTES) {
            census.add(r.method() + " " + r.path());
            if ("POST".equals(r.method())) posts.add(r.path());
        }
        for (String path : POST_READS) assertThat(census).as("POST_READS names a census route").contains("POST " + path);
        for (String path : POST_WRITES) assertThat(census).as("POST_WRITES names a census route").contains("POST " + path);
        assertThat(java.util.stream.Stream.concat(POST_READS.stream(), POST_WRITES.stream()).distinct().count())
            .as("no path is both a reader and a write").isEqualTo(POST_READS.size() + POST_WRITES.size());
        Set<String> unclassified = new java.util.TreeSet<>(posts);
        unclassified.removeAll(POST_READS);
        unclassified.removeAll(POST_WRITES);
        assertThat(unclassified).as("every census POST route is classified as a reader or a write here: classify the new route").isEmpty();

        List<Route> readers = readers();
        for (Route r : readers) {
            assertThat(r.slots()).as(r.label() + " is a one-slot reader").hasSize(1);
            assertThat(r.slots().get(0).nested()).as(r.label()).isFalse();
            assertThat(r.engine()).as(r.label()).isEqualTo(CollectionRouteCensus.Engine.STANDARD);
        }
        assertThat(readers.size()).as("the readers the sweep runs, in each gate state").isEqualTo(53);
        assertThat(readers.stream().map(Route::handler).distinct().sorted().toList()).as("non-vacuity: every store has readers").containsExactly(
            "AspectHandler.java", "CatalogHandler.java", "ChashHandler.java", "PipelineHandler.java", "RemapHandler.java",
            "TaxonomyHandler.java", "TelemetryHandler.java", "VectorHandler.java");
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
                    assertThat(control.status()).as("control: a live id does not fail the route: " + control.body()).isLessThan(500);
                    assertThat(control.status() == 409 && "collection_superseded".equals(control.reason()))
                        .as("control: a live id is not refused as superseded: " + control.body()).isFalse();

                    Resp r = send(route, tombstoneId);

                    assertThat(r.status()).as("%s by the id of a tombstone with no successor: %s", route.label(), r.body()).isEqualTo(409);
                    NoSuccessorBodyAsserts.assertBareMarker(r.json(), route.label(), tombstone, tombstoneId);
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
        NoSuccessorBodyAsserts.assertBareMarker(r.json(), "delete by id", x, xId);
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
        NoSuccessorBodyAsserts.assertBareMarker(r.json(), "the delete's own verdict", x, xId);
        assertThat(fx.exists(x)).as("the refused delete removed nothing").isTrue();
    }

    /**
     * A WRITE-route case, not a reader class: the handler's own read of the retired row, inside its transaction: a hook retires the row (a supersede, then the deletion of its
     * successor) between the boundary, which found it live, and the handler, so the 409 is the transaction's own, not the boundary's.
     */
    @Test
    void aWriteRouteByAnIdRetiredWithNoSuccessorBetweenTheBoundaryAndTheHandlerAnswers409NamingNoSuccessor() {
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
        NoSuccessorBodyAsserts.assertBareMarker(r.json(), "the handler's own read of the retired row", a, x);
        assertThat((int) (Integer) fx.asSuperuser(ctx -> ctx.fetchCount(TOPICS, TOPICS.COLLECTION.eq(a)))).isZero();
    }
}
