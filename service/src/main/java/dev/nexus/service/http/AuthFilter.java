package dev.nexus.service.http;

import dev.nexus.service.db.SessionPrincipal;
import dev.nexus.service.db.TokenCache;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.db.TokenStore;
import com.sun.net.httpserver.Filter;
import com.sun.net.httpserver.HttpExchange;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.util.Objects;
import java.util.Optional;

/**
 * RDR-152 bead nexus-gmiaf.32.2 — Bearer auth with SERVER-SIDE token→tenant
 * resolution and per-session verification. Replaces the Phase 1–4 single-fixed-token
 * posture where any token holder could claim any tenant via {@code X-Nexus-Tenant}.
 *
 * <p><b>Decision 1 (token→tenant binding).</b> The presented bearer is hashed
 * ({@link TokenHashing#sha256Hex}) and resolved against the {@code service_tokens}
 * registry via {@link TokenCache}. Missing/revoked/expired → 401. The matched row's
 * {@code tenant_id} is authoritative: that tenant wins and the client
 * {@code X-Nexus-Tenant} header is IGNORED (logged at debug on mismatch). Phase E
 * (nexus-gmiaf.32.5) retired the transitional wildcard ("*") any-tenant grant — every
 * token, including the persistent root token, is strictly tenant-bound, so no token can
 * cross tenants.
 *
 * <p><b>Decision 2 (per-session verification), require-minted.</b> If
 * {@code X-Nexus-T1-Session} is present, its value is hashed and looked up in
 * {@code session_tokens}:
 * <ul>
 *   <li><b>Minted (live row found):</b> the row's tenant MUST equal the resolved tenant
 *       (else 401 — cross-tenant session); the SERVER-RESOLVED {@code session_id} is
 *       exposed via {@link RequestContext#session()} with {@link RequestContext#isMintedSession()}
 *       true. Session-scoped handlers MUST use it and reject a differing client-supplied
 *       session id (ScratchHandler enforces this with a 403) — this is what makes "a
 *       session-S1 token cannot act as session-S2 within one tenant" hold server-side.</li>
 *   <li><b>Non-live (absent or expired row):</b> 401 ({@code session_not_minted}). Phase E
 *       retired the transitional bootstrap path that exposed the raw header as a bare
 *       session id — that was the victim-impersonation vector. Callers MUST mint a session
 *       token (the MCP lifespan does so at session start). A request with NO session header
 *       still proceeds tenant-scoped (session-scoped handlers are not exercised).</li>
 * </ul>
 *
 * <p>The resolved principal is published via the thread-confined {@link RequestContext}
 * (NOT {@code HttpExchange} attributes, which are HttpContext-shared and would race
 * across the server's virtual-thread-per-request executor). It is cleared in a
 * {@code finally} after dispatch.
 *
 * <p>Secret handling: raw tokens are never compared byte-by-byte in app code; they are
 * SHA-256 hashed and resolved by an indexed PK equality, so there is no single-secret
 * timing oracle. 401 carries no {@code WWW-Authenticate} challenge.
 */
public final class AuthFilter extends Filter {

    private static final Logger log = LoggerFactory.getLogger(AuthFilter.class);

    /**
     * Reserved tenant-name sentinel ("*"). Phase E (nexus-gmiaf.32.5) retired its
     * any-tenant grant in this filter; it now survives only as a FORBIDDEN tenant name
     * that {@code TokenStore.rejectWildcard} refuses to mint or create, so no real tenant
     * can collide with the legacy sentinel.
     */
    public static final String BOOTSTRAP_ANY_TENANT = dev.nexus.service.db.TenantConstants.BOOTSTRAP_ANY_TENANT;

    private static final String BEARER_PREFIX = "Bearer ";
    private static final String TENANT_HEADER = "X-Nexus-Tenant";
    private static final String SESSION_HEADER = "X-Nexus-T1-Session";

    /**
     * Public alias of {@link RequestDeadline#REQUEST_DEADLINE_HEADER} (nexus-8hdg9
     * phase 5): {@code RequestDeadline} is package-private, and {@code
     * AuthFilterTest} lives one package up.
     */
    public static final String REQUEST_DEADLINE_HEADER = RequestDeadline.REQUEST_DEADLINE_HEADER;

    private final TokenCache tokenCache;
    private final TokenStore tokenStore;

    /**
     * Request embed-deadline budget (nexus-8hdg9 phase 2), resolved ONCE at
     * construction -- mirrors {@code LocalOnnxAdmission.fromEnv()}'s resolve-
     * once-at-boot shape rather than re-parsing the env on every request.
     * {@link RequestDeadline#newDeadlineNanos(long)} mints a fresh deadline
     * from this budget per request, for EVERY route, in {@link #doFilter}
     * (see {@code RequestDeadlineExceededException}'s javadoc for why this
     * is not upsert-specific).
     */
    private final long deadlineBudgetMs;

