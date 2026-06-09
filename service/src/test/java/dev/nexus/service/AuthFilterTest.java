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
import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
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
 * <p>Hermetic: embedded Postgres (io.zonky), port 0, no Docker. A fixed/mutable
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

    // Raw tokens (hashed before storage; AuthFilter hashes the presented token).
    private static final String TOK_A       = "raw-token-tenant-a";
    private static final String TOK_B       = "raw-token-tenant-b";
    private static final String TOK_REVOKED = "raw-token-revoked";
    private static final String TOK_EXPIRED = "raw-token-expired";
    private static final String SESS_A1     = "raw-session-a1";
    private static final String SESS_EXPIRED = "raw-session-expired";
    private static final String TOK_WILDCARD = "raw-token-bootstrap-wildcard";

    EmbeddedPostgres pg;
    HikariDataSource ds;
    MutableClock clock;
    TokenStore store;
    TokenCache cache;
    HttpServer server;
    int port;
    final HttpClient http = HttpClient.newHttpClient();

    @BeforeAll
    void startAll() throws Exception {
        pg = EmbeddedPostgres.builder().start();
        // grants-nexus-svc.xml fail-fasts if the role is absent; create it first.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
                + "  END IF; "
                + "END $$");
        }
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        cfg.setUsername("postgres");
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
        server.start();
        port = server.getAddress().getPort();
    }

    @AfterAll
    void stopAll() {
        if (server != null) server.stop(0);
        if (ds != null) ds.close();
        if (pg != null) { try { pg.close(); } catch (IOException ignored) { } }
    }

    @BeforeEach
    void resetClock() {
        clock.set(T0);
    }

    private void seedTokens() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            insertServiceToken(su, TOK_A, "tenant-a", null, null);
            insertServiceToken(su, TOK_B, "tenant-b", null, null);
            insertServiceToken(su, TOK_REVOKED, "tenant-a", null, OffsetDateTime.ofInstant(T0.minusSeconds(60), ZoneOffset.UTC));
            insertServiceToken(su, TOK_EXPIRED, "tenant-a", OffsetDateTime.ofInstant(T0.minusSeconds(60), ZoneOffset.UTC), null);
            insertServiceToken(su, TOK_WILDCARD, AuthFilter.BOOTSTRAP_ANY_TENANT, null, null);
            insertSessionToken(su, SESS_A1, "tenant-a", "session-a1", OffsetDateTime.ofInstant(T0.plusSeconds(3600), ZoneOffset.UTC));
            insertSessionToken(su, SESS_EXPIRED, "tenant-a", "session-old", OffsetDateTime.ofInstant(T0.minusSeconds(60), ZoneOffset.UTC));
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
        assertThat(r.body()).isEqualTo("tenant=tenant-a;session=");
    }

    @Test
    void clientTenantHeader_isIgnored_resolvedWins() throws Exception {
        // Token resolves tenant-a; client lies "tenant-b" — must be ignored.
        HttpResponse<String> r = call(TOK_A, "tenant-b", null);
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.body()).isEqualTo("tenant=tenant-a;session=");
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
        assertThat(r.body()).isEqualTo("tenant=tenant-a;session=session-a1");
    }

    @Test
    void crossTenantSession_is401() throws Exception {
        // sess-a1 belongs to tenant-a; presenting it with a tenant-b bearer must 401.
        assertThat(call(TOK_B, null, SESS_A1).statusCode()).isEqualTo(401);
    }

    @Test
    void bootstrapSession_bareIdStampedAsIs() throws Exception {
        // No minted row for this value → transitional bootstrap: stamped verbatim.
        HttpResponse<String> r = call(TOK_A, null, "bare-session-xyz");
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.body()).isEqualTo("tenant=tenant-a;session=bare-session-xyz");
    }

    @Test
    void expiredSessionToken_degradesToBootstrap_notVictimSession() throws Exception {
        // An expired minted token has no LIVE row → bootstrap path stamps the raw
        // presented value (the token string), NEVER the victim's resolved session_id.
        // Transitional posture; Phase D + a require-minted flip makes this a 401.
        HttpResponse<String> r = call(TOK_A, null, SESS_EXPIRED);
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.body()).isEqualTo("tenant=tenant-a;session=" + SESS_EXPIRED);
    }

    @Test
    void bootstrapWildcardToken_takesTenantFromHeader() throws Exception {
        // Transitional grandfathered token (tenant_id="*") may claim any tenant.
        HttpResponse<String> ra = call(TOK_WILDCARD, "tenant-a", null);
        assertThat(ra.statusCode()).isEqualTo(200);
        assertThat(ra.body()).isEqualTo("tenant=tenant-a;session=");
        HttpResponse<String> rb = call(TOK_WILDCARD, "tenant-zzz", null);
        assertThat(rb.statusCode()).isEqualTo(200);
        assertThat(rb.body()).isEqualTo("tenant=tenant-zzz;session=");
    }

    @Test
    void bootstrapWildcardToken_missingTenantHeader_is400() throws Exception {
        // The wildcard token has no bound tenant, so the header is REQUIRED.
        assertThat(call(TOK_WILDCARD, null, null).statusCode()).isEqualTo(400);
    }

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
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            insertServiceToken(su, raw, "tenant-a", null, null);
        }
        assertThat(c.resolveTenant(h)).contains("tenant-a");  // now cached
        // Revoke in DB, then invalidate the cache entry — must be empty immediately.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
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
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            insertServiceToken(su, raw, "tenant-a", null, null);
        }
        assertThat(c.resolveTenant(h)).contains("tenant-a");  // cached at T0
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
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
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            insertServiceToken(su, raw, "tenant-a",
                OffsetDateTime.ofInstant(T0.plusSeconds(100), ZoneOffset.UTC), null);
        }
        assertThat(c.resolveTenant(h)).contains("tenant-a");  // cached, valid at T0
        clock.set(T0.plusSeconds(50));
        assertThat(c.resolveTenant(h)).as("still valid before expiry").contains("tenant-a");
        clock.set(T0.plusSeconds(101));  // past expiry but well within the 300s TTL
        assertThat(c.resolveTenant(h)).as("expiry re-checked on hit, before TTL elapses").isEmpty();
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

    /** Echoes the AuthFilter-stamped (thread-confined) tenant + session principal. */
    static final class EchoHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange ex) throws IOException {
            String tenant = RequestContext.tenant();
            String session = RequestContext.session();
            String body = "tenant=" + (tenant == null ? "" : tenant)
                + ";session=" + (session == null ? "" : session);
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
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
