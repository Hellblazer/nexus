// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.PgContainerHelper;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * The orphaning record's triggers as vectors-061 (nexus-z0o2p.29) left them: a chunk that loses its last manifest
 * owner is recorded whatever its age, a chunk that gains an owner carries no record, and the trigger functions'
 * posture and lock order. The reapable predicate and the record's older semantics stay in
 * {@link ChunkIsReapableIntegrationTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunkOrphanRecordTriggerIntegrationTest extends ChunkReapableTestBase {

    // ── no write-time margin ──────────────────────────────────────────────────

    /**
     * nexus-z0o2p.29: no write-time margin. A chunk written moments ago and then left with no owner is
     * recorded like an aged one (it used to be skipped, leaving it ownerless with no record when the drop's
     * sweep then failed). Its grace starts at the drop: the later of its write and its record.
     */
    @Test
    void aChunkWrittenWithinTheLastHourIsRecordedWhenItLosesItsLastOwner() throws Exception {
        String t = "reap-stamp-guard";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "recent", Duration.ofMinutes(5));
        document(t, "stamp-g1", KNOWLEDGE, false);
        manifestRow(t, "stamp-g1", KNOWLEDGE, hex, 0);

        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-g1"))).execute());

        assertThat(orphanRecordedRecently(t, KNOWLEDGE, hex)).as("written 5 minutes ago: recorded all the same").isTrue();
        assertThat(isReapable(t, KNOWLEDGE, hex)).isFalse();
    }

    /** The same margin on the record itself: a drop within the hour of the previous one does not move it. */
    @Test
    void aChunkRecordedWithinTheLastHourIsNotRecordedAgain() throws Exception {
        String t = "reap-stamp-guard-record";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "rerecorded", DAYS_40);
        document(t, "stamp-gr1", KNOWLEDGE, false);
        manifestRow(t, "stamp-gr1", KNOWLEDGE, hex, 0);
        // after the insert: an insert clears a record (nexus-z0o2p.29), so the record is written once the row exists
        recordOrphaning(t, KNOWLEDGE, hex, Duration.ofMinutes(20));
        OffsetDateTime before = orphanedAt(t, KNOWLEDGE, hex);
        su(ctx -> ctx.update(CHUNKS).set(CHUNKS.LAST_WRITTEN_AT, OffsetDateTime.now().minus(DAYS_40))
            .where(CHUNKS.TENANT_ID.eq(t).and(CHUNKS.COLLECTION.eq(KNOWLEDGE)).and(CHUNKS.CHASH.eq(bytes(hex)))).execute());

        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("stamp-gr1"))).execute());

        assertThat(orphanedAt(t, KNOWLEDGE, hex)).as("recorded 20 minutes ago: left as it was").isEqualTo(before);
    }

    /** A stale record of a chunk that is owned again can never make it reapable: condition 1 fails. */
    @Test
    void aStaleRecordOfAReOwnedChunkIsHarmless() throws Exception {
        String t = "reap-record-reowned";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "reowned", DAYS_40);
        recordOrphaning(t, KNOWLEDGE, hex, DAYS_40);
        assertThat(isReapable(t, KNOWLEDGE, hex)).as("precondition: orphaned 40 days ago, reapable").isTrue();

        document(t, "record-reowned-doc", KNOWLEDGE, false);
        manifestRow(t, "record-reowned-doc", KNOWLEDGE, hex, 0);
        // the insert cleared the record; put one back, as a racing writer's late commit would leave it
        recordOrphaning(t, KNOWLEDGE, hex, DAYS_40);

        assertThat(isReapable(t, KNOWLEDGE, hex)).as("owned again: the old record means nothing").isFalse();
    }

    // ── a re-owned chunk carries no record (nexus-z0o2p.29) ───────────────────

    /**
     * A manifest REPLACE is a DELETE of the document's rows and an INSERT of them back, two statements in one
     * transaction. The DELETE records every chunk it drops, the INSERT clears the record of every key it inserts,
     * so a replace that keeps its chunks leaves no record, aged or just written.
     */
    @Test
    void aReplaceThatKeepsTheSameChunksLeavesNoRecord() throws Exception {
        String t = "reap-replace-same";
        register(t, KNOWLEDGE);
        String aged = orphan(t, KNOWLEDGE, "aged", DAYS_40);
        String fresh = orphan(t, KNOWLEDGE, "fresh", Duration.ofMinutes(1));
        document(t, "replace-same", KNOWLEDGE, false);
        manifestRow(t, "replace-same", KNOWLEDGE, aged, 0);
        manifestRow(t, "replace-same", KNOWLEDGE, fresh, 1);
        su(ctx -> ageTo(ctx, t, KNOWLEDGE, aged, DAYS_40));

        suTxn(ctx -> {
            ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
               .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("replace-same"))).execute();
            String model = PgContainerHelper.collectionModel(ctx, t, KNOWLEDGE);
            int position = 0;
            for (String hex : List.of(aged, fresh)) {
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
                   .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, t)
                   .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, "replace-same")
                   .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position++)
                   .set(CATALOG_DOCUMENT_CHUNKS.CHASH, bytes(hex))
                   .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, KNOWLEDGE)
                   .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CollectionIds.idOf(t, KNOWLEDGE))
                   .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model).execute();
            }
        });

        assertThat(orphanedAt(t, KNOWLEDGE, aged)).as("aged chunk, re-owned in the same transaction").isNull();
        assertThat(orphanedAt(t, KNOWLEDGE, fresh)).as("fresh chunk, re-owned in the same transaction").isNull();
        assertThat(recordCount(t)).isZero();
    }

    /** A replace that drops one chunk records exactly that one, whatever its age. */
    @Test
    void aReplaceThatDropsAChunkRecordsItAndOnlyIt() throws Exception {
        String t = "reap-replace-drop";
        register(t, KNOWLEDGE);
        String kept = orphan(t, KNOWLEDGE, "kept", Duration.ofMinutes(1));
        String dropped = orphan(t, KNOWLEDGE, "dropped", Duration.ofMinutes(1));
        document(t, "replace-drop", KNOWLEDGE, false);
        manifestRow(t, "replace-drop", KNOWLEDGE, kept, 0);
        manifestRow(t, "replace-drop", KNOWLEDGE, dropped, 1);

        suTxn(ctx -> {
            ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
               .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("replace-drop"))).execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
               .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, t)
               .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, "replace-drop")
               .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0)
               .set(CATALOG_DOCUMENT_CHUNKS.CHASH, bytes(kept))
               .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, KNOWLEDGE)
               .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CollectionIds.idOf(t, KNOWLEDGE))
               .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(ctx, t, KNOWLEDGE))
               .execute();
        });

        assertThat(orphanedAt(t, KNOWLEDGE, kept)).isNull();
        assertThat(orphanRecordedRecently(t, KNOWLEDGE, dropped)).as("fresh, dropped, recorded").isTrue();
        assertThat(recordCount(t)).isEqualTo(1);
    }

    /** A chunk dropped in one transaction and listed again in a later one: the later insert clears its record. */
    @Test
    void aChunkDroppedAndListedAgainInALaterTransactionCarriesNoRecord() throws Exception {
        String t = "reap-relist-later";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "relisted", Duration.ofMinutes(2));
        document(t, "relist-1", KNOWLEDGE, false);
        document(t, "relist-2", KNOWLEDGE, false);
        manifestRow(t, "relist-1", KNOWLEDGE, hex, 0);
        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("relist-1"))).execute());
        assertThat(orphanRecordedRecently(t, KNOWLEDGE, hex)).as("precondition: dropped, recorded").isTrue();

        manifestRow(t, "relist-2", KNOWLEDGE, hex, 0);

        assertThat(orphanedAt(t, KNOWLEDGE, hex)).as("listed again by another document: no record").isNull();
    }

    /** The UPDATE path: a row moved onto a chash that carried a record clears it, and records the chash it left. */
    @Test
    void anUpdateThatMovesARowOntoARecordedChashClearsItsRecord() throws Exception {
        String t = "reap-update-clears";
        register(t, KNOWLEDGE);
        String left = orphan(t, KNOWLEDGE, "left", Duration.ofMinutes(2));
        String arrived = orphan(t, KNOWLEDGE, "arrived", Duration.ofMinutes(2));
        document(t, "update-clears", KNOWLEDGE, false);
        manifestRow(t, "update-clears", KNOWLEDGE, left, 0);
        recordOrphaning(t, KNOWLEDGE, arrived, DAYS_40);
        assertThat(orphanedAt(t, KNOWLEDGE, arrived)).as("precondition").isNotNull();

        su(ctx -> ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.CHASH, bytes(arrived))
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("update-clears"))).execute());

        assertThat(orphanedAt(t, KNOWLEDGE, arrived)).as("gained an owner: record cleared").isNull();
        assertThat(orphanRecordedRecently(t, KNOWLEDGE, left)).as("lost its owner, fresh: recorded").isTrue();
    }

    /** One chash at two positions of one document, dropped together: one record (the keys are distinct). */
    @Test
    void oneChashAtTwoPositionsDroppedTogetherIsRecordedOnce() throws Exception {
        String t = "reap-two-positions";
        register(t, KNOWLEDGE);
        String hex = orphan(t, KNOWLEDGE, "twice", Duration.ofMinutes(3));
        document(t, "two-positions", KNOWLEDGE, false);
        manifestRow(t, "two-positions", KNOWLEDGE, hex, 0);
        manifestRow(t, "two-positions", KNOWLEDGE, hex, 5);

        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("two-positions"))).execute());

        assertThat(orphanRecordedRecently(t, KNOWLEDGE, hex)).isTrue();
        assertThat(recordCount(t)).isEqualTo(1);
    }

    /**
     * The same chash stored in two collections of one tenant is two chunks. Dropping its last owner in one
     * collection records that collection's chunk only; the other collection's owner and chunk are untouched.
     */
    @Test
    void aChashInTwoCollectionsIsRecordedInTheCollectionItWasDroppedFrom() throws Exception {
        String t = "reap-two-collections";
        register(t, KNOWLEDGE);
        register(t, OTHER);
        String hex = ch(t + "/shared");
        su(ctx -> {
            for (String coll : List.of(KNOWLEDGE, OTHER)) {
                PgContainerHelper.insertChunks(ctx, t, coll, List.of(hex), List.of("shared text"),
                    List.of(new float[384]), List.of(Map.of()));
            }
        });
        document(t, "two-coll-k", KNOWLEDGE, false);
        document(t, "two-coll-o", OTHER, false);
        manifestRow(t, "two-coll-k", KNOWLEDGE, hex, 0);
        manifestRow(t, "two-coll-o", OTHER, hex, 0);

        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(t).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("two-coll-k"))).execute());

        assertThat(orphanRecordedRecently(t, KNOWLEDGE, hex)).as("dropped from knowledge").isTrue();
        assertThat(orphanedAt(t, OTHER, hex)).as("still owned in the other collection: no record").isNull();
        assertThat(recordCount(t)).isEqualTo(1);
    }

    /**
     * One INSERT ... ON CONFLICT DO UPDATE statement fires BOTH statement triggers: position 0 conflicts (its row
     * moves from {@code left} to {@code moved}), position 1 is new ({@code added}). End state: {@code left}
     * recorded; {@code moved} (cleared by the UPDATE trigger) and {@code added} (cleared by the INSERT trigger)
     * owned with no record, both of which carried one before.
     */
    @Test
    void anUpsertAppendThatFiresBothTriggersEndsWithOwnedChunksAndNoRecord() throws Exception {
        String t = "reap-upsert-both";
        register(t, KNOWLEDGE);
        String left = orphan(t, KNOWLEDGE, "left", Duration.ofMinutes(4));
        String moved = orphan(t, KNOWLEDGE, "moved", Duration.ofMinutes(4));
        String added = orphan(t, KNOWLEDGE, "added", Duration.ofMinutes(4));
        document(t, "upsert-both", KNOWLEDGE, false);
        manifestRow(t, "upsert-both", KNOWLEDGE, left, 0);
        recordOrphaning(t, KNOWLEDGE, moved, DAYS_40);
        recordOrphaning(t, KNOWLEDGE, added, DAYS_40);

        su(ctx -> {
            String model = PgContainerHelper.collectionModel(ctx, t, KNOWLEDGE);
            Long collectionId = CollectionIds.require(ctx, t, KNOWLEDGE);
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                    CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID,
                    CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
               .values(t, "upsert-both", 0, 0, bytes(moved), KNOWLEDGE, collectionId, model)
               .values(t, "upsert-both", 1, 1, bytes(added), KNOWLEDGE, collectionId, model)
               .onConflict(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION)
               .doUpdate().set(CATALOG_DOCUMENT_CHUNKS.CHASH, DSL.excluded(CATALOG_DOCUMENT_CHUNKS.CHASH))
               .execute();
        });

        assertThat(orphanRecordedRecently(t, KNOWLEDGE, left)).as("displaced from position 0").isTrue();
        assertThat(orphanedAt(t, KNOWLEDGE, moved)).as("moved onto position 0: UPDATE trigger cleared it").isNull();
        assertThat(orphanedAt(t, KNOWLEDGE, added)).as("new at position 1: INSERT trigger cleared it").isNull();
        assertThat(recordCount(t)).isEqualTo(1);
    }

    /**
     * Two writers replacing documents that share chunks, listing them in opposite orders, against the record
     * triggers, over the engine's own manifest write (CatalogRepository#writeManifest, under DeadlockRetry). Every
     * replace must commit: a deadlock between the writers is retried, not surfaced. The end state is every chunk
     * owned and no record, because each replace lists again every chunk it dropped.
     */
    @Test
    void twoConcurrentReplacesOfDocumentsThatShareChunksBothCommit_andLeaveNoRecord() throws Exception {
        String t = "reap-concurrent-replace";
        register(t, KNOWLEDGE);
        List<String> hexes = new java.util.ArrayList<>();
        for (int i = 0; i < 6; i++) hexes.add(orphan(t, KNOWLEDGE, "shared-" + i, Duration.ofMinutes(10)));
        document(t, "conc-a", KNOWLEDGE, false);
        document(t, "conc-b", KNOWLEDGE, false);
        List<String> reversed = new java.util.ArrayList<>(hexes);
        java.util.Collections.reverse(reversed);
        var repo = new dev.nexus.service.db.CatalogRepository(tenantScope);
        repo.writeManifest(t, "conc-a", KNOWLEDGE, rowsOf(hexes));
        repo.writeManifest(t, "conc-b", KNOWLEDGE, rowsOf(reversed));

        int rounds = 40;
        java.util.concurrent.CountDownLatch go = new java.util.concurrent.CountDownLatch(1);
        CompletableFuture<Void> a = CompletableFuture.runAsync(() -> {
            await(go);
            for (int i = 0; i < rounds; i++) repo.writeManifest(t, "conc-a", KNOWLEDGE, rowsOf(hexes));
        });
        CompletableFuture<Void> b = CompletableFuture.runAsync(() -> {
            await(go);
            for (int i = 0; i < rounds; i++) repo.writeManifest(t, "conc-b", KNOWLEDGE, rowsOf(reversed));
        });
        go.countDown();
        a.get(120, TimeUnit.SECONDS);
        b.get(120, TimeUnit.SECONDS);

        assertThat(repo.getManifest(t, "conc-a")).hasSize(6);
        assertThat(repo.getManifest(t, "conc-b")).hasSize(6);
        assertThat(recordCount(t)).as("every dropped chunk was listed again by the same replace").isZero();
    }

    private static List<Map<String, Object>> rowsOf(List<String> hexes) {
        List<Map<String, Object>> rows = new java.util.ArrayList<>();
        for (int i = 0; i < hexes.size(); i++) rows.add(Map.of("position", i, "chash", hexes.get(i), "chunk_index", i));
        return rows;
    }

    private static void await(java.util.concurrent.CountDownLatch latch) {
        try {
            latch.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(e);
        }
    }

    /** The clear function is held to the same posture as the stamp functions, and locks the records in key order. */
    @Test
    void theClearFunctionIsInvokerNotPublic_locksTheRecordsInKeyOrder_andNeverWritesChunks() throws Exception {
        String def = functionDefinition("nexus.clear_orphaned_on_manifest_insert()");
        assertThat(def).contains("FOR UPDATE OF q");
        assertThat(occurrences(def, "ORDER BY q.tenant_id, q.collection, q.chash")).isEqualTo(1);
        assertThat(def.indexOf("FOR UPDATE OF q")).as("the lock comes before the delete")
            .isLessThan(def.indexOf("DELETE FROM nexus.chunk_orphaned_at"));
        assertThat(occurrences(def, "n.tenant_id = q.tenant_id")).as("tenant equality in the lock pass and the delete").isEqualTo(2);
        assertThat(def).doesNotContain("nexus.chunks ").doesNotContain("UPDATE nexus.chunks");
        var proc = DSL.table(DSL.name("pg_catalog", "pg_proc"));
        var name = DSL.field(DSL.name("proname"), String.class);
        var acl = DSL.cast(DSL.field(DSL.name("proacl")), SQLDataType.VARCHAR);
        Boolean[] definer = new Boolean[1];
        String[] aclText = new String[1];
        su(ctx -> {
            var r = ctx.select(DSL.field(DSL.name("prosecdef"), Boolean.class), acl).from(proc)
                .where(name.eq("clear_orphaned_on_manifest_insert")).fetchOne();
            definer[0] = r.value1();
            aclText[0] = r.value2();
        });
        assertThat(definer[0]).as("SECURITY INVOKER").isFalse();
        assertThat(aclText[0]).as("EXECUTE revoked from PUBLIC").isNotNull().doesNotContainPattern("(^\\{|,)=X/");
    }
}
