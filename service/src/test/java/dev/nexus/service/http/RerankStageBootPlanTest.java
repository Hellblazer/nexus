// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.vectors.BootPlan;
import dev.nexus.service.vectors.CrossEncoderReranker;
import dev.nexus.service.vectors.EmbeddingProfile;
import dev.nexus.service.vectors.LocalModelProbe;
import dev.nexus.service.vectors.LocalOnnxAdmission;
import dev.nexus.service.vectors.Reranker;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Phase 2 Step 4 (bead nexus-un7vj) — the boot-level rerank assertions, by value. {@code Main.main}
 * cannot run in a test, so the reranker a given presence-and-profile combination BUILDS ({@link BootPlan}) is run
 * through the real {@link RerankStage} that every search route uses, with a counting stand-in for Voyage. The
 * claim under test is the user-visible one: a {@code bge}-profile engine reranks locally and sends no rerank text
 * to Voyage, with or without a key, with or without the bge files, and with or without the cross-encoder's own.
 *
 * <p>Also nexus-lt84r: a native-load {@link Error} from the local reranker is a loud degrade, not a 500.
 */
class RerankStageBootPlanTest {

    @TempDir Path dir;

    /** Stand-in for Voyage: counts calls so "no Voyage rerank call" is a number, not an absence of evidence. */
    private static final class CountingVoyage implements Reranker {
        final AtomicInteger built = new AtomicInteger();
        final AtomicInteger calls = new AtomicInteger();
        @Override public String modelToken() { return "rerank-2.5"; }
        @Override public List<Scored> rerank(String query, List<String> documents, Integer topK) {
            calls.incrementAndGet();
            return reversed(documents.size());
        }
    }

    /** Stand-in for the cross-encoder: ranks the last document first so a reorder is visible. */
    private static final class FakeCrossEncoder implements Reranker {
        final AtomicInteger calls = new AtomicInteger();
        @Override public String modelToken() { return "ms-marco-minilm-l6-v2"; }
        @Override public List<Scored> rerank(String query, List<String> documents, Integer topK) {
            calls.incrementAndGet();
            return reversed(documents.size());
        }
    }

    private static List<Reranker.Scored> reversed(int n) {
        List<Reranker.Scored> out = new ArrayList<>();
        for (int i = n - 1; i >= 0; i--) out.add(new Reranker.Scored(i, i));
        return out;
    }

    private static LocalModelProbe.Presence presence(boolean bge, boolean key, boolean crossEncoder) {
        return new LocalModelProbe.Presence(bge, false, key, "bge", bge ? List.of() : List.of("/m/bge.onnx"),
                crossEncoder, "ce", crossEncoder ? List.of() : List.of("/m/ce.onnx"));
    }

