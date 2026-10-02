// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.vectors.SuppliedVectorMismatchActivity;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-223 P1.5 (bead nexus-z0o2p.6) -- client-supplied vectors on the three combined
 * routes ({@code write_many}, {@code append}, {@code append_many}), needed by
 * {@code .nxexp} import, whose gate (gate-xr789) needs the stored vectors byte-identical to the
 * exported ones (RDR-223 F-6).
 *
 * <p>Wire shape (chosen here; the RDR names neither): a chunk may carry {@code embedding}
 * (an array of numbers), and the request then carries a top-level {@code embedding_model}
 * naming the model that produced the vectors. {@code /v1/vectors/upsert-chunks} takes a parallel
 * top-level {@code embeddings} array instead; a per-chunk field cannot drift out of alignment
 * when chunks are deduplicated or filtered.
 *
 * <p>The four cells of Technical Design 2 (R-14) are pinned per cell below, on each of the three
 * routes where the cell differs by route (the wrong-dimension and wrong-model refusals).
 */
class SuppliedVectorsTest extends AtomicWriteTestBase {

    private static final String MODEL = "minilm-l6-v2-384";

    /** A deterministic, non-trivial 384-dim vector: distinct per seed, no exact-zero padding. */
    private static float[] vec(int seed) {
        float[] v = new float[384];
        for (int i = 0; i < v.length; i++) {
            v[i] = (float) (Math.sin(seed * 31.0 + i * 0.37) * 0.25 + (seed % 7) * 0.001);
        }
        return v;
    }

    private static Map<String, Object> vchunk(String chash, String text, float[] embedding) {
        Map<String, Object> c = new LinkedHashMap<>();
        c.put("chash", chash);
        c.put("text", text);
        c.put("metadata", Map.of());
        c.put("embedding", embedding);
        return c;
    }

