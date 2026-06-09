package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.security.SecureRandom;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Base64;
import java.util.List;
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

    // ── Admin / lifecycle (RDR-152 bead nexus-gmiaf.32.3) ──────────────────────

    private static final SecureRandom RNG = new SecureRandom();
    private static final Base64.Encoder TOKEN_ENCODER = Base64.getUrlEncoder().withoutPadding();

    /** A freshly issued token: the raw secret (shown ONCE) and its stored hash. */
    public record IssuedToken(String rawToken, String tokenHash) {
    }

    /** A token registry row for listing (never carries the raw secret). */
    public record TokenInfo(String tokenHash, String tenantId, String label,
                            OffsetDateTime createdAt, OffsetDateTime expiresAt,
                            OffsetDateTime revokedAt) {
        /** active | revoked | expired, evaluated against {@code now}. */
        public String status(Instant now) {
            if (revokedAt != null) {
                return "revoked";
            }
            if (expiresAt != null && !expiresAt.toInstant().isAfter(now)) {
                return "expired";
            }
            return "active";
        }
    }

    private static void rejectWildcard(String tenant) {
        if (tenant == null || tenant.isBlank()) {
            throw new IllegalArgumentException("tenant must not be null or blank");
        }
        if (TenantConstants.BOOTSTRAP_ANY_TENANT.equals(tenant)) {
            throw new IllegalArgumentException(
                "tenant '*' is reserved for the transitional bootstrap token and cannot be minted");
        }
    }

    private static String newRawToken() {
        byte[] bytes = new byte[32];
        RNG.nextBytes(bytes);
        return TOKEN_ENCODER.encodeToString(bytes);
    }

    /**
     * Issue a new bound token for {@code tenant} (rejects the wildcard sentinel).
     *
     * @param tenant     the tenant to bind the token to (not {@code '*'})
     * @param label      optional human label (may be null)
     * @param ttlSeconds optional lifetime; null means no expiry
     * @return the issued token: raw secret (show once) + stored hash
     */
    public IssuedToken issueToken(String tenant, String label, Long ttlSeconds) {
        rejectWildcard(tenant);
        if (ttlSeconds != null && ttlSeconds <= 0) {
            throw new IllegalArgumentException("ttl_seconds must be positive");
        }
        String raw = newRawToken();
        String hash = TokenHashing.sha256Hex(raw);
        OffsetDateTime expiresAt = ttlSeconds == null
            ? null
            : OffsetDateTime.ofInstant(clock.instant().plusSeconds(ttlSeconds), ZoneOffset.UTC);
        dsl().insertInto(SERVICE_TOKENS)
            .columns(SERVICE_TOKENS.TOKEN_HASH, SERVICE_TOKENS.TENANT_ID,
                     SERVICE_TOKENS.LABEL, SERVICE_TOKENS.EXPIRES_AT)
            .values(hash, tenant, label, expiresAt)
            .execute();
        log.info("event=service_token_issued tenant={} label={} ttl={}", tenant, label, ttlSeconds);
        return new IssuedToken(raw, hash);
    }

    /**
     * Zero-downtime rotation: set {@code expires_at = now + grace} on every currently-live
     * token for {@code tenant}, then issue a fresh one. Old and new are BOTH valid through
     * the grace window; clients rediscover the new token via the lease the supervisor
     * publishes. Returns the newly issued token.
     *
     * @param tenant       the tenant to rotate (not {@code '*'})
     * @param graceSeconds overlap window before the old tokens expire
     */
    /** A rotation outcome: the freshly issued token + the old hashes now grace-expiring. */
    public record RotationResult(IssuedToken issued, List<String> expiredHashes) {
    }

    public RotationResult rotateTokens(String tenant, long graceSeconds) {
        rejectWildcard(tenant);
        if (graceSeconds <= 0) {
            throw new IllegalArgumentException("grace_seconds must be positive");
        }
        // ATOMIC (RDR-152 P5.3-C review): grace-expire the old rows AND issue the new one in
        // ONE transaction, so a crash can never leave the tenant with zero live tokens (the
        // exact zero-downtime guarantee Decision 3 promises). Returns the expired hashes so
        // the caller can invalidate their cache entries, making grace expiry precise rather
        // than stale-by-up-to-cache-TTL.
        OffsetDateTime graceDeadline =
            OffsetDateTime.ofInstant(clock.instant().plusSeconds(graceSeconds), ZoneOffset.UTC);
        return dsl().transactionResult(cfg -> {
            DSLContext tx = DSL.using(cfg);
            List<String> expired = tx.select(SERVICE_TOKENS.TOKEN_HASH)
                .from(SERVICE_TOKENS)
                .where(SERVICE_TOKENS.TENANT_ID.eq(tenant))
                .and(SERVICE_TOKENS.REVOKED_AT.isNull())
                .and(SERVICE_TOKENS.EXPIRES_AT.isNull().or(SERVICE_TOKENS.EXPIRES_AT.gt(graceDeadline)))
                .fetch(SERVICE_TOKENS.TOKEN_HASH);
            if (!expired.isEmpty()) {
                tx.update(SERVICE_TOKENS)
                    .set(SERVICE_TOKENS.EXPIRES_AT, graceDeadline)
                    .where(SERVICE_TOKENS.TOKEN_HASH.in(expired))
                    .execute();
            }
            String raw = newRawToken();
            String hash = TokenHashing.sha256Hex(raw);
            tx.insertInto(SERVICE_TOKENS)
                .columns(SERVICE_TOKENS.TOKEN_HASH, SERVICE_TOKENS.TENANT_ID, SERVICE_TOKENS.LABEL)
                .values(hash, tenant, "rotated")
                .execute();
            log.info("event=service_token_rotated tenant={} expiring_old={} grace_s={}",
                     tenant, expired.size(), graceSeconds);
            return new RotationResult(new IssuedToken(raw, hash), expired);
        });
    }

    /**
     * Revoke a token by full hash or a unique hash prefix. Sets {@code revoked_at = now}.
     *
     * @param selector full token_hash or a unique prefix
     * @return the full token_hash revoked, or empty if no unique match (caller invalidates
     *         the cache for the returned hash)
     */
    public Optional<String> revokeToken(String selector) {
        if (selector == null || selector.isBlank()) {
            return Optional.empty();
        }
        // Resolve selector to exactly one LIVE, non-bootstrap hash (exact match preferred,
        // else unique prefix). Excluding already-revoked rows avoids false-success on
        // re-revoke and prefix-shadowing by a stale revoked token; excluding the bootstrap
        // sentinel row prevents an authenticated caller from revoking the sole admin
        // credential into a total lockout (review P5.3-C).
        List<String> matches = dsl()
            .select(SERVICE_TOKENS.TOKEN_HASH)
            .from(SERVICE_TOKENS)
            .where(SERVICE_TOKENS.TOKEN_HASH.eq(selector)
                .or(SERVICE_TOKENS.TOKEN_HASH.startsWith(selector)))
            .and(SERVICE_TOKENS.REVOKED_AT.isNull())
            .and(SERVICE_TOKENS.TENANT_ID.ne(TenantConstants.BOOTSTRAP_ANY_TENANT))
            .fetch(SERVICE_TOKENS.TOKEN_HASH);
        String hash;
        if (matches.contains(selector)) {
            hash = selector;  // exact match wins even if it is also a prefix of others
        } else if (matches.size() == 1) {
            hash = matches.get(0);
        } else {
            return Optional.empty();  // not found, already revoked, bootstrap, or ambiguous
        }
        int updated = dsl().update(SERVICE_TOKENS)
            .set(SERVICE_TOKENS.REVOKED_AT,
                 OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC))
            .where(SERVICE_TOKENS.TOKEN_HASH.eq(hash))
            .and(SERVICE_TOKENS.REVOKED_AT.isNull())
            .execute();
        if (updated == 0) {
            return Optional.empty();  // raced to revoked between SELECT and UPDATE
        }
        log.info("event=service_token_revoked hash_prefix={}", hash.substring(0, Math.min(12, hash.length())));
        return Optional.of(hash);
    }

    /**
     * List token registry rows, optionally filtered by tenant. Never returns raw secrets.
     *
     * @param tenant tenant filter, or null for all tenants
     */
    public List<TokenInfo> listTokens(String tenant) {
        if (TenantConstants.BOOTSTRAP_ANY_TENANT.equals(tenant)) {
            throw new IllegalArgumentException("'*' is the reserved bootstrap sentinel, not a listable tenant");
        }
        // Always exclude the bootstrap sentinel row: it is the internal admin credential
        // and must not be enumerable by an authenticated caller (review P5.3-C).
        var base = dsl()
            .select(SERVICE_TOKENS.TOKEN_HASH, SERVICE_TOKENS.TENANT_ID, SERVICE_TOKENS.LABEL,
                    SERVICE_TOKENS.CREATED_AT, SERVICE_TOKENS.EXPIRES_AT, SERVICE_TOKENS.REVOKED_AT)
            .from(SERVICE_TOKENS)
            .where(SERVICE_TOKENS.TENANT_ID.ne(TenantConstants.BOOTSTRAP_ANY_TENANT));
        var filtered = (tenant == null || tenant.isBlank())
            ? base.orderBy(SERVICE_TOKENS.CREATED_AT)
            : base.and(SERVICE_TOKENS.TENANT_ID.eq(tenant)).orderBy(SERVICE_TOKENS.CREATED_AT);
        return filtered.fetch(r -> new TokenInfo(
            r.get(SERVICE_TOKENS.TOKEN_HASH),
            r.get(SERVICE_TOKENS.TENANT_ID),
            r.get(SERVICE_TOKENS.LABEL),
            r.get(SERVICE_TOKENS.CREATED_AT),
            r.get(SERVICE_TOKENS.EXPIRES_AT),
            r.get(SERVICE_TOKENS.REVOKED_AT)));
    }
}
