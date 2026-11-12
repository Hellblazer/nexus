// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TenantExport;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExportKeys;
import dev.nexus.service.db.TenantExportReads;
import dev.nexus.service.db.TenantExportSessions;
import dev.nexus.service.db.TokenStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.OutputStream;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.function.Supplier;
import java.util.regex.Pattern;

/**
 * The tenant export routes (RDR-248 "Export session", bead nexus-csayy.2). The cloud edge ends every exchange
 * at 60 seconds and caps a request body at 20 MB, so an export is a session of short requests that all read
 * one snapshot the engine holds:
 *
 * <pre>
 * POST   /v1/tenant/export                         open the caller's tenant's session; 200 with the header
 * GET    /v1/tenant/export/{id}/rows?table=T&amp;after=K   one page of table T after key K (binary COPY)
 * DELETE /v1/tenant/export/{id}                     close the session and release the snapshot
 * </pre>
 *
 * <p><b>Who may call.</b> A {@code tenant} or {@code data} scope token (the AuthFilter has already refused a
 * {@code mint}, {@code mint-locked} or {@code board-ci} token on every path but its own). A {@code root}
 * token gets 403 {@code export_scope_forbidden}: an export is of one tenant's data by that tenant's credential,
 * and the operator credential is not a tenant's. The session belongs to the tenant the token resolved to,
 * never to the token: a fresh token of the same tenant continues it, an expired token never reaches this
 * handler (the filter answers 401, and the session stays open until its own bounds), and another tenant's
 * token gets 404 {@code no_such_session}, the answer for an id that does not exist, so a session's existence
 * is invisible to every other tenant. The tenant that stamps every read is the session's owner, never a
 * request value.
 *
 * <p><b>POST.</b> 200 {@code {session_id, format_version, engine_version, schema_changeset_count, tenant_id,
 * created_at, expires_at, idle_timeout_seconds, total_timeout_seconds, tables:[{name, class, primary_key:[..],
 * columns:[{name, type}]}], embedding_models:[{..}]}}. {@code columns} are the non-generated columns in the
 * order of every COPY row; {@code schema_changeset_count} is {@code /version}'s, null when that read failed.
 * No row counts: each table's first page carries its count. 409 {@code export_session_exists} (with
 * {@code session_id}) when the tenant already has one; 503 {@code export_capacity_full} with {@code
 * Retry-After} when the engine holds its maximum.
 *
 * <p><b>GET rows.</b> {@code table} must name a data or derived table of {@link TenantExport}: anything else is
 * 400 {@code unknown_table}, and the name is never put into SQL as text. {@code after} is absent for a
 * table's first page, else the {@code X-Nexus-Export-Last-Key} of the previous page; its encoding is
 * {@link TenantExportKeys}'s (base64url of a JSON array of one string per primary-key column, in the registry's
 * column order; for {@code chunks}: {@code tenant_id, collection, chash (hex), embedding_model}). Malformed:
 * 400 {@code malformed_after}. The body is a complete PostgreSQL binary COPY stream of the page's rows
 * ({@code application/octet-stream}), at most about {@link TenantExportSessions.Config#pageBytes()} bytes.
 * Headers: {@code X-Nexus-Export-Table}, {@code -Rows} (rows in this page), {@code -Last-Key} (absent for an
 * empty page), {@code -Done} ({@code true} when this page ended the table), {@code -Sha256} (hex SHA-256 of
 * the body) and, on a first page only, {@code -Table-Rows} (the table's row count in the snapshot). The keyset
 * predicate is one row-value comparison over the whole primary key. 410 {@code session_closed} when
 * Postgres no longer has the snapshot (the held transaction ended outside the engine's own bounds); 409
 * {@code page_in_flight} for a second concurrent page of one session; 503 {@code export_page_busy} when a
 * page waited out its lock bound behind a tenant create (retry it).
 *
 * <p><b>DELETE.</b> 200 {@code {"closed":true}}, or 404 {@code no_such_session}.
 */
public final class TenantExportHandler implements HttpHandler {

    public static final String PATH = "/v1/tenant/export";

    /** The version of the header and page format this engine writes; a client refuses one it does not know. */
    public static final int FORMAT_VERSION = 1;

