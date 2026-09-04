// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ai.djl.huggingface.tokenizers.Encoding;
import ai.djl.huggingface.tokenizers.HuggingFaceTokenizer;
import ai.onnxruntime.OnnxTensor;
import ai.onnxruntime.OrtEnvironment;
import ai.onnxruntime.OrtSession;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.LongBuffer;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-160 (bead nexus-1chpa) — LOCAL bge-768 ONNX embedder for the Java service.
 *
 * <p>The service's local-mode T3 embedder. Loads {@code BAAI/bge-base-en-v1.5}
 * (768-dim) via onnxruntime-java + DJL HuggingFace tokenizer and reproduces the
 * Python <b>fastembed</b> {@code TextEmbedding.embed()} output within tolerance
 * (parity gate {@link dev.nexus.service.Bge768ParityTest}, min cosine ≥ 0.9999).
 *
 * <p><b>Recipe (CA-2, VERIFIED — do not deviate):</b>
 * <ol>
 *   <li>HuggingFace tokenize with {@code truncation=true, maxLength=512} via DJL</li>
 *   <li>ONNX inputs: {@code input_ids}, {@code attention_mask}, and
 *       {@code token_type_ids} (zeros) <em>iff the model declares it</em></li>
 *   <li>Output[0] = {@code last_hidden_state} shape [batch, seq, 768]</li>
 *   <li><b>CLS pooling</b>: take token 0 of {@code last_hidden_state}
 *       (NOT MiniLM's masked mean-pool)</li>
 *   <li>L2-normalize each vector</li>
 * </ol>
 *
 * <p><b>No instruction prefix.</b> fastembed embeds raw input; the
 * {@code "Represent this sentence…"} query prefix is NOT applied (it would break
 * parity). Verified against {@code nexus.db.local_ef}.
 *
 * <p><b>Model artifact (CA-1 caveat, RF-160-1):</b> this loads a STANDARD
 * (un-fused) bge ONNX export (Xenova/bge-base-en-v1.5 {@code model.onnx}, fp32).
 * It must NOT be pointed at fastembed's cached qdrant {@code model_optimized.onnx}
 * — that uses the fused MS contrib op {@code SkipLayerNormalization} which
 * onnxruntime-java 1.20.0 cannot execute. {@code nx init --service} (RDR-160 P3)
 * provisions the standard export to {@link #DEFAULT_MODEL_PATH}.
 *
 * <p><b>Design (Open Q1):</b> implemented as a distinct class rather than by
 * generalizing {@link OnnxEmbedder}, keeping the MiniLM-384 pipeline pristine for
 * the T1 Python seam. A future shared base can factor the common tensor plumbing
 * if a third ONNX model arrives.
 *
 * <p>Thread-safe: {@link OrtSession} and {@link HuggingFaceTokenizer} are kept in
 * fields, both documented thread-safe for inference / encode.
 */
public final class Bge768Embedder implements Embedder {

    private static final Logger log = LoggerFactory.getLogger(Bge768Embedder.class);

    /** Canonical Java-read path for the standard bge ONNX (provisioned by {@code nx init --service}).
     * Root resolved via {@link OnnxModelPaths} (nexus-ogccs): env override, then HOME —
     * the provisioner writes under HOME, and bare {@code user.home} (the passwd entry)
     * diverges from it whenever HOME is overridden. */
    public static final String DEFAULT_MODEL_PATH =
            OnnxModelPaths.root() + "/bge-base-en-v1.5/onnx/model.onnx";

    public static final String DEFAULT_TOKENIZER_PATH =
            OnnxModelPaths.root() + "/bge-base-en-v1.5/onnx/tokenizer.json";

    /** bge-base-en-v1.5 supports 512-token context (MiniLM used 256). */
    private static final int MAX_SEQ_LEN = 512;

    /**
     * bge-base-en-v1.5 is a BERT-base architecture: 12 transformer layers, 12
     * attention heads, 768 hidden dim. Confirmed against the nexus-33hpq/nexus-zu4ma
     * measurements below (not an assumed constant): a single layer's attention-score
     * tensor is {@code [batch, heads, seq, seq]} float32, and
     * {@code batch=64 * heads=12 * seq=512 * seq=512 * 4 bytes = 805,306,368 bytes
     * ≈ 0.81 GB} exactly matches the measured 0.81 GB/tensor at batch=64/seq=512
     * from nexus-33hpq's investigation, and scales linearly to the measured 3.77 GB
     * at batch=300/seq=512.
     */
    private static final int ATTENTION_HEADS = 12;

    /**
     * Sub-batch memory budget (nexus-zu4ma), expressed as bytes for ONE layer's
     * attention-score tensor at the group's own shape:
     * {@code groupBatchSize * ATTENTION_HEADS * maxLen^2 * BYTES_PER_FLOAT32}.
     *
     * <p>Bounding by "padded token area" ({@code groupBatchSize * maxLen^2}) rather
     * than by a flat row-count constant means one long chunk in a group raises that
     * group's {@code maxLen} and therefore shrinks how many rows fit alongside it —
     * a single 512-token chunk can never inflate every OTHER row's padding cost the
     * way a flat 300-row cap did (nexus-33hpq: 3.77 GB attention tensor, ~77.4 GB
     * observed peak RSS, engine wedged).
     *
     * <p>Sized at the nexus-33hpq VERIFIED-SAFE operating point — batch=16 at the
     * full 512-token cap (every row simultaneously at MAX_SEQ_LEN, the worst case
     * for a given batch size) — because that exact shape was measured end-to-end at
     * 3.03 GB peak RSS with {@code flush_concurrency=3} genuinely active (release-
     * sandbox shakedown, 11/11 passing, zero storage_service_unhealthy events), i.e.
     * roughly 1 GB actual peak per single {@code embed()} call at this shape. This
     * is therefore not a re-derived guess: it reuses the one point on the memory
     * curve this codebase has already proven safe under real concurrent load,
     * generalized from "batch=16 rows" to "16*512^2 units of padded token area" so
     * any batch/length combination reaching the same area gets the same ceiling —
     * e.g. a 128-row batch of short (~64-token) chunks is allowed the same area as
     * 16 rows at the 512-token cap, since 128*64^2 ≈ 16*256^2 &lt; 16*512^2.
     */
    private static final long MAX_ATTENTION_TENSOR_BYTES =
            attentionTensorBytes(16, MAX_SEQ_LEN);

    private static long attentionTensorBytes(long batchSize, long seqLen) {
        return batchSize * ATTENTION_HEADS * seqLen * seqLen * Float.BYTES;
    }

    /** Equivalent padded-token-area ceiling ({@code batchSize * maxLen^2}) — heads
     * and bytes-per-float are constants that cancel out of the budget comparison,
     * so the sub-batch planner works in this unit directly rather than re-deriving
     * bytes on every candidate check. */
    private static final long MAX_PADDED_TOKEN_AREA =
            MAX_ATTENTION_TENSOR_BYTES / (ATTENTION_HEADS * (long) Float.BYTES);

    /**
     * Sanity floor for "this is the standard fp32 export, not a truncated download
     * or the ~140MB quantized/fused substitute". Mirrors the CLI's
     * {@code _MIN_MODEL_BYTES} (service_bge_model.py). The fp32 model is ~416MB.
     */
    private static final long MIN_MODEL_BYTES = 200_000_000L;

    private final OrtEnvironment      ortEnv;
    private final OrtSession          session;
    private final HuggingFaceTokenizer tokenizer;
    /** Some bge ONNX exports declare {@code token_type_ids}, some don't — feed it only if present. */
    private final boolean             wantsTokenTypeIds;

    /**
     * Counts {@code session.run()} invocations — the non-vacuity instrument for the
     * sub-batch planner (nexus-zu4ma). Package-private read/reset so
     * {@link Bge768BatchCompositionTest}, in this same package, can prove an
     * oversize batch produced more than one ONNX call without any mocking
     * framework (mirrors this class's existing {@code clsPoolNormalize}
     * package-private test-access convention).
     */
    private final AtomicInteger onnxInvocationCount = new AtomicInteger(0);

    /** Construct with the canonical bge artifact paths. */
    public Bge768Embedder() {
        this(DEFAULT_MODEL_PATH, DEFAULT_TOKENIZER_PATH);
    }

    /**
     * Construct with explicit paths (testing / non-default provisioning locations).
     *
     * @param modelPath     path to the standard un-fused bge {@code model.onnx}
     * @param tokenizerPath path to {@code tokenizer.json}
     */
    public Bge768Embedder(String modelPath, String tokenizerPath) {
        // Fail loud with a remedy BEFORE the opaque onnxruntime error: in local
        // mode this is the service's only embedder, and the ~416MB model is
        // provisioned separately by `nx init --service` (RDR-160 P3). A missing
        // file must name the path and the fix, not crash with an ORT stack trace.
        for (String[] req : new String[][]{
                {modelPath, "bge ONNX model"}, {tokenizerPath, "bge tokenizer"}}) {
            if (!java.nio.file.Files.isRegularFile(java.nio.file.Path.of(req[0]))) {
                throw new IllegalStateException(
                    "Bge768Embedder: " + req[1] + " not found at " + req[0]
                    + ". The local-mode service embeds with bge-base-en-v1.5 (768d); "
                    + "provision the STANDARD fp32 ONNX + tokenizer via `nx init --service` "
                    + "(RDR-160 P3), or point -Dnexus.bge.modelPath / -Dnexus.bge.tokenizerPath "
                    + "at an existing standard export (NOT fastembed's model_optimized.onnx).");
            }
        }
        // Size floor mirrors the CLI's _MIN_MODEL_BYTES (service_bge_model.py): the
        // standard fp32 export is ~416MB, so a model well under that is a truncated
        // download or the ~140MB quantized/fused substitute (CA-3 rejected). Catch
        // it here with a remedy rather than letting ORT fail opaquely at load.
        try {
            long bytes = java.nio.file.Files.size(java.nio.file.Path.of(modelPath));
            if (bytes < MIN_MODEL_BYTES) {
                throw new IllegalStateException(
                    "Bge768Embedder: model at " + modelPath + " is " + bytes
                    + " bytes — far below the standard fp32 bge export (~416MB). It looks "
                    + "truncated or a quantized/fused substitute (parity would silently "
                    + "degrade). Re-provision via `nx init --service` (RDR-160 P3).");
            }
        } catch (java.io.IOException e) {
            // Can't stat — don't block on a transient FS error; ORT load will surface it.
            log.warn("event=bge_model_size_check_failed path={} error={}", modelPath, e.getMessage());
        }

        this.ortEnv = OrtEnvironment.getEnvironment();

        OrtSession          sess = null;
        HuggingFaceTokenizer tok  = null;
        // SessionOptions is AutoCloseable; it holds no state once createSession returns.
        try (var sessionOpts = new OrtSession.SessionOptions()) {
            // nexus-00wsf residual: bound intra-op threads so one request cannot
            // take every core (measured 7.8 of 16 with no bound at all).
            sessionOpts.setIntraOpNumThreads(OnnxThreadPolicy.intraOpThreads());
            sess = ortEnv.createSession(modelPath, sessionOpts);

            tok = HuggingFaceTokenizer.builder()
                    .optTokenizerPath(Path.of(tokenizerPath))
                    .optMaxLength(MAX_SEQ_LEN)
                    .optTruncation(true)
                    .optPadding(false)   // we pad per-sub-batch in runOnnxSubBatch() so each tensor is rectangular
                    .build();

            this.session = sess;
            this.tokenizer = tok;
            this.wantsTokenTypeIds = sess.getInputNames().contains("token_type_ids");

            log.info("event=bge768_embedder_loaded model={} tokenizer={} token_type_ids={}",
                    modelPath, tokenizerPath, wantsTokenTypeIds);
        } catch (Exception e) {
            // Don't leak the native OrtSession handle if tokenizer construction fails.
            if (tok != null)  { try { tok.close();  } catch (Exception ignored) {} }
            if (sess != null) { try { sess.close(); } catch (Exception ignored) {} }
            throw new RuntimeException("Failed to initialise Bge768Embedder: " + e.getMessage(), e);
        }
    }

    @Override
    public String modelToken() {
        return "bge-base-en-v15-768";
    }

    @Override
    public List<float[]> embed(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();
        try {
            return embedSubBatched(texts);
        } catch (Exception e) {
            throw new RuntimeException("Bge768Embedder.embed failed: " + e.getMessage(), e);
        }
    }

    @Override
    public EmbedResult embedWithUsage(List<String> texts) {
        // ONNX is local-only: no API cost, no upstream usage counter. Emit tokens=0
        // (mirrors OnnxEmbedder) so the X-Nexus-Usage-Tokens header is not polluted.
        return new EmbedResult(embed(texts), 0L);
    }

    /**
     * Tokenizes the whole input once (cheap relative to an ONNX forward pass), then
     * greedily partitions it into sub-batches bounded by {@link #MAX_PADDED_TOKEN_AREA}
     * — never re-ordering, so results concatenate directly in input order. Each
     * sub-batch is a separate {@link #runOnnxSubBatch} call, so an oversize request
     * degrades in throughput (more ONNX invocations) rather than in memory (one
     * unbounded rectangular tensor).
     *
     * <p>Sequential (not sorted-by-length) partitioning: extending the current group
     * with the next text in order is enough to bound padded-token area — a long text
     * simply forces its group to close sooner — without the added complexity and
     * result-reordering bookkeeping a sort-then-bucket scheme would need to restore
     * input order afterward.
     *
     * <p>Degrades to the pre-existing single-shot behavior when the whole input
     * already fits under budget: the loop below produces exactly one group and one
     * ONNX call, identical to what the old unconditional {@code embedBatch} did.
     */
    private List<float[]> embedSubBatched(List<String> texts) throws Exception {
        int n = texts.size();
        Encoding[] encodings = new Encoding[n];
        int[] lens = new int[n];
        for (int i = 0; i < n; i++) {
            encodings[i] = tokenizer.encode(texts.get(i));
            // Guard a single empty text against a zero-width group (maxLen >= 1 below).
            lens[i] = Math.min((int) encodings[i].getIds().length, MAX_SEQ_LEN);
        }

        List<float[]> results = new ArrayList<>(n);
        int start = 0;
        while (start < n) {
            int end = start + 1;
            int groupMaxLen = Math.max(lens[start], 1);
            while (end < n) {
                int candidateMaxLen = Math.max(groupMaxLen, Math.max(lens[end], 1));
                long candidateArea = (long) (end - start + 1) * candidateMaxLen * candidateMaxLen;
                if (candidateArea > MAX_PADDED_TOKEN_AREA) break;
                groupMaxLen = candidateMaxLen;
                end++;
            }
            if (end - start < n) {
                // Only log when sub-batching actually engaged — the common case (whole
                // request under budget) stays silent at the old single-call volume.
                log.debug("event=bge768_subbatch start={} size={} maxLen={} totalBatch={}",
                        start, end - start, groupMaxLen, n);
            }
            results.addAll(runOnnxSubBatch(encodings, start, end, groupMaxLen));
            start = end;
        }
        return results;
    }

    /**
     * Runs ONE {@code session.run()} over {@code encodings[start, end)}, padded to
     * the caller-supplied {@code maxLen}. This is the sole ONNX invocation point —
     * {@link #onnxInvocationCount} is incremented here so the sub-batch planner's
     * non-vacuity test can prove an oversize request actually produced multiple
     * ONNX calls (nexus-zu4ma).
     */
    private List<float[]> runOnnxSubBatch(Encoding[] encodings, int start, int end, int maxLen) throws Exception {
        int batchSize = end - start;
        maxLen = Math.max(maxLen, 1);

        long[] inputIdsFlat      = new long[batchSize * maxLen];
        long[] attentionMaskFlat = new long[batchSize * maxLen];

        for (int i = 0; i < batchSize; i++) {
            long[] ids  = encodings[start + i].getIds();
            long[] mask = encodings[start + i].getAttentionMask();
            int seqLen  = Math.min(ids.length, maxLen);
            int offset  = i * maxLen;
            for (int j = 0; j < seqLen; j++) {
                inputIdsFlat[offset + j]      = ids[j];
                attentionMaskFlat[offset + j] = mask[j];
                // tokenTypeIdsFlat stays 0 — already zero-initialised
            }
            // padding positions: 0 ids, 0 mask
        }

        long[] shape = {batchSize, maxLen};

        OnnxTensor inputIdsTensor      = null;
        OnnxTensor attentionMaskTensor = null;
        OnnxTensor tokenTypeIdsTensor  = null;
        try {
            inputIdsTensor      = OnnxTensor.createTensor(ortEnv, LongBuffer.wrap(inputIdsFlat), shape);
            attentionMaskTensor = OnnxTensor.createTensor(ortEnv, LongBuffer.wrap(attentionMaskFlat), shape);

            Map<String, OnnxTensor> inputs = new HashMap<>();
            inputs.put("input_ids", inputIdsTensor);
            inputs.put("attention_mask", attentionMaskTensor);
            if (wantsTokenTypeIds) {
                long[] tokenTypeIdsFlat = new long[batchSize * maxLen]; // all zeros
                tokenTypeIdsTensor = OnnxTensor.createTensor(ortEnv, LongBuffer.wrap(tokenTypeIdsFlat), shape);
                inputs.put("token_type_ids", tokenTypeIdsTensor);
            }

            onnxInvocationCount.incrementAndGet();
            try (OrtSession.Result result = session.run(inputs)) {
                // Output 0 = last_hidden_state shape [batch, seq, 768]
                float[][][] hiddenState = (float[][][]) result.get(0).getValue();

                List<float[]> embeddings = new ArrayList<>(batchSize);
                for (int i = 0; i < batchSize; i++) {
                    embeddings.add(clsPoolNormalize(hiddenState[i]));
                }
                return embeddings;
            }
        } finally {
            if (inputIdsTensor != null)      inputIdsTensor.close();
            if (attentionMaskTensor != null) attentionMaskTensor.close();
            if (tokenTypeIdsTensor != null)  tokenTypeIdsTensor.close();
        }
    }

    /** Test-only: number of {@code session.run()} calls since construction or the
     * last {@link #resetOnnxInvocationCount()}. Package-private (see
     * {@link #onnxInvocationCount}'s javadoc). */
    int onnxInvocationCount() {
        return onnxInvocationCount.get();
    }

    /** Test-only: zero the invocation counter. Package-private (see
     * {@link #onnxInvocationCount}'s javadoc). */
    void resetOnnxInvocationCount() {
        onnxInvocationCount.set(0);
    }

    /**
     * CLS pooling + L2-normalize a single sequence.
     *
     * <p>bge-base-en-v1.5 uses the {@code [CLS]} token representation (row 0 of
     * {@code last_hidden_state}) as the sentence embedding — NOT mean-pooling.
     * Confirmed against fastembed: CLS+norm scores cosine 1.0; mean+norm scores
     * 0.825 (RDR-160 CA-2).
     *
     * @param hidden shape [seq, 768] — one row per token; row 0 is {@code [CLS]}
     */
    // Package-private (not private) so the pooling math has a model-free unit-test
    // guard on CI, where the 416MB ONNX is absent and the parity gate is skipped
    // (RDR-160 P1 review S1). See Bge768EmbedderMathTest.
    static float[] clsPoolNormalize(float[][] hidden) {
        float[] cls = hidden[0].clone();   // [CLS] token

        double sumSq = 0.0;
        for (float v : cls) sumSq += (double) v * v;
        float norm = (float) Math.sqrt(sumSq);
        if (norm > 1e-12f) {
            for (int d = 0; d < cls.length; d++) cls[d] /= norm;
        }
        return cls;
    }

    @Override
    public void close() {
        try { session.close();   } catch (Exception ignored) {}
        try { tokenizer.close(); } catch (Exception ignored) {}
        // ortEnv is a JVM-level singleton; do not close it
    }
}
