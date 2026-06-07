package dev.nexus.service.http;

import com.sun.net.httpserver.Filter;
import com.sun.net.httpserver.HttpExchange;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.security.MessageDigest;
import java.util.Objects;

/**
 * HTTP filter: validates {@code Authorization: Bearer <token>} and extracts
 * {@code X-Nexus-Tenant} on all routes that pass through it.
 *
 * <p><b>Bootstrap posture (Phase 1–4):</b> a single shared {@code NX_SERVICE_TOKEN}
 * authenticates ALL clients. Any token holder can claim ANY tenant by setting
 * {@code X-Nexus-Tenant}. DB-layer RLS is enforced but trusts the client to name
 * the correct tenant. Per-tenant/session credentials and token lifecycle management
 * land in Phase 5 (bead nexus-gmiaf.32). <b>Do NOT deploy in shared or
 * multi-principal environments before bead .32 ships.</b>
 *
 * <p>Auth model (Phase 1–4 bootstrap): single fixed bearer token loaded from
 * {@code NX_SERVICE_TOKEN} env var or service config. Full lifecycle (rotation,
 * per-tenant tokens) is Phase 5 (bead .32).
 *
 * <p>Security properties:
 * <ul>
 *   <li>Constant-time comparison via {@link MessageDigest#isEqual} to prevent
 *       timing-based token enumeration.</li>
 *   <li>401 on missing or incorrect token (no WWW-Authenticate challenge to
 *       avoid leaking token scheme details).</li>
 *   <li>400 on missing {@code X-Nexus-Tenant} header (auth succeeded but
 *       tenant context is required).</li>
 *   <li>Tenant value written to exchange attribute {@code nexus.tenant} for
 *       downstream handlers.</li>
 * </ul>
 */
public final class AuthFilter extends Filter {

    private static final Logger log = LoggerFactory.getLogger(AuthFilter.class);

    /** Exchange attribute key carrying the validated tenant principal. */
    public static final String ATTR_TENANT = "nexus.tenant";

    private final byte[] expectedTokenBytes;

    public AuthFilter(String expectedToken) {
        Objects.requireNonNull(expectedToken, "expectedToken must not be null");
        this.expectedTokenBytes = expectedToken.getBytes(java.nio.charset.StandardCharsets.UTF_8);
    }

    @Override
    public String description() {
        return "Bearer token auth + X-Nexus-Tenant extraction";
    }

    @Override
    public void doFilter(HttpExchange exchange, Chain chain) throws IOException {
        // 1. Validate Authorization: Bearer <token>
        String authHeader = exchange.getRequestHeaders().getFirst("Authorization");
        if (!isValidBearer(authHeader)) {
            log.debug("event=auth_rejected path={} reason=bad_token",
                      exchange.getRequestURI().getPath());
            HttpUtil.send(exchange, 401, "{\"error\":\"unauthorized\"}");
            return;
        }

        // 2. Extract X-Nexus-Tenant (required on all authenticated routes)
        String tenant = exchange.getRequestHeaders().getFirst("X-Nexus-Tenant");
        if (tenant == null || tenant.isBlank()) {
            log.debug("event=tenant_missing path={}", exchange.getRequestURI().getPath());
            HttpUtil.send(exchange, 400, "{\"error\":\"missing X-Nexus-Tenant header\"}");
            return;
        }

        // 3. Stamp tenant into exchange attributes for downstream handlers
        exchange.setAttribute(ATTR_TENANT, tenant);

        chain.doFilter(exchange);
    }

    /**
     * Constant-time bearer token validation.
     *
     * @param authHeader the raw {@code Authorization} header value (may be null)
     * @return true iff the header is {@code "Bearer <expectedToken>"}
     */
    private boolean isValidBearer(String authHeader) {
        if (authHeader == null || !authHeader.startsWith("Bearer ")) {
            return false;
        }
        byte[] provided = authHeader.substring(7)
                                    .getBytes(java.nio.charset.StandardCharsets.UTF_8);
        // MessageDigest.isEqual is constant-time: prevents timing oracle
        return MessageDigest.isEqual(provided, expectedTokenBytes);
    }
}
