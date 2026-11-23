// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.LockSupport;

import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23, row 14), the two CatalogRepository writers the census triage found without a registry read of their own.
 *
 * <p>The post-commit sweep ({@code runSweepTransaction}) deletes chunks of a collection by label. A request by {@code collection_id} whose row is
 * renamed in place before the write's registry read, with the old label taken by a new row, used to sweep under the OLD label: the new holder's
 * chunk went and the renamed collection's stayed. The sweep now reads the row by the id the request named, after its gate, and sweeps under the
 * row's current label. {@link #aSweepAfterARenameInTheGap_sweepsTheRenamedCollection_andLeavesTheNewHolder}.
 *
 * <p>The manifest purge ({@code purgeManifest}) deletes a document's manifest rows, whichever collection they name. It now takes those collections'
 * registry rows FOR KEY SHARE (any state) after the document's index-run lock; against a verb holding a row FOR UPDATE it waits, then completes.
 * {@link #aManifestPurge_meetsAVerbHoldingItsCollectionsRow_waitsAndThenPurges}.
 *
 * <p>The fixture drops, on its own database, what the one-row rename drops (see {@code HeavyRoutesRenameGapTest#prepared}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class SweepAndPurgeRegistryFirstTest {

    CollectionRouteFixture fx;
    ExecutorService pool;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = HeavyRoutesRenameGapTest.prepared(new CollectionRouteFixture());
        pool = Executors.newFixedThreadPool(2);
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (fx != null) fx.close();
    }

    private void chunk(String collection, String hex) {
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(hex), List.of("sweep text " + hex), List.of(new float[384]),
                List.of(Map.<String, Object>of()));
            return null;
        });
    }

    private int chunksIn(String collection, String hex) {
        Integer count = fx.asSuperuser(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(fx.tenant).and(CHUNKS.COLLECTION.eq(collection))
            .and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()))));
        return count;
    }

    @Test
    void aSweepAfterARenameInTheGap_sweepsTheRenamedCollection_andLeavesTheNewHolder() {
        String stem = "swp" + (n++);
        String b = label(stem);
        fx.register(b);
        long x = fx.idOf(b);
        String host = label(stem + "-host");
        fx.register(host);
        String doc = "swp-doc-" + stem;
        fx.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, doc, host);
            return null;
        });
        String hex = Chash.ofText("swp-chunk-" + stem).toHex();
        // after the rename: the same chash is an orphan in the renamed collection (under its current label) and in the new holder of the old one
        CollectionRouteFixture.RenameGap gap = fx.renameInPlaceBeforeTheWritesRead(List.of(x), List.of(b), stem, g -> {
            chunk(g.currentLabelOf().get(x), hex);
            chunk(b, hex);
        });
        Resp r;
        try {
            r = fx.post("/v1/catalog/manifest/append", "{\"doc_id\":\"" + doc + "\",\"collection_id\":" + x + ",\"rows\":[],\"sweep_chashes\":[\"" + hex + "\"]}");
        } finally {
            gap.close();
        }
        assertThat(gap.ran()).as("the append reached its registry read by id: %s", r.body()).isTrue();
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        String current = gap.currentLabelOf().get(x);
        assertThat(chunksIn(current, hex)).as("the renamed collection's orphan was swept, under its current label").isZero();
        assertThat(chunksIn(b, hex)).as("the new holder of the old label kept its chunk").isEqualTo(1);
    }

    private int waitingRows() {
        return fx.asSuperuser(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("granted"), Boolean.class).isFalse()
                .and(DSL.field(DSL.name("locktype"), String.class).in("tuple", "transactionid"))));
    }

    @Test
    void aManifestPurge_meetsAVerbHoldingItsCollectionsRow_waitsAndThenPurges() throws Exception {
        String stem = "prg" + (n++);
        String b = label(stem);
        fx.register(b);
        String doc = "prg-doc-" + stem;
        String hex = Chash.ofText("prg-chunk-" + stem).toHex();
        chunk(b, hex);
        fx.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, doc, b);
            CollectionIdSeeds.manifestRows(ctx, fx.tenant, doc, b, 0, hex);
            return null;
        });
        Future<Resp> purge;
        try (Connection verb = fx.pg.createConnection("")) {
            verb.setAutoCommit(false);
            DSL.using(verb, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(b)).forUpdate().fetch();
            purge = pool.submit(() -> fx.post("/v1/catalog/manifest/purge", "{\"doc_id\":\"" + doc + "\"}"));
            try {
                long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
                while (waitingRows() < 1) {
                    if (purge.isDone() || System.nanoTime() > deadline) {
                        throw new AssertionError("the purge never showed waiting on the collection's row (done: " + purge.isDone() + ")");
                    }
                    LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
                }
                assertThat(manifestRows(doc)).as("nothing was purged while the verb held the row").isEqualTo(1);
            } finally {
                verb.rollback();
            }
        }
        Resp r = purge.get(120, TimeUnit.SECONDS);
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(manifestRows(doc)).as("the purge followed once the verb let go").isZero();
    }

    private int manifestRows(String doc) {
        Integer count = fx.asSuperuser(ctx -> ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS,
            CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(fx.tenant).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc))));
        return count;
    }
}
