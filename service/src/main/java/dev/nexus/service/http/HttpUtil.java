package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;

/**
 * Minimal HTTP response helpers. No framework dependency.
 */
public final class HttpUtil {

    private HttpUtil() {}

    public static void send(HttpExchange exchange, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(bytes);
        }
    }

    /**
     * Minimal JSON string escaping (backslash, double-quote, control chars).
     * For structured responses use Jackson; this is for error detail strings only.
     */
    public static String jsonString(String value) {
        if (value == null) return "null";
        var sb = new StringBuilder("\"");
        for (char c : value.toCharArray()) {
            switch (c) {
                case '"'  -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default   -> {
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        sb.append('"');
        return sb.toString();
    }

    /**
     * Walk the cause chain for a {@link SQLException} whose SQLSTATE is class 23
     * (integrity-constraint violation: 23502 not-null, 23503 foreign-key, 23505
     * unique, 23514 check). Returns the offending SQLSTATE string, or {@code null}
     * if no class-23 cause exists.
     *
     * <p>Extracted from {@code AspectHandler} (RDR-172 P3.1, nexus-gfl3y) to a
     * shared home so sibling handlers with a client-supplied id hitting a DB
     * constraint (RDR-172 follow-up, nexus-7e057) map it to a typed 409 AHEAD of
     * the generic 500, instead of duplicating the walk per handler.
     *
     * <p>jOOQ wraps the driver exception in a {@code DataAccessException}, so the
     * constraint violation is a cause of the thrown runtime exception, not the
     * top-level throwable — hence the chain walk. The walk is depth-bounded to
     * tolerate a malformed (self- or mutually-referential) cause chain.
     *
     * <p>Walks the {@link Throwable#getCause()} chain only — correct for the
     * PostgreSQL JDBC driver, which wraps via {@code initCause()}. It does NOT
     * traverse {@link SQLException#getNextException()} (used by some other
     * drivers for chained violations); generalise here if a non-PG driver is
     * ever introduced.
     */
    public static String sqlState23(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof SQLException se) {
                String state = se.getSQLState();
                if (state != null && state.startsWith("23")) {
                    return state;
                }
            }
        }
        return null;
    }

    /**
     * Walk the cause chain for a {@link SQLTransientConnectionException} — HikariCP's
     * "Connection is not available, request timed out" signal, thrown from
     * {@code dataSource.getConnection()} when every pooled connection is checked out
     * (or blocked waiting on a DB-side lock) longer than {@code connectionTimeout}.
     *
     * <p>Bead nexus-h8rf6.2: this condition is RETRYABLE (the pool recovers as soon as
     * a connection frees up) and distinct from a genuine server fault — it deserves a
     * typed 503, not the opaque 500 catch-all, so callers on the client retry ladder
     * back off and retry instead of treating it as a hard failure. {@code TenantScope}
     * wraps the driver exception in a {@code RuntimeException}, so the transient
     * exception is a cause of the thrown exception, not the top-level throwable —
     * hence the chain walk (mirrors {@link #sqlState23}).
     *
     * @return true if a {@link SQLTransientConnectionException} appears anywhere in
     *         {@code t}'s cause chain
     */
    public static boolean isPoolExhausted(Throwable t) {
        Throwable c = t;
        for (int depth = 0; c != null && depth < 32; depth++, c = c.getCause()) {
            if (c instanceof SQLTransientConnectionException) {
                return true;
            }
        }
        return false;
    }

    /**
     * Terminal typed-DB-error mapper for handler catch-alls (wave review, Java-tree
     * audit High-2): pool exhaustion → retryable 503; class-23 integrity violation →
     * typed 409. Returns {@code true} when a typed response was sent; the caller's
     * catch block falls through to its own opaque-500 branch on {@code false}.
     *
     * <p>Exists so every handler shares ONE mapping instead of copy-pasting the
     * {@code isPoolExhausted}/{@code sqlState23} ladder — pre-fix only 2 of 15
     * handlers mapped pool exhaustion and 4 of 15 mapped class-23, so which typed
     * error a client saw depended on which handler it happened to hit. Client body
     * is a fixed message (+ sqlstate for 409); the raw driver message goes to the
     * server log only, never to the client.
     *
     * @param exchange the exchange to respond on
     * @param e        the caught exception (cause chain is walked)
     * @param log      the HANDLER's logger, so log events keep their per-handler source
     * @param event    handler event prefix (e.g. {@code "memory_handler"})
     * @param context  preformatted log context (e.g. {@code "op=/put tenant=t1"})
     * @return true if a typed 503/409 was sent; false if the caller must 500
     */
    public static boolean sendTypedDbError(HttpExchange exchange, Throwable e,
                                           org.slf4j.Logger log, String event,
                                           String context) throws IOException {
        if (isPoolExhausted(e)) {
            // Bead nexus-h8rf6.2: HikariCP pool exhaustion is retryable — a typed 503
            // lets the client's retry ladder back off instead of failing hard.
            log.warn("event={}_pool_exhausted {} error={}", event, context, e.getMessage());
            send(exchange, 503, "{\"error\":\"database connection pool exhausted, retry\"}");
            return true;
        }
        String sqlState = sqlState23(e);
        if (sqlState != null) {
            // nexus-7e057: class-23 integrity violations are caller errors (bad FK id
            // etc.), not server faults — typed 409 ahead of the generic 500.
            log.warn("event={}_integrity_violation {} sqlstate={} error={}",
                event, context, sqlState, e.getMessage());
            send(exchange, 409,
                "{\"error\":\"integrity constraint violation\",\"sqlstate\":"
                + jsonString(sqlState) + "}");
            return true;
        }
        return false;
    }
}
