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

    /** Canonical Java-read path for the standard bge ONNX (provisioned by {@code nx init --service}). */
    public static final String DEFAULT_MODEL_PATH =
            System.getProperty("user.home") +
            "/.cache/nexus/onnx_models/bge-base-en-v1.5/onnx/model.onnx";

    public static final String DEFAULT_TOKENIZER_PATH =
            System.getProperty("user.home") +
            "/.cache/nexus/onnx_models/bge-base-en-v1.5/onnx/tokenizer.json";

    /** bge-base-en-v1.5 supports 512-token context (MiniLM used 256). */
    private static final int MAX_SEQ_LEN = 512;

    private final OrtEnvironment      ortEnv;
    private final OrtSession          session;
    private final HuggingFaceTokenizer tokenizer;
    /** Some bge ONNX exports declare {@code token_type_ids}, some don't — feed it only if present. */
    private final boolean             wantsTokenTypeIds;

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
        this.ortEnv = OrtEnvironment.getEnvironment();

        OrtSession          sess = null;
        HuggingFaceTokenizer tok  = null;
        // SessionOptions is AutoCloseable; it holds no state once createSession returns.
        try (var sessionOpts = new OrtSession.SessionOptions()) {
            sess = ortEnv.createSession(modelPath, sessionOpts);

            tok = HuggingFaceTokenizer.builder()
                    .optTokenizerPath(Path.of(tokenizerPath))
                    .optMaxLength(MAX_SEQ_LEN)
                    .optTruncation(true)
                    .optPadding(false)   // we pad in embedBatch() so the batch tensor is rectangular
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
            return embedBatch(texts);
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

    private List<float[]> embedBatch(List<String> texts) throws Exception {
        int batchSize = texts.size();

        Encoding[] encodings = new Encoding[batchSize];
        int maxLen = 0;
        for (int i = 0; i < batchSize; i++) {
            encodings[i] = tokenizer.encode(texts.get(i));
            maxLen = Math.max(maxLen, (int) encodings[i].getIds().length);
        }
        maxLen = Math.min(maxLen, MAX_SEQ_LEN);
        // Guard against an all-empty batch producing a zero-width tensor.
        maxLen = Math.max(maxLen, 1);

        long[] inputIdsFlat      = new long[batchSize * maxLen];
        long[] attentionMaskFlat = new long[batchSize * maxLen];

        for (int i = 0; i < batchSize; i++) {
            long[] ids  = encodings[i].getIds();
            long[] mask = encodings[i].getAttentionMask();
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
