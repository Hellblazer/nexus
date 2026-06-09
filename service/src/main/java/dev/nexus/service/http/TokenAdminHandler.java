package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TokenCache;
import dev.nexus.service.db.TokenStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-152 bead nexus-gmiaf.32.3 — token lifecycle admin endpoints. The consumer is the
 * {@code nx tenant} / {@code nx service token} CLI; all SQL lives in {@link TokenStore}.
 *
 * <p>Routes (all POST, all behind {@link AuthFilter} so the caller is authenticated):
 * <ul>
 *   <li>{@code /v1/tenants/create}        {name}                     → mint a tenant's first token</li>
 *   <li>{@code /v1/service-tokens/issue}  {tenant,label?,ttl_seconds?} → issue a bound token</li>
 *   <li>{@code /v1/service-tokens/rotate} {tenant,grace_seconds?}     → zero-downtime overlap rotate</li>
 *   <li>{@code /v1/service-tokens/revoke} {selector}                  → revoke + invalidate cache</li>
 *   <li>{@code /v1/service-tokens/list}   {tenant?}                   → list rows (never plaintext)</li>
 * </ul>
 *
 * <p>The raw token is generated server-side and returned ONCE in the issue/rotate/create
 * response; only its hash is stored. {@code tenant_id='*'} is rejected (the wildcard is the
 * bootstrap sentinel — bead nexus-45ykb). Revoke calls {@link TokenCache#invalidate} on the
 * LIVE cache for immediate effect; rotate likewise invalidates the grace-expiring hashes so
 * their cache entries re-read the new deadline and expire precisely at the grace window's end
 * (not up to a cache-TTL later). Rotate is atomic (one transaction), so a crash cannot strand
 * a tenant with zero live tokens.
 *
 * <p>AUTHORIZATION NOTE (deferred): any authenticated caller may administer any named tenant
 * (provisioning rides the bootstrap token until per-tenant principals exist). Per-caller
 * admin scoping is out of scope for Phase C; the bootstrap token is the admin credential.
 */
public final class TokenAdminHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(TokenAdminHandler.class);

    private static final ObjectMapper MAPPER = new ObjectMapper()
        .registerModule(new JavaTimeModule())
        .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
        .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
        .setSerializationInclusion(JsonInclude.Include.NON_NULL);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};
    private static final long DEFAULT_GRACE_SECONDS = 300L;

    private final TokenStore store;
    private final TokenCache cache;
    private final Clock clock;

    public TokenAdminHandler(TokenStore store, TokenCache cache, Clock clock) {
        this.store = store;
        this.cache = cache;
        this.clock = clock;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        if (RequestContext.tenant() == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: auth principal not set\"}");
            return;
        }
        String path = exchange.getRequestURI().getPath();
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);
        if (!"POST".equals(method)) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            return;
        }
        try {
            switch (path) {
                case "/v1/tenants/create"        -> handleTenantCreate(exchange);
                case "/v1/service-tokens/issue"  -> handleIssue(exchange);
                case "/v1/service-tokens/rotate" -> handleRotate(exchange);
                case "/v1/service-tokens/revoke" -> handleRevoke(exchange);
                case "/v1/service-tokens/list"   -> handleList(exchange);
                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            log.debug("event=token_admin_bad_request path={} error={}", path, e.getMessage());
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            log.error("event=token_admin_error path={}", path, e);
            HttpUtil.send(exchange, 500, json(Map.of("error", "internal server error")));
        }
    }

    private void handleTenantCreate(HttpExchange ex) throws IOException {
        Map<String, Object> body = readBody(ex);
        String name = requireString(body, "name");
        TokenStore.IssuedToken issued = store.issueToken(name, "tenant-create-initial", null);
        HttpUtil.send(ex, 200, json(new LinkedHashMap<>(Map.of(
            "tenant", name, "token", issued.rawToken(), "token_hash", issued.tokenHash()))));
    }

    private void handleIssue(HttpExchange ex) throws IOException {
        Map<String, Object> body = readBody(ex);
        String tenant = requireString(body, "tenant");
        String label = optString(body, "label");
        Long ttl = optLong(body, "ttl_seconds");
        TokenStore.IssuedToken issued = store.issueToken(tenant, label, ttl);
        HttpUtil.send(ex, 200, json(new LinkedHashMap<>(Map.of(
            "tenant", tenant, "token", issued.rawToken(), "token_hash", issued.tokenHash()))));
    }

    private void handleRotate(HttpExchange ex) throws IOException {
        Map<String, Object> body = readBody(ex);
        String tenant = requireString(body, "tenant");
        Long grace = optLong(body, "grace_seconds");
        TokenStore.RotationResult result =
            store.rotateTokens(tenant, grace == null ? DEFAULT_GRACE_SECONDS : grace);
        // Invalidate the grace-expiring tokens so their cache entries re-read the new
        // deadline: they stay valid through the grace window then expire precisely at it.
        result.expiredHashes().forEach(cache::invalidate);
        TokenStore.IssuedToken issued = result.issued();
        HttpUtil.send(ex, 200, json(new LinkedHashMap<>(Map.of(
            "tenant", tenant, "token", issued.rawToken(), "token_hash", issued.tokenHash()))));
    }

    private void handleRevoke(HttpExchange ex) throws IOException {
        Map<String, Object> body = readBody(ex);
        String selector = requireString(body, "selector");
        var revokedHash = store.revokeToken(selector);
        revokedHash.ifPresent(cache::invalidate);  // immediate effect on the live cache
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("revoked", revokedHash.isPresent());
        revokedHash.ifPresent(h -> resp.put("token_hash", h));
        HttpUtil.send(ex, 200, json(resp));
    }

    private void handleList(HttpExchange ex) throws IOException {
        Map<String, Object> body = readBody(ex);
        String tenant = optString(body, "tenant");
        var now = clock.instant();
        List<Map<String, Object>> rows = new ArrayList<>();
        for (TokenStore.TokenInfo t : store.listTokens(tenant)) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("token_hash", t.tokenHash());
            row.put("tenant", t.tenantId());
            row.put("label", t.label());
            row.put("created_at", t.createdAt());
            row.put("expires_at", t.expiresAt());
            row.put("revoked_at", t.revokedAt());
            row.put("status", t.status(now));
            rows.add(row);
        }
        HttpUtil.send(ex, 200, json(Map.of("tokens", rows)));
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    private Map<String, Object> readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        if (bytes.length == 0) {
            return Map.of();
        }
        return MAPPER.readValue(new String(bytes, StandardCharsets.UTF_8), MAP_TYPE);
    }

    private static String requireString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (!(v instanceof String s) || s.isBlank()) {
            throw new IllegalArgumentException("missing required string field: " + key);
        }
        return s;
    }

    private static String optString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        return v instanceof String s && !s.isBlank() ? s : null;
    }

    private static Long optLong(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) {
            return null;
        }
        if (v instanceof Number n) {
            return n.longValue();
        }
        throw new IllegalArgumentException("field must be a number: " + key);
    }

    private static String json(Object value) {
        try {
            return MAPPER.writeValueAsString(value);
        } catch (Exception e) {
            return "{\"error\":\"serialization failed\"}";
        }
    }
}
