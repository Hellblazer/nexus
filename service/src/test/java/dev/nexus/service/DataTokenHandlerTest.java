package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenHashing;
import org.testcontainers.containers.PostgreSQLContainer;
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

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.sql.ResultSet;
import java.time.Instant;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-x1h07 Phase 4 — {@code POST /v1/data-tokens/mint} against the real stack.
 *
 * <p>The verbatim contract (conexus RDR-005 pins, T2
 * {@code conexus/conexus-to-nexus-rdr005-x1h07-pins-2026-06-26}):
 * {@code {tenant, ttl_seconds?} -> {data_token, expires_in_seconds}}; TTL default
 * 300s, ceiling 3600s env-overridable, over-ceiling HTTP 400 (never clamped);
 * the mint credential is body-tenant with CROSS-TENANT mint allowed; rejected on
 * ALL admin routes; revoke-the-mint-credential stops mints while outstanding data
 * tokens drain by their own TTL.
 *
 * <p>Hermetic: Testcontainers Postgres + full Liquibase + real {@link NexusService}
 * (system clock — TTLs asserted from the response contract, not by waiting).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class DataTokenHandlerTest {

    private static final String BOOT = "boot-data-token-test";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    NexusService service;
    int port;
    final HttpClient http = HttpClient.newHttpClient();

    String mintRaw;   // a scope=mint credential issued by the operator in @BeforeAll

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (var ps = su.prepareStatement(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label, scope) VALUES (?, ?, ?, ?) "
                + "ON CONFLICT (token_hash) DO NOTHING")) {
                ps.setString(1, TokenHashing.sha256Hex(BOOT));
                ps.setString(2, TenantConstants.DEFAULT_TENANT);
                ps.setString(3, dev.nexus.service.db.TokenStore.ROOT_TOKEN_LABEL);
                ps.setString(4, dev.nexus.service.db.TokenStore.SCOPE_ROOT);
                ps.executeUpdate();
            }
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        cfg.setConnectionInitSql("SET search_path TO nexus, t1, public");
        ds = new HikariDataSource(cfg);

        service = new NexusService(0, BOOT, ds);
        service.start();
        port = service.getPort();

        // Operator issues the edge's mint-scoped credential (the 868dq surface).
        JsonNode issued = postJsonAs(BOOT, "/v1/service-tokens/issue",
            "{\"tenant\":\"conexus-edge\",\"label\":\"edge-mint\",\"scope\":\"mint\"}");
        mintRaw = issued.get("token").asText();
        assertThat(mintRaw).isNotBlank();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── The pinned contract ───────────────────────────────────────────────────

    @Test
    void mint_defaultTtl300_returnsWorkingDataToken() throws Exception {
        var resp = sendAs(mintRaw, "/v1/data-tokens/mint", "{\"tenant\":\"acme\"}");
        assertThat(resp.statusCode()).as("mint must succeed: %s", resp.body()).isEqualTo(200);
        JsonNode body = MAPPER.readTree(resp.body());
        String dataToken = body.get("data_token").asText();
        assertThat(dataToken).isNotBlank();
        assertThat(body.get("expires_in_seconds").asLong()).isEqualTo(300L);

        // The minted token is a REAL bearer with data-path authority for acme.
        assertThat(whoami(dataToken)).isEqualTo(200);
        assertThat(scopeOfRaw(dataToken)).isEqualTo("data");
        assertThat(tenantOfRaw(dataToken)).isEqualTo("acme");
    }

    @Test
    void mint_crossTenant_allowed() throws Exception {
        // The mint credential is bound to 'conexus-edge'; minting for a DIFFERENT
        // tenant is the whole point (pin i: body-tenant, cross-tenant allowed).
        var resp = sendAs(mintRaw, "/v1/data-tokens/mint", "{\"tenant\":\"other-tenant\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(tenantOfRaw(MAPPER.readTree(resp.body()).get("data_token").asText()))
            .isEqualTo("other-tenant");
    }

    @Test
    void mint_explicitTtl_honored() throws Exception {
        var resp = sendAs(mintRaw, "/v1/data-tokens/mint",
            "{\"tenant\":\"acme\",\"ttl_seconds\":1800}");
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(MAPPER.readTree(resp.body()).get("expires_in_seconds").asLong())
            .isEqualTo(1800L);
    }

    @Test
    void mint_overCeiling_is400_neverClamped() throws Exception {
        // Pin: ceiling 3600s (env-overridable); over-ceiling = HTTP 400, NO silent clamp.
        var resp = sendAs(mintRaw, "/v1/data-tokens/mint",
            "{\"tenant\":\"acme\",\"ttl_seconds\":7200}");
        assertThat(resp.statusCode()).isEqualTo(400);
    }

    @Test
    void mint_nonPositiveTtl_is400() throws Exception {
        assertThat(sendAs(mintRaw, "/v1/data-tokens/mint",
            "{\"tenant\":\"acme\",\"ttl_seconds\":0}").statusCode()).isEqualTo(400);
        assertThat(sendAs(mintRaw, "/v1/data-tokens/mint",
            "{\"tenant\":\"acme\",\"ttl_seconds\":-5}").statusCode()).isEqualTo(400);
    }

    @Test
    void mint_missingTenant_is400_wildcard_is400() throws Exception {
        assertThat(sendAs(mintRaw, "/v1/data-tokens/mint", "{}").statusCode()).isEqualTo(400);
        assertThat(sendAs(mintRaw, "/v1/data-tokens/mint",
            "{\"tenant\":\"*\"}").statusCode()).isEqualTo(400);
    }

    @Test
    void mint_fractionalOrHugeTtl_is400_noSilentCoercion() throws Exception {
        // Gate-B review M3: Number.longValue() would truncate 300.9 to 300 or
        // wrap an out-of-long BigInteger — integral JSON types only.
        assertThat(sendAs(mintRaw, "/v1/data-tokens/mint",
            "{\"tenant\":\"acme\",\"ttl_seconds\":300.9}").statusCode()).isEqualTo(400);
        assertThat(sendAs(mintRaw, "/v1/data-tokens/mint",
            "{\"tenant\":\"acme\",\"ttl_seconds\":99999999999999999999999999}").statusCode())
            .isEqualTo(400);
        assertThat(sendAs(mintRaw, "/v1/data-tokens/mint",
            "{\"tenant\":\"acme\",\"ttl_seconds\":\"300\"}").statusCode()).isEqualTo(400);
    }

    @Test
    void mint_onlyMintScope_admitted() throws Exception {
        // Plan-author decision 4 (design item 4 read literally): ONLY scope=mint
        // may mint — a plain tenant token, a data token, and even the ROOT bearer
        // are all 403.
        JsonNode tenantTok = postJsonAs(BOOT, "/v1/service-tokens/issue",
            "{\"tenant\":\"acme\"}");
        assertThat(sendAs(tenantTok.get("token").asText(),
            "/v1/data-tokens/mint", "{\"tenant\":\"acme\"}").statusCode()).isEqualTo(403);

        var minted = sendAs(mintRaw, "/v1/data-tokens/mint", "{\"tenant\":\"acme\"}");
        String dataToken = MAPPER.readTree(minted.body()).get("data_token").asText();
        assertThat(sendAs(dataToken, "/v1/data-tokens/mint",
            "{\"tenant\":\"acme\"}").statusCode()).isEqualTo(403);

        assertThat(sendAs(BOOT, "/v1/data-tokens/mint",
            "{\"tenant\":\"acme\"}").statusCode()).isEqualTo(403);
    }

    @Test
    void mint_nonPost_is405_unknownPath_is404() throws Exception {
        var get = HttpRequest.newBuilder(URI.create(base() + "/v1/data-tokens/mint"))
            .header("Authorization", "Bearer " + mintRaw).GET().build();
        assertThat(http.send(get, HttpResponse.BodyHandlers.ofString()).statusCode())
            .isEqualTo(405);
        assertThat(sendAs(mintRaw, "/v1/data-tokens/unknown", "{}").statusCode())
            .isEqualTo(404);
    }

    // ── nexus-t8abd: data-scoped session mint stays within the data window ────

    @Test
    void dataToken_sessionMint_cappedAtDataCeiling() throws Exception {
        var minted = sendAs(mintRaw, "/v1/data-tokens/mint", "{\"tenant\":\"sess-tenant\"}");
        String dataToken = MAPPER.readTree(minted.body()).get("data_token").asText();

        // Default: the session TTL for a data-scoped caller is the data ceiling
        // (3600), NOT the endpoint's ordinary 24h default.
        var start = sendAs(dataToken, "/v1/sessions/start", "{\"session_id\":\"s-data-1\"}");
        assertThat(start.statusCode()).as(start.body()).isEqualTo(200);
        assertThat(MAPPER.readTree(start.body()).get("expires_in_seconds").asLong())
            .isEqualTo(3600L);

        // An explicit request beyond the ceiling is a 400, never a silent clamp.
        assertThat(sendAs(dataToken, "/v1/sessions/start",
            "{\"session_id\":\"s-data-2\",\"ttl_seconds\":86400}").statusCode())
            .isEqualTo(400);

        // Ordinary bearers keep the exact prior default (24h).
        JsonNode tenantTok = postJsonAs(BOOT, "/v1/service-tokens/issue",
            "{\"tenant\":\"sess-tenant\"}");
        var plain = sendAs(tenantTok.get("token").asText(),
            "/v1/sessions/start", "{\"session_id\":\"s-plain-1\"}");
        assertThat(plain.statusCode()).isEqualTo(200);
        assertThat(MAPPER.readTree(plain.body()).get("expires_in_seconds").asLong())
            .isEqualTo(86_400L);
    }

    // ── Task 4.3: the revocation guarantee (design item 8 / pin iii) ─────────

    @Test
    void revokeMintCredential_stopsMints_outstandingDataTokensDrain() throws Exception {
        // A dedicated mint credential so revoking it cannot poison the other tests.
        JsonNode issued = postJsonAs(BOOT, "/v1/service-tokens/issue",
            "{\"tenant\":\"conexus-edge\",\"label\":\"edge-mint-revocable\",\"scope\":\"mint\"}");
        String revocableMint = issued.get("token").asText();
        String revocableHash = issued.get("token_hash").asText();

        // Mint a data token with it.
        var minted = sendAs(revocableMint, "/v1/data-tokens/mint", "{\"tenant\":\"drain-tenant\"}");
        assertThat(minted.statusCode()).isEqualTo(200);
        String dataToken = MAPPER.readTree(minted.body()).get("data_token").asText();
        assertThat(whoami(dataToken)).isEqualTo(200);

        // Operator revokes the mint credential (the kill switch).
        var revoke = sendAs(BOOT, "/v1/service-tokens/revoke",
            "{\"selector\":\"" + revocableHash + "\"}");
        assertThat(revoke.statusCode()).isEqualTo(200);
        assertThat(MAPPER.readTree(revoke.body()).get("revoked").asBoolean()).isTrue();

        // (a) Mints STOP: the revoked credential is 401 on its only surface.
        assertThat(sendAs(revocableMint, "/v1/data-tokens/mint",
            "{\"tenant\":\"drain-tenant\"}").statusCode())
            .as("revoked mint credential must no longer mint")
            .isEqualTo(401);

        // (b) The OUTSTANDING data token DRAINS on its own TTL: it is its own
        // service_tokens row, independent of its minting credential's lifecycle.
        assertThat(whoami(dataToken))
            .as("already-minted data token must survive the mint credential's revocation")
            .isEqualTo(200);
    }

    // ── Task 5.3: rate limit over HTTP (small injected bounds, mutable clock) ─

    @Test
    void mint_rateLimited_429_thenRefills() throws Exception {
        // A dedicated server with an explicit tiny limiter (burst 2, 1/min) so the
        // 429 path is exercised over real HTTP without widening NexusService's
        // constructor. Same live TokenStore/DB as the main service.
        var clock = new MutableClock(Instant.parse("2026-07-07T00:00:00Z"));
        var store = new dev.nexus.service.db.TokenStore(ds, clock);
        var cache = new dev.nexus.service.db.TokenCache(store, clock);
        var limiter = new dev.nexus.service.http.MintRateLimiter(clock, 2, 1, 100);
        var handler = new dev.nexus.service.http.DataTokenHandler(store, limiter, 3600L);
        var server = com.sun.net.httpserver.HttpServer.create(
            new java.net.InetSocketAddress("127.0.0.1", 0), 0);
        var ctx = server.createContext("/v1/data-tokens", handler);
        ctx.getFilters().add(new dev.nexus.service.http.AuthFilter(cache, store));
        server.start();
        int rlPort = server.getAddress().getPort();
        try {
            String rlMint = postJsonAs(BOOT, "/v1/service-tokens/issue",
                "{\"tenant\":\"conexus-edge\",\"label\":\"edge-mint-rl\",\"scope\":\"mint\"}")
                .get("token").asText();

            java.util.function.IntSupplier mint = () -> {
                try {
                    var req = HttpRequest.newBuilder(
                            URI.create("http://127.0.0.1:" + rlPort + "/v1/data-tokens/mint"))
                        .header("Authorization", "Bearer " + rlMint)
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString("{\"tenant\":\"rl-tenant\"}"))
                        .build();
                    return http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode();
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            };

            assertThat(mint.getAsInt()).isEqualTo(200);
            assertThat(mint.getAsInt()).isEqualTo(200);
            assertThat(mint.getAsInt()).as("3rd mint within burst window").isEqualTo(429);
            // Sustained 1/min: one token back after the refill window.
            clock.advance(60);
            assertThat(mint.getAsInt()).isEqualTo(200);
            assertThat(mint.getAsInt()).isEqualTo(429);

            // Malformed requests must be 400 (validation precedes the debit) and
            // must NOT consume budget once refilled — both the over-ceiling TTL
            // and the wildcard tenant (Gate-B M1: the wildcard previously debited
            // before TokenStore.rejectWildcard fired).
            clock.advance(60);
            for (String badBody : new String[] {
                    "{\"tenant\":\"rl-tenant\",\"ttl_seconds\":999999}",
                    "{\"tenant\":\"*\"}"}) {
                var bad = HttpRequest.newBuilder(
                        URI.create("http://127.0.0.1:" + rlPort + "/v1/data-tokens/mint"))
                    .header("Authorization", "Bearer " + rlMint)
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(badBody))
                    .build();
                assertThat(http.send(bad, HttpResponse.BodyHandlers.ofString()).statusCode())
                    .as("bad body %s", badBody)
                    .isEqualTo(400);
            }
            assertThat(mint.getAsInt())
                .as("the 400s must not have consumed the refilled budget")
                .isEqualTo(200);
        } finally {
            server.stop(0);
        }
    }

    /** Mutable clock — per-file fake, mirrors AuthFilterTest's pattern. */
    static final class MutableClock extends java.time.Clock {
        private volatile Instant instant;
        MutableClock(Instant instant) { this.instant = instant; }
        void advance(long seconds) { this.instant = this.instant.plusSeconds(seconds); }
        @Override public java.time.ZoneId getZone() { return java.time.ZoneOffset.UTC; }
        @Override public java.time.Clock withZone(java.time.ZoneId zone) { return this; }
        @Override public Instant instant() { return instant; }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private String base() {
        return "http://127.0.0.1:" + port;
    }

    private HttpResponse<String> sendAs(String bearer, String path, String body) throws Exception {
        var req = HttpRequest.newBuilder(URI.create(base() + path))
            .header("Authorization", "Bearer " + bearer)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode postJsonAs(String bearer, String path, String body) throws Exception {
        var resp = sendAs(bearer, path, body);
        assertThat(resp.statusCode()).as("POST %s -> %s", path, resp.body()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    private int whoami(String bearer) throws Exception {
        var req = HttpRequest.newBuilder(URI.create(base() + "/v1/_whoami"))
            .header("Authorization", "Bearer " + bearer).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode();
    }

    private String scopeOfRaw(String rawToken) throws Exception {
        return columnOfRaw(rawToken, "scope");
    }

    private String tenantOfRaw(String rawToken) throws Exception {
        return columnOfRaw(rawToken, "tenant_id");
    }

    private String columnOfRaw(String rawToken, String column) throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT " + column + " FROM nexus.service_tokens WHERE token_hash = '"
                + TokenHashing.sha256Hex(rawToken) + "'");
            assertThat(rs.next()).isTrue();
            return rs.getString(column);
        }
    }
}
