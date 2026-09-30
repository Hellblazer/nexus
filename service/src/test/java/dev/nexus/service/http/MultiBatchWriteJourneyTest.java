// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.db.Chash;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.IntConsumer;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.LIVE_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-223 P1.6 (bead nexus-z0o2p.7) -- the multi-batch document journeys and the Minimum Viable
 * Validation, driven at the HTTP route level against the real PG substrate.
 *
 * <p>A document larger than one request is written the way the RDR's client protocol writes it
 * (Technical Design 1): batch 1 is {@code POST manifest/write_many} with {@code chunks} and
 * {@code sweep} off, and its {@code dropped_chashes} are what the write dropped from the
 * document's previous manifest; batches 2..N are {@code POST manifest/append} with {@code
 * chunks}; the LAST append carries the dropped list as {@code sweep_chashes}. "The client dies
 * after request k" here means the driver stops sending after request k.
 *
 * <p>Every chunk's text is its own identity ({@code chash = sha256(text)}), the embedder is a
 * deterministic counting fake, and "owner" means a {@code catalog_document_chunks} row of any
 * document in the collection.
 */
class MultiBatchWriteJourneyTest extends AtomicWriteTestBase {

    private static final ObjectMapper JSON = new ObjectMapper();

    private CatalogHandler handler;

    @BeforeAll
    void wireHandler() {   // runs after AtomicWriteTestBase#startAll (superclass lifecycle methods first)
        handler = new CatalogHandler(repo, svc);
    }

    // ── the driver: the RDR's client protocol, over HTTP ─────────────────────────

    private static String hex(String text) {
        return Chash.ofText(text).toHex();
    }

    private static Map<String, Object> chunkOf(String text, int position) {
        Map<String, Object> c = new LinkedHashMap<>();
        c.put("chash", hex(text));
        c.put("text", text);
        c.put("metadata", Map.of("position", position, "label", "meta of " + text));
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

    /** What the client learned from batch 1: the chashes the write dropped from the previous manifest. */
    private record Run(List<String> dropped, Map<String, Object> lastResponse) {}

    /**
     * Writes {@code batches} (lists of chunk texts) as one document, sending requests
     * {@code 1..sendUpTo}; {@code sendUpTo == batches.size()} completes the run, fewer is a client
     * that died after that request. {@code afterRequest} runs after each request (1-based).
     */
    @SuppressWarnings("unchecked")
    private Run writeDocument(String collection, String docId, List<List<String>> batches, int sendUpTo,
                              IntConsumer afterRequest) throws Exception {
        List<String> dropped = List.of();
        Map<String, Object> last = null;
        int position = 0;
        for (int k = 1; k <= Math.min(sendUpTo, batches.size()); k++) {
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
                last = send("/v1/catalog/manifest/write_many", body);
                dropped = ((Map<String, List<String>>) last.get("dropped_chashes")).get(docId);
            } else {
                body.put("doc_id", docId);
                body.put("rows", rows);
                if (k == batches.size() && !dropped.isEmpty()) {
                    body.put("sweep_chashes", dropped);       // the deferred sweep rides the last append
                }
                last = send("/v1/catalog/manifest/append", body);
            }
            if (afterRequest != null) afterRequest.accept(k);
        }
        return new Run(dropped, last);
    }

    private Run writeDocument(String collection, String docId, List<List<String>> batches) throws Exception {
        return writeDocument(collection, docId, batches, batches.size(), null);
    }

    // ── reads ───────────────────────────────────────────────────────────────────

