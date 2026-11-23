// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.GcCollectionReads;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.function.Supplier;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23, review of afb50430c1, item 6): a gc route that meets the registry row of its collection HELD by a retiring verb
 * (a hand session with {@code FOR UPDATE}, which is all a verb's first step is). {@code GcCollectionReads.enter} reads the row under the writers'
 * rule (no lock timeout of its own), so the wait ends at the route's statement bound, and each route answers a typed, retryable 503 rather than a
 * 500 or a hang. Pinned per route:
 * <ul>
 * <li>quarantine-orphans, expire-quarantine and restore-rereferenced (the forms that set no statement bound of their own get the read's bound,
 *     shortened here by the test seam): 503 {@code rename_busy} with {@code Retry-After};</li>
 * <li>quarantine-restore (its own 25 s statement bound, so this case takes about that long): 503 {@code quarantine_restore_busy}.</li>
 * </ul>
 * Each case then asserts the same request succeeds once the verb lets go.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GcRegistryRowHeldByVerbTest {

    private static final String STAMP = "2026-01-01T00:00:00Z";

    CollectionRouteFixture fx;
    int n;

    @BeforeAll
    void start() throws Exception {
        fx = new CollectionRouteFixture();
    }

    @AfterAll
    void stop() {
        if (fx != null) fx.close();
    }

    @BeforeEach
    void shortBound() {
        GcCollectionReads.setRegistryReadBoundForTests(600);
    }

    @AfterEach
    void defaultBound() {
        GcCollectionReads.setRegistryReadBoundForTests(25_000);
    }

    private String registered(String stem) {
        String l = label("held-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    private void seedChunk(String collection, String hex) {
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(hex), List.of("held text"),
                List.of(new float[384]), List.of(Map.<String, Object>of()));
            return null;
        });
    }

    /** Runs {@code request} while a hand session holds {@code collection}'s registry row FOR UPDATE; returns the answer. */
    private Resp whileAVerbHoldsTheRow(String collection, Supplier<Resp> request) throws Exception {
        try (Connection verb = fx.pg.createConnection("")) {
            verb.setAutoCommit(false);
            DSL.using(verb, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(collection)).forUpdate().fetch();
            try {
                return request.get();
            } finally {
                verb.rollback();
            }
        }
    }

    private void assertBusy(Resp r, String reason) {
        assertThat(r.status()).as("a typed, retryable busy answer (not a 500, not a hang): %s", r.body()).isEqualTo(503);
        assertThat(r.reason()).isEqualTo(reason);
    }

    @Timeout(120)
    @Test
    void quarantineOrphans_meetingARowAVerbHolds_answers503RenameBusy_thenSucceedsWhenItLetsGo() throws Exception {
        String a = registered("orphans");
        long x = fx.idOf(a);
        String body = "{\"collection_id\":" + x + ",\"quarantine_collection\":" + json("quarantine-" + a) + ",\"quarantined_at\":\"" + STAMP + "\"}";
        assertBusy(whileAVerbHoldsTheRow(a, () -> fx.post("/v1/vectors/gc/quarantine-orphans", body)), "rename_busy");
        assertThat(fx.post("/v1/vectors/gc/quarantine-orphans", body).status()).as("retried after the verb let go").isEqualTo(200);
    }

    @Timeout(120)
    @Test
    void expireQuarantine_meetingARowAVerbHolds_answers503RenameBusy_thenSucceedsWhenItLetsGo() throws Exception {
        String a = registered("expire");
        long x = fx.idOf(a);
        String body = "{\"origin_collection_id\":" + x + ",\"quarantine_collection\":" + json("quarantine-" + a) + ",\"cutoff\":\"" + STAMP + "\"}";
        assertBusy(whileAVerbHoldsTheRow(a, () -> fx.post("/v1/vectors/gc/expire-quarantine", body)), "rename_busy");
        assertThat(fx.post("/v1/vectors/gc/expire-quarantine", body).status()).as("retried after the verb let go").isEqualTo(200);
    }

    @Timeout(120)
    @Test
    void restoreRereferenced_meetingARowAVerbHolds_answers503RenameBusy_thenSucceedsWhenItLetsGo() throws Exception {
        String a = registered("rereferenced");
        long x = fx.idOf(a);
        String body = "{\"origin_collection_id\":" + x + ",\"quarantine_collection\":" + json("quarantine-" + a) + "}";
        assertBusy(whileAVerbHoldsTheRow(a, () -> fx.post("/v1/vectors/gc/restore-rereferenced", body)), "rename_busy");
        assertThat(fx.post("/v1/vectors/gc/restore-rereferenced", body).status()).as("retried after the verb let go").isEqualTo(200);
    }

    @Timeout(180)
    @Test
    void quarantineRestore_meetingARowAVerbHolds_answersItsOwnStatementBound503_quarantineRestoreBusy() throws Exception {
        String a = registered("restore");
        long x = fx.idOf(a);
        String hex = Chash.ofText("held-restore-" + a).toHex();
        seedChunk(a, hex);
        Resp moved = fx.post("/v1/vectors/gc/quarantine-orphans", "{\"collection_id\":" + x + ",\"quarantine_collection\":"
            + json("quarantine-" + a) + ",\"quarantined_at\":\"" + STAMP + "\"}");
        assertThat(moved.status()).as("setup: the orphan moved to its quarantine sibling: %s", moved.body()).isEqualTo(200);
        String body = "{\"origin_collection_id\":" + x + ",\"chashes\":[\"" + hex + "\"]}";
        assertBusy(whileAVerbHoldsTheRow(a, () -> fx.post("/v1/vectors/gc/quarantine-restore", body)), "quarantine_restore_busy");
        assertThat(fx.post("/v1/vectors/gc/quarantine-restore", body).status()).as("retried after the verb let go").isEqualTo(200);
    }
}
