// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
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
        fx.asSuperuser(ctx -> {
            dev.nexus.service.PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(hex), List.of("pre-gate gap " + marker),
                List.of(new float[384]), List.of(Map.<String, Object>of("seed", marker)));
            return null;
        });
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
}
