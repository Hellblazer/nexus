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
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-188 bead nexus-9o6y2.3 — LOCAL cross-encoder reranker for the no-Voyage
 * posture (RDR-109/160 lineage: server-side rerank must never resurrect a
 * client-side key requirement).
 *
 * <p>Clones {@link Bge768Embedder}'s substrate (DJL HuggingFace tokenizer +
 * onnxruntime-java {@link OrtSession}) for {@code cross-encoder/ms-marco-MiniLM-L-6-v2}
 * (~91MB fp32 ONNX): PAIR-encode {@code (query, document)}, feed
 * {@code input_ids}/{@code attention_mask} (+ {@code token_type_ids} iff the
 * model declares it), read the classifier head's logits — one raw score per
 * document, higher = more relevant (no sigmoid; ordering is all the fused
 * stage consumes). Mirrors the retiring Python {@code LocalCrossEncoder}
 * recipe ({@code cross_encoder.py}) with one deliberate difference: pairs are
 * truncated at {@value #MAX_SEQ_LEN} tokens (BERT's position-embedding bound)
 * instead of trusting every chunk to be short.
 *
 * <p><b>Lazy init, typed failure.</b> Unlike {@link Bge768Embedder} (local
 * mode's ONLY embedder — a missing model is rightly fatal at boot), the
 * reranker is optional capability: construction touches no I/O, and a missing
 * or unloadable artifact surfaces on first use as a typed
 * {@link RerankUpstreamException} naming the provisioning remedy — so the
 * fused stage degrades LOUD per request instead of failing engine boot, and an
 * engine that started before {@code nx init} provisioned the model picks it up
 * on the next rerank without a restart.
 *
 * <p>Model artifact: the OFFICIAL {@code cross-encoder/ms-marco-MiniLM-L-6-v2}
 * {@code onnx/model.onnx} export — standard ops, verified to run on
 * onnxruntime-java by {@code CrossEncoderRerankerInferenceTest} (the bge
 * lesson, RDR-160 CA-1: fused exports don't). The CLI provisions it to
 * {@link #DEFAULT_MODEL_PATH} (the client is the network-facing side; the
 * local service makes no outbound HTTP — same topology invariant as bge).
 *
 * <p>Thread-safe: {@link OrtSession} and {@link HuggingFaceTokenizer} are
 * documented thread-safe for inference/encode; init is double-checked-locked.
 */
public final class CrossEncoderReranker implements Reranker {

    private static final Logger log = LoggerFactory.getLogger(CrossEncoderReranker.class);

    /** Canonical Java-read path (provisioned by the CLI, mirroring the bge flow). */
    public static final String DEFAULT_MODEL_PATH =
            System.getProperty("user.home")
            + "/.cache/nexus/onnx_models/ms-marco-minilm-l6-v2/onnx/model.onnx";

    public static final String DEFAULT_TOKENIZER_PATH =
            System.getProperty("user.home")
            + "/.cache/nexus/onnx_models/ms-marco-minilm-l6-v2/onnx/tokenizer.json";

    /** Same request-sanity cap as {@link VoyageReranker}: reject, never truncate. */
    public static final int MAX_DOCS_PER_REQUEST = 1000;

    /** BERT position-embedding bound for the (query, document) pair. */
    private static final int MAX_SEQ_LEN = 512;

    private final String modelPath;
    private final String tokenizerPath;

    private final Object initLock = new Object();
    private volatile boolean initialized;
    private OrtEnvironment       ortEnv;
    private OrtSession           session;
    private HuggingFaceTokenizer tokenizer;
    private boolean              wantsTokenTypeIds;

    /** Construct with the canonical artifact paths. Touches no I/O (lazy init). */
    public CrossEncoderReranker() {
        this(DEFAULT_MODEL_PATH, DEFAULT_TOKENIZER_PATH);
    }

    /** Construct with explicit paths (testing / non-default provisioning). Touches no I/O. */
    public CrossEncoderReranker(String modelPath, String tokenizerPath) {
        this.modelPath = modelPath;
        this.tokenizerPath = tokenizerPath;
    }

    @Override
    public String modelToken() {
        return "ms-marco-minilm-l6-v2";
    }

    @Override
    public List<Scored> rerank(String query, List<String> documents, Integer topK) {
        if (query == null || query.isBlank()) {
            throw new IllegalArgumentException("rerank query must be non-blank");
        }
        if (topK != null && topK <= 0) {
            throw new IllegalArgumentException("rerank top_k must be positive, got " + topK);
        }
        if (documents == null || documents.isEmpty()) {
            return List.of();
        }
        if (documents.size() > MAX_DOCS_PER_REQUEST) {
            throw new IllegalArgumentException(
                    "rerank request has " + documents.size() + " documents; the local cross-encoder"
                    + " cap is " + MAX_DOCS_PER_REQUEST + " — refusing to silently truncate.");
        }
        ensureInitialized();
        try {
            float[] logits = scoreBatch(query, documents);
            return toScored(logits, topK);
        } catch (RerankUpstreamException e) {
            throw e;
        } catch (Exception e) {
            throw new RerankUpstreamException(
                    "local cross-encoder scoring failed: " + e.getMessage(), e);
        }
    }

    // ── Lazy init ─────────────────────────────────────────────────────────────

    private void ensureInitialized() {
        if (initialized) return;
        synchronized (initLock) {
            if (initialized) return;
            for (String[] req : new String[][]{
                    {modelPath, "cross-encoder ONNX model"}, {tokenizerPath, "cross-encoder tokenizer"}}) {
                if (!Files.isRegularFile(Path.of(req[0]))) {
                    throw new RerankUpstreamException(
                            "local cross-encoder unavailable: " + req[1] + " not found at " + req[0]
                            + ". Provision the ms-marco-MiniLM-L-6-v2 artifacts via `nx init` /"
                            + " `nx upgrade` (RDR-188 P1.3), or set -Dnexus.crossencoder.modelPath"
                            + " / -Dnexus.crossencoder.tokenizerPath.");
                }
            }
            OrtSession          sess = null;
            HuggingFaceTokenizer tok = null;
            try (var sessionOpts = new OrtSession.SessionOptions()) {
                OrtEnvironment env = OrtEnvironment.getEnvironment();
                sess = env.createSession(modelPath, sessionOpts);
                tok = HuggingFaceTokenizer.builder()
                        .optTokenizerPath(Path.of(tokenizerPath))
                        .optMaxLength(MAX_SEQ_LEN)
                        .optTruncation(true)
                        .optPadding(false)   // padded per batch in scoreBatch()
                        .build();
                this.ortEnv = env;
                this.session = sess;
                this.tokenizer = tok;
                this.wantsTokenTypeIds = sess.getInputNames().contains("token_type_ids");
                this.initialized = true;
                log.info("event=cross_encoder_reranker_loaded model={} tokenizer={} token_type_ids={}",
                        modelPath, tokenizerPath, wantsTokenTypeIds);
            } catch (Exception e) {
                if (tok != null)  { try { tok.close();  } catch (Exception ignored) {} }
                if (sess != null) { try { sess.close(); } catch (Exception ignored) {} }
                throw new RerankUpstreamException(
                        "local cross-encoder failed to initialise from " + modelPath + ": "
                        + e.getMessage() + ". Re-provision via `nx init` (the artifact may be"
                        + " truncated or a fused export onnxruntime-java cannot run).", e);
            }
        }
    }

    // ── Inference ─────────────────────────────────────────────────────────────

    private float[] scoreBatch(String query, List<String> documents) throws Exception {
        int batchSize = documents.size();

        Encoding[] encodings = new Encoding[batchSize];
        int maxLen = 0;
        for (int i = 0; i < batchSize; i++) {
            // PAIR encoding — [CLS] query [SEP] document [SEP] — the cross-encoder
            // input contract (mirrors the Python client's (query, doc) encode_batch).
            encodings[i] = tokenizer.encode(query, documents.get(i));
            maxLen = Math.max(maxLen, (int) encodings[i].getIds().length);
        }
        maxLen = Math.min(maxLen, MAX_SEQ_LEN);
        maxLen = Math.max(maxLen, 1);

        long[] inputIdsFlat      = new long[batchSize * maxLen];
        long[] attentionMaskFlat = new long[batchSize * maxLen];
        long[] tokenTypeIdsFlat  = new long[batchSize * maxLen];

        for (int i = 0; i < batchSize; i++) {
            long[] ids   = encodings[i].getIds();
            long[] mask  = encodings[i].getAttentionMask();
            long[] types = encodings[i].getTypeIds();
            int seqLen = Math.min(ids.length, maxLen);
            int offset = i * maxLen;
            for (int j = 0; j < seqLen; j++) {
                inputIdsFlat[offset + j]      = ids[j];
                attentionMaskFlat[offset + j] = mask[j];
                tokenTypeIdsFlat[offset + j]  = types[j];
            }
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
                tokenTypeIdsTensor = OnnxTensor.createTensor(ortEnv, LongBuffer.wrap(tokenTypeIdsFlat), shape);
                inputs.put("token_type_ids", tokenTypeIdsTensor);
            }

            try (OrtSession.Result result = session.run(inputs)) {
                return flattenLogits(result.get(0).getValue(), batchSize);
            }
        } finally {
            if (inputIdsTensor != null)      inputIdsTensor.close();
            if (attentionMaskTensor != null) attentionMaskTensor.close();
            if (tokenTypeIdsTensor != null)  tokenTypeIdsTensor.close();
        }
    }

    /**
     * Flatten the classifier head's output to one logit per document.
     *
     * <p>MS-MARCO cross-encoder exports declare {@code logits} as {@code (batch, 1)}
     * or {@code (batch,)} (the Python client reshapes {@code (-1)} for the same
     * reason). Anything else — a multi-class head, a wrong cardinality — degrades
     * LOUD and typed, never gets silently reinterpreted as relevance scores.
     */
    // Package-private: model-free unit seam (CrossEncoderRerankerTest).
    static float[] flattenLogits(Object value, int expectedCount) {
        float[] flat;
        if (value instanceof float[][] twoD) {
            flat = new float[twoD.length];
            for (int i = 0; i < twoD.length; i++) {
                if (twoD[i].length != 1) {
                    throw new RerankUpstreamException(
                            "cross-encoder output row " + i + " has width " + twoD[i].length
                            + " (expected 1 logit per document)");
                }
                flat[i] = twoD[i][0];
            }
        } else if (value instanceof float[] oneD) {
            flat = oneD;
        } else {
            throw new RerankUpstreamException(
                    "cross-encoder output has unexpected type "
                    + (value == null ? "null" : value.getClass().getName()));
        }
        if (flat.length != expectedCount) {
            throw new RerankUpstreamException(
                    "cross-encoder returned " + flat.length + " scores for "
                    + expectedCount + " documents");
        }
        return flat;
    }

    /** Raw logits → {@link Scored} ordered by score descending, {@code topK}-truncated. */
    // Package-private: model-free unit seam (CrossEncoderRerankerTest).
    static List<Scored> toScored(float[] logits, Integer topK) {
        List<Scored> out = new ArrayList<>(logits.length);
        for (int i = 0; i < logits.length; i++) {
            out.add(new Scored(i, logits[i]));
        }
        out.sort(Comparator.comparingDouble(Scored::relevanceScore).reversed());
        return (topK != null && out.size() > topK) ? out.subList(0, topK) : out;
    }

    /** Release the native session/tokenizer (tests; production holds process lifetime). */
    public void close() {
        synchronized (initLock) {
            if (!initialized) return;
            try { session.close();   } catch (Exception ignored) {}
            try { tokenizer.close(); } catch (Exception ignored) {}
            // ortEnv is a JVM-level singleton; do not close it
            initialized = false;
        }
    }
}
