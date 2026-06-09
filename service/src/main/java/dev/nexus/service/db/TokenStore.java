package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Optional;

import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;
import static dev.nexus.service.jooq.nexus.Tables.SESSION_TOKENS;

/**
 * RDR-152 bead nexus-gmiaf.32.2 — credential-resolution reads against the
 * (RLS-off) {@code service_tokens} and {@code session_tokens} tables.
 *
 * <p>Unlike {@link TenantScope}, this store does NOT stamp a tenant GUC: these
 * tables are read by the auth layer BEFORE any tenant context exists (the presented
 * token is what resolves the tenant). It therefore uses a plain DataSource-backed
 * {@link DSLContext} (jOOQ borrows + returns a connection per query). Reads succeed
 * because Phase A leaves these tables un-RLS'd (see service-tokens-001-baseline.xml).
 *
 * <p>Expiry/revocation policy: {@link #lookupServiceToken(String)} filters out
 * MISSING and REVOKED rows but returns {@code expiresAt} so the cache can re-check
 * expiry against the (injected) clock on every hit (expiry is never stale).
 * {@link #resolveSession(String)} is uncached and filters MISSING and EXPIRED rows
 * itself using the same clock.
 */
public final class TokenStore {

    private static final Logger log = LoggerFactory.getLogger(TokenStore.class);

    private final DataSource dataSource;
    private final java.time.Clock clock;

    public TokenStore(DataSource dataSource, java.time.Clock clock) {
        this.dataSource = dataSource;
        this.clock = clock;
    }

    /** A live (non-revoked) service token: its tenant and optional expiry instant. */
    public record ServiceToken(String tenantId, Instant expiresAt) {
    }

    private DSLContext dsl() {
        return DSL.using(dataSource, SQLDialect.POSTGRES);
    }

    /**
     * Resolve a bearer token hash to its tenant, filtering MISSING and REVOKED rows.
     * Expiry is NOT applied here — the caller (cache) re-checks {@code expiresAt}
     * against the clock so a cached entry cannot outlive its expiry.
     *
     * @param tokenHash {@code sha256Hex} of the presented bearer
     * @return the live token (tenant + nullable expiry), or empty if missing/revoked
     */
    public Optional<ServiceToken> lookupServiceToken(String tokenHash) {
        if (tokenHash == null || tokenHash.isBlank()) {
            return Optional.empty();
        }
        var rec = dsl()
            .select(SERVICE_TOKENS.TENANT_ID, SERVICE_TOKENS.EXPIRES_AT)
            .from(SERVICE_TOKENS)
            .where(SERVICE_TOKENS.TOKEN_HASH.eq(tokenHash))
            .and(SERVICE_TOKENS.REVOKED_AT.isNull())
            .fetchOne();
        if (rec == null) {
            return Optional.empty();
        }
        OffsetDateTime exp = rec.get(SERVICE_TOKENS.EXPIRES_AT);
        return Optional.of(new ServiceToken(
            rec.get(SERVICE_TOKENS.TENANT_ID),
            exp == null ? null : exp.toInstant()));
    }

    /**
     * Resolve a session token hash to its (tenant, session), filtering MISSING and
     * EXPIRED rows. session_tokens has no revoked_at (DELETE-on-close + expiry only).
     *
     * @param sessionTokenHash {@code sha256Hex} of the presented X-Nexus-T1-Session
     * @return the verified principal, or empty if missing/expired
     */
    public Optional<SessionPrincipal> resolveSession(String sessionTokenHash) {
        if (sessionTokenHash == null || sessionTokenHash.isBlank()) {
            return Optional.empty();
        }
        var rec = dsl()
            .select(SESSION_TOKENS.TENANT_ID, SESSION_TOKENS.SESSION_ID, SESSION_TOKENS.EXPIRES_AT)
            .from(SESSION_TOKENS)
            .where(SESSION_TOKENS.SESSION_TOKEN_HASH.eq(sessionTokenHash))
            .fetchOne();
        if (rec == null) {
            return Optional.empty();
        }
        OffsetDateTime exp = rec.get(SESSION_TOKENS.EXPIRES_AT);  // NOT NULL by schema
        if (exp == null || !exp.toInstant().isAfter(clock.instant())) {
            return Optional.empty();  // expired
        }
        return Optional.of(new SessionPrincipal(
            rec.get(SESSION_TOKENS.TENANT_ID),
            rec.get(SESSION_TOKENS.SESSION_ID)));
    }

    /**
     * Transitional bootstrap (superseded by Phase E nexus-gmiaf.32.5): ensure a
     * service_tokens row exists for a legacy fixed {@code NX_SERVICE_TOKEN} so the
     * single-token clients started by the storage-service supervisor (gmiaf.30) keep
     * working through the B→E window. Idempotent: inserts under {@code tenantId} only
     * if the hash is absent. Never sets expiry/revocation.
     *
     * @param rawToken the legacy raw bearer (no-op if null/blank)
     * @param tenantId the tenant to bind it to (typically "default")
     */
    public void ensureBootstrapToken(String rawToken, String tenantId) {
        if (rawToken == null || rawToken.isBlank()) {
            return;
        }
        String hash = TokenHashing.sha256Hex(rawToken);
        int inserted = dsl()
            .insertInto(SERVICE_TOKENS)
            .columns(SERVICE_TOKENS.TOKEN_HASH, SERVICE_TOKENS.TENANT_ID, SERVICE_TOKENS.LABEL)
            .values(hash, tenantId, "bootstrap-legacy-token")
            .onConflict(SERVICE_TOKENS.TOKEN_HASH)
            .doNothing()
            .execute();
        if (inserted > 0) {
            log.info("event=bootstrap_token_seeded tenant={}", tenantId);
        }
    }

    static OffsetDateTime nowUtc() {
        return OffsetDateTime.now(ZoneOffset.UTC);
    }
}
