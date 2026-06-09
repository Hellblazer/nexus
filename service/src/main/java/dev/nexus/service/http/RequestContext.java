package dev.nexus.service.http;

/**
 * RDR-152 bead nexus-gmiaf.32.2 — thread-confined per-request principal.
 *
 * <p><b>Why not {@code HttpExchange} attributes:</b> {@code com.sun.net.httpserver}
 * stores exchange attributes on the shared {@link com.sun.net.httpserver.HttpContext},
 * not per-exchange. With the server's {@code newVirtualThreadPerTaskExecutor}, two
 * concurrent requests on the same context would race on the same attribute keys — a
 * cross-tenant data leak (request A could read request B's stamped tenant). A request
 * that did not set an attribute would also observe a previous request's value.
 *
 * <p>{@link AuthFilter} runs and dispatches the handler on a single (virtual) thread
 * per request, so a {@link ThreadLocal} set at the top of the filter and CLEARED in a
 * {@code finally} after {@code chain.doFilter} is exactly request-scoped and race-free.
 * Handlers read {@link #tenant()} / {@link #session()} instead of exchange attributes.
 */
public final class RequestContext {

    /**
     * The resolved principal for the current request.
     *
     * @param tenant  the SERVER-RESOLVED tenant (never null once auth passes)
     * @param session the resolved session id (minted) or bootstrap bare id; null if none
     */
    public record Principal(String tenant, String session) {
    }

    private static final ThreadLocal<Principal> CURRENT = new ThreadLocal<>();

    private RequestContext() {
    }

    static void set(Principal principal) {
        CURRENT.set(principal);
    }

    static void clear() {
        CURRENT.remove();
    }

    /** @return the current request's principal, or null outside a filtered request. */
    public static Principal current() {
        return CURRENT.get();
    }

    /** @return the resolved tenant for the current request, or null if unset. */
    public static String tenant() {
        Principal p = CURRENT.get();
        return p == null ? null : p.tenant();
    }

    /** @return the resolved session id for the current request, or null if none. */
    public static String session() {
        Principal p = CURRENT.get();
        return p == null ? null : p.session();
    }
}
