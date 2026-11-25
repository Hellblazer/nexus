// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DynamicNode;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.TestFactory;
import org.junit.jupiter.api.TestInstance;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.LongFunction;
import java.util.stream.Stream;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), linda's ruling: a writer reads its collection's row {@code FOR KEY SHARE} first, with nothing to write too,
 * so a by-id call answers the same whatever its batch size. Each writer that used to return at once on an empty batch (and claimed
 * {@code nothingToWrite}) is driven here by {@code collection_id} with an EMPTY batch: against a live row it answers its usual 200, and
 * when the collection's delete frees the id after the route boundary resolved it, it answers 404 {@code unknown_collection_id}, where it
 * used to answer 200. One case per method: {@code PgVectorRepository} {@code delete}, {@code updateMetadataWithMissing}, {@code restoreAcross}
 * (no sibling) and {@code markedOnly} (an empty selection); {@code CatalogRepository} {@code importChunksBatch} and
 * {@code refuseRowsUnderSupersededCollection}; {@code AspectRepository.upsertAspect} (a payload below the confidence floor).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class EmptyBatchByIdRoutesTest {

    private static final String DELETE = "/v1/catalog/collections/delete";

    /** One route: its method name, its path, and the body for a collection id. */
    private record Case(String method, String path, LongFunction<String> body) {
    }

    private static final List<Case> CASES = List.of(
        new Case("PgVectorRepository.delete", "/v1/vectors/store-delete", x -> "{\"collection_id\":" + x + ",\"ids\":[]}"),
        new Case("PgVectorRepository.updateMetadataWithMissing", "/v1/vectors/update-metadata",
            x -> "{\"collection_id\":" + x + ",\"ids\":[],\"metadatas\":[]}"),
        new Case("PgVectorRepository.restoreAcross (no sibling)", "/v1/vectors/gc/quarantine-restore",
            x -> "{\"origin_collection_id\":" + x + ",\"chashes\":[\"" + Chash.ofText("empty-batch-restore").toHex() + "\"]}"),
        new Case("PgVectorRepository.markedOnly (an empty selection)", "/v1/vectors/gc/quarantine-restore",
            x -> "{\"origin_collection_id\":" + x + ",\"quarantined_since\":\"2020-01-01T00:00:00Z\"}"),
        new Case("CatalogRepository.importChunksBatch", "/v1/catalog/import/chunk",
            x -> "{\"doc_id\":\"empty-batch-doc\",\"collection_id\":" + x + ",\"rows\":[]}"),
        new Case("CatalogRepository.refuseRowsUnderSupersededCollection", "/v1/catalog/manifest/append_many",
            x -> "{\"collection_id\":" + x + ",\"docs\":[]}"),
        new Case("AspectRepository.upsertAspect", "/v1/aspects/upsert", x -> "{\"collection_id\":" + x + ",\"confidence\":0.0}"));

    CollectionRouteFixture fx;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    private String registered(String stem) {
        String l = label("emptybatch-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    @TestFactory
    Stream<DynamicNode> anEmptyBatchByIdAnswersItsUsual200ForALiveRow_andA404ForAnIdFreedAfterTheLookups() {
        List<DynamicNode> nodes = new ArrayList<>();
        for (Case c : CASES) {
            nodes.add(DynamicTest.dynamicTest(c.method(), () -> {
                String live = registered("live");
                Resp ok = fx.post(c.path(), c.body().apply(fx.idOf(live)));
                assertThat(ok.status()).as(c.method() + " by a live id, empty batch: " + ok.body()).isEqualTo(200);

                String a = registered("freed");
                long x = fx.idOf(a);
                AtomicInteger deleted = new AtomicInteger();
                AtomicInteger fired = new AtomicInteger();
                fx.service.collectionSelectorFilter().testHooks(() -> {
                    if (fired.getAndIncrement() == 0) {
                        CompletableFuture.runAsync(() -> deleted.set(fx.post(DELETE, "{\"name\":" + json(a) + "}").status())).join();
                    }
                }, null, null, null);
                Resp r;
                try {
                    r = fx.post(c.path(), c.body().apply(x));
                } finally {
                    fx.service.collectionSelectorFilter().testHooks(null, null, null, null);
                }
                assertThat(deleted.get()).as("setup: the hook freed the id").isEqualTo(200);
                assertThat(r.status()).as(c.method() + " by an id freed after the lookups, empty batch: " + r.body()).isEqualTo(404);
                assertThat(r.reason()).isEqualTo("unknown_collection_id");
            }));
        }
        nodes.add(DynamicTest.dynamicTest("non-vacuity: one case per method that used to claim nothingToWrite", () ->
            assertThat(CASES).as("the cases").hasSize(7)));
        return nodes.stream();
    }
}
