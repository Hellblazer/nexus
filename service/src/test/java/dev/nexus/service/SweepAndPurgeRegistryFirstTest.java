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

import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
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
 * <p>The fixture drops, on its own database, what the one-row rename drops (see {@code HeavyRoutesRenameGapTest#prepared}). The trash purge
 * test runs on a second, untouched fixture: {@code purge_trash} runs {@code SET CONSTRAINTS fk_catalog_chunks_chunk DEFERRED}, which the
 * one-row rename's preparation drops, and the purge needs no rename.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class SweepAndPurgeRegistryFirstTest {

    CollectionRouteFixture fx;
    /** The database as the product builds it (no foreign key dropped): the trash purge's function names {@code fk_catalog_chunks_chunk}. */
    CollectionRouteFixture fxIntact;
    ExecutorService pool;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = HeavyRoutesRenameGapTest.prepared(new CollectionRouteFixture());
        fxIntact = new CollectionRouteFixture();
        pool = Executors.newFixedThreadPool(2);
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (fx != null) fx.close();
        if (fxIntact != null) fxIntact.close();
    }

    private void chunk(String collection, String hex) {
        chunk(fx, collection, hex);
    }

    private static void chunk(CollectionRouteFixture f, String collection, String hex) {
        f.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, f.tenant, collection, List.of(hex), List.of("sweep text " + hex), List.of(new float[384]),
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

    /**
     * {@code write_many} with {@code sweep}: the post-commit sweep is handed the request's own name ({@code collection}, which the id it named
     * decides), no longer the document's resolved label, and reads the row by id under its own gate. A document whose manifest drops a chash,
     * written by an id whose row is renamed in the gap with the old label retaken, sweeps the dropped chash from the RENAMED collection
     * (under its current label) and leaves the new holder's copy alone.
     */
    @Test
    void writeManyWithSweep_byAnIdWhoseRowWasRenamedInTheGap_sweepsTheRenamedCollection_andLeavesTheNewHolder() {
        String stem = "swm" + (n++);
        String b = label(stem);
        fx.register(b);
        long x = fx.idOf(b);
        String host = label(stem + "-host");
        fx.register(host);
        String doc = "swm-doc-" + stem;
        fx.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, doc, host);
            return null;
        });
        String dropped = Chash.ofText("swm-dropped-" + stem).toHex();
        String kept = Chash.ofText("swm-kept-" + stem).toHex();
        chunk(b, dropped);   // the seed manifest references it, under the label the row bears now
        Resp seed = fx.post("/v1/catalog/manifest/write_many", "{\"collection_id\":" + x + ",\"docs\":[{\"doc_id\":\"" + doc
            + "\",\"rows\":[{\"position\":0,\"chash\":\"" + dropped + "\",\"chunk_index\":0}]}]}");
        assertThat(seed.status()).as("setup: %s", seed.body()).isEqualTo(200);
        CollectionRouteFixture.RenameGap gap = fx.renameInPlaceBeforeTheWritesRead(List.of(x), List.of(b), stem, g -> {
            String current = g.currentLabelOf().get(x);
            chunk(current, dropped);   // the renamed collection's copy, under its current label
            chunk(current, kept);      // what the new manifest row references
        });
        Resp r;
        try {
            r = fx.post("/v1/catalog/manifest/write_many", "{\"collection_id\":" + x + ",\"sweep\":true,\"docs\":[{\"doc_id\":\"" + doc
                + "\",\"rows\":[{\"position\":0,\"chash\":\"" + kept + "\",\"chunk_index\":0}]}]}");
        } finally {
            gap.close();
        }
        assertThat(gap.ran()).as("the write reached its registry read by id: %s", r.body()).isTrue();
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        String current = gap.currentLabelOf().get(x);
        assertThat(chunksIn(current, dropped)).as("the dropped chash was swept from the renamed collection, under its current label").isZero();
        assertThat(chunksIn(b, dropped)).as("the new holder of the old label kept its copy").isEqualTo(1);
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
                // a backend in a lock wait whose statement is the registry row read FOR KEY SHARE: the purge's wait on the verb's lock on that row
                assertThat(PgActivityProbe.waitsOnALock(fx.pg, "%key share%catalog_collections%"))
                    .as("the manifest purge waits on the registry row the verb holds, read FOR KEY SHARE (done: %s)", purge.isDone()).isTrue();
                assertThat(purge.isDone()).as("and has not finished").isFalse();
                assertThat(manifestRows(doc)).as("nothing was purged while the verb held the row").isEqualTo(1);
            } finally {
                verb.rollback();
            }
        }
        Resp r = purge.get(120, TimeUnit.SECONDS);
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(manifestRows(doc)).as("the purge followed once the verb let go").isZero();
    }

    /**
     * {@code purge-trash} hard-deletes aged tombstones and sweeps their chunks in collections the request never names. It reads those
     * collections' registry rows first, so against a verb holding one FOR UPDATE it waits (it has deleted nothing), and completes once the
     * verb lets go.
     */
    @Test
    void aTrashPurge_meetsAVerbHoldingTheRowOfACollectionItReaches_waitsAndThenPurges() throws Exception {
        String stem = "trs" + (n++);
        String b = label(stem);
        fxIntact.register(b);
        String doc = "trs-doc-" + stem;
        String hex = Chash.ofText("trs-chunk-" + stem).toHex();
        chunk(fxIntact, b, hex);
        fxIntact.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fxIntact.tenant, doc, b);
            CollectionIdSeeds.manifestRows(ctx, fxIntact.tenant, doc, b, 0, hex);
            return null;
        });
        int aged = fxIntact.asSuperuser(ctx -> ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.DELETED_AT, java.time.OffsetDateTime.now().minusDays(40))
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(fxIntact.tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(doc))).execute());
        assertThat(aged).as("setup: the document is an aged tombstone").isEqualTo(1);
        Future<Resp> purge;
        try (Connection verb = fxIntact.pg.createConnection("")) {
            verb.setAutoCommit(false);
            DSL.using(verb, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fxIntact.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(b)).forUpdate().fetch();
            purge = pool.submit(() -> fxIntact.post("/v1/catalog/purge-trash", "{\"older_than_days\":30,\"dry_run\":false}"));
            try {
                // a backend in a lock wait whose statement is the registry row read FOR KEY SHARE: the purge's wait on the verb's lock on that row
                // (not any ungranted lock in the database)
                assertThat(PgActivityProbe.waitsOnALock(fxIntact.pg, "%key share%catalog_collections%"))
                    .as("the trash purge waits on the registry row the verb holds, read FOR KEY SHARE (done: %s)", purge.isDone()).isTrue();
                assertThat(purge.isDone()).as("and has not finished").isFalse();
                assertThat(manifestRows(fxIntact, doc)).as("nothing was purged while the verb held the row").isEqualTo(1);
            } finally {
                verb.rollback();
            }
        }
        Resp r = purge.get(120, TimeUnit.SECONDS);
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("documents_purged").asLong(-1)).as("the aged tombstone was purged once the verb let go").isEqualTo(1);
        assertThat(manifestRows(fxIntact, doc)).isZero();
    }

    private int manifestRows(String doc) {
        return manifestRows(fx, doc);
    }

    private static int manifestRows(CollectionRouteFixture f, String doc) {
        Integer count = f.asSuperuser(ctx -> ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS,
            CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(f.tenant).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc))));
        return count;
    }
}
