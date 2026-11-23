// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionRefusals;
import dev.nexus.service.db.CollectionRetiredIdException;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.UnknownCollectionIdException;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 row 16 (nexus-p2m1x.22): supersede, rename and delete by {@code collection_id} read and lock the SOURCE by the requested id
 * inside the verb's own transaction ({@link CollectionRefusals#requireSourceById}). The route boundary resolved the id to a label in an
 * earlier read; here the label is freed and registered again as a NEW row (a new id) in the gap, and a verb that went on by the label
 * would retire, rename or delete the new row's collection. Each verb answers 404 {@code unknown_collection_id} for the id it was
 * asked about and leaves the new holder of the label as it was.
 *
 * <p>The supersede and the rename use the route boundary's {@code afterResolve} seam: the hook runs after the id is resolved and
 * before the handler, so the order is fixed and no timing is involved. The delete cannot: its route asks a verdict (a read of the
 * label's rows by the id) before the purge, which catches a change made before the handler. Its change is in the purge transaction,
 * so its test calls {@code deleteCollectionById} with the stale id directly, which is the state the gap between the verdict and the
 * purge leaves the repository in.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionVerbsByIdGapTest {

    private static final String DELETE = "/v1/catalog/collections/delete";

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
        String l = label("verbgap-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    private void seedChunk(String collection, String hex) {
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(hex), List.of("verbgap text"),
                List.of(new float[384]), List.of(Map.<String, Object>of()));
            return null;
        });
    }

    private int chunksOf(String collection, String hex) {
        byte[] chash = java.util.HexFormat.of().parseHex(hex);
        return fx.asSuperuser(ctx -> (int) ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(collection).and(CHUNKS.CHASH.eq(chash))));
    }

    /** The hook runs on the request's own thread, where the boundary's named ids are set: the seeds belong to another thread. */
    private static void elsewhere(Runnable work) {
        CompletableFuture.runAsync(work).join();
    }

    /** What the hook does in the gap, once: deletes {@code a} (its id is free) and registers the label again, as a new row. */
    private Runnable freeTheIdAndTakeTheLabel(String a, AtomicInteger fired, AtomicInteger deleteStatus) {
        return () -> {
            if (fired.getAndIncrement() == 0) {   // the delete below passes through the boundary too: only the first call acts
                elsewhere(() -> {
                    deleteStatus.set(fx.post(DELETE, "{\"name\":" + json(a) + "}").status());
                    fx.register(a);
                });
            }
        };
    }

    private void noHooks() {
        fx.service.collectionSelectorFilter().testHooks(null, null, null, null);
    }

    private void assertRefusedAsUnknownId(Resp r, long id) {
        assertThat(r.status()).as(r.body()).isEqualTo(404);
        assertThat(r.reason()).isEqualTo("unknown_collection_id");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(id);
    }

    // ---- supersede ------------------------------------------------------------------------------------------------------------------

    @Test
    void aSupersedeByAnIdFreedBetweenTheBoundaryAndTheHandlerIsRefused_andTheNewHolderOfTheLabelIsNotRetired() {
        String a = registered("supersede-a");
        String b = registered("supersede-b");
        long x = fx.idOf(a);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger deleteStatus = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(freeTheIdAndTakeTheLabel(a, fired, deleteStatus), null, null, null);
        Resp r;
        try {
            r = fx.post("/v1/catalog/collections/supersede", "{\"collection_id\":" + x + ",\"superseded_by\":" + json(b) + "}");
        } finally {
            noHooks();
        }

        assertThat(deleteStatus.get()).as("setup: the hook freed the id").isEqualTo(200);
        Long z = fx.idOf(a);
        assertThat(z).as("setup: the label is now another row's").isNotNull().isNotEqualTo(x);
        assertRefusedAsUnknownId(r, x);
        assertThat(fx.supersededBy(a)).as("the new holder of the label was not retired").isEmpty();
        assertThat(fx.supersededBy(b)).as("and the target is as it was").isEmpty();
    }

    /** The id the verb reads is the source's, not the successor's, when the request names both ends by id. */
    @Test
    void aSupersedeNamingBothEndsByIdReadsTheSourceByItsOwnId() {
        String a = registered("supersede-both-a");
        String b = registered("supersede-both-b");
        long x = fx.idOf(a);
        long y = fx.idOf(b);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger deleteStatus = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(freeTheIdAndTakeTheLabel(a, fired, deleteStatus), null, null, null);
        Resp r;
        try {
            r = fx.post("/v1/catalog/collections/supersede", "{\"collection_id\":" + x + ",\"superseded_by_id\":" + y + "}");
        } finally {
            noHooks();
        }

        assertThat(deleteStatus.get()).as("setup: the hook freed the source's id").isEqualTo(200);
        assertRefusedAsUnknownId(r, x);
        assertThat(fx.supersededBy(a)).as("the new holder of the source's label was not retired").isEmpty();
    }

    /** With both ids live all the way through, a supersede and a rename by id succeed, as they did before the read. */
    @Test
    void aSupersedeAndARenameByAnIdThatStaysLiveStillSucceed() {
        String a = registered("live-a");
        String b = registered("live-b");
        String c = registered("live-c");
        String renamed = label("verbgap-live-renamed-" + (n++));
        assertThat(fx.post("/v1/catalog/collections/supersede",
            "{\"collection_id\":" + fx.idOf(a) + ",\"superseded_by_id\":" + fx.idOf(b) + "}").status()).isEqualTo(200);
        assertThat(fx.supersededBy(a)).isEqualTo(b);
        Resp r = fx.post("/v1/catalog/collections/rename", "{\"collection_id\":" + fx.idOf(c) + ",\"new_name\":" + json(renamed) + "}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(fx.supersededBy(c)).isEqualTo(renamed);
    }

    // ---- rename ---------------------------------------------------------------------------------------------------------------------

    /**
     * The by-id check runs at the ordered registry lock, after renameCollectionTxn's by-label pre-reads (model mismatch, a re-embed job,
     * the blocking tables). In this race one of those may refuse first with its own 409, before the 404 this test pins; either way nothing
     * is written to the new holder (linda, 2026-11-23: an honest answer, accepted). This test's new holder trips none of them.
     */
    @Test
    void aRenameByAnIdFreedBetweenTheBoundaryAndTheHandlerIsRefused_andTheNewHolderOfTheLabelIsNotRenamed() {
        String a = registered("rename-a");
        String target = label("verbgap-rename-target-" + (n++));
        long x = fx.idOf(a);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger deleteStatus = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(freeTheIdAndTakeTheLabel(a, fired, deleteStatus), null, null, null);
        Resp r;
        try {
            r = fx.post("/v1/catalog/collections/rename", "{\"collection_id\":" + x + ",\"new_name\":" + json(target) + "}");
        } finally {
            noHooks();
        }

        assertThat(deleteStatus.get()).as("setup: the hook freed the id").isEqualTo(200);
        Long z = fx.idOf(a);
        assertThat(z).as("setup: the label is now another row's").isNotNull().isNotEqualTo(x);
        assertRefusedAsUnknownId(r, x);
        assertThat(fx.supersededBy(a)).as("the new holder of the label was not renamed away").isEmpty();
        assertThat(fx.idOf(a)).as("and kept its id").isEqualTo(z);
        assertThat(fx.exists(target)).as("nothing was registered under the new name").isFalse();
    }

    // ---- delete ---------------------------------------------------------------------------------------------------------------------

    /**
     * The purge transaction of a delete by id reads the id itself: an id whose row is gone is refused 404 and the new holder of its
     * label keeps its row and its content. (Before the read, the purge went by the label before gate 3 and removed the holder's content.)
     */
    @Test
    void aDeleteByAnIdFreedBeforeThePurgeIsRefused_andTheNewHolderOfTheLabelKeepsItsContent() {
        String a = registered("delete-a");
        long x = fx.idOf(a);
        assertThat(fx.post(DELETE, "{\"name\":" + json(a) + "}").status()).as("setup: the id is freed").isEqualTo(200);
        fx.register(a);
        String hex = Chash.ofText(a + "/new-holder-chunk").toHex();
        seedChunk(a, hex);
        long z = fx.idOf(a);
        assertThat(z).as("setup: the label is another row's").isNotEqualTo(x);
        CatalogRepository repo = new CatalogRepository(new TenantScope(fx.svcDs));

        assertThatThrownBy(() -> repo.deleteCollectionById(fx.tenant, a, x, false))
            .as("a delete by an id with no row")
            .isInstanceOf(UnknownCollectionIdException.class)
            .extracting(e -> ((UnknownCollectionIdException) e).collectionId()).isEqualTo(x);

        assertThat(fx.idOf(a)).as("the new holder's row is still there under its own id").isEqualTo(z);
        assertThat(chunksOf(a, hex)).as("and so is its content").isEqualTo(1);
    }

    @Test
    void aDeleteByAnIdRetiredBeforeThePurgeIsRefusedNamingTheSuccessor_andNothingIsPurged() {
        String a = registered("delete-retired-a");
        String b = registered("delete-retired-b");
        String hex = Chash.ofText(a + "/chunk").toHex();
        seedChunk(a, hex);
        long x = fx.idOf(a);
        assertThat(fx.post("/v1/catalog/collections/supersede",
            "{\"name\":" + json(a) + ",\"superseded_by\":" + json(b) + "}").status()).as("setup: the row is retired").isEqualTo(200);
        CatalogRepository repo = new CatalogRepository(new TenantScope(fx.svcDs));

        assertThatThrownBy(() -> repo.deleteCollectionById(fx.tenant, a, x, false))
            .isInstanceOf(CollectionRetiredIdException.class)
            .extracting(e -> ((CollectionRetiredIdException) e).successor().orElseThrow().label()).isEqualTo(b);

        assertThat(fx.exists(a)).as("the tombstone is still there").isTrue();
        assertThat(chunksOf(a, hex)).as("with its content").isEqualTo(1);
    }

    // ---- the lock itself ------------------------------------------------------------------------------------------------------------

    /** A source the transaction already holds (the ordered lock took it) is read again without waiting, and a label that moved is refused. */
    @Test
    void requireSourceByIdIsSafeOnARowTheTransactionAlreadyHolds_andRefusesALabelThatMoved() {
        String a = registered("require-a");
        long x = fx.idOf(a);
        TenantScope scope = new TenantScope(fx.svcDs);

        long again = scope.withTenant(fx.tenant, ctx -> {
            CollectionRefusals.requireSourceById(ctx, fx.tenant, x, a);
            return CollectionRefusals.requireSourceById(ctx, fx.tenant, x, a).collectionId();   // the second FOR UPDATE of a held row
        });
        assertThat(again).isEqualTo(x);

        assertThatThrownBy(() -> scope.withTenant(fx.tenant, ctx -> CollectionRefusals.requireSourceById(ctx, fx.tenant, x, "some-other-label")))
            .isInstanceOfSatisfying(dev.nexus.service.db.CollectionIdMismatchException.class, e -> {
                assertThat(e.labelDisagreement()).isTrue();
                assertThat(e.requestedLabel()).isEqualTo("some-other-label");
                assertThat(e.currentLabel()).isEqualTo(a);
            });
    }
}
