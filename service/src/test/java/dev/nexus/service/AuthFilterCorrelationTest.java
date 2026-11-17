package dev.nexus.service;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.encoder.PatternLayoutEncoder;
import ch.qos.logback.classic.joran.JoranConfigurator;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.classic.spi.LoggingEvent;
import ch.qos.logback.core.UnsynchronizedAppenderBase;
import ch.qos.logback.core.read.ListAppender;
import com.sun.net.httpserver.Filter;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TokenCache;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.http.AuthFilter;
import dev.nexus.service.http.HttpUtil;
import dev.nexus.service.http.IdempotencyKeyGuard;
import dev.nexus.service.http.RequestContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
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
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static dev.nexus.service.jooq.nexus.Tables.SESSION_TOKENS;
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

    private static final Instant T0 = Instant.parse("2026-06-09T00:00:00Z");

    private static final String TOK_A = "corr-token-tenant-a";
    private static final String TOK_B = "corr-token-tenant-b";
    private static final String SESS_A1 = "corr-session-a1";
    private static final String TOK_MINT = "corr-token-mint-scope";

    private static final String HEADER = "X-Correlation-ID";
    private static final Set<String> ALLOWED_MDC_KEYS = Set.of("correlation_id", "tenant", "session");

    /** The context whose handler answers with the shape named by {@code ?shape=} (section 5). */
    private static final String FAIL_PATH = "/v1/fail";
    /** The text the throwing shapes carry; it must never reach a log line. */
    private static final String THROWN_MESSAGE = "client-text-in-the-exception-message";

    /** Logger the test handler writes its one INFO line to. */
    private static final String HANDLER_LOGGER = "dev.nexus.service.AuthFilterCorrelationTest.handler";

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TokenStore store;
    TokenCache cache;
    /** The shared server: a fixed pool of 8 platform threads, so handler threads are reused. */
    Srv shared;
    final HttpClient http = TestHttp.client();

    /** Thread-level MDC observations made by {@link MdcProbeFilter}, which wraps the AuthFilter. */
    final ConcurrentLinkedQueue<String> mdcObservations = new ConcurrentLinkedQueue<>();
    /** Requests the client sent in this test / requests the probe entered / requests it finished. */
    final AtomicInteger sent = new AtomicInteger();
    final AtomicInteger probeRuns = new AtomicInteger();
    final AtomicInteger probeDone = new AtomicInteger();
    /** What the probe saw propagate out of the AuthFilter (the filter must rethrow unchanged). */
    final ConcurrentLinkedQueue<Throwable> probeThrown = new ConcurrentLinkedQueue<>();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);

        var clock = new AuthFilterTest.MutableClock(T0);
        store = new TokenStore(ds, clock);
        cache = new TokenCache(store, clock);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, TOK_A, "tenant-a", "test", null, null, null);
            PgContainerHelper.seedServiceToken(dsl, TOK_B, "tenant-b", "test", null, null, null);
            PgContainerHelper.seedServiceToken(dsl, TOK_MINT, "edge-tenant", "test-scoped", "mint", null, null);
            dsl.insertInto(SESSION_TOKENS)
                .columns(SESSION_TOKENS.SESSION_TOKEN_HASH, SESSION_TOKENS.TENANT_ID,
                    SESSION_TOKENS.SESSION_ID, SESSION_TOKENS.EXPIRES_AT)
                .values(TokenHashing.sha256Hex(SESS_A1), "tenant-a", "session-a1",
                    OffsetDateTime.ofInstant(T0.plusSeconds(3600), ZoneOffset.UTC))
                .onConflictDoNothing()
                .execute();
        }
        shared = startServer(Executors.newFixedThreadPool(8));
    }

    /**
     * One server on the given executor, with the production filter order (NexusService.java:760-761:
     * AuthFilter, then IdempotencyKeyGuard) and the MDC probe in front of both.
     */
    private Srv startServer(ExecutorService executor) throws IOException {
        var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(executor);
        var handler = new LoggingHandler();
        var ctx = server.createContext("/v1/corr", handler);
        ctx.getFilters().add(new MdcProbeFilter());
        ctx.getFilters().add(new AuthFilter(cache, store));
        ctx.getFilters().add(new IdempotencyKeyGuard());
        var failCtx = server.createContext(FAIL_PATH, new ShapeHandler());
        failCtx.getFilters().add(new MdcProbeFilter());
        failCtx.getFilters().add(new AuthFilter(cache, store));
        failCtx.getFilters().add(new IdempotencyKeyGuard());
        server.start();
        return new Srv(server, executor, server.getAddress().getPort(), handler);
    }

    @AfterAll
    void stopAll() {
        if (shared != null) shared.close();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void resetProbe() {
        mdcObservations.clear();
        sent.set(0);
        probeRuns.set(0);
        probeDone.set(0);
        probeThrown.clear();
        shared.handler.reset();
    }

    /**
     * Every test ends here: waits until the probe has finished every request the client sent (the
     * client can return before the server thread has run the probe's last check), then asserts the
     * probe ran once per request and saw an empty MDC at entry and after the AuthFilter returned.
     */
    @AfterEach
    void everyRequestWasProbedAndLeftTheMdcEmpty() throws Exception {
        awaitProbe();
    }

    private void awaitProbe() throws Exception {
        long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(10);
        while (probeDone.get() < sent.get() && System.nanoTime() < deadline) {
            Thread.sleep(5);
        }
        assertThat(probeRuns.get()).as("the probe entered once per request sent").isEqualTo(sent.get());
        assertThat(probeDone.get()).as("the probe finished once per request sent").isEqualTo(sent.get());
        assertThat(mdcObservations)
            .as("MDC and RequestContext empty on the handler thread at entry and after the AuthFilter returned, every request")
            .isEmpty();
    }

    // ── 1. MDC on a line ─────────────────────────────────────────────────────

    @Test
    void anInfoLineFromTheHandlerCarriesCorrelationIdTenantAndSession() throws Exception {
        var capture = new Capture(HANDLER_LOGGER, Level.INFO);
        try (capture) {
            var r = send("/v1/corr?i=1", TOK_A, SESS_A1, "cid-mdc-1");
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
            assertThat(send("/v1/corr?i=2", TOK_B, null, "cid-nosess").statusCode()).isEqualTo(200);
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
            r = send("/v1/corr?i=3", TOK_A, null, "client.id_7-A");
        }
        assertThat(r.headers().firstValue(HEADER)).contains("client.id_7-A");
        assertThat(capture.appender.list).hasSize(1);
        assertThat(capture.appender.list.get(0).getMDCPropertyMap())
            .containsEntry("correlation_id", "client.id_7-A");
    }

    @Test
    void anOverLongIdIsReplacedByAnEngineId() throws Exception {
        var r = send("/v1/corr?i=4", TOK_A, null, "a".repeat(65));
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.headers().firstValue(HEADER).orElseThrow()).startsWith("nxe-");
    }

    @Test
    void aMalformedIdIsReplacedByAnEngineId() throws Exception {
        var r = send("/v1/corr?i=5", TOK_A, null, "has space/and slash");
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.headers().firstValue(HEADER).orElseThrow()).startsWith("nxe-");
    }

    @Test
    void aRequestWithNoIdGetsAnEngineId_theSameOneThatIsInMdc() throws Exception {
        var capture = new Capture(HANDLER_LOGGER, Level.INFO);
        HttpResponse<String> r;
        try (capture) {
            r = send("/v1/corr?i=6", TOK_A, null, null);
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
            r = send("/v1/corr?i=7", null, null, "cid-401");
            r2 = send("/v1/corr?i=8", null, null, null);
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
        assertThat(send("/v1/corr?i=9", null, null, "cid-e1").statusCode()).isEqualTo(401);
        assertThat(send("/v1/corr?i=10", "no-such-token", null, "cid-e2").statusCode()).isEqualTo(401);
        assertThat(send("/v1/corr?i=11", TOK_A, "not-a-minted-session", "cid-e3").statusCode())
            .isEqualTo(401);
        assertThat(sent.get()).isEqualTo(3);
        awaitProbe();   // exactly 3 probe runs, each with an empty MDC at entry and after
    }

    @Test
    void anEarly403ForAMintScopedTokenEchoesTheIdAndLeavesTheMdcEmpty() throws Exception {
        // A mint-scoped credential on a non-mint path is a 403 from inside the AuthFilter body.
        var capture = new Capture(HANDLER_LOGGER, Level.INFO);
        HttpResponse<String> r;
        try (capture) {
            r = send("/v1/corr?i=12", TOK_MINT, null, "cid-403");
        }
        assertThat(r.statusCode()).isEqualTo(403);
        assertThat(r.headers().firstValue(HEADER)).contains("cid-403");
        assertThat(capture.appender.list).as("the handler never ran").isEmpty();
        assertThat(sent.get()).isEqualTo(1);
        awaitProbe();
    }

    @Test
    void theIdempotencyKeyGuard400EchoesTheIdAndLeavesTheMdcEmpty() throws Exception {
        // The guard follows the AuthFilter on every authenticated context (NexusService.java:760-761);
        // a key on a route that cannot use it is a 400 from the guard, inside the AuthFilter's try.
        var capture = new Capture(HANDLER_LOGGER, Level.INFO);
        HttpResponse<String> r;
        try (capture) {
            r = send(shared.port, "/v1/corr?i=13", TOK_A, null, "cid-idem", Map.of("Idempotency-Key", "k-1"));
        }
        assertThat(r.statusCode()).isEqualTo(400);
        assertThat(r.body()).contains("idempotency_key_unsupported");
        assertThat(r.headers().firstValue(HEADER)).contains("cid-idem");
        assertThat(capture.appender.list).as("the handler never ran").isEmpty();
        assertThat(sent.get()).isEqualTo(1);
        awaitProbe();
    }

    @Test
    void aHandlerThatThrowsAfterSendingItsStatusStillEchoesTheIdAndTheMdcIsCleared() throws Exception {
        // The handler sends 200 and then throws. A throw before anything is sent gives the client no
        // response, so the echo could not be read; the clear runs through the same finally either way.
        var r = send("/v1/corr?i=14&mode=throw", TOK_A, null, "cid-throw");
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.headers().firstValue(HEADER)).contains("cid-throw");
        assertThat(shared.handler.threw.get()).as("non-vacuity: the handler threw").isEqualTo(1);
        assertThat(sent.get()).isEqualTo(1);
        awaitProbe();   // the probe's after-check runs in a finally, so the throw is observed too
    }

    // ── 3. concurrency ───────────────────────────────────────────────────────

    @Test
    void fourHundredConcurrentRequestsOnAFixedPoolKeepTheirOwnMdc_andThreadsAreReused() throws Exception {
        long elapsedMs = runFourHundred(shared, "fixed-pool-8");
        Set<Long> threads = shared.handler.threadIds;
        System.out.println("AuthFilterCorrelationTest: executor=fixed-pool-8 requests=400 clientConcurrency=32"
            + " elapsed=" + elapsedMs + " ms distinctHandlerThreads=" + threads.size()
            + " names=" + new java.util.TreeSet<>(shared.handler.threadNames));
        assertThat(threads).as("handler threads were reused, so a missed clear would leak").hasSizeLessThan(400);
        assertThat(shared.handler.virtualCalls.get()).isZero();
    }

    @Test
    void fourHundredConcurrentRequestsOnVirtualThreadsKeepTheirOwnMdc() throws Exception {
        // The production shape: NexusService sets newVirtualThreadPerTaskExecutor().
        var virtual = startServer(Executors.newVirtualThreadPerTaskExecutor());
        try {
            long elapsedMs = runFourHundred(virtual, "virtual-per-task");
            System.out.println("AuthFilterCorrelationTest: executor=virtual-per-task requests=400"
                + " clientConcurrency=32 elapsed=" + elapsedMs + " ms distinctHandlerThreads="
                + virtual.handler.threadIds.size());
            assertThat(virtual.handler.virtualCalls.get()).as("every handler ran on a virtual thread")
                .isEqualTo(400);
        } finally {
            virtual.close();
        }
    }

    /** 400 requests across two tenants, client concurrency 32; returns the elapsed milliseconds. */
    private long runFourHundred(Srv srv, String label) throws Exception {
        final int total = 400;
        final int clientConcurrency = 32;
        srv.handler.reset();
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
                    var r = send(srv.port, "/v1/corr?i=" + n, token, null, "cid-conc-" + n, Map.of());
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
        assertThat(sent.get()).isEqualTo(total);
        awaitProbe();   // exactly 400 probe runs, the last one included, each with an empty MDC
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
            r = send(shared.port, FAIL_PATH + "?shape=" + row.shape(), row.token(), null, cid, row.headers());
            awaitProbe();   // the line is written before the probe's finally runs: bounded poll, 10 s
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
            var get = request(shared.port, FAIL_PATH + "?shape=throwBeforeSend", TOK_A, SESS_A1,
                "cid-throw-1", Map.of());
            var post = java.net.http.HttpRequest.newBuilder(get, (k, v) -> true)
                .timeout(TestHttp.REQUEST_TIMEOUT)
                .POST(java.net.http.HttpRequest.BodyPublishers.noBody()).build();
            assertThatThrownBy(() -> http.send(post, HttpResponse.BodyHandlers.ofString()))
                .as("the client sees the connection close").isInstanceOf(IOException.class);
            awaitProbe();   // the probe's after-check also proves the MDC is empty after the throw
        }
        var lines = failureLines(capture);
        assertThat(lines).hasSize(1);
        assertThat(lines.get(0).getFormattedMessage()).matches("event=request_failed method=POST path="
            + FAIL_PATH + " status=none exception=java\\.lang\\.IllegalStateException duration_ms=\\d+");
        assertThat(lines.get(0).getMDCPropertyMap())
            .containsEntry("correlation_id", "cid-throw-1")
            .containsEntry("tenant", "tenant-a")
            .containsEntry("session", "session-a1");
        assertThat(probeThrown).as("the exception left the AuthFilter unchanged")
            .singleElement().isInstanceOf(IllegalStateException.class)
            .extracting(Throwable::getMessage).isEqualTo(THROWN_MESSAGE);
        assertNoExceptionTextInLine(lines.get(0));
    }

    @Test
    void aHandlerThatReturnsWithoutSendingLogsStatusNoneAndNoException() throws Exception {
        var capture = new Capture(AuthFilter.class.getName(), Level.WARN);
        var pending = http.sendAsync(
            request(shared.port, FAIL_PATH + "?shape=noSend", TOK_A, SESS_A1, "cid-nosend", Map.of()),
            HttpResponse.BodyHandlers.ofString());
        try (capture) {
            awaitProbe();   // the handler returned, the exchange is still open, the client still waits
        } finally {
            pending.cancel(true);
        }
        var lines = failureLines(capture);
        assertThat(lines).hasSize(1);
        assertThat(lines.get(0).getFormattedMessage()).matches("event=request_failed method=GET path="
            + FAIL_PATH + " status=none exception=none duration_ms=\\d+");
        assertThat(lines.get(0).getMDCPropertyMap()).containsEntry("correlation_id", "cid-nosend");
        assertThat(probeThrown).isEmpty();
    }

    @Test
    void aHandlerThatSends200WithABodyAndThenThrowsLogsStatus200AndTheExceptionClass() throws Exception {
        var capture = new Capture(AuthFilter.class.getName(), Level.WARN);
        HttpResponse<String> r;
        try (capture) {
            r = send(shared.port, FAIL_PATH + "?shape=throwAfter200Body", TOK_A, null, "cid-throw-2", Map.of());
            awaitProbe();
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
            assertThat(send(shared.port, FAIL_PATH + "?shape=send200", TOK_MINT, null, "cid-t-403", Map.of())
                .statusCode()).isEqualTo(403);
            assertThat(send(shared.port, FAIL_PATH + "?shape=send200", TOK_B, SESS_A1, "cid-t-xtenant", Map.of())
                .statusCode()).isEqualTo(401);
            assertThat(send(shared.port, FAIL_PATH + "?shape=send200", TOK_A, "not-a-minted-session",
                "cid-t-nomint", Map.of()).statusCode()).isEqualTo(401);
            awaitProbe();
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
            var r = send(shared.port, FAIL_PATH + "?shape=send400", TOK_A, null, "cid-logfail", Map.of());
            assertThat(r.statusCode()).isEqualTo(400);
            awaitProbe();
        } finally {
            logger.detachAppender(thrower);
            logger.setLevel(previous);
            thrower.stop();
        }
        assertThat(thrower.calls.get()).as("non-vacuity: the failure line reached the appender").isEqualTo(1);
        assertThat(probeThrown).as("a logging failure does not fail the request").isEmpty();
    }

    private static List<ILoggingEvent> failureLines(Capture capture) {
        return capture.appender.list.stream()
            .filter(e -> e.getFormattedMessage().contains("event=request_failed")).toList();
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

    private static void assertNoCredentialInMdc(List<ILoggingEvent> events) {
        for (ILoggingEvent e : events) {
            Map<String, String> mdc = e.getMDCPropertyMap();
            assertThat(mdc.keySet()).isSubsetOf(ALLOWED_MDC_KEYS);
            for (String raw : List.of(TOK_A, TOK_B, SESS_A1)) {
                assertThat(mdc.values()).doesNotContain(raw, TokenHashing.sha256Hex(raw));
            }
        }
    }

    private HttpResponse<String> send(String path, String token, String session, String cid)
            throws Exception {
        return send(shared.port, path, token, session, cid, Map.of());
    }

    private HttpResponse<String> send(int port, String path, String token, String session, String cid,
                                      Map<String, String> extraHeaders) throws Exception {
        return http.send(request(port, path, token, session, cid, extraHeaders),
            HttpResponse.BodyHandlers.ofString());
    }

    /** Builds one request and counts it as sent. */
    private java.net.http.HttpRequest request(int port, String path, String token, String session,
                                              String cid, Map<String, String> extraHeaders) {
        sent.incrementAndGet();
        var b = TestHttp.request("http://127.0.0.1:" + port + path).GET();
        if (token != null) b.header("Authorization", "Bearer " + token);
        if (session != null) b.header("X-Nexus-T1-Session", session);
        if (cid != null) b.header(HEADER, cid);
        extraHeaders.forEach(b::header);
        return b.build();
    }

    /** A started server, its executor and its handler. */
    private record Srv(HttpServer server, ExecutorService executor, int port, LoggingHandler handler)
            implements AutoCloseable {
        @Override
        public void close() {
            server.stop(0);
            executor.shutdownNow();
        }
    }

    /** Appender that reads the MDC on the logging thread, where it is still set. */
    private static final class MdcSnapshotAppender extends ListAppender<ILoggingEvent> {
        @Override
        protected void append(ILoggingEvent e) {
            e.prepareForDeferredProcessing();
            super.append(e);
        }
    }

    /** Attaches an MDC-snapshotting appender to one logger and restores the logger on close. */
    private static final class Capture implements AutoCloseable {
        final ch.qos.logback.classic.Logger logger;
        final MdcSnapshotAppender appender = new MdcSnapshotAppender();
        final Level previousLevel;

        Capture(String loggerName, Level level) {
            logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(loggerName);
            previousLevel = logger.getLevel();
            appender.start();
            logger.addAppender(appender);
            logger.setLevel(level);
        }

        @Override
        public void close() {
            logger.detachAppender(appender);
            logger.setLevel(previousLevel);
            appender.stop();
        }
    }

    /**
     * Sits before the AuthFilter on the same context. It sees the handler thread's MDC and
     * RequestContext (principal and deadline) on entry (a leak from the previous request on this
     * thread) and, in a finally so a throw is covered, after the AuthFilter has returned (a missed
     * clear). {@code probeDone} counts finished checks so
     * the test can wait for the last request's check, which can run after the client has its response.
     */
    private final class MdcProbeFilter extends Filter {
        @Override
        public void doFilter(HttpExchange exchange, Chain chain) throws IOException {
            probeRuns.incrementAndGet();
            String q = exchange.getRequestURI().getQuery();
            try {
                var entry = MDC.getCopyOfContextMap();
                if (entry != null && !entry.isEmpty()) {
                    mdcObservations.add("entry " + q + " " + entry);
                }
                if (RequestContext.current() != null || RequestContext.deadlineNanos() != null) {
                    mdcObservations.add("entry " + q + " RequestContext left over");
                }
                chain.doFilter(exchange);
            } catch (Throwable t) {
                probeThrown.add(t);
                throw t;
            } finally {
                var after = MDC.getCopyOfContextMap();
                if (after != null && !after.isEmpty()) {
                    mdcObservations.add("after " + q + " " + after);
                }
                if (RequestContext.current() != null || RequestContext.deadlineNanos() != null) {
                    mdcObservations.add("after " + q + " RequestContext not cleared");
                }
                probeDone.incrementAndGet();
            }
        }

        @Override
        public String description() {
            return "MDC probe";
        }
    }

    /**
     * Logs one INFO line and answers 200. With {@code mode=throw} it sends the 200 and then throws.
     * It records the threads it ran on.
     */
    private static final class LoggingHandler implements com.sun.net.httpserver.HttpHandler {
        private static final org.slf4j.Logger LOG = LoggerFactory.getLogger(HANDLER_LOGGER);

        final Set<Long> threadIds = ConcurrentHashMap.newKeySet();
        final Set<String> threadNames = ConcurrentHashMap.newKeySet();
        final AtomicInteger virtualCalls = new AtomicInteger();
        final AtomicInteger threw = new AtomicInteger();

        void reset() {
            threadIds.clear();
            threadNames.clear();
            virtualCalls.set(0);
            threw.set(0);
        }

        @Override
        public void handle(HttpExchange ex) throws IOException {
            Thread t = Thread.currentThread();
            threadIds.add(t.threadId());
            threadNames.add(t.getName());
            if (t.isVirtual()) virtualCalls.incrementAndGet();
            String q = ex.getRequestURI().getQuery();
            LOG.info("event=probe {}", q);
            if (q != null && q.contains("mode=throw")) {
                // counted before the status goes out: the client reads the 200 and asserts the count at once, so a count taken
                // after sendResponseHeaders raced that read (intermittent red, 1 run in 3 on chas)
                threw.incrementAndGet();
                ex.sendResponseHeaders(200, -1);
                throw new IllegalStateException("handler failed after it sent its status");
            }
            byte[] bytes = ("tenant=" + RequestContext.tenant()).getBytes(StandardCharsets.UTF_8);
            ex.sendResponseHeaders(200, bytes.length);
            try (OutputStream os = ex.getResponseBody()) {
                os.write(bytes);
            }
        }
    }

    /** An appender whose doAppend throws, which Logger.callAppenders does not catch. */
    private static final class ThrowingAppender extends UnsynchronizedAppenderBase<ILoggingEvent> {
        final AtomicInteger calls = new AtomicInteger();

        @Override
        public void doAppend(ILoggingEvent e) {
            calls.incrementAndGet();
            throw new IllegalStateException("the appender failed");
        }

        @Override
        protected void append(ILoggingEvent e) {
        }
    }

    /** Answers by the {@code shape} query parameter; the shapes are the rows of section 5. */
    private static final class ShapeHandler implements com.sun.net.httpserver.HttpHandler {
        @Override
        public void handle(HttpExchange ex) throws IOException {
            String q = ex.getRequestURI().getQuery();
            String shape = q == null ? "" : q.substring(q.indexOf("shape=") + "shape=".length());
            switch (shape) {
                case "send400" -> HttpUtil.send(ex, 400, "{\"error\":\"bad_request\"}");
                case "send404" -> HttpUtil.send(ex, 404, "{\"error\":\"not_found\"}");
                case "send503" -> HttpUtil.send(ex, 503, "{\"error\":\"unavailable\"}");
                case "chunked503" -> {
                    ex.sendResponseHeaders(503, 0);
                    try (OutputStream os = ex.getResponseBody()) {
                        os.write("{\"error\":\"unavailable\"}".getBytes(StandardCharsets.UTF_8));
                    }
                }
                case "send200" -> HttpUtil.send(ex, 200, "{}");
                // The two synthetic direct senders: no HttpUtil.send, no body (-1).
                case "direct409" -> ex.sendResponseHeaders(409, -1);
                case "direct204" -> ex.sendResponseHeaders(204, -1);
                case "direct302" -> {
                    ex.getResponseHeaders().set("Location", "/v1/elsewhere");
                    ex.sendResponseHeaders(302, -1);
                }
                case "noSend" -> { }
                case "throwBeforeSend" -> throw new IllegalStateException(THROWN_MESSAGE);
                case "throwAfter200Body" -> {
                    byte[] bytes = "body-sent-before-the-throw".getBytes(StandardCharsets.UTF_8);
                    ex.sendResponseHeaders(200, bytes.length);
                    try (OutputStream os = ex.getResponseBody()) {
                        os.write(bytes);
                    }
                    throw new IllegalStateException(THROWN_MESSAGE);
                }
                default -> throw new IllegalArgumentException("unknown shape " + shape);
            }
        }
    }
}
