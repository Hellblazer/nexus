package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
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
import java.util.Map;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 P5.3 Phase F (bead nexus-gmiaf.32.6) — the cross-cutting adversarial
 * security test matrix mandated by the locked token-lifecycle decisions
 * (T2 nexus_rdr/gmiaf.32-token-design-DECISIONS).
 *
 * <p>This bead IS the tests. Each row is an executable assertion against the LIVE
 * boundary, integration over mocks: a real {@link NexusService} bound to a real
 * embedded Postgres through an RLS-ENFORCING role ({@code NOSUPERUSER
 * NOBYPASSRLS}), so cross-tenant / cross-session denials are real RLS / column-
 * predicate filtering, not app-layer pretence. Sibling data is seeded via a
 * SUPERUSER connection (which bypasses RLS) and its existence is proven by an
 * explicit {@code COUNT(*) == 1} before each negative — the denials are
 * non-vacuous (the row exists and is invisible, not merely absent).
 *
 * <p>EXACT assertions only ({@code == N} rows, {@code == status}); never
 * inequalities (feedback_exact_assertions_for_fixture_regression).
 *
 * <p>The six classes:
 * <ol>
 *   <li><b>Cross-tenant</b> — a tenant-A token cannot read tenant-B rows; a forged
 *       {@code X-Nexus-Tenant} header is ignored (server-resolved tenant wins).</li>
 *   <li><b>Cross-session</b> — a session-S1 token cannot read/close session-S2
 *       within one tenant (server-enforced: 403 on a named sibling, 0 rows on its
 *       own scope).</li>
 *   <li><b>Rotation overlap</b> — after rotate, both old and new authorize through
 *       the grace window; the old 401s exactly at grace expiry. Deterministic clock.</li>
 *   <li><b>Revocation</b> — immediate via cache invalidate; bounded by the cache TTL
 *       otherwise (the {@link TokenCache#DEFAULT_TTL} backstop, asserted at the bound).</li>
 *   <li><b>Expiry</b> — an expired service token and an expired session token both 401.</li>
 *   <li><b>Missing / blank / malformed</b> — no header, blank bearer, wrong scheme all
 *       401, preserving the 401-no-{@code WWW-Authenticate} posture.</li>
 * </ol>
 *
 * <p>Two harnesses share one embedded Postgres: the full-stack {@link NexusService}
 * (system clock) covers the data-plane denials, expiry, missing/blank, and immediate
 * revocation; a {@link MutableClock}-driven {@link AuthFilter} server (classes 3 and the
 * revocation TTL bound) covers the precise grace/TTL crossings the service's system
 * clock cannot drive.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TokenBoundaryAdversarialTest {

    // ── Tenants / sessions ────────────────────────────────────────────────────
    private static final String TENANT_A = "adv-tenant-a";
    private static final String TENANT_B = "adv-tenant-b";
    private static final String SESS_1   = "adv-sess-1";
    private static final String SESS_2   = "adv-sess-2";

    // ── Raw bearer / session secrets (hashed before storage) ──────────────────
    private static final String TOK_A             = "adv-raw-token-tenant-a";
    private static final String TOK_B             = "adv-raw-token-tenant-b";
    private static final String TOK_EXPIRED_A     = "adv-raw-token-expired-a";
    private static final String TOK_REVOKE_ME     = "adv-raw-token-revoke-me";
    private static final String SESS_TOK_1        = "adv-raw-session-token-1";
    private static final String SESS_TOK_2        = "adv-raw-session-token-2";
    private static final String SESS_TOK_EXPIRED  = "adv-raw-session-token-expired";

    private static final String SVC_ROLE = "svc_adv_matrix_test";
    private static final String SVC_PASS = "svc_adv_matrix_test_pass";

    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    EmbeddedPostgres pg;
    NexusService service;
    HikariDataSource svcDs;
    HttpClient http;
    ObjectMapper mapper;

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        http   = HttpClient.newHttpClient();
        pg     = EmbeddedPostgres.builder().start();

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            // RLS-enforcing app role: NOSUPERUSER NOBYPASSRLS makes the tenant policy a
            // real boundary. nexus_svc is fail-fast-required by grants-nexus-svc.xml.
            su.createStatement().execute(
                "DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS
                + "    ' NOSUPERUSER NOBYPASSRLS; "
                + "  END IF; "
                + "END $$");
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

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("GRANT USAGE ON SCHEMA t1 TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.memory TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.memory_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON t1.scratch TO " + SVC_ROLE);
            // AuthFilter resolves bearer→tenant against the (RLS-off) credential tables
            // as the app role: SELECT only (writes go through the superuser seed path).
            su.createStatement().execute(
                "GRANT SELECT ON nexus.service_tokens, nexus.session_tokens TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, t1, public");

            // Bound service tokens (Phase E: every token is strictly tenant-bound).
            insertServiceToken(su, TOK_A, TENANT_A, null, null);
            insertServiceToken(su, TOK_B, TENANT_B, null, null);
            // Expired service token bound to tenant-a (class 5).
            insertServiceToken(su, TOK_EXPIRED_A, TENANT_A, ago(su, 60), null);
            // A live token we will revoke at runtime (class 4 immediate).
            insertServiceToken(su, TOK_REVOKE_ME, TENANT_A, null, null);

            // Minted session tokens for the cross-session matrix (same tenant).
            insertSessionToken(su, SESS_TOK_1, TENANT_A, SESS_1, ahead(su, 3600));
            insertSessionToken(su, SESS_TOK_2, TENANT_A, SESS_2, ahead(su, 3600));
            // Expired minted session token (class 5).
            insertSessionToken(su, SESS_TOK_EXPIRED, TENANT_A, "adv-sess-old", ago(su, 60));
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        service = new NexusService(0, TOK_A, svcDs);
        service.start();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (pg      != null) pg.close();
    }

    // ══════════════════════════════════════════════════════════════════════════
    //  Class 1 — CROSS-TENANT denial
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void crossTenant_siblingTenantRowExists_butTokenAseesZero() throws Exception {
        // Seed a tenant-B memory row via the SUPERUSER path (bypasses RLS).
        String project = "adv-xtenant-" + System.nanoTime();
        seedMemoryRow(TENANT_B, project, "secret-title", "tenant-B secret");

        // Non-vacuous: the row genuinely exists in tenant-B's space.
        assertThat(superuserMemoryCount(TENANT_B, project))
            .as("sibling tenant-B row must exist before the denial is meaningful")
            .isEqualTo(1);

        // Positive control: the OWNER (tenant-B's bound token) DOES see the row, so a
        // tenant-A 404 is a real denial, not an artifact of a mis-seeded fixture.
        HttpResponse<String> owner = svcGet(
            "/v1/memory/get?project=" + project + "&title=secret-title", TOK_B, TENANT_B);
        assertThat(owner.statusCode())
            .as("owner tenant-B must see its own row (positive control)").isEqualTo(200);

        // tenant-A's bound token must NOT see it: RLS filters → 404 (not an error).
        HttpResponse<String> r = svcGet(
            "/v1/memory/get?project=" + project + "&title=secret-title", TOK_A, TENANT_A);
        assertThat(r.statusCode())
            .as("tenant-A GET of a tenant-B row must 404 (RLS yields 0 visible rows)")
            .isEqualTo(404);
    }

    @Test
    void crossTenant_forgedTenantHeaderIsIgnored_boundTenantWins() throws Exception {
        // tenant-A token lies "X-Nexus-Tenant: tenant-b" — the server-resolved tenant wins.
        HttpResponse<String> r = svcGet("/v1/_whoami", TOK_A, TENANT_B);
        assertThat(r.statusCode()).isEqualTo(200);
        Map<String, Object> body = mapper.readValue(r.body(), MAP_T);
        assertThat(body.get("tenant"))
            .as("forged X-Nexus-Tenant ignored; bound tenant resolved").isEqualTo(TENANT_A);
        assertThat(body.get("guc_tenant"))
            .as("the GUC actually stamped is the bound tenant, not the forged header")
            .isEqualTo(TENANT_A);
    }

    // ══════════════════════════════════════════════════════════════════════════
    //  Class 2 — CROSS-SESSION denial (same tenant)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void crossSession_siblingSessionRowExists_butSessionOneSeesZero() throws Exception {
        // Seed a scratch row owned by session-2 (tenant-A) via superuser.
        String id = UUID.randomUUID().toString();
        seedScratchRow(id, TENANT_A, SESS_2, "session-2 private note");

        // Non-vacuous: the sibling-session row exists.
        assertThat(superuserScratchCount(TENANT_A, SESS_2))
            .as("sibling session-2 row must exist").isEqualTo(1);

        // Positive control: session-2's OWN minted token sees its row, so session-1's
        // not-found below is a real denial, not a mis-seeded fixture.
        HttpResponse<String> owner = sessionPost("/v1/t1/get", TOK_A, SESS_TOK_2,
            json("id", id, "session_id", SESS_2));
        assertThat(owner.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(owner.body(), MAP_T).get("id"))
            .as("session-2's own token sees its row (positive control)").isEqualTo(id);

        // session-1's minted token, scoped to its OWN session, sees 0 of session-2's rows.
        HttpResponse<String> r = sessionPost("/v1/t1/get", TOK_A, SESS_TOK_1,
            json("id", id, "session_id", SESS_1));
        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(r.body(), MAP_T).get("found"))
            .as("session-1 must not see a session-2 row (column-predicate isolation)")
            .isEqualTo(false);
    }

    @Test
    void crossSession_namingSiblingSessionInBody_is403() throws Exception {
        // session-1's minted token names session-2 in the body → server rejects the
        // cross-session attempt with 403 (resolved session id is authoritative).
        HttpResponse<String> r = sessionPost("/v1/t1/get", TOK_A, SESS_TOK_1,
            json("id", "anything", "session_id", SESS_2));
        assertThat(r.statusCode())
            .as("a session-1 token may not act as session-2 within one tenant")
            .isEqualTo(403);
    }

    @Test
    void crossSession_closingSiblingSessionIsDenied_butOwnSucceeds() throws Exception {
        // /session/close is the highest-impact cross-session surface.
        HttpResponse<String> denied = sessionPost("/v1/t1/session/close", TOK_A, SESS_TOK_1,
            json("session_id", SESS_2));
        assertThat(denied.statusCode())
            .as("session-1 token must not close session-2").isEqualTo(403);

        HttpResponse<String> ok = sessionPost("/v1/t1/session/close", TOK_A, SESS_TOK_1,
            json("session_id", SESS_1));
        assertThat(ok.statusCode())
            .as("closing its OWN session is allowed").isEqualTo(200);
    }

    // ══════════════════════════════════════════════════════════════════════════
    //  Class 5 — EXPIRY
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void expiredServiceToken_is401() throws Exception {
        assertThat(svcGet("/v1/_whoami", TOK_EXPIRED_A, TENANT_A).statusCode())
            .as("expired service token → 401").isEqualTo(401);
    }

    @Test
    void expiredSessionToken_is401() throws Exception {
        // Valid service token, but a present-but-expired minted session header → 401
        // (require-minted: a non-live session row never degrades to a bare id).
        HttpResponse<String> r = sessionPost("/v1/t1/get", TOK_A, SESS_TOK_EXPIRED,
            json("id", "x", "session_id", "adv-sess-old"));
        assertThat(r.statusCode())
            .as("expired session token → 401").isEqualTo(401);
    }

    // ══════════════════════════════════════════════════════════════════════════
    //  Class 6 — MISSING / BLANK / MALFORMED
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void missingAuthorizationHeader_is401() throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(svcBase() + "/v1/_whoami"))
            .GET().build();
        assertThat(http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode())
            .as("no Authorization header → 401").isEqualTo(401);
    }

    @Test
    void blankBearer_is401() throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(svcBase() + "/v1/_whoami"))
            .header("Authorization", "Bearer ").GET().build();
        assertThat(http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode())
            .as("blank bearer → 401").isEqualTo(401);
    }

    @Test
    void malformedScheme_is401() throws Exception {
        // A non-Bearer scheme is rejected before any token resolution.
        HttpRequest req = HttpRequest.newBuilder(URI.create(svcBase() + "/v1/_whoami"))
            .header("Authorization", "Basic " + TOK_A).GET().build();
        assertThat(http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode())
            .as("malformed (non-Bearer) scheme → 401").isEqualTo(401);
    }

    // ══════════════════════════════════════════════════════════════════════════
    //  Class 4 — REVOCATION (immediate, through the live service cache)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void revokedToken_is401_immediatelyAfterCacheInvalidate() throws Exception {
        // Warm the live cache: the token resolves (200) and is now cached.
        assertThat(svcGet("/v1/_whoami", TOK_REVOKE_ME, TENANT_A).statusCode())
            .as("token is live before revocation").isEqualTo(200);

        // Revoke out-of-band, then invalidate the SAME cache the AuthFilter reads.
        String hash = TokenHashing.sha256Hex(TOK_REVOKE_ME);
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            try (var ps = su.prepareStatement(
                "UPDATE nexus.service_tokens SET revoked_at = now() WHERE token_hash = ?")) {
                ps.setString(1, hash);
                assertThat(ps.executeUpdate())
                    .as("exactly one row revoked").isEqualTo(1);
            }
        }
        service.getTokenCache().invalidate(hash);

        assertThat(svcGet("/v1/_whoami", TOK_REVOKE_ME, TENANT_A).statusCode())
            .as("revoked token → 401 immediately after invalidate").isEqualTo(401);
    }

    // ══════════════════════════════════════════════════════════════════════════
    //  Class 3 — ROTATION overlap   (deterministic-clock AuthFilter harness)
    //  Class 4 — REVOCATION TTL bound (same harness)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    void rotation_bothTokensAuthorizeWithinGrace_oldExpiresAtGraceBound() throws Exception {
        Instant t0 = Instant.parse("2026-06-09T00:00:00Z");
        MutableClock clock = new MutableClock(t0);
        try (ClockHarness h = new ClockHarness(clock)) {
            String tenant = "rot-tenant-" + System.nanoTime();
            long graceSeconds = 300;

            // An existing ("old") live token, then rotate → new token + old grace-expiring.
            TokenStore.IssuedToken old = h.store.issueToken(tenant, "old", null);
            TokenStore.RotationResult rot = h.store.rotateTokens(tenant, graceSeconds);
            String newRaw = rot.issued().rawToken();

            // Within the grace window (T0): BOTH authorize.
            assertThat(h.call(old.rawToken()).statusCode())
                .as("old token authorizes within grace").isEqualTo(200);
            assertThat(h.call(newRaw).statusCode())
                .as("new token authorizes within grace").isEqualTo(200);

            // One second before grace expiry: old STILL authorizes (no early 401).
            clock.set(t0.plusSeconds(graceSeconds - 1));
            assertThat(h.call(old.rawToken()).statusCode())
                .as("no rotation-induced 401 inside the grace window").isEqualTo(200);

            // At/after grace expiry: old 401s; new is unaffected (it has no expiry).
            clock.set(t0.plusSeconds(graceSeconds + 1));
            assertThat(h.call(old.rawToken()).statusCode())
                .as("old token 401s after grace expiry").isEqualTo(401);
            assertThat(h.call(newRaw).statusCode())
                .as("new token still authorizes after grace expiry").isEqualTo(200);
        }
    }

    @Test
    void revocation_boundedByCacheTtl_immediateBeforeTtlOnlyAfter() throws Exception {
        Instant t0 = Instant.parse("2026-06-09T00:00:00Z");
        MutableClock clock = new MutableClock(t0);
        // Default-TTL cache: worst-case revocation latency is exactly DEFAULT_TTL.
        long ttlSeconds = TokenCache.DEFAULT_TTL.toSeconds();
        try (ClockHarness h = new ClockHarness(clock)) {
            String tenant = "rev-tenant-" + System.nanoTime();
            TokenStore.IssuedToken tok = h.store.issueToken(tenant, "revoke-ttl", null);

            // Warm the cache at T0.
            assertThat(h.call(tok.rawToken()).statusCode()).isEqualTo(200);

            // Revoke out-of-band WITHOUT invalidating the cache (e.g. a direct DB edit).
            try (Connection su = pg.getPostgresDatabase().getConnection()) {
                su.setAutoCommit(true);
                try (var ps = su.prepareStatement(
                    "UPDATE nexus.service_tokens SET revoked_at = now() WHERE token_hash = ?")) {
                    ps.setString(1, tok.tokenHash());
                    assertThat(ps.executeUpdate()).isEqualTo(1);
                }
            }

            // One second before the TTL bound: still served stale (200).
            clock.set(t0.plusSeconds(ttlSeconds - 1));
            assertThat(h.call(tok.rawToken()).statusCode())
                .as("within the cache TTL the revoked token still resolves (stale)").isEqualTo(200);

            // One second past the TTL bound: cache entry ages out, DB re-read sees revocation.
            clock.set(t0.plusSeconds(ttlSeconds + 1));
            assertThat(h.call(tok.rawToken()).statusCode())
                .as("past the cache TTL the revocation takes effect → 401").isEqualTo(401);
        }
    }

    // ── Full-stack HTTP helpers (NexusService, RLS role) ──────────────────────

    private String svcBase() {
        return "http://127.0.0.1:" + service.getPort();
    }

    private HttpResponse<String> svcGet(String path, String bearer, String tenantHeader) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(svcBase() + path))
            .header("Authorization", "Bearer " + bearer)
            .header("X-Nexus-Tenant", tenantHeader)
            .GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> sessionPost(String path, String bearer, String rawSessionToken,
                                             String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(svcBase() + path))
            .header("Authorization", "Bearer " + bearer)
            .header("X-Nexus-T1-Session", rawSessionToken)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    // ── Superuser seeding + counts (bypass RLS for non-vacuous negatives) ─────

    private void seedMemoryRow(String tenant, String project, String title, String content) throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            try (var ps = su.prepareStatement(
                "INSERT INTO nexus.memory (tenant_id, project, title, content, timestamp) "
                + "VALUES (?, ?, ?, ?, now())")) {
                ps.setString(1, tenant);
                ps.setString(2, project);
                ps.setString(3, title);
                ps.setString(4, content);
                ps.executeUpdate();
            }
        }
    }

    private int superuserMemoryCount(String tenant, String project) throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection();
             var ps = su.prepareStatement(
                 "SELECT COUNT(*) AS c FROM nexus.memory WHERE tenant_id = ? AND project = ?")) {
            ps.setString(1, tenant);
            ps.setString(2, project);
            ResultSet rs = ps.executeQuery();
            rs.next();
            return rs.getInt("c");
        }
    }

    private void seedScratchRow(String id, String tenant, String session, String content) throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            try (var ps = su.prepareStatement(
                "INSERT INTO t1.scratch (id, tenant_id, session_id, content) VALUES (?, ?, ?, ?)")) {
                ps.setString(1, id);
                ps.setString(2, tenant);
                ps.setString(3, session);
                ps.setString(4, content);
                ps.executeUpdate();
            }
        }
    }

    private int superuserScratchCount(String tenant, String session) throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection();
             var ps = su.prepareStatement(
                 "SELECT COUNT(*) AS c FROM t1.scratch WHERE tenant_id = ? AND session_id = ?")) {
            ps.setString(1, tenant);
            ps.setString(2, session);
            ResultSet rs = ps.executeQuery();
            rs.next();
            return rs.getInt("c");
        }
    }

    private static void insertServiceToken(Connection su, String raw, String tenant,
                                           OffsetDateTime expiresAt, OffsetDateTime revokedAt) throws Exception {
        try (var ps = su.prepareStatement(
            "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label, expires_at, revoked_at) "
            + "VALUES (?, ?, ?, ?, ?) ON CONFLICT (token_hash) DO NOTHING")) {
            ps.setString(1, TokenHashing.sha256Hex(raw));
            ps.setString(2, tenant);
            ps.setString(3, "adv-test");
            if (expiresAt == null) ps.setNull(4, java.sql.Types.TIMESTAMP_WITH_TIMEZONE); else ps.setObject(4, expiresAt);
            if (revokedAt == null) ps.setNull(5, java.sql.Types.TIMESTAMP_WITH_TIMEZONE); else ps.setObject(5, revokedAt);
            ps.executeUpdate();
        }
    }

    private static void insertSessionToken(Connection su, String raw, String tenant,
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

    /** {@code now()} minus {@code seconds}, resolved by the DB clock (for fixtures). */
    private static OffsetDateTime ago(Connection su, long seconds) throws Exception {
        return offsetNow(su).minusSeconds(seconds);
    }

    /** {@code now()} plus {@code seconds}, resolved by the DB clock (for fixtures). */
    private static OffsetDateTime ahead(Connection su, long seconds) throws Exception {
        return offsetNow(su).plusSeconds(seconds);
    }

    private static OffsetDateTime offsetNow(Connection su) throws Exception {
        try (ResultSet rs = su.createStatement().executeQuery("SELECT now()")) {
            rs.next();
            return rs.getObject(1, OffsetDateTime.class);
        }
    }

    /** Minimal flat JSON builder (string values only). */
    private static String json(String... kv) {
        StringBuilder sb = new StringBuilder("{");
        for (int i = 0; i < kv.length; i += 2) {
            if (i > 0) sb.append(",");
            sb.append("\"").append(kv[i]).append("\":\"").append(kv[i + 1]).append("\"");
        }
        return sb.append("}").toString();
    }

    // ── Deterministic-clock AuthFilter harness (classes 3 + revocation TTL) ───

    /**
     * A throwaway {@link HttpServer} wired exactly like production ({@link AuthFilter}
     * over a {@link TokenStore} + {@link TokenCache}) but on a {@link MutableClock}, so
     * grace/TTL crossings are deterministic. Uses the superuser DataSource (the credential
     * tables are RLS-off; the store also performs token writes for rotation/issue). A
     * {@code 200} means the filter authorized and the {@link EchoHandler} ran; {@code 401}
     * means the filter rejected.
     */
    private final class ClockHarness implements AutoCloseable {
        final TokenStore store;
        final TokenCache cache;
        final HttpServer server;
        final int port;

        ClockHarness(Clock clock) throws IOException {
            this.store  = new TokenStore(pg.getPostgresDatabase(), clock);
            this.cache  = new TokenCache(store, clock);
            this.server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            var ctx = server.createContext("/v1/echo", new EchoHandler());
            ctx.getFilters().add(new AuthFilter(cache, store));
            server.start();
            this.port = server.getAddress().getPort();
        }

        HttpResponse<String> call(String bearer) throws Exception {
            HttpRequest req = HttpRequest.newBuilder(
                URI.create("http://127.0.0.1:" + port + "/v1/echo"))
                .header("Authorization", "Bearer " + bearer).GET().build();
            return http.send(req, HttpResponse.BodyHandlers.ofString());
        }

        @Override public void close() {
            server.stop(0);
        }
    }

    /** Echoes the AuthFilter-resolved principal; a reached 200 proves authorization. */
    static final class EchoHandler implements HttpHandler {
        @Override public void handle(HttpExchange ex) throws IOException {
            String body = "tenant=" + nullToEmpty(RequestContext.tenant())
                + ";session=" + nullToEmpty(RequestContext.session());
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            ex.sendResponseHeaders(200, bytes.length);
            try (OutputStream os = ex.getResponseBody()) {
                os.write(bytes);
            }
        }
        private static String nullToEmpty(String s) { return s == null ? "" : s; }
    }

    /** A {@link Clock} whose instant can be advanced for deterministic grace/TTL tests. */
    static final class MutableClock extends Clock {
        private volatile Instant instant;
        MutableClock(Instant instant) { this.instant = instant; }
        void set(Instant instant) { this.instant = instant; }
        @Override public ZoneId getZone() { return ZoneOffset.UTC; }
        @Override public Clock withZone(ZoneId zone) { return this; }
        @Override public Instant instant() { return instant; }
    }
}
