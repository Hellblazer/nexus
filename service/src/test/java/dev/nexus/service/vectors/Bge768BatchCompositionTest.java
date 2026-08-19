// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Engine v0.1.70 hardening, work item W1 (T2 {@code nexus/engine-embed-path-hardening-design-v0.1.70}).
 *
 * <p><b>This test is the GATE for sub-batching {@link Bge768Embedder} (design items W2/W3)
 * and for ORT {@code SessionOptions} tuning (W9).</b> Both change how many texts are packed
 * into one rectangular ONNX tensor and therefore change {@code maxLen} (the padding width)
 * for any given text depending on which other texts share its batch. {@link EmbedParityTest}
 * (bead nexus-gmiaf.21, {@code onnx_bitExact_determinism_batchVsSingle}) already documents
 * for the MiniLM {@link OnnxEmbedder} that batch-vs-single ONNX inference is <b>not
 * bit-exact</b> — different tensor shapes drive different SIMD/thread-scheduling paths inside
 * ONNX Runtime, producing float32 rounding differences. That equivalence test exists for
 * MiniLM only. Before this repo can safely split a Bge768 batch into sub-batches, it must be
 * able to prove Bge768 embeddings are STABLE under re-composition, not merely assume it
 * because MiniLM was. <b>If you are about to change {@link Bge768Embedder}'s batching (a new
 * sub-batch planner, a shape-bucketing scheme, an ORT threading/arena knob), this test failing
 * means your change measurably moved the embedding for the SAME text depending on which other
 * texts happened to share its tensor — that is exactly the class of silent, un-gated numeric
 * drift this design exists to catch. Do not raise the tolerance to make it pass; find out why
 * the composition changed the output that much.</b>
 *
 * <h2>Tolerance: cosine, never bit-equality</h2>
 * <p>{@code pom.xml} deliberately carries no {@code rerunFailingTestsCount} ("the suite's job
 * is to fail loud on nondeterminism") — a bit-exact assertion here would therefore be a
 * <em>permanently red</em> test the moment ONNX Runtime's shape-dependent rounding kicks in,
 * which the MiniLM precedent already proves it does. So, exactly as {@code EmbedParityTest}
 * does for MiniLM, this test asserts cosine similarity against a tolerance, never
 * {@code isEqualTo}.
 *
 * <h2>How the tolerance was derived (measured, not copied)</h2>
 * <p>The MiniLM test's comment attributes ~1e-7-level float32 rounding to cross-shape ONNX
 * execution and gates at {@code cosine > 1.0 - 1e-6}. That number was <b>not</b> reused here
 * without checking — bge-base-en-v1.5 is a different model, tokenizer, and (per bead RDR-160
 * CA-2) a different pooling strategy (CLS vs MiniLM's masked mean-pool), so its batch-shape
 * sensitivity had to be measured independently. Measured on this exact model/runtime
 * (bge-base-en-v1.5 fp32, onnxruntime-java 1.20.0, CPU execution provider) via a throwaway
 * harness run twice for determinism, comparing the SAME text's embedding across four distinct
 * batch compositions (all-in-one, one-at-a-time, and two different pairwise groupings that
 * force different {@code maxLen} per group):
 * <ul>
 *   <li>6-text corpus mixing short (~10 tokens) and near-cap-length (~500 token) texts:
 *       minimum pairwise cosine observed = {@code 0.9999999999996041} (delta ≈ 4.0e-13),
 *       reproduced identically across two independent runs.</li>
 *   <li>40-text corpus spanning 10 to ~2000 chars, all-in-one vs. 8-row sub-batches vs.
 *       singles (the actual shape production sub-batching will produce): minimum pairwise
 *       cosine observed = {@code 0.9999999999992428} (delta ≈ 7.6e-13).</li>
 * </ul>
 * <p>So the true batch-composition sensitivity of Bge768 on this hardware is ~1e-12–1e-13 —
 * about four orders of magnitude TIGHTER than the ~1e-7 the MiniLM test's own comment
 * attributes to this phenomenon in general. The gate below is nonetheless still set to
 * {@code cosine > 1.0 - 1e-6}, matching the repo's existing documented magnitude class for
 * exactly this phenomenon (cross-shape ONNX float32 rounding), for three reasons: (a) it is
 * not an arbitrary or copied number — measurement confirmed it comfortably covers the observed
 * effect rather than being blindly inherited; (b) it leaves roughly six orders of magnitude of
 * headroom over the locally-measured floor to absorb legitimate cross-platform variance (CI
 * runners use different CPU microarchitectures / thread counts / SIMD paths than this dev
 * machine — a real difference in {@code maxLen}-driven kernel scheduling could plausibly move
 * the rounding delta closer to the MiniLM-documented ~1e-7 class on different hardware, and
 * given {@code pom.xml}'s no-rerun policy this test must never flake against that); (c) it
 * remains many orders of magnitude tighter than the collapse a genuine composition bug
 * produces — see the RED evidence recorded in the class-level non-vacuity note below.
 *
 * <h2>Non-vacuity (mandatory per the W1 design task)</h2>
 * <p>Before this test was committed, its ability to fail was demonstrated by temporarily
 * comparing each text's all-in-one embedding against a DIFFERENT text's embedding (an
 * off-by-one index shift) instead of the same text's embedding under a different grouping.
 * That run went RED with a measured cosine similarity around 0.1–0.4 (semantically unrelated
 * vectors), many orders of magnitude below the {@code 1.0 - 1e-6} gate — proving the assertion
 * discriminates real drift from noise rather than passing unconditionally. The change was
 * reverted before commit; see the W1 write-back (T2 {@code nexus/engine-w1-bge-batch-equivalence})
 * for the exact console output.
 *
 * <h2>Model-gating</h2>
 * <p>Mirrors {@link Bge768ParityTest}'s idiom exactly: the ~416MB standard fp32 bge ONNX
 * export is not committed, so when it is absent (and {@code -Dnexus.bge.modelPath} /
 * {@code -Dnexus.bge.tokenizerPath} are not pointed elsewhere) this test SKIPS via a JUnit
 * {@link Assumptions} check with a loud message naming the remedy — never a silent pass. CI's
 * {@code prime-bge-onnx} action (see {@code .github/workflows/service-ci.yml}) provisions the
 * model unconditionally, so on CI this gate always executes; it can only skip in a local
 * checkout that has not run {@code nx init --service}. The Java-side max-skip / non-vacuity
 * meta-assert this javadoc used to note as absent is now {@code scripts/assert_bge_gates_ran.py}
 * (nexus-zbwgb), which fails the CI job outright if this class or {@link Bge768ParityTest}
 * ever reports zero testcases or any {@code <skipped>} entry.
 *
 * <h2>W2/W3 landed (nexus-zu4ma)</h2>
 * <p>{@link Bge768Embedder} now sub-batches internally ({@code embedSubBatched}), bounded by
 * a memory budget (padded-token-area, {@code Bge768Embedder#MAX_PADDED_TOKEN_AREA}) rather
 * than a request-count constant. {@link #oversizeBatch_internalSubBatching_multipleInvocations_outputsMatchSingles()}
 * below is the non-vacuity test the class javadoc originally called for: it drives
 * {@link Bge768Embedder#embed} with a batch sized to force the INTERNAL planner to split (not
 * a manually pre-grouped call as the three tests above do), and proves the split actually
 * happened via the package-private {@code onnxInvocationCount()} instrument rather than
 * inferring it from timing or output shape.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Bge768BatchCompositionTest {

    private static final double COSINE_TOLERANCE = 1.0 - 1e-6;

    // Fixed corpus: short texts (little/no padding when paired with another short text),
    // a medium text, and two long texts near/at the 512-token cap (MAX_SEQ_LEN). Pairing a
    // short text with a long one forces a much larger maxLen for that short text's group than
    // pairing it with another short text -- this is the mechanism that makes composition
    // matter. A corpus where every text is the same length could not detect it.
    private static final String SHORT_A = "The quick brown fox jumps over the lazy dog.";
    private static final String SHORT_B = "Semantic search connects questions to answers through meaning.";
    private static final String SHORT_C = "A short cat sat quietly on a mat near the door.";
    private static final String MEDIUM =
            "In the beginning God created the heavens and the earth. Now the earth was formless " +
            "and empty, darkness was over the surface of the deep, and the Spirit of God was " +
            "hovering over the waters. And God said, Let there be light, and there was light.";
    private static final String LONG_A = longText(
            "attention layer embedding vector token sequence padding batch composition cosine " +
            "similarity normalize gradient tensor kernel scheduling thread rounding shape", 480);
    private static final String LONG_B = longText(
            "search index retrieval document chunk manifest catalog collection query result " +
            "score rank filter aggregate summarize extract compare generate verify", 480);

    /** Deterministically cycles through the words of {@code seedPhrase} to build a text of
     * approximately {@code targetWords} words -- long enough to approach/exceed the 512-token
     * cap after tokenization, with no dependency on a PRNG so the corpus is fully literal and
     * auditable. */
    private static String longText(String seedPhrase, int targetWords) {
        String[] words = seedPhrase.split(" ");
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < targetWords; i++) {
            sb.append(words[i % words.length]).append(' ');
        }
        return sb.toString().trim() + ".";
    }

    // Index order matters -- referenced positionally by the grouping helpers below.
    private static final List<String> CORPUS = List.of(
            SHORT_A, SHORT_B, MEDIUM, LONG_A, SHORT_C, LONG_B);

    private Bge768Embedder embedder;

    @BeforeAll
    void setUp() {
        String modelPath = System.getProperty("nexus.bge.modelPath", Bge768Embedder.DEFAULT_MODEL_PATH);
        String tokPath   = System.getProperty("nexus.bge.tokenizerPath", Bge768Embedder.DEFAULT_TOKENIZER_PATH);

        boolean present = Files.isRegularFile(Path.of(modelPath)) && Files.isRegularFile(Path.of(tokPath));
        Assumptions.assumeTrue(present, () ->
                "bge-768 batch-composition gate SKIPPED: standard un-fused bge ONNX not found.\n" +
                "  expected model:     " + modelPath + "\n" +
                "  expected tokenizer: " + tokPath + "\n" +
                "  Provision via `nx init --service` (RDR-160 P3), or set -Dnexus.bge.modelPath / " +
                "-Dnexus.bge.tokenizerPath.\n" +
                "  This gate gates engine v0.1.70 sub-batching (W2/W3) -- do not bypass it, provision the model.");

        embedder = new Bge768Embedder(modelPath, tokPath);
    }

    private static double cosine(float[] a, float[] b) {
        double dot = 0.0, na = 0.0, nb = 0.0;
        for (int d = 0; d < a.length; d++) {
            dot += (double) a[d] * b[d];
            na  += (double) a[d] * a[d];
            nb  += (double) b[d] * b[d];
        }
        return dot / (Math.sqrt(na) * Math.sqrt(nb));
    }

    /**
     * All-in-one batch (batch size 6, one shared {@code maxLen} across every text) vs.
     * one-at-a-time (batch size 1, own {@code maxLen} each, no cross-text padding at all).
     * The starkest possible composition contrast for a given corpus.
     */
    @Test
    void allInOne_vs_singles_cosineWithinTolerance() {
        List<float[]> allInOne = embedder.embed(CORPUS);
        assertThat(allInOne).hasSize(CORPUS.size());

        List<float[]> singles = new ArrayList<>();
        for (String text : CORPUS) singles.add(embedder.embedOne(text));

        for (int i = 0; i < CORPUS.size(); i++) {
            double cos = cosine(allInOne.get(i), singles.get(i));
            assertThat(cos)
                    .as("text[%d] all-in-one vs single-item cosine must clear the batch-composition " +
                        "equivalence gate (tolerance derivation: class javadoc)", i)
                    .isGreaterThan(COSINE_TOLERANCE);
        }
    }

    /**
     * Two different pairwise groupings of the SAME corpus, chosen so the padding a given text
     * receives genuinely differs between them: grouping A pairs short-with-short and
     * medium/short-with-long; grouping B pairs several of those same texts with a DIFFERENT
     * partner, flipping several texts from "padded to a short partner's length" to "padded to
     * the 512-token cap" (or vice versa). This is the case a same-length corpus cannot exercise.
     */
    @Test
    void differentPairwiseGroupings_cosineWithinTolerance() {
        // Grouping A: [0,1] [2,3] [4,5] -- short+short, medium+long, short+long.
        List<float[]> groupA = new ArrayList<>();
        groupA.addAll(embedder.embed(List.of(CORPUS.get(0), CORPUS.get(1))));
        groupA.addAll(embedder.embed(List.of(CORPUS.get(2), CORPUS.get(3))));
        groupA.addAll(embedder.embed(List.of(CORPUS.get(4), CORPUS.get(5))));

        // Grouping B: [0,3] [1,4] [2,5] -- short+long, short+short, medium+long. Text[0] goes
        // from padded-to-short(1)'s length in A to padded-to-long(3)'s 512-cap length in B;
        // text[1] goes the opposite direction. This is the mechanism under test.
        List<float[]> groupBRaw = new ArrayList<>();
        groupBRaw.addAll(embedder.embed(List.of(CORPUS.get(0), CORPUS.get(3))));
        groupBRaw.addAll(embedder.embed(List.of(CORPUS.get(1), CORPUS.get(4))));
        groupBRaw.addAll(embedder.embed(List.of(CORPUS.get(2), CORPUS.get(5))));
        float[][] groupB = new float[CORPUS.size()][];
        groupB[0] = groupBRaw.get(0);
        groupB[3] = groupBRaw.get(1);
        groupB[1] = groupBRaw.get(2);
        groupB[4] = groupBRaw.get(3);
        groupB[2] = groupBRaw.get(4);
        groupB[5] = groupBRaw.get(5);

        for (int i = 0; i < CORPUS.size(); i++) {
            double cos = cosine(groupA.get(i), groupB[i]);
            assertThat(cos)
                    .as("text[%d] grouping-A vs grouping-B cosine must clear the batch-composition " +
                        "equivalence gate (tolerance derivation: class javadoc)", i)
                    .isGreaterThan(COSINE_TOLERANCE);
        }
    }

    /**
     * Production-shaped check: an all-in-one batch vs. the SAME corpus split into fixed-size
     * sub-batches of 3 -- the shape W2/W3's sub-batch planner will actually produce once wired
     * in. This is the test that must keep passing once sub-batching lands; it does not require
     * the planner to exist yet because {@link Bge768Embedder#embed} already accepts an
     * arbitrary sub-list and the concatenation-order contract is what sub-batching depends on.
     */
    @Test
    void allInOne_vs_fixedSizeSubBatches_cosineWithinTolerance() {
        List<float[]> allInOne = embedder.embed(CORPUS);

        List<float[]> subBatched = new ArrayList<>();
        int subBatchSize = 3;
        for (int i = 0; i < CORPUS.size(); i += subBatchSize) {
            subBatched.addAll(embedder.embed(CORPUS.subList(i, Math.min(i + subBatchSize, CORPUS.size()))));
        }
        assertThat(subBatched).hasSize(CORPUS.size());

        for (int i = 0; i < CORPUS.size(); i++) {
            double cos = cosine(allInOne.get(i), subBatched.get(i));
            assertThat(cos)
                    .as("text[%d] all-in-one vs 3-row-sub-batched cosine must clear the batch-composition " +
                        "equivalence gate (tolerance derivation: class javadoc)", i)
                    .isGreaterThan(COSINE_TOLERANCE);
        }
    }

    /**
     * THE non-vacuity gate for the sub-batch planner (nexus-zu4ma). A 20-row batch where every
     * row is a near-512-token text has padded-token-area {@code 20 * 512^2 = 5,242,880}, which
     * exceeds {@code Bge768Embedder#MAX_PADDED_TOKEN_AREA} ({@code 16 * 512^2 = 4,194,304}) — so
     * the embedder's OWN internal planner (not this test) MUST split it into more than one
     * {@code session.run()} call. This is proven directly via the package-private invocation
     * counter, not inferred from wall-clock time or from the shape of the result.
     *
     * <p>Also re-asserts the output-parity property {@link #allInOne_vs_singles_cosineWithinTolerance()}
     * establishes for a manually-grouped call, but here for the embedder's OWN internally-chosen
     * grouping — the actual production code path this bead hardens.
     *
     * <p><b>RED/GREEN non-vacuity, demonstrated 2026-08-19 (recorded on nexus-zu4ma, not
     * committed here):</b> temporarily hard-coding {@code MAX_PADDED_TOKEN_AREA} to
     * {@code Long.MAX_VALUE} (i.e. disabling sub-batching — the planner always produces exactly
     * one group) turned this test RED on the invocation-count assertion
     * ({@code expected: greater than 1, but was: 1}); the cosine assertions stayed green
     * (single-group output is unchanged, as expected). Reverted before commit — this class'
     * own git history / working tree never carried the disabled state.
     */
    @Test
    void oversizeBatch_internalSubBatching_multipleInvocations_outputsMatchSingles() {
        List<String> big = new ArrayList<>();
        for (int i = 0; i < 20; i++) {
            big.add(longText("padding memory budget sub batch planner invocation counter shape " + i, 480));
        }

        embedder.resetOnnxInvocationCount();
        List<float[]> batched = embedder.embed(big);
        assertThat(batched).hasSize(big.size());

        int invocations = embedder.onnxInvocationCount();
        assertThat(invocations)
                .as("a 20-row near-512-token batch must be split into more than one ONNX " +
                    "invocation by the embedder's own sub-batch planner (padded-token-area " +
                    "20*512^2 exceeds the 16*512^2 budget)")
                .isGreaterThan(1);

        for (int i = 0; i < big.size(); i++) {
            float[] single = embedder.embedOne(big.get(i));
            double cos = cosine(batched.get(i), single);
            assertThat(cos)
                    .as("text[%d] internally-sub-batched vs single-item cosine must clear the " +
                        "batch-composition equivalence gate (tolerance derivation: class javadoc)", i)
                    .isGreaterThan(COSINE_TOLERANCE);
        }
    }
}
