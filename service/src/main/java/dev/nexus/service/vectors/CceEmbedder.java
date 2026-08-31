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
import java.time.Duration;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Random;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

/**
 * RDR-152 bead nexus-gmiaf.21 — Voyage AI Contextualized Chunk Embedding (CCE) embedder.
 *
 * <p>Mirrors the voyageai Python SDK's {@code contextualized_embed} call EXACTLY, including
 * the Python t3.py per-text calling convention:
 * <ul>
 *   <li>REST endpoint: {@code POST https://api.voyageai.com/v1/contextualizedembeddings}</li>
 *   <li>Each text is embedded in a SEPARATE API call: {@code inputs=[[text]]} per text.</li>
 *   <li>{@code encoding_format: "base64"} — Python SDK default; gives exact float32 binary</li>
 *   <li>No {@code output_dtype} field — production _cce_embed does not set it</li>
 *   <li>Response: {@code data[0].data[0].embedding}</li>
 *   <li>No {@code truncation} field — CCE API does not accept it (unlike /v1/embeddings)</li>
 * </ul>
 *
 * <p><strong>CRITICAL: per-text not batch.</strong> The CCE model embeds each document
 * in the context of ALL other documents in the same API call.  Sending multiple texts as
 * {@code inputs=[[t0],[t1],...]} produces DIFFERENT embeddings than sending
 * {@code inputs=[[t0]]} and {@code inputs=[[t1]]} separately (measured cosine ≈ 0.999,
 * not 1.0).  Python's t3.py always calls one text at a time:
 * <pre>
 *   result = _voyage_with_retry(
 *       self._voyage_client.contextualized_embed,
 *       inputs=[[text]],
 *       model="voyage-context-3",
 *       input_type=input_type,
 *   )
 *   return result.results[0].embeddings[0]
 * </pre>
 * Java must match this: one API call per text, even for batch inputs.
 *
 * <p><strong>CRITICAL: base64 encoding.</strong>  The Python voyageai SDK uses
 * {@code encoding_format="base64"} by default (see {@code ContextualizedEmbedding.create}).
 * Base64 contains the raw float32 binary (little-endian IEEE 754), decoded via
 * {@code np.frombuffer(b64decode(s), np.float32)}.  JSON float decimals differ by up to
 * 115 ULPs from the true float32 binary.  Java must also use base64 for bit-identical parity.
 *
 * <p>Collection routing: collections starting with {@code knowledge__}, {@code docs__},
 * or {@code rdr__} use this embedder (model {@code voyage-context-3}).
 * Routing enforced by {@link EmbedderRouter}.
 *
 * <p><strong>Parallel fan-out (nexus-9okyk).</strong> Deep-analysis T2
 * {@code nexus/embed-path-latency-analysis-2026-08-06} measured this per-text
 * loop as 86.6% of a full reindex's wall time — N fully-serialized Voyage round
 * trips, strictly sequential on the request thread. {@link #embed}, {@link
 * #embedWithUsage}, and {@link #embedDouble} now fan the per-text calls out
 * across a bounded shared executor ({@link #embedParallel}) instead of looping.
 * Vectors stay BIT-IDENTICAL — the request shape ({@code inputs=[[text]]}), model,
 * and every other param are unchanged; only the round trips overlap. Document-
 * grouped CCE ({@code inputs=[[c0..cN]]}, which WOULD change vector values via
 * cross-chunk context) is explicitly out of scope — that is RDR territory
 * (R2 in the analysis), not this fix.
 *
 * <p>Thread-safe: {@link #http} and {@link #mapper} are safe for concurrent use
 * (immutable {@link HttpClient}, Jackson {@link ObjectMapper} read/decode calls);
 * {@link #executor} and {@link #inFlight} are the shared, bounded concurrency
 * primitives every {@code embed*} call on THIS instance draws from — see {@link
 * #embedParallel} for the ordering and failure-propagation contract.
 */
public final class CceEmbedder implements Embedder {

    private static final Logger log = LoggerFactory.getLogger(CceEmbedder.class);

    /** CCE endpoint — note: no hyphen, different from /v1/embeddings. */
    private static final String CCE_URL = "https://api.voyageai.com/v1/contextualizedembeddings";
    private static final int    MAX_RETRIES   = 3;
    private static final long   RETRY_BASE_MS = 500L;