    private static List<Map<String, Object>> rows() {
        List<Map<String, Object>> rows = new ArrayList<>();
        for (String id : List.of("a", "b", "c")) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("id", id);
            row.put("content", "text " + id);
            rows.add(row);
        }
        return rows;
    }

    @SuppressWarnings("unchecked")
    private static List<String> ids(Map<String, Object> envelope) {
        return ((List<Map<String, Object>>) envelope.get("results")).stream().map(r -> (String) r.get("id")).toList();
    }

    /** Build the reranker for {@code plan} with a counting Voyage and the given cross-encoder, apply it to 3 rows. */
    private Map<String, Object> rerankThrough(BootPlan plan, CountingVoyage voyage, Reranker crossEncoder) {
        Reranker built = plan.buildReranker(() -> { voyage.built.incrementAndGet(); return voyage; },
                () -> crossEncoder,
                plan.buildsAdmissionGate() ? new LocalOnnxAdmission(1, 1000) : null);
        return new RerankStage(built).apply("query", rows(), null);
    }

    // ── the bead's listed engine tests ─────────────────────────────────────────────────────────

    @Test
    void aKeyedBgeProfileEngine_reranksWithTheCrossEncoder_andMakesNoVoyageRerankCall() {
        var plan = BootPlan.decide(presence(true, true, true), EmbeddingProfile.BGE);
        var voyage = new CountingVoyage();
        var cross = new FakeCrossEncoder();

        var envelope = rerankThrough(plan, voyage, cross);

        assertThat(envelope.get("rerank_degraded")).isEqualTo(false);
        assertThat(envelope.get("rerank_model")).isEqualTo("ms-marco-minilm-l6-v2");
        assertThat(ids(envelope)).as("reordered by the cross-encoder").containsExactly("c", "b", "a");
        assertThat(cross.calls).hasValue(1);
        assertThat(voyage.calls).as("no rerank text reaches Voyage").hasValue(0);
        assertThat(voyage.built).as("the Voyage reranker is not even constructed").hasValue(0);
    }

    @Test
    void aVoyageProfileWithNoKey_reranksWithTheCrossEncoder() {
        var plan = BootPlan.decide(presence(true, false, true), EmbeddingProfile.VOYAGE);
        var voyage = new CountingVoyage();
        var cross = new FakeCrossEncoder();

        var envelope = rerankThrough(plan, voyage, cross);

        assertThat(envelope.get("rerank_degraded")).isEqualTo(false);
        assertThat(envelope.get("rerank_model")).isEqualTo("ms-marco-minilm-l6-v2");
        assertThat(cross.calls).hasValue(1);
        assertThat(voyage.calls).hasValue(0);
    }

    @Test
    void aVoyageProfileWithAKey_reranksWithVoyage_onlyThere() {
        var plan = BootPlan.decide(presence(false, true, false), EmbeddingProfile.VOYAGE);
        var voyage = new CountingVoyage();
        var cross = new FakeCrossEncoder();

        var envelope = rerankThrough(plan, voyage, cross);

        assertThat(envelope.get("rerank_model")).isEqualTo("rerank-2.5");
        assertThat(voyage.calls).hasValue(1);
        assertThat(cross.calls).as("the cross-encoder is not the reranker here").hasValue(0);
    }

    @Test
    void aKeyedBgeProfileWithNoBgeFiles_buildsTheCrossEncoderAndNoBgeEmbedder_andMakesNoVoyageRerankCall() {
        // The degraded boot of the bead: profile bge, key present, bge files absent, cross-encoder files present.
        var plan = BootPlan.decide(presence(false, true, true), EmbeddingProfile.BGE);
        assertThat(plan.buildsBge()).as("no bge embedder is built").isFalse();
        assertThat(plan.degradation()).as("the choice is degraded and named").isPresent();
        assertThat(plan.buildsAdmissionGate()).as("the shared admission gate is built with no bge embedder").isTrue();
        var voyage = new CountingVoyage();
        var cross = new FakeCrossEncoder();

        var envelope = rerankThrough(plan, voyage, cross);

        assertThat(envelope.get("rerank_degraded")).isEqualTo(false);
        assertThat(envelope.get("rerank_model")).isEqualTo("ms-marco-minilm-l6-v2");
        assertThat(cross.calls).hasValue(1);
        assertThat(voyage.calls).as("a keyed bge-profile engine still sends no rerank text to Voyage").hasValue(0);
        assertThat(voyage.built).hasValue(0);
    }

    @Test
    void theSameEngineWithoutCrossEncoderFiles_skipsTheRerankLoudly_andStillMakesNoVoyageCall() {
        var plan = BootPlan.decide(presence(false, true, false), EmbeddingProfile.BGE);
        assertThat(plan.crossEncoderFilesMissing()).isTrue();
        var voyage = new CountingVoyage();
        // The REAL cross-encoder, pointed at files that are not there: its lazy init fails typed, per request.
        var missing = new CrossEncoderReranker(dir.resolve("absent-model.onnx").toString(),
                dir.resolve("absent-tokenizer.json").toString());

        var envelope = rerankThrough(plan, voyage, missing);

        assertThat(envelope.get("rerank_degraded")).as("loud, structured, never silent").isEqualTo(true);
        assertThat((String) envelope.get("rerank_error")).contains("cross-encoder").contains("not found");
        assertThat(ids(envelope)).as("the rows still serve, in distance order").containsExactly("a", "b", "c");
        assertThat(envelope).doesNotContainKey("rerank_model");
        assertThat(voyage.calls).as("the skip does not fall back to Voyage").hasValue(0);
        assertThat(voyage.built).hasValue(0);
    }

    // ── nexus-lt84r: 'cannot load' covers native-load Errors ───────────────────────────────────

    /** A reranker whose native runtime will not load: throws the given Error from every call. */
    private static Reranker failingWith(Error error) {
        return new Reranker() {
            @Override public String modelToken() { return "ms-marco-minilm-l6-v2"; }
            @Override public List<Scored> rerank(String query, List<String> documents, Integer topK) { throw error; }
        };
    }

    @Test
    void aNativeLoadError_isALoudDegrade_notAFailedSearch() {
        for (Error error : List.of(new UnsatisfiedLinkError("no onnxruntime4j_jni in java.library.path"),
                                   new NoClassDefFoundError("Could not initialize class ai.onnxruntime.OrtEnvironment"),
                                   new ExceptionInInitializerError("static init failed"))) {
            var envelope = new RerankStage(failingWith(error)).apply("query", rows(), null);

            assertThat(envelope.get("rerank_degraded")).as(error.getClass().getSimpleName()).isEqualTo(true);
            assertThat((String) envelope.get("rerank_error")).contains("cannot load its native runtime")
                    .contains(error.getClass().getSimpleName()).contains(BootPlan.PROVISION_COMMAND);
            assertThat(ids(envelope)).containsExactly("a", "b", "c");
        }
    }

    @Test
    void aNativeLoadError_throughTheAdmissionGate_releasesThePermit_soTheNextRerankDegradesTheSameWay() {
        var plan = BootPlan.decide(presence(false, true, true), EmbeddingProfile.BGE);
        // One permit: a leaked permit would make the second call fail on admission, with a different message.
        var admission = new LocalOnnxAdmission(1, 200);
        Reranker built = plan.buildReranker(() -> new CountingVoyage(),
                () -> failingWith(new UnsatisfiedLinkError("no jni")), admission);
        var stage = new RerankStage(built);

        for (int i = 0; i < 3; i++) {
            var envelope = stage.apply("query", rows(), null);
            assertThat((String) envelope.get("rerank_error")).as("call %d", i)
                    .contains("cannot load its native runtime");
        }
    }

    @Test
    void anOrdinaryExceptionStillTakesTheExistingDegradeArm() {
        // The new arm does not swallow what the old ones handled.
        var envelope = new RerankStage(new Reranker() {
            @Override public String modelToken() { return "x"; }
            @Override public List<Scored> rerank(String query, List<String> documents, Integer topK) {
                throw new dev.nexus.service.vectors.RerankUpstreamException("boom");
            }
        }).apply("query", rows(), null);
        assertThat(envelope.get("rerank_error")).isEqualTo("boom");
    }
}
