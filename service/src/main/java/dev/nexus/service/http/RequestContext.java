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
 *
 * <p><b>Embed deadline (nexus-8hdg9 phase 2).</b> {@link #deadlineNanos()} carries
 * a second, independent thread-confined value alongside {@link Principal}: a {@code
 * System.nanoTime()}-based deadline minted for EVERY request (not just a write) by
 * {@link AuthFilter} from {@link RequestDeadline#deadlineMsFromEnv()}, generalizing
 * {@code VoyageRetryLoop}'s request-scoped-deadline idiom (nexus-99r7y) from a single
 * embedder's 429 budget to every route's embed call -- see {@code
 * RequestDeadlineExceededException}'s javadoc for why this is not upsert-specific. It
 * is a separate {@link ThreadLocal}, not a {@link Principal} field, because it is not
 * part of the auth outcome -- every request gets one regardless of who the bearer is.
 * Set and cleared together with {@link Principal} by {@link AuthFilter}. This phase is
 * plumbing only: nothing yet reads it inside an embed loop (phases 3/4, nexus-8hdg9.3/.4).
 */
public final class RequestContext {

    /**
     * The resolved principal for the current request.
     *
     * @param tenant        the SERVER-RESOLVED tenant (never null once auth passes)
     * @param session       the resolved session id (minted) or bootstrap bare id; null if none
     * @param mintedSession true iff {@code session} came from a verified minted
     *                      {@code session_tokens} row (server-resolved). When true,
     *                      handlers MUST use {@code session} and reject any client-supplied
     *                      session id that differs (Decision 2 cross-session denial). When
     *                      false the session is a transitional bootstrap bare id.
     * @param isOperator    true iff the bearer is the persistent root token (nexus-e4130).
     *                      The root token is the cross-tenant admin credential; every other
     *                      token is confined to its own tenant on the admin surface
     *                      ({@code TokenAdminHandler}). Resolved server-side from the
     *                      token's {@code scope == 'root'}; never client-asserted.
     * @param scope         the bearer's server-assigned scope (nexus-868dq):
     *                      {@code root|tenant|mint|data}. Carries the mint/data route
     *                      restrictions (a mint credential may only call the data-token
     *                      mint endpoint; mint/data are rejected on the admin surface).
     * @param credentialHash {@code sha256Hex} of the presented bearer — the rate-limit
     *                      key for the data-token mint endpoint (nexus-x1h07). Never the
     *                      raw secret.
     */
    public record Principal(String tenant, String session, boolean mintedSession,
                            boolean isOperator, String scope, String credentialHash) {
    }

    private static final ThreadLocal<Principal> CURRENT = new ThreadLocal<>();

    /** See the class javadoc's "Embed deadline" section. */
    private static final ThreadLocal<Long> DEADLINE_NANOS = new ThreadLocal<>();

    private RequestContext() {
    }

    static void set(Principal principal) {
        CURRENT.set(principal);
    }

    static void clear() {
        CURRENT.remove();
    }

    static void setDeadlineNanos(long deadlineNanos) {
        DEADLINE_NANOS.set(deadlineNanos);
    }

    static void clearDeadline() {
        DEADLINE_NANOS.remove();
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

    /** @return true iff the current session was resolved from a verified minted token. */
    public static boolean isMintedSession() {
        Principal p = CURRENT.get();
        return p != null && p.mintedSession();
    }

    /**
     * @return true iff the current request's bearer is the root/operator token
     *         (nexus-e4130). False for every ordinary tenant token and outside a request.
     */
    public static boolean isOperator() {
        Principal p = CURRENT.get();
        return p != null && p.isOperator();
    }

    /**
     * @return the current bearer's server-assigned scope (nexus-868dq:
     *         {@code root|tenant|mint|data}), or null outside a filtered request.
     */
    public static String scope() {
        Principal p = CURRENT.get();
        return p == null ? null : p.scope();
    }

    /**
     * @return {@code sha256Hex} of the current bearer (the mint rate-limit key,
     *         nexus-x1h07), or null outside a filtered request.
     */
    public static String credentialHash() {
        Principal p = CURRENT.get();
        return p == null ? null : p.credentialHash();
    }

    /**
     * @return the current request's embed deadline (nexus-8hdg9 phase 2) as a
     *         {@link System#nanoTime()} value, or null outside a filtered request.
     *         Minted by {@link AuthFilter} from {@link RequestDeadline#deadlineMsFromEnv()}
     *         for every route, not just a write -- see the class javadoc's "Embed
     *         deadline" section. No embed-loop check point reads this yet (phases
     *         3/4, nexus-8hdg9.3/.4).
     */
    public static Long deadlineNanos() {
        return DEADLINE_NANOS.get();
    }
}
