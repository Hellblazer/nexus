package dev.nexus.service;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TokenCache;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.http.AuthFilter;
import dev.nexus.service.http.RequestContext;
import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.ResultSet;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-gmiaf.32.2 — AuthFilter integration tests.
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), port 0, requires Docker. A fixed/mutable
 * {@link Clock} drives all expiry/TTL assertions deterministically. Two layers:
 * HTTP-level (the filter end to end against a real {@link HttpServer}) and
 * cache-level (the TTL/invalidate/expiry seam against {@link TokenCache}).
 *
 * <p>Required cases (from the bead): valid token → tenant resolved + client
 * X-Nexus-Tenant ignored; missing/unknown/revoked/expired → 401; cross-tenant
 * session → 401; minted session resolves server-side; bootstrap bare session;
 * cache hit; revocation via invalidate is immediate; revocation via TTL backstop;
 * expiry is precise on a cache hit.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class AuthFilterTest {

    private static final Instant T0 = Instant.parse("2026-06-09T00:00:00Z");

    /**
     * Explicit embed-deadline budget for {@code /v1/echo-deadline} (nexus-8hdg9
     * phase 2 review remediation, T2 {@code code-review-nexus-8hdg9-p2-5ce59b36d}
     * [24650]): fed to {@code AuthFilter}'s test-support 3-arg constructor so the
     * wiring can be asserted deterministically, without mutating the real
     * {@code NX_EMBED_DEADLINE_MS} process env.
     */
    private static final long EXPLICIT_DEADLINE_BUDGET_MS = 12_345L;

    /** Explicit hard ceiling for the capped contexts (nexus-8hdg9 phase 3 carry-in). */
    private static final long EXPLICIT_DEADLINE_MAX_MS = 20_000L;

    // Raw tokens (hashed before storage; AuthFilter hashes the presented token).
    private static final String TOK_A       = "raw-token-tenant-a";
    private static final String TOK_B       = "raw-token-tenant-b";
    private static final String TOK_REVOKED = "raw-token-revoked";
    private static final String TOK_EXPIRED = "raw-token-expired";
    private static final String SESS_A1     = "raw-session-a1";
    private static final String SESS_EXPIRED = "raw-session-expired";
    private static final String TOK_WILDCARD = "raw-token-bootstrap-wildcard";
    private static final String TOK_MINT     = "raw-token-mint-scope";
    private static final String TOK_DATA     = "raw-token-data-scope";

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    MutableClock clock;
    TokenStore store;
    TokenCache cache;
    HttpServer server;
    java.util.concurrent.ExecutorService serverExecutor;
    int port;
    final HttpClient http = HttpClient.newHttpClient();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        // grants-nexus-svc.xml fail-fasts if the role is absent; create it first.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
                + "  END IF; "
                + "END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }

        // nexus-5j7pb: back the code-under-test with nexus_svc (NOSUPERUSER NOBYPASSRLS),
        // the SAME credential as production, rather than the Postgres superuser (BYPASSRLS).
        // Scope honesty: these auth-resolution tests touch ONLY the credential tables
        // (service_tokens/session_tokens), which are RLS-off by design, so this does NOT
        // itself exercise an RLS boundary. Its value is harness honesty — the auth layer
        // runs under the production role and its real grants (grants-nexus-svc.xml,
        // runAlways/LAST), so an RLS policy ever added to a credential table would surface
        // as a fail-closed break here instead of being masked by BYPASSRLS. Production-role
        // convention consistent with TokenBoundaryAdversarialTest (which is where actual
        // cross-tenant RLS enforcement on domain tables is asserted).
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        cfg.setConnectionInitSql("SET search_path TO nexus, t1, public");
        ds = new HikariDataSource(cfg);

        clock = new MutableClock(T0);
        store = new TokenStore(ds, clock);
        cache = new TokenCache(store, clock);

        seedTokens();

        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        var ctx = server.createContext("/v1/echo", new EchoHandler());
        ctx.getFilters().add(new AuthFilter(cache, store));
        // nexus-868dq: the mint-scope path restriction allows ONLY /v1/data-tokens/*
        // through the filter — a second echo context proves pass-through there,
        // and a third at the adversarial near-miss path ("/v1/data-tokens-evil",
        // shares the string prefix but is NOT the mint surface) proves the check
        // is segment-exact, not a raw startsWith.
        var mintCtx = server.createContext("/v1/data-tokens", new EchoHandler());
        mintCtx.getFilters().add(new AuthFilter(cache, store));
        var evilCtx = server.createContext("/v1/data-tokens-evil", new EchoHandler());
        evilCtx.getFilters().add(new AuthFilter(cache, store));

        // nexus-8hdg9 phase 2 review remediation (T2 code-review-nexus-8hdg9-p2-5ce59b36d
        // [24650]): three contexts proving RequestContext.deadlineNanos()'s wiring end to
        // end. A SINGLE-THREAD executor is set explicitly (rather than relying on
        // com.sun.net.httpserver's unspecified default) so the "cleared after the request"
        // test below can assert on THREAD-LOCAL non-leakage deterministically: every request
        // this server ever handles, across every context, runs on the exact same one thread,
        // sequentially. Transparent to the 21 existing tests above -- they already drive the
        // server with synchronous, sequential http.send() calls from one JUnit test thread.
        serverExecutor = java.util.concurrent.Executors.newSingleThreadExecutor();
        server.setExecutor(serverExecutor);

        var deadlineExplicitCtx = server.createContext("/v1/echo-deadline", new DeadlineEchoHandler());
        deadlineExplicitCtx.getFilters().add(
            new AuthFilter(cache, store, EXPLICIT_DEADLINE_BUDGET_MS));
        var deadlineDefaultCtx = server.createContext("/v1/echo-deadline-default", new DeadlineEchoHandler());
        // Plain two-arg (production) constructor: real env, no NX_EMBED_DEADLINE_MS
        // override in this test process, so RequestDeadline.DEFAULT_DEADLINE_MS applies.
        deadlineDefaultCtx.getFilters().add(new AuthFilter(cache, store));
        // No AuthFilter at all -- proves clearing: a request here right after a
        // deadline-echoing request must see deadlineNanos() as null, not a leaked
        // value from the prior request's ThreadLocal.
        server.createContext("/v1/echo-deadline-noauth", new DeadlineEchoHandler());
        // nexus-8hdg9 phase 3 carry-in: an explicit hard ceiling (4-arg constructor),
        // below the header a client may send and, on the second context, below the
        // env default itself.
        var deadlineCappedCtx = server.createContext("/v1/echo-deadline-capped", new DeadlineEchoHandler());
        deadlineCappedCtx.getFilters().add(
            new AuthFilter(cache, store, EXPLICIT_DEADLINE_BUDGET_MS, EXPLICIT_DEADLINE_MAX_MS));
        var deadlineDefaultAboveCapCtx = server.createContext(
            "/v1/echo-deadline-default-above-cap", new DeadlineEchoHandler());
        deadlineDefaultAboveCapCtx.getFilters().add(
            new AuthFilter(cache, store, EXPLICIT_DEADLINE_MAX_MS * 3, EXPLICIT_DEADLINE_MAX_MS));

        server.start();
        port = server.getAddress().getPort();
    }

    @AfterAll
    void stopAll() {
        if (server != null) server.stop(0);
        if (serverExecutor != null) serverExecutor.shutdownNow();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void resetClock() {
        clock.set(T0);
    }

    private void seedTokens() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            insertServiceToken(su, TOK_A, "tenant-a", null, null);
            insertServiceToken(su, TOK_B, "tenant-b", null, null);
            insertServiceToken(su, TOK_REVOKED, "tenant-a", null, OffsetDateTime.ofInstant(T0.minusSeconds(60), ZoneOffset.UTC));
            insertServiceToken(su, TOK_EXPIRED, "tenant-a", OffsetDateTime.ofInstant(T0.minusSeconds(60), ZoneOffset.UTC), null);
            insertServiceToken(su, TOK_WILDCARD, AuthFilter.BOOTSTRAP_ANY_TENANT, null, null);
            insertSessionToken(su, SESS_A1, "tenant-a", "session-a1", OffsetDateTime.ofInstant(T0.plusSeconds(3600), ZoneOffset.UTC));
            insertSessionToken(su, SESS_EXPIRED, "tenant-a", "session-old", OffsetDateTime.ofInstant(T0.minusSeconds(60), ZoneOffset.UTC));
            // nexus-868dq scoped credentials.
            insertScopedToken(su, TOK_MINT, "edge-tenant", "mint");
            insertScopedToken(su, TOK_DATA, "tenant-a", "data");
        }
    }

    private void insertScopedToken(Connection su, String raw, String tenant, String scope)
            throws Exception {
        try (var ps = su.prepareStatement(
            "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label, scope) "
            + "VALUES (?, ?, ?, ?) ON CONFLICT (token_hash) DO NOTHING")) {
            ps.setString(1, TokenHashing.sha256Hex(raw));
            ps.setString(2, tenant);
            ps.setString(3, "test-scoped");
            ps.setString(4, scope);
            ps.executeUpdate();
        }
    }

    private void insertServiceToken(Connection su, String raw, String tenant,
                                    OffsetDateTime expiresAt, OffsetDateTime revokedAt) throws Exception {
        try (var ps = su.prepareStatement(
            "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label, expires_at, revoked_at) "
            + "VALUES (?, ?, ?, ?, ?) ON CONFLICT (token_hash) DO NOTHING")) {
            ps.setString(1, TokenHashing.sha256Hex(raw));
            ps.setString(2, tenant);
            ps.setString(3, "test");
            if (expiresAt == null) ps.setNull(4, java.sql.Types.TIMESTAMP_WITH_TIMEZONE); else ps.setObject(4, expiresAt);
            if (revokedAt == null) ps.setNull(5, java.sql.Types.TIMESTAMP_WITH_TIMEZONE); else ps.setObject(5, revokedAt);
            ps.executeUpdate();
        }
    }

    private void insertSessionToken(Connection su, String raw, String tenant,
                                    String sessionId, OffsetDateTime expiresAt) throws Exception {
        try (var ps = su.prepareStatement(
            "INSERT INTO nexus.session_tokens (session_token_hash, tenant_id, session_id, expires_at) "
            + "VALUES (?, ?, ?, ?) ON CONFLICT (session_token_hash) DO NOTHING")) {
            ps.setString(1, TokenHashing.sha256Hex(raw));
            ps.setString(2, tenant);
            ps.setString(3, sessionId);
            ps.setObject(4, expiresAt);
            ps.executeUpdate();
        }
    }

    // ── HTTP-level filter behavior ────────────────────────────────────────────

    @Test
    void validToken_resolvesTenant() throws Exception {
        HttpResponse<String> r = call(TOK_A, null, null);
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.body()).isEqualTo("tenant=tenant-a;session=;scope=tenant");
    }

    @Test
    void clientTenantHeader_isIgnored_resolvedWins() throws Exception {
        // Token resolves tenant-a; client lies "tenant-b" — must be ignored.
        HttpResponse<String> r = call(TOK_A, "tenant-b", null);
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.body()).isEqualTo("tenant=tenant-a;session=;scope=tenant");
    }

    @Test
    void missingBearer_is401() throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base() + "/v1/echo")).GET().build();
        HttpResponse<String> r = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).isEqualTo(401);
    }

    @Test
    void unknownToken_is401() throws Exception {
        assertThat(call("no-such-token", null, null).statusCode()).isEqualTo(401);
    }

    @Test
    void revokedToken_is401() throws Exception {
        assertThat(call(TOK_REVOKED, null, null).statusCode()).isEqualTo(401);
    }

    @Test
    void expiredToken_is401() throws Exception {
        assertThat(call(TOK_EXPIRED, null, null).statusCode()).isEqualTo(401);
    }

    @Test
    void mintedSession_resolvesServerSide() throws Exception {
        HttpResponse<String> r = call(TOK_A, null, SESS_A1);
        assertThat(r.statusCode()).isEqualTo(200);
        // Server-resolved session_id, NOT the presented token string.
        assertThat(r.body()).isEqualTo("tenant=tenant-a;session=session-a1;scope=tenant");
    }

    @Test
    void crossTenantSession_is401() throws Exception {
        // sess-a1 belongs to tenant-a; presenting it with a tenant-b bearer must 401.
        assertThat(call(TOK_B, null, SESS_A1).statusCode()).isEqualTo(401);
    }

    @Test
    void nonLiveSessionToken_is401() throws Exception {
        // Phase E require-minted (nexus-gmiaf.32.5): a present-but-non-live session
        // header is a 401. The transitional bootstrap path that stamped the raw value
        // as a bare session id is retired (it was the victim-impersonation vector).
        assertThat(call(TOK_A, null, "bare-session-xyz").statusCode()).isEqualTo(401);
    }

    @Test
    void expiredSessionToken_is401_notVictimSession() throws Exception {
        // An expired minted token has no LIVE row → 401 (no degrade to a bare id).
        assertThat(call(TOK_A, null, SESS_EXPIRED).statusCode()).isEqualTo(401);
    }

    @Test
    void noSessionHeader_proceedsTenantScoped() throws Exception {
        // A request with NO session header still authenticates and proceeds
        // tenant-scoped (session-scoped handlers simply are not exercised).
        HttpResponse<String> r = call(TOK_A, null, null);
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.body()).isEqualTo("tenant=tenant-a;session=;scope=tenant");
    }

    @Test
    void wildcardBoundToken_isDenied() throws Exception {
        // nexus-45ykb: a token whose tenant_id == "*" (a legacy grandfathered row — the
        // sentinel can no longer be minted) is now DENIED at the filter. Phase E retired
        // the any-tenant GRANT (the client header is already ignored); this closes the
        // residual legacy-credential vector: '*' is a reserved name that is never a
        // registered catalog_owners principal, so operating under it would write ghost
        // data under an unregistered tenant. Defense in depth → 401.
        HttpResponse<String> r = call(TOK_WILDCARD, "tenant-zzz", null);
        assertThat(r.statusCode()).isEqualTo(401);
    }

    // ── nexus-8hdg9 phase 2: RequestContext.deadlineNanos() wiring ────────────

    @Test
    void deadline_explicitBudget_visibleInsideHandler_matchesConfiguredBudget() throws Exception {
        long before = System.nanoTime();
        HttpResponse<String> r = deadlineCall("/v1/echo-deadline", TOK_A);
        long after = System.nanoTime();
        assertThat(r.statusCode()).isEqualTo(200);

        long deadlineNanos = Long.parseLong(r.body());
        long budgetNanos = java.util.concurrent.TimeUnit.MILLISECONDS.toNanos(EXPLICIT_DEADLINE_BUDGET_MS);
        // Deadline = AuthFilter's own System.nanoTime() call (between `before` and `after`)
        // plus the configured budget -- not the default, not a hardcoded value. A generous
        // tolerance (the full before..after bracket) absorbs the real gap between this test's
        // nanoTime() reads and AuthFilter's, with no risk of a false pass against the wrong
        // budget: EXPLICIT_DEADLINE_BUDGET_MS (12.345s) and DEFAULT_DEADLINE_MS (300s) differ
        // by orders of magnitude.
        assertThat(deadlineNanos)
            .as("deadline must reflect the EXPLICIT constructor budget, not the default")
            .isBetween(before + budgetNanos, after + budgetNanos);
    }

    @Test
    void deadline_defaultBudget_visibleInsideHandler_isPositiveAndFarInTheFuture() throws Exception {
        // The plain two-arg (production) AuthFilter constructor, real env, no
        // NX_EMBED_DEADLINE_MS override in this test process -- RequestDeadline
        // .DEFAULT_DEADLINE_MS (300s) applies. Assert order-of-magnitude correctness
        // (comfortably beyond EXPLICIT_DEADLINE_BUDGET_MS's 12.345s, comfortably under a
        // generous outer bound) rather than pinning the literal default here, so this test
        // does not silently rot into a second hand-copy of RequestDeadlineTest's own
        // default-value assertion.
        long now = System.nanoTime();
        HttpResponse<String> r = deadlineCall("/v1/echo-deadline-default", TOK_A);
        assertThat(r.statusCode()).isEqualTo(200);

        long deadlineNanos = Long.parseLong(r.body());
        long minExpectedNanos = now + java.util.concurrent.TimeUnit.SECONDS.toNanos(60);
        long maxExpectedNanos = now + java.util.concurrent.TimeUnit.SECONDS.toNanos(600);
        assertThat(deadlineNanos)
            .as("default-budget deadline must be well beyond the explicit-budget test's"
                + " 12.345s and well under this generous 600s outer bound")
            .isBetween(minExpectedNanos, maxExpectedNanos);
    }

    @Test
    void deadline_isCleared_afterRequestCompletes_doesNotLeakIntoNextRequest() throws Exception {
        // A deadline-setting request, immediately followed (same single-thread server
        // executor, see startAll()) by a request through a context with NO AuthFilter at
        // all. If AuthFilter's finally block ever stopped clearing the ThreadLocal, this
        // second request would observe the FIRST request's leftover deadline instead of
        // null -- exactly the cross-request leak RequestContext's own class javadoc warns
        // an unscoped ThreadLocal would risk.
        HttpResponse<String> first = deadlineCall("/v1/echo-deadline", TOK_A);
        assertThat(first.statusCode()).isEqualTo(200);

        HttpRequest req = HttpRequest.newBuilder(URI.create(base() + "/v1/echo-deadline-noauth"))
            .GET().build();
        HttpResponse<String> second = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(second.statusCode()).isEqualTo(200);
        assertThat(second.body())
            .as("no AuthFilter ran on this context -- deadlineNanos() must be null, not a"
                + " value leaked from the PRIOR request's ThreadLocal")
            .isEqualTo("null");
    }

    private HttpResponse<String> deadlineCall(String path, String bearer) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base() + path))
            .header("Authorization", "Bearer " + bearer).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    // ── nexus-8hdg9 phase 5: X-Nexus-Request-Deadline-Ms advisory header ──────
    //
    // All four run against /v1/echo-deadline (EXPLICIT_DEADLINE_BUDGET_MS = 12.345s
    // as the env-default stand-in) and bracket the echoed deadline between
    // before/after + the EXPECTED budget, the same shape as the phase-2 test above.
    // 5s vs 12.345s vs 99.999s differ enough that a wrong budget cannot false-pass.

    private static final long HEADER_BUDGET_BELOW_DEFAULT_MS = 5_000L;
    private static final long HEADER_BUDGET_ABOVE_DEFAULT_MS = 99_999L;

    private long echoedDeadlineWithHeader(String headerValue, long before, long[] afterOut) throws Exception {
        return echoedDeadlineWithHeader("/v1/echo-deadline", headerValue, afterOut);
    }

    private long echoedDeadlineWithHeader(String path, String headerValue, long[] afterOut) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base() + path))
            .header("Authorization", "Bearer " + TOK_A).GET();
        if (headerValue != null) {
            b.header(AuthFilter.REQUEST_DEADLINE_HEADER, headerValue);
        }
        HttpResponse<String> r = http.send(b.build(), HttpResponse.BodyHandlers.ofString());
        afterOut[0] = System.nanoTime();
        assertThat(r.statusCode()).isEqualTo(200);
        return Long.parseLong(r.body());
    }

    private static long nanos(long ms) {
        return java.util.concurrent.TimeUnit.MILLISECONDS.toNanos(ms);
    }

    @Test
    void deadlineHeaderOverridesEnvDefault() throws Exception {
        long before = System.nanoTime();
        long[] after = new long[1];
        long deadline = echoedDeadlineWithHeader(
            Long.toString(HEADER_BUDGET_BELOW_DEFAULT_MS), before, after);
        assertThat(deadline)
            .as("a present, positive header below the env default must be the budget used")
            .isBetween(before + nanos(HEADER_BUDGET_BELOW_DEFAULT_MS),
                       after[0] + nanos(HEADER_BUDGET_BELOW_DEFAULT_MS));
    }

    @Test
    void absentHeaderFallsBackToEnvDefault() throws Exception {
        long before = System.nanoTime();
        long[] after = new long[1];
        long deadline = echoedDeadlineWithHeader(null, before, after);
        assertThat(deadline)
            .as("no header: the constructor (env-default stand-in) budget applies unchanged")
            .isBetween(before + nanos(EXPLICIT_DEADLINE_BUDGET_MS),
                       after[0] + nanos(EXPLICIT_DEADLINE_BUDGET_MS));
    }

    @Test
    void malformedHeaderFallsBackToEnvDefault() throws Exception {
        long before = System.nanoTime();
        long[] after = new long[1];
        long deadline = echoedDeadlineWithHeader("soon-ish", before, after);
        assertThat(deadline)
            .as("a malformed advisory header is ignored (200, env default), never a 400")
            .isBetween(before + nanos(EXPLICIT_DEADLINE_BUDGET_MS),
                       after[0] + nanos(EXPLICIT_DEADLINE_BUDGET_MS));
    }

    @Test
    void oversizedHeaderWinsOverEnvDefault() throws Exception {
        long before = System.nanoTime();
        long[] after = new long[1];
        long deadline = echoedDeadlineWithHeader(
            Long.toString(HEADER_BUDGET_ABOVE_DEFAULT_MS), before, after);
        assertThat(deadline)
            .as("a header above the env default replaces it -- the client's own budget wins,"
                + " the env default is only the fallback")
            .isBetween(before + nanos(HEADER_BUDGET_ABOVE_DEFAULT_MS),
                       after[0] + nanos(HEADER_BUDGET_ABOVE_DEFAULT_MS));
    }

    // ── nexus-8hdg9 phase 3 carry-in: NX_EMBED_DEADLINE_MAX_MS hard ceiling ──

    @Test
    void headerAboveCeilingIsClampedToCeiling() throws Exception {
        long before = System.nanoTime();
        long[] after = new long[1];
        long deadline = echoedDeadlineWithHeader("/v1/echo-deadline-capped",
            Long.toString(HEADER_BUDGET_ABOVE_DEFAULT_MS), after);
        assertThat(deadline)
            .as("a 99.999s header against a 20s ceiling yields the ceiling, not the header")
            .isBetween(before + nanos(EXPLICIT_DEADLINE_MAX_MS),
                       after[0] + nanos(EXPLICIT_DEADLINE_MAX_MS));
    }

    @Test
    void headerBelowCeilingIsNotClamped() throws Exception {
        long before = System.nanoTime();
        long[] after = new long[1];
        long deadline = echoedDeadlineWithHeader("/v1/echo-deadline-capped",
            Long.toString(HEADER_BUDGET_BELOW_DEFAULT_MS), after);
        assertThat(deadline)
            .isBetween(before + nanos(HEADER_BUDGET_BELOW_DEFAULT_MS),
                       after[0] + nanos(HEADER_BUDGET_BELOW_DEFAULT_MS));
    }

    @Test
    void envDefaultAboveCeilingIsClampedWhenHeaderAbsent() throws Exception {
        long before = System.nanoTime();
        long[] after = new long[1];
        long deadline = echoedDeadlineWithHeader("/v1/echo-deadline-default-above-cap", null, after);
        assertThat(deadline)
            .as("an operator default (60s) above the ceiling (20s) is clamped to the ceiling")
            .isBetween(before + nanos(EXPLICIT_DEADLINE_MAX_MS),
                       after[0] + nanos(EXPLICIT_DEADLINE_MAX_MS));
    }

    @Test
    void leadingPlusHeaderFallsBackToEnvDefault() throws Exception {
        long before = System.nanoTime();
        long[] after = new long[1];
        long deadline = echoedDeadlineWithHeader("+" + HEADER_BUDGET_BELOW_DEFAULT_MS, before, after);
        assertThat(deadline)
            .as("'+5000' is not the client's grammar: ignored, env default applies")
            .isBetween(before + nanos(EXPLICIT_DEADLINE_BUDGET_MS),
                       after[0] + nanos(EXPLICIT_DEADLINE_BUDGET_MS));
    }

    // Non-ASCII digit headers cannot be exercised at this layer: java.net.http
    // refuses to SEND a non-ASCII header value ("invalid header value"), so the
    // resolver-level case lives in RequestDeadlineTest
    // .resolveBudgetMs_leadingPlusAndNonAsciiDigitsAreMalformed instead.

    // ── Cache-level seam (fresh cache per test, mutable clock) ────────────────

    @Test
    void cache_hitReturnsCorrectTenant() {
        var c = new TokenCache(store, clock);
        String h = TokenHashing.sha256Hex(TOK_A);
        assertThat(c.resolveTenant(h)).contains("tenant-a");
        assertThat(c.size()).isEqualTo(1);
        assertThat(c.resolveTenant(h)).contains("tenant-a");  // served from cache
        assertThat(c.size()).isEqualTo(1);
    }

    @Test
    void cache_revocationViaInvalidate_isImmediate() throws Exception {
        var c = new TokenCache(store, clock);
        String raw = "raw-token-invalidate";
        String h = TokenHashing.sha256Hex(raw);
        try (Connection su = pg.createConnection("")) {
            insertServiceToken(su, raw, "tenant-a", null, null);
        }
        assertThat(c.resolveTenant(h)).contains("tenant-a");  // now cached
        // Revoke in DB, then invalidate the cache entry — must be empty immediately.
        try (Connection su = pg.createConnection("")) {
            su.createStatement().execute(
                "UPDATE nexus.service_tokens SET revoked_at = now() WHERE token_hash = '" + h + "'");
        }
        c.invalidate(h);
        assertThat(c.resolveTenant(h)).as("invalidate must take effect immediately").isEmpty();
    }

    @Test
    void cache_revocationViaTtlBackstop() throws Exception {
        // TTL backstop: without an explicit invalidate, a revoked token keeps
        // resolving until the entry ages past the TTL, then re-reads the DB.
        var c = new TokenCache(store, clock, Duration.ofSeconds(30), 10_000);
        String raw = "raw-token-ttl";
        String h = TokenHashing.sha256Hex(raw);
        try (Connection su = pg.createConnection("")) {
            insertServiceToken(su, raw, "tenant-a", null, null);
        }
        assertThat(c.resolveTenant(h)).contains("tenant-a");  // cached at T0
        try (Connection su = pg.createConnection("")) {
            su.createStatement().execute(
                "UPDATE nexus.service_tokens SET revoked_at = now() WHERE token_hash = '" + h + "'");
        }
        clock.set(T0.plusSeconds(29));   // within TTL — still served (stale)
        assertThat(c.resolveTenant(h)).as("within TTL the revoked token still resolves").contains("tenant-a");
        clock.set(T0.plusSeconds(31));   // past TTL — re-reads DB, sees revocation
        assertThat(c.resolveTenant(h)).as("past TTL the revocation takes effect").isEmpty();
    }

    @Test
    void cache_expiryIsPreciseOnHit() throws Exception {
        // A cached token whose expires_at falls WITHIN the TTL window must be
        // rejected at the exact expiry instant (re-checked on every hit), not served
        // until the cache entry's TTL elapses.
        var c = new TokenCache(store, clock, Duration.ofSeconds(300), 10_000);
        String raw = "raw-token-expiring";
        String h = TokenHashing.sha256Hex(raw);
        try (Connection su = pg.createConnection("")) {
            insertServiceToken(su, raw, "tenant-a",
                OffsetDateTime.ofInstant(T0.plusSeconds(100), ZoneOffset.UTC), null);
        }
        assertThat(c.resolveTenant(h)).contains("tenant-a");  // cached, valid at T0
        clock.set(T0.plusSeconds(50));
        assertThat(c.resolveTenant(h)).as("still valid before expiry").contains("tenant-a");
        clock.set(T0.plusSeconds(99));
        assertThat(c.resolveTenant(h)).as("valid one second before expiry").contains("tenant-a");
        // Boundary: expiry is !isAfter(now), so the token is expired AT expires_at, not after.
        clock.set(T0.plusSeconds(100));
        assertThat(c.resolveTenant(h)).as("expired exactly AT expires_at (boundary)").isEmpty();
        clock.set(T0.plusSeconds(101));  // past expiry but well within the 300s TTL
        assertThat(c.resolveTenant(h)).as("expiry re-checked on hit, before TTL elapses").isEmpty();
    }

    @Test
    void ensureBootstrapToken_nullNoop_seedsBoundDefault_idempotent() throws Exception {
        String raw = "raw-bootstrap-ensure";
        String h = TokenHashing.sha256Hex(raw);
        String defaultTenant = dev.nexus.service.db.TenantConstants.DEFAULT_TENANT;

        // null / blank is a no-op (no row, no error).
        store.ensureBootstrapToken(null, defaultTenant);
        store.ensureBootstrapToken("   ", defaultTenant);
        assertThat(store.lookupServiceToken(h)).isEmpty();

        // Phase E (nexus-gmiaf.32.5): seeds a BOUND default-tenant row, not a wildcard.
        store.ensureBootstrapToken(raw, defaultTenant);
        var seeded = store.lookupServiceToken(h);
        assertThat(seeded).isPresent();
        assertThat(seeded.get().tenantId()).isEqualTo(defaultTenant);

        // Idempotent: a second call does not error or duplicate.
        store.ensureBootstrapToken(raw, defaultTenant);
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) AS c FROM nexus.service_tokens WHERE token_hash = '" + h + "'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("c")).as("bootstrap seed must be idempotent (one row)").isEqualTo(1L);
        }
    }

    // ── nexus-868dq: scope threading + mint-scope path restriction ────────────

    @Test
    void dataScope_threadsThroughToRequestContext() throws Exception {
        HttpResponse<String> r = call(TOK_DATA, null, null);
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.body()).isEqualTo("tenant=tenant-a;session=;scope=data");
    }

    @Test
    void mintScope_onOrdinaryRoute_is403() throws Exception {
        // A mint credential exists to call /v1/data-tokens/* and nothing else
        // (RDR-005 pin: rejected on ALL admin routes; no data-path authority).
        HttpResponse<String> r = call(TOK_MINT, null, null);
        assertThat(r.statusCode()).isEqualTo(403);
    }

    @Test
    void mintScope_onDataTokensPath_passesFilter() throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base() + "/v1/data-tokens/mint"))
            .header("Authorization", "Bearer " + TOK_MINT).GET().build();
        HttpResponse<String> r = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.body()).isEqualTo("tenant=edge-tenant;session=;scope=mint");
    }

    @Test
    void mintScope_craftedPathPrefix_is403() throws Exception {
        // The TRUE adversarial near-miss: "/v1/data-tokens-evil" shares the raw
        // string prefix with the mint surface. A context IS bound there (see
        // startAll) so the FILTER decides — the segment-exact check must 403 it.
        HttpRequest evil = HttpRequest.newBuilder(URI.create(base() + "/v1/data-tokens-evil"))
            .header("Authorization", "Bearer " + TOK_MINT).GET().build();
        assertThat(http.send(evil, HttpResponse.BodyHandlers.ofString()).statusCode())
            .as("raw-prefix near-miss path must be outside the mint surface")
            .isEqualTo(403);
        // And an ordinary unrelated path is 403 too.
        HttpRequest echo = HttpRequest.newBuilder(URI.create(base() + "/v1/echo/data-tokens"))
            .header("Authorization", "Bearer " + TOK_MINT).GET().build();
        assertThat(http.send(echo, HttpResponse.BodyHandlers.ofString()).statusCode())
            .isEqualTo(403);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private String base() {
        return "http://127.0.0.1:" + port;
    }

    private HttpResponse<String> call(String bearer, String tenantHeader, String sessionHeader) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base() + "/v1/echo"))
            .header("Authorization", "Bearer " + bearer).GET();
        if (tenantHeader != null) b.header("X-Nexus-Tenant", tenantHeader);
        if (sessionHeader != null) b.header("X-Nexus-T1-Session", sessionHeader);
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    /** Echoes the AuthFilter-stamped (thread-confined) tenant + session + scope principal. */
    static final class EchoHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange ex) throws IOException {
            String tenant = RequestContext.tenant();
            String session = RequestContext.session();
            String scope = RequestContext.scope();
            String body = "tenant=" + (tenant == null ? "" : tenant)
                + ";session=" + (session == null ? "" : session)
                + ";scope=" + (scope == null ? "" : scope);
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            ex.sendResponseHeaders(200, bytes.length);
            try (OutputStream os = ex.getResponseBody()) {
                os.write(bytes);
            }
        }
    }

    /**
     * Echoes {@link RequestContext#deadlineNanos()} as a bare long, or the literal
     * string {@code "null"} when unset -- e.g. on a context with no {@link AuthFilter}
     * attached (nexus-8hdg9 phase 2 review remediation).
     */
    static final class DeadlineEchoHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange ex) throws IOException {
            Long deadlineNanos = RequestContext.deadlineNanos();
            byte[] bytes = String.valueOf(deadlineNanos).getBytes(StandardCharsets.UTF_8);
            ex.sendResponseHeaders(200, bytes.length);
            try (OutputStream os = ex.getResponseBody()) {
                os.write(bytes);
            }
        }
    }

    /** A {@link Clock} whose instant can be advanced for deterministic expiry/TTL tests. */
    static final class MutableClock extends Clock {
        private volatile Instant instant;
        MutableClock(Instant instant) { this.instant = instant; }
        void set(Instant instant) { this.instant = instant; }
        @Override public ZoneId getZone() { return ZoneOffset.UTC; }
        @Override public Clock withZone(ZoneId zone) { return this; }
        @Override public Instant instant() { return instant; }
    }
}