    private float[] storedVector(String collection, String hexChash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var v = DSL.using(su, SQLDialect.POSTGRES)
                .select(CHUNKS.EMBEDDING_384).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT))
                .and(CHUNKS.COLLECTION.eq(collection))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hexChash)))
                .fetchOne(CHUNKS.EMBEDDING_384);
            return v == null ? null : v.floats();
        }
    }

    private String storedMetadata(String collection, String hexChash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var m = DSL.using(su, SQLDialect.POSTGRES)
                .select(CHUNKS.METADATA).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT))
                .and(CHUNKS.COLLECTION.eq(collection))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hexChash)))
                .fetchOne(CHUNKS.METADATA);
            return m == null ? null : m.data();
        }
    }

    /** One route through the engine: writes {@code rows} to {@code docId} with {@code chunks}. */
    @FunctionalInterface
    interface Route {
        Map<String, Object> write(Fx f, List<Map<String, Object>> rows, List<Map<String, Object>> chunks,
                                  String embeddingModel);
    }

    private Route writeManyRoute() {
        return (f, rows, chunks, model) -> svc.writeManyCombined(TENANT, f.collection(), chunks,
            List.of(doc(f.docId(), rows)), null, false, false, model).response();
    }

    private Route appendRoute() {
        return (f, rows, chunks, model) -> svc.appendCombined(TENANT, f.collection(), f.docId(),
            rows, chunks, false, null, model).response();
    }

    private Route appendManyRoute() {
        return (f, rows, chunks, model) -> svc.appendManyCombined(TENANT, f.collection(),
            List.of(doc(f.docId(), rows)), chunks, false, model).response();
    }

    private Map<String, Route> routes() {
        Map<String, Route> m = new LinkedHashMap<>();
        m.put("write_many", writeManyRoute());
        m.put("append", appendRoute());
        m.put("append_many", appendManyRoute());
        return m;
    }

    // ── cell: new chash, no vector -> embedded as today ─────────────────────────

    @Test
    void newChash_noVector_isEmbedded_onEveryRoute() throws Exception {
        for (var e : routes().entrySet()) {
            Fx f = fixture("c1");
            String c = ch("c1-" + e.getKey());
            int before = embedder.calls.get();

            var response = e.getValue().write(f, List.of(row(0, c)), List.of(chunk(c, "c1 text " + e.getKey())), null);

            assertThat(embedder.calls.get() - before).as(e.getKey()).isEqualTo(1);
            assertThat(response).as(e.getKey()).containsEntry("embed_embedded", 1).containsEntry("vectors_supplied", 0);
            assertThat(storedVector(f.collection(), c)).as(e.getKey()).isNotNull();
        }
    }

    // ── cell: new chash, vector -> stored as-is, no embedder call ───────────────

    @Test
    void newChash_withVector_isStoredByteIdentical_withZeroEmbedderCalls_onEveryRoute() throws Exception {
        int n = 0;
        for (var e : routes().entrySet()) {
            Fx f = fixture("c2");
            String a = ch("c2-a-" + e.getKey()), b = ch("c2-b-" + e.getKey());
            float[] va = vec(++n), vb = vec(++n);
            int before = embedder.calls.get();

            var response = e.getValue().write(f, List.of(row(0, a), row(1, b)),
                List.of(vchunk(a, "c2 a", va), vchunk(b, "c2 b", vb)), MODEL);

            assertThat(embedder.calls.get() - before)
                .as("%s: a request whose new chunks all carry vectors makes zero embedder calls", e.getKey())
                .isZero();
            assertThat(response).as(e.getKey())
                .containsEntry("embed_embedded", 0).containsEntry("vectors_supplied", 2)
                .containsEntry("chunks_written", 2).containsEntry("vector_mismatches", 0);
            assertThat(storedVector(f.collection(), a)).as("%s: byte-identical", e.getKey()).containsExactly(va);
            assertThat(storedVector(f.collection(), b)).as("%s: byte-identical", e.getKey()).containsExactly(vb);
            assertThat(manifestChashes(f.docId())).as(e.getKey()).containsExactlyInAnyOrder(a, b);
        }
    }

    @Test
    void aMixedRequest_embedsOnlyTheChunksWithoutVectors() throws Exception {
        Fx f = fixture("mix");
        String withV = ch("mix-v"), without = ch("mix-w");
        float[] v = vec(101);
        int before = embedder.calls.get();

        var response = svc.writeManyCombined(TENANT, f.collection(),
            List.of(vchunk(withV, "mix v", v), chunk(without, "mix w")),
            List.of(doc(f.docId(), List.of(row(0, withV), row(1, without)))),
            null, false, false, MODEL).response();

        assertThat(embedder.calls.get() - before).isEqualTo(1);
        assertThat(response).containsEntry("embed_embedded", 1).containsEntry("vectors_supplied", 1)
            .containsEntry("embed_skipped", 0).containsEntry("chunks_deduped", 2);
        assertThat(storedVector(f.collection(), withV)).containsExactly(v);
    }

    // ── cell: existing chash, no vector -> not re-embedded, metadata refreshed ──

    @Test
    void existingChash_noVector_isNotReEmbedded_andItsMetadataIsRefreshed() throws Exception {
        Fx f = fixture("c3");
        String c = ch("c3-c");
        float[] original = vec(7);
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(vchunk(c, "c3 text", original)),
            List.of(doc(f.docId(), List.of(row(0, c)))), null, false, false, MODEL);
        int before = embedder.calls.get();

        var response = svc.appendCombined(TENANT, f.collection(), f.docId(),
            List.of(row(1, c)),
            List.of(Map.of("chash", c, "text", "c3 text", "metadata", Map.of("section", "refreshed"))),
            false, null, null).response();

        assertThat(embedder.calls.get() - before).as("identical text: not re-embedded").isZero();
        assertThat(response).containsEntry("embed_skipped", 1).containsEntry("embed_embedded", 0);
        assertThat(storedVector(f.collection(), c)).as("the vector is untouched").containsExactly(original);
        assertThat(storedMetadata(f.collection(), c)).contains("refreshed");
    }

    // ── cell: existing chash, vector -> stored kept; a different one is counted, not written ──

    @Test
    void existingChash_withADifferentVector_keepsTheStoredVector_andCountsTheMismatch() throws Exception {
        Fx f = fixture("c4");
        String c = ch("c4-c");
        float[] stored = vec(11), different = vec(12);
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(vchunk(c, "c4 text", stored)),
            List.of(doc(f.docId(), List.of(row(0, c)))), null, false, false, MODEL);
        long before = SuppliedVectorMismatchActivity.total();

        var response = svc.appendCombined(TENANT, f.collection(), f.docId(),
            List.of(row(1, c)), List.of(vchunk(c, "c4 text", different)), false, null, MODEL).response();

        assertThat(storedVector(f.collection(), c)).as("the stored vector is kept").containsExactly(stored);
        assertThat(response).containsEntry("vector_mismatches", 1).containsEntry("vectors_supplied", 0)
            .containsEntry("chunks_written", 0);
        assertThat(SuppliedVectorMismatchActivity.total() - before).isEqualTo(1L);
    }

    @Test
    void existingChash_withTheSameVector_isNoMismatch() throws Exception {
        Fx f = fixture("c4b");
        String c = ch("c4b-c");
        float[] v = vec(13);
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(vchunk(c, "c4b text", v)),
            List.of(doc(f.docId(), List.of(row(0, c)))), null, false, false, MODEL);
        long before = SuppliedVectorMismatchActivity.total();

        var response = svc.appendCombined(TENANT, f.collection(), f.docId(),
            List.of(row(1, c)), List.of(vchunk(c, "c4b text", v)), false, null, MODEL).response();

        assertThat(response).containsEntry("vector_mismatches", 0);
        assertThat(SuppliedVectorMismatchActivity.total() - before).isZero();
    }

    // ── Test Plan 10: wrong dimension / wrong model refused, nothing stored ─────

    private static Map<String, Object> chunkMeta(String chash, String text, String value) {
        return Map.of("chash", chash, "text", text, "metadata", Map.of("m", value));
    }

    /** Stores {@code chash} (metadata m=original) through a document of its own, in {@code f}'s collection. */
    private void seedExisting(Fx f, String chash, String text) {
        String holder = freshDoc("seed", f.collection());
        svc.writeManyCombined(TENANT, f.collection(), List.of(chunkMeta(chash, text, "original")),
            List.of(doc(holder, List.of(row(0, chash)))), null, false, false);
    }

    @Test
    void aWrongDimension_isRefused_namingBothValues_andNothingIsStored_onEveryRoute() throws Exception {
        for (var e : routes().entrySet()) {
            Fx f = fixture("dim");
            String good = ch("dim-good-" + e.getKey()), bad = ch("dim-bad-" + e.getKey());
            String existing = ch("dim-existing-" + e.getKey());
            seedExisting(f, existing, "existing text");
            long chunksBefore = chunkCount(f.collection());

            // The refused request also carries an EXISTING chash with CHANGED metadata: the
            // refusal must come before the existence partition's metadata refresh (phase 2a).
            assertThatThrownBy(() -> e.getValue().write(f, List.of(row(0, good), row(1, bad), row(2, existing)),
                    List.of(vchunk(good, "good", vec(1)), vchunk(bad, "bad", new float[383]),
                            chunkMeta(existing, "existing text", "changed")), MODEL))
                .as(e.getKey())
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("383").hasMessageContaining("384").hasMessageContaining("chunks[1]");

            assertThat(chunkCount(f.collection())).as("%s: nothing stored", e.getKey()).isEqualTo(chunksBefore);
            assertThat(repo.getManifest(TENANT, f.docId())).as(e.getKey()).isEmpty();
            assertThat(storedMetadata(f.collection(), existing))
                .as("%s: the existing chunk's metadata was not refreshed by a refused request", e.getKey())
                .contains("original").doesNotContain("changed");
        }
    }

    @Test
    void aWrongModel_isRefused_namingBothValues_andNothingIsStored_onEveryRoute() throws Exception {
        for (var e : routes().entrySet()) {
            Fx f = fixture("mdl");
            String c = ch("mdl-" + e.getKey());
            String existing = ch("mdl-existing-" + e.getKey());
            seedExisting(f, existing, "existing text");
            long chunksBefore = chunkCount(f.collection());

            assertThatThrownBy(() -> e.getValue().write(f, List.of(row(0, c), row(1, existing)),
                    List.of(vchunk(c, "c", vec(2)), chunkMeta(existing, "existing text", "changed")),
                    "voyage-context-3"))
                .as(e.getKey())
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("voyage-context-3").hasMessageContaining(MODEL);

            assertThat(chunkCount(f.collection())).as("%s: nothing stored", e.getKey()).isEqualTo(chunksBefore);
            assertThat(storedMetadata(f.collection(), existing))
                .as("%s: refused before the metadata refresh", e.getKey())
                .contains("original").doesNotContain("changed");
        }
    }

    // ── R-14 cell 4 in full: a supplied vector never overwrites a stored one unless forced ──

    @Test
    void withForceReEmbed_theSuppliedVectorIsWritten_andTheDifferingStoredOneIsCounted() throws Exception {
        Fx f = fixture("force");
        String c = ch("force-c");
        float[] stored = vec(21), supplied = vec(22);
        svc.writeManyCombined(TENANT, f.collection(), List.of(vchunk(c, "force text", stored)),
            List.of(doc(f.docId(), List.of(row(0, c)))), null, false, false, MODEL);
        long before = SuppliedVectorMismatchActivity.total();

        var response = svc.writeManyCombined(TENANT, f.collection(), List.of(vchunk(c, "force text", supplied)),
            List.of(doc(freshDoc("force2", f.collection()), List.of(row(0, c)))), null, false, true, MODEL).response();

        assertThat(storedVector(f.collection(), c)).as("explicit intent: written").containsExactly(supplied);
        assertThat(response).containsEntry("vectors_supplied", 1).containsEntry("vector_mismatches", 1);
        assertThat(SuppliedVectorMismatchActivity.total() - before).isEqualTo(1L);
    }

    @Test
    void withoutForce_anExistingChashKeepsItsStoredVector_evenWhenTheSuppliedTextDiffers() throws Exception {
        Fx f = fixture("keep");
        String c = ch("keep-c");
        float[] stored = vec(31), supplied = vec(32);
        svc.writeManyCombined(TENANT, f.collection(), List.of(vchunk(c, "original text", stored)),
            List.of(doc(f.docId(), List.of(row(0, c)))), null, false, false, MODEL);
        long before = SuppliedVectorMismatchActivity.total();

        var response = svc.writeManyCombined(TENANT, f.collection(),
            List.of(vchunk(c, "DIFFERENT text", supplied)),
            List.of(doc(freshDoc("keep2", f.collection()), List.of(row(0, c)))), null, false, false, MODEL).response();

        assertThat(storedVector(f.collection(), c)).containsExactly(stored);
        assertThat(chunkText(f.collection(), c)).as("text kept with the vector").isEqualTo("original text");
        assertThat(response).containsEntry("vectors_supplied", 0).containsEntry("vector_mismatches", 1)
            .containsEntry("embed_embedded", 0);
        assertThat(SuppliedVectorMismatchActivity.total() - before).isEqualTo(1L);
    }

    @Test
    void aWriterThatCommitsTheChashBetweenTheExistenceCheckAndTheInsert_isNotOverwrittenBySuppliedVectors() throws Exception {
        for (var e : routes().entrySet()) {
            Fx f = fixture("race");
            String c = ch("race-c-" + e.getKey());
            String text = "race text " + e.getKey();
            String otherDoc = freshDoc("race-other", f.collection());
            float[] supplied = vec(41);
            float[] winnerVector = new CountingFakeEmbedder().embed(List.of(text)).get(0);
            assertThat(winnerVector).as("sanity: the winner's vector differs from the supplied one")
                .isNotEqualTo(supplied);

            var slow = new dev.nexus.service.db.CombinedWriteService(tenantScope, repo,
                new dev.nexus.service.vectors.EmbedderRouter(embedder, "document"));
            var winner = new dev.nexus.service.db.CombinedWriteService(tenantScope, repo,
                new dev.nexus.service.vectors.EmbedderRouter(embedder, "document"));
            // After slow's existence partition found c ABSENT, the winner commits it (server-embedded).
            slow.setAfterNeedEmbedResolvedHookForTests(() ->
                winner.writeManyCombined(TENANT, f.collection(), List.of(chunk(c, text)),
                    List.of(doc(otherDoc, List.of(row(0, c)))), null, false, false));

            switch (e.getKey()) {
                case "write_many" -> slow.writeManyCombined(TENANT, f.collection(),
                    List.of(Map.of("chash", c, "text", text, "metadata", Map.of("m", "racer"), "embedding", supplied)),
                    List.of(doc(f.docId(), List.of(row(0, c)))), null, false, false, MODEL);
                case "append" -> slow.appendCombined(TENANT, f.collection(), f.docId(), List.of(row(0, c)),
                    List.of(Map.of("chash", c, "text", text, "metadata", Map.of("m", "racer"), "embedding", supplied)),
                    false, null, MODEL);
                default -> slow.appendManyCombined(TENANT, f.collection(),
                    List.of(doc(f.docId(), List.of(row(0, c)))),
                    List.of(Map.of("chash", c, "text", text, "metadata", Map.of("m", "racer"), "embedding", supplied)),
                    false, MODEL);
            }

            assertThat(storedVector(f.collection(), c))
                .as("%s: the racing writer's stored vector survives the ON CONFLICT", e.getKey())
                .containsExactly(winnerVector);
            assertThat(storedMetadata(f.collection(), c)).as("%s: metadata still refreshed", e.getKey()).contains("racer");
            assertThat(manifestChashes(f.docId())).as(e.getKey()).containsExactly(c);
        }
    }

    @Test
    void aMixedRequestUnderARace_keepsTheWinnersVectorForTheSuppliedChashOnly() throws Exception {
        for (var e : routes().entrySet()) {
            Fx f = fixture("mixrace");
            String withV = ch("mixrace-v-" + e.getKey()), without = ch("mixrace-w-" + e.getKey());
            String textV = "mixrace v " + e.getKey(), textW = "mixrace w " + e.getKey();
            String otherDoc = freshDoc("mixrace-other", f.collection());
            float[] supplied = vec(61);
            float[] winnerV = new CountingFakeEmbedder().embed(List.of(textV)).get(0);

            var slow = new dev.nexus.service.db.CombinedWriteService(tenantScope, repo,
                new dev.nexus.service.vectors.EmbedderRouter(embedder, "document"));
            var winner = new dev.nexus.service.db.CombinedWriteService(tenantScope, repo,
                new dev.nexus.service.vectors.EmbedderRouter(embedder, "document"));
            slow.setAfterNeedEmbedResolvedHookForTests(() ->
                winner.writeManyCombined(TENANT, f.collection(),
                    List.of(chunk(withV, textV), chunk(without, textW)),
                    List.of(doc(otherDoc, List.of(row(0, withV), row(1, without)))), null, false, false));

            List<Map<String, Object>> chunks = List.of(
                vchunk(withV, textV, supplied), chunk(without, textW));
            List<Map<String, Object>> rows = List.of(row(0, withV), row(1, without));
            switch (e.getKey()) {
                case "write_many" -> slow.writeManyCombined(TENANT, f.collection(), chunks,
                    List.of(doc(f.docId(), rows)), null, false, false, MODEL);
                case "append" -> slow.appendCombined(TENANT, f.collection(), f.docId(), rows, chunks,
                    false, null, MODEL);
                default -> slow.appendManyCombined(TENANT, f.collection(),
                    List.of(doc(f.docId(), rows)), chunks, false, MODEL);
            }

            assertThat(storedVector(f.collection(), withV))
                .as("%s: the supplied chash keeps the racing writer's vector", e.getKey())
                .containsExactly(winnerV);
            assertThat(chunkExists(f.collection(), without)).as(e.getKey()).isTrue();
            assertThat(manifestChashes(f.docId())).as(e.getKey()).containsExactlyInAnyOrder(withV, without);
        }
    }

    /**
     * Two writers send the SAME brand-new chashes classified opposite ways: writer A supplies a
     * vector for X (keep-stored) and embeds Y (overwrite); writer B does the reverse. Split into an
     * overwrite statement and a keep statement, A would insert Y then X and B X then Y and each
     * would wait on the other's uncommitted insert (40P01). One insert in one chash order cannot.
     * A barrier after the existence partition starts both per-document transactions together.
     */
    @Test
    void twoWritersClassifyingTheSameNewChashesOppositeWays_doNotDeadlock() throws Exception {
        Fx f = fixture("dead");
        String docA = f.docId(), docB = freshDoc("dead-b", f.collection());
        var writerA = new dev.nexus.service.db.CombinedWriteService(tenantScope, repo,
            new dev.nexus.service.vectors.EmbedderRouter(embedder, "document"));
        var writerB = new dev.nexus.service.db.CombinedWriteService(tenantScope, repo,
            new dev.nexus.service.vectors.EmbedderRouter(embedder, "document"));
        java.util.concurrent.ExecutorService pool = java.util.concurrent.Executors.newFixedThreadPool(2);
        try {
            for (int i = 0; i < 60; i++) {
                String x = ch("dead-x-" + i), y = ch("dead-y-" + i);
                var barrier = new java.util.concurrent.CyclicBarrier(2);
                Runnable await = () -> {
                    try {
                        barrier.await(20, java.util.concurrent.TimeUnit.SECONDS);
                    } catch (Exception ex) {
                        throw new RuntimeException(ex);
                    }
                };
                writerA.setAfterNeedEmbedResolvedHookForTests(await);
                writerB.setAfterNeedEmbedResolvedHookForTests(await);
                int n = i;
                var fa = pool.submit(() -> writerA.writeManyCombined(TENANT, f.collection(),
                    List.of(vchunk(x, "dead x " + n, vec(70 + n)), chunk(y, "dead y " + n)),
                    List.of(doc(docA, List.of(row(2 * n, x), row(2 * n + 1, y)))), null, false, false, MODEL));
                var fb = pool.submit(() -> writerB.writeManyCombined(TENANT, f.collection(),
                    List.of(vchunk(y, "dead y " + n, vec(170 + n)), chunk(x, "dead x " + n)),
                    List.of(doc(docB, List.of(row(2 * n, y), row(2 * n + 1, x)))), null, false, false, MODEL));
                var ra = fa.get(60, java.util.concurrent.TimeUnit.SECONDS).response();
                var rb = fb.get(60, java.util.concurrent.TimeUnit.SECONDS).response();
                assertThat(ra.get("failed_doc_ids")).as("iteration %d: writer A's document committed", i).isEqualTo(List.of());
                assertThat(rb.get("failed_doc_ids")).as("iteration %d: writer B's document committed (a"
                    + " deadlock victim lands here as a failed document)", i).isEqualTo(List.of());
                assertThat(chunkExists(f.collection(), x)).isTrue();
                assertThat(chunkExists(f.collection(), y)).isTrue();
            }
        } finally {
            writerA.setAfterNeedEmbedResolvedHookForTests(null);
            writerB.setAfterNeedEmbedResolvedHookForTests(null);
            pool.shutdownNow();
        }
    }

    @Test
    void aMismatchIsCountedOnlyOnceTheWriteHasCommitted() throws Exception {
        Fx f = fixture("commit");
        String c = ch("commit-c");
        float[] stored = vec(51), different = vec(52);
        svc.writeManyCombined(TENANT, f.collection(), List.of(vchunk(c, "commit text", stored)),
            List.of(doc(freshDoc("commit-holder", f.collection()), List.of(row(0, c)))), null, false, false, MODEL);
        assertThat(repo.deleteDocument(TENANT, f.docId())).isEqualTo(1);
        long before = SuppliedVectorMismatchActivity.total();

        // The append fails (tombstoned document) after the mismatch was detected in the partition.
        assertThatThrownBy(() -> svc.appendCombined(TENANT, f.collection(), f.docId(), List.of(row(0, c)),
                List.of(vchunk(c, "commit text", different)), false, null, MODEL))
            .isInstanceOf(dev.nexus.service.db.CatalogRepository.TombstonedDocumentException.class);
        assertThat(SuppliedVectorMismatchActivity.total() - before)
            .as("a failed write that the client will retry must not be counted").isZero();

        // Positive control: the SAME differing vector, appended to a live document, is counted once.
        var ok = svc.appendCombined(TENANT, f.collection(), freshDoc("commit-live", f.collection()),
            List.of(row(0, c)), List.of(vchunk(c, "commit text", different)), false, null, MODEL).response();
        assertThat(ok).containsEntry("vector_mismatches", 1);
        assertThat(SuppliedVectorMismatchActivity.total() - before).isEqualTo(1L);
    }

    @Test
    void inAMultiDocumentWrite_aDocumentThatFailedInPlaceContributesNoMismatch() throws Exception {
        for (String route : List.of("write_many", "append_many")) {
            Fx f = fixture("multi");
            String cOk = ch("multi-ok-" + route), cBad = ch("multi-bad-" + route);
            float[] storedOk = vec(81), storedBad = vec(82);
            seedWithVector(f, cOk, "ok text", storedOk);
            seedWithVector(f, cBad, "bad text", storedBad);
            String okDoc = freshDoc("multi-okdoc", f.collection());
            long before = SuppliedVectorMismatchActivity.total();
            List<Map<String, Object>> chunks = List.of(vchunk(cOk, "ok text", vec(83)), vchunk(cBad, "bad text", vec(84)));
            List<Map<String, Object>> docs = List.of(
                doc(okDoc, List.of(row(0, cOk))), doc("aw.multi-missing", List.of(row(0, cBad))));

            var response = route.equals("write_many")
                ? svc.writeManyCombined(TENANT, f.collection(), chunks, docs, null, false, false, MODEL).response()
                : svc.appendManyCombined(TENANT, f.collection(), docs, chunks, false, MODEL).response();

            assertThat(response.get("failed_doc_ids")).as(route).isEqualTo(List.of("aw.multi-missing"));
            assertThat(response).as(route).containsEntry("vector_mismatches", 1);
            assertThat(SuppliedVectorMismatchActivity.total() - before)
                .as("%s: only the chash a committed document references is counted", route).isEqualTo(1L);
        }
    }

    private void seedWithVector(Fx f, String chash, String text, float[] vector) {
        svc.writeManyCombined(TENANT, f.collection(), List.of(vchunk(chash, text, vector)),
            List.of(doc(freshDoc("seedv", f.collection()), List.of(row(0, chash)))), null, false, false, MODEL);
    }

    @Test
    void aVectorWithNoEmbeddingModel_isRefused() throws Exception {
        Fx f = fixture("nomdl");
        String c = ch("nomdl-c");
        assertThatThrownBy(() -> svc.writeManyCombined(TENANT, f.collection(),
                List.of(vchunk(c, "c", vec(3))), List.of(doc(f.docId(), List.of(row(0, c)))),
                null, false, false, null))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("embedding_model");
        assertThat(chunkExists(f.collection(), c)).isFalse();
    }

    @Test
    void aRequestWithoutVectors_ignoresEmbeddingModel() throws Exception {
        Fx f = fixture("ignore");
        String c = ch("ignore-c");
        // No chunk carries a vector, so the model is not checked (it need not even be right).
        var response = svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(c, "c")), List.of(doc(f.docId(), List.of(row(0, c)))),
            null, false, false, "no-such-model").response();
        assertThat(response).containsEntry("chunks_written", 1);
    }

    @Test
    void anAllowedListOfNumbersIsAccepted_notOnlyAFloatArray() throws Exception {
        Fx f = fixture("list");
        String c = ch("list-c");
        List<Object> asList = new ArrayList<>();
        float[] v = vec(4);
        for (float x : v) asList.add((double) x);   // what Jackson hands the engine
        Map<String, Object> ch = new LinkedHashMap<>();
        ch.put("chash", c); ch.put("text", "c"); ch.put("metadata", Map.of()); ch.put("embedding", asList);

        svc.writeManyCombined(TENANT, f.collection(), List.of(ch),
            List.of(doc(f.docId(), List.of(row(0, c)))), null, false, false, MODEL);

        assertThat(storedVector(f.collection(), c)).containsExactly(v);
    }
}