    /**
     * Total wall-clock budget ONE WHOLE embed request (the full
     * {@link #embedParallel} fan-out, however many texts) may spend absorbing
     * Voyage 429s before failing fast with a typed
     * {@link UpstreamRateLimitedException} (nexus-99r7y). Sized well under
     * the public edge's 30s upstream bound: the 2026-08-15 incident
     * (engine-service-v0.1.76, conexus-ddh0) had sustained project-RPM 429s
     * burn the fixed {@link #MAX_RETRIES} attempts inside the edge timeout,
     * so bulk {@code write_many} surfaced opaque 5xx while the engine was
     * idle.
     *
     * <p><strong>Request-scoped, not per-call</strong> (substantive-critic
     * ship-blocker, 2026-08-31): the deadline is minted ONCE in {@link #embed}/
     * {@link #embedWithUsage}/{@link #embedDouble} and shared by every worker's
     * {@link #callApi}. A per-call budget re-arms each fan-out wave — with
     * {@link #CCE_PARALLELISM}=12 and the client's 300-record batch cap, a
     * write_many under sustained-but-yielding limiting would grind through
     * ~25 waves at up to a full budget each, reproducing the exact incident
     * past N≈24. With the shared deadline, workers starting in later waves
     * see only the REMAINING budget and the whole request either completes
     * or answers an honest 429 + Retry-After inside the edge bound. 429
     * attempts are bounded by THIS budget, not by {@link #MAX_RETRIES} —
     * throttling is pacing, not failure; 5xx keeps the old per-call
     * attempt-bounded semantics.
     */
    private static final long RATE_LIMIT_BUDGET_MS = 20_000L;

    /**
     * Headroom a retry must leave inside the budget for the HTTP call
     * itself — sleeping right up to the deadline just moves the timeout
     * into the request.
     */
    private static final long MIN_CALL_ALLOWANCE_MS = 1_000L;

    /**
     * Bound on concurrent in-flight Voyage HTTPS calls for THIS embedder instance
     * (nexus-9okyk). Chosen inside the analysis's expected 8-16x win range.
     *
     * <p>Virtual threads ({@link #executor}) are cheap to spawn — no per-task OS
     * thread — so the executor itself does not need a bound; {@link #inFlight} is
     * the actual governor, and is what keeps concurrent HTTP requests to the
     * engine from MULTIPLYING the parallelism (each request draws from the SAME
     * semaphore on this instance, it does not get its own fresh 12). The number
     * is deliberately conservative rather than the top of the range: it composes
     * with the request-thread model the rest of the engine already uses
     * ({@code NexusService} runs {@code Executors.newVirtualThreadPerTaskExecutor()}
     * per HTTP request, so a single busy indexing request can already have
     * several {@code embedWithUsage} calls in flight from OTHER concurrent
     * requests against a DIFFERENT {@code CceEmbedder} instance — see
     * {@code EmbedderRouter}'s doc/query split), and it leaves headroom under
     * Voyage's rate limit before the per-call retry/backoff in {@link #callApi}
     * has to start absorbing 429s.
     */
    private static final int CCE_PARALLELISM = 12;

    private final String     apiKey;
    private final String     inputType;  // "document" or "query"
    private final String     url;         // test-injectable; production = CCE_URL
    private final long       retryBaseMs; // test-injectable; production = RETRY_BASE_MS
    private final long       rateLimitBudgetMs; // test-injectable; production = RATE_LIMIT_BUDGET_MS
    private final HttpClient http;
    private final ObjectMapper mapper;

    /** Shared bounded executor for the per-chunk fan-out; virtual threads, cheap to spawn. */
    private final ExecutorService executor;
    /** Actual concurrency governor — caps in-flight Voyage calls at construction-time bound. */
    private final Semaphore inFlight;
    /** Jitter source for {@link #backoffDelayMs} — test-injectable, seeded for determinism. */
    private final Random jitterRandom;

    /**
     * @param apiKey    Voyage AI API key
     * @param inputType {@code "document"} for indexing, {@code "query"} for search
     */
    public CceEmbedder(String apiKey, String inputType) {
        this(apiKey, inputType, CCE_URL, RETRY_BASE_MS, CCE_PARALLELISM, EgressProxy.selector());
    }

