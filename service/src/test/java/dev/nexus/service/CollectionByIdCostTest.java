// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): what a request by {@code collection_id} costs against the same request by label. A request by
 * id reads the row of the id inside the write's transaction ({@code FOR KEY SHARE}) instead of answering from the label cache, and a
 * handler that resolves the collection once per record pays that read per record unless the transaction's memo answers the repeats
 * ({@code WriteRegistryMemo}); since linda's option (b) a request by label takes the same read, so both are counted, in alternated order. The write measured is the centroid upsert of a few hundred records, the route with the
 * per-record registry call; the report line (stderr) is the number to read, and the assertion bounds only a blow-up.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionByIdCostTest {

    private static final int RECORDS = 300;
    private static final int ROUNDS = 5;
    private static final String EMBEDDING_384 = "[" + String.join(",", Collections.nCopies(384, "0.1")) + "]";

    CollectionRouteFixture fx;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    private String body(String selectorField, String selectorValue, int round) {
        StringBuilder b = new StringBuilder("{\"records\":[");
        for (int i = 0; i < RECORDS; i++) {
            if (i > 0) b.append(',');
            b.append("{\"").append(selectorField).append("\":").append(selectorValue).append(",\"topic_id\":").append(round * RECORDS + i + 1)
                .append(",\"label\":\"cost-").append(round).append('-').append(i).append("\",\"doc_count\":1,\"embedding\":")
                .append(EMBEDDING_384).append('}');
        }
        return b.append("]}").toString();
    }

    private long millis(String path, String body) {
        long t0 = System.nanoTime();
        Resp r = fx.post(path, body);
        long ms = (System.nanoTime() - t0) / 1_000_000;
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        return ms;
    }

    private static long median(List<Long> v) {
        List<Long> sorted = new ArrayList<>(v);
        Collections.sort(sorted);
        return sorted.get(sorted.size() / 2);
    }

    /**
     * The registry reads a write transaction makes ({@code CollectionRegistry#setWriteReadHookForTests}): the number is the cost, and
     * timing is not (review S4: a single read is ~0.2 ms against ~900 ms of 300 x 384-dim payload, so a missing memo, 300 reads, hides
     * in the noise). One tenant|label key per read, counted by key, so a request that read two rows would show two keys.
     */
    private final java.util.concurrent.ConcurrentLinkedQueue<String> reads = new java.util.concurrent.ConcurrentLinkedQueue<>();

    /** Sends the upsert and returns how many registry reads its write transactions made. */
    private int readsOf(String path, String body) {
        reads.clear();
        millis(path, body);
        return reads.size();
    }

    @Test
    void aCentroidUpsertOfManyRecordsByIdCostsAboutWhatItCostsByLabel() {
        String byLabel = label("cost-label");
        String byId = label("cost-id");
        fx.register(byLabel, byId);
        long id = fx.idOf(byId);
        String path = "/v1/taxonomy/centroids/upsert";
        dev.nexus.service.db.CollectionRegistry.setWriteReadHookForTests(reads::add);
        try {
            millis(path, body("collection", json(byLabel), 90));   // warm both paths: connection pool, plan cache, class loading
            millis(path, body("collection_id", String.valueOf(id), 91));
            measure(path, byLabel, id);
        } finally {
            dev.nexus.service.db.CollectionRegistry.setWriteReadHookForTests(null);
        }
    }

    private void measure(String path, String byLabel, long id) {
        List<Long> labelMs = new ArrayList<>();
        List<Long> idMs = new ArrayList<>();
        List<Integer> labelReads = new ArrayList<>();
        List<Integer> idReads = new ArrayList<>();
        for (int round = 0; round < ROUNDS; round++) {
            // alternate the order: the request that goes first pays the cold costs of the round
            boolean idFirst = round % 2 == 1;
            for (int k = 0; k < 2; k++) {
                if ((k == 0) == idFirst) {
                    reads.clear();
                    idMs.add(millis(path, body("collection_id", String.valueOf(id), round)));
                    idReads.add(reads.size());
                } else {
                    reads.clear();
                    labelMs.add(millis(path, body("collection", json(byLabel), round)));
                    labelReads.add(reads.size());
                }
            }
        }
        // one transaction, one collection: ONE FOR KEY SHARE read of its registry row for the 300 records, by label and by id alike
        assertThat(labelReads).as("registry reads of a %d-record upsert by label, per request", RECORDS).containsOnly(1);
        assertThat(idReads).as("registry reads of a %d-record upsert by id, per request", RECORDS).containsOnly(1);
        long labelMedian = median(labelMs);
        long byIdMedian = median(idMs);
        System.err.printf("by_id_cost records=%d rounds=%d by_label_ms=%s by_id_ms=%s median_by_label=%d median_by_id=%d "
            + "registry_reads_by_label=%s registry_reads_by_id=%s%n",
            RECORDS, ROUNDS, labelMs, idMs, labelMedian, byIdMedian, labelReads, idReads);
        assertThat(byIdMedian).as("by id (%s ms) against by label (%s ms) for %d records", idMs, labelMs, RECORDS)
            .isLessThanOrEqualTo(labelMedian * 3 + 1000);
    }
}
