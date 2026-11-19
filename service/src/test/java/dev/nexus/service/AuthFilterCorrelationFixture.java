package dev.nexus.service;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
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
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import static dev.nexus.service.jooq.nexus.Tables.SESSION_TOKENS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * The shared fixture of {@link AuthFilterCorrelationTest} and {@link AuthFilterLogEscapeTest}
 * (RDR-167 Step 3, beads nexus-dngtz.1.5 to .1.8): a PG container, a token store seeded with the
 * correlation tokens, the shared server (a fixed pool of 8 platform threads, so handler threads are
 * reused) with the MDC probe in front of the production filter order, and the log capture. Each test
 * class owns one instance by composition and runs {@link #reset()} before and {@link #awaitProbe()}
 * after every test, so the invariant (every request probed, MDC empty) holds in both.
 */
final class AuthFilterCorrelationFixture implements AutoCloseable {

    static final Instant T0 = Instant.parse("2026-06-09T00:00:00Z");

    static final String TOK_A = "corr-token-tenant-a";
    static final String TOK_B = "corr-token-tenant-b";
    static final String SESS_A1 = "corr-session-a1";
    static final String TOK_MINT = "corr-token-mint-scope";
    static final String TOK_BOARD_CI = "corr-token-board-ci-scope";

    static final String HEADER = "X-Correlation-ID";
    static final Set<String> ALLOWED_MDC_KEYS = Set.of("correlation_id", "tenant", "session");

    /** The context whose handler answers with the shape named by {@code ?shape=} (section 5). */
    static final String FAIL_PATH = "/v1/fail";
    /** The text the throwing shapes carry; it must never reach a log line. */
    static final String THROWN_MESSAGE = "client-text-in-the-exception-message";

    /** Logger the test handler writes its one INFO line to. */
    static final String HANDLER_LOGGER = "dev.nexus.service.AuthFilterCorrelationTest.handler";

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

    void start() throws Exception {
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
            PgContainerHelper.seedServiceToken(dsl, TOK_BOARD_CI, "tenant-bci", "test-board-ci",
                TokenStore.SCOPE_BOARD_CI, null, null);
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

    Srv startServer(ExecutorService executor) throws IOException {
        var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(executor);
        var handler = new LoggingHandler();
        var ctx = server.createContext("/v1/corr", handler);
        ctx.getFilters().add(new MdcProbeFilter());
        ctx.getFilters().add(new AuthFilter(cache, store));
        ctx.getFilters().add(new IdempotencyKeyGuard());
        // RDR-253 Step 2: the identity routes, for the identity-token filter step (the handler is the same 200 probe).
        var identityCtx = server.createContext("/v1/identity", handler);
        identityCtx.getFilters().add(new MdcProbeFilter());
        identityCtx.getFilters().add(new AuthFilter(cache, store));
        identityCtx.getFilters().add(new IdempotencyKeyGuard());
        var failCtx = server.createContext(FAIL_PATH, new ShapeHandler());
        failCtx.getFilters().add(new MdcProbeFilter());
        failCtx.getFilters().add(new AuthFilter(cache, store));
        failCtx.getFilters().add(new IdempotencyKeyGuard());
        server.start();
        return new Srv(server, executor, server.getAddress().getPort(), handler);
    }

    @Override
    public void close() {
        if (shared != null) shared.close();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    void reset() {
        mdcObservations.clear();
        sent.set(0);
        probeRuns.set(0);
        probeDone.set(0);
        probeThrown.clear();
        shared.handler.reset();
    }

    void awaitProbe() throws Exception {
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

    static List<ILoggingEvent> failureLines(Capture capture) {
        return capture.appender.list.stream()
            .filter(e -> e.getFormattedMessage().contains("event=request_failed")).toList();
    }

    static void assertNoCredentialInMdc(List<ILoggingEvent> events) {
        for (ILoggingEvent e : events) {
            Map<String, String> mdc = e.getMDCPropertyMap();
            assertThat(mdc.keySet()).isSubsetOf(ALLOWED_MDC_KEYS);
            for (String raw : List.of(TOK_A, TOK_B, SESS_A1)) {
                assertThat(mdc.values()).doesNotContain(raw, TokenHashing.sha256Hex(raw));
            }
        }
    }

    HttpResponse<String> send(String path, String token, String session, String cid)
            throws Exception {
        return send(shared.port, path, token, session, cid, Map.of());
    }

    HttpResponse<String> send(int port, String path, String token, String session, String cid,
                                      Map<String, String> extraHeaders) throws Exception {
        return http.send(request(port, path, token, session, cid, extraHeaders),
            HttpResponse.BodyHandlers.ofString());
    }

    /** Builds one request and counts it as sent. */
    java.net.http.HttpRequest request(int port, String path, String token, String session,
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
    record Srv(HttpServer server, ExecutorService executor, int port, LoggingHandler handler)
            implements AutoCloseable {
        @Override
        public void close() {
            server.stop(0);
            executor.shutdownNow();
        }
    }

    /** Appender that reads the MDC on the logging thread, where it is still set. */
    static final class MdcSnapshotAppender extends ListAppender<ILoggingEvent> {
        @Override
        protected void append(ILoggingEvent e) {
            e.prepareForDeferredProcessing();
            super.append(e);
        }
    }

    /** Attaches an MDC-snapshotting appender to one logger and restores the logger on close. */
    static final class Capture implements AutoCloseable {
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
    final class MdcProbeFilter extends Filter {
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
    static final class LoggingHandler implements com.sun.net.httpserver.HttpHandler {
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
                // Counted before the status is sent: the client can read the 200 and assert on the
                // counter before the handler thread runs the next statement.
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
    static final class ThrowingAppender extends UnsynchronizedAppenderBase<ILoggingEvent> {
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
    static final class ShapeHandler implements com.sun.net.httpserver.HttpHandler {
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
