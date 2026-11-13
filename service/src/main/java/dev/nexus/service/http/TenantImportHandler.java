// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TenantExport.Column;
import dev.nexus.service.db.TenantImportPlan.FileTable;
import dev.nexus.service.db.TenantImportReport;
import dev.nexus.service.db.TenantImportSessions;
import dev.nexus.service.db.TenantImportSessions.RefusedException;
import dev.nexus.service.db.TokenStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.function.Supplier;
import java.util.regex.Pattern;

/**
 * The tenant import routes (RDR-248 "Import session", bead nexus-csayy.6). An import is a session of short requests
 * (the cloud edge ends an exchange at 60 seconds and caps a body at 20 MB); the counterpart of
 * {@link TenantExportHandler}:
 *
 * <pre>
 * POST   /v1/tenant/import                                open: the file's header; 200 with the session
 * PUT    /v1/tenant/import/{id}/rows?table=T&amp;part=k       one part of table T (a binary COPY stream, at most 8 MiB)
 * POST   /v1/tenant/import/{id}/close?table=T&amp;rows=N      table T is complete at N rows (the trailer's count)
 * POST   /v1/tenant/import/{id}/commit                    start the load: 202; a repeat answers the status with 200
 * GET    /v1/tenant/import/{id}                           status: staging, loading T, verifying, committed, failed
 * DELETE /v1/tenant/import/{id}                           abort
 * GET    /v1/tenant/import-marker                         the tenant's {@code rdr248_restore} marker, if any
 * </pre>
 *
 * <p><b>Who may call.</b> As the export: a {@code tenant} or {@code data} token, for the tenant it resolves to, and
 * every route answers only that tenant (any other tenant's session id is 404 {@code no_such_session}). A session
 * belongs to the tenant, not the token: an expired token never reaches this handler (the filter answers 401), and
 * a fresh token of the tenant continues the session. A root token gets 403 {@code import_scope_forbidden}.
 *
 * <p><b>Open.</b> Body: {@code {format_version, engine_version, schema_changeset_count, tenant_id, header_sha256,
 * row_bytes, tables:[{name, class, primary_key, columns:[{name, type}]}], embedding_models:[{embedding_model, ...}]}}:
 * the export's header, plus the SHA-256 of the file's header bytes ({@code header_sha256}, which the restore marker
 * names) and the file's total row bytes. 200 {@code {session_id, part_bytes, idle_timeout_seconds,
 * total_timeout_seconds, load_max_seconds, max_bytes, tables:[load order], skipped_tables:{name: reason},
 * notes:[..]}}. Refusals, each naming what failed and each costing no upload: 503 {@code singleton_lock_not_held};
 * 422 {@code unsupported_format_version}, {@code file_not_loadable} (with {@code problems}) or {@code
 * missing_embedding_model}; 413 {@code import_too_large}; 409 {@code tenant_not_empty} (with {@code table}); 409
 * {@code session_exists} (with the open session's {@code session_id}, which {@code DELETE} closes); 503 {@code
 * import_capacity_full} with {@code Retry-After}.
 *
 * <p><b>Parts.</b> Header {@code X-Nexus-Part-Sha256} (hex SHA-256 of the body) is required and checked before
 * anything touches the session's transaction: a mismatch is 400 {@code part_sha256_mismatch} and changes nothing.
 * Parts of a table are numbered from 0 and arrive in order; tables arrive in the order of {@code tables} in the open
 * answer. 200 {@code {staged:true}}, or {@code {repeat:true}} for a repeat of the last staged part with the same
 * hash, or {@code {skipped:true}} for a table the load skips. 409 {@code part_in_flight} (Retry-After 1), {@code
 * part_gap}, {@code part_already_staged}, {@code part_sha_conflict}, {@code table_closed}, {@code
 * table_out_of_order}; 413 {@code part_too_large} (over 8 MiB) or {@code import_too_large}; 422 {@code part_failed}
 * (a malformed stream or a database error: the session is closed and the client starts over).
 *
 * <p><b>Close.</b> 200, or 409 {@code row_count_mismatch} with {@code staged} and {@code expected}. <b>Commit.</b>
 * 409 {@code tables_open} while a table of the open answer is not closed; 503 {@code restore_busy} with {@code
 * Retry-After: 60} when another load holds the restore lock (the upload stays staged); 202 once the load has started.
 * <b>Status.</b> {@code {session_id, state, status, notes, staged_bytes, report?}}; the {@code status} text is
 * {@code staging}, {@code loading <table>}, {@code verifying}, {@code committed} or {@code failed: <reason>}, kept 24
 * hours after the session ends (every other route answers 404 {@code no_such_session} then).
 */
public final class TenantImportHandler implements HttpHandler {

