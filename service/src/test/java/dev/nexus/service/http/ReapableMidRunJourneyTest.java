// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.IntConsumer;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_IS_REAPABLE;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 7/8 journeys through the REAL writers (review T2 nexus/review-wbfpw15-17-code C1): the
 * multi-batch combined writer ({@code manifest/write_many} then {@code manifest/append}), the document
 * delete and manifest purge, and the quarantine round trip, with the gc functions and the reapable listing
 * run in the middle.
 *
 * <p>No fixture here stamps anything. The old chunks are written by the real writer and then made OLD by
 * moving {@code last_written_at} into the past, which is what time does; everything that has to protect them
 * (the orphaning record, vectors-021-1 and -3) is the engine's own. An earlier design protected an in-flight run
 * through a metadata key no writer sets, and its tests passed on hand-stamped fixtures; these cannot.
 *
 * <p>Collections are {@code docs__}, one of the all-prefix producers (streaming PDF, oversize code and
 * markdown are the multi-batch writers). The driver is the RDR's client protocol as {@code
 * MultiBatchWriteJourneyTest} drives it.
 */
class ReapableMidRunJourneyTest extends AtomicWriteTestBase {

    private static final ObjectMapper JSON = new ObjectMapper();

    private CatalogHandler handler;
    private PgVectorRepository vectors;

    @BeforeAll
    void wire() {
        handler = new CatalogHandler(repo, svc);
        vectors = new PgVectorRepository(tenantScope, embedder, embedder);
    }

    // ── the driver ───────────────────────────────────────────────────────────

    private static String hex(String text) {
        return Chash.ofText(text).toHex();
    }

    private static Map<String, Object> chunkOf(String text, int position) {
        Map<String, Object> c = new LinkedHashMap<>();
        c.put("chash", hex(text));
        c.put("text", text);
        c.put("metadata", Map.of("position", position));
        return c;
    }

