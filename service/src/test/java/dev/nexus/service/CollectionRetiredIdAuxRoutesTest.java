// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.GcCollectionReads;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), the routes {@code CollectionSelectors.WRITES} had missed (review of 55a26acc04, F4;
 * {@code CollectionRouteClassificationTest} now holds every POST and DELETE route to a classification): {@code set_aspects_enabled} and the
 * four {@code /v1/vectors/gc} writes. The pattern of {@code CollectionRetiredIdRoutesTest}: a request by {@code collection_id}; every
 * lookup outside a transaction has passed (the id is live); in the window before the write's own work the id is freed and its label taken
 * again as a new row Z, from another thread ({@link #elsewhere}); the request answers 404 {@code unknown_collection_id} and nothing of Z
 * changes. Each test states what a write that went on by label would have changed in Z, so removing the route's in-transaction read turns
 * the test red.
 *
 * <p>The window for set_aspects_enabled is the filter's {@code beforeTransactionRead} hook. The gc writes read the id AFTER their SQL
 * function (which takes the sweep gate first, and the lock order is gate then registry row; see {@code GcCollectionReads}), so a hook at the
 * read would wait on the gate the function holds: their window is {@link GcCollectionReads#setBeforeMoveForTests}, which runs inside the
 * write's transaction, before the function.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRetiredIdAuxRoutesTest {

    private static final String DELETE = "/v1/catalog/collections/delete";
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

    @AfterEach
    void noHooks() {
        fx.service.collectionSelectorFilter().testHooks(null, null, null, null);
        GcCollectionReads.setBeforeMoveForTests(null);
    }

    private String registered(String stem) {
        String l = label("aux-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    private void seedChunk(String collection, String hex) {
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(hex), List.of("aux text"),
                List.of(new float[384]), List.of(Map.<String, Object>of()));
            return null;
        });
        // the gc functions select orphans with reapable(c), which honours the 30 day grace: this chunk stands for one orphaned long ago
        ReapableFixtures.agePastGrace(fx.pg, fx.tenant, collection);
    }

    private int chunksOf(String collection, String hex) {
        byte[] chash = java.util.HexFormat.of().parseHex(hex);
        return fx.asSuperuser(ctx -> (int) ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(collection).and(CHUNKS.CHASH.eq(chash))));
    }

    /** A hook runs on the request's own thread, where the boundary's named ids are set: the seeds belong to another thread. */
    private static void elsewhere(Runnable work) {
        CompletableFuture.runAsync(work).join();
    }

    /** What the hook does in the gap: frees the id by deleting {@code a}, takes the label again as a new row Z, and files {@code hex} in Z. */
    private Runnable freeTheIdAndTakeTheLabel(String a, String hex, AtomicInteger deleteStatus) {
        return freeTheIdAndTakeTheLabel(a, hex, deleteStatus, false);
    }

    /** As above; {@code keepQuarantine} deletes {@code a} with {@code keep_quarantine}, which leaves its rows in its quarantine sibling. */
    private Runnable freeTheIdAndTakeTheLabel(String a, String hex, AtomicInteger deleteStatus, boolean keepQuarantine) {
        return () -> elsewhere(() -> {
            deleteStatus.set(fx.post(DELETE, "{\"name\":" + json(a) + (keepQuarantine ? ",\"keep_quarantine\":true" : "") + "}").status());
            fx.register(a);
            if (hex != null) {
                seedChunk(a, hex);
            }
        });
    }

    private void assertUnknownId(Resp r, long x) {
        assertThat(r.status()).as(r.body()).isEqualTo(404);
        assertThat(r.reason()).isEqualTo("unknown_collection_id");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(x);
    }

    // ---- set_aspects_enabled ---------------------------------------------------------------------------------------------------------

    private Boolean aspectsEnabled(String collection) {
        return fx.asSuperuser(ctx -> ctx.select(CATALOG_COLLECTIONS.ASPECTS_ENABLED).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(collection))
            .fetchOne(CATALOG_COLLECTIONS.ASPECTS_ENABLED));
    }

    @Test
    void setAspectsEnabled_byAnIdFreedAfterTheLookupsFlipsNothingOfTheNewHolder() {
        String control = registered("aspects-control");
        Resp ok = fx.post("/v1/catalog/collections/set_aspects_enabled",
            "{\"collection_id\":" + fx.idOf(control) + ",\"aspects_enabled\":true}");
        assertThat(ok.status()).as("control: %s", ok.body()).isEqualTo(200);
        assertThat(aspectsEnabled(control)).as("control: the flag landed on the row of the id").isTrue();

        String a = registered("aspects-gap");
        long x = fx.idOf(a);
        AtomicInteger deleteStatus = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(null, freeTheIdAndTakeTheLabel(a, null, deleteStatus), null, null);
        Resp r = fx.post("/v1/catalog/collections/set_aspects_enabled", "{\"collection_id\":" + x + ",\"aspects_enabled\":true}");

        assertThat(deleteStatus.get()).as("setup: the hook freed the id").isEqualTo(200);
        assertThat(fx.idOf(a)).as("setup: the label is now another row's").isNotEqualTo(x);
        assertUnknownId(r, x);
        assertThat(aspectsEnabled(a)).as("the new holder of the label was not flipped").isNull();
    }

    // ---- the gc writes ---------------------------------------------------------------------------------------------------------------

    /** Runs {@code request} with the label of {@code a} taken again, inside the write's transaction and before its SQL function. */
    private Resp withTheLabelTakenBeforeTheMove(String a, String hex, Supplier<Resp> request) {
        return withTheLabelTakenBeforeTheMove(a, hex, false, request);
    }

    private Resp withTheLabelTakenBeforeTheMove(String a, String hex, boolean keepQuarantine, Supplier<Resp> request) {
        AtomicInteger deleteStatus = new AtomicInteger();
        GcCollectionReads.setBeforeMoveForTests(freeTheIdAndTakeTheLabel(a, hex, deleteStatus, keepQuarantine));
        Resp r = request.get();
        assertThat(deleteStatus.get()).as("setup: the hook ran, freeing the id").isEqualTo(200);
        return r;
    }

    @Test
    void gcQuarantineOrphans_byAnIdFreedAfterTheLookupsMovesNothingOfTheNewHolder() {
        String control = registered("orphans-control");
        seedChunk(control, Chash.ofText("aux-orphans-control-" + control).toHex());
        Resp ok = fx.post("/v1/vectors/gc/quarantine-orphans", "{\"collection_id\":" + fx.idOf(control)
            + ",\"quarantine_collection\":" + json("quarantine-" + control) + ",\"quarantined_at\":\"" + STAMP + "\"}");
        assertThat(ok.status()).as("control: %s", ok.body()).isEqualTo(200);
        assertThat(ok.json().path("moved").asLong(-1)).as("control: the aged orphan was moved: %s", ok.body()).isEqualTo(1);

        String a = registered("orphans-gap");
        long x = fx.idOf(a);
        String hex = Chash.ofText("aux-orphans-gap-" + a).toHex();
        Resp r = withTheLabelTakenBeforeTheMove(a, hex, () -> fx.post("/v1/vectors/gc/quarantine-orphans",
            "{\"collection_id\":" + x + ",\"quarantine_collection\":" + json("quarantine-" + a)
                + ",\"quarantined_at\":\"" + STAMP + "\"}"));

        assertUnknownId(r, x);
        assertThat(chunksOf(a, hex)).as("the new holder's chunk, which has no manifest row, was not moved out of it").isEqualTo(1);
        assertThat(chunksOf("quarantine-" + a, hex)).as("and nothing reached the quarantine sibling").isZero();
    }

    @Test
    void gcRestoreRereferenced_byAnIdFreedAfterTheLookupsAnswers404() {
        String control = registered("restore-control");
        Resp ok = fx.post("/v1/vectors/gc/restore-rereferenced", "{\"origin_collection_id\":" + fx.idOf(control)
            + ",\"quarantine_collection\":" + json("quarantine-" + control) + "}");
        assertThat(ok.status()).as("control: %s", ok.body()).isEqualTo(200);

        String a = registered("restore-gap");
        long x = fx.idOf(a);
        Resp r = withTheLabelTakenBeforeTheMove(a, null, () -> fx.post("/v1/vectors/gc/restore-rereferenced",
            "{\"origin_collection_id\":" + x + ",\"quarantine_collection\":" + json("quarantine-" + a) + "}"));

        assertUnknownId(r, x);
    }

    @Test
    void gcExpireQuarantine_byAnIdFreedAfterTheLookupsAnswers404() {
        String control = registered("expire-control");
        Resp ok = fx.post("/v1/vectors/gc/expire-quarantine", "{\"origin_collection_id\":" + fx.idOf(control)
            + ",\"quarantine_collection\":" + json("quarantine-" + control) + ",\"cutoff\":\"" + STAMP + "\"}");
        assertThat(ok.status()).as("control: %s", ok.body()).isEqualTo(200);

        String a = registered("expire-gap");
        long x = fx.idOf(a);
        Resp r = withTheLabelTakenBeforeTheMove(a, null, () -> fx.post("/v1/vectors/gc/expire-quarantine",
            "{\"origin_collection_id\":" + x + ",\"quarantine_collection\":" + json("quarantine-" + a)
                + ",\"cutoff\":\"" + STAMP + "\"}"));

        assertUnknownId(r, x);
    }

    /** A collection {@code a} (by label {@code a}, id {@code x}) whose one aged orphan {@code hex} the engine moved into {@code quarantine-a}; each step asserted. */
    private record Quarantined(String a, long x, String hex) {
    }

    private Quarantined aQuarantinedChunk(String stem) {
        String a = registered(stem);
        long x = fx.idOf(a);
        String hex = Chash.ofText("aux-" + stem + "-" + a).toHex();
        seedChunk(a, hex);
        assertThat(chunksOf(a, hex)).as("setup: the chunk is in the origin").isEqualTo(1);
        Resp moved = fx.post("/v1/vectors/gc/quarantine-orphans", "{\"collection_id\":" + x
            + ",\"quarantine_collection\":" + json("quarantine-" + a) + ",\"quarantined_at\":\"" + STAMP + "\"}");
        assertThat(moved.status()).as("setup: the orphans route answered: %s", moved.body()).isEqualTo(200);
        assertThat(moved.json().path("moved").asLong(-1)).as("setup: the route moved the aged orphan: %s", moved.body()).isEqualTo(1);
        assertThat(chunksOf("quarantine-" + a, hex)).as("setup: the chunk is in the quarantine sibling").isEqualTo(1);
        assertThat(chunksOf(a, hex)).as("setup: and no longer in the origin").isZero();
        return new Quarantined(a, x, hex);
    }

    private Resp restoreByTheOldId(Quarantined q) {
        return fx.post("/v1/vectors/gc/quarantine-restore", "{\"origin_collection_id\":" + q.x() + ",\"chashes\":[\"" + q.hex() + "\"]}");
    }

    /**
     * The restore needs a quarantine sibling that holds the chunk, or it runs no transaction at all: the engine makes one by moving an
     * orphan with the orphans route. The window frees the origin's id (its delete keeps the quarantine rows) and takes its label again
     * as Z; the restore's function runs against Z and would put the chunk into it, and the read after it refuses and rolls the restore
     * back, so the chunk stays in its quarantine.
     */
    @Test
    void gcQuarantineRestore_byAnIdFreedAfterTheLookupsRestoresNothingIntoTheNewHolder() {
        Quarantined q = aQuarantinedChunk("qrestore-gap");

        Resp r = withTheLabelTakenBeforeTheMove(q.a(), null, true, () -> restoreByTheOldId(q));

        assertUnknownId(r, q.x());
        assertThat(chunksOf(q.a(), q.hex())).as("nothing was restored into the new holder of the label").isZero();
        assertThat(chunksOf("quarantine-" + q.a(), q.hex())).as("the chunk is still in its quarantine").isEqualTo(1);
    }

    /**
     * The same window, with the id's delete taking the origin's quarantine rows and unregistering the sibling (the delete's default):
     * the restore's function itself refuses, 22023, because the sibling is not a registered quarantine collection. The answer for an
     * id that names no row is still the typed 404, and the new holder of the label has nothing restored into it.
     */
    @Test
    void gcQuarantineRestore_byAnIdWhoseDeleteTookTheSiblingAnswers404NotThe22023() {
        Quarantined q = aQuarantinedChunk("qrestore-gone");

        Resp r = withTheLabelTakenBeforeTheMove(q.a(), null, false, () -> restoreByTheOldId(q));

        assertUnknownId(r, q.x());
        assertThat(chunksOf(q.a(), q.hex())).as("nothing was restored into the new holder of the label").isZero();
    }

    /** With no sibling the restore opens no transaction and writes nothing: the id is accounted for, not reported as unread. */
    @Test
    void gcQuarantineRestore_withNoSiblingIsNotReportedAsAnUnreadId() {
        String a = registered("qrestore-none");
        List<String> unconsumed = new ArrayList<>();
        fx.service.collectionSelectorFilter().testHooks(null, null, null, unconsumed::add);

        Resp r = fx.post("/v1/vectors/gc/quarantine-restore", "{\"origin_collection_id\":" + fx.idOf(a) + ",\"chashes\":[\""
            + Chash.ofText("aux-qrestore-none-" + a).toHex() + "\"]}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(unconsumed).as("the route answered 2xx and the filter did not find the id unread").isEmpty();
    }
}