    public static final String PATH = "/v1/tenant/import";
    public static final String MARKER_PATH = "/v1/tenant/import-marker";

    private static final Logger log = LoggerFactory.getLogger(TenantImportHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Pattern CANONICAL_UUID = Pattern.compile(
        "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}");
    private static final int MAX_OPEN_BODY_BYTES = 4 * 1024 * 1024;

    private final Supplier<TenantImportSessions> sessions;
    private final Supplier<dev.nexus.service.db.TenantExportSessions> exports;

    public TenantImportHandler(Supplier<TenantImportSessions> sessions,
                               Supplier<dev.nexus.service.db.TenantExportSessions> exports) {
        this.sessions = sessions;
        this.exports = exports;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            error(exchange, 500, "internal", "internal: auth principal not set");
            return;
        }
        String scope = RequestContext.scope();
        if (!TokenStore.SCOPE_TENANT.equals(scope) && !TokenStore.SCOPE_DATA.equals(scope)) {
            error(exchange, 403, "import_scope_forbidden",
                "an import is made with the tenant's own tenant or data token, not a " + scope + " token");
            return;
        }
        String path = exchange.getRequestURI().getPath();
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);
        try {
            if (path.equals(MARKER_PATH)) {
                if (!"GET".equals(method)) {
                    error(exchange, 405, "method_not_allowed", "method not allowed");
                    return;
                }
                marker(exchange, tenant);
                return;
            }
            if (!path.equals(PATH) && !path.startsWith(PATH + "/")) {
                error(exchange, 404, "not_found", "no such route");
                return;
            }
            String rest = path.substring(PATH.length());
            if (rest.equals("/")) rest = "";
            if (rest.isEmpty()) {
                if (!"POST".equals(method)) {
                    error(exchange, 405, "method_not_allowed", "method not allowed");
                    return;
                }
                open(exchange, tenant);
                return;
            }
            String[] segments = rest.substring(1).split("/", -1);
            if (segments.length == 1 && !segments[0].isEmpty()) {
                String id = wellFormed(segments[0]);
                switch (method) {
                    case "GET" -> status(exchange, tenant, id);
                    case "DELETE" -> abort(exchange, tenant, id);
                    default -> error(exchange, 405, "method_not_allowed", "method not allowed");
                }
                return;
            }
            if (segments.length == 2 && !segments[0].isEmpty()) {
                String id = wellFormed(segments[0]);
                switch (segments[1]) {
                    case "rows" -> {
                        if (!"PUT".equals(method)) error(exchange, 405, "method_not_allowed", "method not allowed");
                        else part(exchange, tenant, id);
                    }
                    case "close" -> {
                        if (!"POST".equals(method)) error(exchange, 405, "method_not_allowed", "method not allowed");
                        else close(exchange, tenant, id);
                    }
                    case "commit" -> {
                        if (!"POST".equals(method)) error(exchange, 405, "method_not_allowed", "method not allowed");
                        else commit(exchange, tenant, id);
                    }
                    default -> error(exchange, 404, "not_found", "no such route");
                }
                return;
            }
            error(exchange, 404, "not_found", "no such route");
        } catch (RefusedException e) {
            refused(exchange, e);
        } catch (RuntimeException e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "tenant_import", "tenant=" + tenant)) {
                log.error("event=tenant_import_error tenant={} error={}", tenant, e.getMessage(), e);
                error(exchange, 500, "internal", "internal server error");
            }
        }
    }

    // ── routes ──────────────────────────────────────────────────────────────────────────────────

    private void open(HttpExchange exchange, String tenant) throws IOException {
        byte[] raw = exchange.getRequestBody().readNBytes(MAX_OPEN_BODY_BYTES + 1);
        if (raw.length > MAX_OPEN_BODY_BYTES) {
            error(exchange, 413, "header_too_large", "the open request's header is over " + MAX_OPEN_BODY_BYTES + " bytes");
            return;
        }
        TenantImportSessions.OpenRequest req;
        try {
            req = parseOpen(MAPPER.readTree(raw));
        } catch (IllegalArgumentException | IOException e) {
            error(exchange, 400, "bad_request", "the open request is not a valid header: " + e.getMessage());
            return;
        }
        TenantImportSessions s = sessions.get();
        TenantImportSessions.Opened opened = s.open(tenant, req);
        var export = exports.get().config();
        ObjectNode body = MAPPER.createObjectNode();
        body.put("session_id", opened.session().id());
        body.put("part_bytes", TenantImportSessions.PART_BYTES);
        body.put("idle_timeout_seconds", export.idleBound().toSeconds());
        body.put("total_timeout_seconds", export.totalBound().toSeconds());
        body.put("load_max_seconds", s.config().loadMax().toSeconds());
        body.put("max_bytes", s.config().maxBytes());
        ArrayNode tables = body.putArray("tables");
        opened.loadOrder().forEach(tables::add);
        ObjectNode skipped = body.putObject("skipped_tables");
        opened.skippedTables().forEach(skipped::put);
        ArrayNode notes = body.putArray("notes");
        opened.notes().forEach(notes::add);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(body));
    }

    private static TenantImportSessions.OpenRequest parseOpen(JsonNode root) {
        if (root == null || !root.isObject()) throw new IllegalArgumentException("the body must be a JSON object");
        int format = root.path("format_version").asInt(-1);
        String engine = root.path("engine_version").asText(null);
        Long changesets = root.path("schema_changeset_count").isNumber() ? root.get("schema_changeset_count").asLong() : null;
        String source = root.path("tenant_id").asText(null);
        String headerSha = root.path("header_sha256").asText(null);
        long rowBytes = root.path("row_bytes").asLong(-1);
        List<FileTable> tables = new ArrayList<>();
        JsonNode tableNodes = root.path("tables");
        if (!tableNodes.isArray()) throw new IllegalArgumentException("tables must be an array");
        for (JsonNode t : tableNodes) {
            String name = t.path("name").asText(null);
            if (name == null || name.isBlank()) throw new IllegalArgumentException("a table has no name");
            List<Column> columns = new ArrayList<>();
            for (JsonNode c : t.path("columns")) {
                String cn = c.path("name").asText(null);
                String ct = c.path("type").asText(null);
                if (cn == null || ct == null) throw new IllegalArgumentException("a column of " + name + " has no name or type");
                columns.add(new Column(cn, ct));
            }
            if (columns.isEmpty()) throw new IllegalArgumentException("table " + name + " lists no columns");
            tables.add(new FileTable(name, columns));
        }
        List<String> models = new ArrayList<>();
        for (JsonNode m : root.path("embedding_models")) {
            String model = m.isTextual() ? m.asText() : m.path("embedding_model").asText(null);
            if (model == null || model.isBlank()) throw new IllegalArgumentException("an embedding model has no name");
            models.add(model);
        }
        return new TenantImportSessions.OpenRequest(format, engine, changesets, source, headerSha, rowBytes, tables, models);
    }

    private void part(HttpExchange exchange, String tenant, String id) throws IOException {
        String table = queryParam(exchange, "table");
        String partText = queryParam(exchange, "part");
        String sha = exchange.getRequestHeaders().getFirst("X-Nexus-Part-Sha256");
        if (table == null || partText == null) {
            error(exchange, 400, "bad_request", "table and part are required");
            return;
        }
        int part;
        try {
            part = Integer.parseInt(partText);
        } catch (NumberFormatException e) {
            error(exchange, 400, "bad_part", "part is not a whole number");
            return;
        }
        if (sha == null || !sha.toLowerCase(Locale.ROOT).matches("[0-9a-f]{64}")) {
            error(exchange, 400, "missing_part_sha256", "X-Nexus-Part-Sha256 (64 hex digits) is required");
            return;
        }
        // 404 for a stranger before the body is read: nothing is revealed and no memory is spent
        sessions.get().status(tenant, id);
        byte[] body = exchange.getRequestBody().readNBytes(TenantImportSessions.PART_BYTES + 1);
        if (body.length > TenantImportSessions.PART_BYTES) {
            error(exchange, 413, "part_too_large", "a part is at most " + TenantImportSessions.PART_BYTES + " bytes");
            return;
        }
        if (!sha256Hex(body).equals(sha.toLowerCase(Locale.ROOT))) {
            error(exchange, 400, "part_sha256_mismatch", "the part's SHA-256 is not the one sent; send it again");
            return;
        }
        var result = sessions.get().stagePart(tenant, id, table, part, sha.toLowerCase(Locale.ROOT), body);
        ObjectNode out = MAPPER.createObjectNode();
        switch (result) {
            case STAGED -> out.put("staged", true);
            case REPEAT -> out.put("repeat", true);
            case SKIPPED -> out.put("skipped", true);
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(out));
    }

    private void close(HttpExchange exchange, String tenant, String id) throws IOException {
        String table = queryParam(exchange, "table");
        String rowsText = queryParam(exchange, "rows");
        if (table == null || rowsText == null) {
            error(exchange, 400, "bad_request", "table and rows are required");
            return;
        }
        long rows;
        try {
            rows = Long.parseLong(rowsText);
        } catch (NumberFormatException e) {
            error(exchange, 400, "bad_rows", "rows is not a whole number");
            return;
        }
        boolean skipped = sessions.get().closeTable(tenant, id, table, rows);
        ObjectNode out = MAPPER.createObjectNode();
        out.put("closed", true);
        if (skipped) out.put("skipped", true);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(out));
    }

    private void commit(HttpExchange exchange, String tenant, String id) throws IOException {
        var result = sessions.get().commit(tenant, id);
        HttpUtil.send(exchange, result == TenantImportSessions.CommitResult.STARTED ? 202 : 200,
            MAPPER.writeValueAsString(statusBody(sessions.get().status(tenant, id))));
    }

    private void status(HttpExchange exchange, String tenant, String id) throws IOException {
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(statusBody(sessions.get().status(tenant, id))));
    }

    private void abort(HttpExchange exchange, String tenant, String id) throws IOException {
        sessions.get().abort(tenant, id);
        HttpUtil.send(exchange, 200, "{\"closed\":true}");
    }

    private void marker(HttpExchange exchange, String tenant) throws IOException {
        String json = sessions.get().readMarker(tenant);
        ObjectNode out = MAPPER.createObjectNode();
        out.put("present", json != null);
        if (json != null) {
            try {
                out.set("marker", MAPPER.readTree(json));
            } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
                // the key's value is data a restore carries, so it can be anything: an unreadable marker names no file, which a
                // client reads as "not committed by this file"
                out.put("present", true);
                out.put("committed", false);
                out.put("reason", "the rdr248_restore row does not hold a readable marker");
            }
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(out));
    }

    // ── bodies ──────────────────────────────────────────────────────────────────────────────────

    static ObjectNode statusBody(TenantImportSessions.StatusView v) {
        ObjectNode out = MAPPER.createObjectNode();
        out.put("session_id", v.id());
        out.put("state", v.state());
        out.put("status", v.status());
        out.put("staged_bytes", v.stagedBytes());
        ArrayNode notes = out.putArray("notes");
        v.notes().forEach(notes::add);
        TenantImportReport r = v.report();
        if (r != null) {
            ObjectNode report = out.putObject("report");
            ArrayNode tables = report.putArray("tables");
            for (TenantImportReport.TableResult t : r.tables()) {
                tables.addObject().put("table", t.table()).put("staged", t.staged()).put("inserted", t.inserted())
                    .put("seeded_skipped", t.seededSkipped()).put("dropped", t.dropped());
            }
            ArrayNode softs = report.putArray("soft_references");
            for (TenantImportReport.SoftResult s : r.softReferences()) {
                softs.addObject().put("column", s.column()).put("remapped", s.remapped())
                    .put("passed_through", s.passedThrough()).put("negated", s.negated());
            }
            ObjectNode skipped = report.putObject("skipped_tables");
            r.skippedTables().forEach(skipped::put);
            ArrayNode columns = report.putArray("skipped_columns");
            r.skippedColumns().forEach(columns::add);
            report.put("rows_inserted", r.insertedRows());
            report.put("rows_dropped", r.droppedRows());
        }
        return out;
    }

    private static void refused(HttpExchange exchange, RefusedException e) throws IOException {
        if (e.retryAfterSeconds() != null) {
            exchange.getResponseHeaders().set("Retry-After", Integer.toString(e.retryAfterSeconds()));
        }
        ObjectNode body = errorBody(e.reason(), e.getMessage());
        if (e.retryAfterSeconds() != null) body.put("retry_after_seconds", e.retryAfterSeconds());
        for (Map.Entry<String, Object> x : e.extra().entrySet()) {
            body.set(x.getKey(), MAPPER.valueToTree(x.getValue()));
        }
        HttpUtil.send(exchange, e.status(), MAPPER.writeValueAsString(body));
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────

    private static String wellFormed(String id) {
        if (!CANONICAL_UUID.matcher(id).matches()) {
            // the answer for an id that cannot name a session: the same as for one that does not exist
            throw new RefusedException(404, "no_such_session", "no such import session");
        }
        return id;
    }

    private static String sha256Hex(byte[] bytes) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is a required JDK algorithm", e);
        }
    }

    private static String queryParam(HttpExchange exchange, String name) {
        String raw = exchange.getRequestURI().getRawQuery();
        if (raw == null || raw.isEmpty()) return null;
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            String key = URLDecoder.decode(eq < 0 ? pair : pair.substring(0, eq), StandardCharsets.UTF_8);
            if (key.equals(name)) {
                return eq < 0 ? "" : URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8);
            }
        }
        return null;
    }

    private static ObjectNode errorBody(String reason, String message) {
        ObjectNode body = MAPPER.createObjectNode();
        body.put("error", message);
        body.put("reason", reason);
        return body;
    }

    private static void error(HttpExchange exchange, int status, String reason, String message) throws IOException {
        HttpUtil.send(exchange, status, MAPPER.writeValueAsString(errorBody(reason, message)));
    }
}
