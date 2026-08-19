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
import java.util.Collections;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-195 bead nexus-kmtlp.10 — {@link VoyageEmbedder}'s token-aware sub-batch planner and
 * adaptive halving, against a fake Voyage {@code /v1/embeddings} upstream.
 *
 * <p>Hermetic, mirroring {@code VoyageRerankerTest} / {@code CceEmbedderParallelTest}:
 * {@link HttpServer} on 127.0.0.1 port 0, the RDR-195 test-seam constructor injects the fake
 * URL, a fast retry base, and {@code Optional.empty()} so an ambient {@code HTTPS_PROXY} can
 * never route the localhost upstream.
 *
 * <p>Default success response: one deterministic content-derived vector per input text
 * (independent of thread/order — same shape as {@code CceEmbedderParallelTest.vectorFor}),
 * emitted in REVERSED {@code index} order within each response's {@code data[]} array (so
 * the pre-existing sort-by-index parse path stays genuinely exercised under sub-batching),
 * with {@code usage.total_tokens} set to the sum of {@code text.length()} for exactly the
 * texts in that one request — letting {@link #embedWithUsageSums} assert against the FULL
 * input's total without hand-computing per-request splits. Tests needing a specific upstream
 * failure (400/exhaustion) push an explicit scripted response consumed FIFO ahead of the
 * default.
 */
class VoyageEmbedderBatchSplitTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private HttpServer server;
    private String url;
    /** Scripted responses, consumed one per request ahead of the default handler: [status, body]. */
    private final ConcurrentLinkedQueue<Object[]> scripted = new ConcurrentLinkedQueue<>();
    private final List<String> requestBodies = new CopyOnWriteArrayList<>();

    @BeforeEach
    void startFakeVoyage() throws Exception {
        scripted.clear();
        requestBodies.clear();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/embeddings", exchange -> {
            String reqBody = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            requestBodies.add(reqBody);

            Object[] script = scripted.poll();
            int status;
            byte[] respBytes;
            if (script != null) {
                status = (Integer) script[0];
                respBytes = ((String) script[1]).getBytes(StandardCharsets.UTF_8);
            } else {
                status = 200;
                respBytes = defaultSuccessBody(reqBody).getBytes(StandardCharsets.UTF_8);
            }
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, respBytes.length);
            try (OutputStream os = exchange.getResponseBody()) {
                os.write(respBytes);
            }
        });
        server.start();
        url = "http://127.0.0.1:" + server.getAddress().getPort() + "/v1/embeddings";
    }

    @AfterEach
    void stopFakeVoyage() {
        server.stop(0);
    }

    private VoyageEmbedder embedder(String model) {
        // retryBaseMs=5 keeps retry/exhaustion tests fast; production uses 500.
        return new VoyageEmbedder("test-key", model, url, 5L, Optional.empty());
    }

    private static void respond(ConcurrentLinkedQueue<Object[]> queue, int status, String body) {
        queue.add(new Object[] {status, body});
    }

    private static final String TOO_MANY_TOKENS_BODY =
            "{\"detail\": \"Request to model failed. The max allowed tokens per submitted batch"
            + " is 120000. Please lower the number of tokens in the batch.\","
            + " \"error_code\": \"TOO_MANY_TOKENS_IN_BATCH\"}";

    // ── Fake-server response construction ─────────────────────────────────────

    @SuppressWarnings("unchecked")
    private static String defaultSuccessBody(String reqBody) {
        try {
            JsonNode root = MAPPER.readTree(reqBody);
            List<String> texts = new ArrayList<>();
            root.get("input").forEach(n -> texts.add(n.asText()));

            long totalTokens = 0L;
            StringBuilder data = new StringBuilder("[");
            // Emit in REVERSED index order to keep the pre-existing sort-by-index parse
            // path genuinely exercised under sub-batching (Voyage documents out-of-order
            // data[]).
            for (int i = texts.size() - 1; i >= 0; i--) {
                if (data.length() > 1) data.append(", ");
                String b64 = encodeBase64Float32(vectorFor(texts.get(i)));
                data.append("{\"index\": ").append(i).append(", \"embedding\": \"").append(b64).append("\"}");
                totalTokens += texts.get(i).length();
            }
            data.append("]");
            return "{\"data\": " + data + ", \"usage\": {\"total_tokens\": " + totalTokens + "}}";
        } catch (Exception e) {
            throw new RuntimeException("failed to build default fake-Voyage response for: " + reqBody, e);
        }
    }

    /** Deterministic vector from text content — same text always yields the same bytes. */
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

    /** ~1 byte/char (ASCII repeat), so PROVISIONAL_BYTES_PER_TOKEN=4 gives ~approxBytes/4
     * estimated tokens — used to force the planner across per-model budgets deterministically. */
    private static String bigText(int approxBytes, char fill) {
        return String.valueOf(fill).repeat(approxBytes);
    }

    private List<String> requestInputTexts(int requestIndex) throws Exception {
        JsonNode root = MAPPER.readTree(requestBodies.get(requestIndex));
        List<String> out = new ArrayList<>();
        root.get("input").forEach(n -> out.add(n.asText()));
        return out;
    }

    // ── Scenario 1: over-budget input -> more than one POST, each under budget ──

    @Test
    void overBudgetInputProducesMultiplePostsEachUnderBudget() throws Exception {
        // 5 texts x ~150,000 bytes (~37,500 est. tokens each) = ~187,500 est. tokens total,
        // against voyage-code-3's 120,000 budget: greedy planning must split.
        List<String> texts = List.of(
                bigText(150_000, 'a'), bigText(150_000, 'b'), bigText(150_000, 'c'),
                bigText(150_000, 'd'), bigText(150_000, 'e'));

        VoyageEmbedder e = embedder("voyage-code-3");
        List<float[]> result = e.embed(texts);

        assertThat(result).hasSize(5);
        for (int i = 0; i < texts.size(); i++) {
            assertThat(result.get(i)).isEqualTo(vectorFor(texts.get(i)));
        }
        // THE non-vacuity assertion: a split actually happened.
        assertThat(requestBodies.size()).as("more than one POST for an over-budget batch").isGreaterThan(1);
        assertThat(e.voyageRequestCount()).isEqualTo(requestBodies.size());

        // Each individual request stayed under the model's token budget.
        for (int i = 0; i < requestBodies.size(); i++) {
            long estTokens = requestInputTexts(i).stream()
                    .mapToLong(t -> Math.max(t.getBytes(StandardCharsets.UTF_8).length / 4L, 1L))
                    .sum();
            assertThat(estTokens).as("request %d under the 120,000 budget", i).isLessThanOrEqualTo(120_000L);
        }
    }

    // ── Scenario 2: sub-batch responses map back to exact input positions ───────

    @Test
    void subBatchVectorsMapBackToExactInputPositions() throws Exception {
        List<String> texts = List.of(
                bigText(150_000, 'p'), bigText(150_000, 'q'), bigText(150_000, 'r'),
                bigText(150_000, 's'), bigText(150_000, 't'), bigText(150_000, 'u'));

        VoyageEmbedder e = embedder("voyage-code-3");
        List<float[]> result = e.embed(texts);

        assertThat(requestBodies.size()).isGreaterThan(1); // confirms this test actually spans sub-batches
        assertThat(result).hasSize(texts.size());
        for (int i = 0; i < texts.size(); i++) {
            assertThat(result.get(i)).as("position %d maps to its own input text", i)
                    .isEqualTo(vectorFor(texts.get(i)));
        }
    }

    // ── Scenario 3: multi-sub-batch embedWithUsage sums tokens across sub-calls ──

    @Test
    void embedWithUsageSumsTokensAcrossSubBatches() throws Exception {
        // 2 texts x 400,000 bytes (~100,000 est. tokens each): together ~200,000 > 120,000,
        // so voyage-code-3 must split into (at least) 2 single-text requests.
        String t0 = bigText(400_000, 'x');
        String t1 = bigText(400_000, 'y');
        List<String> texts = List.of(t0, t1);

        VoyageEmbedder e = embedder("voyage-code-3");
        EmbedResult result = e.embedWithUsage(texts);

        assertThat(requestBodies.size()).isGreaterThan(1);
        assertThat(result.embeddings()).hasSize(2);
        assertThat(result.embeddings().get(0)).isEqualTo(vectorFor(t0));
        assertThat(result.embeddings().get(1)).isEqualTo(vectorFor(t1));
        // Sum, not "last response's value": the default handler sets each response's
        // usage.total_tokens to that request's own text lengths, so a bug that used only
        // the FINAL sub-call's usage would under-report vs this full-input sum.
        assertThat(result.tokens()).isEqualTo((long) t0.length() + t1.length());
    }

    // ── Scenario 4: 400 on first attempt -> halves, retries, succeeds ───────────

    @Test
    void oversizeTooManyTokens400OnFirstAttemptHalvesAndRetries() throws Exception {
        List<String> texts = List.of("alpha", "beta", "gamma", "delta"); // tiny — fits comfortably
        respond(scripted, 400, TOO_MANY_TOKENS_BODY); // simulates an estimator miss

        VoyageEmbedder e = embedder("voyage-code-3");
        List<float[]> result = e.embed(texts);

        assertThat(result).hasSize(4);
        for (int i = 0; i < texts.size(); i++) {
            assertThat(result.get(i)).isEqualTo(vectorFor(texts.get(i)));
        }
        // 1 failed whole-batch attempt + 2 half-batch successes.
        assertThat(requestBodies).hasSize(3);
        assertThat(e.voyageRequestCount()).isEqualTo(3);
    }

    // ── Scenario 5: a single text alone still trips 400 -> typed error, nothing dropped ──

    @Test
    void singleTextStillOversizeRethrowsTypedErrorWithDetail() {
        respond(scripted, 400, TOO_MANY_TOKENS_BODY);

        VoyageEmbedder e = embedder("voyage-code-3");
        assertThatThrownBy(() -> e.embed(List.of("lonely text")))
                .isInstanceOf(VoyageTooManyTokensException.class)
                .hasMessageContaining("TOO_MANY_TOKENS_IN_BATCH");
        assertThat(requestBodies).hasSize(1); // un-splittable: no further attempt
    }

    // ── Scenario 6: input that already fits -> exactly one POST, byte-identical body ──

    @Test
    void fittingInputIssuesExactlyOnePostWithUnchangedBody() throws Exception {
        List<String> texts = List.of("The quick brown fox jumps over the lazy dog.", "second text");
        VoyageEmbedder e = embedder("voyage-code-3");

        List<float[]> result = e.embed(texts);

        assertThat(result).hasSize(2);
        assertThat(requestBodies).hasSize(1);
        assertThat(e.voyageRequestCount()).isEqualTo(1);
        // Regression guard: the fast path's body is byte-identical to the pre-RDR-195,
        // unsplit buildJson() output — no accidental extra whitespace, field, or reordering.
        assertThat(requestBodies.get(0)).isEqualTo(e.buildJson(texts));
    }

    // ── Scenario 7: input above MAX_BATCH_TEXTS -> loud refusal, no truncation ──

    @Test
    void aboveMaxBatchTextsRefusesLoudlyWithoutTruncating() {
        List<String> texts = new ArrayList<>(Collections.nCopies(1001, "t"));
        VoyageEmbedder e = embedder("voyage-code-3");

        assertThatThrownBy(() -> e.embed(texts))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("1000");
        assertThat(requestBodies).isEmpty();
    }

    // ── Scenario 8: per-model budget — higher tier issues strictly fewer requests ──

    @Test
    void higherTierModelIssuesStrictlyFewerRequestsThanVoyageCode3() {
        // 4 texts x 400,000 bytes (~100,000 est. tokens each): voyage-code-3 (120K budget)
        // can only fit ONE per request -> 4 requests. voyage-3.5 (320K budget) fits three
        // then one -> 2 requests. Strictly fewer.
        List<String> texts = List.of(
                bigText(400_000, 'a'), bigText(400_000, 'b'),
                bigText(400_000, 'c'), bigText(400_000, 'd'));

        VoyageEmbedder code3 = embedder("voyage-code-3");
        code3.embed(texts);
        int code3Requests = requestBodies.size();

        requestBodies.clear();
        VoyageEmbedder tier35 = embedder("voyage-3.5");
        tier35.embed(texts);
        int tier35Requests = requestBodies.size();

        assertThat(code3Requests).isGreaterThan(1);
        assertThat(tier35Requests).isLessThan(code3Requests);
    }

    // ── Scenario 9: model absent from table (incl. voyage-3) -> tightest ceiling, over-splits ──

    @Test
    void unknownModelFallsBackToTightestCeilingRatherThanFailing() {
        List<String> texts = List.of(
                bigText(400_000, 'a'), bigText(400_000, 'b'),
                bigText(400_000, 'c'), bigText(400_000, 'd'));

        VoyageEmbedder code3 = embedder("voyage-code-3");
        code3.embed(texts);
        int code3Requests = requestBodies.size();

        requestBodies.clear();
        VoyageEmbedder unknown = embedder("voyage-3"); // absent from the published tiers
        List<float[]> result = unknown.embed(texts); // must NOT throw — over-splits, doesn't fail
        int unknownRequests = requestBodies.size();

        assertThat(result).hasSize(4);
        // Falls back to the SAME tightest (120,000) ceiling as voyage-code-3.
        assertThat(unknownRequests).isEqualTo(code3Requests);

        requestBodies.clear();
        VoyageEmbedder tier35 = embedder("voyage-3.5");
        tier35.embed(texts);
        assertThat(unknownRequests).as("conservative fallback over-splits vs a documented higher tier")
                .isGreaterThan(requestBodies.size());
    }

    // ── Scenario 10: exhaustion — cap forced low via the test seam ──────────────

    @Test
    void exhaustedSubRequestBudgetRaisesTypedErrorWithNoFurtherUpstreamCalls() {
        respond(scripted, 400, TOO_MANY_TOKENS_BODY); // the one attempt this budget allows

        VoyageEmbedder e = embedder("voyage-code-3");
        e.setMaxSubRequestsPerBatchForTest(1);

        assertThatThrownBy(() -> e.embed(List.of("first half text", "second half text")))
                .isInstanceOf(VoyageTooManyTokensException.class)
                .hasMessageContainingAll("exhaust", "1");
        // The first attempt (budget-permitted) reached upstream and 400'd; the retry that
        // would have been the second sub-request was refused BEFORE any further call.
        assertThat(requestBodies).hasSize(1);
        assertThat(e.voyageRequestCount()).isEqualTo(1);
    }

    // ── Scenario 11: sub-request budget is PER-PLANNED-BATCH, not shared across the
    //    whole top-level call — a first planned batch consuming its own budget must not
    //    starve a second, independent planned batch (gate remediation, 2026-08-19; T2
    //    substantive-critique-rdr195-phase2-da9c61781-2026-08-19 finding 1) ────────────

    @Test
    void subRequestBudgetIsPerPlannedBatchNotSharedAcrossTheWholeCall() throws Exception {
        // Two texts, each individually WELL under voyage-code-3's 120,000-token budget
        // but together over it, so planBatches produces exactly two planned batches,
        // each of which fits in ONE POST (no halving needed for either). With the cap
        // forced to 1 via the test seam: if the sub-request budget were shared across
        // the whole top-level call (the pre-fix bug), the first planned batch's single
        // successful POST would consume the ONLY permitted attempt, and the second
        // planned batch would be refused with VoyageTooManyTokensException BEFORE ever
        // reaching upstream -- even though its own request would have succeeded. With
        // the budget correctly scoped per planned batch, both batches get their own
        // attempt=1<=cap(1) and both succeed.
        String t0 = bigText(400_000, 'm'); // ~100,000 est. tokens
        String t1 = bigText(400_000, 'n'); // ~100,000 est. tokens -- together ~200,000 > 120,000
        List<String> texts = List.of(t0, t1);

        VoyageEmbedder e = embedder("voyage-code-3");
        e.setMaxSubRequestsPerBatchForTest(1);

        List<float[]> result = e.embed(texts); // must NOT throw

        assertThat(result).hasSize(2);
        assertThat(result.get(0)).isEqualTo(vectorFor(t0));
        assertThat(result.get(1)).isEqualTo(vectorFor(t1));
        // Two planned batches, each its own single successful POST -- the non-vacuity
        // assertion that BOTH planned batches actually reached upstream despite the
        // cap being exhausted (at value 1) by the FIRST one, if the budget were shared.
        assertThat(requestBodies).as("both planned batches must reach upstream independently").hasSize(2);
        assertThat(e.voyageRequestCount()).isEqualTo(2);
    }

    // ── Scenario 12: per-sub-request instrumentation fires on EVERY POST, incl. the
    //    fast (unsplit) path (gate remediation, 2026-08-19; substantive-critique finding
    //    3: the pre-remediation event was guarded so the dominant unsplit case emitted NO
    //    log line at any level, and never paired usage.total_tokens with request bytes) ──

    @Test
    void perSubRequestEventFiresOnEveryPostIncludingTheFastPath() throws Exception {
        var root = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(
                org.slf4j.Logger.ROOT_LOGGER_NAME);
        var logs = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        logs.start();
        root.addAppender(logs);
        try {
            VoyageEmbedder e = embedder("voyage-code-3");
            // Fast path: one small input, fits in a single planned batch, no halving.
            e.embed(List.of("The quick brown fox jumps over the lazy dog."));

            var messages = logs.list.stream()
                    .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
                    .toList();

            // event=voyage_subbatch_planned must fire even for the single-batch fast path
            // (planned=1) -- previously guarded out entirely for this exact case.
            assertThat(messages)
                    .as("planned-count event must fire on the fast path (planned=1)")
                    .anyMatch(m -> m.startsWith("event=voyage_subbatch_planned")
                            && m.contains("planned=1"));

            // event=voyage_subrequest_sent must fire exactly once, pairing request bytes
            // with the response's actual usage.total_tokens -- the pairing the critic
            // found impossible via logs alone for this exact (dominant) case.
            long sentCount = messages.stream()
                    .filter(m -> m.startsWith("event=voyage_subrequest_sent")).count();
            assertThat(sentCount).as("exactly one real POST on the fast path").isEqualTo(1);
            assertThat(messages)
                    .anyMatch(m -> m.startsWith("event=voyage_subrequest_sent")
                            && m.contains("model=voyage-code-3")
                            && m.contains("texts=1")
                            && m.contains("attempt=1")
                            && m.contains("splitDepth=0")
                            // usageTokens must be a real, non-sentinel value (the fake
                            // server's default handler always sets usage.total_tokens).
                            && !m.contains("usageTokens=-1"));
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }
}
