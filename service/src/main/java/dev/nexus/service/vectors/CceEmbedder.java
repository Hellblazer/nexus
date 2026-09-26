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
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * RDR-152 bead nexus-gmiaf.21 — Voyage AI Contextualized Chunk Embedding (CCE) embedder.
 *
 * <p>Mirrors the voyageai Python SDK's {@code contextualized_embed} call's parameters:
 * <ul>
 *   <li>REST endpoint: {@code POST https://api.voyageai.com/v1/contextualizedembeddings}</li>
 *   <li>Each text is its OWN single-chunk document: {@code inputs=[[t0],[t1],...]}, up to
 *       {@link #DEFAULT_BATCH_CHUNKS} texts per call (nexus-u2mlh.1, shape C; see below).</li>
 *   <li>{@code encoding_format: "base64"} — Python SDK default; gives exact float32 binary</li>
 *   <li>No {@code output_dtype} field — production _cce_embed does not set it</li>
 *   <li>Response: {@code data[0].data[0].embedding}</li>
 *   <li>No {@code truncation} field — CCE API does not accept it (unlike /v1/embeddings)</li>
 * </ul>
 *
 * <p><strong>Batched as single-chunk documents (nexus-u2mlh.1, Sam 2026-09-24).</strong>
 * Sending {@code inputs=[[t0],[t1],...]} makes each text its own document, so no text is
 * embedded in another's context. Measured from the engine host on 2026-09-24 (conexus-1f,
 * 1024-d): a chunk alone vs the same chunk inside such a batch, cosine 0.999951-0.999999,
 * which is batch numerics, not context; the control (alone vs alone) was 1.000000. That
 * keeps content-addressed chunk identity (RDR-108/180) and RDR-181's embed-skip sound,
 * and needs no reindex. Throughput matched the per-chunk fan-out's best (12.6-13.0 vs
 * 7.9-13.6 chunks/s) with a twelfth of the HTTP calls and semaphore permits. Grouping one
 * document's chunks as {@code [[c0..cN]]} is a DIFFERENT shape: it changes the vectors
 * (cosine 0.83-0.89) and was slower, so it is not used. A batch that Voyage refuses with a
 * 400 (for example past its 32k-token pre-chunked request cap) falls back to one call per
 * text, so one oversized text fails alone.
 *
 * <p><strong>What this does not fix, and what it costs.</strong> The probe ran while Voyage
 * was fast; shape C was not measured while Voyage is slow, which is when the 2026-09-24
 * incident happened (1-5 chunks/s). The incident's fix is admission control and deadline
 * enforcement (nexus-u2mlh.2/.3), not this shape. Two costs grow with the batch: a retried
 * 5xx or 429 resends the whole batch's payload, and the request deadline is checked between
 * batches, so one slow batch hides up to {@code batchChunks} texts' worth of work from it.
 * The drift batching introduces (cosine 0.99995+) is below CCE's own call-to-call noise
 * (2.6e-4 to 3.7e-4 between identical calls, nexus-mcgnz), which is why nothing was
 * reindexed; nexus-u2mlh.7 checks recall after deploy. The pre-batching convention, kept here as the
 * record of what the parity oracle used to mirror, was Python's t3.py one text per call:
 * <pre>
 *   result = _voyage_with_retry(
 *       self._voyage_client.contextualized_embed,
 *       inputs=[[text]],
 *       model="voyage-context-3",
 *       input_type=input_type,
 *   )
 *   return result.results[0].embeddings[0]
 * </pre>
 * The parity gate ({@code tests/db/test_embed_parity.py}) sends its oracle request in the
 * same batched shape and compares by cosine within 1e-3, because CCE output also drifts
 * between identical calls (nexus-mcgnz measured 2.6e-4 to 3.7e-4).
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
 * across a bounded shared executor ({@link #embedAll}) instead of looping.
 * Since nexus-u2mlh.1 each task carries a batch of single-chunk documents rather
 * than one text (see "Batched as single-chunk documents" above).
 *
 * <p>Thread-safe: {@link #http} and {@link #mapper} are safe for concurrent use
 * (immutable {@link HttpClient}, Jackson {@link ObjectMapper} read/decode calls);
 * {@link #executor} and {@link #inFlight} are the shared, bounded concurrency
 * primitives every {@code embed*} call on THIS instance draws from — see {@link
 * #embedAll} for the ordering and failure-propagation contract.
 */
public final class CceEmbedder implements Embedder {

    private static final Logger log = LoggerFactory.getLogger(CceEmbedder.class);

    /** CCE endpoint — note: no hyphen, different from /v1/embeddings. */
    private static final String CCE_URL = "https://api.voyageai.com/v1/contextualizedembeddings";
    private static final long   RETRY_BASE_MS = 500L;

    /**
     * Total wall-clock budget ONE WHOLE embed request (the full
     * {@link #embedAll} fan-out, however many texts) may spend absorbing
     * Voyage 429s before failing fast with a typed
     * {@link UpstreamRateLimitedException} (nexus-99r7y). Sized well under
     * the public edge's 30s upstream bound: the 2026-08-15 incident
     * (engine-service-v0.1.76, conexus-ddh0) had sustained project-RPM 429s
     * burn the fixed {@link VoyageRetryLoop#MAX_RETRIES} attempts inside the edge timeout,
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
     * attempts are bounded by THIS budget, not by
     * {@link VoyageRetryLoop#MAX_RETRIES} — throttling is pacing, not
     * failure; 5xx keeps the old per-call attempt-bounded semantics.
     *
     * <p>The loop itself now lives in {@link VoyageRetryLoop} (nexus-1vpal:
     * the three private near-identical {@code callApi} loops consolidated);
     * this constant remains the CCE production default fed into it.
     */
    private static final long RATE_LIMIT_BUDGET_MS = 20_000L;

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

    /** Chunks per CCE request, each its own single-chunk document (nexus-u2mlh.1). The
     *  probe that chose shape C used 12; {@code NX_CCE_BATCH_CHUNKS} overrides. */
    static final int DEFAULT_BATCH_CHUNKS = 12;

    /** UTF-8 byte budget for one batched request. Voyage caps a pre-chunked request at
     *  32,000 tokens in total; 48 KiB (49,152 bytes) stays under that for text averaging
     *  at least 1.536 bytes per token. English prose runs near 4 and code near 3; a batch
     *  that still exceeds the cap gets a 400 and falls back to one text per call. A single
     *  text over the budget still goes alone. */
    static final int BATCH_MAX_BYTES = 48 * 1024;

    /** A call slower than this is logged at INFO ({@code event=cce_call_slow}); every
     *  call is logged at DEBUG (nexus-u2mlh.4). */
    static final long SLOW_CALL_MS = 5_000L;

    /** {@code NX_CCE_PARALLELISM} / {@code NX_CCE_BATCH_CHUNKS} (nexus-u2mlh.4): a blank or
     *  absent value takes the default; an unparsable or out-of-range one is refused with a
     *  warning and the default, never a crash at boot. */
    static int envInt(String name, String raw, int dflt, int min, int max) {
        if (raw == null || raw.isBlank()) {
            return dflt;
        }
        try {
            int v = Integer.parseInt(raw.trim());
            if (v >= min && v <= max) {
                return v;
            }
        } catch (NumberFormatException ignored) {
            // fall through to the warning
        }
        log.warn("event=cce_config_invalid name={} value={} allowed={}..{} using={}", name, raw, min, max, dflt);
        return dflt;
    }

    /** Thrown for a non-2xx CCE status the retry loop gives up on, so the batch path can
     *  tell a 400 (fall back to one call per text) from anything else. */
    static final class CceStatusException extends RuntimeException {
        final int status;

        CceStatusException(int status, String message) {
            super(message);
            this.status = status;
        }
    }

    private final String     apiKey;
    private final String     inputType;  // "document" or "query"
    private final String     url;         // test-injectable; production = CCE_URL
    private final HttpClient http;
    private final ObjectMapper mapper;

    /** Shared bounded executor for the per-chunk fan-out; virtual threads, cheap to spawn. */
    private final ExecutorService executor;
    /** Actual concurrency governor — caps in-flight Voyage calls at construction-time bound. */
    private final Semaphore inFlight;
    /** Texts per request; 1 is the historical one-text-per-call shape. */
    private final int batchChunks;
    /** The {@link #inFlight} bound, kept for admission arithmetic and the status snapshot. */
    private final int parallelism;
    /** Batch tasks started and blocked on {@link #inFlight} (nexus-u2mlh.2). Counted from
     *  inside the task, not at submit, so a task cancelled before it ever runs is never
     *  counted and cannot leak a count. */
    private final AtomicInteger waitingBatches = new AtomicInteger(0);
    /** Moving average of one batch's Voyage call time in nanos, 0 until the first call
     *  completes (nexus-u2mlh.2). Admission refuses nothing without a measurement. */
    private final AtomicLong callEwmaNanos = new AtomicLong(0L);
    /** The consolidated retry choreography (nexus-1vpal) — owns backoff,
     *  Retry-After, the 429 budget arithmetic, and the shared auth arm. */
    private final VoyageRetryLoop retryLoop;

    /**
     * Bead nexus-s71lr, code-review-expert pass 2 finding a — same mechanism as
     * {@link Bge768Embedder#progressGate} / {@code VoyageEmbedder}'s own copy:
     * rate-limited to about once per 5s, per instance.
     */
    private static final long PROGRESS_LOG_INTERVAL_NANOS = java.util.concurrent.TimeUnit.SECONDS.toNanos(5);
    private final EmbedProgressGate progressGate = new EmbedProgressGate(PROGRESS_LOG_INTERVAL_NANOS);

    /**
     * Bead nexus-s71lr, pass 3: {@code GET /v1/status}'s {@code
     * local_embed_activity} was null for every cloud install — this class
     * now feeds the SAME {@link EmbedActivityTracker} mechanism {@link
     * Bge768Embedder} does. Since nexus-u2mlh.2, {@code queue_depth} is
     * {@link #waitingBatches} and {@code thread_width} is {@link #parallelism};
     * both were -1 before.
     */
    private static final long ACTIVE_WINDOW_NANOS = 2 * PROGRESS_LOG_INTERVAL_NANOS;
    private final EmbedActivityTracker activityTracker = new EmbedActivityTracker(ACTIVE_WINDOW_NANOS);

    /** Terminal-failure vocabulary handed to {@link #retryLoop} — CCE has no
     *  special statuses beyond the loop's shared arms. */
    private static final VoyageRetryLoop.Failures CCE_FAILURES = new VoyageRetryLoop.Failures() {
        @Override
        public RuntimeException status(int status, String body) {
            return new CceStatusException(status, "Voyage AI CCE request failed: HTTP " + status + " body=" + body);
        }

        @Override
        public RuntimeException wrap(String message, Throwable cause) {
            return new RuntimeException(message, cause);
        }
    };

    /**
     * @param apiKey    Voyage AI API key
     * @param inputType {@code "document"} for indexing, {@code "query"} for search
     */
    public CceEmbedder(String apiKey, String inputType) {
        this(apiKey, inputType, CCE_URL, RETRY_BASE_MS,
             envInt("NX_CCE_PARALLELISM", System.getenv("NX_CCE_PARALLELISM"), CCE_PARALLELISM, 1, 64),
             EgressProxy.selector(), new Random(), RATE_LIMIT_BUDGET_MS,
             envInt("NX_CCE_BATCH_CHUNKS", System.getenv("NX_CCE_BATCH_CHUNKS"), DEFAULT_BATCH_CHUNKS, 1, 128));
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
        this(apiKey, inputType, url, retryBaseMs, parallelism, proxy, jitterRandom, rateLimitBudgetMs, 1);
    }

    /**
     * Full wiring with the batch size (nexus-u2mlh.1). The shorter test constructors
     * keep {@code batchChunks=1}, the one-text-per-call shape their fakes and
     * assertions were written for; production batches at {@link #DEFAULT_BATCH_CHUNKS}.
     *
     * @param batchChunks texts per Voyage request, each its own single-chunk document
     */
    CceEmbedder(String apiKey, String inputType, String url, long retryBaseMs,
                int parallelism, Optional<ProxySelector> proxy, Random jitterRandom,
                long rateLimitBudgetMs, int batchChunks) {
        if (batchChunks < 1) {
            throw new IllegalArgumentException("batchChunks must be >= 1, got " + batchChunks);
        }
        this.batchChunks = batchChunks;
        this.parallelism = parallelism;
        this.apiKey      = apiKey;
        this.inputType   = inputType;
        this.url         = url;
        this.retryLoop   = new VoyageRetryLoop("cce", "CCE embed", retryBaseMs,
                                               rateLimitBudgetMs, jitterRandom, () -> { });
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
        log.info("event=cce_embedder_configured input_type={} parallelism={} batch_chunks={} batch_max_bytes={}",
                 inputType, parallelism, batchChunks, BATCH_MAX_BYTES);
    }

    @Override
    public String modelToken() {
        return "voyage-context-3";
    }

    /**
     * Bead nexus-s71lr, pass 3 — see {@link #activityTracker}'s javadoc.
     */
    @Override
    public EmbedActivitySnapshot activitySnapshot() {
        return activityTracker.snapshot(System.nanoTime(), waitingBatches.get(), parallelism);
    }

    /**
     * Embed texts via CCE, batched as single-chunk documents and run in parallel
     * across the bounded executor. See {@link #embedAll} and the class doc.
     */
    @Override
    public List<float[]> embed(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();
        return embedAll(texts).embeddings();
    }

    /**
     * Embed a batch of texts and return vectors plus the accumulated token count
     * from {@code usage.total_tokens} summed over every CCE request the call made
     * (bead nexus-ehc4q), in batch order, so the sum does not depend on scheduling.
     */
    @Override
    public EmbedResult embedWithUsage(List<String> texts) {
        if (texts == null || texts.isEmpty()) return new EmbedResult(List.of(), 0L);
        return embedAll(texts);
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
        List<float[]> f32 = embedAll(texts).embeddings();
        List<double[]> out = new ArrayList<>(f32.size());
        for (float[] v : f32) {
            double[] d = new double[v.length];
            for (int i = 0; i < v.length; i++) d[i] = v[i];  // exact float32 -> float64
            out.add(d);
        }
        return out;
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
    /** One batch's vectors and billed tokens, plus how long it queued for a permit
     *  and how long its Voyage call(s) took (nexus-u2mlh.4). */
    private record BatchOutcome(List<float[]> vectors, long tokens, long queuedNanos, long callNanos) {
        /** A batch whose permit arrived after the request deadline; compared by identity. */
        static final BatchOutcome SKIPPED = new BatchOutcome(List.of(), 0L, 0L, 0L);
    }

    /** Splits {@code texts} into consecutive batches of at most {@link #batchChunks}
     *  texts and {@link #BATCH_MAX_BYTES} UTF-8 bytes; a text over the byte budget
     *  goes alone. Package-private for tests. */
    List<int[]> planBatches(List<String> texts) {
        List<int[]> out = new ArrayList<>();
        int start = 0;
        int bytes = 0;
        for (int i = 0; i < texts.size(); i++) {
            int len = texts.get(i).getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
            boolean full = (i - start) >= batchChunks || (i > start && bytes + len > BATCH_MAX_BYTES);
            if (full) {
                out.add(new int[] {start, i});
                start = i;
                bytes = 0;
            }
            bytes += len;
        }
        if (start < texts.size()) {
            out.add(new int[] {start, texts.size()});
        }
        return out;
    }

    /**
     * The embed path (nexus-9okyk fan-out, batched by nexus-u2mlh.1): one virtual-thread
     * task per BATCH of up to {@link #batchChunks} texts, gated by {@link #inFlight} to
     * at most the constructor's {@code parallelism} concurrent Voyage requests.
     *
     * <p><strong>Order preservation.</strong> By construction: batch {@code b} covers a
     * fixed index range, and its vectors are placed at that range, whatever order the
     * batches complete in or the response lists them in (the parser sorts by index).
     *
     * <p><strong>Failure semantics.</strong> Batches are consumed in index order; the first
     * one whose {@link Future#get()} raises is terminal for the whole request, and every
     * not-yet-done sibling is cancelled. No partial result is ever returned. A batch Voyage
     * refuses with a 400 first retries as one call per text ({@link #embedBatch}), so only
     * a text that fails alone fails the request.
     *
     * <p><strong>Accepted cost: billing asymmetry on terminal failure (nexus-9okyk critic
     * fix 2).</strong> All batches are submitted eagerly, so batches past the failing one
     * may already be billed; {@code cancelFrom} stops only those not yet started.
     *
     * <p><strong>Request deadline (nexus-8hdg9 phase 4).</strong> Checked before each
     * collected batch; past it, not-yet-started batches are cancelled and a
     * {@link RequestDeadlineExceededException} is thrown.
     *
     * <p><strong>Admission (nexus-u2mlh.2).</strong> Before anything is submitted,
     * {@link #admit} refuses a request the waiting batches make late. A batch that gets
     * its permit after the deadline returns {@code SKIPPED} without calling Voyage, and
     * the collector turns that into the same deadline abort.
     *
     * <p><strong>Logging (nexus-u2mlh.4).</strong> Each batch logs its queue wait and call
     * time at DEBUG, at INFO when the call exceeds {@link #SLOW_CALL_MS}; each request logs
     * one {@code event=embed_done} summary at INFO.
     */
    private EmbedResult embedAll(List<String> texts) {
        int n = texts.size();
        // nexus-99r7y critic fold: ONE 429 deadline for the WHOLE request.
        long deadlineNanos = newEmbedDeadlineNanos();
        // Read on the calling thread: the request context does not follow the tasks
        // onto the executor, and the 400 fallback inside a task needs it.
        long requestDeadlineNanos = RequestDeadlineProbe.currentDeadlineNanos();
        List<int[]> ranges = planBatches(texts);
        admit(n, ranges.size(), requestDeadlineNanos, System.nanoTime());
        List<Future<BatchOutcome>> futures = new ArrayList<>(ranges.size());
        for (int[] r : ranges) {
            List<String> sub = texts.subList(r[0], r[1]);
            long submittedNanos = System.nanoTime();
            futures.add(executor.submit(() -> {
                waitingBatches.incrementAndGet();
                try {
                    inFlight.acquire();
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    throw e;
                } finally {
                    waitingBatches.decrementAndGet();
                }
                long startedNanos = System.nanoTime();
                try {
                    // nexus-u2mlh.2: a permit granted after the caller's deadline is
                    // handed straight back. The collector turns SKIPPED into the abort.
                    if (RequestDeadlineProbe.expired(requestDeadlineNanos, startedNanos)) {
                        return BatchOutcome.SKIPPED;
                    }
                    BatchOutcome o = embedBatch(sub, deadlineNanos, requestDeadlineNanos);
                    long callNanos = System.nanoTime() - startedNanos;
                    long queuedNanos = startedNanos - submittedNanos;
                    logCall(sub.size(), queuedNanos, callNanos);
                    recordCallNanos(callNanos);
                    return new BatchOutcome(o.vectors(), o.tokens(), queuedNanos, callNanos);
                } finally {
                    inFlight.release();
                }
            }));
        }

        List<float[]> vectors = new ArrayList<>(n);
        long tokens = 0L;
        long maxQueuedNanos = 0L;
        long maxCallNanos = 0L;
        long sumCallNanos = 0L;
        long callStartNanos = System.nanoTime();
        long lastNanos = callStartNanos;
        int chunksDone = 0;
        for (int b = 0; b < futures.size(); b++) {
            if (RequestDeadlineProbe.expired(requestDeadlineNanos, lastNanos)) {
                cancelFrom(futures, b);
                throw deadlineAbort(chunksDone, n, callStartNanos, lastNanos, requestDeadlineNanos);
            }
            BatchOutcome outcome;
            try {
                outcome = futures.get(b).get();
            } catch (ExecutionException e) {
                cancelFrom(futures, b + 1);
                Throwable cause = e.getCause();
                if (cause instanceof RuntimeException re) throw re;
                throw new RuntimeException("CCE parallel embed failed", cause);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                cancelFrom(futures, b + 1);
                throw new RuntimeException("CCE parallel embed interrupted", e);
            }
            if (outcome == BatchOutcome.SKIPPED) {
                // Its permit came after the deadline, so the deadline has passed now too.
                cancelFrom(futures, b + 1);
                throw deadlineAbort(chunksDone, n, callStartNanos, System.nanoTime(), requestDeadlineNanos);
            }
            vectors.addAll(outcome.vectors());
            tokens += outcome.tokens();
            maxQueuedNanos = Math.max(maxQueuedNanos, outcome.queuedNanos());
            maxCallNanos = Math.max(maxCallNanos, outcome.callNanos());
            sumCallNanos += outcome.callNanos();
            int batchSize = ranges.get(b)[1] - ranges.get(b)[0];
            chunksDone += batchSize;
            long nowNanos = System.nanoTime();
            lastNanos = nowNanos;
            double elapsedSec = (nowNanos - callStartNanos) / 1_000_000_000.0;
            double chunksPerSec = elapsedSec > 0.0 ? chunksDone / elapsedSec : 0.0;
            activityTracker.record(batchSize, chunksPerSec, nowNanos);
            if (progressGate.shouldLog(nowNanos)) {
                log.info("event=embed_progress embedder=cce chunks_done={} chunks_total={} "
                        + "elapsed_s={} chunks_per_sec={}",
                        chunksDone, n, String.format("%.1f", elapsedSec),
                        String.format("%.1f", chunksPerSec));
            }
        }
        long elapsedMs = (System.nanoTime() - callStartNanos) / 1_000_000L;
        log.info("event=embed_done embedder=cce chunks={} batches={} elapsed_ms={} "
                + "max_queue_ms={} max_call_ms={} mean_call_ms={} tokens={}",
                n, futures.size(), elapsedMs, maxQueuedNanos / 1_000_000L, maxCallNanos / 1_000_000L,
                futures.isEmpty() ? 0 : sumCallNanos / futures.size() / 1_000_000L, tokens);
        return new EmbedResult(vectors, tokens);
    }

    /** Longest {@code Retry-After} an admission refusal suggests, in seconds. The edge
     *  deadline on the embed routes is 55 s; a longer pause than that only idles a client
     *  that the next estimate may well admit. */
    static final long MAX_ADMISSION_RETRY_AFTER_S = 30L;

    /**
     * Admission (nexus-u2mlh.2): refuse, before anything queues, a request that cannot
     * finish inside its deadline behind the batches already waiting for {@link #inFlight}.
     * Such a request would otherwise wait, take permits after its caller has gone, and
     * push every later request past its own deadline too, which is how 2026-09-24's
     * retries compounded.
     *
     * <p>The estimate is {@code ceil((waiting + mine) / parallelism)} waves of one
     * average call ({@link #callEwmaNanos}). It is deliberately simple: batches differ in
     * size and Voyage's latency moves several-fold within minutes, so the test is only
     * whether the queue ahead makes the deadline unreachable. Three cases admit
     * unconditionally: no deadline in context, no call measured yet, and a request that
     * cannot finish even on an empty queue. The last matters: refusing it would refuse
     * every retry too, while admitting it leaves it to the deadline checks in
     * {@link #embedAll}, which bound its cost.
     *
     * <p>The refusal is a {@link RequestDeadlineExceededException}, so the wire shape is
     * the existing 503 + {@code Retry-After}. The suggested wait is the estimated time for
     * the waiting batches to drain, clamped to {@code [1, MAX_ADMISSION_RETRY_AFTER_S]}.
     */
    void admit(int chunks, int myBatches, long requestDeadlineNanos, long nowNanos) {
        if (requestDeadlineNanos == RequestDeadlineProbe.NONE) {
            return;
        }
        long perCall = callEwmaNanos.get();
        if (perCall <= 0L) {
            return;
        }
        long remaining = requestDeadlineNanos - nowNanos;
        if (waves(myBatches) * perCall >= remaining) {
            return;
        }
        int ahead = waitingBatches.get();
        long predicted = waves(ahead + myBatches) * perCall;
        if (predicted <= remaining) {
            return;
        }
        long drainNanos = waves(ahead) * perCall;
        long retryAfterS = Math.max(1L, Math.min(MAX_ADMISSION_RETRY_AFTER_S,
                (drainNanos + 999_999_999L) / 1_000_000_000L));
        activityTracker.recordAdmissionRefusal();  // GET /v1/status admission_refusals_total
        log.warn("event=embed_admission_refused embedder=cce chunks={} batches={} waiting_batches={} "
                + "parallelism={} mean_call_ms={} predicted_ms={} remaining_ms={} retry_after_s={}",
                chunks, myBatches, ahead, parallelism, perCall / 1_000_000L,
                predicted / 1_000_000L, remaining / 1_000_000L, retryAfterS);
        throw new RequestDeadlineExceededException(
                "embed admission refused: " + myBatches + " batches behind " + ahead
                        + " waiting need about " + predicted / 1_000_000L + "ms, "
                        + remaining / 1_000_000L + "ms left before the deadline",
                retryAfterS);
    }

    private long waves(int batches) {
        return (batches + parallelism - 1L) / parallelism;
    }

    /** Folds one batch's call time into {@link #callEwmaNanos}, weight 1/5 on the new
     *  sample; the first sample seeds it. Package-private for tests. */
    void recordCallNanos(long callNanos) {
        long sample = Math.max(1L, callNanos);
        callEwmaNanos.updateAndGet(prev -> prev == 0L ? sample : prev + (sample - prev) / 5L);
    }

    /** Test-only: the current call-time average, 0 before any call. */
    long callEwmaNanos() {
        return callEwmaNanos.get();
    }

    /** Test-only: tasks currently blocked on {@link #inFlight}. */
    int waitingBatches() {
        return waitingBatches.get();
    }

    private RequestDeadlineExceededException deadlineAbort(int chunksDone, int n, long callStartNanos,
                                                           long nowNanos, long requestDeadlineNanos) {
        long elapsedMs = (nowNanos - callStartNanos) / 1_000_000L;
        long pastDeadlineMs = (nowNanos - requestDeadlineNanos) / 1_000_000L;
        activityTracker.recordDeadlineAbort();  // GET /v1/status deadline_aborts_total
        log.warn("event=embed_deadline_exceeded embedder=cce chunks_done={} chunks_total={} "
                + "elapsed_ms={} past_deadline_ms={} retry_after_s={}",
                chunksDone, n, elapsedMs, pastDeadlineMs,
                RequestDeadlineExceededException.DEFAULT_RETRY_AFTER_SECONDS);
        return new RequestDeadlineExceededException(
                "embed deadline exceeded after " + chunksDone + "/" + n + " chunks ("
                        + elapsedMs + "ms elapsed, " + pastDeadlineMs + "ms past deadline)",
                RequestDeadlineExceededException.DEFAULT_RETRY_AFTER_SECONDS);
    }

    private void logCall(int chunks, long queuedNanos, long callNanos) {
        long queuedMs = queuedNanos / 1_000_000L;
        long callMs = callNanos / 1_000_000L;
        if (callMs >= SLOW_CALL_MS) {
            log.info("event=cce_call_slow chunks={} queue_ms={} call_ms={} available_permits={}",
                     chunks, queuedMs, callMs, inFlight.availablePermits());
        } else if (log.isDebugEnabled()) {
            log.debug("event=cce_call chunks={} queue_ms={} call_ms={}", chunks, queuedMs, callMs);
        }
    }

    /**
     * One Voyage request for {@code texts} as single-chunk documents. A 400 on a batch of
     * more than one retries each text alone (logged {@code event=cce_batch_rejected} with
     * the start of Voyage's body), so a request Voyage refuses for its size succeeds and a
     * bad text fails alone. Any 400 triggers it, not only a size error: the contextualized
     * endpoint's error codes are not documented the way {@code VoyageEmbedder} relies on
     * {@code TOO_MANY_TOKENS_IN_BATCH}, and a non-size 400 costs at most one extra round
     * of single calls, stopping at the first text that fails alone.
     *
     * <p>The fallback runs inside the batch's one permit, one call at a time: submitting
     * the texts back to the executor from a task that holds a permit could deadlock when
     * every permit is held that way. It checks the request deadline between calls, so it
     * never holds that permit past the point the caller has given up.
     */
    private BatchOutcome embedBatch(List<String> texts, long deadlineNanos, long requestDeadlineNanos) {
        try {
            return callAndParse(texts, deadlineNanos);
        } catch (CceStatusException e) {
            if (e.status != 400 || texts.size() == 1) {
                throw e;
            }
            String msg = String.valueOf(e.getMessage());
            log.warn("event=cce_batch_rejected status=400 chunks={} fallback=per_text body={}",
                     texts.size(), msg.substring(0, Math.min(200, msg.length())));
            List<float[]> vectors = new ArrayList<>(texts.size());
            long tokens = 0L;
            for (int i = 0; i < texts.size(); i++) {
                String text = texts.get(i);
                if (RequestDeadlineProbe.expired(requestDeadlineNanos, System.nanoTime())) {
                    activityTracker.recordDeadlineAbort();
                    log.warn("event=embed_deadline_exceeded embedder=cce phase=per_text_fallback "
                            + "chunks_done={} chunks_total={} retry_after_s={}",
                            i, texts.size(), RequestDeadlineExceededException.DEFAULT_RETRY_AFTER_SECONDS);
                    throw new RequestDeadlineExceededException(
                            "embed deadline exceeded in the per-text fallback after " + i + "/"
                                    + texts.size() + " texts of a refused batch",
                            RequestDeadlineExceededException.DEFAULT_RETRY_AFTER_SECONDS);
                }
                BatchOutcome one = callAndParse(List.of(text), deadlineNanos);
                vectors.addAll(one.vectors());
                tokens += one.tokens();
            }
            return new BatchOutcome(vectors, tokens, 0L, 0L);
        }
    }

    private BatchOutcome callAndParse(List<String> texts, long deadlineNanos) {
        String body = callApi(buildJson(texts), deadlineNanos);
        try {
            return parseBatch(body, texts.size());
        } catch (CceStatusException e) {
            throw e;
        } catch (Exception e) {
            String first = texts.get(0);
            throw new RuntimeException("CCE parse failed for a batch of " + texts.size()
                    + " starting with: " + first.substring(0, Math.min(40, first.length())), e);
        }
    }

    /** Test-only (nexus-8hdg9 phase 4): {@link #inFlight}'s free permits, so a test can
     * assert the fan-out's permit count returns to the constructor's {@code parallelism}
     * after a deadline abort, once the already-dispatched siblings drain. */
    int inFlightAvailablePermits() {
        return inFlight.availablePermits();
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

    // ── Request and response ──────────────────────────────────────────────────

    /** Package-private for tests: the wire body for {@code texts}, each its own
     *  single-chunk document (nexus-u2mlh.1, shape C). */
    String buildJson(List<String> texts) {
        // ContextualizedEmbedding.create's parameters:
        //   inputs=[[t0],[t1],...]: each text its own single-chunk document
        //   input_type: "document" for indexing, "query" for search (passed through)
        //   encoding_format="base64": Python SDK default — gives exact float32 binary
        //   No output_dtype field — production does not set it
        //   No truncation field — CCE API does not accept it
        Map<String, Object> body = new HashMap<>();
        body.put("model",           "voyage-context-3");
        List<List<String>> inputs = new ArrayList<>(texts.size());
        for (String t : texts) inputs.add(List.of(t));
        body.put("inputs",          inputs);
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
     * directly with a seeded jitter {@link Random} and assert the formula without
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
     * <p><strong>Scope note — RESOLVED (nexus-1vpal).</strong> The three
     * near-identical {@code callApi} retry loops this note used to flag are
     * consolidated into {@link VoyageRetryLoop}; this method is a thin
     * delegator kept package-private so the seeded-jitter formula tests
     * continue to exercise the exact instance CCE retries with.
     */
    long backoffDelayMs(int attempt) {
        return retryLoop.backoffDelayMs(attempt);
    }

    // nexus-ehc4q billing note: on transient-error retries, usage.total_tokens is
    // taken from the final successful response only; tokens from prior failed
    // attempts are not accumulated — a billing UNDER-count on retried calls (safe
    // direction: under-charges the customer). Documented, not corrected.
    /**
     * Parse a {@code Retry-After} header value as milliseconds. Thin
     * delegator to {@link VoyageRetryLoop#parseRetryAfterMs} (nexus-1vpal
     * consolidation), kept package-private for the pre-existing direct unit
     * tests.
     */
    static Long parseRetryAfterMs(String value) {
        return VoyageRetryLoop.parseRetryAfterMs(value);
    }

    /** Shared 429 deadline for ONE whole embed request (critic fold, see
     *  {@link #RATE_LIMIT_BUDGET_MS}). */
    private long newEmbedDeadlineNanos() {
        return retryLoop.newDeadlineNanos();
    }

    private String callApi(String json, long deadlineNanos) {
        // nexus-99r7y semantics, now in the consolidated VoyageRetryLoop:
        // 429s are BUDGET-bounded (throttling is pacing, not failure), 5xx /
        // network failures keep MAX_RETRIES attempt-bounded semantics. The
        // deadline is SHARED across the whole embed request's fan-out —
        // passed in, never recomputed here.
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .header("Authorization", "Bearer " + apiKey)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json))
                .timeout(Duration.ofSeconds(120))
                .build();
        return retryLoop.send(http, req, deadlineNanos, CCE_FAILURES);
    }

    // ── Response parsers ──────────────────────────────────────────────────────

    /**
     * Parse a CCE base64 response for {@code expected} single-chunk documents: {@code
     * data[i]} is document {@code i} (sorted by {@code index}), and its one chunk's
     * {@code embedding} is the vector. {@code usage.total_tokens} is the billed count
     * (bead nexus-ehc4q). A count mismatch is an error, never a silent misalignment.
     */
    @SuppressWarnings("unchecked")
    private BatchOutcome parseBatch(String body, int expected) throws Exception {
        Map<String, Object> root = mapper.readValue(body, Map.class);
        List<Map<String, Object>> outerData = (List<Map<String, Object>>) root.get("data");
        if (outerData == null || outerData.size() != expected) {
            throw new RuntimeException("CCE response has " + (outerData == null ? 0 : outerData.size())
                    + " documents for " + expected + " inputs");
        }
        outerData.sort(Comparator.comparingInt(m -> ((Number) m.get("index")).intValue()));
        List<float[]> vectors = new ArrayList<>(expected);
        for (Map<String, Object> doc : outerData) {
            List<Map<String, Object>> innerData = (List<Map<String, Object>>) doc.get("data");
            if (innerData == null || innerData.isEmpty()) {
                throw new RuntimeException("CCE response: doc group has empty data array");
            }
            innerData.sort(Comparator.comparingInt(m -> ((Number) m.get("index")).intValue()));
            Object emb = innerData.get(0).get("embedding");
            if (emb == null) throw new RuntimeException("CCE response: chunk missing 'embedding'");
            if (emb instanceof String b64) {
                vectors.add(decodeBase64Float32(b64));
            } else {
                List<Number> rawEmb = (List<Number>) emb;
                float[] vec = new float[rawEmb.size()];
                for (int i = 0; i < rawEmb.size(); i++) vec[i] = rawEmb.get(i).floatValue();
                vectors.add(vec);
            }
        }
        long tokens = 0L;
        Map<String, Object> usage = (Map<String, Object>) root.get("usage");
        if (usage != null && usage.get("total_tokens") instanceof Number t) {
            tokens = t.longValue();
        }
        return new BatchOutcome(vectors, tokens, 0L, 0L);
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
