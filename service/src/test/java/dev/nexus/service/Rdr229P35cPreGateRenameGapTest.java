// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CHASH_REMAP;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_LINKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25) regressions before gate 3, in the rename gap of {@link Rdr229P35cIdAddressedRenameRaceTest} (linda's rulings
 * on the census triage of c774b2ecb4, nexus-p2m1x.23): the route boundary resolved the id X to the label B, and just before the write's own
 * by-id read X's row is renamed in place to C and a NEW row Y takes B. Gate 3 has NOT latched, so a statement still names a collection by
 * its name column. A write by the id X must therefore name the row's CURRENT label C, never the label B the request carried:
 * <ul>
 *   <li>{@code updateMetadataWithMissing} locked X's row and computed C, then handed the request's B to {@code updateMetadataOneRow}
 *       (regression of 368cf7da04): it changed Y's chunk, not X's;</li>
 *   <li>{@code RemapRepository.clearLeg} matched both legs by the request's label through {@code CollectionKey.eqHistory}, whose
 *       pre-gate-3 arm is {@code name = request label} (regression of ced0ada045): it cleared Y's leg, not X's.</li>
 * </ul>
 * The rename runs on another thread ({@link CollectionRouteFixture#renameInPlaceBeforeTheWritesRead}); the rows a write looks for under
 * the row's current label are filed once the rename is done (its {@code afterRename} callback), as that method's javadoc says.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35cPreGateRenameGapTest {

    CollectionRouteFixture fx;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        fx.asSuperuser(ctx -> {
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(ctx);   // a one-row rename cascades nowhere: the name keys are P3.7's and P4.1's
            return null;
        });
        assertThat(fx.gate3.holds()).as("setup: gate 3 has not latched on this install").isFalse();
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    private String registered(String stem) {
        String l = label("pg-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    private void seedChunk(String collection, String hex, String marker) {
        seedChunk(collection, hex, "pre-gate gap " + marker, marker);
    }

    private void seedChunk(String collection, String hex, String text, String marker) {
        fx.asSuperuser(ctx -> {
            dev.nexus.service.PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(hex), List.of(text),
                List.of(new float[384]), List.of(Map.<String, Object>of("seed", marker)));
            return null;
        });
    }

    private boolean chunkExists(long collectionId, String hex) {
        byte[] chash = java.util.HexFormat.of().parseHex(hex);
        Integer n = fx.asSuperuser(ctx -> ctx.fetchCount(CHUNKS,
            CHUNKS.TENANT_ID.eq(fx.tenant).and(CHUNKS.COLLECTION_ID.eq(collectionId)).and(CHUNKS.CHASH.eq(chash))));
        return n > 0;
    }

    private String metadataOf(long collectionId, String hex) {
        byte[] chash = java.util.HexFormat.of().parseHex(hex);
        return fx.asSuperuser(ctx -> ctx.select(CHUNKS.METADATA).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(fx.tenant).and(CHUNKS.COLLECTION_ID.eq(collectionId)).and(CHUNKS.CHASH.eq(chash)))
            .fetchOne(CHUNKS.METADATA).data());
    }

    /**
     * (i) {@code updateMetadataWithMissing}: the metadata merge lands on X's chunk (filed under its current label C) and the new holder Y's chunk
     * of the same chash, filed under B, keeps what it had.
     */
    @Test
    void updateMetadataById_beforeGate3_inTheRenameGap_changesTheRenamedCollectionsChunk_notTheNewHoldersOfTheOldLabel() {
        String b = registered("meta");
        long x = fx.idOf(b);
        String hex = Chash.ofText("pre-gate-meta-" + b).toHex();
        try (CollectionRouteFixture.RenameGap gap = fx.renameInPlaceBeforeTheWritesRead(List.of(x), List.of(b), "pg-meta-" + (n++), g -> {
            seedChunk(g.currentLabelOf().get(x), hex, "x-own");   // X's chunk, under the label it bears now
            seedChunk(b, hex, "y-own");                           // the new holder's chunk of the same chash, under B
        })) {
            Resp r = fx.post("/v1/vectors/update-metadata",
                "{\"collection_id\":" + x + ",\"ids\":[" + json(hex) + "],\"metadatas\":[{\"gap_key\":\"gap\"}]}");

            assertThat(gap.ran()).as("setup: the rename ran before the write's by-id read (%s)", r.body()).isTrue();
            assertThat(r.status()).as(r.body()).isEqualTo(200);
            long y = gap.newHolders().iterator().next();
            assertThat(y).as("setup: B is a different row now").isNotEqualTo(x);
            String xMeta = metadataOf(x, hex);
            String yMeta = metadataOf(y, hex);
            assertThat(xMeta).as("the renamed collection's chunk is the one changed").contains("gap_key").contains("x-own");
            assertThat(yMeta).as("the new holder's chunk is untouched").doesNotContain("gap_key").contains("y-own");
        }
    }

    /**
     * (1) {@code resolveNeedEmbedIdx}: an upsert by id of a chunk already stored with the same text takes the metadata-only path, and the
     * merge lands on X's chunk (under its current label), not on the new holder Y's chunk of the same chash under the old label. Before the
     * fix the batched UPDATE named the request's label: it changed Y's chunk, and X's was left stale.
     */
    @Test
    void upsertChunksById_beforeGate3_inTheRenameGap_mergesIntoTheRenamedCollectionsChunk_notTheNewHoldersOfTheOldLabel() {
        String b = registered("upsert");
        long x = fx.idOf(b);
        String text = "pre-gate upsert text " + b;
        String hex = Chash.ofText(text).toHex();
        try (CollectionRouteFixture.RenameGap gap = fx.renameInPlaceBeforeTheWritesRead(List.of(x), List.of(b), "pg-upsert-" + (n++), g -> {
            seedChunk(g.currentLabelOf().get(x), hex, text, "x-own");
            seedChunk(b, hex, text, "y-own");
        })) {
            Resp r = fx.post("/v1/vectors/upsert-chunks", "{\"collection_id\":" + x + ",\"ids\":[" + json(hex) + "],\"documents\":[" + json(text)
                + "],\"metadatas\":[{\"gap_key\":\"gap\"}]}");

            assertThat(gap.ran()).as("setup: the rename ran before the write's by-id read (%s)", r.body()).isTrue();
            assertThat(r.status()).as(r.body()).isEqualTo(200);
            long y = gap.newHolders().iterator().next();
            assertThat(metadataOf(x, hex)).as("the renamed collection's chunk took the merge").contains("gap_key").contains("x-own");
            assertThat(metadataOf(y, hex)).as("the new holder's chunk is untouched").doesNotContain("gap_key").contains("y-own");
        }
    }

    /**
     * (2) {@code PgVectorRepository.delete}: a store-delete by id removes X's chunk (under its current label) and leaves the new holder Y's chunk
     * of the same chash. Before the fix the DELETE named the request's label: it removed Y's chunk and answered for X with nothing deleted.
     */
    @Test
    void storeDeleteById_beforeGate3_inTheRenameGap_deletesTheRenamedCollectionsChunk_notTheNewHoldersOfTheOldLabel() {
        String b = registered("del");
        long x = fx.idOf(b);
        String hex = Chash.ofText("pre-gate-delete-" + b).toHex();
        try (CollectionRouteFixture.RenameGap gap = fx.renameInPlaceBeforeTheWritesRead(List.of(x), List.of(b), "pg-del-" + (n++), g -> {
            seedChunk(g.currentLabelOf().get(x), hex, "x-own");
            seedChunk(b, hex, "y-own");
        })) {
            Resp r = fx.post("/v1/vectors/store-delete", "{\"collection_id\":" + x + ",\"ids\":[" + json(hex) + "]}");

            assertThat(gap.ran()).as("setup: the rename ran before the write's by-id read (%s)", r.body()).isTrue();
            assertThat(r.status()).as(r.body()).isEqualTo(200);
            long y = gap.newHolders().iterator().next();
            assertThat(chunkExists(x, hex)).as("the renamed collection's chunk is deleted").isFalse();
            assertThat(chunkExists(y, hex)).as("the new holder's chunk is still there").isTrue();
        }
    }

    private long seedTopic(long collectionId, String label) {
        Resp r = fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + collectionId + ",\"label\":" + json(label) + ",\"doc_count\":1}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
        return r.json().path("id").asLong();
    }

    private void seedLink(long from, long to) {
        fx.asSuperuser(ctx -> ctx.insertInto(TOPIC_LINKS, TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID,
            TOPIC_LINKS.LINK_COUNT, TOPIC_LINKS.LINK_TYPES).values(fx.tenant, from, to, 1, org.jooq.JSONB.jsonb("[]")).execute());
    }

    private int linksBetween(long from, long to) {
        Integer n = fx.asSuperuser(ctx -> ctx.fetchCount(TOPIC_LINKS,
            TOPIC_LINKS.TENANT_ID.eq(fx.tenant).and(TOPIC_LINKS.FROM_TOPIC_ID.eq(from)).and(TOPIC_LINKS.TO_TOPIC_ID.eq(to))));
        return n;
    }

    /**
     * (3) {@code TaxonomyRepository.purgeCollection}: the doomed topics (whose links and assignments go) are those of the locked row. Before the
     * fix they were found by the request's label, which is the new holder Y's topics after the rename: Y's link was deleted and X's survived.
     */
    @Test
    void purgeCollectionById_beforeGate3_inTheRenameGap_deletesTheRenamedCollectionsLinks_notTheNewHoldersOfTheOldLabel() {
        String b = registered("purge");
        long x = fx.idOf(b);
        long x1 = seedTopic(x, "x-one");
        long x2 = seedTopic(x, "x-two");
        seedLink(x1, x2);
        long[] y = new long[2];
        try (CollectionRouteFixture.RenameGap gap = fx.renameInPlaceBeforeTheWritesRead(List.of(x), List.of(b), "pg-purge-" + (n++), g -> {
            long yid = g.newHolders().iterator().next();
            y[0] = seedTopic(yid, "y-one");
            y[1] = seedTopic(yid, "y-two");
            seedLink(y[0], y[1]);
        })) {
            Resp r = fx.post("/v1/taxonomy/purge_collection", "{\"collection_id\":" + x + "}");

            assertThat(gap.ran()).as("setup: the rename ran before the write's by-id read (%s)", r.body()).isTrue();
            assertThat(r.status()).as(r.body()).isEqualTo(200);
            assertThat(linksBetween(x1, x2)).as("the renamed collection's link is purged").isZero();
            assertThat(linksBetween(y[0], y[1])).as("the new holder's link is untouched").isEqualTo(1);
        }
    }

    private void recordLeg(String source, String target, String marker) {
        Resp r = fx.post("/v1/remap/record_batch", "{\"source_collection\":" + json(source) + ",\"entries\":[{\"old_id\":\"old-" + marker + "\","
            + "\"new_chash\":\"" + Chash.ofText("pre-gate-remap-" + marker + source + target).toHex() + "\",\"target_collection\":"
            + json(target) + ",\"provenance\":\"pg\"}]}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
    }

    private int legRows(String source, String target) {
        Integer rows = fx.asSuperuser(ctx -> ctx.fetchCount(CHASH_REMAP,
            CHASH_REMAP.TENANT_ID.eq(fx.tenant).and(CHASH_REMAP.SOURCE_COLLECTION.eq(source)).and(CHASH_REMAP.TARGET_COLLECTION.eq(target))));
        return rows;
    }

    /**
     * (ii) {@code RemapRepository.clearLeg}: the source leg named by X's id clears X's rows (filed under its current label C) and leaves the
     * new holder Y's rows, filed under B, in place.
     */
    @Test
    void clearLegById_beforeGate3_inTheRenameGap_clearsTheRenamedCollectionsLeg_notTheNewHoldersOfTheOldLabel() {
        String b = registered("leg-s");
        String t = registered("leg-t");
        long x = fx.idOf(b);
        try (CollectionRouteFixture.RenameGap gap = fx.renameInPlaceBeforeTheWritesRead(List.of(x), List.of(b), "pg-leg-" + (n++), g -> {
            recordLeg(g.currentLabelOf().get(x), t, "x-own");   // X's leg, under the label it bears now
            recordLeg(b, t, "y-own");                           // the new holder's leg onto the same target, under B
        })) {
            Resp r = fx.post("/v1/remap/clear_leg", "{\"source_collection_id\":" + x + ",\"target_collection\":" + json(t) + "}");

            assertThat(gap.ran()).as("setup: the rename ran before the write's by-id read (%s)", r.body()).isTrue();
            assertThat(r.status()).as(r.body()).isEqualTo(200);
            String c = gap.currentLabelOf().get(x);
            assertThat(r.json().path("deleted").asInt(-1)).as("the renamed collection's leg row was cleared").isEqualTo(1);
            assertThat(legRows(c, t)).as("X's leg, under its current label, is gone").isZero();
            assertThat(legRows(b, t)).as("the new holder's leg is untouched").isEqualTo(1);
        }
    }
}
