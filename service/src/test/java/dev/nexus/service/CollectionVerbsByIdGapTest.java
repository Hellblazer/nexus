// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.RenameGuard;
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
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
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
 * <p>A supersede that names its SUCCESSOR by id ({@code superseded_by_id}) reads and locks the successor by that id in the same
 * ordered statement as the source (B3): the successor's id freed and its label taken again, retired, or bearing another label is
 * refused, and no tombstone points at the label's later holder. A successor named by label is locked and read by that label, and a
 * test pins it. Supersede and rename also answer 409 for a source retired in the gap (the {@code beforeTransactionRead} seam, which
 * the verb runs just before its first by-id read, the ordered lock).
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

    /** What the hook does in the gap, once: retires {@code victim}'s row by superseding it to {@code successor} (both by label). */
    private Runnable retireInTheGap(String victim, String successor, AtomicInteger fired, AtomicInteger status) {
        return () -> {
            if (fired.getAndIncrement() == 0) {   // the supersede below passes through the boundary too: only the first call acts
                elsewhere(() -> status.set(fx.post("/v1/catalog/collections/supersede",
                    "{\"name\":" + json(victim) + ",\"superseded_by\":" + json(successor) + "}").status()));
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

    // ---- supersede: the SUCCESSOR named by id (B3) ----------------------------------------------------------------------------------

    /**
     * A supersede that names its successor by {@code superseded_by_id} reads and locks the successor by that id in the verb's own
     * transaction. The successor's id is freed and its label registered again between the route boundary and the verb: the handler's
     * pre-reads (by label) find the new holder and go on, and only the by-id read of the successor refuses. No tombstone points at the
     * new holder.
     */
    @Test
    void aSupersedeByASuccessorIdFreedBetweenTheBoundaryAndTheHandlerIsRefused_andNoTombstonePointsAtTheNewHolder() {
        String a = registered("succ-gone-a");
        String b = registered("succ-gone-b");
        long x = fx.idOf(a);
        long y = fx.idOf(b);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger deleteStatus = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(freeTheIdAndTakeTheLabel(b, fired, deleteStatus), null, null, null);
        Resp r;
        try {
            r = fx.post("/v1/catalog/collections/supersede", "{\"collection_id\":" + x + ",\"superseded_by_id\":" + y + "}");
        } finally {
            noHooks();
        }

        assertThat(deleteStatus.get()).as("setup: the hook freed the successor's id").isEqualTo(200);
        Long z = fx.idOf(b);
        assertThat(z).as("setup: the successor's label is now another row's").isNotNull().isNotEqualTo(y);
        assertRefusedAsUnknownId(r, y);
        assertThat(fx.supersededBy(a)).as("the source was not retired, so no tombstone points at the new holder").isEmpty();
        assertThat(fx.idOf(a)).as("and it is the row it was").isEqualTo(x);
        assertThat(fx.supersededBy(b)).as("the new holder of the successor's label is as it was").isEmpty();
    }

    /** The same, with the source named by label: only the successor's slot carries an id. */
    @Test
    void aSupersedeByLabelOfASourceWhoseSuccessorIdIsFreedInTheGapIsRefusedToo() {
        String a = registered("succ-gone-label-a");
        String b = registered("succ-gone-label-b");
        long y = fx.idOf(b);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger deleteStatus = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(freeTheIdAndTakeTheLabel(b, fired, deleteStatus), null, null, null);
        Resp r;
        try {
            r = fx.post("/v1/catalog/collections/supersede", "{\"name\":" + json(a) + ",\"superseded_by_id\":" + y + "}");
        } finally {
            noHooks();
        }

        assertThat(deleteStatus.get()).as("setup: the hook freed the successor's id").isEqualTo(200);
        assertRefusedAsUnknownId(r, y);
        assertThat(fx.supersededBy(a)).as("no tombstone points at the new holder").isEmpty();
    }

    /** A successor retired between the handler's pre-reads and the verb's lock answers 409 collection_superseded, naming ITS successor. */
    @Test
    void aSupersedeByASuccessorIdRetiredInTheGapIsRefused409_andNothingIsWritten() {
        String a = registered("succ-retired-a");
        String b = registered("succ-retired-b");
        String c = registered("succ-retired-c");
        long y = fx.idOf(b);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger retireStatus = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(null, retireInTheGap(b, c, fired, retireStatus), null, null);
        Resp r;
        try {
            r = fx.post("/v1/catalog/collections/supersede", "{\"name\":" + json(a) + ",\"superseded_by_id\":" + y + "}");
        } finally {
            noHooks();
        }

        assertThat(retireStatus.get()).as("setup: the hook retired the successor").isEqualTo(200);
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(y);
        assertThat(r.json().path("superseded_by").asText()).isEqualTo(c);
        assertThat(fx.supersededBy(a)).as("the source was not retired").isEmpty();
    }

    /**
     * A successor named by LABEL is locked and read by that label, in the verb's transaction: a label freed and taken again in the gap
     * is followed to its current holder, which is what a request by label means. Pins the by-label form against a change that gives the
     * successor an id it was never sent.
     */
    @Test
    void aSupersedeByASuccessorLabelFollowsTheLabelToItsCurrentHolder() {
        String a = registered("succ-label-a");
        String b = registered("succ-label-b");
        long x = fx.idOf(a);
        long y = fx.idOf(b);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger deleteStatus = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(freeTheIdAndTakeTheLabel(b, fired, deleteStatus), null, null, null);
        Resp r;
        try {
            r = fx.post("/v1/catalog/collections/supersede", "{\"collection_id\":" + x + ",\"superseded_by\":" + json(b) + "}");
        } finally {
            noHooks();
        }

        assertThat(deleteStatus.get()).as("setup: the hook freed the label's first row").isEqualTo(200);
        long z = fx.idOf(b);
        assertThat(z).as("setup: the label is now another row's").isNotEqualTo(y);
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(fx.supersededBy(a)).as("the tombstone names the label").isEqualTo(b);
        Long pointsAt = fx.asSuperuser(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(x))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID));
        assertThat(pointsAt).as("and the id of the label's current holder, locked by that label").isEqualTo(z);
    }

    /**
     * A successor id whose row bears another label than the one the verb was given is refused 409 collection_id_mismatch (the id and the
     * label disagree), and nothing is written: no tombstone carries a label its id's row does not bear. Direct, because no request can
     * make them disagree while the one-row rename is fail-closed.
     */
    @Test
    void aSupersedeWhoseSuccessorIdBearsAnotherLabelIsRefusedAsAMismatch_andNothingIsWritten() {
        String a = registered("succ-mismatch-a");
        String b = registered("succ-mismatch-b");
        long y = fx.idOf(b);
        CatalogRepository repo = new CatalogRepository(new TenantScope(fx.svcDs));

        assertThatThrownBy(() -> repo.supersedeCollection(fx.tenant, a, null, "some-other-label", y, ""))
            .isInstanceOfSatisfying(dev.nexus.service.db.CollectionIdMismatchException.class, e -> {
                assertThat(e.labelDisagreement()).isTrue();
                assertThat(e.requestedLabel()).isEqualTo("some-other-label");
                assertThat(e.currentLabel()).isEqualTo(b);
            });

        assertThat(fx.supersededBy(a)).as("the source was not retired").isEmpty();
    }

    // ---- the idempotent retry, decided under the lock by id; the label read cannot decide it (row 16 ship-blockers 1 and 2) ----------

    private Long supersededById(String label) {
        return fx.asSuperuser(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID));
    }

    private java.time.OffsetDateTime supersededAt(String label) {
        return fx.asSuperuser(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_AT).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_AT));
    }

    /** A request by id that arrives after another identical supersede already retired the source to the same target: today's 200, nothing written. */
    @Test
    void aByIdRetryToTheSameTargetAnswers200UpdatedOne_andWritesAndLeavesNothingUnconsumed() {
        for (boolean successorById : new boolean[] {true, false}) {
            String a = registered("idem-a-" + successorById);
            String b = registered("idem-b-" + successorById);
            long x = fx.idOf(a);
            long y = fx.idOf(b);
            AtomicInteger fired = new AtomicInteger();
            AtomicInteger retireStatus = new AtomicInteger();
            java.util.List<String> unconsumed = java.util.Collections.synchronizedList(new java.util.ArrayList<>());
            fx.service.collectionSelectorFilter().testHooks(retireInTheGap(a, b, fired, retireStatus), null, null, unconsumed::add);
            Resp r;
            try {
                r = fx.post("/v1/catalog/collections/supersede", "{\"collection_id\":" + x
                    + (successorById ? ",\"superseded_by_id\":" + y : ",\"superseded_by\":" + json(b)) + "}");
            } finally {
                noHooks();
            }

            String why = "successor by id: " + successorById;
            assertThat(retireStatus.get()).as("setup: the first supersede retired the source. " + why).isEqualTo(200);
            assertThat(r.status()).as(why + ": " + r.body()).isEqualTo(200);
            assertThat(r.json().path("updated").asInt(-1)).as(why).isEqualTo(1);
            assertThat(fx.supersededBy(a)).as(why).isEqualTo(b);
            assertThat(supersededById(a)).as(why).isEqualTo(y);
            assertThat(supersededAt(a)).as("a retry re-stamps nothing. " + why).isNotNull();
            assertThat(unconsumed).as("every named id was read in the verb's own transaction. " + why).isEmpty();
        }
    }

    /**
     * A source retired to ANOTHER successor, which the request does not name (the retire lands after the handler's reads): 409, and the
     * tombstone is not re-pointed. Red against an idempotence check that ignores which successor the source holds.
     */
    @Test
    void aByIdSupersedeOfASourceAlreadyRetiredToADifferentSuccessorIdAnswers409_andRepointsNothing() {
        String a = registered("other-a");
        String xs = registered("other-x");
        String ys = registered("other-y");
        long x = fx.idOf(a);
        long xsId = fx.idOf(xs);
        long ysId = fx.idOf(ys);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger retireStatus = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(null, retireInTheGap(a, xs, fired, retireStatus), null, null);
        Resp r;
        try {
            r = fx.post("/v1/catalog/collections/supersede", "{\"collection_id\":" + x + ",\"superseded_by_id\":" + ysId + "}");
        } finally {
            noHooks();
        }

        assertThat(retireStatus.get()).as("setup: the source was retired to the first successor").isEqualTo(200);
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        assertThat(fx.supersededBy(a)).as("superseded_by is the first successor's").isEqualTo(xs);
        assertThat(supersededById(a)).as("and so is superseded_by_id").isEqualTo(xsId);
    }

    /**
     * The successor's label was taken again: the request's successor id bears the label, but the source (retired, by the label's text)
     * points at the id of the label's OLD holder. The label read of the handler cannot tell them apart; the by-id compare in the CAS can.
     * The state is built directly: a pre-P4.1 registry cannot hold two rows of a label.
     */
    @Test
    void aSupersedeByASuccessorIdWhoseLabelTheSourceNamesForAnotherRowIsRefused_andSupersededByIdIsUnchanged() {
        String a = registered("retaken-succ-a");
        String old = registered("retaken-succ-old");
        String b = registered("retaken-succ-b");
        long oldId = fx.idOf(old);
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, b)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, oldId)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(a)).execute());
        Long before = supersededById(a);
        assertThat(before).as("setup: the source names the label of b but the id of another row").isEqualTo(oldId);

        Resp r = fx.post("/v1/catalog/collections/supersede", "{\"name\":" + json(a) + ",\"superseded_by_id\":" + fx.idOf(b) + "}");

        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(fx.supersededBy(a)).isEqualTo(b);
        assertThat(supersededById(a)).as("not re-pointed at the new holder of the label").isEqualTo(oldId);
    }

    /**
     * A source named by id whose label was freed and taken again by a row already superseded to the requested label: the handler's label
     * read finds that row and its same-target shortcut would answer 200 for the wrong collection. The verb decides by id: 404.
     */
    @Test
    void aSupersedeByAnIdWhoseLabelNowBelongsToARowAlreadySupersededToTheTargetIsRefused_notAnsweredWithAFalse200() {
        String a = registered("retaken-src-a");
        String b = registered("retaken-src-b");
        long x = fx.idOf(a);
        long y = fx.idOf(b);
        AtomicInteger fired = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(() -> {
            if (fired.getAndIncrement() == 0) {
                elsewhere(() -> {
                    assertThat(fx.post(DELETE, "{\"name\":" + json(a) + "}").status()).isEqualTo(200);
                    fx.register(a);
                    assertThat(fx.post("/v1/catalog/collections/supersede",
                        "{\"name\":" + json(a) + ",\"superseded_by\":" + json(b) + "}").status()).isEqualTo(200);
                });
            }
        }, null, null, null);
        Resp r;
        try {
            r = fx.post("/v1/catalog/collections/supersede", "{\"collection_id\":" + x + ",\"superseded_by_id\":" + y + "}");
        } finally {
            noHooks();
        }

        assertThat(fx.idOf(a)).as("setup: the label is another row's").isNotEqualTo(x);
        assertThat(fx.supersededBy(a)).as("setup: that row is superseded to the target").isEqualTo(b);
        assertRefusedAsUnknownId(r, x);
        assertThat(supersededById(a)).as("the holder's tombstone is untouched").isEqualTo(y);
    }

    /** Pin: by label, the same-target retry keeps the handler's shortcut: 200 {"updated":1} and nothing rewritten. */
    @Test
    void aByLabelRetryToTheSameTargetStillAnswers200UpdatedOne() {
        String a = registered("label-retry-a");
        String b = registered("label-retry-b");
        String body = "{\"name\":" + json(a) + ",\"superseded_by\":" + json(b) + "}";
        assertThat(fx.post("/v1/catalog/collections/supersede", body).status()).isEqualTo(200);
        java.time.OffsetDateTime stamped = supersededAt(a);

        Resp again = fx.post("/v1/catalog/collections/supersede", body);

        assertThat(again.status()).as(again.body()).isEqualTo(200);
        assertThat(again.json().path("updated").asInt(-1)).isEqualTo(1);
        assertThat(supersededAt(a)).as("not re-stamped").isEqualTo(stamped);
    }

    // ---- retired between the handler's reads and the verb's lock (code 6) -----------------------------------------------------------

    @Test
    void aSupersedeByAnIdRetiredInTheGapAnswers409CollectionSuperseded_andTheNewTargetIsNotApplied() {
        String a = registered("retired-sup-a");
        String b = registered("retired-sup-b");
        String c = registered("retired-sup-c");
        long x = fx.idOf(a);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger retireStatus = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(null, retireInTheGap(a, c, fired, retireStatus), null, null);
        Resp r;
        try {
            r = fx.post("/v1/catalog/collections/supersede", "{\"collection_id\":" + x + ",\"superseded_by\":" + json(b) + "}");
        } finally {
            noHooks();
        }

        assertThat(retireStatus.get()).as("setup: the hook retired the source").isEqualTo(200);
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(x);
        assertThat(r.json().path("superseded_by").asText()).as("it names the row's own successor, not the one asked for").isEqualTo(c);
        assertThat(fx.supersededBy(a)).as("the tombstone is the hook's").isEqualTo(c);
    }

    @Test
    void aRenameByAnIdRetiredInTheGapAnswers409CollectionSuperseded_andNothingIsRegisteredUnderTheNewName() {
        String a = registered("retired-ren-a");
        String c = registered("retired-ren-c");
        String target = label("verbgap-retired-ren-target-" + (n++));
        long x = fx.idOf(a);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger retireStatus = new AtomicInteger();
        fx.service.collectionSelectorFilter().testHooks(null, retireInTheGap(a, c, fired, retireStatus), null, null);
        Resp r;
        try {
            r = fx.post("/v1/catalog/collections/rename", "{\"collection_id\":" + x + ",\"new_name\":" + json(target) + "}");
        } finally {
            noHooks();
        }

        assertThat(retireStatus.get()).as("setup: the hook retired the source").isEqualTo(200);
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(x);
        assertThat(fx.exists(target)).as("nothing was registered under the new name").isFalse();
        assertThat(fx.supersededBy(a)).as("the tombstone is the hook's").isEqualTo(c);
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

    /** From gate 3 the purge is by id, so the old code purged nothing and answered 200 for a freed id: the refusal is what changed. */
    @Test
    void fromGate3ADeleteByAnIdFreedBeforeThePurgeIsRefused_andTheNewHolderOfTheLabelKeepsItsContent() {
        String a = registered("delete-g3-a");
        long x = fx.idOf(a);
        assertThat(fx.post(DELETE, "{\"name\":" + json(a) + "}").status()).as("setup: the id is freed").isEqualTo(200);
        fx.register(a);
        String hex = Chash.ofText(a + "/g3-new-holder-chunk").toHex();
        seedChunk(a, hex);
        long z = fx.idOf(a);
        assertThat(z).as("setup: the label is another row's").isNotEqualTo(x);
        CatalogRepository repo = new CatalogRepository(new TenantScope(fx.svcDs), () -> true, RenameGuard.open());

        assertThatThrownBy(() -> repo.deleteCollectionById(fx.tenant, a, x, false))
            .as("a delete by an id with no row, gate 3 holding")
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