    /**
     * Full wiring, the single build path (mirrors {@link VoyageReranker}'s two-
     * constructor pattern): tests inject a fake upstream URL, a fast retry base,
     * an arbitrary parallelism bound, and {@code Optional.empty()} so an ambient
     * {@code HTTPS_PROXY} can never route a localhost fake. Production uses the
     * 2-arg constructor. Delegates to the 7-arg constructor with a fresh,
     * unseeded {@link Random} — production jitter does not need to be
     * reproducible, only tests that assert the jitter FORMULA do.
     *
     * @param apiKey      Voyage AI API key
     * @param inputType   {@code "document"} for indexing, {@code "query"} for search
     * @param url         CCE endpoint (test-injectable)
     * @param retryBaseMs per-call retry backoff base in ms (test-injectable, fast in tests)
     * @param parallelism bound on concurrent in-flight Voyage calls (test-injectable)
     * @param proxy       egress proxy selector; empty bypasses it for localhost fakes
     */
    CceEmbedder(String apiKey, String inputType, String url, long retryBaseMs,
                int parallelism, Optional<ProxySelector> proxy) {
        this(apiKey, inputType, url, retryBaseMs, parallelism, proxy, new Random());
    }

    /**
     * Same as the 6-arg test constructor, plus an injectable jitter source
     * (nexus-9okyk critic fix 1) so a test can seed {@link Random} and assert
     * the backoff FORMULA deterministically without any wall-clock/HTTP timing.
     *
     * @param jitterRandom source for {@link #backoffDelayMs}'s random jitter
     */
    CceEmbedder(String apiKey, String inputType, String url, long retryBaseMs,
                int parallelism, Optional<ProxySelector> proxy, Random jitterRandom) {
        this(apiKey, inputType, url, retryBaseMs, parallelism, proxy, jitterRandom,
             RATE_LIMIT_BUDGET_MS);
    }

