// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.List;

/**
 * RDR-152 bead nexus-gmiaf.21 — Routes embedding requests to the correct embedder
 * based on collection name prefix, mirroring Python's {@code t3.py} routing.
 *
 * <p>Python routing (t3.py § _embedding_fn and _cce_embed):
 * <ul>
 *   <li>{@code knowledge__}, {@code docs__}, {@code rdr__} → CCE via
 *       {@code voyageai.Client.contextualized_embed} (model {@code voyage-context-3})</li>
 *   <li>{@code code__} → standard embed via {@code voyageai.Client.embed}
 *       (model {@code voyage-code-3})</li>
 *   <li>local mode / fallback → ONNX (all-MiniLM-L6-v2, 384d)</li>
 * </ul>
 *
 * <p>The Java service is always in one of two modes:
 * <ul>
 *   <li><strong>Local mode</strong> ({@code voyageApiKey == null}): all collections → ONNX.</li>
 *   <li><strong>Cloud mode</strong> ({@code voyageApiKey != null}): prefix-based routing above.</li>
 * </ul>
 *
 * <p>Thread-safe: all embedder instances are stateless per-call.
 */
public final class EmbedderRouter implements Embedder {

    private static final Logger log = LoggerFactory.getLogger(EmbedderRouter.class);

    /** Collection prefixes that use CCE (voyage-context-3). */
    private static final List<String> CCE_PREFIXES =
            List.of("knowledge__", "docs__", "rdr__");

    /** Collection prefix that uses standard Voyage embedding (voyage-code-3). */
    private static final String CODE_PREFIX = "code__";

    private final OnnxEmbedder   onnxEmbedder;
    private final VoyageEmbedder voyageCodeEmbedder;    // voyage-code-3, null in local mode
    private final CceEmbedder    cceEmbedder;           // voyage-context-3, null in local mode
    private final String         inputType;             // "document" or "query"

    /**
     * Local-mode constructor: all collections embedded via ONNX.
     *
     * @param onnxEmbedder the ONNX embedder instance
     * @param inputType    {@code "document"} for indexing, {@code "query"} for search
     */
    public EmbedderRouter(OnnxEmbedder onnxEmbedder, String inputType) {
        this.onnxEmbedder        = onnxEmbedder;
        this.voyageCodeEmbedder  = null;
        this.cceEmbedder         = null;
        this.inputType           = inputType;
    }

    /**
     * Cloud-mode constructor: routes by collection prefix.
     *
     * @param onnxEmbedder  ONNX fallback (used when collection prefix is unrecognised)
     * @param voyageApiKey  Voyage AI API key
     * @param inputType     {@code "document"} or {@code "query"}
     */
    public EmbedderRouter(OnnxEmbedder onnxEmbedder, String voyageApiKey, String inputType) {
        this.onnxEmbedder       = onnxEmbedder;
        this.voyageCodeEmbedder = new VoyageEmbedder(voyageApiKey, "voyage-code-3", inputType);
        this.cceEmbedder        = new CceEmbedder(voyageApiKey, inputType);
        this.inputType          = inputType;
    }

    /**
     * Embed texts for a specific collection — picks the correct embedder by prefix.
     *
     * @param collection collection name (four-segment conformant), used for routing
     * @param texts      texts to embed
     * @return embedding vectors aligned with input
     */
    public List<float[]> embedForCollection(String collection, List<String> texts) {
        Embedder embedder = resolveEmbedder(collection);
        log.debug("event=embed_router collection={} embedder={} count={}",
                collection, embedder.getClass().getSimpleName(), texts.size());
        return embedder.embed(texts);
    }

    /**
     * Default {@link Embedder#embed} — uses ONNX (local mode default).
     * Prefer {@link #embedForCollection} when a collection name is available.
     */
    @Override
    public List<float[]> embed(List<String> texts) {
        return onnxEmbedder.embed(texts);
    }

    /**
     * Embed a single text for a specific collection.
     */
    public float[] embedOneForCollection(String collection, String text) {
        return embedForCollection(collection, List.of(text)).get(0);
    }

    /**
     * Embed texts for a collection, preserving full double (float64) precision.
     *
     * <p>Used by the parity gate ({@code /v1/vectors/embed}) to avoid the float32
     * round-trip that causes cosine ≈ 0.9999669 instead of 1.0 exactly.
     *
     * <p>For ONNX (float32 output), converts float32 → double exactly.
     * For Voyage/CCE, calls the embedder's {@code embedDouble} method to preserve
     * the original JSON double values without float32 truncation.
     */
    public List<double[]> embedDoubleForCollection(String collection, List<String> texts) {
        Embedder embedder = resolveEmbedder(collection);
        log.debug("event=embed_double_router collection={} embedder={} count={}",
                collection, embedder.getClass().getSimpleName(), texts.size());

        if (embedder instanceof VoyageEmbedder ve) {
            return ve.embedDouble(texts);
        }
        if (embedder instanceof CceEmbedder ce) {
            return ce.embedDouble(texts);
        }
        // ONNX: float32 → double is exact (no precision loss)
        List<float[]> floatVecs = embedder.embed(texts);
        List<double[]> result = new ArrayList<>(floatVecs.size());
        for (float[] fv : floatVecs) {
            double[] dv = new double[fv.length];
            for (int i = 0; i < fv.length; i++) dv[i] = fv[i];
            result.add(dv);
        }
        return result;
    }

    /**
     * Resolve the appropriate embedder for a collection name.
     *
     * <p>Returns the CCE embedder for CCE prefix collections (in cloud mode),
     * the standard Voyage embedder for {@code code__} (in cloud mode), or the
     * ONNX embedder for everything else / local mode.
     */
    public Embedder resolveEmbedder(String collection) {
        if (voyageCodeEmbedder == null) {
            // Local mode: always ONNX
            return onnxEmbedder;
        }
        // Cloud mode: route by prefix
        if (collection != null) {
            for (String prefix : CCE_PREFIXES) {
                if (collection.startsWith(prefix)) {
                    return cceEmbedder;
                }
            }
            if (collection.startsWith(CODE_PREFIX)) {
                return voyageCodeEmbedder;
            }
        }
        // Unrecognised prefix — fall back to ONNX (safe, logged)
        log.warn("event=embed_router_fallback collection={} fallback=onnx", collection);
        return onnxEmbedder;
    }

    @Override
    public void close() {
        try { onnxEmbedder.close();       } catch (Exception ignored) {}
        // VoyageEmbedder and CceEmbedder are stateless HTTP clients; no close needed
    }
}
