package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.LadderRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-186 bead nexus-146xx.12 (engine half) — ladder-completion HTTP endpoints.
 *
 * <p>Routes (all under {@code /v1/ladder/}):
 * <pre>
 *   POST /v1/ladder/record       record one rung's verified completion
 *                                {rung_name, package_version, detail?}
 *                                → {recorded: true} (upsert on rung_name;
 *                                verified_at stamped server-side)
 *   GET  /v1/ladder/completions  every completion fact for the tenant →
 *                                {completions: [{rung_name, verified_at,
 *                                package_version, detail}]}
 * </pre>
 *
 * <p>NO position surface, ever: facts only — ladder position is DERIVED
 * client-side ({@code derive_ladder_position}, the single Gap-4 mechanism-1
 * algorithm). Adding a position/ordering field here would create the second
 * data authority {@code test_gap4_two_mechanisms.py} pins against.
 *
 * <p>All endpoints require {@code Authorization: Bearer} (enforced by
 * {@link AuthFilter}) and {@code X-Nexus-Tenant}.
 */
public final class LadderHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(LadderHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final LadderRepository repo;

    public LadderHandler(LadderRepository repo) {
        this.repo = repo;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }

        String path   = exchange.getRequestURI().getPath();
        String op     = path.replaceFirst("^/v1/ladder", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                case "/record"      -> handleRecord(exchange, tenant, method);
                case "/completions" -> handleCompletions(exchange, tenant, method);
                default             -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "ladder_handler",
                    "op=" + op + " tenant=" + tenant)) {
                log.error("event=ladder_handler_error op={} tenant={} error={}",
                        op, tenant, e.getMessage(), e);
                HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    // ── POST /v1/ladder/record ───────────────────────────────────────────────

    private void handleRecord(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String rungName = requireString(body, "rung_name");
        String packageVersion = requireString(body, "package_version");
        String detail = body.get("detail") instanceof String s ? s : "";

        repo.record(tenant, rungName, packageVersion, detail);
        HttpUtil.send(exchange, 200, "{\"recorded\":true}");
    }

    // ── GET /v1/ladder/completions ───────────────────────────────────────────

    private void handleCompletions(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        HttpUtil.send(exchange, 200,
                MAPPER.writeValueAsString(Map.of("completions", repo.completions(tenant))));
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private static String requireString(Map<String, Object> body, String field) {
        Object v = body.get(field);
        if (!(v instanceof String s) || s.isBlank()) {
            throw new IllegalArgumentException("'" + field + "' is required");
        }
        return s;
    }

    private Map<String, Object> readBody(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            if (bytes.length == 0) throw new IllegalArgumentException("request body is required");
            return MAPPER.readValue(bytes, MAP_TYPE);
        }
    }
}
