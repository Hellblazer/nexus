package dev.nexus.service;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.encoder.PatternLayoutEncoder;
import ch.qos.logback.classic.joran.JoranConfigurator;
import ch.qos.logback.classic.spi.ILoggingEvent;
import dev.nexus.service.http.AuthFilter;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.stream.Stream;

import static dev.nexus.service.AuthFilterCorrelationFixture.*;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-dngtz.1.8: client text in an engine log line is escaped. The path, the method token and the
 * claimed tenant header reach the {@link AuthFilter} lines decoded, so a {@code %0A} or a control
 * character would write a real second line. Split from {@link AuthFilterCorrelationTest} to keep both
 * under the file-size cap; the server, PG and probe come from {@link AuthFilterCorrelationFixture}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class AuthFilterLogEscapeTest {

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

    /**
     * nexus-dngtz.1.8: getRequestURI().getPath() is the decoded path, so {@code %0A} would write a
     * real newline and forge a second log line. A missing token ends in 401, which writes both the
     * DEBUG {@code auth_rejected} line and the WARN {@code request_failed} line. {@code %5C} is a
     * backslash: it must come out doubled, so typed text cannot pass for an escape.
     */
    @Test
    void aNewlineInTheRequestPathStaysOnOneLineInBothTheRequestFailedAndTheAuthRejectedLines() throws Exception {
        var capture = new Capture(AuthFilter.class.getName(), Level.DEBUG);
        HttpResponse<String> r;
        try (capture) {
            r = fx.send("/v1/corr/x%0Aevent=forged%5Cx41", null, null, "cid-escape");
            fx.awaitProbe();
        }
        assertThat(r.statusCode()).isEqualTo(401);
        var failed = failureLines(capture);
        assertThat(failed).as("exactly one request_failed event, not two").hasSize(1);
        var rejected = capture.appender.list.stream()
            .filter(e -> e.getFormattedMessage().contains("event=auth_rejected")).toList();
        assertThat(rejected).as("exactly one auth_rejected event").hasSize(1);
        for (var e : List.of(failed.get(0), rejected.get(0))) {
            String text = e.getFormattedMessage();
            assertThat(text).contains("path=/v1/corr/x\\x0aevent=forged\\\\x41");
            assertThat(text).doesNotContain("\n").doesNotContain("\r");
        }
    }

    /**
     * Writes one raw HTTP/1.1 request to the shared server and returns its status line. HttpClient
     * refuses to send a control character in a method or a header value, so these tests bypass it.
     * The request counts as sent. The server keeps the connection open, so only the status line is
     * read, with a bounded socket timeout.
     */
    private String rawStatusLine(String requestLine, String extraHeaders) throws IOException {
        try (var socket = new java.net.Socket()) {
            socket.connect(new InetSocketAddress("127.0.0.1", fx.shared.port()), 5_000);
            socket.setSoTimeout(10_000);
            fx.sent.incrementAndGet();
            socket.getOutputStream().write((requestLine + "\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
                + extraHeaders + "\r\n").getBytes(StandardCharsets.ISO_8859_1));
            socket.getOutputStream().flush();
            return new java.io.BufferedReader(new java.io.InputStreamReader(
                socket.getInputStream(), StandardCharsets.ISO_8859_1)).readLine();
        }
    }

    /** One raw method: what is sent, what the line must print, and whether the JDK may refuse it first. */
    static Stream<org.junit.jupiter.params.provider.Arguments> rawMethods() {
        return Stream.of(
            // JDK 25.0.3 passes ESC to getRequestMethod(); 25.0.5 answers 400 itself, before any filter.
            org.junit.jupiter.params.provider.Arguments.of("GE\u001bT", "GE\\x1bT", true),
            // Both JDKs pass a tab and a C1 control in the method token.
            org.junit.jupiter.params.provider.Arguments.of("GE\tT", "GE\\x09T", false),
            org.junit.jupiter.params.provider.Arguments.of("GE\u009bT", "GE\\x9bT", false));
    }

    /**
     * nexus-dngtz.1.8 fix round: the method token is client text too. HttpClient refuses to send a control
     * character in a method, so this test writes the request line to a raw socket. A method the JDK
     * refuses with its own 400 never reaches the filters and writes no engine line; every other row must
     * reach them and print the escaped method.
     */
    @ParameterizedTest(name = "{1}")
    @MethodSource("rawMethods")
    void aControlCharacterInTheRequestMethodIsEscapedInTheRequestFailedLine(String method, String printed,
                                                                           boolean jdkMayRefuse) throws Exception {
        var capture = new Capture(AuthFilter.class.getName(), Level.DEBUG);
        String status;
        try (capture) {
            status = rawStatusLine(method + " /v1/corr/x HTTP/1.1", "");
            if (status != null && status.startsWith("HTTP/1.1 400")) {
                fx.sent.decrementAndGet();   // refused by the JDK server: the probe filter never ran
            } else {
                fx.awaitProbe();
            }
        }
        if (jdkMayRefuse && status != null && status.startsWith("HTTP/1.1 400")) {
            assertThat(failureLines(capture)).as("a JDK-refused method writes no request_failed line").isEmpty();
            return;
        }
        assertThat(status).as("the request reached the filters").startsWith("HTTP/1.1 401");
        var failed = failureLines(capture);
        assertThat(failed).as("exactly one request_failed event").hasSize(1);
        String text = failed.get(0).getFormattedMessage();
        assertThat(text).contains("method=" + printed + " path=/v1/corr/x status=401");
        assertThat(text).doesNotContain(method).doesNotContain("\r").doesNotContain("\n");
    }

    /**
     * nexus-dngtz.1.8 fix round: the claimed X-Nexus-Tenant header is client text. A raw socket, because
     * HttpClient refuses a control character in a value. The JDK server turns a tab into a space and (25.0.5)
     * refuses C0 and DEL in a value, so the test sends U+009B, which reaches the filter on both.
     */
    @Test
    void aControlCharacterInTheClaimedTenantHeaderIsEscapedInTheTenantHeaderIgnoredLine() throws Exception {
        var capture = new Capture(AuthFilter.class.getName(), Level.DEBUG);
        String status;
        try (capture) {
            status = rawStatusLine("GET /v1/corr?i=20 HTTP/1.1",
                "Authorization: Bearer " + TOK_A + "\r\nX-Nexus-Tenant: other\u009bx\r\n");
            fx.awaitProbe();
        }
        assertThat(status).startsWith("HTTP/1.1 200");
        var lines = capture.appender.list.stream()
            .filter(e -> e.getFormattedMessage().contains("event=tenant_header_ignored")).toList();
        assertThat(lines).as("exactly one tenant_header_ignored event").hasSize(1);
        assertThat(lines.get(0).getFormattedMessage()).contains("claimed=other\\x9bx resolved=tenant-a")
            .doesNotContain("\u009b");
    }

    /** nexus-dngtz.1.8 fix round: the board-ci surface refusal logs the decoded path. */
    @Test
    void aNewlineInThePathOfABoardCiSurfaceRefusalStaysOnOneLine() throws Exception {
        var capture = new Capture(AuthFilter.class.getName(), Level.DEBUG);
        HttpResponse<String> r;
        try (capture) {
            r = fx.send("/v1/corr/x%0Aevent=forged", TOK_BOARD_CI, null, "cid-escape-bci");
            fx.awaitProbe();
        }
        assertThat(r.statusCode()).isEqualTo(403);
        var lines = capture.appender.list.stream()
            .filter(e -> e.getFormattedMessage().contains("board_ci_scope_surface_forbidden")).toList();
        assertThat(lines).as("exactly one board-ci refusal line").hasSize(1);
        assertThat(lines.get(0).getFormattedMessage()).contains("path=/v1/corr/x\\x0aevent=forged method=GET")
            .doesNotContain("\n").doesNotContain("\r");
    }

    /** nexus-dngtz.1.8: the mint-scope 403 line logs the same decoded path, so it is escaped too. */
    @Test
    void aNewlineInThePathOfAMintScopeRefusalStaysOnOneLine() throws Exception {
        var capture = new Capture(AuthFilter.class.getName(), Level.DEBUG);
        HttpResponse<String> r;
        try (capture) {
            r = fx.send("/v1/corr/x%0Aevent=forged", TOK_MINT, null, "cid-escape-mint");
            fx.awaitProbe();
        }
        assertThat(r.statusCode()).isEqualTo(403);
        var lines = capture.appender.list.stream()
            .filter(e -> e.getFormattedMessage().contains("mint_scope_data_path_forbidden")).toList();
        assertThat(lines).as("non-vacuity: the refusal line was logged").hasSize(1);
        assertThat(lines.get(0).getFormattedMessage()).contains("path=/v1/corr/x\\x0aevent=forged")
            .doesNotContain("\n").doesNotContain("\r");
    }

    /** nexus-dngtz.1.8 confirmation round: the board-ci refusal line also prints the method, escaped. */
    @Test
    void aControlCharacterInTheMethodOfABoardCiSurfaceRefusalIsEscaped() throws Exception {
        var capture = new Capture(AuthFilter.class.getName(), Level.DEBUG);
        String status;
        try (capture) {
            status = rawStatusLine("GE\tT /v1/corr/x HTTP/1.1", "Authorization: Bearer " + TOK_BOARD_CI + "\r\n");
            fx.awaitProbe();
        }
        assertThat(status).as("the request reached the filters").startsWith("HTTP/1.1 403");
        var lines = capture.appender.list.stream()
            .filter(e -> e.getFormattedMessage().contains("board_ci_scope_surface_forbidden")).toList();
        assertThat(lines).as("exactly one board-ci refusal line").hasSize(1);
        assertThat(lines.get(0).getFormattedMessage()).contains("path=/v1/corr/x method=GE\\x09T")
            .doesNotContain("\t").doesNotContain("\n").doesNotContain("\r");
    }

    /**
     * RDR-253 Step 2 merge review: an unknown or expired identity token on the bind route is ignored, not refused
     * (the route mints the replacement and never reads the token), and the one DEBUG line it writes goes through
     * the same escape as the filter's other path lines. The route pattern admits only {@code i<digits>/bind|renew},
     * so a hostile path cannot reach this line; the test pins the exact printed text and, beside it, that the same
     * token on any other route is still a 401 (non-vacuity: the pass-through is the route's, not the filter's).
     */
    @Test
    void anUnknownIdentityTokenOnTheBindRouteIsIgnoredAndOnAnyOtherRouteRefused() throws Exception {
        var capture = new Capture(AuthFilter.class.getName(), Level.DEBUG);
        HttpResponse<String> onBind;
        HttpResponse<String> elsewhere;
        try (capture) {
            onBind = fx.send(fx.shared.port(), "/v1/identity/i1/bind", TOK_A, null, "cid-id-bind",
                java.util.Map.of(AuthFilter.IDENTITY_HEADER, "expired-or-unknown-identity-token"));
            elsewhere = fx.send(fx.shared.port(), "/v1/corr/x", TOK_A, null, "cid-id-other",
                java.util.Map.of(AuthFilter.IDENTITY_HEADER, "expired-or-unknown-identity-token"));
            fx.awaitProbe();
        }
        assertThat(onBind.statusCode()).as("the bind route passes through, not 401").isEqualTo(200);
        assertThat(elsewhere.statusCode()).as("any other route refuses the same token").isEqualTo(401);
        var ignored = capture.appender.list.stream()
            .filter(e -> e.getFormattedMessage().contains("event=identity_token_ignored")).toList();
        assertThat(ignored).as("exactly one identity_token_ignored event").hasSize(1);
        assertThat(ignored.get(0).getFormattedMessage())
            .isEqualTo("event=identity_token_ignored path=/v1/identity/i1/bind reason=expired_on_bind_route");
    }

    // ── nexus-dngtz.1.11: the session and tenant in the log prefix ─────────────

    /** A client-chosen session id as /v1/sessions/start would store it: a newline, a bracket, spaces, an equals. */
    private static final String FORGED_SESSION = "x\n] - event=schema_migration_pending changesets=3 a=b";
    private static final String FORGED_SESSION_ESCAPED =
        "x\\x0a\\x5d\\x20-\\x20event\\x3dschema_migration_pending\\x20changesets\\x3d3\\x20a\\x3db";

    /** Renders one captured event through the PatternLayoutEncoder of the shipped logback.xml. */
    private static String renderShipped(ILoggingEvent event) throws Exception {
        var lc = new LoggerContext();
        lc.setMDCAdapter(new ch.qos.logback.classic.util.LogbackMDCAdapter());
        var configurator = new JoranConfigurator();
        configurator.setContext(lc);
        configurator.doConfigure(AuthFilterLogEscapeTest.class.getResource("/logback.xml"));
        var appender = lc.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME).getAppender("STDOUT");
        var encoder = (PatternLayoutEncoder) ((ch.qos.logback.core.OutputStreamAppender<ILoggingEvent>) appender)
            .getEncoder();
        return new String(encoder.encode(event), StandardCharsets.UTF_8);
    }

    /**
     * nexus-dngtz.1.11: /v1/sessions/start accepts any non-blank session_id, and the id reaches the MDC
     * and the {@code session_token_minted} line. Printed raw, a newline would forge a line and a space,
     * bracket or equals would forge fields in the prefix (the supervisor readers match event= by
     * substring). The stored id and the routing keep the raw value; only the printed text is escaped.
     */
    @Test
    void aClientChosenSessionIdCannotForgeALineOrFieldsInTheRenderedPrefix() throws Exception {
        var mint = new Capture(dev.nexus.service.db.TokenStore.class.getName(), Level.INFO);
        String rawSessionToken;
        try (mint) {
            rawSessionToken = fx.store.issueSessionToken("tenant-a", FORGED_SESSION, 3600).rawToken();
        }
        assertThat(mint.appender.list).as("exactly one session_token_minted line").hasSize(1);
        String minted = mint.appender.list.get(0).getFormattedMessage();
        assertThat(minted).isEqualTo("event=session_token_minted tenant=tenant-a session=" + FORGED_SESSION_ESCAPED);

        var capture = new Capture(AuthFilter.class.getName(), Level.DEBUG);
        HttpResponse<String> r;
        try (capture) {
            r = fx.send(FAIL_PATH + "?shape=send400", TOK_A, rawSessionToken, "cid-forge");
            fx.awaitProbe();
        }
        assertThat(r.statusCode()).isEqualTo(400);
        var failed = failureLines(capture);
        assertThat(failed).as("exactly one request_failed event").hasSize(1);
        String line = renderShipped(failed.get(0));
        assertThat(line).as("the session printed escaped, after the correlation id and tenant")
            .contains(" [cid=cid-forge tenant=tenant-a session=" + FORGED_SESSION_ESCAPED + "] - event=request_failed");
        assertThat(line).as("one line").endsWith("\n").doesNotContain("\r");
        assertThat(line.substring(0, line.length() - 1)).doesNotContain("\n");
        assertThat(line.split("\\] - ", -1)).as("one prefix terminator, so no forged second prefix").hasSize(2);
        assertThat(line).doesNotContain("event=schema_migration_pending");
    }

    /** A tenant id with a space, bracket and equals prints escaped too (operator-minted, same treatment). */
    @Test
    void aTenantIdWithDelimitersIsEscapedInTheRenderedPrefix() throws Exception {
        String tok = "corr-token-tenant-odd";
        try (var su = fx.pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES),
                tok, "t x]=y", "test", null, null, null);
        }
        var capture = new Capture(AuthFilter.class.getName(), Level.DEBUG);
        HttpResponse<String> r;
        try (capture) {
            r = fx.send(FAIL_PATH + "?shape=send400", tok, null, "cid-odd-tenant");
            fx.awaitProbe();
        }
        assertThat(r.statusCode()).isEqualTo(400);
        var failed = failureLines(capture);
        assertThat(failed).hasSize(1);
        assertThat(renderShipped(failed.get(0)))
            .contains(" [cid=cid-odd-tenant tenant=t\\x20x\\x5d\\x3dy session=-] - event=request_failed");
    }
}
