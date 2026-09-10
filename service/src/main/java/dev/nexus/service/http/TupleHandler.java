// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TupleException;
import dev.nexus.service.db.TupleRepository;
import dev.nexus.service.tuples.TemplateRegistry;
import dev.nexus.service.tuples.TemplateSchema;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;

/**
 * RDR-205 Phase 1 Step 4 (bead nexus-em75s.4) — the Linda tuple space's HTTP
 * surface.
 *
 * <p>Routes (all under {@code /v1/tuples/}), every one POST except the two
 * census reads which are GET (mirroring {@code AspectHandler}'s GET-for-
 * simple-reads / POST-for-everything-else convention — these two carry a
 * query-string parameter, not a JSON pattern body):
 * <pre>
 *   POST /v1/tuples/out             {subspace, keys, dims?, body?, nonce?, ttl_seconds?} -&gt; {"id": "&lt;hex&gt;"}
 *   POST /v1/tuples/rd              {subspace, keys_pattern?, n?, since?, timeout_s?} -&gt; {"tuples": [...]}
 *   POST /v1/tuples/rdp             {subspace, keys_pattern?, n?, since?} -&gt; {"tuples": [...]}
 *   POST /v1/tuples/in              {subspace, keys_pattern, claimant, lease_s, timeout_s?} -&gt; {"tuple": ..|null, "claim_id": ..|null}
 *   POST /v1/tuples/inp             {subspace, keys_pattern, claimant, lease_s} -&gt; same shape as /in
 *   POST /v1/tuples/ack             {claim_id, claimant} -&gt; {"acked": true}
 *   POST /v1/tuples/nack            {claim_id, claimant} -&gt; {"nacked": true}
 *   GET  /v1/tuples/registry        -&gt; {"digest", "sources", "templates": [...]}
 *   GET  /v1/tuples/subspace_list   ?prefix= -&gt; {"subspaces": [...]}
 *   GET  /v1/tuples/subspace_stats  ?subspace= -&gt; {subspace, total, available, claimed, dead, consumed, expired_unpurged, oldest_created_at, newest_created_at}
 * </pre>
 *
 * <p>A tuple's {@code id} and a {@code since} cursor's id half are lowercase
 * hex on the wire (the RDR-086 chunk-identity convention this repo already
 * uses at every other bytea-identity boundary); {@code keys}/{@code dims}
 * are plain JSON objects of string values.
 *
 * <p>Errors: the nine RDR-205 typed errors ({@link TupleException} and its
 * subtypes) are caught ahead of the generic ladder and rendered as {@code
 * {"error": "<code>", "detail": "<message>"}} at each exception's own
 * {@link TupleException#httpStatus()} — {@code ParkCapExceeded} additionally
 * carries the (always-empty, by construction — see its javadoc) probe-result
 * shape the endpoint would otherwise have returned. {@code
 * IllegalArgumentException} (malformed request body) maps to 400, same as
 * every other handler in this package; anything else falls through to
 * {@link HttpUtil#sendTypedDbError}.
 *
 * <p>All endpoints require {@code Authorization: Bearer <token>} and
 * {@code X-Nexus-Tenant}.
 */