    private static Map<String, Object> rowOf(String text, int position) {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("position", position);
        r.put("chash", hex(text));
        r.put("chunk_index", position);
        return r;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> send(String path, Map<String, Object> body) throws Exception {
        CapturingExchange ex = new CapturingExchange("POST", URI.create(path), JSON.writeValueAsString(body));
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        assertThat(ex.status).as("%s -> %s", path, ex.bodyString()).isEqualTo(200);
        return JSON.readValue(ex.bodyString(), Map.class);
    }

    /** Batch 1 is write_many (replaces the manifest, sweep off); later batches are append; the last carries the sweep. */
    @SuppressWarnings("unchecked")
    private void writeDocument(String collection, String docId, List<List<String>> batches, IntConsumer afterRequest)
            throws Exception {
        List<String> dropped = List.of();
        int position = 0;
        for (int k = 1; k <= batches.size(); k++) {
            List<Map<String, Object>> chunks = new ArrayList<>();
            List<Map<String, Object>> rows = new ArrayList<>();
            for (String text : batches.get(k - 1)) {
                chunks.add(chunkOf(text, position));
                rows.add(rowOf(text, position));
                position++;
            }
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("collection", collection);
            body.put("chunks", chunks);
            if (k == 1) {
                body.put("sweep", false);
                body.put("docs", List.of(Map.of("doc_id", docId, "rows", rows)));
                Map<String, Object> r = send("/v1/catalog/manifest/write_many", body);
                dropped = ((Map<String, List<String>>) r.get("dropped_chashes")).get(docId);
            } else {
                body.put("doc_id", docId);
                body.put("rows", rows);
                if (k == batches.size() && !dropped.isEmpty()) body.put("sweep_chashes", dropped);
                send("/v1/catalog/manifest/append", body);
            }
            if (afterRequest != null) afterRequest.accept(k);
        }
    }

    private static List<List<String>> batches(String p, String[][] names) {
        List<List<String>> out = new ArrayList<>();
        for (String[] batch : names) {
            List<String> texts = new ArrayList<>();
            for (String n : batch) texts.add(p + n);
            out.add(texts);
        }
        return out;
    }

    private String prefix() {
        return "rm" + seq.incrementAndGet() + "/";
    }

    // ── reads and the clock ──────────────────────────────────────────────────

    private Set<String> chunkChashes(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            Set<String> out = new LinkedHashSet<>();
            DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.CHASH).from(CHUNKS)
               .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection)))
               .fetch().forEach(r -> out.add(HexFormat.of().formatHex(r.value1())));
            return out;
        }
    }

    /**
     * Moves every chunk of the collection 40 days into the past, its write time and its orphaning record:
     * what time does to a chunk written, and orphaned, long ago.
     */
    private void makeOld(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            OffsetDateTime then = OffsetDateTime.now().minusDays(40);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.update(CHUNKS)
               .set(CHUNKS.CREATED_AT, then).set(CHUNKS.LAST_WRITTEN_AT, then)
               .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection))).execute();
            ctx.update(CHUNK_ORPHANED_AT).set(CHUNK_ORPHANED_AT.ORPHANED_AT, then)
               .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(TENANT).and(CHUNK_ORPHANED_AT.COLLECTION.eq(collection))).execute();
        }
    }

    private Set<String> listed(String collection) {
        Set<String> out = new LinkedHashSet<>();
        for (var c : vectors.reapableChunks(TENANT, collection, null, null, 300, 0)) out.add(c.chash());
        return out;
    }

    private static Set<String> hexes(String p, String... names) {
        Set<String> s = new LinkedHashSet<>();
        for (String n : names) s.add(hex(p + n));
        return s;
    }

    private String quarantineOf(String collection) {
        return "quarantine-" + collection;
    }

    /** Both gc functions, then the listing; returns what each moved/listed. */
    private record Sweep(long movedUnbounded, long movedBounded, Set<String> listed) {}

    private Sweep gcAndList(String collection) {
        long unbounded = vectors.quarantineOrphans(TENANT, collection, quarantineOf(collection),
            "2026-10-01T00:00:00Z", 20).moved();
        long bounded = vectors.quarantineOrphansBounded(TENANT, collection, quarantineOf(collection),
            "2026-10-01T00:00:00Z", 20, 100).moved();
        return new Sweep(unbounded, bounded, listed(collection));
    }

    private String docsCollection(String tag) {
        String c = "docs__" + tag + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(c);
        return c;
    }

    // ── (a) a multi-batch re-index: the old tail survives the pass between batches ──

    @Test
    void anOldTailChunkOfARunInFlightIsNotReapable_betweenBatches_andTheRunCompletes() throws Exception {
        String col = docsCollection("mid");
        String docId = freshDoc("mid", col);
        String p = prefix();
        writeDocument(col, docId, batches(p, new String[][] {{"a1", "a2"}, {"a3", "a4"}, {"a5", "a6"}}), null);
        makeOld(col);
        assertThat(listed(col)).as("precondition: everything is owned, nothing is reapable").isEmpty();
        Set<String> oldTail = hexes(p, "a3", "a4", "a5", "a6");

        // Run 2: a1, a2, a4, a5 unchanged; a3 and a6 replaced by b3 and b6. After each request, the gc pair
        // and the listing run, as the reaper and nx t3 gc would between batches.
        List<Sweep> seen = new ArrayList<>();
        writeDocument(col, docId, batches(p, new String[][] {{"a1", "a2"}, {"b3", "a4"}, {"a5", "b6"}}), k -> {
            if (k < 3) {
                seen.add(gcAndList(col));
                try {
                    assertThat(chunkChashes(col)).as("after request %d the old tail is still in the collection", k)
                        .containsAll(oldTail);
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            }
        });

        assertThat(seen).hasSize(2);
        for (Sweep s : seen) {
            assertThat(s.movedUnbounded()).as("gc_quarantine_orphans between batches").isZero();
            assertThat(s.movedBounded()).as("gc_quarantine_orphans_bounded between batches").isZero();
            assertThat(s.listed()).as("the reapable listing between batches").doesNotContainAnyElementsOf(oldTail);
        }
        // The run completed: the manifest is the new one, a3 and a6 (dropped) were swept, a4 and a5 (re-added) kept.
        assertThat(manifestChashes(docId)).containsExactlyElementsOf(
            List.of(hex(p + "a1"), hex(p + "a2"), hex(p + "b3"), hex(p + "a4"), hex(p + "a5"), hex(p + "b6")));
        assertThat(chunkChashes(col)).contains(hex(p + "a4"), hex(p + "a5")).doesNotContain(hex(p + "a3"), hex(p + "a6"));
        assertThat(listed(col)).as("nothing is left ownerless").isEmpty();
    }

    // ── (b) a deleted file's chunks are not reapable until the grace passes ────

    @Test
    void aDeletedFilesChunksAreNotReapableUntilTheGracePasses() throws Exception {
        String col = docsCollection("del");
        String p = prefix();

        // 1. The client deletes the file: the document is tombstoned and its manifest rows stay, so the
        //    chunks are owned (purge_trash's arm, not reapable) however old they are.
        String tombstoned = freshDoc("del", col);
        writeDocument(col, tombstoned, batches(p, new String[][] {{"t1", "t2", "t3"}}), null);
        makeOld(col);
        repo.deleteDocument(TENANT, tombstoned);
        assertThat(gcAndList(col)).as("tombstoned: the manifest rows still own the chunks")
            .isEqualTo(new Sweep(0, 0, Set.of()));

        // 2. The manifest is purged (CatalogRepository.purgeManifest, the unlink): the chunks lose their last
        //    owner NOW, and the clock starts then, whatever their age.
        String purged = freshDoc("del", col);
        writeDocument(col, purged, batches(p, new String[][] {{"p1", "p2", "p3"}}), null);
        makeOld(col);
        Set<String> purgedChunks = hexes(p, "p1", "p2", "p3");
        assertThat(listed(col)).as("precondition: owned").isEmpty();
        repo.purgeManifest(TENANT, purged);

        Sweep afterPurge = gcAndList(col);
        assertThat(afterPurge.movedUnbounded()).as("purged just now: the clock starts at the purge").isZero();
        assertThat(afterPurge.movedBounded()).isZero();
        assertThat(afterPurge.listed()).isEmpty();
        assertThat(chunkChashes(col)).containsAll(purgedChunks);

        // 3. Thirty days pass.
        makeOld(col);
        assertThat(listed(col)).as("past the grace they are reapable (the tombstoned document's still are not)")
            .containsExactlyInAnyOrderElementsOf(purgedChunks);
        assertThat(vectors.quarantineOrphans(TENANT, col, quarantineOf(col), "2026-10-01T00:00:00Z", 20).moved())
            .isEqualTo(3);
    }

    // ── (c) a floor-refused quarantine chunk is not reapable ───────────────────

    @Test
    void aFloorRefusedQuarantineChunkAgedFortyDaysIsNotReapable() throws Exception {
        String col = docsCollection("qfloor");
        String q = quarantineOf(col);
        List<String> hashes = new ArrayList<>();
        for (int i = 0; i < 10; i++) hashes.add(hex("qfloor-" + col + "-" + i));
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertChunks(DSL.using(su, SQLDialect.POSTGRES), TENANT, col, hashes,
                hashes.stream().map(h -> "text " + h).toList(),
                hashes.stream().map(h -> new float[384]).toList(),
                hashes.stream().map(h -> Map.<String, Object>of()).toList());
        }
        makeOld(col);
        assertThat(vectors.quarantineOrphans(TENANT, col, q, "2026-07-01T00:00:00Z", 20).moved()).isEqualTo(10);

        // The expiry floor refuses to delete 10 of 10 (floor_fraction 0.5, floor_min_chunks 5): nothing is removed.
        var refused = vectors.expireQuarantine(TENANT, q, col, "2026-08-01T00:00:00Z", 0.5, 5, false);
        assertThat(refused.refused()).isEqualTo(10);
        assertThat(chunkChashes(q)).as("the quarantined chunks are still there").hasSize(10);

        makeOld(q);   // forty days pass in quarantine
        assertThat(reapableCount(q)).as("every quarantined chunk has no manifest row and is 40 days old,"
            + " but a quarantine sibling is never reapable: the reaper cannot get past the floor").isZero();
    }

    private long reapableCount(String collection) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.fetchCount(ctx.selectOne().from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(collection)))
            .and(DSL.exists(DSL.selectFrom(CHUNK_IS_REAPABLE.call(
                CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.LAST_WRITTEN_AT,
                DSL.val((org.jooq.types.YearToSecond) null, SQLDataType.INTERVAL)))))));
    }
}
