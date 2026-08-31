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
import java.util.Base64;
import java.util.List;
import java.util.Optional;
import java.util.Random;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-1vpal — {@link VoyageEmbedder} carries the nexus-99r7y rate-limit
 * semantics via the consolidated {@link VoyageRetryLoop}: 429s are
 * budget-bounded (not {@code MAX_RETRIES}-bounded) with {@code Retry-After}
 * honoured and a typed {@link UpstreamRateLimitedException} on exhaustion,
 * while 5xx keeps the old 3-attempt contract. {@code code__} collections
 * share the same account-wide Voyage RPM ceiling the 2026-08-15 incident hit
 * through CCE, so this is the same incident shape on the sibling path.
 *
 * <p>Hermetic (mirrors {@code VoyageRerankerTest}): {@link HttpServer} on
 * 127.0.0.1 port 0 scripts {@code POST /v1/embeddings} responses; no network,
 * no API key. The package-private constructor injects the fake URL, a fast
 * retry base, a jitter source, and the 429 budget so the fail-fast tests need
 * essentially no wall clock.
 */
class VoyageEmbedderRateLimitTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private HttpServer server;
    private String url;
    /** Scripted non-200 responses, consumed one per request:
     *  [status, body, retryAfterHeaderOrNull]. Queue empty → generated 200. */
    private final ConcurrentLinkedQueue<Object[]> responses = new ConcurrentLinkedQueue<>();
    private final List<String> requestBodies = new CopyOnWriteArrayList<>();

    @BeforeEach
    void startFakeVoyage() throws Exception {
        responses.clear();
        requestBodies.clear();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/embeddings", exchange -> {
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            requestBodies.add(body);
            Object[] scripted = responses.poll();
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            if (scripted == null || (Integer) scripted[0] == 200) {
                // Queue empty, or an explicitly scripted 200: generate a
                // well-formed response for THIS request's inputs.
                byte[] ok = embedResponseFor(body).getBytes(StandardCharsets.UTF_8);
                exchange.sendResponseHeaders(200, ok.length);
                try (OutputStream os = exchange.getResponseBody()) { os.write(ok); }
                return;
            }
            if (scripted[2] != null) {
                exchange.getResponseHeaders().set("Retry-After", (String) scripted[2]);
            }
            byte[] bytes = ((String) scripted[1]).getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders((Integer) scripted[0], bytes.length);
            try (OutputStream os = exchange.getResponseBody()) { os.write(bytes); }
        });
        server.start();
        url = "http://127.0.0.1:" + server.getAddress().getPort() + "/v1/embeddings";
    }

    @AfterEach
    void stopFakeVoyage() {
        server.stop(0);
    }

    private void respond(int status, String body) {
        responses.add(new Object[] {status, body, null});
    }

    private void respondWithRetryAfter(int status, String body, String retryAfter) {
        responses.add(new Object[] {status, body, retryAfter});
    }

    /** Well-formed /v1/embeddings 200 for however many inputs the request carried. */
    private static String embedResponseFor(String requestJson) {
        try {
            JsonNode input = MAPPER.readTree(requestJson).get("input");
            StringBuilder data = new StringBuilder();
            for (int i = 0; i < input.size(); i++) {
                if (i > 0) data.append(", ");
                ByteBuffer buf = ByteBuffer.allocate(16).order(ByteOrder.LITTLE_ENDIAN);
                for (int d = 0; d < 4; d++) buf.putFloat(i + d / 10f);
                data.append("{\"index\": ").append(i).append(", \"embedding\": \"")
                    .append(Base64.getEncoder().encodeToString(buf.array())).append("\"}");
            }
            return "{\"object\": \"list\", \"data\": [" + data + "], "
                    + "\"usage\": {\"total_tokens\": " + (input.size() * 7) + "}}";
        } catch (Exception e) {
            throw new RuntimeException("failed to parse fake Voyage request body", e);
        }
    }

    /** retryBaseMs=5 keeps retry tests fast; production uses 500 / the 20s budget. */
    private VoyageEmbedder embedderWithBudget(long budgetMs) {
        return new VoyageEmbedder("test-key", "voyage-code-3", url, 5L,
                                  Optional.empty(), new Random(), budgetMs);
    }

    @Test
    void sustained429IsBudgetBoundedNotAttemptBoundedAndTyped() {
        // The 2026-08-15 incident class on the sibling path: old behavior
        // burned MAX_RETRIES(3) attempts and threw a generic RuntimeException
        // → opaque 500. New behavior keeps absorbing 429s past 3 attempts
        // while the budget lasts, then fails TYPED.
        for (int i = 0; i < 500; i++) respond(429, "{\"detail\": \"rate limited\"}");

        assertThatThrownBy(() -> embedderWithBudget(2_500L).embed(List.of("alpha")))
                .isInstanceOf(UpstreamRateLimitedException.class);
        assertThat(requestBodies.size())
                .as("429s must be budget-bounded, not capped at 3 attempts")
                .isGreaterThan(3);
    }

    @Test
    void retryAfterExceedingBudgetFailsFastTyped() {
        // Voyage says "come back in 60s" but the budget is 500ms: sleeping
        // would guarantee an edge-timeout 5xx, so the call must fail fast
        // with the typed exception carrying an honest Retry-After — after
        // exactly ONE upstream request and essentially no wall clock.
        respondWithRetryAfter(429, "{\"detail\": \"rate limited\"}", "60");

        long start = System.nanoTime();
        assertThatThrownBy(() -> embedderWithBudget(500L).embed(List.of("alpha")))
                .isInstanceOf(UpstreamRateLimitedException.class)
                .satisfies(e -> assertThat(
                        ((UpstreamRateLimitedException) e).retryAfterSeconds())
                        .isEqualTo(60L));
        long elapsedMs = (System.nanoTime() - start) / 1_000_000L;
        assertThat(elapsedMs)
                .as("fail-fast must not sleep out the advertised Retry-After")
                .isLessThan(5_000L);
        assertThat(requestBodies).hasSize(1);
    }

    @Test
    void retryAfterWithinBudgetIsHonouredThenSucceeds() {
        respondWithRetryAfter(429, "{\"detail\": \"rate limited\"}", "1");

        long start = System.nanoTime();
        List<float[]> out = embedderWithBudget(20_000L).embed(List.of("alpha"));
        long elapsedMs = (System.nanoTime() - start) / 1_000_000L;

        assertThat(out).hasSize(1);
        assertThat(elapsedMs)
                .as("the sleep must honour the Retry-After header, not just the jitter base")
                .isGreaterThanOrEqualTo(900L);
        assertThat(requestBodies).hasSize(2);
    }

    @Test
    void serverErrorsKeepAttemptBoundedSemanticsAndTheRequestCounter() {
        // 5xx retains the old MAX_RETRIES contract even though 429 no longer
        // does — regression pin against the consolidation widening it. Also
        // pins the voyageRequestCount onPost hook surviving the migration
        // into VoyageRetryLoop (it is the batch-split suite's non-vacuity
        // instrument).
        for (int i = 0; i < 10; i++) respond(500, "{\"detail\": \"upstream sad\"}");

        VoyageEmbedder embedder = embedderWithBudget(20_000L);
        assertThatThrownBy(() -> embedder.embed(List.of("alpha")))
                .isInstanceOf(RuntimeException.class)
                .isNotInstanceOf(UpstreamRateLimitedException.class)
                .hasMessageContaining("500");
        assertThat(requestBodies).hasSize(3);
        assertThat(embedder.voyageRequestCount()).isEqualTo(3);
    }

    @Test
    void oneBudgetSpansAllPlannedSubBatchesNeverOnePerBatch() {
        // nexus-1vpal core semantics: the deadline is minted ONCE per
        // top-level embed call in executeWithHalving and shared across every
        // planned sub-batch — never re-armed per batch. Two ~300KB texts
        // exceed voyage-code-3's 120K-token budget together, so planBatches
        // emits TWO planned batches ([a...], [b...]).
        //
        // DETERMINISTIC DISCRIMINATOR (code-review finding 1, 2026-08-31 —
        // the first version scripted unconditional 429s, under which batch
        // one's typed failure aborts the embed in BOTH designs, so it could
        // not falsify per-batch re-arming): batch one absorbs a
        // Retry-After: 2 sleep (2000ms of the 3500ms budget; permitted,
        // 2000+1000 < 3500) then SUCCEEDS; batch two then meets one 429
        // whose honoured 2000ms delay no longer fits the REMAINDER
        // (~1450ms): shared deadline → immediate typed failure after
        // exactly ONE batch-two request. A per-batch re-minted deadline
        // would grant batch two a fresh 3500ms, sleep the 2000ms, hit the
        // empty-queue generated 200, and the embed would SUCCEED — making
        // assertThatThrownBy itself the falsifier, no wall-clock margins.
        respondWithRetryAfter(429, "{\"detail\": \"rate limited\"}", "2");
        respond(200, "");  // generated success for batch one's retry
        respondWithRetryAfter(429, "{\"detail\": \"rate limited\"}", "2");
        String a = "a".repeat(300_000);
        String b = "b".repeat(300_000);

        assertThatThrownBy(() -> embedderWithBudget(3_500L).embed(List.of(a, b)))
                .isInstanceOf(UpstreamRateLimitedException.class);
        assertThat(requestBodies.stream().filter(r -> r.contains("aaa")).count())
                .as("batch one: the budget-permitted 429 + the successful retry")
                .isEqualTo(2);
        assertThat(requestBodies.stream().filter(r -> r.contains("bbb")).count())
                .as("batch two: exactly one attempt — the shared remainder cannot "
                    + "honour Retry-After: 2, so it fails typed without re-arming")
                .isEqualTo(1);
    }

    @Test
    void interleaved429sDoNotConsumeTheServerErrorAttemptCounter() {
        // Critic finding 3 (2026-08-31), pinned deliberately: the OLD loop
        // shared one 3-attempt counter across 429+5xx+network, so the
        // sequence 500,429,500 already exhausted it (3 requests total). The
        // consolidated loop counts 5xx/network on their own MAX_RETRIES=3
        // counter while 429s run against the budget — so 500,429,500,429,500
        // makes all FIVE upstream requests before failing generic (never
        // typed rate-limited: the budget was nowhere near exhausted).
        respond(500, "{\"detail\": \"upstream sad\"}");
        respond(429, "{\"detail\": \"rate limited\"}");
        respond(500, "{\"detail\": \"upstream sad\"}");
        respond(429, "{\"detail\": \"rate limited\"}");
        respond(500, "{\"detail\": \"upstream sad\"}");

        assertThatThrownBy(() -> embedderWithBudget(20_000L).embed(List.of("alpha")))
                .isInstanceOf(RuntimeException.class)
                .isNotInstanceOf(UpstreamRateLimitedException.class)
                .hasMessageContaining("500");
        assertThat(requestBodies)
                .as("429s are budget-paced, not counted against the 5xx cap — "
                    + "all five scripted responses must be consumed")
                .hasSize(5);
    }
}