public final class TupleHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(TupleHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
            .setSerializationInclusion(JsonInclude.Include.ALWAYS);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {
    };
    private static final HexFormat HEX = HexFormat.of();

    private final TupleRepository repo;

    public TupleHandler(TupleRepository repo) {
        this.repo = repo;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }

        String path = exchange.getRequestURI().getPath();
        String op = path.replaceFirst("^/v1/tuples", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                case "/out" -> handleOut(exchange, tenant, method);
                case "/rd" -> handleRd(exchange, tenant, method);
                case "/rdp" -> handleRdp(exchange, tenant, method);
                case "/in" -> handleIn(exchange, tenant, method);
                case "/inp" -> handleInp(exchange, tenant, method);
                case "/ack" -> handleAck(exchange, tenant, method);
                case "/nack" -> handleNack(exchange, tenant, method);
                case "/registry" -> handleRegistry(exchange, method);
                case "/subspace_list" -> handleSubspaceList(exchange, tenant, method);
                case "/subspace_stats" -> handleSubspaceStats(exchange, tenant, method);
                default -> HttpUtil.send(exchange, 404, "{\"error\":\"unknown tuples op: " + op + "\"}");
            }
        } catch (TupleException e) {
            log.warn("event=tuples_handler_typed_error op={} code={} detail={}", op, e.code(), e.getMessage());
            HttpUtil.send(exchange, e.httpStatus(),
                    "{\"error\":" + HttpUtil.jsonString(e.code())
                            + ",\"detail\":" + HttpUtil.jsonString(e.getMessage()) + "}");
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, "{\"error\":" + HttpUtil.jsonString(e.getMessage()) + "}");
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "tuples_handler", "op=" + op)) {
                log.error("event=tuples_handler_error op={} error={}", op, e.getMessage(), e);
                HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    // ── out ──────────────────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private void handleOut(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) {
            HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}");
            return;
        }
        Map<String, Object> body = readBody(ex);
        String subspace = requireString(body, "subspace");
        Map<String, String> keys = stringMap((Map<String, Object>) body.get("keys"));
        Map<String, String> dims = stringMap((Map<String, Object>) body.get("dims"));
        String tupleBody = (String) body.get("body");
        String nonce = (String) body.get("nonce");
        Long ttlSeconds = numberOrNull(body.get("ttl_seconds"));

        byte[] id = repo.out(tenant, subspace, keys, dims, tupleBody, nonce, ttlSeconds);
        HttpUtil.send(ex, 200, "{\"id\":" + HttpUtil.jsonString(HEX.formatHex(id)) + "}");
    }

    // ── rd / rdp ─────────────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private void handleRd(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) {
            HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}");
            return;
        }
        Map<String, Object> body = readBody(ex);
        String subspace = requireString(body, "subspace");
        Map<String, String> pattern = stringMap((Map<String, Object>) body.get("keys_pattern"));
        int n = intOrDefault(body.get("n"), 1);
        long timeoutS = longOrDefault(body.get("timeout_s"), 0);
        TupleRepository.ReadCursor since = readCursor(body.get("since"));

        List<TupleRepository.TupleRow> rows = repo.rd(tenant, subspace, pattern, n, since, timeoutS);
        HttpUtil.send(ex, 200, renderTuples(rows));
    }

    @SuppressWarnings("unchecked")
    private void handleRdp(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) {
            HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}");
            return;
        }
        Map<String, Object> body = readBody(ex);
        String subspace = requireString(body, "subspace");
        Map<String, String> pattern = stringMap((Map<String, Object>) body.get("keys_pattern"));
        int n = intOrDefault(body.get("n"), 1);
        TupleRepository.ReadCursor since = readCursor(body.get("since"));

        List<TupleRepository.TupleRow> rows = repo.rdp(tenant, subspace, pattern, n, since);
        HttpUtil.send(ex, 200, renderTuples(rows));
    }

    // ── in / inp ─────────────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private void handleIn(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) {
            HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}");
            return;
        }
        Map<String, Object> body = readBody(ex);
        String subspace = requireString(body, "subspace");
        Map<String, String> pattern = stringMap((Map<String, Object>) body.get("keys_pattern"));
        String claimant = requireString(body, "claimant");
        long leaseS = requireLong(body, "lease_s");
        long timeoutS = longOrDefault(body.get("timeout_s"), 0);

        Optional<TupleRepository.ClaimedTuple> result = repo.in(tenant, subspace, pattern, claimant, leaseS, timeoutS);
        HttpUtil.send(ex, 200, renderClaim(result));
    }

    @SuppressWarnings("unchecked")
    private void handleInp(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) {
            HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}");
            return;
        }
        Map<String, Object> body = readBody(ex);
        String subspace = requireString(body, "subspace");
        Map<String, String> pattern = stringMap((Map<String, Object>) body.get("keys_pattern"));
        String claimant = requireString(body, "claimant");
        long leaseS = requireLong(body, "lease_s");

        Optional<TupleRepository.ClaimedTuple> result = repo.inp(tenant, subspace, pattern, claimant, leaseS);
        HttpUtil.send(ex, 200, renderClaim(result));
    }

    // ── ack / nack ───────────────────────────────────────────────────────────

    private void handleAck(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) {
            HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}");
            return;
        }
        Map<String, Object> body = readBody(ex);
        repo.ack(tenant, requireString(body, "claim_id"), requireString(body, "claimant"));
        HttpUtil.send(ex, 200, "{\"acked\":true}");
    }

    private void handleNack(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) {
            HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}");
            return;
        }
        Map<String, Object> body = readBody(ex);
        repo.nack(tenant, requireString(body, "claim_id"), requireString(body, "claimant"));
        HttpUtil.send(ex, 200, "{\"nacked\":true}");
    }

    // ── registry / census ────────────────────────────────────────────────────

    private void handleRegistry(HttpExchange ex, String method) throws IOException {
        if (!"GET".equals(method)) {
            HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}");
            return;
        }
        TemplateRegistry.Snapshot snap = repo.registry();
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("digest", snap.digest());
        out.put("sources", snap.sources());
        List<Map<String, Object>> templates = snap.templates().stream().map(this::renderTemplate).toList();
        out.put("templates", templates);
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(out));
    }

    /**
     * nexus-em75s.36: a Phase 2 client learns a template's shape solely from this
     * response, so it must carry everything {@code out()} validates against — the
     * original omitted {@code dimensions} entirely (a client could not learn a
     * template's required dims) and {@code keys} carried no hint that a key's value
     * is pinned to a set. {@code key_values} is present only for templates that pin
     * at least one key (mirrors {@link TemplateRegistry}'s canonical-map digest
     * shape, which also omits it when empty).
     */
    private Map<String, Object> renderTemplate(TemplateSchema t) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", t.name());
        m.put("keys", t.keys());
        if (!t.keyValues().isEmpty()) {
            m.put("key_values", t.keyValues());
        }
        Map<String, Object> dims = new LinkedHashMap<>();
        for (var e : t.dimensions().entrySet()) {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("type", e.getValue().type());
            d.put("values", e.getValue().values());
            d.put("required", e.getValue().required());
            dims.put(e.getKey(), d);
        }
        m.put("dimensions", dims);
        m.put("id_from", t.idFrom().wire());
        m.put("id_dims", t.idDims());
        Map<String, Object> take = new LinkedHashMap<>();
        take.put("enabled", t.take().enabled());
        take.put("default_lease_seconds", t.take().defaultLeaseSeconds());
        take.put("max_lease_seconds", t.take().maxLeaseSeconds());
        take.put("max_attempts", t.take().maxAttempts());
        m.put("take", take);
        m.put("retention_seconds", t.retentionSeconds());
        return m;
    }

    private void handleSubspaceList(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) {
            HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}");
            return;
        }
        String prefix = parseQuery(ex.getRequestURI()).get("prefix");
        List<TupleRepository.SubspaceCensus> list = repo.subspaceList(tenant, prefix);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("subspaces", list.stream().map(this::renderCensus).toList());
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(out));
    }

    private void handleSubspaceStats(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) {
            HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}");
            return;
        }
        String subspace = parseQuery(ex.getRequestURI()).get("subspace");
        if (subspace == null || subspace.isBlank()) {
            HttpUtil.send(ex, 400, "{\"error\":\"subspace required\"}");
            return;
        }
        TupleRepository.SubspaceCensus census = repo.subspaceStats(tenant, subspace);
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(renderCensus(census)));
    }

    private Map<String, Object> renderCensus(TupleRepository.SubspaceCensus c) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("subspace", c.subspace());
        m.put("total", c.total());
        m.put("available", c.available());
        m.put("claimed", c.claimed());
        m.put("dead", c.dead());
        m.put("consumed", c.consumed());
        m.put("expired_unpurged", c.expiredUnpurged());
        m.put("oldest_created_at", c.oldestCreatedAt());
        m.put("newest_created_at", c.newestCreatedAt());
        return m;
    }

    // ── rendering ────────────────────────────────────────────────────────────

    private String renderTuples(List<TupleRepository.TupleRow> rows) throws IOException {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("tuples", rows.stream().map(this::renderTuple).toList());
        return MAPPER.writeValueAsString(out);
    }

    private String renderClaim(Optional<TupleRepository.ClaimedTuple> result) throws IOException {
        Map<String, Object> out = new LinkedHashMap<>();
        if (result.isPresent()) {
            out.put("tuple", renderTuple(result.get().tuple()));
            out.put("claim_id", result.get().claimId());
        } else {
            out.put("tuple", null);
            out.put("claim_id", null);
        }
        return MAPPER.writeValueAsString(out);
    }

    private Map<String, Object> renderTuple(TupleRepository.TupleRow t) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", HEX.formatHex(t.id()));
        m.put("subspace", t.subspace());
        m.put("template", t.template());
        m.put("keys", t.keys());
        m.put("dims", t.dims());
        m.put("body", t.body());
        m.put("claim_state", t.claimState());
        m.put("claimant", t.claimant());
        // nexus-em75s.35 (RDR-205 review, M5): claim_id is the ack/nack credential --
        // never readable off a probe/read response (/rd, /rdp). This same method also
        // renders the tuple embedded in /in and /inp's response, but those already
        // deliver the credential via their own top-level "claim_id" field (see
        // renderClaim), so dropping it here loses nothing there and closes the leak
        // for every other caller who reads a claimed row without having won the claim.
        m.put("lease_until", t.leaseUntil());
        m.put("attempts", t.attempts());
        m.put("consumed_at", t.consumedAt());
        m.put("consumed_by", t.consumedBy());
        m.put("expires_at", t.expiresAt());
        m.put("created_at", t.createdAt());
        return m;
    }

    // ── request parsing ──────────────────────────────────────────────────────

    private Map<String, Object> readBody(HttpExchange ex) throws IOException {
        try (InputStream is = ex.getRequestBody()) {
            return MAPPER.readValue(is, MAP_TYPE);
        }
    }

    private static Map<String, String> parseQuery(java.net.URI uri) {
        Map<String, String> out = new LinkedHashMap<>();
        String raw = uri.getRawQuery();
        if (raw == null || raw.isBlank()) {
            return out;
        }
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            if (eq < 0) {
                continue;
            }
            out.put(java.net.URLDecoder.decode(pair.substring(0, eq), java.nio.charset.StandardCharsets.UTF_8),
                    java.net.URLDecoder.decode(pair.substring(eq + 1), java.nio.charset.StandardCharsets.UTF_8));
        }
        return out;
    }

    private static String requireString(Map<String, Object> body, String field) {
        Object v = body.get(field);
        if (!(v instanceof String s) || s.isBlank()) {
            throw new IllegalArgumentException(field + " required");
        }
        return s;
    }

    private static long requireLong(Map<String, Object> body, String field) {
        Long v = numberOrNull(body.get(field));
        if (v == null) {
            throw new IllegalArgumentException(field + " required");
        }
        return v;
    }

    private static Map<String, String> stringMap(Map<String, Object> raw) {
        if (raw == null) {
            return Map.of();
        }
        Map<String, String> out = new LinkedHashMap<>();
        for (var e : raw.entrySet()) {
            out.put(e.getKey(), e.getValue() == null ? null : String.valueOf(e.getValue()));
        }
        return out;
    }

    private static Long numberOrNull(Object v) {
        if (v == null) {
            return null;
        }
        if (v instanceof Number n) {
            return n.longValue();
        }
        return Long.parseLong(String.valueOf(v));
    }

    private static int intOrDefault(Object v, int def) {
        Long l = numberOrNull(v);
        return l == null ? def : l.intValue();
    }

    private static long longOrDefault(Object v, long def) {
        Long l = numberOrNull(v);
        return l == null ? def : l;
    }

    @SuppressWarnings("unchecked")
    private static TupleRepository.ReadCursor readCursor(Object raw) {
        if (raw == null) {
            return null;
        }
        Map<String, Object> m = (Map<String, Object>) raw;
        Object createdAtRaw = m.get("created_at");
        Object idRaw = m.get("id");
        if (createdAtRaw == null || idRaw == null) {
            return null;
        }
        OffsetDateTime createdAt = OffsetDateTime.parse(String.valueOf(createdAtRaw));
        byte[] id = HEX.parseHex(String.valueOf(idRaw));
        return new TupleRepository.ReadCursor(createdAt, id);
    }
}
