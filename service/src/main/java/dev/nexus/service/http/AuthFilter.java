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
 * {@code tenant_id} determines the tenant:
 * <ul>
 *   <li><b>Bound token</b> (concrete tenant_id): that tenant is authoritative; the
 *       client {@code X-Nexus-Tenant} header is IGNORED (logged at debug on mismatch).</li>
 *   <li><b>Bootstrap token</b> (tenant_id = {@value #BOOTSTRAP_ANY_TENANT}): a
 *       transitional grandfathered credential (the legacy fixed NX_SERVICE_TOKEN seeded
 *       by Phase B; retired by Phase E nexus-gmiaf.32.5). It may act as ANY tenant, taken
 *       from the required {@code X-Nexus-Tenant} header (400 if absent) — exactly the
 *       Phase 1–4 posture, preserved ONLY for this one legacy credential so existing
 *       clients/tests keep working through the B→E window. Minted tokens are strictly
 *       bound; once the bootstrap token is retired, no token can cross tenants.</li>
 * </ul>
 *
 * <p><b>Decision 2 (per-session verification), transitional option (a).</b> If
 * {@code X-Nexus-T1-Session} is present, its value is hashed and looked up in
 * {@code session_tokens}:
 * <ul>
 *   <li><b>Minted (live row found):</b> the row's tenant MUST equal the resolved tenant
 *       (else 401 — cross-tenant session); the SERVER-RESOLVED {@code session_id} is
 *       exposed via {@link RequestContext#session()} with {@link RequestContext#isMintedSession()}
 *       true. Session-scoped handlers MUST use it and reject a differing client-supplied
 *       session id (ScratchHandler enforces this with a 403) — this is what makes "a
 *       session-S1 token cannot act as session-S2 within one tenant" hold server-side.
 *       Phase D (nexus-gmiaf.32.4) wires clients to mint and send these tokens.</li>
 *   <li><b>Bootstrap (no live row — absent or expired):</b> the raw header value is
 *       exposed as a bare session id (the pre-Decision-2 client-side-scoping posture).
 *       An expired minted token therefore degrades to its own token string as the
 *       session id, NEVER the victim's resolved session_id. Transitional: once Phase D
 *       mints session tokens universally and a require-minted flag flips, a non-live
 *       session token is a 401. No regression: t1.scratch stays tenant-GUC isolated.</li>
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

    /** Sentinel tenant_id marking the transitional grandfathered bootstrap token. */
    public static final String BOOTSTRAP_ANY_TENANT = "*";

    private static final String BEARER_PREFIX = "Bearer ";
    private static final String TENANT_HEADER = "X-Nexus-Tenant";
    private static final String SESSION_HEADER = "X-Nexus-T1-Session";

    private final TokenCache tokenCache;
    private final TokenStore tokenStore;

    public AuthFilter(TokenCache tokenCache, TokenStore tokenStore) {
        this.tokenCache = Objects.requireNonNull(tokenCache, "tokenCache");
        this.tokenStore = Objects.requireNonNull(tokenStore, "tokenStore");
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

        // 2. Resolve the token's tenant_id SERVER-SIDE (Decision 1).
        Optional<String> resolved = tokenCache.resolveTenant(TokenHashing.sha256Hex(rawToken));
        if (resolved.isEmpty()) {
            reject(exchange, "unresolved_token");  // missing / revoked / expired
            return;
        }
        String tokenTenant = resolved.get();

        String tenant;
        String claimedTenant = exchange.getRequestHeaders().getFirst(TENANT_HEADER);
        if (BOOTSTRAP_ANY_TENANT.equals(tokenTenant)) {
            // Transitional grandfathered token: tenant comes from the (required) header.
            if (claimedTenant == null || claimedTenant.isBlank()) {
                sendError(exchange, 400, "missing X-Nexus-Tenant header");
                return;
            }
            tenant = claimedTenant;
        } else {
            // Bound token: the token's tenant wins; client header is not trusted.
            tenant = tokenTenant;
            if (claimedTenant != null && !claimedTenant.equals(tenant)) {
                log.debug("event=tenant_header_ignored claimed={} resolved={} path={}",
                          claimedTenant, tenant, exchange.getRequestURI().getPath());
            }
        }

        // 3. Per-session verification (Decision 2), transitional option (a).
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
                sessionId = sessionHeader;   // transitional bootstrap bare id
            }
        }

        // 4. Publish the principal thread-confined; clear after dispatch.
        RequestContext.set(new RequestContext.Principal(tenant, sessionId, mintedSession));
        try {
            chain.doFilter(exchange);
        } finally {
            RequestContext.clear();
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
