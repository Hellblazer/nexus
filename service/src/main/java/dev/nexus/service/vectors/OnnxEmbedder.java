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
import java.util.List;
import java.util.Map;

/**
 * RDR-152 bead nexus-gmiaf.20 — LOCAL ONNX embedder.
 *
 * <p>Reproduces chromadb's {@code ONNXMiniLM_L6_V2} pipeline EXACTLY (S0.2 proof,
 * cosine 1.0 verified):
 * <ol>
 *   <li>HuggingFace tokenize with {@code truncation=true, maxLength=256} via DJL</li>
 *   <li>ONNX inputs: {@code input_ids}, {@code attention_mask},
 *       {@code token_type_ids} (zeros)</li>
 *   <li>Output[0] = {@code last_hidden_state} shape [batch, seq, 384]</li>
 *   <li>Masked mean-pool:
 *       {@code sum(hidden * mask_expanded) / clip(mask.sum, 1e-9)}</li>
 *   <li>L2-normalize each vector</li>
 * </ol>
 *
 * <p>Loads the IDENTICAL chromadb-cached artifact at
 * {@code ~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/{model.onnx,tokenizer.json}}.
 *
 * <p>Thread-safe: OrtSession and HuggingFaceTokenizer are kept in fields, both
 * are documented thread-safe for inference (session) and encode (tokenizer).
 */
public final class OnnxEmbedder implements Embedder {

    private static final Logger log = LoggerFactory.getLogger(OnnxEmbedder.class);

    /** Default location of the chromadb-cached ONNX model. */
    public static final String DEFAULT_MODEL_PATH =
            System.getProperty("user.home") +
            "/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/model.onnx";

    public static final String DEFAULT_TOKENIZER_PATH =
            System.getProperty("user.home") +
            "/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/tokenizer.json";

    private static final int   MAX_SEQ_LEN = 256;
    private static final float MEAN_POOL_EPS = 1e-9f;

    private final OrtEnvironment  ortEnv;
    private final OrtSession      session;
    private final HuggingFaceTokenizer tokenizer;

    /** Construct with default chromadb artifact paths. */
    public OnnxEmbedder() {
        this(DEFAULT_MODEL_PATH, DEFAULT_TOKENIZER_PATH);
    }

    /**
     * Construct with explicit paths (for testing with non-default cache locations).
     *
     * @param modelPath     path to {@code model.onnx}
     * @param tokenizerPath path to {@code tokenizer.json}
     */
    public OnnxEmbedder(String modelPath, String tokenizerPath) {
        try {
            this.ortEnv = OrtEnvironment.getEnvironment();

            var sessionOpts = new OrtSession.SessionOptions();
            // Use CPU only — GPU not required for local mode
            this.session = ortEnv.createSession(modelPath, sessionOpts);

            // DJL HuggingFace tokenizer: truncation to maxLength=256
            this.tokenizer = HuggingFaceTokenizer.builder()
                    .optTokenizerPath(Path.of(tokenizerPath))
                    .optMaxLength(MAX_SEQ_LEN)
                    .optTruncation(true)
                    .optPadding(false)   // we pad in embed() so batch works correctly
                    .build();

            log.info("event=onnx_embedder_loaded model={} tokenizer={}", modelPath, tokenizerPath);
        } catch (Exception e) {
            throw new RuntimeException("Failed to initialise OnnxEmbedder: " + e.getMessage(), e);
        }
    }

    @Override
    public List<float[]> embed(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();
        try {
            return embedBatch(texts);
        } catch (Exception e) {
            throw new RuntimeException("OnnxEmbedder.embed failed: " + e.getMessage(), e);
        }
    }

    private List<float[]> embedBatch(List<String> texts) throws Exception {
        int batchSize = texts.size();

        // Tokenize each text individually with truncation (no batch-level padding needed
        // because we compute attention_mask and do masked mean-pool per-item).
        // For ONNX inference we still need a rectangular batch — we'll right-pad with 0s.
        Encoding[] encodings = new Encoding[batchSize];
        int maxLen = 0;
        for (int i = 0; i < batchSize; i++) {
            encodings[i] = tokenizer.encode(texts.get(i));
            maxLen = Math.max(maxLen, (int) encodings[i].getIds().length);
        }
        maxLen = Math.min(maxLen, MAX_SEQ_LEN);

        long[] inputIdsFlat       = new long[batchSize * maxLen];
        long[] attentionMaskFlat  = new long[batchSize * maxLen];
        long[] tokenTypeIdsFlat   = new long[batchSize * maxLen]; // all zeros

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
            // padding positions: 0 ids, 0 mask — correct for masked mean-pool
        }

        long[] shape = {batchSize, maxLen};

        try (OnnxTensor inputIdsTensor     = OnnxTensor.createTensor(ortEnv,
                 LongBuffer.wrap(inputIdsFlat),  shape);
             OnnxTensor attentionMaskTensor = OnnxTensor.createTensor(ortEnv,
                 LongBuffer.wrap(attentionMaskFlat), shape);
             OnnxTensor tokenTypeIdsTensor  = OnnxTensor.createTensor(ortEnv,
                 LongBuffer.wrap(tokenTypeIdsFlat), shape);
             OrtSession.Result result = session.run(Map.of(
                 "input_ids",      inputIdsTensor,
                 "attention_mask", attentionMaskTensor,
                 "token_type_ids", tokenTypeIdsTensor
             ))) {

            // Output 0 = last_hidden_state shape [batch, seq, 384]
            float[][][] hiddenState = (float[][][]) result.get(0).getValue();

            List<float[]> embeddings = new ArrayList<>(batchSize);
            for (int i = 0; i < batchSize; i++) {
                long[] mask = encodings[i].getAttentionMask();
                int seqLen  = Math.min(mask.length, maxLen);
                embeddings.add(maskedMeanPoolNormalize(hiddenState[i], mask, seqLen));
            }
            return embeddings;
        }
    }

    /**
     * Masked mean-pool + L2-normalize a single sequence.
     *
     * <p>Mirrors chromadb's Python:
     * {@code sum(token_embeddings * input_mask_expanded) / clamp(input_mask_expanded.sum, min=1e-9)}
     * then {@code F.normalize(sentence_embedding, p=2, dim=1)}.
     *
     * @param hidden  shape [seq, 384] — one row per token
     * @param mask    attention mask (1=real, 0=pad)
     * @param seqLen  actual sequence length to consider (clamped at MAX_SEQ_LEN)
     */
    private static float[] maskedMeanPoolNormalize(float[][] hidden, long[] mask, int seqLen) {
        int dims = hidden[0].length;
        float[] pooled = new float[dims];
        float maskSum  = 0f;

        for (int t = 0; t < seqLen; t++) {
            float m = (t < mask.length && mask[t] == 1L) ? 1f : 0f;
            maskSum += m;
            for (int d = 0; d < dims; d++) {
                pooled[d] += hidden[t][d] * m;
            }
        }

        float denom = Math.max(maskSum, MEAN_POOL_EPS);
        float sumSq = 0f;
        for (int d = 0; d < dims; d++) {
            pooled[d] /= denom;
            sumSq += pooled[d] * pooled[d];
        }
        float norm = (float) Math.sqrt(sumSq);
        if (norm > 1e-12f) {
            for (int d = 0; d < dims; d++) {
                pooled[d] /= norm;
            }
        }
        return pooled;
    }

    @Override
    public void close() {
        try { session.close();   } catch (Exception ignored) {}
        try { tokenizer.close(); } catch (Exception ignored) {}
        // ortEnv is a JVM-level singleton; do not close it
    }
}
