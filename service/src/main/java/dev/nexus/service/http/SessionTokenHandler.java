package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TokenStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-152 bead nexus-gmiaf.32.4 — per-session token mint/close. The consumer is the MCP
 * lifespan (mint on session start, close on session end). All SQL lives in {@link TokenStore}.
 *
 * <p>Unlike {@link TokenAdminHandler}, the tenant is taken from the AUTHENTICATED bearer
 * ({@link RequestContext#tenant()}), not a body field: a client mints a session token for
 * its OWN tenant. The session_id comes from the body.
 *
 * <p>Routes (POST, behind {@link AuthFilter}):
 * <ul>
 *   <li>{@code /v1/sessions/start} {session_id, ttl_seconds?} → {session_token, session_id,
 *       expires_in_seconds}. The raw token is returned ONCE (the client sets it into
 *       NX_T1_SESSION); only its hash is stored.</li>
 *   <li>{@code /v1/sessions/close} {session_id} → {closed: <count>}.</li>
 * </ul>
 */
public final class SessionTokenHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(SessionTokenHandler.class);

    private static final ObjectMapper MAPPER = new ObjectMapper()
        .registerModule(new JavaTimeModule())
        .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
        .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
        .setSerializationInclusion(JsonInclude.Include.ALWAYS);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    /** Default session-token lifetime: long enough not to expire mid-session, re-minted on
     *  each session start. Bounds how long a leaked session token is usable. NOTE: a session
     *  alive past the TTL without a re-mint loses minted-enforcement — its token stops
     *  resolving and the client degrades to the transitional bootstrap path (body session_id
     *  trusted) until Phase E's require-minted flag (nexus-gmiaf.32.5) makes that a 401. */
    static final long DEFAULT_TTL_SECONDS = 86_400L;  // 24h

    private final TokenStore store;

    public SessionTokenHandler(TokenStore store) {
        this.store = store;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
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
                case "/v1/sessions/start" -> handleStart(exchange, tenant);
                case "/v1/sessions/close" -> handleClose(exchange, tenant);
                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            log.debug("event=session_token_bad_request path={} error={}", path, e.getMessage());
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            log.error("event=session_token_error path={}", path, e);
            HttpUtil.send(exchange, 500, json(Map.of("error", "internal server error")));
        }
    }

    private void handleStart(HttpExchange ex, String tenant) throws IOException {
        Map<String, Object> body = readBody(ex);
        String sessionId = requireString(body, "session_id");
        long ttl = optLong(body, "ttl_seconds", DEFAULT_TTL_SECONDS);
        TokenStore.IssuedToken issued = store.issueSessionToken(tenant, sessionId, ttl);
        HttpUtil.send(ex, 200, json(new LinkedHashMap<>(Map.of(
            "session_token", issued.rawToken(),
            "session_id", sessionId,
            "expires_in_seconds", ttl))));
    }

    private void handleClose(HttpExchange ex, String tenant) throws IOException {
        Map<String, Object> body = readBody(ex);
        String sessionId = requireString(body, "session_id");
        int closed = store.closeSession(tenant, sessionId);
        HttpUtil.send(ex, 200, json(Map.of("closed", closed)));
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

    private static long optLong(Map<String, Object> body, String key, long dflt) {
        Object v = body.get(key);
        if (v == null) {
            return dflt;
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
