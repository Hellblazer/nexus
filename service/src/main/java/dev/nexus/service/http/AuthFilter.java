package dev.nexus.service.http;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.SessionPrincipal;
import dev.nexus.service.db.TokenCache;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.db.TokenStore;
import com.sun.net.httpserver.Filter;
import com.sun.net.httpserver.HttpExchange;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.slf4j.event.Level;

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

    // MDC keys; logback.xml prints them as [cid=... tenant=... session=...].
    private static final String MDC_CORRELATION_ID = "correlation_id";
    private static final String MDC_TENANT = "tenant";
    private static final String MDC_SESSION = "session";

    /** RDR-253 Step 2: the header an identity token travels in, beside the session token's. */
    public static final String IDENTITY_HEADER = "X-Nexus-Identity-Token";

    /**
     * The routes that mint or keep a binding: an expired identity token is ignored there (treated as absent), not a 401.
     * The tenant credential's binds never use it; a data-credential {@code refresh} or {@code fork} does (RDR-253, Sam
     * 2026-11-19, option A), and with the expired token ignored the fence answers 403 {@code identity_token_required}.
     */
    private static final java.util.regex.Pattern IDENTITY_BIND_ROUTE =
        java.util.regex.Pattern.compile("^/v1/identity/i[0-9]+/(bind|renew)$");

    /**
     * Public alias of {@link RequestDeadline#REQUEST_DEADLINE_HEADER} (nexus-8hdg9
     * phase 5): {@code RequestDeadline} is package-private, and {@code
     * AuthFilterTest} lives one package up.
     */
    public static final String REQUEST_DEADLINE_HEADER = RequestDeadline.REQUEST_DEADLINE_HEADER;

    private final TokenCache tokenCache;
    private final TokenStore tokenStore;

    /**
     * RDR-204 Phase 1 (bead nexus-ft04v.3): the per-tenant, at-most-once-per-
     * process ghost-sweep + dormant-marking trigger, fired from {@link
     * #doFilter} once a request's tenant is resolved — see that call site's
     * comment for why THIS filter is the chosen choke point. {@code null} for
     * every constructor that predates this wiring (the test-support overloads
     * below); {@link #doFilter} skips the call entirely when {@code null}
     * rather than defaulting to some other behavior, so an {@code AuthFilter}
     * built without one is simply not wired to the sweep, same as before this
     * bead.
     */
    private final CatalogRepository catalogRepo;

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
             RequestDeadline.deadlineMaxMsFromEnv(), null);
    }

    /**
     * Production constructor carrying the RDR-204 Phase 1 (bead nexus-ft04v.3)
     * ghost-sweep trigger. {@code NexusService} is the one call site that uses
     * this form; every other constructor here predates the sweep and passes
     * {@code null} for it (see {@link #catalogRepo}'s own javadoc).
     */
    public AuthFilter(TokenCache tokenCache, TokenStore tokenStore, CatalogRepository catalogRepo) {
        this(tokenCache, tokenStore, RequestDeadline.deadlineMsFromEnv(),
             RequestDeadline.deadlineMaxMsFromEnv(), catalogRepo);
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
     * above delegates here with the real env-resolved budget; this constructor
     * exists for tests only, and (like the two-arg form) carries no {@code
     * CatalogRepository} — see {@link #catalogRepo}.
     *
     * @param deadlineBudgetMs the embed-deadline budget in milliseconds --
     *                         what {@link RequestDeadline#deadlineMsFromEnv()}
     *                         would have returned for some {@code
     *                         NX_EMBED_DEADLINE_MS} value
     */
    public AuthFilter(TokenCache tokenCache, TokenStore tokenStore, long deadlineBudgetMs) {
        this(tokenCache, tokenStore, deadlineBudgetMs, RequestDeadline.deadlineMaxMsFromEnv(), null);
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
        this(tokenCache, tokenStore, deadlineBudgetMs, deadlineMaxMs, null);
    }

    private AuthFilter(TokenCache tokenCache, TokenStore tokenStore, long deadlineBudgetMs,
                        long deadlineMaxMs, CatalogRepository catalogRepo) {
        this.tokenCache = Objects.requireNonNull(tokenCache, "tokenCache");
        this.tokenStore = Objects.requireNonNull(tokenStore, "tokenStore");
        this.deadlineBudgetMs = deadlineBudgetMs;
        this.deadlineMaxMs = deadlineMaxMs;
        this.catalogRepo = catalogRepo;
    }

    @Override
    public String description() {
        return "Bearer token→tenant resolution + per-session verification";
    }

    @Override
    public void doFilter(HttpExchange exchange, Chain chain) throws IOException {
        // RDR-167 Step 3 (nexus-dngtz.1.5): the correlation id is accepted or generated and
        // set on the response BEFORE anything can send a response, so every path below (the
        // 401 early returns included) echoes it and logs under it. Everything after the
        // accept sits inside the try, so the MDC is cleared on every exit. Production runs one
        // virtual thread per request (NexusService: newVirtualThreadPerTaskExecutor), so a
        // ThreadLocal cannot outlive a request there; the clear is defense for any other
        // executor, not a fix for a pool leak.
        String correlationId = CorrelationId.acceptOrGenerate(
            exchange.getRequestHeaders().getFirst(CorrelationId.HEADER));
        long startNanos = System.nanoTime();
        Throwable thrown = null;
        try {
            // Needs no escape: CorrelationId admits only [A-Za-z0-9._-]{1,64}.
            MDC.put(MDC_CORRELATION_ID, correlationId);
            exchange.getResponseHeaders().set(CorrelationId.HEADER, correlationId);
            authenticateAndDispatch(exchange, chain);
        } catch (Throwable t) {
            // Precise rethrow: t is not reassigned, so this compiles under `throws IOException` and the
            // exception reaches the server unchanged. It is kept only to name its class in the line.
            thrown = t;
            throw t;
        } finally {
            logRequestFailed(exchange, thrown, startNanos);   // never throws; before the clears so the line carries the ids
            RequestContext.clear();
            RequestContext.clearDeadline();
            MDC.clear();
        }
    }

    /**
     * RDR-167 Step 3 (nexus-dngtz.1.6): one line, at WARN (a sent 404 with nothing thrown at INFO), for a request that failed or was never answered:
     * the response status is 400 or above, nothing was sent ({@code getResponseCode() == -1}, printed as
     * {@code none}), or the chain threw (a handler that sent 200 and then threw while writing the body
     * counts). The exception's class name is printed, never its message or stack trace: both can carry
     * client text. The path has no query string, as in {@code event=auth_rejected}. A failure to log
     * is swallowed: it must not replace the request's own exception, and the caller clears the request
     * context behind this call either way.
     */
    private void logRequestFailed(HttpExchange exchange, Throwable thrown, long startNanos) {
        try {
            int status = exchange.getResponseCode();
            if (status >= 400 || status == -1 || thrown != null) {
                // Sam's ruling 2026-11-17 (RDR-167 Step 3): a 404 is a normal "not found" answer on
                // lookup routes, so a sent 404 with nothing thrown logs at INFO; every other failure WARN.
                Level level = status == 404 && thrown == null ? Level.INFO : Level.WARN;
                log.atLevel(level).log("event=request_failed method={} path={} status={} exception={} duration_ms={}",
                    LogText.escapeControl(exchange.getRequestMethod()),
                    LogText.escapeControl(exchange.getRequestURI().getPath()),
                    status == -1 ? "none" : Integer.toString(status),
                    thrown == null ? "none" : thrown.getClass().getName(),
                    (System.nanoTime() - startNanos) / 1_000_000L);
            }
        } catch (Throwable loggingFailure) {
            // Deliberately empty: see the method comment.
        }
    }

    private void authenticateAndDispatch(HttpExchange exchange, Chain chain) throws IOException {
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
        // The tenant goes in MDC as soon as the token names it, so the request_failed line for a 403
        // scope refusal or a cross-tenant / unminted-session 401 carries it. An unknown token has no
        // tenant and prints none. The session is put only once it is verified (step 3).
        // mdcValue: only the printed text is escaped; routing and every comparison keep the raw tenant.
        MDC.put(MDC_TENANT, LogText.mdcValue(tokenTenant));
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
                log.debug("event=auth_rejected path={} reason=mint_scope_data_path_forbidden",
                    LogText.escapeControl(path));
                sendError(exchange, 403, "forbidden");
                return;
            }
        }

        // nexus-r3ur5 (choke point, mirrors the nexus-868dq mint pattern): a
        // board-ci credential exists to write CI board posts via
        // POST /v1/tuples/out and NOTHING else — exact path AND method, not a
        // prefix (a GET on /v1/tuples/out, or any other tuples op such as
        // /v1/tuples/rd, carries no meaning for this scope and is refused
        // here so every route behind this filter is covered without
        // per-handler opt-in; TupleHandler's own scope guard is defense in
        // depth layer 2, same shape as the mint-surface check above).
        if (dev.nexus.service.db.TokenStore.SCOPE_BOARD_CI.equals(scope)) {
            String path = exchange.getRequestURI().getPath();
            boolean boardCiSurface = "POST".equalsIgnoreCase(exchange.getRequestMethod())
                && path.equals("/v1/tuples/out");
            if (!boardCiSurface) {
                log.debug("event=auth_rejected path={} method={} "
                        + "reason=board_ci_scope_surface_forbidden",
                        LogText.escapeControl(path), LogText.escapeControl(exchange.getRequestMethod()));
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
                      LogText.escapeControl(claimedTenant), tenant,
                      LogText.escapeControl(exchange.getRequestURI().getPath()));
        }

        // RDR-204 Phase 1 (bead nexus-ft04v.3): THIS is the single per-request
        // choke point where every /v1/* route resolves its tenant -- catalog
        // and vector requests included, but not only those -- so it is where
        // the per-tenant, once-per-process ghost sweep + dormant marking fires,
        // at whichever request happens to be this tenant's first since boot.
        // catalogRepo is null only for AuthFilter instances built by a
        // constructor that predates this wiring (see its own javadoc);
        // ensureGhostSweepRanOnce never throws (it catches and logs
        // internally), so this can never fail the request it rides on.
        if (catalogRepo != null) {
            catalogRepo.ensureGhostSweepRanOnce(tenant);
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

        // 3b. RDR-253 Step 2: an identity token names an identity and the generation it was minted for. It is
        // resolved here and published; whether that generation is still the identity's current one is the
        // fence's question, not this filter's (a read is never checked, and a superseded generation's token stays
        // resolvable until it expires so the fence can answer 409 generation_superseded). An unknown or expired
        // token is a 401, which the client answers by re-reading its token file once.
        String identity = null;
        Integer identityGeneration = null;
        String identityHeader = exchange.getRequestHeaders().getFirst(IDENTITY_HEADER);
        if (identityHeader != null && !identityHeader.isBlank()) {
            Optional<dev.nexus.service.db.IdentityTokenStore.Principal> ip =
                tokenStore.identityTokens().resolve(TokenHashing.sha256Hex(identityHeader));
            if (ip.isPresent() && !ip.get().tenantId().equals(tenant)) {
                reject(exchange, "cross_tenant_identity_token");
                return;
            }
            if (ip.isPresent()) {
                identity = ip.get().identityId();
                identityGeneration = ip.get().generation();
            } else if (IDENTITY_BIND_ROUTE.matcher(exchange.getRequestURI().getPath()).matches()) {
                // An expired token is treated as absent here rather than 401: the tenant credential's binds never need
                // it, and a data-credential refresh or fork, which does (RDR-253 option A, no grace), is then refused by
                // the fence with 403 identity_token_required; that session rebinds on its next start.
                log.debug("event=identity_token_ignored path={} reason=expired_on_bind_route",
                          LogText.escapeControl(exchange.getRequestURI().getPath()));
            } else {
                reject(exchange, "identity_token_expired");
                return;
            }
        }

        // 4. Publish the principal thread-confined; clear after dispatch.
        RequestContext.set(new RequestContext.Principal(
            tenant, sessionId, mintedSession, isOperator, scope, credentialHash, identity, identityGeneration));
        // Tenant (put at resolution) and session ids only: never the token or the credential hash.
        if (sessionId != null) {
            // A client chose this id (/v1/sessions/start takes any non-blank one): escape it for the prefix.
            MDC.put(MDC_SESSION, LogText.mdcValue(sessionId));
        }
        // nexus-8hdg9 phase 2: mint the request's embed deadline alongside the
        // principal, for EVERY route, from the budget resolved once at construction.
        // Phase 5: a client that declares its own budget via the advisory
        // X-Nexus-Request-Deadline-Ms header replaces the env default outright;
        // absent or malformed falls back to the default. Either is clamped to the
        // NX_EMBED_DEADLINE_MAX_MS hard ceiling (phase 3 carry-in).
        // Cleared together with the principal in doFilter's finally.
        long budgetMs = RequestDeadline.resolveBudgetMs(
            exchange.getRequestHeaders().getFirst(RequestDeadline.REQUEST_DEADLINE_HEADER),
            deadlineBudgetMs, deadlineMaxMs);
        RequestContext.setDeadlineNanos(RequestDeadline.newDeadlineNanos(budgetMs));
        chain.doFilter(exchange);
    }

    private void reject(HttpExchange exchange, String reason) throws IOException {
        log.debug("event=auth_rejected path={} reason={}",
                  LogText.escapeControl(exchange.getRequestURI().getPath()), reason);
        sendError(exchange, 401, "unauthorized");
    }

    private void sendError(HttpExchange exchange, int status, String message) throws IOException {
        HttpUtil.send(exchange, status, "{\"error\":\"" + message + "\"}");
    }
}
