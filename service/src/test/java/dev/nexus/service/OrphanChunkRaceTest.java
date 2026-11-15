// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-z0o2p.29: the two production signatures of an ownerless chunk row (a chunk with no
 * {@code catalog_document_chunks} owner AND no {@code chunk_orphaned_at} record), each driven
 * deterministically on a local engine. The invariant every test asserts last: every chunk row of
 * the collection has an owner row, or a record that it lost its last one.
 *
 * <p>The sweep's gate timeout is forced by holding the sweep gate SHARED from a second connection
 * (the sweeper's EXCLUSIVE acquire then hits its 2 s {@code lock_timeout}, SQLSTATE 55P03), never by
 * sleeping. These began as debug reproductions; with the fixes (a write_many that resolves only listed chunks, an
 * append that refuses a duplicate position, vectors-061's record of every drop) each now pins one of the holes closed.
 */
class OrphanChunkRaceTest extends AtomicWriteTestBase {

    // ── path A ──────────────────────────────────────────────────────────────────────────────────

    /**
     * A's signature: rows a combined write INSERTED (fresh last_written_at), now with no owner, no
     * chunk_orphaned_at, no gc_audit. The insert and its owner row commit together; the owner is
     * then dropped by the next REPLACE of the document within the hour, and that write's post-commit
     * sweep fails open on gate_timeout. The manifest trigger used to skip the drop (chunk written within
     * the hour) and the failed sweep records nothing; vectors-061 records every drop.
     */
    @Test
    void pathA_insertedChunksDroppedWithinTheHour_sweepGateTimeout_areRecorded() throws Exception {
        Fx f = fixture("pa");
        String a = ch("pa-a"), n1 = ch("pa-n1"), n2 = ch("pa-n2");
        // The run's write of the document: inserts n1, n2 (new) with their owner rows, one txn.
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(a, "pa a"), chunk(n1, "pa n1"), chunk(n2, "pa n2")),
            List.of(doc(f.docId(), List.of(row(0, a), row(1, n1), row(2, n2)))),
            null, true, false);
        assertThat(unaccounted(f.collection())).as("precondition: every chunk owned").isEmpty();

        Map<String, Object> response;
        try (Connection holder = holdSweepGateShared(f.collection())) {
            // A later REPLACE of the same document drops n1, n2; its sweep cannot get the gate.
            response = svc.writeManyCombined(TENANT, f.collection(),
                List.of(chunk(a, "pa a")),
                List.of(doc(f.docId(), List.of(row(0, a)))),
                null, true, false).response();
            holder.rollback();
        }
        assertSweepGateTimeout(response, f.docId());

        assertThat(unaccounted(f.collection()))
            .as("RDR-223: a chunk the engine inserted has an owner row or a chunk_orphaned_at record")
            .isEmpty();
    }

    /**
     * Two rows at one position in one append used to insert BOTH chashes and then let the per-row
     * UPSERT_APPEND keep only the second, committing the first chunk with no row listing it. The
     * engine now refuses the request as a bad one (IllegalArgumentException, 400) before the embed and
     * before any chunk is written.
     */
    @Test
    void pathA_appendTwoRowsAtOnePosition_isRefusedAndInsertsNoChunk() throws Exception {
        Fx f = fixture("pd");
        String x = ch("pd-x"), y = ch("pd-y");
        int embedsBefore = embedder.calls.get();

        assertThatThrownBy(() -> svc.appendCombined(TENANT, f.collection(), f.docId(),
                List.of(row(0, x), row(0, y)),
                List.of(chunk(x, "pd x"), chunk(y, "pd y")), false))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("duplicate 'position' 0");

        assertThat(embedder.calls.get()).as("refused before the embed").isEqualTo(embedsBefore);
        assertThat(chunkExists(f.collection(), x)).isFalse();
        assertThat(chunkExists(f.collection(), y)).isFalse();
        assertThat(manifestChashes(f.docId())).isEmpty();
        assertThat(unaccounted(f.collection())).isEmpty();
    }

    /**
     * The same refusal on append_many, as that route reports a bad document: the document fails in
     * place and writes nothing, a sibling document of the request is unaffected.
     */
    @Test
    void pathA_appendManyTwoRowsAtOnePosition_failsThatDocWithNoChunk() throws Exception {
        Fx f = fixture("pe");
        String good = freshDoc("pe-good", f.collection());
        String seedDoc = freshDoc("pe-seed", f.collection());
        String x = ch("pe-x"), y = ch("pe-y"), g = ch("pe-g"), old = ch("pe-old");
        // An existing chunk, aged, that only the refused document lists in this request.
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(old, "pe old")),
            List.of(doc(seedDoc, List.of(row(0, old)))), null, false, false);
        OffsetDateTime aged = ageChunks(f.collection(), 2);
        int embedsBefore = embedder.calls.get();

        var response = svc.appendManyCombined(TENANT, f.collection(),
            List.of(doc(f.docId(), List.of(row(0, x), row(0, y), row(1, old))),
                    doc(good, List.of(row(0, g)))),
            List.of(chunk(x, "pe x"), chunk(y, "pe y"), chunk(g, "pe g"), chunk(old, "pe old")), false).response();

        assertThat(failed(response)).containsExactly(f.docId());
        assertThat(chunkExists(f.collection(), x)).isFalse();
        assertThat(chunkExists(f.collection(), y)).isFalse();
        assertThat(chunkExists(f.collection(), g)).isTrue();
        assertThat(manifestChashes(f.docId())).isEmpty();
        assertThat(response.get("chunks_unreferenced")).as("x, y and old: the refused doc lists them").isEqualTo(3);
        assertThat(embedder.calls.get() - embedsBefore).as("only g is embedded").isEqualTo(1);
        assertThat(lastWrittenAt(f.collection(), old))
            .as("the refused doc's identical-text chunk is not refreshed").isBefore(aged.plusMinutes(30));
        assertThat(unaccounted(f.collection())).isEmpty();
    }

    /**
     * write_many has no such hole and needs no check of its own: its manifest insert is PLAIN, so two
     * rows at one position violate the primary key, the document fails in its own transaction and the
     * chunk upsert that ran before the rows rolls back with it.
     */
    @Test
    void pathA_writeManyTwoRowsAtOnePosition_failsTheDocAndRollsBackItsChunks() throws Exception {
        Fx f = fixture("pf");
        String x = ch("pf-x"), y = ch("pf-y");

        var response = svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(x, "pf x"), chunk(y, "pf y")),
            List.of(doc(f.docId(), List.of(row(0, x), row(0, y)))),
            null, true, false).response();

        assertThat(failed(response)).containsExactly(f.docId());
        assertThat(chunkExists(f.collection(), x)).isFalse();
        assertThat(chunkExists(f.collection(), y)).isFalse();
        assertThat(unaccounted(f.collection())).isEmpty();
    }

    /**
     * What did NOT reproduce A on write_many: a payload chunk referenced only by an unregistered
     * document, and one referenced only by a document that fails in its own transaction (it also
     * names a chash that exists nowhere), are never inserted. upsertManifestChunkVectors inserts
     * only chashes the doc's own rows list, before those rows, in the same transaction.
     */
    @Test
    void pathA_writeMany_chunksOfFailedOrUnregisteredDocs_areNotInserted() throws Exception {
        Fx f = fixture("pn");
        String good = freshDoc("pn-good", f.collection());
        String n1 = ch("pn-n1"), n2 = ch("pn-n2"), n3 = ch("pn-n3"), absent = ch("pn-absent");

        var response = svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(n1, "pn n1"), chunk(n2, "pn n2"), chunk(n3, "pn n3")),
            List.of(doc("aw.pn.unregistered", List.of(row(0, n1))),
                    doc(f.docId(), List.of(row(0, n2), row(1, absent))),
                    doc(good, List.of(row(0, n3)))),
            null, true, false).response();

        assertThat(failed(response))
            .containsExactlyInAnyOrder("aw.pn.unregistered", f.docId());
        assertThat(chunkExists(f.collection(), n1)).isFalse();
        assertThat(chunkExists(f.collection(), n2)).isFalse();
        assertThat(chunkExists(f.collection(), n3)).isTrue();
        assertThat(unaccounted(f.collection())).isEmpty();
    }

    /**
     * The same hole with the sweep switched off (a multi-batch write's first batch, sweep=false): a
     * second write drops chunks the first one inserted within the hour. The manifest trigger used to
     * skip the drop (written within the hour) and no sweep runs at all, so nothing recorded it.
     */
    @Test
    void pathA_insertedChunksDroppedWithinTheHour_sweepOff_areRecorded() throws Exception {
        Fx f = fixture("pg");
        String a = ch("pg-a"), n1 = ch("pg-n1");
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(a, "pg a"), chunk(n1, "pg n1")),
            List.of(doc(f.docId(), List.of(row(0, a), row(1, n1)))),
            null, false, false);
        assertThat(unaccounted(f.collection())).as("precondition: every chunk owned").isEmpty();

        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(a, "pg a")),
            List.of(doc(f.docId(), List.of(row(0, a)))),
            null, false, false);

        assertThat(unaccounted(f.collection()))
            .as("RDR-223: a chunk the engine inserted has an owner row or a chunk_orphaned_at record")
            .isEmpty();
    }

    // ── path B ──────────────────────────────────────────────────────────────────────────────────

    /**
     * B's signature: OLD rows (created long before) rewritten by one transaction together with
     * many linked rows, then ownerless with no chunk_orphaned_at. The rewrite was the combined write's
     * Phase 2a metadata-only refresh (CombinedWriteService.resolveChunks to
     * PgVectorRepository.batchUpdateMetadata), which sets last_written_at = now() on EVERY
     * identical-text chunk it is handed, whether or not a row of a document that commits lists it.
     * Here c is in the payload only for a document that is not registered, the one owner that held it
     * drops it, and the drop's sweep fails on gate_timeout. The fix keeps the unreferenced chunk out
     * of the refresh, so c stays aged, the manifest DELETE trigger records the drop (older than an
     * hour), and the invariant holds.
     */
    @Test
    void pathB_unreferencedChunkIsNotRefreshed_soItsDropIsRecordedDespiteAFailedSweep() throws Exception {
        Fx f = fixture("pb");
        String a = ch("pb-a"), b = ch("pb-b"), c = ch("pb-c");
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(a, "pb a"), chunk(b, "pb b"), chunk(c, "pb c")),
            List.of(doc(f.docId(), List.of(row(0, a), row(1, b), row(2, c)))),
            null, false, false);
        OffsetDateTime aged = ageChunks(f.collection(), 2);

        Map<String, Object> response;
        try (Connection holder = holdSweepGateShared(f.collection())) {
            response = svc.writeManyCombined(TENANT, f.collection(),
                List.of(chunk(a, "pb a"), chunk(b, "pb b"), chunk(c, "pb c")),     // identical text
                List.of(doc(f.docId(), List.of(row(0, a), row(1, b))),            // drops c
                        doc("aw.pb.unregistered", List.of(row(0, c)))),           // the only lister of c fails
                null, true, false).response();
            holder.rollback();
        }
        assertThat(failed(response)).containsExactly("aw.pb.unregistered");
        assertSweepGateTimeout(response, f.docId());
        assertThat(response.get("chunks_unreferenced")).as("c: only an unregistered document lists it").isEqualTo(1);
        assertThat(lastWrittenAt(f.collection(), c))
            .as("c was not refreshed: no document that can commit lists it")
            .isBefore(aged.plusMinutes(30));

        assertThat(unaccounted(f.collection()))
            .as("a chunk that lost its last owner has a chunk_orphaned_at record")
            .isEmpty();
    }

    /**
     * The part of path B that fix 2 does not reach: c is listed by a REGISTERED document that then
     * fails in its own transaction (it also names a chash that exists nowhere). That document is a
     * referencer, so c is refreshed (outside any transaction, so the failure does not undo it), and
     * the drop of c by the document that commits used to be skipped by the trigger and left unrecorded by
     * the failed sweep. vectors-061 records it in the dropping transaction, whatever the refresh did.
     */
    @Test
    void pathB_chunkListedOnlyByARegisteredFailingDoc_isRefreshed_thenFailedSweep_isRecordedAnyway()
            throws Exception {
        Fx f = fixture("ph");
        String failing = freshDoc("ph-failing", f.collection());
        String a = ch("ph-a"), b = ch("ph-b"), c = ch("ph-c"), absent = ch("ph-absent");
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(a, "ph a"), chunk(b, "ph b"), chunk(c, "ph c")),
            List.of(doc(f.docId(), List.of(row(0, a), row(1, b), row(2, c)))),
            null, false, false);
        ageChunks(f.collection(), 2);

        Map<String, Object> response;
        try (Connection holder = holdSweepGateShared(f.collection())) {
            response = svc.writeManyCombined(TENANT, f.collection(),
                List.of(chunk(a, "ph a"), chunk(b, "ph b"), chunk(c, "ph c")),
                List.of(doc(f.docId(), List.of(row(0, a), row(1, b))),
                        doc(failing, List.of(row(0, c), row(1, absent)))),
                null, true, false).response();
            holder.rollback();
        }
        assertThat(failed(response)).containsExactly(failing);
        assertSweepGateTimeout(response, f.docId());

        assertThat(unaccounted(f.collection()))
            .as("a chunk that lost its last owner has a chunk_orphaned_at record")
            .isEmpty();
    }

    /**
     * Fix 2 directly: a payload chunk that no registered document's rows reference is neither embedded
     * (new chash, so it is not stored) nor refreshed (existing chash, so its last_written_at is
     * unchanged), and the response counts it as unreferenced, the field the append routes report.
     */
    @Test
    void writeMany_unreferencedPayloadChunk_isNotEmbeddedNorRefreshed() throws Exception {
        Fx f = fixture("pu");
        String a = ch("pu-a"), old = ch("pu-old"), fresh = ch("pu-fresh");
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(a, "pu a"), chunk(old, "pu old")),
            List.of(doc(f.docId(), List.of(row(0, a), row(1, old)))),
            null, false, false);
        OffsetDateTime aged = ageChunks(f.collection(), 2);
        int embedsBefore = embedder.calls.get();

        var response = svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(a, "pu a"), chunk(old, "pu old"), chunk(fresh, "pu fresh")),
            List.of(doc(f.docId(), List.of(row(0, a)))),     // lists a only; old and fresh are unreferenced
            null, false, false).response();

        assertThat(response.get("chunks_unreferenced")).isEqualTo(2);
        assertThat(response.get("embed_embedded")).as("fresh is not embedded").isEqualTo(0);
        assertThat(embedder.calls.get()).as("nothing was sent to the embedder").isEqualTo(embedsBefore);
        assertThat(chunkExists(f.collection(), fresh)).isFalse();
        assertThat(lastWrittenAt(f.collection(), old)).as("old is not refreshed").isBefore(aged.plusMinutes(30));
        assertThat(lastWrittenAt(f.collection(), a)).as("a is referenced, so it is refreshed").isAfter(aged.plusMinutes(30));
    }

    /**
     * Control for the test above: the same aged chunk dropped with the same failed sweep, but NOT in
     * the payload, so no refresh. The DELETE trigger records the drop and the invariant holds. The
     * difference between the two is the refresh alone.
     */
    @Test
    void pathB_control_agedChunkDroppedWithoutRefresh_isRecorded() throws Exception {
        Fx f = fixture("pc");
        String a = ch("pc-a"), b = ch("pc-b"), c = ch("pc-c");
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(a, "pc a"), chunk(b, "pc b"), chunk(c, "pc c")),
            List.of(doc(f.docId(), List.of(row(0, a), row(1, b), row(2, c)))),
            null, false, false);
        ageChunks(f.collection(), 2);

        Map<String, Object> response;
        try (Connection holder = holdSweepGateShared(f.collection())) {
            response = svc.writeManyCombined(TENANT, f.collection(),
                List.of(chunk(a, "pc a"), chunk(b, "pc b")),
                List.of(doc(f.docId(), List.of(row(0, a), row(1, b)))),
                null, true, false).response();
            holder.rollback();
        }
        assertSweepGateTimeout(response, f.docId());
        assertThat(chunkExists(f.collection(), c)).as("the failed sweep left c").isTrue();
        assertThat(unaccounted(f.collection())).isEmpty();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────

    /**
     * Chunk rows of {@code collection} with no own-collection manifest owner and no
     * chunk_orphaned_at record, as hex, sorted. Superuser read: RLS bypassed.
     */
    private List<String> unaccounted(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<String> out = new ArrayList<>();
            ctx.select(CHUNKS.CHASH).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT))
                .and(CHUNKS.COLLECTION.eq(collection))
                .and(DSL.notExists(ctx.selectOne().from(CATALOG_DOCUMENT_CHUNKS)
                    .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(CHUNKS.TENANT_ID))
                    .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(CHUNKS.COLLECTION))
                    .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(CHUNKS.CHASH))))
                .and(DSL.notExists(ctx.selectOne().from(CHUNK_ORPHANED_AT)
                    .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(CHUNKS.TENANT_ID))
                    .and(CHUNK_ORPHANED_AT.COLLECTION.eq(CHUNKS.COLLECTION))
                    .and(CHUNK_ORPHANED_AT.CHASH.eq(CHUNKS.CHASH))))
                .fetch()
                .forEach(r -> out.add(HexFormat.of().formatHex(r.value1())));
            out.sort(null);
            return out;
        }
    }

    /** Sets every chunk of {@code collection} to have been last written {@code hours} ago; returns that time. */
    private OffsetDateTime ageChunks(String collection, int hours) throws Exception {
        OffsetDateTime then = OffsetDateTime.now().minusHours(hours);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS)
                .set(CHUNKS.LAST_WRITTEN_AT, then)
                .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection)))
                .execute();
        }
        return then;
    }

    private OffsetDateTime lastWrittenAt(String collection, String hexChash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(CHUNKS.LAST_WRITTEN_AT).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT))
                .and(CHUNKS.COLLECTION.eq(collection))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hexChash)))
                .fetchOne(CHUNKS.LAST_WRITTEN_AT);
        }
    }

    /**
     * Holds the sweep gate SHARED for {@code collection} in an open transaction on its own
     * connection (acquireSweepGateShared's exact shape). Writers (also SHARED) pass; the post-commit
     * sweep's EXCLUSIVE acquire times out. The caller rolls back to release.
     */
    private Connection holdSweepGateShared(String collection) throws Exception {
        Connection conn = pg.createConnection("");
        conn.setAutoCommit(false);
        DSL.using(conn, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock_shared", Object.class,
                DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + TENANT + "/" + collection)))).fetch();
        return conn;
    }

    @SuppressWarnings("unchecked")
    private static List<String> failed(Map<String, Object> response) {
        return (List<String>) response.get("failed_doc_ids");
    }

    @SuppressWarnings("unchecked")
    private static void assertSweepGateTimeout(Map<String, Object> response, String docId) {
        var detail = (List<Map<String, Object>>) response.get("sweep_detail");
        assertThat(detail).as("the scenario forced a gate_timeout on " + docId)
            .anySatisfy(d -> {
                assertThat(d.get("doc_id")).isEqualTo(docId);
                assertThat(d.get("reason")).isEqualTo("gate_timeout");
            });
    }
}
