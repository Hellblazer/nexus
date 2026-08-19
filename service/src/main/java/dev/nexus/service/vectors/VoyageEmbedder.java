// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-152 bead nexus-gmiaf.20 — CLOUD Voyage AI standard embedder (voyage-code-3).
 *
 * <p>Mirrors the production Python path EXACTLY:
 * Production uses {@code chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction(
 * model_name=model, api_key=key)} with default {@code input_type=None} and
 * {@code truncation=True}.
 *
 * <p>CRITICAL: The Python voyageai SDK uses {@code encoding_format="base64"} by default
 * (see {@code voyageai.Embedding.create}).  Base64 responses contain the raw float32 binary
 * representation, decoded via {@code np.frombuffer(b64decode(embedding), np.float32)}.
 * This gives EXACT float32 bit patterns.  JSON float responses are decimal approximations
 * that differ by up to 23 ULPs from the true float32.  Java must also use base64 to get
 * bit-identical float32 values.
 *
 * <ul>
 *   <li>{@code encoding_format: "base64"} — matches Python SDK default, gives exact float32</li>
 *   <li>{@code input_type: null}, {@code output_dtype: null}, {@code output_dimension: null}
 *       — sent EXPLICITLY: the Python SDK serializes the unset params as JSON nulls, it
 *       does NOT omit them (captured wire body, voyageai 0.3.7, nexus-f4wcg 2026-07-07)</li>
 *   <li>{@code truncation: true} — the API default, sent explicitly for byte parity with the
 *       SDK. The earlier "omitting gives cosine ≈ 0.99995 drift" rationale is NOT supported:
 *       omission is semantically identical to {@code true}, and a toggle-only probe (RDR-195
 *       spike, nexus-kmtlp.1, 2026-08-19; 3000-char text, 3x each) put omitted-vs-true pairs
 *       at 0.99998..1.0, the same band as true-vs-true repeats — i.e. the repeat-variance band
 *       described in the next item, not a flag effect.</li>
 *   <li>Request body is BYTE-faithful to the Python SDK's (key order, {@code ", "}/{@code ": "}
 *       separators, ensure_ascii escaping): Voyage can differ across
 *       byte-different-but-semantically-equal bodies by ~4e-5 cosine (region-dependent;
 *       broke the linux parity gate while macOS stayed bit-exact, nexus-f4wcg). Byte identity
 *       keeps both legs on the same serving identity. Scope of that stability (measured
 *       2026-08-19, nexus-kmtlp.1, T2 {@code nexus_rdr/195-research-3}): byte-identical SINGLE
 *       short-input requests repeat bit-exact (20/20); ~2 KB single inputs and any BATCHED
 *       request repeat across 1-2 discrete variants, up to ~5e-5 cosine for 2-125 inputs and
 *       down to 0.99978 for a 263-input/101K-token near-ceiling batch. Batch composition was
 *       never seen to move a vector outside that repeat-variance band. Any live-Voyage equality
 *       assertion on batched output must tolerate ~2e-4 cosine, not demand bit-equality.</li>
 *   <li>Sort response {@code data[]} by {@code index} (API may return out-of-order)</li>
 *   <li>Retry on 429 / 5xx with exponential backoff (max 3 attempts)</li>
 * </ul>
 *
 * <p>REST endpoint: {@code POST https://api.voyageai.com/v1/embeddings}
 * Headers: {@code Authorization: Bearer <key>}, {@code Content-Type: application/json}.
 *
 * <p><b>RDR-195 — token-aware sub-batch planning.</b> Every bound upstream of this class
 * was historically a row count; Voyage's actual limit is a per-request token ceiling
 * ({@code MAX_BATCH_ESTIMATED_TOKENS_BY_MODEL}, resolved per {@link #model} with a
 * fail-safe default) that no layer here previously respected. {@link #embed},
 * {@link #embedWithUsage}, and {@link #embedDouble} now route every call through
 * {@link #planBatches}, a greedy sub-batch planner mirroring
 * {@link Bge768Embedder#embedSubBatched} in shape (never re-orders, never emits an empty
 * batch, degrades to exactly one request when the whole input already fits). A batch that
 * still trips Voyage's {@code TOO_MANY_TOKENS_IN_BATCH} 400 (the estimator is provisional
 * by design — see the RDR's Decision Rationale) is caught and adaptively halved by
 * {@link #collectWithHalving}, bounded by {@link #maxSubRequestsPerBatch} so no single
 * input can turn one logical embed into unbounded upstream spend. {@code buildJson} itself
 * is untouched: each sub-batch is serialized by the exact same byte-contract method,
 * whether or not splitting ever engages.
 *
 * <p>Stateless: each {@link #embed} call is independent.  Thread-safe.
 */
public final class VoyageEmbedder implements Embedder {

    private static final Logger log = LoggerFactory.getLogger(VoyageEmbedder.class);

    private static final String VOYAGE_URL = "https://api.voyageai.com/v1/embeddings";
    private static final int    MAX_RETRIES = 3;
    private static final long   RETRY_BASE_MS = 500L;

    /**
     * Voyage {@code /v1/embeddings} hard input-count cap (Voyage AI docs, verified via
     * Context7 during RDR-195 research). Requests above it are rejected, never silently
     * truncated — mirrors {@link VoyageReranker#MAX_DOCS_PER_REQUEST}. The 1,000-input cap
     * is never the binding constraint in practice: the Python client already pages at 300
     * chunks (RDR-195 Problem Statement); the binding constraint is the TOKEN ceiling below.
     */
    static final int MAX_BATCH_TEXTS = 1000;

    /**
     * Per-model documented Voyage {@code /v1/embeddings} token-per-request ceiling
     * (RDR-195 Technical Design, verified against Voyage AI docs via Context7). Two tiers
     * exist among the models this class is ever instantiated with ({@link EmbedderRouter}
     * uses {@code voyage-code-3} and {@code voyage-3}): 120,000 for {@code voyage-code-3}
     * (and the other 120K-tier models Voyage documents), 320,000 for the
     * {@code voyage-3.5}/{@code voyage-2} tier. {@code voyage-3} deliberately does NOT
     * appear here — Voyage's current published limit table does not cover it at all — so it
     * falls through to {@link #DEFAULT_MAX_BATCH_ESTIMATED_TOKENS} below, the conservative
     * direction (over-split costs round trips; under-split costs a failure).
     */
    private static final Map<String, Long> MAX_BATCH_ESTIMATED_TOKENS_BY_MODEL = Map.of(
            "voyage-code-3", 120_000L,
            "voyage-3-large", 120_000L,
            "voyage-finance-2", 120_000L,
            "voyage-law-2", 120_000L,
            "voyage-3.5", 320_000L,
            "voyage-2", 320_000L
    );

    /**
     * Fail-safe default token budget for any {@link #model} absent from
     * {@link #MAX_BATCH_ESTIMATED_TOKENS_BY_MODEL} — the TIGHTEST documented ceiling, so an
     * unknown model (including {@code voyage-3}) can only ever be over-split, never
     * under-split. Deliberately not tuned per-model when the model is unknown: guessing
     * a looser ceiling for an undocumented model risks the exact failure this RDR fixes.
     */
    private static final long DEFAULT_MAX_BATCH_ESTIMATED_TOKENS = 120_000L;

    /**
     * PROVISIONAL bytes-per-token divisor for {@link #estimateTokens}. No JVM-native Voyage
     * BPE tokenizer exists (RDR-195 research; the only tokenizer available is the Python
     * SDK's), so this is a starting estimate for English/code UTF-8 text, explicitly
     * self-correcting: because {@link #collectWithHalving} adaptively halves on an actual
     * Voyage 400, a wrong divisor only ever costs extra round trips (a throughput/cost
     * defect, measured during the MVV per the RDR's Performance Expectations), never a
     * failed index. Recalibrate this constant if that measurement shows steady-state
     * adaptive splits (divisor too optimistic) or a request count far above the
     * theoretical minimum on the common path (divisor too pessimistic).
     */
    private static final long PROVISIONAL_BYTES_PER_TOKEN = 4L;

    /**
     * Hard ceiling on adaptive-halving sub-requests per top-level {@link #embed}/
     * {@link #embedWithUsage}/{@link #embedDouble} call (RDR-195 gate remediation,
     * 2026-08-19). Derivation: halving terminates in at most {@code ceil(log2(n))} levels
     * for any single planned sub-batch, and — per the RDR's "single pathological item"
     * analysis — a batch with one over-budget chunk among many costs about
     * {@code 2 * log2(n)} total sub-requests (one failure plus one success per level) even
     * across the WHOLE original input. {@code n} is bounded by {@link #MAX_BATCH_TEXTS}
     * (1,000), so {@code 2 * log2(1000) ≈ 20}; 64 gives more than 3x that margin — wide
     * enough that it can only fire on adversarial input (e.g. a pathologically tiny
     * per-model budget or many simultaneously-oversize chunks), never on ordinary
     * calibration drift in {@link #PROVISIONAL_BYTES_PER_TOKEN}. On exhaustion the embed
     * fails with {@link VoyageTooManyTokensException} carrying the attempted sub-request
     * count and the offending sub-batch's size — the same loud, no-silent-partial-result
     * path as the single-unsplittable-text rethrow.
     */
    static final int MAX_SUB_REQUESTS_PER_BATCH = 64;

    private final String     apiKey;
    private final String     model;
    private final String     url;
    private final long       retryBaseMs;
    private final HttpClient http;
    private final ObjectMapper mapper;

    /**
     * Non-vacuity test instrument (RDR-195, mirrors {@link Bge768Embedder#onnxInvocationCount}):
     * counts every REAL Voyage POST this instance has sent (including internal 429/5xx
     * retries within a single {@link #callApi} call), since construction or the last
     * {@link #resetVoyageRequestCount()}. This is how {@code VoyageEmbedderBatchSplitTest}
     * proves a split actually happened rather than merely not throwing.
     */
    private final AtomicInteger voyageRequestCount = new AtomicInteger(0);

    /**
     * Mutable only for exhaustion testing (RDR-195 test scenario 10, package-private test
     * seam via {@link #setMaxSubRequestsPerBatchForTest}): defaults to
     * {@link #MAX_SUB_REQUESTS_PER_BATCH}. Forcing it low lets a test reach exhaustion
     * without needing 64 real halving levels of fake-server scripting.
     */
    private int maxSubRequestsPerBatch = MAX_SUB_REQUESTS_PER_BATCH;

    /**
     * @param apiKey Voyage AI API key
     * @param model  e.g. {@code "voyage-code-3"}
     * @param inputType ignored — retained for API compatibility but never sent (production
     *                  omits input_type by using {@code input_type=None})
     */
    public VoyageEmbedder(String apiKey, String model, String inputType) {
        this(apiKey, model, VOYAGE_URL, RETRY_BASE_MS, EgressProxy.selector());
        // inputType deliberately NOT stored: production VoyageAIEmbeddingFunction
        // always passes input_type=None (field omitted from request), matching what
        // the Voyage API uses as its "unspecified" default.
    }

    /**
     * Full wiring, the single build path (RDR-195 test seam, mirrors
     * {@link VoyageReranker#VoyageReranker(String, String, String, long, Optional)}): tests
     * inject a fake upstream URL, a fast retry base, and {@code Optional.empty()} so an
     * ambient {@code HTTPS_PROXY} can never route the localhost upstream. Production uses
     * the 3-arg constructor above. Before this constructor existed, {@code VoyageEmbedder}
     * had no injectable URL and no fake-server test of this class was possible at all.
     */
    public VoyageEmbedder(String apiKey, String model, String url, long retryBaseMs,
                          Optional<ProxySelector> proxy) {
        this.apiKey  = apiKey;
        this.model   = model;
        this.url     = url;
        this.retryBaseMs = retryBaseMs;
        // nexus-... egress proxy: java.net.http.HttpClient ignores https.proxyHost
        // system properties unless a proxy is set explicitly on the client. The cloud
        // deploy routes api.voyageai.com through squid (private subnet has no NAT), so
        // set the proxy from env (HTTPS_PROXY / NX_HTTPS_PROXY); absent = direct.
        var builder = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10));
        proxy.ifPresent(builder::proxy);
        this.http = builder.build();
        this.mapper = new ObjectMapper();
    }

    @Override
    public String modelToken() {
        return model;
    }

    @Override
    public List<float[]> embed(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();
        List<SubBatchResponse> responses = executeWithHalving(texts);
        try {
            List<float[]> result = new ArrayList<>(texts.size());
            for (SubBatchResponse r : responses) {
                result.addAll(parseResponseFloat(r.body()));
            }
            return result;
        } catch (Exception e) {
            throw new RuntimeException("Voyage embed parse failed", e);
        }
    }

    /**
     * Embed a batch of texts and return vectors plus the token count from
     * {@code usage.total_tokens} in the Voyage response (bead nexus-ehc4q), summed across
     * every sub-request RDR-195's planner/halving issues (mirrors
     * {@link CceEmbedder#embedWithUsage}'s multi-call accumulation).
     */
    @Override
    public EmbedResult embedWithUsage(List<String> texts) {
        if (texts == null || texts.isEmpty()) return new EmbedResult(List.of(), 0L);
        List<SubBatchResponse> responses = executeWithHalving(texts);
        try {
            List<float[]> result = new ArrayList<>(texts.size());
            long totalTokens = 0L;
            for (SubBatchResponse r : responses) {
                EmbedResult one = parseResponseWithUsage(r.body());
                result.addAll(one.embeddings());
                totalTokens += one.tokens();
            }
            return new EmbedResult(result, totalTokens);
        } catch (Exception e) {
            throw new RuntimeException("Voyage embedWithUsage parse failed", e);
        }
    }

    /**
     * Embed texts preserving full double (float64) precision for the parity gate.
     *
     * <p>The Python SDK uses base64 encoding and decodes as float32 binary.  This method
     * decodes the same base64 and promotes float32 → float64 exactly (no further precision
     * loss).  Returning float64 avoids the float32 → JSON → float64 round-trip that caused
     * cosine ≈ 0.9999669 instead of 1.0.
     */
    public List<double[]> embedDouble(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();
        List<SubBatchResponse> responses = executeWithHalving(texts);
        try {
            List<double[]> result = new ArrayList<>(texts.size());
            for (SubBatchResponse r : responses) {
                result.addAll(parseResponseDouble(r.body()));
            }
            return result;
        } catch (Exception e) {
            throw new RuntimeException("Voyage embedDouble parse failed", e);
        }
    }

    // ── RDR-195: sub-batch planning and adaptive halving ────────────────────────

    /** One request/response unit after planning and any adaptive halving: the exact
     * sub-batch of texts sent and the raw JSON body Voyage returned for it. Consecutive
     * units across a whole {@link #executeWithHalving} call cover the original input
     * contiguously and in order — {@link #planBatches} never re-orders, and {@link
     * #collectWithHalving} recurses left-half-then-right-half, so simple in-order
     * concatenation by the three {@code embed*} methods is correct. */
    private record SubBatchResponse(List<String> texts, String body) {}

    /**
     * Top-level RDR-195 entry point shared by {@link #embed}, {@link #embedWithUsage}, and
     * {@link #embedDouble}: asserts {@link #MAX_BATCH_TEXTS} (via {@link #planBatches}),
     * plans sub-batches under the per-model token budget, and executes each with adaptive
     * halving on a fresh per-call sub-request budget (an {@link AtomicInteger} local to
     * this invocation, never shared across concurrent calls on the same instance — the
     * class's thread-safety guarantee depends on that).
     */
    private List<SubBatchResponse> executeWithHalving(List<String> texts) {
        List<List<String>> planned = planBatches(texts);
        List<SubBatchResponse> out = new ArrayList<>();
        AtomicInteger subRequestBudget = new AtomicInteger(0);
        for (List<String> batch : planned) {
            collectWithHalving(batch, subRequestBudget, out);
        }
        return out;
    }

    /**
     * Greedy sub-batch planner (RDR-195), mirrors {@link Bge768Embedder#embedSubBatched}'s
     * shape: never re-orders, never emits an empty batch, a single over-budget text still
     * gets its own batch (the estimator alone cannot refuse it — only
     * {@link #collectWithHalving}'s single-text rethrow can, and only on an actual Voyage
     * 400). Degrades to exactly one batch — and therefore exactly one POST — when the whole
     * input already fits, byte-identical to the pre-RDR-195 behavior.
     *
     * @throws IllegalArgumentException more than {@link #MAX_BATCH_TEXTS} texts — refused,
     *         never silently truncated (mirrors {@link VoyageReranker#MAX_DOCS_PER_REQUEST}).
     */
    List<List<String>> planBatches(List<String> texts) {
        if (texts.size() > MAX_BATCH_TEXTS) {
            throw new IllegalArgumentException(
                    "Voyage embed request has " + texts.size() + " texts; the Voyage API cap is "
                    + MAX_BATCH_TEXTS + " — refusing to silently truncate. Trim the batch before"
                    + " embedding.");
        }
        long budget = maxBatchEstimatedTokens();
        int n = texts.size();
        List<List<String>> batches = new ArrayList<>();
        int start = 0;
        while (start < n) {
            int end = start + 1;
            long groupTokens = estimateTokens(texts.get(start));
            while (end < n) {
                long candidateTokens = groupTokens + estimateTokens(texts.get(end));
                if (candidateTokens > budget) break;
                groupTokens = candidateTokens;
                end++;
            }
            if (end - start < n) {
                log.debug("event=voyage_subbatch_planned start={} size={} estTokens={} totalBatch={} model={}",
                        start, end - start, groupTokens, n, model);
            }
            batches.add(texts.subList(start, end));
            start = end;
        }
        return batches;
    }

    /** This instance's token budget, resolved from {@link #model} against
     * {@link #MAX_BATCH_ESTIMATED_TOKENS_BY_MODEL} with the fail-safe default. */
    private long maxBatchEstimatedTokens() {
        return MAX_BATCH_ESTIMATED_TOKENS_BY_MODEL.getOrDefault(model, DEFAULT_MAX_BATCH_ESTIMATED_TOKENS);
    }

    /** PROVISIONAL token estimate: UTF-8 byte length / {@link #PROVISIONAL_BYTES_PER_TOKEN},
     * floored at 1 for any non-empty text so an all-tiny-text batch still accumulates a
     * budget instead of appearing free. See {@link #PROVISIONAL_BYTES_PER_TOKEN}'s javadoc
     * for why exactness here is not load-bearing. */
    static long estimateTokens(String text) {
        if (text == null || text.isEmpty()) return 0L;
        long byteLen = text.getBytes(StandardCharsets.UTF_8).length;
        return Math.max(byteLen / PROVISIONAL_BYTES_PER_TOKEN, 1L);
    }

    /**
     * Sends {@code batch}, catching {@link VoyageTooManyTokensException} and adaptively
     * halving (RDR-195 Technical Design: placement above {@link #callApi} keeps this split
     * path orthogonal to {@code callApi}'s own transient 429/5xx retry budget — halving
     * contributes a separate O(log) factor rather than compounding into it). Recurses
     * left-half-then-right-half so results append to {@code out} in input order. A
     * single-text batch that still trips the ceiling is DEFENSIVE — genuinely un-splittable
     * — and is rethrown with the upstream detail intact rather than silently dropped.
     */
    private void collectWithHalving(List<String> batch, AtomicInteger subRequestBudget,
                                     List<SubBatchResponse> out) {
        String body;
        try {
            body = sendSubBatch(batch, subRequestBudget);
        } catch (VoyageTooManyTokensException e) {
            if (batch.size() <= 1) {
                throw e; // un-splittable — fail loud, never silently drop the text
            }
            int mid = batch.size() / 2;
            log.warn("event=voyage_adaptive_split original_size={} left_size={} right_size={} model={}",
                    batch.size(), mid, batch.size() - mid, model);
            collectWithHalving(new ArrayList<>(batch.subList(0, mid)), subRequestBudget, out);
            collectWithHalving(new ArrayList<>(batch.subList(mid, batch.size())), subRequestBudget, out);
            return;
        }
        out.add(new SubBatchResponse(batch, body));
    }

    /**
     * Checks {@link #maxSubRequestsPerBatch} BEFORE issuing the call, so exhaustion never
     * makes a further upstream request — then serializes and sends {@code batch} via the
     * UNMODIFIED {@link #buildJson}/{@link #callApi}.
     *
     * @throws VoyageTooManyTokensException if the sub-request budget for this top-level
     *         call is exhausted, carrying the attempted count and the offending batch size
     */
    private String sendSubBatch(List<String> batch, AtomicInteger subRequestBudget) {
        int attemptNum = subRequestBudget.incrementAndGet();
        if (attemptNum > maxSubRequestsPerBatch) {
            throw new VoyageTooManyTokensException(
                    "Voyage embed exhausted the adaptive-split sub-request budget ("
                    + maxSubRequestsPerBatch + ") before this batch converged; offending"
                    + " sub-batch size=" + batch.size() + ", sub-requests already attempted="
                    + (attemptNum - 1) + ". Refusing further upstream calls rather than risking"
                    + " unbounded spend — no partial result was returned.");
        }
        String json = buildJson(batch);
        return callApi(json);
    }

    /** Test-only: number of {@code callApi()} HTTP POSTs sent since construction or the
     * last {@link #resetVoyageRequestCount()}, including internal 429/5xx retries.
     * Package-private (see {@link #voyageRequestCount}'s javadoc). */
    int voyageRequestCount() {
        return voyageRequestCount.get();
    }

    /** Test-only: zero the request counter. Package-private (see
     * {@link #voyageRequestCount}'s javadoc). */
    void resetVoyageRequestCount() {
        voyageRequestCount.set(0);
    }

    /** Test-only: force {@link #maxSubRequestsPerBatch} lower than
     * {@link #MAX_SUB_REQUESTS_PER_BATCH} so a test can reach exhaustion without scripting
     * 64 real halving levels (RDR-195 test scenario 10). Package-private. */
    void setMaxSubRequestsPerBatchForTest(int cap) {
        this.maxSubRequestsPerBatch = cap;
    }

    // ── Request / response helpers ────────────────────────────────────────────

    String buildJson(List<String> texts) {  // package-private: byte contract locked by VoyageEmbedderBodyTest
        // BYTE-faithful mirror of the production wire body (voyageai SDK 0.3.7 via
        // chromadb VoyageAIEmbeddingFunction; captured 2026-07-07, nexus-f4wcg):
        //   {"input": [...], "model": "...", "input_type": null, "truncation": true,
        //    "output_dtype": null, "output_dimension": null, "encoding_format": "base64"}
        // Python json.dumps defaults: insertion key order, ", "/" : " separators,
        // ensure_ascii (non-ASCII as backslash-u escapes). Byte identity is load-bearing, not
        // cosmetic: Voyage serves per-request stable results that can differ across
        // byte-different-but-equal bodies by ~4e-5 cosine (nexus-f4wcg linux gate).
        // RDR-195: unmodified by sub-batch planning/halving — every sub-batch is serialized
        // by this exact method, so the byte contract holds per request regardless of split.
        StringBuilder sb = new StringBuilder(128 + texts.size() * 64);
        sb.append("{\"input\": [");
        for (int i = 0; i < texts.size(); i++) {
            if (i > 0) sb.append(", ");
            sb.append(jsonString(texts.get(i)));
        }
        sb.append("], \"model\": ").append(jsonString(model))
          .append(", \"input_type\": null, \"truncation\": true, \"output_dtype\": null")
          .append(", \"output_dimension\": null, \"encoding_format\": \"base64\"}");
        return sb.toString();
    }

    /**
     * JSON string literal byte-identical to python json.dumps ensure_ascii output:
     * shorthand escapes for backslash/quote/b/f/n/r/t, LOWERCASE hex unicode escapes
     * for other controls (&lt;0x20) and everything above 0x7e (Jackson's
     * ESCAPE_NON_ASCII emits uppercase hex, which byte-diverges). UTF-16 code units
     * escape individually, so astral chars become surrogate pairs — same as python.
     */
    static String jsonString(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 8);
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"'  -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20 || c > 0x7e) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        return sb.append('"').toString();
    }

    // nexus-ehc4q billing note: on transient-error retries, usage.total_tokens is
    // taken from the final successful response only; tokens from prior failed
    // attempts are not accumulated — a billing UNDER-count on retried calls (safe
    // direction: under-charges the customer). Documented, not corrected. RDR-195: this
    // stance now also applies across adaptive-split sub-requests — a rejected oversize
    // attempt's tokenization is billed by Voyage but never appears in the summed
    // usage.total_tokens returned to the caller. Same safe direction, not a new divergence.
    private String callApi(String json) {
        for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
            try {
                HttpRequest req = HttpRequest.newBuilder()
                        .uri(URI.create(url))
                        .header("Authorization", "Bearer " + apiKey)
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(json))
                        .timeout(Duration.ofSeconds(120))
                        .build();

                voyageRequestCount.incrementAndGet();
                HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
                int status = resp.statusCode();

                if (status == 200) return resp.body();

                boolean retryable = (status == 429 || status >= 500);
                if (retryable && attempt < MAX_RETRIES) {
                    long delay = retryBaseMs * (1L << (attempt - 1));
                    log.warn("event=voyage_retry attempt={} status={} delay_ms={}", attempt, status, delay);
                    Thread.sleep(delay);
                    continue;
                }
                if (status == 401 || status == 403) {
                    // nexus-pmhpc: credentials problem, not an engine defect —
                    // typed so VectorHandler returns 502-with-detail, not 500.
                    throw new UpstreamAuthException(
                            "Voyage AI rejected the service's API key (HTTP " + status
                            + "): the key is invalid, expired, or lacks scope. Rotate the"
                            + " service's Voyage key and restart. body=" + resp.body());
                }
                if (status == 400 && isTooManyTokensBatchError(resp.body())) {
                    // RDR-195 Gap 2: a precisely described, actionable 400 — surfaced typed
                    // so the sub-batch caller (collectWithHalving) can halve and retry,
                    // instead of falling through to the generic RuntimeException below.
                    throw new VoyageTooManyTokensException(
                            "Voyage AI rejected the batch as exceeding the per-request token"
                            + " ceiling (HTTP 400, error_code=TOO_MANY_TOKENS_IN_BATCH): "
                            + resp.body());
                }
                throw new RuntimeException(
                        "Voyage AI request failed: HTTP " + status + " body=" + resp.body());

            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException("Voyage embed interrupted", e);
            } catch (RuntimeException e) {
                throw e;
            } catch (Exception e) {
                if (attempt == MAX_RETRIES) {
                    throw new RuntimeException("Voyage embed failed after " + MAX_RETRIES + " attempts", e);
                }
                try { Thread.sleep(retryBaseMs * (1L << (attempt - 1))); }
                catch (InterruptedException ix) {
                    Thread.currentThread().interrupt();
                    throw new RuntimeException("interrupted", ix);
                }
            }
        }
        throw new RuntimeException("Voyage embed: exhausted retries"); // unreachable
    }

    /**
     * Recognizes Voyage's {@code TOO_MANY_TOKENS_IN_BATCH} discriminator
     * (RDR-195 Key Discoveries: verified — source, the stable machine-readable
     * {@code error_code} field in the captured 400 response). Never throws on a malformed
     * or unexpected body — falls back to {@code false} so callApi's generic 400 handling
     * still applies (this check must never be the reason a genuinely different 400 is
     * mis-surfaced as a splittable-batch error).
     */
    @SuppressWarnings("unchecked")
    private boolean isTooManyTokensBatchError(String body) {
        try {
            Map<String, Object> root = mapper.readValue(body, Map.class);
            return "TOO_MANY_TOKENS_IN_BATCH".equals(root.get("error_code"));
        } catch (Exception e) {
            return false;
        }
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> sortedData(String body) throws Exception {
        Map<String, Object> root = mapper.readValue(body, Map.class);
        List<Map<String, Object>> data = (List<Map<String, Object>>) root.get("data");
        if (data == null || data.isEmpty()) {
            throw new RuntimeException("Voyage AI returned empty data array: " + body);
        }
        data.sort(Comparator.comparingInt(m -> ((Number) m.get("index")).intValue()));
        return data;
    }

    /**
     * Parse the Voyage response returning both the float32 vectors and the token count
     * from {@code usage.total_tokens} (bead nexus-ehc4q).
     *
     * <p>The Voyage {@code /v1/embeddings} response root structure:
     * <pre>
     * {
     *   "data":  [...],
     *   "usage": {"total_tokens": N}
     * }
     * </pre>
     */
    @SuppressWarnings("unchecked")
    private EmbedResult parseResponseWithUsage(String body) throws Exception {
        Map<String, Object> root = mapper.readValue(body, Map.class);
        List<Map<String, Object>> data = (List<Map<String, Object>>) root.get("data");
        if (data == null || data.isEmpty()) {
            throw new RuntimeException("Voyage AI returned empty data array: " + body);
        }
        data.sort(Comparator.comparingInt(m -> ((Number) m.get("index")).intValue()));

        List<float[]> result = new ArrayList<>(data.size());
        for (Map<String, Object> item : data) {
            result.add(decodeBase64Float32(getEmbeddingField(item, body)));
        }

        long tokens = 0L;
        Map<String, Object> usage = (Map<String, Object>) root.get("usage");
        if (usage != null) {
            Object totalTokens = usage.get("total_tokens");
            if (totalTokens instanceof Number n) tokens = n.longValue();
        }
        return new EmbedResult(result, tokens);
    }

    /**
     * Parse base64-encoded response as float32 vectors.
     *
     * <p>The Python SDK decodes base64 as:
     * {@code np.frombuffer(base64.b64decode(embedding), np.float32)}
     * Little-endian IEEE 754 float32 binary.
     */
    private List<float[]> parseResponseFloat(String body) throws Exception {
        List<Map<String, Object>> data = sortedData(body);
        List<float[]> result = new ArrayList<>(data.size());
        for (Map<String, Object> item : data) {
            result.add(decodeBase64Float32(getEmbeddingField(item, body)));
        }
        return result;
    }

    /**
     * Parse base64-encoded response as float64 vectors (float32 promoted exactly).
     */
    private List<double[]> parseResponseDouble(String body) throws Exception {
        List<Map<String, Object>> data = sortedData(body);
        List<double[]> result = new ArrayList<>(data.size());
        for (Map<String, Object> item : data) {
            float[] f32 = decodeBase64Float32(getEmbeddingField(item, body));
            double[] f64 = new double[f32.length];
            for (int i = 0; i < f32.length; i++) f64[i] = f32[i];   // exact promotion
            result.add(f64);
        }
        return result;
    }

    private String getEmbeddingField(Map<String, Object> item, String body) {
        Object emb = item.get("embedding");
        if (emb == null) throw new RuntimeException("Voyage AI item missing 'embedding': " + body);
        return emb.toString();
    }

    /**
     * Decode a base64 string as an array of IEEE 754 float32 values (little-endian).
     *
     * <p>Matches Python {@code np.frombuffer(base64.b64decode(b64str), np.float32)}.
     */
    private static float[] decodeBase64Float32(String b64) {
        byte[] bytes = Base64.getDecoder().decode(b64);
        if (bytes.length % 4 != 0) {
            throw new RuntimeException("Base64 embedding byte length not multiple of 4: " + bytes.length);
        }
        int dims = bytes.length / 4;
        float[] vec = new float[dims];
        ByteBuffer buf = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        for (int i = 0; i < dims; i++) vec[i] = buf.getFloat();
        return vec;
    }
}
