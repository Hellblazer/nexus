// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.Chash;
import dev.nexus.service.jooq.nexus.Routines;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.Record5;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS;
import static dev.nexus.service.jooq.nexus.Tables.GC_QUARANTINE_ORPHANS_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.GC_RESTORE_REREFERENCED_BOUNDED;
import static dev.nexus.service.jooq.nexus.Tables.REAPER_QUARANTINE_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28, linda 2026-11-25): the five SQL functions that register or refresh a registry row from another collection's tuple
 * never raise a bare 23505 from {@code catalog_collections_live_tuple_uq} when another live collection holds the tuple, and never create a
 * second live holder of it (vectors-085). They refuse by name, SQLSTATE 22023, before anything moves.
 *
 * <p>Both families are reachable by direct SQL only (the routes read the origin's row first), so the tests call the functions on the real
 * engine database, with the index built by hand (the changeset waits for the re-own move) and, as in {@code Rdr229P35bRegistryArbiterIntegrationTest},
 * the name keys and name foreign keys dropped, the state P3.7 and P4.1 leave: a retired row and its successor then share a label with a live row.
 * <ul>
 *   <li>The moves ({@code gc_quarantine_orphans}, {@code _bounded}, {@code reaper_quarantine_chunks}) refresh the row that bears the sibling's name
 *       with the ORIGIN's tuple. A row that is not a quarantine collection and bears that name sits inside the index; the copy lands on the origin's
 *       own tuple (the origin is live) and raised 23505. A quarantine row is refreshed as before.</li>
 *   <li>The restores ({@code gc_restore_rereferenced}, {@code _bounded}) mint a first-ever origin live with the sibling's tuple. An origin that is only
 *       a retired row (its label has no live row) while its successor holds the tuple raised 23505 at that INSERT.</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class QuarantineTupleGuardIntegrationTest {

    static final String T = "qtguard";
    static final String TS = "2026-11-21T00:00:00Z";
    static final String BGE = "bge-base-en-v15-768";
    static final String INVALID_PARAMETER_VALUE = "22023";

    PostgreSQLContainer<?> pg;
    Connection su;
    DSLContext ctx;
    final AtomicInteger seq = new AtomicInteger();

    enum Move { GC_QUARANTINE_ORPHANS, GC_QUARANTINE_ORPHANS_BOUNDED, REAPER_QUARANTINE_CHUNKS }

    enum Restore { GC_RESTORE_REREFERENCED, GC_RESTORE_REREFERENCED_BOUNDED }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        su = pg.createConnection("");
        su.setAutoCommit(true);
        ctx = DSL.using(su, SQLDialect.POSTGRES);
        ctx.select(DSL.function("set_config", String.class, DSL.val("nexus.tenant"), DSL.val(T), DSL.val(false))).fetch();
        PgContainerHelper.ensureTenantPartitions(ctx, T);
        // P3.7's and P4.1's state: no foreign key onto the registry's name, no name key (a retired row and a live row may share a label)
        assertThat(ByIdRestoreSupport.dropRegistryNameKeys(ctx)).as("non-vacuity: name foreign keys existed").isNotEmpty();
        ctx.alterTable(CATALOG_COLLECTIONS).dropConstraint(DSL.name("catalog_collections_pk")).cascade().execute();
        ctx.alterTable(CATALOG_COLLECTIONS).dropConstraintIfExists(DSL.name("catalog_collections_tenant_name_model_uq")).cascade().execute();
        // the restores name a chunk by a manifest row of the origin while the chunk is in the quarantine sibling
        ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists(DSL.name("fk_catalog_chunks_chunk")).execute();
        CollectionMintSupport.dropIndex(ctx);
        CollectionMintSupport.createIndex(ctx);
        assertThat(CollectionMintSupport.indexPresent(ctx)).as("non-vacuity: the live tuple index is in force").isTrue();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    // -- the fixture -------------------------------------------------------------------------------------

    private String owner(String tag) {
        return "qtg-" + tag;
    }

    /** A registry row of {@code name} with the tuple of {@code owner}, as a superuser writes it. */
    private long insertRow(String name, String owner, String lifecycle, String supersededBy, Long supersededById) {
        var insert = ctx.insertInto(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.TENANT_ID, T).set(CATALOG_COLLECTIONS.NAME, name)
            .set(CATALOG_COLLECTIONS.CONTENT_TYPE, "docs").set(CATALOG_COLLECTIONS.OWNER_ID, owner)
            .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL, BGE).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, lifecycle);
        if (supersededBy != null) {
            insert.set(CATALOG_COLLECTIONS.SUPERSEDED_BY, supersededBy).set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, supersededById);
        }
        insert.execute();
        return ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID.desc()).limit(1).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
    }

    private byte[] chunk(String collection, long collectionId, String tag, String metadataJson, int ageDays) {
        byte[] hash = Chash.ofText(collection + "/" + tag).toBytes();
        ctx.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, T).set(CHUNKS.COLLECTION, collection).set(CHUNKS.COLLECTION_ID, collectionId).set(CHUNKS.CHASH, hash)
            .set(CHUNKS.EMBEDDING_MODEL, BGE)
            .set(CHUNKS.CHUNK_TEXT, tag + " text").set(VectorSeeds.chunkEmbedding(768), VectorSeeds.vec(768, 0.1f))
            .set(CHUNKS.METADATA, JSONB.valueOf(metadataJson))
            .set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minusDays(ageDays))
            .execute();
        return hash;
    }

    private boolean chunkIn(long collectionId, byte[] hash) {
        return ctx.fetchExists(CHUNKS, CHUNKS.TENANT_ID.eq(T).and(CHUNKS.COLLECTION_ID.eq(collectionId)).and(CHUNKS.CHASH.eq(hash)));
    }

    private long liveRowsNamed(String name) {
        return ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(T).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("")));
    }

    private Record5<String, String, String, String, String> tupleOf(long collectionId) {
        return ctx.select(CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(collectionId))
            .fetchOne();
    }

    private void callMove(Move fn, String origin, String sibling) {
        switch (fn) {
            case GC_QUARANTINE_ORPHANS -> ctx.selectFrom(GC_QUARANTINE_ORPHANS.call(768, T, origin, sibling, TS, 20)).fetch();
            case GC_QUARANTINE_ORPHANS_BOUNDED ->
                ctx.selectFrom(GC_QUARANTINE_ORPHANS_BOUNDED.call(768, T, origin, sibling, TS, 20, 100)).fetch();
            case REAPER_QUARANTINE_CHUNKS ->
                ctx.selectFrom(REAPER_QUARANTINE_CHUNKS.call(T, origin, sibling, TS, 100, null, 1.0, 0, false)).fetch();
        }
    }

    private static String sqlState(Throwable t) {
        return t instanceof DataAccessException e ? e.sqlState() : null;
    }

    // -- the moves: the refresh of the row that bears the sibling's name ----------------------------------

    @ParameterizedTest(name = "{0}: a live row that is not a quarantine collection bears the sibling's name")
    @EnumSource(Move.class)
    void aMove_refusesByName_whenTheRowBearingTheSiblingsNameWouldCopyTheOriginsLiveTuple(Move fn) {
        String tag = "mv" + fn.ordinal() + "-" + seq.incrementAndGet();
        String origin = "docs__" + owner(tag) + "__" + BGE + "__v1";
        String sibling = "quarantine-" + origin;
        long originId = insertRow(origin, owner(tag), "live", null, null);
        // a LIVE row bearing the sibling's name, with a tuple of its own: the copy of the origin's tuple onto it lands on the origin's live tuple
        long squatterId = insertRow(sibling, owner(tag) + "-other", "live", null, null);
        byte[] hash = chunk(origin, originId, "orphan", "{}", 90);
        var before = tupleOf(squatterId);

        assertThatThrownBy(() -> callMove(fn, origin, sibling))
            .as("%s refuses by name, not with the tuple index's 23505", fn)
            .satisfies(t -> assertThat(sqlState(t)).isEqualTo(INVALID_PARAMETER_VALUE))
            .hasMessageContaining("not a quarantine collection")
            .hasMessageNotContaining("catalog_collections_live_tuple_uq");

        assertThat(tupleOf(squatterId)).as("the row that bears the sibling's name keeps its own tuple").isEqualTo(before);
        assertThat(chunkIn(originId, hash)).as("nothing moved: the chunk is still in the origin").isTrue();
        assertThat(chunkIn(squatterId, hash)).isFalse();
    }

    @ParameterizedTest(name = "{0}: a quarantine row bears the sibling's name, refreshed as before")
    @EnumSource(Move.class)
    void aMove_stillRefreshesAQuarantineRowWithTheOriginsTuple(Move fn) {
        String tag = "mq" + fn.ordinal() + "-" + seq.incrementAndGet();
        String origin = "docs__" + owner(tag) + "__" + BGE + "__v1";
        String sibling = "quarantine-" + origin;
        long originId = insertRow(origin, owner(tag), "live", null, null);
        long siblingId = insertRow(sibling, owner(tag) + "-stale", "quarantine", null, null);
        byte[] hash = chunk(origin, originId, "orphan", "{}", 90);

        callMove(fn, origin, sibling);

        var refreshed = tupleOf(siblingId);
        assertThat(refreshed.value2()).as("the sibling took the origin's owner").isEqualTo(owner(tag));
        assertThat(refreshed.value5()).isEqualTo("quarantine");
        assertThat(chunkIn(siblingId, hash)).as("the chunk moved into the sibling").isTrue();
        assertThat(chunkIn(originId, hash)).isFalse();
    }

    // -- the restores: the first-ever registration of the origin -------------------------------------------

    @ParameterizedTest(name = "{0}: the origin has only a retired row and its successor holds the tuple")
    @EnumSource(Restore.class)
    void aRestore_refusesByName_whenTheOriginHasNoLiveRowAndAnotherLiveRowHoldsTheTuple(Restore fn) {
        String tag = "rs" + fn.ordinal() + "-" + seq.incrementAndGet();
        String origin = "docs__" + owner(tag) + "__" + BGE + "__v1";
        String successor = "docs__" + owner(tag) + "-succ__" + BGE + "__v1";
        String sibling = "quarantine-" + origin;
        // the origin was superseded by its successor and the successor took the tuple; the origin's manifest rows stayed (a supersede moves nothing)
        long successorId = insertRow(successor, owner(tag), "live", null, null);
        long originId = insertRow(origin, owner(tag), "live", successor, successorId);
        long siblingId = insertRow(sibling, owner(tag), "quarantine", null, null);
        byte[] hash = chunk(sibling, siblingId, "held", "{\"origin_collection\": \"" + origin + "\", \"quarantined_at\": \"" + TS + "\"}", 1);
        String doc = "qtg" + tag + ".1";
        PgContainerHelper.insertCatalogDocument(ctx, T, doc);
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, T).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0).set(CATALOG_DOCUMENT_CHUNKS.CHASH, hash)
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, origin).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, originId)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, BGE)
            .execute();
        assertThat(liveRowsNamed(origin)).as("non-vacuity: the origin's label has no live row").isZero();

        assertThatThrownBy(() -> {
            if (fn == Restore.GC_RESTORE_REREFERENCED) {
                Routines.gcRestoreRereferenced(ctx.configuration(), 768, T, sibling, origin);
            } else {
                ctx.selectFrom(GC_RESTORE_REREFERENCED_BOUNDED.call(768, T, sibling, origin, 100)).fetch();
            }
        })
            .as("%s refuses by name, not with the tuple index's 23505", fn)
            .satisfies(t -> assertThat(sqlState(t)).isEqualTo(INVALID_PARAMETER_VALUE))
            .hasMessageContaining("already holds the tuple")
            .hasMessageContaining(successor)
            .hasMessageNotContaining("catalog_collections_live_tuple_uq");

        assertThat(liveRowsNamed(origin)).as("no second live holder: the origin was not minted live").isZero();
        assertThat(chunkIn(siblingId, hash)).as("the origin is not lost: its chunk is still quarantined").isTrue();
        assertThat(chunkIn(originId, hash)).isFalse();
        assertThat(ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(T)
            .and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("")).and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.ne("quarantine"))
            .and(CATALOG_COLLECTIONS.OWNER_ID.eq(owner(tag)))))
            .as("one live holder of the tuple, the successor").isEqualTo(1);
    }

    @Test
    void theIndexIsTheSameOneTheGuardsAnswerFor() {
        // the control of the tests above: the unguarded INSERT of a second live holder IS refused by the index with the 23505 the functions used to raise
        String tag = "ix-" + seq.incrementAndGet();
        insertRow("docs__" + owner(tag) + "__" + BGE + "__v1", owner(tag), "live", null, null);
        assertThatThrownBy(() -> insertRow("docs__" + owner(tag) + "-dup__" + BGE + "__v1", owner(tag), "live", null, null))
            .satisfies(t -> assertThat(sqlState(t)).isEqualTo("23505"))
            .hasMessageContaining(CollectionMintSupport.INDEX);
    }
}