    /**
     * Hard ceiling on any request's embed budget (nexus-8hdg9 phase 3, review
     * carry-in T2 [24681]): {@link RequestDeadline#DEADLINE_MAX_MS_ENV},
     * resolved once at construction like {@link #deadlineBudgetMs}. Both the
     * client's header budget and the env default are clamped to it in
     * {@link RequestDeadline#resolveBudgetMs(String, long, long)}.
     */
    private final long deadlineMaxMs;

    public AuthFilter(TokenCache tokenCache, TokenStore tokenStore) {
        this(tokenCache, tokenStore, RequestDeadline.deadlineMsFromEnv(),
             RequestDeadline.deadlineMaxMsFromEnv());
    }

    /**
     * Test-support constructor (nexus-8hdg9 phase 2 review remediation, T2
     * {@code code-review-nexus-8hdg9-p2-5ce59b36d} [24650]): bypasses {@link
     * RequestDeadline#deadlineMsFromEnv()}'s real-process-env read so a test
     * can assert the WIRING -- that {@link RequestContext#deadlineNanos()}
     * carries the budget this constructor was given -- without mutating the
     * JVM's actual environment. Java offers no supported, non-reflective way
     * to set an env var for a running process, so this constructor is the
     * injectable-resolver seam {@code deadlineMsFromEnv(Function)} already
     * gives {@link AuthFilter} itself; a same-package test would call that
     * resolver directly, but {@code AuthFilterTest} lives in {@code
     * dev.nexus.service}, one package up, where a package-private overload
     * is not visible -- hence public, unlike {@code LocalOnnxAdmission}'s
     * same-package-private injection points. The public two-arg constructor
     * above delegates here with the real env-resolved budget; production
     * code has exactly one construction path
     * ({@code NexusService} → the two-arg form), this constructor exists
     * for tests only.
     *
     * @param deadlineBudgetMs the embed-deadline budget in milliseconds --
     *                         what {@link RequestDeadline#deadlineMsFromEnv()}
     *                         would have returned for some {@code
     *                         NX_EMBED_DEADLINE_MS} value
     */
    public AuthFilter(TokenCache tokenCache, TokenStore tokenStore, long deadlineBudgetMs) {
        this(tokenCache, tokenStore, deadlineBudgetMs, RequestDeadline.deadlineMaxMsFromEnv());
    }

    /**
     * Test-support constructor with an explicit hard ceiling (nexus-8hdg9
     * phase 3 carry-in); same rationale as the 3-arg form.
     *
     * @param deadlineMaxMs the ceiling in milliseconds -- what {@link
     *                      RequestDeadline#deadlineMaxMsFromEnv()} would have
     *                      returned for some {@code NX_EMBED_DEADLINE_MAX_MS}
     */
    public AuthFilter(TokenCache tokenCache, TokenStore tokenStore, long deadlineBudgetMs,
                      long deadlineMaxMs) {
        this.tokenCache = Objects.requireNonNull(tokenCache, "tokenCache");
        this.tokenStore = Objects.requireNonNull(tokenStore, "tokenStore");
        this.deadlineBudgetMs = deadlineBudgetMs;
        this.deadlineMaxMs = deadlineMaxMs;
    }

    @Override
    public String description() {
        return "Bearer token→tenant resolution + per-session verification";
    }

