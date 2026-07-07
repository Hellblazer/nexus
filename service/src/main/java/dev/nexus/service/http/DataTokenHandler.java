package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TokenStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

/**
 * nexus-x1h07 — {@code POST /v1/data-tokens/mint}: short-TTL per-tenant DATA
 * tokens minted just-in-time by a {@code scope=mint} credential (conexus RDR-005
 * option A1). Shrinks the edge's data-path blast radius from "every tenant,
 * forever" (the bulk {@code tenant_engine_token} read) to "one tenant, one TTL
 * window".
 *
 * <p>The VERBATIM pinned contract (T2
 * {@code conexus/conexus-to-nexus-rdr005-x1h07-pins-2026-06-26} — deviations go
 * through the bus BEFORE code lands):
 * <ul>
 *   <li>{@code {tenant, ttl_seconds?} → {data_token, expires_in_seconds}}</li>
 *   <li>TTL default {@value #DEFAULT_TTL_SECONDS}s; ceiling 3600s, env-overridable
 *       ({@code NX_DATA_TOKEN_TTL_CEILING_SECONDS}); over-ceiling = HTTP 400,
 *       NEVER silently clamped.</li>
 *   <li>Body-tenant; CROSS-tenant mint allowed (the edge mints for whichever
 *       tenant's request it is proxying).</li>
 *   <li>Only {@code scope=mint} callers are admitted — not tenant tokens, not
 *       data tokens (no self-replication), not even root (privilege separation;
 *       the operator issues a mint credential first). The {@link AuthFilter}
 *       additionally confines mint credentials TO this surface.</li>
 *   <li>Rate-limited per (credential, tenant) + per-credential global
 *       ({@link MintRateLimiter}) → HTTP 429. Validation failures (400/403) are
 *       checked BEFORE the rate-limit debit so malformed requests cannot burn
 *       the caller's budget.</li>
 *   <li>Revocation: revoke the mint credential ({@code /v1/service-tokens/revoke})
 *       → mints stop immediately; outstanding data tokens drain on their own TTL
 *       (each is an independent {@code service_tokens} row). Per-tenant bulk
 *       revoke is deliberately v2 (pin iii).</li>
 * </ul>
 */
