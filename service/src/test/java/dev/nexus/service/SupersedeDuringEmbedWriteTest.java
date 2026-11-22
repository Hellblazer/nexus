// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CollectionSupersededException;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-i15oc: a supersede that commits DURING a write's embed round trip. The up-front
 * {@code lookupWritable} (nexus-mp8ys) has already passed by then; the check after the embedder is the one
 * that sees the tombstone, because the supersede evicts the cached live row and the re-read hits the
 * database. The embedder here runs the supersede on its first call, which is the production timing (a
 * rename on another process while this one waits on Voyage).
 *
 * <p>{@code upsertChunks} re-checks with {@code requireWritable}; plain {@code requireRegistered} would
 * pass the tombstone and file the batch under the retired name, so the first test fails if that site is
 * reverted. The combined write is covered by a second check inside its per-document transaction (the
 * manifest rows' {@code requireWritable} rolls the chunk rows back); the second test pins that, and passes
 * on either form of {@code upsertManifestChunkVectors}' own check.
 */
class SupersedeDuringEmbedWriteTest extends AtomicWriteTestBase {

    /** A 384-dim one-hot embedder that runs {@code onFirstEmbed} once, the first time it is asked to embed. */
    private static final class HookedEmbedder implements Embedder {
        private Runnable onFirstEmbed = () -> { };

        void arm(Runnable r) {
            this.onFirstEmbed = r;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            Runnable r = onFirstEmbed;
            onFirstEmbed = () -> { };
            r.run();
            List<float[]> out = new ArrayList<>(texts.size());
            for (String t : texts) {
                float[] v = new float[384];
                v[Math.floorMod(t.hashCode(), 384)] = 1.0f;
                out.add(v);
            }
            return out;
        }

        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            return new EmbedResult(embed(texts), texts.size());
        }

        @Override
        public String modelToken() {
            return "minilm-l6-v2-384";
        }
    }

    private HookedEmbedder hooked;
    private PgVectorRepository vectors;
    private CombinedWriteService combined;

    @BeforeAll
    void wire() {
        hooked = new HookedEmbedder();
        vectors = new PgVectorRepository(tenantScope, hooked, hooked);
        combined = new CombinedWriteService(tenantScope, repo, new EmbedderRouter(hooked, "document"));
    }

    private static CollectionSupersededException supersededIn(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof CollectionSupersededException s) return s;
        }
        return null;
    }

    private String retireDuringNextEmbed(Fx f) {
        String successor = f.collection().replace("__v1", "__v2");
        hooked.arm(() -> assertThat(repo.supersedeCollection(TENANT, f.collection(), successor, "")).isEqualTo(1));
        return successor;
    }

    @Test
    void upsertChunks_whenTheNameIsRetiredWhileEmbedding_isRefusedNamingTheSuccessor_andStoresNothing() throws Exception {
        Fx f = fixture("se-up");
        String successor = retireDuringNextEmbed(f);
        String text = "retired-while-embedding-" + f.docId();

        Throwable thrown = null;
        try {
            vectors.upsertChunks(TENANT, f.collection(), List.of(ch(text)), List.of(text), List.of(Map.of()));
        } catch (RuntimeException e) {
            thrown = e;
        }

        assertThat(thrown).as("the write must be refused, not stored under the tombstone").isNotNull();
        CollectionSupersededException s = supersededIn(thrown);
        assertThat(s).as("refusal type, got: %s", thrown).isNotNull();
        assertThat(s.collection()).isEqualTo(f.collection());
        assertThat(s.supersededBy()).isEqualTo(successor);
        assertThat(chunkExists(f.collection(), ch(text))).as("no chunk strands under the tombstone").isFalse();
    }

    @Test
    void writeManyCombined_whenTheNameIsRetiredWhileEmbedding_isRefusedAndRollsBackTheChunks() throws Exception {
        Fx f = fixture("se-wm");
        String successor = retireDuringNextEmbed(f);
        String text = "combined-retired-while-embedding-" + f.docId();
        String chash = ch(text);

        assertThatThrownBy(() -> combined.writeManyCombined(TENANT, f.collection(),
                List.of(chunk(chash, text)), List.of(doc(f.docId(), List.of(row(0, chash)))),
                Map.of(), false, false))
            .satisfies(t -> {
                CollectionSupersededException s = supersededIn(t);
                assertThat(s).as("refusal type, got: %s", t).isNotNull();
                assertThat(s.supersededBy()).isEqualTo(successor);
            });

        assertThat(chunkExists(f.collection(), chash)).as("the chunk row rolled back with the refusal").isFalse();
    }

    @Test
    void control_upsertChunks_toALiveName_isStored() throws Exception {
        Fx f = fixture("se-live");
        String text = "live-" + f.docId();

        vectors.upsertChunks(TENANT, f.collection(), List.of(ch(text)), List.of(text), List.of(Map.of()));

        assertThat(chunkExists(f.collection(), ch(text))).isTrue();
    }
}