    private static final Logger log = LoggerFactory.getLogger(TenantExportHandler.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Pattern CANONICAL_UUID = Pattern.compile(
        "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}");

    private final Supplier<TenantExportSessions> sessions;
    private final Supplier<String> engineVersion;
    private final Supplier<Long> schemaChangesetCount;

    /**
     * @param sessions             the engine's sessions; a supplier so a test can swap them
     * @param engineVersion        the engine's release identity (its app version on an unstamped build)
     * @param schemaChangesetCount {@code /version}'s memoized count of applied changesets, null when unread
     */
    public TenantExportHandler(Supplier<TenantExportSessions> sessions, Supplier<String> engineVersion,
                               Supplier<Long> schemaChangesetCount) {
        this.sessions = sessions;
        this.engineVersion = engineVersion;
        this.schemaChangesetCount = schemaChangesetCount;
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
            log.debug("event=export_denied reason=scope scope={} tenant={}", scope, tenant);
            error(exchange, 403, "export_scope_forbidden",
                "an export is read with the tenant's own tenant or data token, not a " + scope + " token");
            return;
        }
        String path = exchange.getRequestURI().getPath();
        String rest = path.length() > PATH.length() ? path.substring(PATH.length()) : "";
        if (rest.equals("/")) rest = "";
        if (!rest.isEmpty() && !rest.startsWith("/")) {   // /v1/tenant/exportX shares this context's prefix
            error(exchange, 404, "not_found", "no such route");
            return;
        }
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);
        try {
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
                if (!"DELETE".equals(method)) {
                    error(exchange, 405, "method_not_allowed", "method not allowed");
                    return;
                }
                close(exchange, tenant, segments[0]);
                return;
            }
            if (segments.length == 2 && "rows".equals(segments[1]) && !segments[0].isEmpty()) {
                if (!"GET".equals(method)) {
                    error(exchange, 405, "method_not_allowed", "method not allowed");
                    return;
                }
                rows(exchange, tenant, segments[0]);
                return;
            }
            error(exchange, 404, "not_found", "no such route");
        } catch (TenantExportSessions.NoSuchSessionException e) {
            error(exchange, 404, "no_such_session", "no such export session");
        } catch (RuntimeException e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "tenant_export", "tenant=" + tenant)) {
                log.error("event=tenant_export_error tenant={} error={}", tenant, e.getMessage(), e);
                error(exchange, 500, "internal", "internal server error");
            }
        }
    }

    // ── POST ─────────────────────────────────────────────────────────────────────────────────────

    private void open(HttpExchange exchange, String tenant) throws IOException {
        TenantExportSessions s = sessions.get();
        TenantExportSessions.Opened opened;
        try {
            opened = s.open(tenant);
        } catch (TenantExportSessions.SessionExistsException e) {
            ObjectNode body = errorBody("export_session_exists", e.getMessage());
            if (e.sessionId() != null) body.put("session_id", e.sessionId());
            HttpUtil.send(exchange, 409, MAPPER.writeValueAsString(body));
            return;
        } catch (TenantExportSessions.CapacityFullException e) {
            exchange.getResponseHeaders().set("Retry-After", Integer.toString(e.retryAfterSeconds()));
            ObjectNode body = errorBody("export_capacity_full", e.getMessage());
            body.put("retry_after_seconds", e.retryAfterSeconds());
            HttpUtil.send(exchange, 503, MAPPER.writeValueAsString(body));
            return;
        }
        TenantExportSessions.Session session = opened.session();
        TenantExportSessions.Config config = s.config();
        ObjectNode body = MAPPER.createObjectNode();
        body.put("session_id", session.id());
        body.put("format_version", FORMAT_VERSION);
        body.put("engine_version", engineVersion.get());
        Long changesets = schemaChangesetCount.get();
        if (changesets == null) {
            body.putNull("schema_changeset_count");
        } else {
            body.put("schema_changeset_count", changesets);
        }
        body.put("tenant_id", tenant);
        body.put("created_at", session.openedAt().toString());
        body.put("expires_at", session.openedAt().plus(config.totalBound()).toString());
        body.put("idle_timeout_seconds", config.idleBound().toSeconds());
        body.put("total_timeout_seconds", config.totalBound().toSeconds());
        ArrayNode tables = body.putArray("tables");
        for (TenantExportReads.TableHeader t : opened.header().tables()) {
            ObjectNode node = tables.addObject();
            node.put("name", t.name());
            node.put("class", t.exportClass().name().toLowerCase(Locale.ROOT));
            ArrayNode key = node.putArray("primary_key");
            t.primaryKey().forEach(key::add);
            ArrayNode columns = node.putArray("columns");
            for (TenantExport.Column c : t.columns()) {
                columns.addObject().put("name", c.name()).put("type", c.type());
            }
        }
        ArrayNode models = body.putArray("embedding_models");
        for (var row : opened.header().embeddingModels()) {
            ObjectNode node = models.addObject();
            row.forEach((k, v) -> node.set(k, MAPPER.valueToTree(v)));
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(body));
    }

    // ── DELETE ───────────────────────────────────────────────────────────────────────────────────

    private void close(HttpExchange exchange, String tenant, String id) throws IOException {
        requireWellFormedId(id);
        sessions.get().close(tenant, id);
        HttpUtil.send(exchange, 200, "{\"closed\":true}");
    }

    // ── GET rows ─────────────────────────────────────────────────────────────────────────────────

    private void rows(HttpExchange exchange, String tenant, String id) throws IOException {
        requireWellFormedId(id);
        TenantExportSessions s = sessions.get();
        TenantExportSessions.Session session = s.lookup(tenant, id);   // 404 first: nothing below is revealed to a stranger
        String tableName = queryParam(exchange, "table");
        var entry = TenantExport.exportable(tableName);
        if (entry.isEmpty()) {
            error(exchange, 400, "unknown_table", "table must name an exportable data or derived table");
            return;
        }
        ExportTable table = entry.get();
        List<Object> after = null;
        String afterText = queryParam(exchange, "after");
        if (afterText != null) {
            try {
                after = TenantExportKeys.binds(table.primaryKey(),
                    TenantExportKeys.decode(afterText, table.primaryKey()));
            } catch (TenantExportKeys.MalformedKeyException e) {
                error(exchange, 400, "malformed_after", e.getMessage());
                return;
            }
        }
        final List<Object> bounds = after;
        TenantExportReads.Page page;
        try {
            page = s.withPage(session, held -> s.reads().page(held.snapshotId(), held.owner(), table, bounds,
                s.config().pageBytes(), s.config().pageMaxRows()));
        } catch (TenantExportReads.SnapshotGoneException e) {
            s.forget(session);
            error(exchange, 410, "session_closed", "the export session's snapshot no longer exists; open a new session");
            return;
        } catch (TenantExportSessions.PageInFlightException e) {
            error(exchange, 409, "page_in_flight", e.getMessage());
            return;
        } catch (TenantExportReads.PageBusyException e) {
            exchange.getResponseHeaders().set("Retry-After", "1");
            error(exchange, 503, "export_page_busy", e.getMessage());
            return;
        }
        sendPage(exchange, table, page);
    }

    private static void sendPage(HttpExchange exchange, ExportTable table, TenantExportReads.Page page)
            throws IOException {
        var headers = exchange.getResponseHeaders();
        headers.set("Content-Type", "application/octet-stream");
        headers.set("X-Nexus-Export-Table", table.name());
        headers.set("X-Nexus-Export-Rows", Integer.toString(page.rows()));
        if (page.lastKey() != null) headers.set("X-Nexus-Export-Last-Key", page.lastKey());
        headers.set("X-Nexus-Export-Done", Boolean.toString(page.done()));
        if (page.tableRows() != null) headers.set("X-Nexus-Export-Table-Rows", Long.toString(page.tableRows()));
        headers.set("X-Nexus-Export-Sha256", sha256Hex(page.buffer(), page.length()));
        exchange.sendResponseHeaders(200, page.length());
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(page.buffer(), 0, page.length());
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────────────────────────

    private static void requireWellFormedId(String id) {
        if (!CANONICAL_UUID.matcher(id).matches()) {
            throw new TenantExportSessions.NoSuchSessionException();
        }
    }

    /** The first value of query parameter {@code name}, percent-decoded, or null when absent. */
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

    private static String sha256Hex(byte[] bytes, int length) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            digest.update(bytes, 0, length);
            return HexFormat.of().formatHex(digest.digest());
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is a required JDK algorithm", e);
        }
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