    @Override
    public void doFilter(HttpExchange exchange, Chain chain) throws IOException {
        // 1. Extract the bearer.
        String authHeader = exchange.getRequestHeaders().getFirst("Authorization");
        if (authHeader == null || !authHeader.startsWith(BEARER_PREFIX)) {
            reject(exchange, "bad_token");
            return;
        }
        String rawToken = authHeader.substring(BEARER_PREFIX.length());
        if (rawToken.isBlank()) {
            reject(exchange, "bad_token");
            return;
        }

        // 2. Resolve the token's tenant_id SERVER-SIDE (Decision 1). The resolution also
        // carries the root/operator flag (nexus-e4130) so the admin surface can scope
        // cross-tenant operations to the root token without a second lookup.
        String credentialHash = TokenHashing.sha256Hex(rawToken);
        Optional<dev.nexus.service.db.TokenCache.Resolved> resolved =
            tokenCache.resolve(credentialHash);
        if (resolved.isEmpty()) {
            reject(exchange, "unresolved_token");  // missing / revoked / expired
            return;
        }
        String tokenTenant = resolved.get().tenantId();
        boolean isOperator = resolved.get().isRoot();
        String scope = resolved.get().scope();

        // nexus-868dq (choke point, mirrors the nexus-45ykb pattern): a MINT-scoped
        // credential exists to call POST /v1/data-tokens/mint and nothing else — it
        // carries no data-path tenant authority and is rejected on every admin route
        // (RDR-005 pin: "rejected on ALL admin routes"). Enforced here so every
        // handler behind this filter is covered without per-handler opt-in; the
        // admin handler's own scope guard is defense-in-depth layer 2. Exact
        // segment match ("/v1/data-tokens" or "/v1/data-tokens/..."), not a raw
        // prefix — "/v1/data-tokens-evil" must not slip through.
        if (dev.nexus.service.db.TokenStore.SCOPE_MINT.equals(scope)
                || dev.nexus.service.db.TokenStore.SCOPE_MINT_LOCKED.equals(scope)) {
            String path = exchange.getRequestURI().getPath();
            boolean mintSurface = path.equals("/v1/data-tokens")
                || path.startsWith("/v1/data-tokens/");
            if (!mintSurface) {
                log.debug("event=auth_rejected path={} reason=mint_scope_data_path_forbidden", path);
                sendError(exchange, 403, "forbidden");
                return;
            }
        }

        // nexus-45ykb (defense in depth): deny any token that resolves to the wildcard
        // sentinel tenant. '*' is a reserved name that token minting, `nx tenant create`,
        // and catalog owner-registration all refuse, so it can NEVER be a registered
        // catalog_owners principal. A token bound to '*' is therefore a legacy grandfathered
        // credential with no legitimate owner; letting it operate would write ghost data
        // under an unregistered tenant. Phase E (nexus-gmiaf.32.5) already retired the
        // any-tenant GRANT (the header is no longer honored); this closes the residual
        // legacy-credential vector by refusing the sentinel tenant outright. Validated
        // against catalog_owners by construction (no DB read): '*' is never an owner.
        if (BOOTSTRAP_ANY_TENANT.equals(tokenTenant)) {
            reject(exchange, "wildcard_sentinel_tenant");
            return;
        }

        // Bound token (Phase E nexus-gmiaf.32.5): the token's tenant_id is authoritative;
        // the client X-Nexus-Tenant header is never trusted (logged at debug on mismatch).
        // The transitional wildcard ("*") any-tenant grant is retired — no token crosses
        // tenants.
        String tenant = tokenTenant;
        String claimedTenant = exchange.getRequestHeaders().getFirst(TENANT_HEADER);
        if (claimedTenant != null && !claimedTenant.equals(tenant)) {
            log.debug("event=tenant_header_ignored claimed={} resolved={} path={}",
                      claimedTenant, tenant, exchange.getRequestURI().getPath());
        }

        // 3. Per-session verification (Decision 2), require-minted.
        String sessionId = null;
        boolean mintedSession = false;
        String sessionHeader = exchange.getRequestHeaders().getFirst(SESSION_HEADER);
        if (sessionHeader != null && !sessionHeader.isBlank()) {
            Optional<SessionPrincipal> minted =
                tokenStore.resolveSession(TokenHashing.sha256Hex(sessionHeader));
            if (minted.isPresent()) {
                SessionPrincipal sp = minted.get();
                if (!sp.tenantId().equals(tenant)) {
                    reject(exchange, "cross_tenant_session");
                    return;
                }
                sessionId = sp.sessionId();  // server-resolved
                mintedSession = true;        // handlers MUST use this, not a body value
            } else {
                // Phase E (nexus-gmiaf.32.5) require-minted: a present-but-non-live
                // session token (absent or expired) is a 401. The transitional bootstrap
                // path that degraded to a bare session id is retired — it was the
                // victim-impersonation vector (a bare id could collide with another
                // session's resolved id). Callers MUST mint a session token.
                reject(exchange, "session_not_minted");
                return;
            }
        }

        // 4. Publish the principal thread-confined; clear after dispatch.
        RequestContext.set(new RequestContext.Principal(
            tenant, sessionId, mintedSession, isOperator, scope, credentialHash));
        // nexus-8hdg9 phase 2: mint the request's embed deadline alongside the
        // principal, for EVERY route, from the budget resolved once at construction.
        // Phase 5: a client that declares its own budget via the advisory
        // X-Nexus-Request-Deadline-Ms header replaces the env default outright;
        // absent or malformed falls back to the default. Either is clamped to the
        // NX_EMBED_DEADLINE_MAX_MS hard ceiling (phase 3 carry-in).
        // Cleared together with the principal in the finally below.
        long budgetMs = RequestDeadline.resolveBudgetMs(
            exchange.getRequestHeaders().getFirst(RequestDeadline.REQUEST_DEADLINE_HEADER),
            deadlineBudgetMs, deadlineMaxMs);
        RequestContext.setDeadlineNanos(RequestDeadline.newDeadlineNanos(budgetMs));
        try {
            chain.doFilter(exchange);
        } finally {
            RequestContext.clear();
            RequestContext.clearDeadline();
        }
    }

    private void reject(HttpExchange exchange, String reason) throws IOException {
        log.debug("event=auth_rejected path={} reason={}",
                  exchange.getRequestURI().getPath(), reason);
        sendError(exchange, 401, "unauthorized");
    }

    private void sendError(HttpExchange exchange, int status, String message) throws IOException {
        HttpUtil.send(exchange, status, "{\"error\":\"" + message + "\"}");
    }
}
