package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.sql.SQLException;

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
}
