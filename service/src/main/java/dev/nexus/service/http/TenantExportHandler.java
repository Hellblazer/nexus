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
 * 400 {@code malformed_after}. (A malformed percent-encoding in the query string never reaches this handler:
 * the JDK server answers its own bare 400, with no JSON body and no reason.) The body is a complete PostgreSQL binary COPY stream of the page's rows
 * ({@code application/octet-stream}), at most about {@link TenantExportSessions.Config#pageBytes()} bytes.
 * Headers: {@code X-Nexus-Export-Table}, {@code -Rows} (rows in this page), {@code -Last-Key} (absent for an
 * empty page), {@code -Done} ({@code true} when this page ended the table), {@code -Sha256} (hex SHA-256 of
 * the body) and, on a first page only, {@code -Table-Rows} (the table's row count in the snapshot). The keyset
 * predicate is one row-value comparison over the whole primary key. 410 {@code session_closed} when
 * Postgres no longer has the snapshot (the held transaction ended outside the engine's own bounds); 409
 * {@code page_in_flight} with {@code Retry-After: 1} for a second concurrent page of one session; 503
 * {@code export_page_busy} with {@code Retry-After: 1} when a read (a page, or the header of a POST) waited out
 * its lock bound behind a tenant create (retry it; a POST that answers it opened nothing); 503 {@code
 * export_page_over_budget}, with no {@code Retry-After} and a {@code page_budget_ms} field, when a read was cancelled
 * at its time budget (below): another try may meet the same limit, so a client repeats it a few times and then reports
 * it. The engine logs {@code event=export_page_over_budget} with the tenant, the table and the elapsed time.
 *
 * <p><b>The page cut and its time budget, against the edge limits.</b> The cloud edge ends an exchange at 60
 * seconds (RDR-248 A1) and the bead's gate N1 wants a page to START its response inside 30 seconds. A page is
 * cut by bytes at {@link TenantExportSessions.Config#pageBytes()} (default 16 MiB, at most 19,000,000: under
 * the 20 MB body cap, which a page may overshoot by one row). The engine bounds the page's DATABASE time to
 * {@link TenantExportSessions.Config#pageBudgetMs()} (default 20 s, {@code NX_EXPORT_PAGE_BUDGET_SECONDS}), all
 * its statements together (the first page's row count, then the COPY): each runs under {@code statement_timeout}
 * set to what is left of the budget, and a statement cancelled at the bound answers 503 {@code
 * export_page_over_budget}. The database serves about 4,000 chunk rows in 0.1 s (T2 {@code
 * nexus/rdr248-local-spike-r2-2026-11-11}), so the budget bounds a pathological page and is not the expected
 * time. The budget does NOT include the pool borrow: that is bounded by the pool's own connection timeout
 * (HikariCP's 30 s default; {@code Main} does not change it for the engine's pool) and runs before the budget
 * starts. With a free pool connection a page starts its response well inside 30 s; on an exhausted pool the
 * borrow can take up to 30 s first, so a page may start up to about 50 s after the request, inside the 60 s
 * exchange limit but past the 30 s bound, and an exhausted pool answers 503 (see below). The header read of a
 * POST gives each of its statements the full budget, not a shared one (they are catalog reads). A page is
 * buffered whole before its first byte is sent (its hash is a header), so the transfer is outside the
 * response-start bound and the database transaction is already closed while the client reads. Measured at 500,000
 * chunks (nexus-csayy.4, RDR-248 Performance Expectations): a 16 MB chunks page starts its response in about 0.2 s at the
 * median and 0.28 s at the worst, flat from the first page to the last, because the page's transaction keeps the
 * keyset read an ordered index scan ({@code TenantExportReads#pinOrderedScan}). Before that, a keyset page after the
 * first was planned as a sort over every row left in the tenant and cost 0.9 s at the median and 2.6 to 3.1 s at the
 * worst, in proportion to the tenant's size (about 30 s near 5 million chunks). A table keyed by {@code id} alone costs
 * its rows divided by the tenant's share of the table: see the Performance Expectations.
 *
 * <p><b>Other answers.</b> An exhausted pool answers 503 {@code {"error":"database connection pool exhausted,
 * retry"}} with no reason code and no {@code Retry-After}: the client backs off on its own ladder. The auth filter
 * answers 401, and 403 {@code forbidden} for a token confined to its own route; an unknown path under the prefix
 * is 404 {@code not_found}, a wrong method 405 {@code method_not_allowed}, and anything unexpected 500 {@code
 * internal}. A client may DELETE the {@code session_id} that 409 {@code export_session_exists} returns before it
 * opens again.
 *
 * <p><b>A gone session.</b> The registry is in memory and PER ENGINE INSTANCE: the deployment assumes one
 * engine instance, or routing that keeps one tenant on one instance. A 404 {@code no_such_session} or a 410
 * {@code session_closed} on the caller's OWN session id means the session is gone: it expired (idle 10 minutes
 * or 60 minutes old, from {@code expires_at}/{@code total_timeout_seconds} in the header), the engine
 * restarted, the request reached a different engine instance, or its snapshot ended. The two are not told
 * apart, and a client must not retry the page: it starts over with a new session, and when it was past
 * {@code expires_at} it fails naming the 60-minute limit. A client must also refuse a header whose {@code
 * schema_changeset_count} is null (the engine could not read its changelog identity, so the export carries no
 * schema marker a restore can check).
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
        long opened0 = System.nanoTime();
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
        } catch (TenantExportReads.PageBusyException e) {
            // the header read waited out a table lock: nothing was opened (the held transaction ended)
            exchange.getResponseHeaders().set("Retry-After", "1");
            error(exchange, 503, "export_page_busy", e.getMessage());
            return;
        } catch (TenantExportReads.PageOverBudgetException e) {
            overBudget(exchange, tenant, "(header)", opened0, s.config().pageBudgetMs(), e);
            return;
        }
        TenantExportSessions.Session session = opened.session();
        TenantExportSessions.Config config = s.config();
        ObjectNode body = MAPPER.createObjectNode();
        body.put("session_id", session.id());
        body.put("format_version", FORMAT_VERSION);
        body.put("engine_version", engineVersion.get());
        putSchemaChangesetCount(body, schemaChangesetCount.get());
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

    /** {@code schema_changeset_count}: the number, or JSON null when the journal read failed (a client refuses a null). */
    static void putSchemaChangesetCount(ObjectNode body, Long changesets) {
        if (changesets == null) {
            body.putNull("schema_changeset_count");
        } else {
            body.put("schema_changeset_count", changesets.longValue());
        }
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
        String afterText = queryParam(exchange, "after");
        var entry = TenantExport.exportable(tableName);
        if (entry.isEmpty()) {
            error(exchange, 400, "unknown_table", "table must name an exportable data or derived table");
            return;
        }
        ExportTable table = entry.get();
        List<Object> after = null;
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
        long read0 = System.nanoTime();
        try {
            page = s.withPage(session, held -> s.reads().page(held.snapshotId(), held.owner(), table, bounds,
                s.config().pageBytes(), s.config().pageMaxRows()));
        } catch (TenantExportReads.SnapshotGoneException e) {
            s.forget(session);
            error(exchange, 410, "session_closed", "the export session's snapshot no longer exists; open a new session");
            return;
        } catch (TenantExportSessions.PageInFlightException e) {
            exchange.getResponseHeaders().set("Retry-After", "1");
            error(exchange, 409, "page_in_flight", e.getMessage());
            return;
        } catch (TenantExportReads.PageBusyException e) {
            exchange.getResponseHeaders().set("Retry-After", "1");
            error(exchange, 503, "export_page_busy", e.getMessage());
            return;
        } catch (TenantExportReads.PageOverBudgetException e) {
            overBudget(exchange, tenant, table.name(), read0, s.config().pageBudgetMs(), e);
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

    /**
     * A read cancelled at its time budget: 503 {@code export_page_over_budget}, no {@code Retry-After} (a repeat is
     * likely to meet the same limit, so the client does not wait it out as it does a lock), the budget in the body, and
     * a warn line that names the tenant, the table and the time the read ran, so an operator can see which page it was.
     */
    private static void overBudget(HttpExchange exchange, String tenant, String table, long startNanos, int budgetMs,
                                   TenantExportReads.PageOverBudgetException e) throws IOException {
        long elapsedMs = (System.nanoTime() - startNanos) / 1_000_000L;
        log.warn("event=export_page_over_budget tenant={} table={} elapsed_ms={} budget_ms={}", tenant, table, elapsedMs, budgetMs);
        ObjectNode body = errorBody("export_page_over_budget", "the export page for " + table
            + " was cancelled at its " + budgetMs + " ms time budget");
        body.put("page_budget_ms", budgetMs);
        HttpUtil.send(exchange, 503, MAPPER.writeValueAsString(body));
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
