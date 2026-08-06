// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Random;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-9okyk — {@link CceEmbedder}'s bounded-parallel fan-out against a fake
 * Voyage CCE upstream.
 *
 * <p>Hermetic, mirroring {@code VoyageRerankerTest}: {@link HttpServer} on
 * 127.0.0.1 port 0 scripts {@code POST /v1/contextualizedembeddings}
 * responses; no network, no API key. The package-private constructor injects
 * the fake URL, a fast retry base, an explicit parallelism bound, and an
 * empty proxy selector. The fake server runs its own bounded thread pool
 * ({@link #startFakeVoyage}) so it can actually serve concurrent requests —
 * the default {@link HttpServer} executor serves one request at a time,
 * which would silently mask whether the CLIENT fires calls concurrently.
 *
 * <p>The fake computes a DETERMINISTIC vector from each request's text (a
 * pure function of content, independent of call order or thread), which is
 * what makes bit-identity and order-preservation checkable without
 * hand-maintaining a second copy of the old sequential implementation: calling
 * the (now inherently parallel) {@code embed(List)} with N texts at once must
 * produce exactly the same vectors, in exactly the same positions, as calling
 * it N times with a single text each (which is trivially sequential — no
 * cross-chunk interleaving is possible with n=1).
 *
 * <p>Contract under test (bead nexus-9okyk, T2 {@code
 * nexus/embed-path-latency-analysis-2026-08-06} R1):
 * <ul>
 *   <li>(a) bit-identity: parallel batch call == N sequential single-text calls;</li>
 *   <li>(b) order preservation under ADVERSARIAL completion order (last chunk
 *       answers first);</li>
 *   <li>(c) failure propagation: one chunk's terminal failure fails the whole
 *       request, never a partial result;</li>
 *   <li>(d) concurrency actually happens (max-in-flight &gt; 1, deterministic
 *       counter, not wall-clock);</li>
 *   <li>(e) the constructor's parallelism bound is never exceeded;</li>
 *   <li>per-call 429 retry composes with the parallel fan-out (evidence for
 *       requirement 4 in the bead: the retry lives inside each worker, so N
 *       concurrent workers each retry independently).</li>
 * </ul>
 */
class CceEmbedderParallelTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private HttpServer server;
    private String url;
    private ExecutorService serverExecutor;

    /** text -> configured artificial latency (ms) before responding. */
    private final Map<String, Long> latencyMs = new ConcurrentHashMap<>();
    /** text -> remaining scripted failure count (each hit returns 500, then decrements). */
    private final Map<String, AtomicInteger> failuresRemaining = new ConcurrentHashMap<>();
    /** text -> remaining scripted 429 count (each hit returns 429, then decrements). */
    private final Map<String, AtomicInteger> rateLimitsRemaining = new ConcurrentHashMap<>();

    private final List<String> requestTexts = new CopyOnWriteArrayList<>();
    private final AtomicInteger inFlight = new AtomicInteger(0);
    private final AtomicInteger maxInFlight = new AtomicInteger(0);

    @BeforeEach
    void startFakeVoyage() throws Exception {
        latencyMs.clear();
        failuresRemaining.clear();
        rateLimitsRemaining.clear();
        requestTexts.clear();
        inFlight.set(0);
        maxInFlight.set(0);

        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        // A single-threaded default HttpServer executor would serialize every
        // request regardless of what the client does — defeats the purpose of
        // testing client-side concurrency. Give it real headroom.
        serverExecutor = Executors.newFixedThreadPool(32);
        server.setExecutor(serverExecutor);
        server.createContext("/v1/contextualizedembeddings", exchange -> {
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            String text = extractText(body);
            requestTexts.add(text);

            int current = inFlight.incrementAndGet();
            maxInFlight.updateAndGet(prev -> Math.max(prev, current));
            try {
                Long delay = latencyMs.get(text);
                if (delay != null && delay > 0) {
                    Thread.sleep(delay);
                }

                AtomicInteger rl = rateLimitsRemaining.get(text);
                if (rl != null && rl.get() > 0) {
                    rl.decrementAndGet();
                    sendStatus(exchange, 429, "{\"detail\": \"rate limited\"}");
                    return;
                }
                AtomicInteger fail = failuresRemaining.get(text);
                if (fail != null && fail.get() > 0) {
                    fail.decrementAndGet();
                    sendStatus(exchange, 500, "{\"detail\": \"upstream sad\"}");
                    return;
                }
                sendStatus(exchange, 200, cceResponseBody(text));
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            } finally {
                inFlight.decrementAndGet();
            }
        });
        server.start();
        url = "http://127.0.0.1:" + server.getAddress().getPort() + "/v1/contextualizedembeddings";
    }

    @AfterEach
    void stopFakeVoyage() {
        server.stop(0);
        serverExecutor.shutdownNow();
    }

    private static void sendStatus(com.sun.net.httpserver.HttpExchange exchange, int status, String body)
            throws java.io.IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(bytes);
        }
    }

    /**
     * {@code {"model":..., "inputs":[["text"]], "input_type":..., "encoding_format":"base64"}}
     * → "text". Unchecked on parse failure — called from inside the {@code
     * HttpHandler} lambda, whose functional interface only permits {@link
     * java.io.IOException}.
     */
    private static String extractText(String requestBody) {
        try {
            JsonNode root = MAPPER.readTree(requestBody);
            return root.get("inputs").get(0).get(0).asText();
        } catch (Exception e) {
            throw new RuntimeException("failed to parse fake CCE request body: " + requestBody, e);
        }
    }

    /** Deterministic vector from text content — same text always yields the same bytes, any thread, any order. */
    private static float[] vectorFor(String text) {
        int h = text.hashCode();
        float[] v = new float[4];
        for (int i = 0; i < 4; i++) {
            v[i] = ((h >>> (i * 8)) & 0xFF) / 255f;
        }
        return v;
    }

    private static String encodeBase64Float32(float[] vec) {
        ByteBuffer buf = ByteBuffer.allocate(vec.length * 4).order(ByteOrder.LITTLE_ENDIAN);
        for (float f : vec) buf.putFloat(f);
        return Base64.getEncoder().encodeToString(buf.array());
    }

    private static String cceResponseBody(String text) {
        String b64 = encodeBase64Float32(vectorFor(text));
        return "{\"data\": [{\"index\": 0, \"data\": [{\"index\": 0, \"embedding\": \"" + b64 + "\"}]}]}";
    }

    private CceEmbedder embedder(int parallelism) {
        // retryBaseMs=5 keeps retry tests fast; production uses 500.
        return new CceEmbedder("test-key", "document", url, 5L, parallelism, Optional.empty());
    }

    private CceEmbedder embedderWithJitterSeed(int parallelism, long seed) {
        return new CceEmbedder("test-key", "document", url, 5L, parallelism, Optional.empty(), new Random(seed));
    }

    // ── (a) bit-identity: parallel batch == N sequential single-text calls ───

    @Test
    void parallelBatchProducesBitIdenticalVectorsToSequentialSingleCalls() {
        List<String> texts = List.of("alpha chunk", "beta chunk", "gamma chunk", "delta chunk", "epsilon chunk");
        try (CceEmbedder cce = embedder(8)) {
            // Reference: N calls of size 1 — trivially sequential, no reordering possible.
            List<float[]> sequential = new ArrayList<>();
            for (String t : texts) {
                sequential.add(cce.embed(List.of(t)).get(0));
            }

            // Under test: one call with all N texts — the parallel fan-out.
            List<float[]> parallel = cce.embed(texts);

            assertThat(parallel).hasSize(sequential.size());
            for (int i = 0; i < texts.size(); i++) {
                assertThat(parallel.get(i)).as("position %d", i).isEqualTo(sequential.get(i));
                assertThat(parallel.get(i)).isEqualTo(vectorFor(texts.get(i)));
            }
        }
    }

    // ── (b) order preservation under adversarial completion order ────────────

    @Test
    void orderPreservedWhenLastChunkCompletesFirst() {
        List<String> texts = List.of("chunk-0-slow", "chunk-1-mid", "chunk-2-fast");
        // Inverted latency: the LAST text answers essentially immediately, the
        // FIRST text is the slowest to answer — an adversarial completion order
        // for any implementation that (incorrectly) assembled results by
        // completion order instead of input index.
        latencyMs.put("chunk-0-slow", 300L);
        latencyMs.put("chunk-1-mid", 120L);
        latencyMs.put("chunk-2-fast", 0L);

        try (CceEmbedder cce = embedder(8)) {
            List<float[]> result = cce.embed(texts);

            assertThat(result).hasSize(3);
            for (int i = 0; i < texts.size(); i++) {
                assertThat(result.get(i)).as("position %d maps to its own input text", i)
                        .isEqualTo(vectorFor(texts.get(i)));
            }
        }
    }

    @Test
    void embedWithUsageOrderAndTokenSumPreservedUnderAdversarialCompletion() {
        List<String> texts = List.of("u-chunk-0", "u-chunk-1", "u-chunk-2", "u-chunk-3");
        latencyMs.put("u-chunk-0", 200L);
        latencyMs.put("u-chunk-1", 0L);
        latencyMs.put("u-chunk-2", 150L);
        latencyMs.put("u-chunk-3", 0L);

        try (CceEmbedder cce = embedder(8)) {
            EmbedResult result = cce.embedWithUsage(texts);

            assertThat(result.embeddings()).hasSize(4);
            for (int i = 0; i < texts.size(); i++) {
                assertThat(result.embeddings().get(i)).isEqualTo(vectorFor(texts.get(i)));
            }
            // Every fake response omits "usage" (0 tokens) — the sum being
            // exactly 0 here just confirms summation ran over all 4 without
            // throwing; the token-accumulation shape itself is exercised by
            // the pre-existing sequential nexus-ehc4q behavior (unchanged).
            assertThat(result.tokens()).isEqualTo(0L);
        }
    }

    // ── (c) failure propagation — no partial-silent-success ───────────────────

    @Test
    void oneChunkTerminalFailureFailsTheWholeRequest() {
        List<String> texts = List.of("ok-1", "ok-2", "poison-chunk", "ok-3");
        // Exceeds MAX_RETRIES (3) so it is terminal, not merely retried-through.
        failuresRemaining.put("poison-chunk", new AtomicInteger(10));

        try (CceEmbedder cce = embedder(8)) {
            assertThatThrownBy(() -> cce.embed(texts))
                    .isInstanceOf(RuntimeException.class)
                    .hasMessageContaining("500");
        }
    }

    @Test
    void embedWithUsagePropagatesTerminalFailureTheSameWay() {
        List<String> texts = List.of("wu-ok-1", "wu-poison", "wu-ok-2");
        failuresRemaining.put("wu-poison", new AtomicInteger(10));

        try (CceEmbedder cce = embedder(8)) {
            assertThatThrownBy(() -> cce.embedWithUsage(texts))
                    .isInstanceOf(RuntimeException.class);
        }
    }

    // ── retry composes with the fan-out (requirement 4 evidence) ─────────────

    @Test
    void perCallRetryOn429ComposesWithParallelFanOut() {
        List<String> texts = List.of("rl-a", "rl-b", "rl-c", "rl-d");
        // Every text gets rate-limited once, then succeeds — proves each of the
        // N concurrent workers retries its OWN call independently (the retry
        // loop lives inside callApi, invoked once per worker, unmodified by
        // the fan-out).
        for (String t : texts) {
            rateLimitsRemaining.put(t, new AtomicInteger(1));
        }

        try (CceEmbedder cce = embedder(8)) {
            List<float[]> result = cce.embed(texts);

            assertThat(result).hasSize(4);
            for (int i = 0; i < texts.size(); i++) {
                assertThat(result.get(i)).isEqualTo(vectorFor(texts.get(i)));
            }
            // Each text was requested twice: the 429 attempt + the retry that succeeded.
            long rlARequests = requestTexts.stream().filter(t -> t.equals("rl-a")).count();
            assertThat(rlARequests).isEqualTo(2);
        }
    }

    // ── (d) concurrency actually happens ──────────────────────────────────────

    @Test
    void chunksAreEmbeddedConcurrentlyNotSequentially() {
        List<String> texts = new ArrayList<>();
        for (int i = 0; i < 8; i++) {
            String t = "conc-chunk-" + i;
            texts.add(t);
            latencyMs.put(t, 80L);
        }

        try (CceEmbedder cce = embedder(8)) {
            List<float[]> result = cce.embed(texts);

            assertThat(result).hasSize(8);
            // If this were still the old sequential loop, in-flight would never
            // exceed 1. A bounded-parallel fan-out with 8 permits and 8 texts of
            // equal latency should let several requests overlap.
            assertThat(maxInFlight.get())
                    .as("max concurrent in-flight Voyage calls observed by the fake server")
                    .isGreaterThan(1);
        }
    }

    // ── (e) executor bound is respected ───────────────────────────────────────

    @Test
    void concurrencyNeverExceedsTheConfiguredParallelismBound() {
        int bound = 4;
        List<String> texts = new ArrayList<>();
        for (int i = 0; i < 20; i++) {
            String t = "bound-chunk-" + i;
            texts.add(t);
            latencyMs.put(t, 60L);
        }

        try (CceEmbedder cce = embedder(bound)) {
            List<float[]> result = cce.embed(texts);

            assertThat(result).hasSize(20);
            assertThat(maxInFlight.get())
                    .as("max concurrent in-flight Voyage calls must never exceed the bound")
                    .isLessThanOrEqualTo(bound);
            assertThat(maxInFlight.get())
                    .as("concurrency actually happened, not accidentally serialized to 1")
                    .isGreaterThan(1);
        }
    }

    // ── close() lifecycle ──────────────────────────────────────────────────────

    @Test
    void closeShutsDownExecutorAndIsIdempotent() {
        CceEmbedder cce = embedder(4);
        assertThat(cce.embed(List.of("pre-close chunk"))).hasSize(1);

        cce.close();
        cce.close(); // idempotent — must not throw

        assertThatThrownBy(() -> cce.embed(List.of("post-close chunk")))
                .isInstanceOf(RuntimeException.class);
    }

    // ── retry jitter (nexus-9okyk critic fix 1) — pure formula, no timing ────

    @Test
    void backoffDelayFallsWithinEqualJitterBounds() {
        // retryBaseMs=5 (embedderWithJitterSeed): cap(attempt) = 5 * 2^(attempt-1).
        // Equal Jitter: delay in [cap/2, cap) for every attempt, every draw.
        try (CceEmbedder cce = embedderWithJitterSeed(4, 1L)) {
            for (int attempt = 1; attempt <= 3; attempt++) {
                long cap = 5L * (1L << (attempt - 1));
                long floor = cap / 2;
                for (int trial = 0; trial < 50; trial++) {
                    long delay = cce.backoffDelayMs(attempt);
                    assertThat(delay)
                            .as("attempt %d trial %d must be in [%d, %d)", attempt, trial, floor, cap)
                            .isGreaterThanOrEqualTo(floor)
                            .isLessThan(cap);
                }
            }
        }
    }

    @Test
    void backoffDelayIsReproducibleFromASeedButVariesAcrossSuccessiveCalls() {
        // Same seed -> the FIRST draw is deterministic and reproducible (what
        // makes the formula testable at all): two independently constructed
        // embedders, same seed, must agree on their first delay.
        long seed = 42L;
        long firstFromInstanceA;
        long firstFromInstanceB;
        try (CceEmbedder a = embedderWithJitterSeed(4, seed)) {
            firstFromInstanceA = a.backoffDelayMs(2);
        }
        try (CceEmbedder b = embedderWithJitterSeed(4, seed)) {
            firstFromInstanceB = b.backoffDelayMs(2);
        }
        assertThat(firstFromInstanceA).isEqualTo(firstFromInstanceB);

        // The core anti-lockstep property: repeated retries on the SAME
        // instance (the same shape a single worker's retry loop would hit) do
        // NOT keep producing the identical delay — pre-fix, backoffDelayMs's
        // predecessor (the raw exponential formula) was a pure function of
        // `attempt` alone, so every retry at the same attempt number got the
        // IDENTICAL delay; that identity across calls is exactly what let
        // concurrent workers wake in lockstep. Post-fix, successive calls at
        // the same attempt diverge because the PRNG stream advances.
        try (CceEmbedder cce = embedderWithJitterSeed(4, seed)) {
            long first = cce.backoffDelayMs(2);
            boolean sawDifferentValue = false;
            for (int i = 0; i < 20 && !sawDifferentValue; i++) {
                if (cce.backoffDelayMs(2) != first) {
                    sawDifferentValue = true;
                }
            }
            assertThat(sawDifferentValue)
                    .as("successive backoffDelayMs(2) calls must not all collapse to the same value")
                    .isTrue();
        }
    }
}