    private <T> T reading(java.util.function.Function<org.jooq.DSLContext, T> f) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return f.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    private Set<String> chunkChashes(String collection) throws Exception {
        return reading(ctx -> {
            Set<String> out = new LinkedHashSet<>();
            ctx.select(CHUNKS.CHASH).from(CHUNKS)
               .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(collection))
               .fetch().forEach(r -> out.add(HexFormat.of().formatHex(r.value1())));
            return out;
        });
    }

    /** Chunks in {@code collection} that no document's manifest references. */
    private Set<String> ownerless(String collection) throws Exception {
        return reading(ctx -> {
            Set<String> out = new LinkedHashSet<>();
            ctx.select(CHUNKS.CHASH).from(CHUNKS)
               .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(collection))
               .andNotExists(ctx.selectOne().from(CATALOG_DOCUMENT_CHUNKS)
                   .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(CHUNKS.TENANT_ID))
                   .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(CHUNKS.CHASH)))
               .fetch().forEach(r -> out.add(HexFormat.of().formatHex(r.value1())));
            return out;
        });
    }

    /** The chunks the search-visibility predicate (live(c)) shows for {@code collection}. */
    private Set<String> liveChunks(String collection) throws Exception {
        return reading(ctx -> {
            Set<String> out = new LinkedHashSet<>();
            ctx.select(LIVE_CHUNKS.CHASH).from(LIVE_CHUNKS)
               .where(LIVE_CHUNKS.TENANT_ID.eq(TENANT)).and(LIVE_CHUNKS.COLLECTION.eq(collection))
               .fetch().forEach(r -> out.add(HexFormat.of().formatHex(r.value1())));
            return out;
        });
    }

    private record Stored(String text, String metadata, List<Float> vector) {}

    private Map<String, Stored> chunkContents(String collection) throws Exception {
        return reading(ctx -> {
            Map<String, Stored> out = new LinkedHashMap<>();
            ctx.select(CHUNKS.CHASH, CHUNKS.CHUNK_TEXT, CHUNKS.METADATA, CHUNKS.EMBEDDING_384).from(CHUNKS)
               .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(collection))
               .fetch().forEach(r -> {
                   List<Float> v = new ArrayList<>();
                   for (float x : r.value4().floats()) v.add(x);
                   out.put(HexFormat.of().formatHex(r.value1()),
                           new Stored(r.value2(), r.value3().data(), v));
               });
            return out;
        });
    }

    private List<String> manifest(String docId) {
        List<String> out = new ArrayList<>();
        for (var r : repo.getManifest(TENANT, docId)) {
            out.add(r.get("position") + ":" + r.get("chash") + ":" + r.get("chunk_index"));
        }
        return out;
    }

    /**
     * A chunk's text is {@code prefix + name}: the prefix is per test because the sweep's
     * shared-chash guard reads manifests tenant-wide, across collections, so two tests that
     * wrote the same text would keep each other's "dropped" chunks alive.
     */
    private String prefix() {
        return "j" + seq.incrementAndGet() + "/";
    }

    private static Set<String> hexes(String p, String... names) {
        Set<String> s = new LinkedHashSet<>();
        for (String n : names) s.add(hex(p + n));
        return s;
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

    // Run 1 of the document the journeys re-index: six chunks in three batches.
    private static List<List<String>> run1(String p) {
        return batches(p, new String[][] {{"a1", "a2"}, {"a3", "a4"}, {"a5", "a6"}});
    }

    // Run 2: a1, a2, a4, a5 unchanged; a3 and a6 replaced by b3 and b6.
    private static List<List<String>> run2(String p) {
        return batches(p, new String[][] {{"a1", "a2"}, {"b3", "a4"}, {"a5", "b6"}});
    }

    // ── Minimum Viable Validation / Test Plan 1 ─────────────────────────────────

    @Test
    void mvv_clientDiesBetweenTwoAppends_noChunkThisRunWroteIsWithoutAnOwner_andNothingWasSwept() throws Exception {
        Fx f = fixture("mvv");
        String p = prefix();
        // Four batches so the death falls BETWEEN two appends (after append 2 of 3).
        List<List<String>> run1 = batches(p, new String[][] {{"m1"}, {"m2"}, {"m3"}, {"m4"}});
        List<List<String>> run2 = batches(p, new String[][] {{"m1"}, {"n2"}, {"n3"}, {"m4"}});
        writeDocument(f.collection(), f.docId(), run1);
        assertThat(ownerless(f.collection())).isEmpty();
        Set<String> beforeRun2 = chunkChashes(f.collection());
        int embedsBefore = embedder.calls.get();

        // Run 2 dies after request 3 (write_many + append 1 + append 2), before append 3.
        Run r = writeDocument(f.collection(), f.docId(), run2, 3, null);

        Set<String> wroteThisRun = hexes(p, "n2", "n3");
        assertThat(embedder.calls.get() - embedsBefore).as("only n2 and n3 are new").isEqualTo(2);
        assertThat(chunkChashes(f.collection())).containsAll(wroteThisRun);
        assertThat(ownerless(f.collection()))
            .as("no chunk run 2 wrote is ownerless; the ownerless ones are run 1's m2, m3 and m4, which"
                + " run 2 replaced or has not re-added yet")
            .doesNotContainAnyElementsOf(wroteThisRun)
            .containsExactlyInAnyOrderElementsOf(hexes(p, "m2", "m3", "m4"));
        assertThat(chunkChashes(f.collection()))
            .as("no sweep ran: everything that existed before run 2 still exists")
            .containsAll(beforeRun2);
        assertThat(r.dropped()).containsExactlyInAnyOrderElementsOf(hexes(p, "m2", "m3", "m4"));
    }

    // ── Test Plan 3: the completed run equals one combined write of the whole document ──

    @Test
    void aCompletedRun_leavesTheManifestAndChunksOfOneCombinedWriteOfTheWholeDocument() throws Exception {
        Fx f = fixture("eq");
        Fx control = fixture("eqc");
        String p = prefix();
        writeDocument(f.collection(), f.docId(), run1(p));
        writeDocument(f.collection(), f.docId(), run2(p));

        // The control: run 2's whole document in ONE write_many, in a fresh collection.
        List<Map<String, Object>> chunks = new ArrayList<>();
        List<Map<String, Object>> rows = new ArrayList<>();
        int position = 0;
        for (List<String> batch : run2(p)) {
            for (String t : batch) {
                chunks.add(chunkOf(t, position));
                rows.add(rowOf(t, position));
                position++;
            }
        }
        send("/v1/catalog/manifest/write_many", Map.of("collection", control.collection(),
            "sweep", true, "chunks", chunks, "docs", List.of(Map.of("doc_id", control.docId(), "rows", rows))));

        assertThat(manifest(f.docId()))
            .as("same positions, same chashes, same chunk_index")
            .isEqualTo(manifest(control.docId()));
        Map<String, Stored> actual = chunkContents(f.collection());
        Map<String, Stored> expected = chunkContents(control.collection());
        assertThat(actual.keySet()).as("the swept previous-run chunks (a3, a6) are gone: same chunk set")
            .isEqualTo(expected.keySet());
        assertThat(actual).as("same text, metadata and vector for every chunk").isEqualTo(expected);
        assertThat(ownerless(f.collection())).isEmpty();
    }

    // ── Test Plan 4a: re-index of an unchanged document ─────────────────────────

    @Test
    void anUnchangedReindex_sweepsNothing_andCallsTheEmbedderZeroTimes() throws Exception {
        Fx f = fixture("same");
        String p = prefix();
        writeDocument(f.collection(), f.docId(), run1(p));
        Set<String> before = chunkChashes(f.collection());
        List<String> manifestBefore = manifest(f.docId());
        int embedsBefore = embedder.calls.get();

        List<Set<String>> midRun = new ArrayList<>();
        Run r = writeDocument(f.collection(), f.docId(), run1(p), run1(p).size(),
            k -> { try { midRun.add(chunkChashes(f.collection())); } catch (Exception e) { throw new RuntimeException(e); } });

        assertThat(embedder.calls.get() - embedsBefore).as("nothing changed: zero embedder calls").isZero();
        assertThat(r.dropped())
            .as("batch 1 replaced the manifest with two rows, so the other four are reported dropped")
            .containsExactlyInAnyOrderElementsOf(hexes(p, "a3", "a4", "a5", "a6"));
        for (Set<String> seen : midRun) {
            assertThat(seen).as("no chunk is swept between batches").isEqualTo(before);
        }
        assertThat(r.lastResponse()).containsEntry("swept", 0).containsEntry("sweep_skipped", 0);
        assertThat(chunkChashes(f.collection())).as("and none after the last append").isEqualTo(before);
        assertThat(manifest(f.docId())).isEqualTo(manifestBefore);
        assertThat(ownerless(f.collection())).isEmpty();
    }

    // ── Test Plan 4b: re-index of a partially changed document ───────────────────

    @Test
    void aPartiallyChangedReindex_sweepsOnlyWhatTheNewVersionDroppedAndNobodyElseOwns() throws Exception {
        Fx f = fixture("part");
        String p = prefix();
        writeDocument(f.collection(), f.docId(), run1(p));
        // Another document shares a3, so it must survive the deferred sweep.
        String other = freshDoc("part-other", f.collection());
        send("/v1/catalog/manifest/append", Map.of("doc_id", other, "collection", f.collection(),
            "rows", List.of(rowOf(p + "a3", 0)), "chunks", List.of(chunkOf(p + "a3", 0))));
        Set<String> previousRun = hexes(p, "a1", "a2", "a3", "a4", "a5", "a6");
        int embedsBefore = embedder.calls.get();

        List<Set<String>> midRun = new ArrayList<>();
        Run r = writeDocument(f.collection(), f.docId(), run2(p), 3, k -> {
            try {
                if (k < 3) midRun.add(chunkChashes(f.collection()));
            } catch (Exception e) { throw new RuntimeException(e); }
        });

        assertThat(embedder.calls.get() - embedsBefore).as("only b3 and b6 are new").isEqualTo(2);
        assertThat(r.dropped()).containsExactlyInAnyOrderElementsOf(hexes(p, "a3", "a4", "a5", "a6"));
        assertThat(midRun).hasSize(2);
        for (Set<String> seen : midRun) {
            assertThat(seen)
                .as("between batches no chunk of the previous run has been swept, re-added ones included")
                .containsAll(previousRun);
        }
        Set<String> after = chunkChashes(f.collection());
        assertThat(after).as("a6: dropped and unowned, swept").doesNotContain(hex(p + "a6"));
        assertThat(after).as("a4, a5: dropped by batch 1 but re-added by a later batch, survive")
            .contains(hex(p + "a4"), hex(p + "a5"));
        assertThat(after).as("a3: dropped but another document owns it, survives").contains(hex(p + "a3"));
        assertThat(after).as("the new version's chunks")
            .contains(hex(p + "b3"), hex(p + "b6"), hex(p + "a1"), hex(p + "a2"));
        assertThat(r.lastResponse()).containsEntry("swept", 1).containsEntry("sweep_skipped", 0);
        assertThat(manifest(f.docId()).stream().map(m -> m.split(":")[1]).toList())
            .containsExactly(hex(p + "a1"), hex(p + "a2"), hex(p + "b3"), hex(p + "a4"), hex(p + "a5"), hex(p + "b6"));
        assertThat(ownerless(f.collection())).isEmpty();
    }

    // ── Test Plan 5: client dies before the last append ─────────────────────────

    @Test
    void whenTheClientDiesBeforeTheLastAppend_theDeferredSweepNeverRuns_andSearchHidesTheLeftovers() throws Exception {
        Fx f = fixture("die");
        String p = prefix();
        writeDocument(f.collection(), f.docId(), run1(p));
        int embedsBefore = embedder.calls.get();

        // Run 2 sends write_many and append 1, and dies before append 2, the LAST one.
        Run r = writeDocument(f.collection(), f.docId(), run2(p), 2, null);

        assertThat(embedder.calls.get() - embedsBefore).as("b3 was embedded; b6 never sent").isEqualTo(1);
        assertThat(chunkChashes(f.collection()))
            .as("the deferred sweep did not run: every previous-run chunk still exists")
            .containsAll(hexes(p, "a1", "a2", "a3", "a4", "a5", "a6"));
        assertThat(ownerless(f.collection()))
            .as("no chunk run 2 wrote is ownerless; the ownerless ones are run 1's a3 (replaced by b3),"
                + " a5 and a6 (not yet re-added), which the deferred sweep would have removed")
            .doesNotContainAnyElementsOf(hexes(p, "b3"))
            .containsExactlyInAnyOrderElementsOf(hexes(p, "a3", "a5", "a6"));
        assertThat(liveChunks(f.collection()))
            .as("live(c) hides the ownerless leftovers from search and shows the document's own chunks")
            .doesNotContain(hex(p + "a3"), hex(p + "a5"), hex(p + "a6"))
            .contains(hex(p + "a1"), hex(p + "a2"), hex(p + "b3"), hex(p + "a4"));
        assertThat(r.dropped()).containsExactlyInAnyOrderElementsOf(hexes(p, "a3", "a4", "a5", "a6"));
    }
}
