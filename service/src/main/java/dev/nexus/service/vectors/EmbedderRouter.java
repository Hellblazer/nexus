// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

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
 *   <li>local mode → the local ONNX-runtime embedder (RDR-160: bge-base-en-v1.5,
 *       768d); cloud-mode non-conformant prefix fallback → the same injected
 *       local embedder</li>
 * </ul>
 *
 * <p>The Java service is always in one of two modes:
 * <ul>
 *   <li><strong>Local mode</strong> ({@code voyageApiKey == null}): all collections
 *       → the injected local embedder (RDR-160 wires bge-768).</li>
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

    /**
     * Local-mode embedder (RDR-160: a {@link Bge768Embedder}); also the
     * cloud-mode fallback for non-conformant prefixes. Typed to the
     * {@link Embedder} interface so the local model can change (MiniLM → bge-768)
     * without a signature churn — the MiniLM {@link OnnxEmbedder} stays a valid
     * argument, it is simply no longer what production local mode passes.
     */
    private final Embedder       localEmbedder;
    private final VoyageEmbedder voyageCodeEmbedder;    // voyage-code-3, null in local mode
    private final CceEmbedder    cceEmbedder;           // voyage-context-3, null in local mode
    private final String         inputType;             // "document" or "query"

    /**
     * RDR-103 model-segment → embedder dispatch table (bead nexus-pebfx.2).
     * Built per mode at construction; the collection name's model segment is
     * the authority for conformant names — prefix routing is the fallback for
     * non-conformant names only. A conformant collection whose model token is
     * absent here is REFUSED ({@link EmbeddingModelUnavailableException}),
     * never silently embedded with a different model.
     */
    private final Map<String, Embedder> modelEmbedders;

    /**
     * Local-mode constructor: all collections embedded via the injected local
     * embedder. RDR-160 wires a {@link Bge768Embedder} here; the dispatch table
     * is self-keyed by {@link Embedder#modelToken()}, so a collection whose model
     * segment is not this embedder's token is REFUSED (no silent fallback).
     *
     * @param localEmbedder the local ONNX-runtime embedder (production: bge-768)
     * @param inputType     {@code "document"} for indexing, {@code "query"} for search
     */
    public EmbedderRouter(Embedder localEmbedder, String inputType) {
        this.localEmbedder       = localEmbedder;
        this.voyageCodeEmbedder  = null;
        this.cceEmbedder         = null;
        this.inputType           = inputType;
        this.modelEmbedders      = Map.of(localEmbedder.modelToken(), localEmbedder);
    }

    /**
     * Cloud-mode constructor: routes by collection model segment (prefix for
     * non-conformant names).
     *
     * @param onnxEmbedder  ONNX fallback (used when collection prefix is unrecognised)
     * @param voyageApiKey  Voyage AI API key
     * @param inputType     {@code "document"} or {@code "query"}
     */
    public EmbedderRouter(OnnxEmbedder onnxEmbedder, String voyageApiKey, String inputType) {
        this.localEmbedder      = onnxEmbedder;
        this.voyageCodeEmbedder = new VoyageEmbedder(voyageApiKey, "voyage-code-3", inputType);
        this.cceEmbedder        = new CceEmbedder(voyageApiKey, inputType);
        this.inputType          = inputType;
        // voyage-3 gets its own standard-embed instance: prefix routing sent it
        // to CCE (voyage-context-3) — the same-dim wrong-model contamination
        // hole this dispatch table closes. Every entry is self-keyed by the
        // embedder's own modelToken() so a key can never drift from the
        // identity actually dispatched.
        VoyageEmbedder voyage3 = new VoyageEmbedder(voyageApiKey, "voyage-3", inputType);
        this.modelEmbedders     = Map.of(
                onnxEmbedder.modelToken(),       onnxEmbedder,
                voyageCodeEmbedder.modelToken(), voyageCodeEmbedder,
                cceEmbedder.modelToken(),        cceEmbedder,
                voyage3.modelToken(),            voyage3);
    }

    /** Embedding mode for banners and refusal messages. */
    public String modeName() {
        return voyageCodeEmbedder == null ? "onnx-local" : "voyage";
    }

    /** Model tokens this router can embed for (sorted, for stable banner output). */
    public List<String> availableModels() {
        return modelEmbedders.keySet().stream().sorted().toList();
    }

    /**
     * Embed texts for a specific collection — picks the correct embedder by prefix.
     *
     * @param collection collection name (four-segment conformant), used for routing
     * @param texts      texts to embed
     * @return embedding vectors aligned with input
     */
    public List<float[]> embedForCollection(String collection, List<String> texts) {
        Embedder embedder = resolveEmbedderStrict(collection);
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
        return localEmbedder.embed(texts);
    }

    /**
     * Embed a single text for a specific collection.
     */
    public float[] embedOneForCollection(String collection, String text) {
        return embedForCollection(collection, List.of(text)).get(0);
    }

    /**
     * Embed texts for a specific collection and return both the vectors and the
     * token count consumed by the embedding call (bead nexus-ehc4q).
     *
     * <p>Routes to the same embedder as {@link #embedForCollection}, then
     * delegates to {@link Embedder#embedWithUsage} to capture the token count.
     * No second embed call — the token count comes from the same API response.
     *
     * @param collection collection name (four-segment conformant), used for routing
     * @param texts      texts to embed
     * @return {@link EmbedResult} carrying vectors aligned with input and the token count
     */
    public EmbedResult embedForCollectionWithUsage(String collection, List<String> texts) {
        Embedder embedder = resolveEmbedderStrict(collection);
        log.debug("event=embed_router_with_usage collection={} embedder={} count={}",
                collection, embedder.getClass().getSimpleName(), texts.size());
        return embedder.embedWithUsage(texts);
    }

    /**
     * Embed a single text for a specific collection, returning the vector and token count
     * (bead nexus-ehc4q). Convenience wrapper over {@link #embedForCollectionWithUsage}.
     */
    public EmbedResult embedOneForCollectionWithUsage(String collection, String text) {
        return embedForCollectionWithUsage(collection, List.of(text));
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
        Embedder embedder = resolveEmbedderStrict(collection);
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
     * Strict, model-segment-authoritative resolution (bead nexus-pebfx.2).
     *
     * <p>For a four-segment conformant name, the RDR-103 model segment decides
     * the embedder — never the prefix. Model identity is validated by
     * construction: the dispatched embedder's {@link Embedder#modelToken()}
     * keys the table, so a same-dimension wrong-model embed cannot happen.
     * A conformant name whose model token has no embedder in this mode is
     * REFUSED loudly instead of silently embedded with the wrong model
     * (no-silent-fallbacks-for-correctness):
     * <ul>
     *   <li>onnx-local mode + {@code voyage-*} segment → refuse (no credentials)
     *   <li>any mode + a segment with no embedder wired in this mode → refuse
     *       (e.g. {@code minilm-l6-v2-384} on the RDR-160 bge-768 local service,
     *       or {@code bge-base-en-v15-768} in a MiniLM-wired test router)
     *   <li>cloud mode + {@code minilm-l6-v2-384} segment → ONNX (the segment
     *       is the authority; prefix routing wrongly sent these to CCE)
     * </ul>
     *
     * <p>Non-conformant names keep legacy {@link #resolveEmbedder prefix
     * routing} (test fixtures and the parity-gate /embed endpoint).
     *
     * @throws EmbeddingModelUnavailableException when a conformant collection's
     *         model segment cannot be served in the current mode (→ HTTP 422)
     */
    public Embedder resolveEmbedderStrict(String collection) {
        if (collection != null) {
            String[] segments = collection.split("__");
            if (segments.length == 4) {
                Embedder embedder = modelEmbedders.get(segments[2]);
                if (embedder == null) {
                    throw new EmbeddingModelUnavailableException(
                        "service (embedding mode " + modeName() + ") has no embedder for "
                        + "model '" + segments[2] + "' — refusing to embed collection '"
                        + collection + "' with a different model. Available models: "
                        + availableModels()
                        + ("onnx-local".equals(modeName())
                           ? ". Voyage collections need NX_VOYAGE_API_KEY in the service "
                             + "environment (supervisor plumbs it from the nexus credential "
                             + "chain when set)."
                           : "."));
                }
                return embedder;
            }
        }
        return resolveEmbedder(collection);
    }

    /**
     * Resolve the appropriate embedder for a collection name by PREFIX.
     *
     * <p>Returns the CCE embedder for CCE prefix collections (in cloud mode),
     * the standard Voyage embedder for {@code code__} (in cloud mode), or the
     * ONNX embedder for everything else / local mode.
     *
     * <p>Legacy fallback for non-conformant names only — production embed
     * paths go through {@link #resolveEmbedderStrict} (model segment is the
     * authority for conformant names).
     */
    public Embedder resolveEmbedder(String collection) {
        if (voyageCodeEmbedder == null) {
            // Local mode: the single injected local embedder (RDR-160: bge-768)
            return localEmbedder;
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
        // Unrecognised prefix — fall back to the local embedder (safe, logged)
        log.warn("event=embed_router_fallback collection={} fallback=local", collection);
        return localEmbedder;
    }

    @Override
    public void close() {
        try { localEmbedder.close();      } catch (Exception ignored) {}
        // VoyageEmbedder and CceEmbedder are stateless HTTP clients; no close needed
    }
}
