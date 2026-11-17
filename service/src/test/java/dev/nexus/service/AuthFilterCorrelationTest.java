package dev.nexus.service;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.encoder.PatternLayoutEncoder;
import ch.qos.logback.classic.joran.JoranConfigurator;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.classic.spi.LoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.http.AuthFilter;
import dev.nexus.service.http.RequestContext;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;

import java.io.IOException;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static dev.nexus.service.AuthFilterCorrelationFixture.*;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
/**
 * RDR-167 Step 3 (beads nexus-dngtz.1.5, .1.6, .1.7): the engine MDC bridge and the failure line.
 *
 * <p>{@link AuthFilter} puts {@code correlation_id}, {@code tenant} (once the bearer token
 * resolves) and {@code session} (once it is verified) in the SLF4J MDC, echoes the correlation id
 * on every response (a 401 included), and clears the MDC and the {@code RequestContext} on every
 * path, a throwing handler and a failing logger included; the shipped {@code logback.xml} pattern
 * prints the three fields (sections 1 to 4). Section 5 covers the WARN {@code event=request_failed}
 * line it writes before those clears: the ten-row status matrix, the thrown, unsent and
 * sent-then-thrown handlers, no exception message in the line, and the tenant on early exits.
 *
 * <p>Server shape follows {@link AuthFilterTest}; log capture follows
 * {@code OwnerlessWriteRefusalTest}'s ListAppender, with one change: a logback event reads its MDC
 * lazily, so the appender calls {@code prepareForDeferredProcessing()} on the logging thread
 * before the test thread reads the event.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class AuthFilterCorrelationTest {

    private final AuthFilterCorrelationFixture fx = new AuthFilterCorrelationFixture();

    @BeforeAll
    void startAll() throws Exception {
        fx.start();
    }

    @AfterAll
    void stopAll() {
        fx.close();
    }

    @BeforeEach
    void resetProbe() {
        fx.reset();
    }

    /** Every test ends here: the probe saw every request the client sent, and the MDC was empty. */
    @AfterEach
    void everyRequestWasProbedAndLeftTheMdcEmpty() throws Exception {
        fx.awaitProbe();
    }

    // ── 1. MDC on a line ─────────────────────────────────────────────────────

    @Test
    void anInfoLineFromTheHandlerCarriesCorrelationIdTenantAndSession() throws Exception {
        var capture = new Capture(HANDLER_LOGGER, Level.INFO);
        try (capture) {
            var r = fx.send("/v1/corr?i=1", TOK_A, SESS_A1, "cid-mdc-1");
            assertThat(r.statusCode()).isEqualTo(200);
        }
        assertThat(capture.appender.list).as("non-vacuity: the handler logged one line").hasSize(1);
        Map<String, String> mdc = capture.appender.list.get(0).getMDCPropertyMap();
        assertThat(mdc).containsEntry("correlation_id", "cid-mdc-1")
            .containsEntry("tenant", "tenant-a")
            .containsEntry("session", "session-a1");
        assertNoCredentialInMdc(capture.appender.list);
    }

    @Test
    void aRequestWithoutASessionPutsNoSessionKeyInMdc() throws Exception {
        var capture = new Capture(HANDLER_LOGGER, Level.INFO);
        try (capture) {
            assertThat(fx.send("/v1/corr?i=2", TOK_B, null, "cid-nosess").statusCode()).isEqualTo(200);
        }
        assertThat(capture.appender.list).hasSize(1);
        Map<String, String> mdc = capture.appender.list.get(0).getMDCPropertyMap();
        assertThat(mdc).containsEntry("tenant", "tenant-b").doesNotContainKey("session");
    }

    // ── 2. id handling ───────────────────────────────────────────────────────

    @Test
    void aValidIdIsKeptAndEchoed() throws Exception {
        var capture = new Capture(HANDLER_LOGGER, Level.INFO);
        HttpResponse<String> r;
        try (capture) {
            r = fx.send("/v1/corr?i=3", TOK_A, null, "client.id_7-A");
        }
        assertThat(r.headers().firstValue(HEADER)).contains("client.id_7-A");
        assertThat(capture.appender.list).hasSize(1);
        assertThat(capture.appender.list.get(0).getMDCPropertyMap())
            .containsEntry("correlation_id", "client.id_7-A");
    }

    @Test
    void anOverLongIdIsReplacedByAnEngineId() throws Exception {
        var r = fx.send("/v1/corr?i=4", TOK_A, null, "a".repeat(65));
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.headers().firstValue(HEADER).orElseThrow()).startsWith("nxe-");
    }

    @Test
    void aMalformedIdIsReplacedByAnEngineId() throws Exception {
        var r = fx.send("/v1/corr?i=5", TOK_A, null, "has space/and slash");
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.headers().firstValue(HEADER).orElseThrow()).startsWith("nxe-");
    }

    @Test
    void aRequestWithNoIdGetsAnEngineId_theSameOneThatIsInMdc() throws Exception {
        var capture = new Capture(HANDLER_LOGGER, Level.INFO);
        HttpResponse<String> r;
        try (capture) {
            r = fx.send("/v1/corr?i=6", TOK_A, null, null);
        }
        String echoed = r.headers().firstValue(HEADER).orElseThrow();
        assertThat(echoed).matches("nxe-[0-9a-f]{32}");
        assertThat(capture.appender.list).hasSize(1);
        assertThat(capture.appender.list.get(0).getMDCPropertyMap())
            .containsEntry("correlation_id", echoed);
    }

    @Test
    void a401ForAMissingTokenEchoesTheIdAndLogsItInTheRejectionLine() throws Exception {
        var authLogger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(AuthFilter.class);
        Level before = authLogger.getLevel();
        var capture = new Capture(AuthFilter.class.getName(), Level.DEBUG);
        HttpResponse<String> r;
        HttpResponse<String> r2;
        try (capture) {
            r = fx.send("/v1/corr?i=7", null, null, "cid-401");
            r2 = fx.send("/v1/corr?i=8", null, null, null);
        }
        assertThat(authLogger.getLevel()).as("the capture restored the level").isEqualTo(before);
        assertThat(r.statusCode()).isEqualTo(401);
        assertThat(r.headers().firstValue(HEADER)).contains("cid-401");
        assertThat(r2.statusCode()).isEqualTo(401);
        assertThat(r2.headers().firstValue(HEADER).orElseThrow()).startsWith("nxe-");
        var rejections = capture.appender.list.stream()
            .filter(e -> e.getFormattedMessage().contains("event=auth_rejected")).toList();
        assertThat(rejections).as("non-vacuity: both rejections were logged").hasSize(2);
        assertThat(rejections.get(0).getMDCPropertyMap()).containsEntry("correlation_id", "cid-401");
        assertThat(rejections.get(1).getMDCPropertyMap())
            .containsEntry("correlation_id", r2.headers().firstValue(HEADER).orElseThrow());
    }

    @Test
    void everyEarly401PathLeavesTheMdcEmpty() throws Exception {
        // bad_token (no header), unresolved_token, session_not_minted: three distinct early returns.
        assertThat(fx.send("/v1/corr?i=9", null, null, "cid-e1").statusCode()).isEqualTo(401);
        assertThat(fx.send("/v1/corr?i=10", "no-such-token", null, "cid-e2").statusCode()).isEqualTo(401);
        assertThat(fx.send("/v1/corr?i=11", TOK_A, "not-a-minted-session", "cid-e3").statusCode())
            .isEqualTo(401);
        assertThat(fx.sent.get()).isEqualTo(3);
        fx.awaitProbe();   // exactly 3 probe runs, each with an empty MDC at entry and after
    }

    @Test
    void anEarly403ForAMintScopedTokenEchoesTheIdAndLeavesTheMdcEmpty() throws Exception {
        // A mint-scoped credential on a non-mint path is a 403 from inside the AuthFilter body.
        var capture = new Capture(HANDLER_LOGGER, Level.INFO);
        HttpResponse<String> r;
        try (capture) {
            r = fx.send("/v1/corr?i=12", TOK_MINT, null, "cid-403");
        }
        assertThat(r.statusCode()).isEqualTo(403);
        assertThat(r.headers().firstValue(HEADER)).contains("cid-403");
        assertThat(capture.appender.list).as("the handler never ran").isEmpty();
        assertThat(fx.sent.get()).isEqualTo(1);
        fx.awaitProbe();
    }

    @Test
    void theIdempotencyKeyGuard400EchoesTheIdAndLeavesTheMdcEmpty() throws Exception {
        // The guard follows the AuthFilter on every authenticated context (NexusService.java:760-761);
        // a key on a route that cannot use it is a 400 from the guard, inside the AuthFilter's try.
        var capture = new Capture(HANDLER_LOGGER, Level.INFO);
        HttpResponse<String> r;
        try (capture) {
            r = fx.send(fx.shared.port(), "/v1/corr?i=13", TOK_A, null, "cid-idem", Map.of("Idempotency-Key", "k-1"));
        }
        assertThat(r.statusCode()).isEqualTo(400);
        assertThat(r.body()).contains("idempotency_key_unsupported");
        assertThat(r.headers().firstValue(HEADER)).contains("cid-idem");
        assertThat(capture.appender.list).as("the handler never ran").isEmpty();
        assertThat(fx.sent.get()).isEqualTo(1);
        fx.awaitProbe();
    }

    @Test
    void aHandlerThatThrowsAfterSendingItsStatusStillEchoesTheIdAndTheMdcIsCleared() throws Exception {
        // The handler sends 200 and then throws. A throw before anything is sent gives the client no
        // response, so the echo could not be read; the clear runs through the same finally either way.
        var r = fx.send("/v1/corr?i=14&mode=throw", TOK_A, null, "cid-throw");
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.headers().firstValue(HEADER)).contains("cid-throw");
        assertThat(fx.shared.handler().threw.get()).as("non-vacuity: the handler threw").isEqualTo(1);
        assertThat(fx.sent.get()).isEqualTo(1);
        fx.awaitProbe();   // the probe's after-check runs in a finally, so the throw is observed too
    }

    // ── 3. concurrency ───────────────────────────────────────────────────────

    @Test
    void fourHundredConcurrentRequestsOnAFixedPoolKeepTheirOwnMdc_andThreadsAreReused() throws Exception {
        long elapsedMs = runFourHundred(fx.shared, "fixed-pool-8");
        Set<Long> threads = fx.shared.handler().threadIds;
        System.out.println("AuthFilterCorrelationTest: executor=fixed-pool-8 requests=400 clientConcurrency=32"
            + " elapsed=" + elapsedMs + " ms distinctHandlerThreads=" + threads.size()
            + " names=" + new java.util.TreeSet<>(fx.shared.handler().threadNames));
        assertThat(threads).as("handler threads were reused, so a missed clear would leak").hasSizeLessThan(400);
        assertThat(fx.shared.handler().virtualCalls.get()).isZero();
    }

    @Test
    void fourHundredConcurrentRequestsOnVirtualThreadsKeepTheirOwnMdc() throws Exception {
        // The production shape: NexusService sets newVirtualThreadPerTaskExecutor().
        var virtual = fx.startServer(Executors.newVirtualThreadPerTaskExecutor());
        try {
            long elapsedMs = runFourHundred(virtual, "virtual-per-task");
            System.out.println("AuthFilterCorrelationTest: executor=virtual-per-task requests=400"
                + " clientConcurrency=32 elapsed=" + elapsedMs + " ms distinctHandlerThreads="
                + virtual.handler().threadIds.size());
            assertThat(virtual.handler().virtualCalls.get()).as("every handler ran on a virtual thread")
                .isEqualTo(400);
        } finally {
            virtual.close();
        }
    }

    /** 400 requests across two tenants, client concurrency 32; returns the elapsed milliseconds. */
    private long runFourHundred(Srv srv, String label) throws Exception {
        final int total = 400;
        final int clientConcurrency = 32;
        srv.handler().reset();
        var capture = new Capture(HANDLER_LOGGER, Level.INFO);
        var responseIds = new String[total];
        long startNs = System.nanoTime();
        var clients = Executors.newFixedThreadPool(clientConcurrency);
        try (capture) {
            List<Future<?>> futures = new ArrayList<>();
            for (int i = 0; i < total; i++) {
                final int n = i;
                futures.add(clients.submit(() -> {
                    String token = n % 2 == 0 ? TOK_A : TOK_B;
                    var r = fx.send(srv.port(), "/v1/corr?i=" + n, token, null, "cid-conc-" + n, Map.of());
                    assertThat(r.statusCode()).isEqualTo(200);
                    responseIds[n] = r.headers().firstValue(HEADER).orElse(null);
                    return null;
                }));
            }
            for (Future<?> f : futures) f.get();
        } finally {
            clients.shutdownNow();
        }
        long elapsedMs = (System.nanoTime() - startNs) / 1_000_000L;

        List<ILoggingEvent> events = capture.appender.list;
        assertThat(events).as("%s: one handler line per request", label).hasSize(total);
        Pattern idx = Pattern.compile("i=(\\d+)");
        boolean[] seen = new boolean[total];
        for (ILoggingEvent e : events) {
            var m = idx.matcher(e.getFormattedMessage());
            assertThat(m.find()).as(e.getFormattedMessage()).isTrue();
            int n = Integer.parseInt(m.group(1));
            assertThat(seen[n]).as("request %d logged once", n).isFalse();
            seen[n] = true;
            assertThat(e.getMDCPropertyMap())
                .as("%s request %d", label, n)
                .containsEntry("correlation_id", "cid-conc-" + n)
                .containsEntry("tenant", n % 2 == 0 ? "tenant-a" : "tenant-b");
            assertThat(responseIds[n]).isEqualTo("cid-conc-" + n);
        }
        assertNoCredentialInMdc(events);
        assertThat(fx.sent.get()).isEqualTo(total);
        fx.awaitProbe();   // exactly 400 probe runs, the last one included, each with an empty MDC
        return elapsedMs;
    }

    // ── 5. the request_failed line (nexus-dngtz.1.6, nexus-dngtz.1.7) ───────────

    /** One row of the status matrix: a handler shape or trigger and what the line must say. */
    record Row(String id, String shape, String token, Map<String, String> headers, int status, boolean line,
               Level level) {
        @Override
        public String toString() {
            return id;
        }
    }

    static Stream<Row> failureRows() {
        Map<String, String> none = Map.of();
        return Stream.of(
            new Row("send400", "send400", TOK_A, none, 400, true, Level.WARN),
            new Row("send404", "send404", TOK_A, none, 404, true, Level.INFO),
            new Row("send503Fixed", "send503", TOK_A, none, 503, true, Level.WARN),
            new Row("send503Chunked", "chunked503", TOK_A, none, 503, true, Level.WARN),
            new Row("guard400", "send200", TOK_A, Map.of("Idempotency-Key", "k-1"), 400, true, Level.WARN),
            new Row("badToken401", "send200", "no-such-token", none, 401, true, Level.WARN),
            new Row("direct409", "direct409", TOK_A, none, 409, true, Level.WARN),
            new Row("direct204", "direct204", TOK_A, none, 204, false, null),
            new Row("direct302", "direct302", TOK_A, none, 302, false, null),
            new Row("send200", "send200", TOK_A, none, 200, false, null));
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("failureRows")
    void theRequestFailedLineAcrossResponseStatuses(Row row) throws Exception {
        String cid = "cid-" + row.id();
        var capture = new Capture(AuthFilter.class.getName(), Level.INFO);   // INFO so a 404 line is seen
        HttpResponse<String> r;
        try (capture) {
            r = fx.send(fx.shared.port(), FAIL_PATH + "?shape=" + row.shape(), row.token(), null, cid, row.headers());
            fx.awaitProbe();   // the line is written before the probe's finally runs: bounded poll, 10 s
        }
        assertThat(r.statusCode()).as("the status the client sees (the client does not follow redirects)")
            .isEqualTo(row.status());
        assertThat(r.headers().firstValue(HEADER)).contains(cid);
        var lines = failureLines(capture);
        if (!row.line()) {
            assertThat(lines).as("no line for a status below 400 that was sent (204 and 302)").isEmpty();
            return;
        }
        assertThat(lines).as("exactly one request_failed line").hasSize(1);
        var e = lines.get(0);
        assertThat(e.getLevel()).as("Sam 2026-11-17: a sent 404 logs at INFO, every other failure at WARN")
            .isEqualTo(row.level());
        assertThat(e.getFormattedMessage()).matches("event=request_failed method=GET path=" + FAIL_PATH
            + " status=" + row.status() + " exception=none duration_ms=\\d+");
        assertThat(e.getMDCPropertyMap()).containsEntry("correlation_id", cid).doesNotContainKey("session");
        if (row.id().equals("badToken401")) {
            assertThat(e.getMDCPropertyMap()).as("the principal never resolved").doesNotContainKey("tenant");
        } else {
            assertThat(e.getMDCPropertyMap()).containsEntry("tenant", "tenant-a");
        }
    }

    @Test
    void aHandlerThatThrowsBeforeSendingLogsStatusNoneAndTheExceptionClass_andTheThrowPropagates() throws Exception {
        var capture = new Capture(AuthFilter.class.getName(), Level.WARN);
        try (capture) {
            // A POST: the JDK client retries an idempotent request whose connection closes with no
            // response bytes, which would run the handler (and write the line) twice.
            var get = fx.request(fx.shared.port(), FAIL_PATH + "?shape=throwBeforeSend", TOK_A, SESS_A1,
                "cid-throw-1", Map.of());
            var post = java.net.http.HttpRequest.newBuilder(get, (k, v) -> true)
                .timeout(TestHttp.REQUEST_TIMEOUT)
                .POST(java.net.http.HttpRequest.BodyPublishers.noBody()).build();
            assertThatThrownBy(() -> fx.http.send(post, HttpResponse.BodyHandlers.ofString()))
                .as("the client sees the connection close").isInstanceOf(IOException.class);
            fx.awaitProbe();   // the probe's after-check also proves the MDC is empty after the throw
        }
        var lines = failureLines(capture);
        assertThat(lines).hasSize(1);
        assertThat(lines.get(0).getFormattedMessage()).matches("event=request_failed method=POST path="
            + FAIL_PATH + " status=none exception=java\\.lang\\.IllegalStateException duration_ms=\\d+");
        assertThat(lines.get(0).getMDCPropertyMap())
            .containsEntry("correlation_id", "cid-throw-1")
            .containsEntry("tenant", "tenant-a")
            .containsEntry("session", "session-a1");
        assertThat(fx.probeThrown).as("the exception left the AuthFilter unchanged")
            .singleElement().isInstanceOf(IllegalStateException.class)
            .extracting(Throwable::getMessage).isEqualTo(THROWN_MESSAGE);
        assertNoExceptionTextInLine(lines.get(0));
    }

    @Test
    void aHandlerThatReturnsWithoutSendingLogsStatusNoneAndNoException() throws Exception {
        var capture = new Capture(AuthFilter.class.getName(), Level.WARN);
        var pending = fx.http.sendAsync(
            fx.request(fx.shared.port(), FAIL_PATH + "?shape=noSend", TOK_A, SESS_A1, "cid-nosend", Map.of()),
            HttpResponse.BodyHandlers.ofString());
        try (capture) {
            fx.awaitProbe();   // the handler returned, the exchange is still open, the client still waits
        } finally {
            pending.cancel(true);
        }
        var lines = failureLines(capture);
        assertThat(lines).hasSize(1);
        assertThat(lines.get(0).getFormattedMessage()).matches("event=request_failed method=GET path="
            + FAIL_PATH + " status=none exception=none duration_ms=\\d+");
        assertThat(lines.get(0).getMDCPropertyMap()).containsEntry("correlation_id", "cid-nosend");
        assertThat(fx.probeThrown).isEmpty();
    }

    @Test
    void aHandlerThatSends200WithABodyAndThenThrowsLogsStatus200AndTheExceptionClass() throws Exception {
        var capture = new Capture(AuthFilter.class.getName(), Level.WARN);
        HttpResponse<String> r;
        try (capture) {
            r = fx.send(fx.shared.port(), FAIL_PATH + "?shape=throwAfter200Body", TOK_A, null, "cid-throw-2", Map.of());
            fx.awaitProbe();
        }
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.body()).isEqualTo("body-sent-before-the-throw");
        var lines = failureLines(capture);
        assertThat(lines).hasSize(1);
        assertThat(lines.get(0).getFormattedMessage()).matches("event=request_failed method=GET path="
            + FAIL_PATH + " status=200 exception=java\\.lang\\.IllegalStateException duration_ms=\\d+");
        assertNoExceptionTextInLine(lines.get(0));
    }

    @Test
    void theLineCarriesTheTenantOfAnEarlyExitThatFollowsTokenResolution() throws Exception {
        // The tenant is put in MDC when the token resolves, so exits between resolution and the
        // principal (a 403 scope refusal, a cross-tenant session, a session that was never minted)
        // still print it. The session is never put on these exits.
        var capture = new Capture(AuthFilter.class.getName(), Level.WARN);
        try (capture) {
            assertThat(fx.send(fx.shared.port(), FAIL_PATH + "?shape=send200", TOK_MINT, null, "cid-t-403", Map.of())
                .statusCode()).isEqualTo(403);
            assertThat(fx.send(fx.shared.port(), FAIL_PATH + "?shape=send200", TOK_B, SESS_A1, "cid-t-xtenant", Map.of())
                .statusCode()).isEqualTo(401);
            assertThat(fx.send(fx.shared.port(), FAIL_PATH + "?shape=send200", TOK_A, "not-a-minted-session",
                "cid-t-nomint", Map.of()).statusCode()).isEqualTo(401);
            fx.awaitProbe();
        }
        var byId = new java.util.HashMap<String, ILoggingEvent>();
        for (var e : failureLines(capture)) byId.put(e.getMDCPropertyMap().get("correlation_id"), e);
        assertThat(byId).containsOnlyKeys("cid-t-403", "cid-t-xtenant", "cid-t-nomint");
        assertThat(byId.get("cid-t-403").getMDCPropertyMap()).containsEntry("tenant", "edge-tenant");
        assertThat(byId.get("cid-t-xtenant").getMDCPropertyMap()).containsEntry("tenant", "tenant-b");
        assertThat(byId.get("cid-t-nomint").getMDCPropertyMap()).containsEntry("tenant", "tenant-a");
        for (var e : byId.values()) {
            assertThat(e.getMDCPropertyMap()).doesNotContainKey("session");
        }
    }

    @Test
    void aLoggingFailureStillClearsTheMdcAndTheRequestContext() throws Exception {
        // A logger call that throws must not skip the clears behind it. This appender throws out of
        // Logger.callAppenders; the probe then proves the MDC was empty after the AuthFilter returned.
        var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(AuthFilter.class);
        var previous = logger.getLevel();
        var thrower = new ThrowingAppender();
        thrower.setContext(logger.getLoggerContext());
        thrower.start();
        logger.addAppender(thrower);
        logger.setLevel(Level.WARN);
        try {
            var r = fx.send(fx.shared.port(), FAIL_PATH + "?shape=send400", TOK_A, null, "cid-logfail", Map.of());
            assertThat(r.statusCode()).isEqualTo(400);
            fx.awaitProbe();
        } finally {
            logger.detachAppender(thrower);
            logger.setLevel(previous);
            thrower.stop();
        }
        assertThat(thrower.calls.get()).as("non-vacuity: the failure line reached the appender").isEqualTo(1);
        assertThat(fx.probeThrown).as("a logging failure does not fail the request").isEmpty();
    }

    private static void assertNoExceptionTextInLine(ILoggingEvent e) {
        assertThat(e.getFormattedMessage()).doesNotContain(THROWN_MESSAGE).doesNotContain("at dev.nexus");
        assertThat(e.getThrowableProxy()).as("no exception object, hence no message or stack trace").isNull();
        assertThat(e.getArgumentArray() == null ? "" : java.util.Arrays.toString(e.getArgumentArray()))
            .doesNotContain(THROWN_MESSAGE);
    }

    // ── 4. the pattern ───────────────────────────────────────────────────────

    @Test
    void theShippedLogbackPatternPrintsTheThreeFieldsAndKeepsTheReaderRegexes() throws Exception {
        // The shipped file, off the classpath. Test resources hold no logback*.xml, so nothing shadows it.
        java.net.URL shipped = AuthFilterCorrelationTest.class.getResource("/logback.xml");
        assertThat(shipped).as("logback.xml on the classpath").isNotNull();
        assertThat(shipped.getPath()).as("the shipped copy, not a test resource").doesNotContain("test-classes");
        var lc = new LoggerContext();
        // A bare LoggerContext has no MDC adapter until the service provider installs one.
        lc.setMDCAdapter(new ch.qos.logback.classic.util.LogbackMDCAdapter());
        var configurator = new JoranConfigurator();
        configurator.setContext(lc);
        configurator.doConfigure(shipped);
        var appender = lc.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME).getAppender("STDOUT");
        assertThat(appender).isNotNull();
        var encoder = (PatternLayoutEncoder) ((ch.qos.logback.core.OutputStreamAppender<ILoggingEvent>) appender)
            .getEncoder();
        assertThat(encoder.getPattern())
            .contains("%logger{36} [cid=%X{correlation_id:--} tenant=%X{tenant:--} session=%X{session:--}] - %msg%n");

        // Engine reader of record: src/nexus/daemon/storage_service_daemon.py:986,
        // _ENGINE_LOGBACK_LINE = r"^\d{4}-\d\d-\d\d[ T]\S+\s+[A-Z]+\s". Re-confirm there if it moves.
        Pattern engineLine = Pattern.compile("^\\d{4}-\\d\\d-\\d\\d[ T]\\S+\\s+[A-Z]+\\s");

        // With MDC.
        lc.getMDCAdapter().put("correlation_id", "cid-pat");
        lc.getMDCAdapter().put("tenant", "tenant-a");
        lc.getMDCAdapter().put("session", "sess-1");
        String withMdc = layout(lc, encoder, "event=schema_migration_start");
        lc.getMDCAdapter().clear();
        assertThat(withMdc).matches(m -> engineLine.matcher(m).find());
        assertThat(withMdc).contains(" [cid=cid-pat tenant=tenant-a session=sess-1] - event=schema_migration_start");
        assertThat(withMdc.substring(withMdc.indexOf(" - ") + 3)).startsWith("event=schema_migration_start");

        // Without MDC: the default is "-".
        String bare = layout(lc, encoder, "event=schema_migration_start");
        assertThat(bare).matches(m -> engineLine.matcher(m).find());
        assertThat(bare).contains(" [cid=- tenant=- session=-] - event=schema_migration_start");
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static String layout(LoggerContext lc, PatternLayoutEncoder encoder, String msg) {
        var event = new LoggingEvent(ch.qos.logback.classic.Logger.class.getName(),
            lc.getLogger("dev.nexus.service.Probe"), Level.INFO, msg, null, null);
        return new String(encoder.encode(event), StandardCharsets.UTF_8);
    }
}