public final class DataTokenHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(DataTokenHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    /** Pin: default data-token lifetime. Deliberately NOT env-tunable — the body's
     *  {@code ttl_seconds} covers every within-ceiling need. */
    static final long DEFAULT_TTL_SECONDS = 300L;
    /** Pin: default ceiling; env-overridable via {@code NX_DATA_TOKEN_TTL_CEILING_SECONDS}. */
    static final long DEFAULT_TTL_CEILING_SECONDS = 3600L;

    private final TokenStore store;
    private final MintRateLimiter rateLimiter;
    private final long ttlCeilingSeconds;

    public DataTokenHandler(TokenStore store, MintRateLimiter rateLimiter, long ttlCeilingSeconds) {
        this.store = store;
        this.rateLimiter = rateLimiter;
        this.ttlCeilingSeconds = ttlCeilingSeconds;
    }

    /** Production factory: ceiling + rate bounds from env. */
    public static DataTokenHandler fromEnv(TokenStore store, Clock clock) {
        return new DataTokenHandler(store, MintRateLimiter.fromEnv(clock), ttlCeilingFromEnv());
    }

    /** The env-resolved data-token TTL ceiling — shared with the session-mint
     *  guard for data-scoped callers ({@link SessionTokenHandler}). */
    static long ttlCeilingFromEnv() {
        String raw = System.getenv("NX_DATA_TOKEN_TTL_CEILING_SECONDS");
        long ceiling = DEFAULT_TTL_CEILING_SECONDS;
        if (raw != null && !raw.isBlank()) {
            try {
                ceiling = Long.parseLong(raw.trim());
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(
                    "NX_DATA_TOKEN_TTL_CEILING_SECONDS must be an integer, got: " + raw, e);
            }
            if (ceiling <= 0) {
                throw new IllegalArgumentException(
                    "NX_DATA_TOKEN_TTL_CEILING_SECONDS must be positive, got: " + ceiling);
            }
        }
        return ceiling;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String path = exchange.getRequestURI().getPath();
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);
        if (!"/v1/data-tokens/mint".equals(path)) {
            HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            return;
        }
        if (!"POST".equals(method)) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        try {
            handleMint(exchange);
        } catch (IllegalArgumentException e) {
            log.debug("event=data_token_bad_request error={}", e.getMessage());
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "data_token", "path=" + path)) {
                log.error("event=data_token_error", e);
                HttpUtil.send(exchange, 500, json(Map.of("error", "internal server error")));
            }
        }
    }

    private void handleMint(HttpExchange ex) throws IOException {
        // ONLY a mint-scoped credential may mint (plan-author decision 4: root is
        // deliberately excluded — the operator issues itself a mint credential).
        if (!TokenStore.SCOPE_MINT.equals(RequestContext.scope())) {
            log.debug("event=data_token_denied reason=mint_scope_required scope={}",
                      RequestContext.scope());
            HttpUtil.send(ex, 403, json(Map.of(
                "error", "forbidden: data-token mint requires a 'mint'-scoped credential")));
            return;
        }

        Map<String, Object> body = readBody(ex);
        Object tenantRaw = body.get("tenant");
        if (!(tenantRaw instanceof String tenant) || tenant.isBlank()) {
            throw new IllegalArgumentException("missing required string field: tenant");
        }
        // Gate-B review M1: EVERY validation precedes the rate-limit debit — a
        // knowingly-invalid tenant must neither consume budget nor grow a bucket
        // (it compounds the map-bound concern). The wildcard check duplicates
        // TokenStore.rejectWildcard deliberately: the store's copy fires after
        // the debit, too late for the budget invariant.
        if (dev.nexus.service.db.TenantConstants.BOOTSTRAP_ANY_TENANT.equals(tenant)) {
            throw new IllegalArgumentException(
                "tenant '*' is a reserved sentinel and cannot be used");
        }
        long ttlSeconds = DEFAULT_TTL_SECONDS;
        Object ttlRaw = body.get("ttl_seconds");
        if (ttlRaw != null) {
            // Gate-B review M3: integral JSON types only. Number.longValue() would
            // silently truncate a fraction (300.9 -> 300) or wrap an out-of-range
            // BigInteger into an unrelated in-range value — no silent coercion on
            // a correctness-bearing field.
            if (!(ttlRaw instanceof Integer) && !(ttlRaw instanceof Long)) {
                throw new IllegalArgumentException(
                    "ttl_seconds must be an integral number of seconds, got: " + ttlRaw);
            }
            ttlSeconds = ((Number) ttlRaw).longValue();
        }
        if (ttlSeconds <= 0) {
            throw new IllegalArgumentException("ttl_seconds must be positive");
        }
        if (ttlSeconds > ttlCeilingSeconds) {
            // Pin: over-ceiling is a 400, never a silent clamp.
            throw new IllegalArgumentException(
                "ttl_seconds " + ttlSeconds + " exceeds the ceiling of " + ttlCeilingSeconds
                + " (NX_DATA_TOKEN_TTL_CEILING_SECONDS)");
        }

        // Rate limit LAST among the checks: a malformed request must not debit
        // the caller's budget (Gate-B precedence: 403 scope → 400 validation → 429).
        String credentialHash = RequestContext.credentialHash();
        if (!rateLimiter.tryAcquire(credentialHash, tenant)) {
            log.warn("event=data_token_rate_limited tenant={} credential_prefix={}",
                     tenant, credentialHash == null ? "?"
                         : credentialHash.substring(0, Math.min(12, credentialHash.length())));
            // Retry-After: the per-tenant sustained refill period, COMPUTED from
            // the limiter's (env-tunable) rate so a conexus retune stays honest
            // (nexus-ox1as). The edge's legitimate cadence (~1 mint per TTL per
            // tenant) never sees this; a bursty client learns when to come back.
            long retryAfter = Math.max(1, 60 / rateLimiter.tenantSustainedPerMinute());
            ex.getResponseHeaders().set("Retry-After", Long.toString(retryAfter));
            HttpUtil.send(ex, 429, json(Map.of("error", "rate limit exceeded, retry later")));
            return;
        }

        // issueToken validates the wildcard sentinel + inserts the scope='data' row
        // with expires_at = now + ttl. The data token is its OWN registry row:
        // revoking the mint credential stops future mints but never touches it.
        TokenStore.IssuedToken issued =
            store.issueToken(tenant, "data-token", ttlSeconds, TokenStore.SCOPE_DATA);
        log.info("event=data_token_minted tenant={} ttl_s={} minted_by_prefix={}",
                 tenant, ttlSeconds, credentialHash == null ? "?"
                     : credentialHash.substring(0, Math.min(12, credentialHash.length())));
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("data_token", issued.rawToken());
        resp.put("expires_in_seconds", ttlSeconds);
        HttpUtil.send(ex, 200, json(resp));
    }

    private Map<String, Object> readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        if (bytes.length == 0) {
            return Map.of();
        }
        try {
            return MAPPER.readValue(new String(bytes, StandardCharsets.UTF_8), MAP_TYPE);
        } catch (com.fasterxml.jackson.core.JacksonException e) {
            throw new IllegalArgumentException("invalid JSON body: " + e.getOriginalMessage());
        }
    }

    private static String json(Map<String, ?> value) {
        try {
            return MAPPER.writeValueAsString(value);
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new IllegalStateException("JSON serialization failed", e);
        }
    }
}
