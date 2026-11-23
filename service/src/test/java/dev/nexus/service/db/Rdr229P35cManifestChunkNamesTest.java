// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.RegistryRenameSupport;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.5c, G17 (linda 2026-11-23): {@code ManifestChunkNames.of} chooses deterministically between two chunk rows of one collection
 * id and one chash, which exist only across embedding models (P3.3c's id key on chunks is {@code (collection_id, chash, model)}): the row
 * whose stored name is the collection's current registry label, else the lexicographically smallest stored name, whatever order the rows
 * come back in.
 */
class Rdr229P35cManifestChunkNamesTest extends AtomicWriteTestBase {

    @BeforeAll
    void dropTheNameKeys() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            RegistryRenameSupport.dropForeignKeysOntoTheRegistry(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    private <T> T su(java.util.function.Function<DSLContext, T> body) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return body.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private Map<String, String> names(long id, String hex) {
        return tenantScope.withTenant(TENANT, ctx -> ManifestChunkNames.of(ctx, TENANT, id, List.of(hex)));
    }

    @Test
    void twoModelsOfOneChash_theCurrentLabelWins_elseTheSmallestName_neverTheRowOrder() throws Exception {
        int n = seq.incrementAndGet();
        // same id, one chash, two models (two names: a 384 and a 768 collection); the 768 name sorts below the 384 one
        String big = "code__zz" + n + "__minilm-l6-v2-384__v1";
        String small = "code__aa" + n + "__bge-base-en-v15-768__v1";
        String hex = Chash.ofText("p35c manifest names " + n).toHex();
        long id = su(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, big);
            PgContainerHelper.insertCollection(ctx, TENANT, small);
            PgContainerHelper.insertChunks(ctx, TENANT, big, List.of(hex), List.of("t"), List.of(new float[384]), List.of(Map.of()));
            PgContainerHelper.insertChunks(ctx, TENANT, small, List.of(hex), List.of("t"), List.of(new float[768]), List.of(Map.of()));
            Long bigId = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(big)).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            int moved = ctx.update(CHUNKS).set(CHUNKS.COLLECTION_ID, bigId)
                .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(small)).execute();
            assertThat(moved).as("fixture: the 768 chunk row carries the 384 collection's id").isEqualTo(1);
            return bigId;
        });
        int rowsOfId = su(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION_ID.eq(id))));
        assertThat(rowsOfId).as("non-vacuity: two chunk rows of one id and one chash").isEqualTo(2);

        assertThat(names(id, hex)).as("the registry's current label is the stored name of one row: that one, not the smallest")
            .containsEntry(hex, big);

        String mid = "code__mm" + n + "__minilm-l6-v2-384__v1";
        su(ctx -> { RegistryRenameSupport.rename(ctx, TENANT, big, mid); return null; });
        assertThat(names(id, hex)).as("no stored name is the current label (a one-row rename): the smallest name")
            .containsEntry(hex, small);
    }
}
