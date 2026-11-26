// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.PgActivityProbe;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.datamove.Gate3;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.7 residual of nexus-p2m1x.27 (shipped in the Phase 3 cut by Sam's choice, owed to the next engine): a rename's retag of the quarantine
 * rows of the origin ({@link QuarantineOrigin#retagRowsOf}) reads and writes the rows of the quarantine siblings, and took none of the siblings'
 * registry rows. A sibling renamed or retired in the retag's window was then read and written under a label or id the registry no longer gave it.
 * The retag now takes each sibling's row {@code FOR KEY SHARE}, in {@code collection_id} order, before it reads the rows.
 *
 * <p>The sibling's row is held {@code FOR UPDATE} by a hand session, as a rename or a delete of the sibling holds it. The retag must wait on that row
 * BEFORE it changes a chunk (without the lock it ran straight through, the rows being the hand session's no concern), and finish once the row is
 * released. The wait is seen through {@code pg_stat_activity} ({@link PgActivityProbe}), never a sleep.
 */
class QuarantineRetagSiblingLockTest extends AtomicWriteTestBase {

    private final ExecutorService pool = Executors.newCachedThreadPool();

    @AfterEach
    void stopPool() {
        pool.shutdownNow();
    }

    private String col(String tag) {
        return "docs__qrt" + tag + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    /** The origin tags of the rows of {@code collection}, as text. */
    private List<String> tagsIn(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "origin_collection")).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(collection))
                .fetch(0, String.class);
        }
    }

    /** One run of the retag against a verb that holds the sibling's registry row FOR UPDATE: {@code latched} reads by id (gate 3), else by label. */
    private void retagWaitsOnTheSibling(boolean latched) throws Exception {
        String tenant = "qrt" + seq.incrementAndGet();
        String from = col("from");
        String to = col("to");
        String sibling = "quarantine-" + from;
        long originId;
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, from);
            PgContainerHelper.insertCollection(ctx, tenant, sibling);
            originId = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(from))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            // the tag names the origin by its id as well, as the move writes it: from gate 3 that key is the one read
            PgContainerHelper.insertChunks(ctx, tenant, sibling, List.of(Chash.ofText("qrt retag " + from).toHex()), List.of("qrt retag"),
                List.of(new float[384]), List.of(Map.<String, Object>of("origin_collection", from, QuarantineOrigin.ORIGIN_ID_KEY, originId)));
        }
        assertThat(tagsIn(tenant, sibling)).as("setup: the sibling holds a row tagged for the origin").containsExactly(from);
        Gate3 gate3 = latched ? () -> true : Gate3.never();

        Future<Integer> retag;
        try (Connection verb = pg.createConnection("")) {
            verb.setAutoCommit(false);
            DSL.using(verb, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(sibling)).forUpdate().fetch();
            retag = pool.submit(() -> tenantScope.withTenant(tenant, ctx -> QuarantineOrigin.retagRowsOf(ctx, tenant, from, to, gate3)));

            assertThat(PgActivityProbe.waitsOnALock(pg, "%key share%catalog_collections%"))
                .as("the retag waits on the sibling's registry row, read FOR KEY SHARE, held FOR UPDATE by the verb").isTrue();
            assertThat(retag.isDone()).as("and has not finished").isFalse();
            assertThat(tagsIn(tenant, sibling)).as("it had changed no row when it met the verb").containsExactly(from);
            verb.rollback();
        }

        assertThat(retag.get(60, TimeUnit.SECONDS)).as("rows retagged once the verb let go").isEqualTo(1);
        assertThat(tagsIn(tenant, sibling)).as("the row now names the new origin label").containsExactly(to);
    }

    @Timeout(120)
    @Test
    void aRetagTakesTheSiblingsRegistryRow_andWaitsOnAVerbThatHoldsItForUpdate_beforeChangingAChunk() throws Exception {
        retagWaitsOnTheSibling(false);
    }

    /** The same, from gate 3: the siblings are found by the chunks' collection ids, and their rows are locked by those ids. */
    @Timeout(120)
    @Test
    void fromGate3_aRetagTakesTheSiblingsRowById_andWaitsOnAVerbThatHoldsItForUpdate_beforeChangingAChunk() throws Exception {
        retagWaitsOnTheSibling(true);
    }
}