    /**
     * Full wiring — the single build path. Adds the injectable 429 budget
     * (nexus-99r7y) so a test can drive the budget-bounded fail-fast without
     * 20s of wall clock; production always takes {@link #RATE_LIMIT_BUDGET_MS}
     * via the shorter constructors.
     *
     * @param rateLimitBudgetMs total 429-absorption budget per {@link #callApi}
     */
    CceEmbedder(String apiKey, String inputType, String url, long retryBaseMs,
                int parallelism, Optional<ProxySelector> proxy, Random jitterRandom,
                long rateLimitBudgetMs) {
        this.apiKey      = apiKey;
        this.inputType   = inputType;
        this.url         = url;
        this.retryBaseMs = retryBaseMs;
        this.rateLimitBudgetMs = rateLimitBudgetMs;
        // nexus-... egress proxy: java.net.http.HttpClient ignores https.proxyHost
        // system properties unless a proxy is set explicitly on the client. The cloud
        // deploy routes api.voyageai.com through squid (private subnet has no NAT), so
        // set the proxy from env (HTTPS_PROXY / NX_HTTPS_PROXY); absent = direct.
        var builder = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10));
        proxy.ifPresent(builder::proxy);
        this.http = builder.build();
        this.mapper = new ObjectMapper();
        this.executor = Executors.newVirtualThreadPerTaskExecutor();
        this.inFlight = new Semaphore(parallelism);
        this.jitterRandom = jitterRandom;
    }

    @Override
    public String modelToken() {
        return "voyage-context-3";
    }

    /**
     * Embed a batch of texts via CCE, one Voyage API call per text, run in
     * parallel across the bounded executor. See {@link #embedParallel}.
     *
     * <p>Each text is still sent as a separate API call ({@code inputs=[[text]]})
     * to match Python's t3.py per-text behavior.  Batching multiple texts in one
     * call produces different embeddings due to cross-document context
     * propagation in the CCE model — that is a DIFFERENT, out-of-scope change
     * (see the class doc); this method only overlaps the round trips.
     */
    @Override
    public List<float[]> embed(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();
        // nexus-99r7y critic fold: ONE deadline for the WHOLE request — see
        // RATE_LIMIT_BUDGET_MS's javadoc. Binding it here (not per worker)
        // is what makes the budget request-scoped.
        long deadlineNanos = newEmbedDeadlineNanos();
        return embedParallel(texts, t -> embedOneFloat(t, deadlineNanos));
    }

    /**
     * Embed a batch of texts and return vectors plus the accumulated token count
     * from {@code usage.total_tokens} across all per-text CCE API calls
     * (bead nexus-ehc4q).
     *
     * <p>One API call per text (CCE per-text convention), run in parallel across
     * the bounded executor (nexus-9okyk; see {@link #embedParallel}); total_tokens
     * is summed across all N calls in INPUT order (not completion order), so the
     * sum is identical to the old sequential loop regardless of scheduling.
     */
    @Override
    public EmbedResult embedWithUsage(List<String> texts) {
        if (texts == null || texts.isEmpty()) return new EmbedResult(List.of(), 0L);
        long deadlineNanos = newEmbedDeadlineNanos();  // nexus-99r7y: request-scoped budget
        List<EmbedResult> perChunk = embedParallel(texts, t -> embedOneWithUsage(t, deadlineNanos));
        List<float[]> result = new ArrayList<>(perChunk.size());
        long totalTokens = 0L;
        for (EmbedResult oneResult : perChunk) {
            result.add(oneResult.embeddings().get(0));
            totalTokens += oneResult.tokens();
        }
        return new EmbedResult(result, totalTokens);
    }

    /**
     * Embed a batch of texts, preserving full double (float64) precision.
     *
     * <p>Decodes base64 as float32, then promotes float32 → float64 exactly.
     * Used by the parity gate ({@code /v1/vectors/embed}) so the returned JSON doubles
     * can be compared against the Python float32 values without serialization loss.
     * Parallelized the same way as {@link #embed} (nexus-9okyk).
     */
    public List<double[]> embedDouble(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();
        long deadlineNanos = newEmbedDeadlineNanos();  // nexus-99r7y: request-scoped budget
        return embedParallel(texts, t -> embedOneDouble(t, deadlineNanos));
    }

    /**
     * Bounded parallel fan-out (nexus-9okyk): one virtual-thread task per input
     * text, gated by {@link #inFlight} to at most the constructor's {@code
     * parallelism} concurrent Voyage calls.
     *
     * <p><strong>Order preservation.</strong> Guaranteed BY CONSTRUCTION, not by
     * completion order: {@code results.get(i)} always comes from {@code
     * futures.get(i)} — the i-th submitted future for the i-th input text — so an
     * adversarial upstream that answers chunk N-1 before chunk 0 still maps every
     * vector back to its exact input position.
     *
     * <p><strong>Failure semantics.</strong> Futures are consumed in index order.
     * The first index whose {@link Future#get()} raises is terminal for the whole
     * batch — no partial result is ever returned to the caller (the method either
     * returns the full list or throws). Every NOT-YET-DONE sibling future is then
     * cancelled (best effort — {@code cancel(true)} interrupts an in-flight HTTP
     * call, which surfaces as {@link InterruptedException} inside {@link
     * #callApi} and aborts it); already-completed siblings are simply discarded.
     * This is a DELIBERATE choice: checking in index order (rather than
     * whichever future fails first in wall-clock time) keeps the result
     * deterministic and keeps "the first chunk that fails wins" close to the old
     * sequential semantics, at the cost of not being maximally low-latency when a
     * late-index chunk fails before an early-index chunk finishes.
     *
     * <p><strong>Accepted cost: billing asymmetry on terminal failure
     * (nexus-9okyk critic fix 2).</strong> The OLD sequential loop only ever
     * billed Voyage for chunks {@code 0..k-1} when chunk {@code k} failed
     * terminally — chunks {@code k+1..N-1} were never even attempted. This
     * fan-out submits ALL N futures eagerly, before any result is checked, so
     * by the time index {@code k}'s failure is discovered, sibling futures at
     * indices {@code > k} may already be in flight or even complete (they are
     * genuinely running concurrently — that is the whole point of the fan-out).
     * {@code cancelFrom} only stops NOT-YET-STARTED siblings; anything already
     * dispatched to Voyage has already been billed regardless of the eventual
     * cancel. Net effect: a terminal failure on a batch can cost MORE Voyage
     * calls (and more billed tokens) than the same failure would have under the
     * old sequential code — worse the earlier in wall-clock time the OTHER
     * workers happen to run relative to the failing one. This is NOT a
     * correctness bug (the DB write is still all-or-nothing, per the "no
     * partial result" guarantee above) and is not fixed here — it is an
     * accepted, honestly-recorded cost of trading serialized billing for an
     * 8-16x wall-clock win on the (overwhelmingly common) success path. It
     * compounds if a caller blindly retries a whole failed page rather than
     * narrowing to the chunk that actually failed.
     *
     * <p>Per-call retry/backoff ({@link #callApi}'s 3-attempt exponential
     * backoff on 429/5xx) is unchanged and lives entirely inside {@code perText}
     * — it composes with this fan-out for free: each of the up-to-{@code
     * CCE_PARALLELISM} concurrent workers retries its OWN call independently.
     */
    private <T> List<T> embedParallel(List<String> texts, Function<String, T> perText) {
        int n = texts.size();
        List<Future<T>> futures = new ArrayList<>(n);
        for (String text : texts) {
            futures.add(executor.submit(() -> {
                try {
                    inFlight.acquire();
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    throw e;
                }
                try {
                    return perText.apply(text);
                } finally {
                    inFlight.release();
                }
            }));
        }
        List<T> results = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            try {
                results.add(futures.get(i).get());
            } catch (ExecutionException e) {
                cancelFrom(futures, i + 1);
                Throwable cause = e.getCause();
                if (cause instanceof RuntimeException re) throw re;
                throw new RuntimeException("CCE parallel embed failed", cause);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                cancelFrom(futures, i + 1);
                throw new RuntimeException("CCE parallel embed interrupted", e);
            }
        }
        return results;
    }

    private static void cancelFrom(List<? extends Future<?>> futures, int fromIdx) {
        for (int j = fromIdx; j < futures.size(); j++) {
            futures.get(j).cancel(true);
        }
    }

    /**
     * Shuts down this instance's bounded executor (nexus-9okyk). Called by
     * {@link EmbedderRouter#close()} at service shutdown. Idempotent — a second
     * call is harmless ({@link ExecutorService#shutdown()} tolerates it).
     */
    @Override
    public void close() {
        executor.shutdown();
        try {
            if (!executor.awaitTermination(5, TimeUnit.SECONDS)) {
                executor.shutdownNow();
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            executor.shutdownNow();
        }
    }

    // ── Per-text API call helpers ─────────────────────────────────────────────

    private float[] embedOneFloat(String text, long deadlineNanos) {
        String json = buildJson(text);
        String body = callApi(json, deadlineNanos);
        try {
            return parseOneFloat(body);
        } catch (Exception e) {
            throw new RuntimeException("CCE float parse failed for text: " + text.substring(0, Math.min(40, text.length())), e);
        }
    }

    /**
     * Embed one text and return the vector plus the token count from
     * {@code usage.total_tokens} in the CCE response (bead nexus-ehc4q).
     *
     * <p>CCE response root:
     * <pre>
     * {
     *   "data":  [...],
     *   "usage": {"total_tokens": N}
     * }
     * </pre>
     *
     * <p>Parses the JSON body ONCE and extracts both the vector and the usage count
     * from the same {@code root} map (avoids double-deserialisation).
     */
    private EmbedResult embedOneWithUsage(String text, long deadlineNanos) {
        String json = buildJson(text);
        String body = callApi(json, deadlineNanos);
        try {
            return parseOneFloatWithUsage(body);
        } catch (Exception e) {
            throw new RuntimeException("CCE embedOneWithUsage failed for text: " + text.substring(0, Math.min(40, text.length())), e);
        }
    }

    /** Parse a CCE response body ONCE: extract the float32 vector AND {@code usage.total_tokens}. */
    @SuppressWarnings("unchecked")
    private EmbedResult parseOneFloatWithUsage(String body) throws Exception {
        Map<String, Object> root = mapper.readValue(body, Map.class);
        // ── vector ────────────────────────────────────────────────────────────────
        List<Map<String, Object>> outerData = (List<Map<String, Object>>) root.get("data");
        if (outerData == null || outerData.isEmpty()) {
            throw new RuntimeException("CCE response missing data array: " + body);
        }
        outerData.sort(Comparator.comparingInt(m -> ((Number) m.get("index")).intValue()));
        List<Map<String, Object>> innerData = (List<Map<String, Object>>) outerData.get(0).get("data");
        if (innerData == null || innerData.isEmpty()) {
            throw new RuntimeException("CCE response: doc group has empty data array");
        }
        innerData.sort(Comparator.comparingInt(m -> ((Number) m.get("index")).intValue()));
        Object emb = innerData.get(0).get("embedding");
        if (emb == null) throw new RuntimeException("CCE response: chunk missing 'embedding'");
        float[] vec;
        if (emb instanceof String b64) {
            vec = decodeBase64Float32(b64);
        } else {
            List<Number> rawEmb = (List<Number>) emb;
            vec = new float[rawEmb.size()];
            for (int i = 0; i < rawEmb.size(); i++) vec[i] = rawEmb.get(i).floatValue();
        }
        // ── usage ─────────────────────────────────────────────────────────────────
        long tokens = 0L;
        Map<String, Object> usage = (Map<String, Object>) root.get("usage");
        if (usage != null) {
            Object totalTokens = usage.get("total_tokens");
            if (totalTokens instanceof Number n) tokens = n.longValue();
        }
        return new EmbedResult(java.util.List.of(vec), tokens);
    }

    private double[] embedOneDouble(String text, long deadlineNanos) {
        String json = buildJson(text);
        String body = callApi(json, deadlineNanos);
        try {
            float[] f32 = parseOneFloat(body);
            double[] f64 = new double[f32.length];
            for (int i = 0; i < f32.length; i++) f64[i] = f32[i];  // exact float32 → float64
            return f64;
        } catch (Exception e) {
            throw new RuntimeException("CCE double parse failed for text: " + text.substring(0, Math.min(40, text.length())), e);
        }
    }

    private String buildJson(String text) {
        // Mirror production _cce_embed / ContextualizedEmbedding.create exactly:
        //   inputs=[[text]]: one doc, one chunk — per-text independent embedding
        //   input_type: "document" for indexing, "query" for search (passed through)
        //   encoding_format="base64": Python SDK default — gives exact float32 binary
        //   No output_dtype field — production does not set it
        //   No truncation field — CCE API does not accept it
        Map<String, Object> body = new HashMap<>();
        body.put("model",           "voyage-context-3");
        body.put("inputs",          List.of(List.of(text)));
        body.put("input_type",      inputType);
        body.put("encoding_format", "base64");
        try {
            return mapper.writeValueAsString(body);
        } catch (Exception e) {
            throw new RuntimeException("Failed to serialize CCE request", e);
        }
    }

    /**
     * Equal-Jitter backoff delay (nexus-9okyk critic fix 1; AWS "Equal Jitter"
     * convention — <a href="https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/">
     * Exponential Backoff And Jitter</a>). Package-private so a test can call it
     * directly with a seeded {@link #jitterRandom} and assert the formula without
     * any wall-clock timing.
     *
     * <p>{@code cap = retryBaseMs * 2^(attempt-1)} is the OLD deterministic delay
     * (unchanged — still the exponential envelope). This method splits it in
     * half: the first half is a FLOOR every retry always waits (a worker can
     * never retry at t≈0), the second half is uniform random jitter added on
     * top. That floor+jitter split is what decorrelates concurrent retries —
     * before this fix, every one of the (up to {@link #CCE_PARALLELISM})
     * concurrently-retrying workers computed the IDENTICAL {@code cap} and slept
     * for the IDENTICAL duration, waking at the IDENTICAL millisecond and
     * re-hitting Voyage in lockstep. Reproduced empirically in review (nexus-
     * 9okyk substantive-critique T2 nexus/nexus-9okyk-critique-2026-08-06: 4
     * concurrently-retrying workers logged {@code event=cce_retry} at the same
     * millisecond, from {@code perCallRetryOn429ComposesWithParallelFanOut}).
     * The old sequential loop could never trigger this — it never had more than
     * one in-flight call, so lockstep was structurally impossible; the fan-out
     * makes it possible, hence the fix rides the same change.
     *
     * <p><strong>Full Jitter</strong> ({@code delay = uniform(0, cap)}, the
     * same blog's other named convention) was considered and rejected: an
     * unlucky draw near 0 would retry almost immediately after a 429, which
     * against a REAL rate limit (this method's own caller only ever talks to a
     * hermetic fake in tests) risks an immediate re-hit — the opposite of what
     * backoff is for. Equal Jitter's floor avoids that while still fully
     * decorrelating the herd.
     *
     * <p><strong>Scope note.</strong> {@link VoyageEmbedder#callApi} and {@link
     * VoyageReranker} carry their own PRIVATE, un-jittered copies of the same
     * exponential-backoff shape (not a shared helper — each class inlines it).
     * Neither is fanned out concurrently by this bead (only CCE gained a
     * parallel caller), so neither is fixed here; a future consolidation of
     * the three near-identical {@code callApi} retry loops (including jitter)
     * is worth its own bead if any of them gains concurrent callers too.
     */
    long backoffDelayMs(int attempt) {
        // Exponent clamped (nexus-99r7y): 429 attempts are now budget-bounded
        // rather than MAX_RETRIES-bounded, so `attempt` can exceed the old
        // 1..3 range — an unclamped shift would overflow long past attempt 63
        // and the envelope should stop growing once it is already
        // budget-scale. 1..6 is unchanged for every pre-existing caller.
        long cap = retryBaseMs * (1L << Math.min(attempt - 1, 5));
        long floor = cap / 2;
        long jitterSpan = cap - floor;  // remaining half; handles odd cap without losing a unit
        long jitter = jitterSpan > 0 ? (long) (jitterRandom.nextDouble() * jitterSpan) : 0;
        return floor + jitter;
    }

    // nexus-ehc4q billing note: on transient-error retries, usage.total_tokens is
    // taken from the final successful response only; tokens from prior failed
    // attempts are not accumulated — a billing UNDER-count on retried calls (safe
    // direction: under-charges the customer). Documented, not corrected.
    /**
     * Parse a {@code Retry-After} header value as milliseconds.
     *
     * <p>Voyage sends delay-seconds (integer or decimal). The HTTP-date form
     * and any unparsable value yield {@code null} — treated as "no header",
     * falling back to the computed backoff, never failing the request over a
     * malformed advisory header. Package-private for direct unit testing.
     */
    static Long parseRetryAfterMs(String value) {
        if (value == null || value.isBlank()) return null;
        try {
            double seconds = Double.parseDouble(value.trim());
            if (seconds < 0) return null;
            return (long) (seconds * 1000.0);
        } catch (NumberFormatException e) {
            return null;  // HTTP-date form or garbage: advisory only, ignore
        }
    }

    /** Shared 429 deadline for ONE whole embed request (critic fold, see
     *  {@link #RATE_LIMIT_BUDGET_MS}). */
    private long newEmbedDeadlineNanos() {
        return System.nanoTime() + rateLimitBudgetMs * 1_000_000L;
    }

    private String callApi(String json, long deadlineNanos) {
        // nexus-99r7y: 429s are BUDGET-bounded (throttling is pacing, not
        // failure — keep absorbing while the edge bound allows), while 5xx /
        // network failures keep the old MAX_RETRIES attempt-bounded
        // semantics via their own counter. The deadline is SHARED across the
        // whole embed request's fan-out — passed in, never recomputed here.
        int transientFailures = 0;
        int consecutive429s = 0;
        for (int attempt = 1; ; attempt++) {
            try {
                HttpRequest req = HttpRequest.newBuilder()
                        .uri(URI.create(url))
                        .header("Authorization", "Bearer " + apiKey)
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(json))
                        .timeout(Duration.ofSeconds(120))
                        .build();

                HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
                int status = resp.statusCode();

                if (status == 200) return resp.body();

                if (status == 429) {
                    consecutive429s++;
                    // Honour Voyage's own Retry-After when present; otherwise
                    // the equal-jitter envelope paces the herd.
                    Long retryAfterMs = parseRetryAfterMs(
                            resp.headers().firstValue("Retry-After").orElse(null));
                    long delay = Math.max(backoffDelayMs(attempt),
                                          retryAfterMs != null ? retryAfterMs : 0L);
                    long remainingMs = (deadlineNanos - System.nanoTime()) / 1_000_000L;
                    if (delay + MIN_CALL_ALLOWANCE_MS > remainingMs) {
                        // Fail fast and HONEST, with margin under the edge's
                        // 30s bound: an engine-authored 429 + Retry-After
                        // beats an opaque edge-timeout 5xx (the 2026-08-15
                        // incident shape), and arms the client-side rate
                        // brake (conexus-cy9u7) that keys on the header.
                        long retryAfterS = Math.max(1L, (long) Math.ceil(delay / 1000.0));
                        // Review fold (2026-08-31): no floor on remainingMs — a
                        // negative remainder means the loop ran PAST the budget
                        // (e.g. a slow in-flight call), and the log must say so
                        // rather than under-report elapsed as exactly the budget.
                        long elapsedMs = rateLimitBudgetMs - remainingMs;
                        log.warn("event=cce_rate_limited attempts={} consecutive_429s={} elapsed_ms={} retry_after_s={}",
                                 attempt, consecutive429s, elapsedMs, retryAfterS);
                        throw new UpstreamRateLimitedException(
                                "Voyage AI is rate limiting (HTTP 429 x" + consecutive429s
                                + " within the " + rateLimitBudgetMs + "ms budget); failing"
                                + " fast before the edge timeout. Retry after ~" + retryAfterS
                                + "s. body=" + resp.body(), retryAfterS);
                    }
                    log.warn("event=cce_retry attempt={} status=429 delay_ms={} retry_after_header_ms={}",
                             attempt, delay, retryAfterMs);
                    Thread.sleep(delay);
                    continue;
                }
                consecutive429s = 0;
                if (status >= 500) {
                    transientFailures++;
                    if (transientFailures < MAX_RETRIES) {
                        long delay = backoffDelayMs(transientFailures);
                        log.warn("event=cce_retry attempt={} status={} delay_ms={}", attempt, status, delay);
                        Thread.sleep(delay);
                        continue;
                    }
                }
                if (status == 401 || status == 403) {
                    // nexus-pmhpc: credentials problem, not an engine defect —
                    // typed so VectorHandler returns 502-with-detail, not 500.
                    throw new UpstreamAuthException(
                            "Voyage AI rejected the service's API key (HTTP " + status
                            + "): the key is invalid, expired, or lacks scope. Rotate the"
                            + " service's Voyage key and restart. body=" + resp.body());
                }
                throw new RuntimeException("Voyage AI CCE request failed: HTTP " + status + " body=" + resp.body());

            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException("CCE embed interrupted", e);
            } catch (RuntimeException e) {
                throw e;
            } catch (Exception e) {
                transientFailures++;
                if (transientFailures >= MAX_RETRIES) {
                    throw new RuntimeException("CCE embed failed after " + MAX_RETRIES + " attempts", e);
                }
                try { Thread.sleep(backoffDelayMs(transientFailures)); }
                catch (InterruptedException ix) { Thread.currentThread().interrupt(); throw new RuntimeException("interrupted", ix); }
            }
        }
    }

    // ── Response parsers ──────────────────────────────────────────────────────

    /**
     * Parse CCE base64 response body for a single-text call (inputs=[[text]]).
     *
     * <p>Response structure with encoding_format="base64":
     * <pre>
     * {
     *   "object": "list",
     *   "data": [
     *     {
     *       "object": "list",
     *       "index": 0,              // outer doc index (always 0 for single-text call)
     *       "data": [
     *         {
     *           "object": "embedding",
     *           "index": 0,          // chunk index (always 0 for single chunk)
     *           "embedding": "base64string..."
     *         }
     *       ]
     *     }
     *   ]
     * }
     * </pre>
     */
    @SuppressWarnings("unchecked")
    private float[] parseOneFloat(String body) throws Exception {
        Map<String, Object> root = mapper.readValue(body, Map.class);
        List<Map<String, Object>> outerData = (List<Map<String, Object>>) root.get("data");
        if (outerData == null || outerData.isEmpty()) {
            throw new RuntimeException("CCE response missing data array: " + body);
        }
        outerData.sort(Comparator.comparingInt(m -> ((Number) m.get("index")).intValue()));

        List<Map<String, Object>> innerData = (List<Map<String, Object>>) outerData.get(0).get("data");
        if (innerData == null || innerData.isEmpty()) {
            throw new RuntimeException("CCE response: doc group has empty data array");
        }
        innerData.sort(Comparator.comparingInt(m -> ((Number) m.get("index")).intValue()));

        Object emb = innerData.get(0).get("embedding");
        if (emb == null) throw new RuntimeException("CCE response: chunk missing 'embedding'");

        // Decode base64 as float32 binary (same as Python np.frombuffer(b64decode(s), np.float32))
        if (emb instanceof String b64) {
            return decodeBase64Float32(b64);
        }
        // Fallback: if API returns JSON array (non-base64 path)
        List<Number> rawEmb = (List<Number>) emb;
        float[] vec = new float[rawEmb.size()];
        for (int i = 0; i < rawEmb.size(); i++) vec[i] = rawEmb.get(i).floatValue();
        return vec;
    }

    /**
     * Decode a base64 string as an array of IEEE 754 float32 values (little-endian).
     *
     * <p>Matches Python {@code np.frombuffer(base64.b64decode(b64str), np.float32)}.
     */
    private static float[] decodeBase64Float32(String b64) {
        byte[] bytes = Base64.getDecoder().decode(b64);
        if (bytes.length % 4 != 0) {
            throw new RuntimeException("CCE base64 byte length not multiple of 4: " + bytes.length);
        }
        int dims = bytes.length / 4;
        float[] vec = new float[dims];
        ByteBuffer buf = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        for (int i = 0; i < dims; i++) vec[i] = buf.getFloat();
        return vec;
    }
}
