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

    // ── Scope vocabulary (nexus-868dq, conexus RDR-005 A1) ────────────────────
    //
    // Server-assigned; NEVER derived from the client-supplied label (a label-
    // derived privilege would let any /v1/service-tokens/issue caller
    // self-escalate by crafting a label). The DB CHECK constraint mirrors this
    // set (service-tokens-003, extended by service-tokens-004 for mint-locked).

    /** The single operator credential (bootstrap). Cross-tenant admin. */
    public static final String SCOPE_ROOT = "root";
    /** Ordinary per-tenant bearer — the default; every pre-868dq row is this. */
    public static final String SCOPE_TENANT = "tenant";
    /** Mint-only credential (conexus edge): may ONLY call POST /v1/data-tokens/mint. */
    public static final String SCOPE_MINT = "mint";
    /** Tenant-locked mint credential (RDR-005 2a, nexus-xidcq): like SCOPE_MINT but may
     *  ONLY mint data tokens for its OWN bound tenant — no cross-tenant mint. */
    public static final String SCOPE_MINT_LOCKED = "mint-locked";
    /** Short-TTL per-tenant data token minted by a mint credential. */
    public static final String SCOPE_DATA = "data";

    private static final java.util.Set<String> VALID_SCOPES =
        java.util.Set.of(SCOPE_ROOT, SCOPE_TENANT, SCOPE_MINT, SCOPE_MINT_LOCKED, SCOPE_DATA);

    /**
     * A live (non-revoked) service token: its tenant, optional expiry instant, and
     * its server-assigned {@code scope}. {@code isRoot} — the operator privilege the
     * {@link dev.nexus.service.http.AuthFilter} threads to the admin surface
     * (nexus-e4130) — now derives from {@code scope == 'root'}, NOT from the label:
     * labels are client-supplied on issue, scope is server-assigned only
     * (nexus-868dq). The root row keeps its {@link #ROOT_TOKEN_LABEL} marker solely
     * for the single-root unique index and the lifecycle protections keyed on it.
     */
    public record ServiceToken(String tenantId, Instant expiresAt, String scope) {
        public boolean isRoot() {
            return SCOPE_ROOT.equals(scope);
        }
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
            .select(SERVICE_TOKENS.TENANT_ID, SERVICE_TOKENS.EXPIRES_AT, SERVICE_TOKENS.SCOPE)
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
            exp == null ? null : exp.toInstant(),
            rec.get(SERVICE_TOKENS.SCOPE)));
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
     * Label of the persistent root token (gmiaf.32.5), seeded by Main from the
     * provisioned {@code NX_SERVICE_TOKEN}. It re-keys the lockout protection that
     * formerly relied on the wildcard sentinel: the root credential is protected from
     * {@code revokeToken} (no self-lockout), excluded from {@code listTokens}
     * enumeration, and left untouched by {@code rotateTokens}'s expiry sweep — all keyed
     * on this label rather than {@code tenant_id = '*'} (which is retired). The root token
     * is now a BOUND default-tenant row; only this label distinguishes it from ordinary
     * default-tenant tokens.
     */
    public static final String ROOT_TOKEN_LABEL = "bootstrap-legacy-token";

    /**
     * Seed the persistent root token (Phase E nexus-gmiaf.32.5): ensure a service_tokens
     * row exists for the provisioned {@code NX_SERVICE_TOKEN}, BOUND to {@code tenantId}
     * (the default tenant) with the {@link #ROOT_TOKEN_LABEL} marker. Idempotent: inserts
     * only if the hash is absent. Never sets expiry/revocation.
     *
     * @param rawToken the root raw bearer (no-op if null/blank)
     * @param tenantId the tenant to bind it to (the default tenant)
     */
    public void ensureBootstrapToken(String rawToken, String tenantId) {
        if (rawToken == null || rawToken.isBlank()) {
            return;
        }
        String hash = TokenHashing.sha256Hex(rawToken);
        // Scope set explicitly (not via the column default): root provisioning must
        // never silently change if the default ever does (nexus-868dq).
        int inserted = dsl()
            .insertInto(SERVICE_TOKENS)
            .columns(SERVICE_TOKENS.TOKEN_HASH, SERVICE_TOKENS.TENANT_ID,
                     SERVICE_TOKENS.LABEL, SERVICE_TOKENS.SCOPE)
            .values(hash, tenantId, ROOT_TOKEN_LABEL, SCOPE_ROOT)
            .onConflict(SERVICE_TOKENS.TOKEN_HASH)
            .doNothing()
            .execute();
        if (inserted > 0) {
            log.info("event=root_token_seeded tenant={}", tenantId);
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
                            String scope, OffsetDateTime createdAt,
                            OffsetDateTime expiresAt, OffsetDateTime revokedAt) {
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
                "tenant '*' is a reserved sentinel and cannot be used");
        }
    }

    /**
     * Reject minting a token under the reserved root label. The lockout protection
     * (revoke-refused / list-excluded / rotate-expiry-skip) keys on
     * {@link #ROOT_TOKEN_LABEL}; without this guard an authenticated caller could mint a
     * token carrying that label and inherit those protections — an irrevocable, invisible,
     * non-rotating token (P5.3-E review). Only the internal {@code ensureBootstrapToken}
     * seeder may use the root label.
     */
    private static void rejectRootLabel(String label) {
        if (ROOT_TOKEN_LABEL.equals(label)) {
            throw new IllegalArgumentException(
                "label '" + ROOT_TOKEN_LABEL + "' is reserved for the root token");
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
        return issueToken(tenant, label, ttlSeconds, SCOPE_TENANT);
    }

    /**
     * Issue a new bound token for {@code tenant} with an explicit server-assigned
     * {@code scope} (nexus-868dq). Scope is validated against the vocabulary the
     * DB CHECK also enforces; callers decide WHO may request which scope (e.g.
     * {@code TokenAdminHandler} restricts {@code mint} issuance to the operator,
     * and only the data-token mint endpoint issues {@code data}).
     */
    public IssuedToken issueToken(String tenant, String label, Long ttlSeconds, String scope) {
        rejectWildcard(tenant);
        rejectRootLabel(label);
        if (!VALID_SCOPES.contains(scope)) {
            throw new IllegalArgumentException(
                "scope must be one of " + VALID_SCOPES + ", got: " + scope);
        }
        // Gate-A review (nexus-868dq): the single-root DB invariant
        // (idx_service_tokens_single_root, service-tokens-002) keys on the LABEL,
        // but the PRIVILEGE now keys on scope — issuing scope='root' under an
        // ordinary label would mint a SECOND operator credential that evades
        // every label-keyed lockout (revocable, enumerable, rotate-swept root).
        // Root is seeded exclusively by ensureBootstrapToken; mirror
        // rejectRootLabel at this class boundary, not just in the one handler.
        if (SCOPE_ROOT.equals(scope)) {
            throw new IllegalArgumentException(
                "scope 'root' may not be issued via issueToken; the root credential "
                + "is seeded exclusively by ensureBootstrapToken");
        }
        // Gate-A critique (nexus-868dq): RDR-005 pin iii defers per-tenant bulk
        // revoke on the premise that EVERY data token drains by TTL. A scope='data'
        // row with no expiry would be a permanent full-data-authority credential —
        // silently invalidating that deferral's justification. Enforced here, not
        // just in DataTokenHandler, so no future caller can recreate the hole.
        if (SCOPE_DATA.equals(scope) && ttlSeconds == null) {
            throw new IllegalArgumentException(
                "scope 'data' requires a ttl_seconds: data tokens must drain by TTL "
                + "(RDR-005 pin iii — the bulk-revoke deferral rests on it)");
        }
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
                     SERVICE_TOKENS.LABEL, SERVICE_TOKENS.EXPIRES_AT, SERVICE_TOKENS.SCOPE)
            .values(hash, tenant, label, expiresAt, scope)
            .execute();
        log.info("event=service_token_issued tenant={} label={} ttl={} scope={}",
                 tenant, label, ttlSeconds, scope);
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

    /**
     * Zero-downtime rotation: set {@code expires_at = now + grace} on every currently-live
     * token for {@code tenant}, then issue a fresh one, ALL in one transaction so a crash can
     * never leave the tenant with zero live tokens (Decision 3). Returns the new token plus
     * the grace-expired hashes so the caller can invalidate their cache entries.
     *
     * @param tenant       the tenant to rotate (not {@code '*'})
     * @param graceSeconds overlap window before the old tokens expire (must be positive)
     */
    public RotationResult rotateTokens(String tenant, long graceSeconds) {
        rejectWildcard(tenant);
        if (graceSeconds <= 0) {
            throw new IllegalArgumentException("grace_seconds must be positive");
        }
        OffsetDateTime graceDeadline =
            OffsetDateTime.ofInstant(clock.instant().plusSeconds(graceSeconds), ZoneOffset.UTC);
        return dsl().transactionResult(cfg -> {
            DSLContext tx = DSL.using(cfg);
            var liveRows = tx.select(SERVICE_TOKENS.TOKEN_HASH, SERVICE_TOKENS.SCOPE)
                .from(SERVICE_TOKENS)
                .where(SERVICE_TOKENS.TENANT_ID.eq(tenant))
                .and(SERVICE_TOKENS.REVOKED_AT.isNull())
                .and(SERVICE_TOKENS.LABEL.isDistinctFrom(ROOT_TOKEN_LABEL))
                .and(SERVICE_TOKENS.EXPIRES_AT.isNull().or(SERVICE_TOKENS.EXPIRES_AT.gt(graceDeadline)))
                // Gate-A review: deterministic scope carry — oldest row first (the
                // tenant's original credential). Without an ORDER BY the replacement
                // row's scope under a MIXED-scope live set would be arbitrary.
                // token_hash tiebreak (Gate-B M2): created_at can tie under
                // concurrent issuance and Postgres guarantees nothing for ties.
                .orderBy(SERVICE_TOKENS.CREATED_AT, SERVICE_TOKENS.TOKEN_HASH)
                .fetch();
            List<String> expired = liveRows.map(r -> r.get(SERVICE_TOKENS.TOKEN_HASH));
            if (!expired.isEmpty()) {
                tx.update(SERVICE_TOKENS)
                    .set(SERVICE_TOKENS.EXPIRES_AT, graceDeadline)
                    .where(SERVICE_TOKENS.TOKEN_HASH.in(expired))
                    .execute();
            }
            // Scope-preserving (nexus-868dq Task 2.5): without this, rotating a
            // mint-scoped credential would issue a replacement with the schema
            // default 'tenant' — silently stripping the mint privilege. The carry is
            // the OLDEST live row's scope (deterministic via the ORDER BY above);
            // rotating a deliberately mixed-scope tenant collapses to that scope and
            // logs it loudly below — one scope per tenant's credential set is the
            // intended usage. No live rows → the pre-scope default.
            String scope = liveRows.isEmpty()
                ? SCOPE_TENANT
                : liveRows.get(0).get(SERVICE_TOKENS.SCOPE);
            long distinctScopes = liveRows.stream()
                .map(r -> r.get(SERVICE_TOKENS.SCOPE)).distinct().count();
            if (distinctScopes > 1) {
                log.warn("event=service_token_rotate_mixed_scopes tenant={} scopes={} carried={}",
                         tenant, distinctScopes, scope);
            }
            String raw = newRawToken();
            String hash = TokenHashing.sha256Hex(raw);
            tx.insertInto(SERVICE_TOKENS)
                .columns(SERVICE_TOKENS.TOKEN_HASH, SERVICE_TOKENS.TENANT_ID,
                         SERVICE_TOKENS.LABEL, SERVICE_TOKENS.SCOPE)
                .values(hash, tenant, "rotated", scope)
                .execute();
            log.info("event=service_token_rotated tenant={} expiring_old={} grace_s={} scope={}",
                     tenant, expired.size(), graceSeconds, scope);
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
        return revokeToken(selector, null);
    }

    /**
     * Revoke a token by full hash or unique prefix, optionally scoped to a single tenant.
     *
     * <p>nexus-e4130: when {@code tenantScope} is non-null the selector must resolve to a
     * token whose {@code tenant_id} equals it; a selector matching only another tenant's
     * token returns empty (no cross-tenant revoke). A null scope is the operator path
     * (root token) and matches across all tenants. The tenant predicate is applied in the
     * selector resolution so a non-operator cannot even learn that another tenant's prefix
     * exists.
     *
     * @param selector    full token_hash or a unique prefix
     * @param tenantScope restrict the match to this tenant, or null for any (operator)
     * @return the full token_hash revoked, or empty if no unique in-scope match
     */
    public Optional<String> revokeToken(String selector, String tenantScope) {
        if (selector == null || selector.isBlank()) {
            return Optional.empty();
        }
        // Resolve selector to exactly one LIVE, non-root hash (exact match preferred,
        // else unique prefix). Excluding already-revoked rows avoids false-success on
        // re-revoke and prefix-shadowing by a stale revoked token; excluding the root
        // token (by ROOT_TOKEN_LABEL — re-keyed off the retired wildcard sentinel in
        // Phase E) prevents an authenticated caller from revoking the supervisor
        // credential into a total lockout (review P5.3-C). nexus-e4130: a non-null
        // tenantScope confines the match to the caller's own tenant.
        var sel = dsl()
            .select(SERVICE_TOKENS.TOKEN_HASH)
            .from(SERVICE_TOKENS)
            .where(SERVICE_TOKENS.TOKEN_HASH.eq(selector)
                .or(SERVICE_TOKENS.TOKEN_HASH.startsWith(selector)))
            .and(SERVICE_TOKENS.REVOKED_AT.isNull())
            .and(SERVICE_TOKENS.LABEL.isDistinctFrom(ROOT_TOKEN_LABEL));
        List<String> matches = (tenantScope == null
                ? sel
                : sel.and(SERVICE_TOKENS.TENANT_ID.eq(tenantScope)))
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
        // Always exclude the root token row: it is the internal supervisor credential
        // and must not be enumerable by an authenticated caller (review P5.3-C, re-keyed
        // off the retired wildcard sentinel onto ROOT_TOKEN_LABEL in Phase E).
        var base = dsl()
            .select(SERVICE_TOKENS.TOKEN_HASH, SERVICE_TOKENS.TENANT_ID, SERVICE_TOKENS.LABEL,
                    SERVICE_TOKENS.SCOPE, SERVICE_TOKENS.CREATED_AT, SERVICE_TOKENS.EXPIRES_AT,
                    SERVICE_TOKENS.REVOKED_AT)
            .from(SERVICE_TOKENS)
            .where(SERVICE_TOKENS.LABEL.isDistinctFrom(ROOT_TOKEN_LABEL));
        var filtered = (tenant == null || tenant.isBlank())
            ? base.orderBy(SERVICE_TOKENS.CREATED_AT)
            : base.and(SERVICE_TOKENS.TENANT_ID.eq(tenant)).orderBy(SERVICE_TOKENS.CREATED_AT);
        return filtered.fetch(r -> new TokenInfo(
            r.get(SERVICE_TOKENS.TOKEN_HASH),
            r.get(SERVICE_TOKENS.TENANT_ID),
            r.get(SERVICE_TOKENS.LABEL),
            r.get(SERVICE_TOKENS.SCOPE),
            r.get(SERVICE_TOKENS.CREATED_AT),
            r.get(SERVICE_TOKENS.EXPIRES_AT),
            r.get(SERVICE_TOKENS.REVOKED_AT)));
    }

    // ── Session tokens (RDR-152 bead nexus-gmiaf.32.4) ─────────────────────────

    /**
     * Mint (or re-mint) the per-session token for {@code (tenant, sessionId)}. The raw
     * secret is returned once; only its hash is stored. UPSERT on the
     * {@code UNIQUE(tenant_id, session_id)} constraint so a re-mint REPLACES the prior
     * token (the old session token is immediately invalidated), keeping at most one live
     * token per logical session (Decision 2).
     *
     * @param tenant     the session's tenant (not {@code '*'})
     * @param sessionId  the logical session id
     * @param ttlSeconds session-token lifetime (must be positive)
     * @return the minted token: raw secret (set into NX_T1_SESSION) + stored hash
     */
    public IssuedToken issueSessionToken(String tenant, String sessionId, long ttlSeconds) {
        rejectWildcard(tenant);
        if (sessionId == null || sessionId.isBlank()) {
            throw new IllegalArgumentException("session_id must not be null or blank");
        }
        if (ttlSeconds <= 0) {
            throw new IllegalArgumentException("ttl_seconds must be positive");
        }
        String raw = newRawToken();
        String hash = TokenHashing.sha256Hex(raw);
        OffsetDateTime expiresAt =
            OffsetDateTime.ofInstant(clock.instant().plusSeconds(ttlSeconds), ZoneOffset.UTC);
        dsl().insertInto(SESSION_TOKENS)
            .columns(SESSION_TOKENS.SESSION_TOKEN_HASH, SESSION_TOKENS.TENANT_ID,
                     SESSION_TOKENS.SESSION_ID, SESSION_TOKENS.EXPIRES_AT)
            .values(hash, tenant, sessionId, expiresAt)
            .onConflict(SESSION_TOKENS.TENANT_ID, SESSION_TOKENS.SESSION_ID)
            .doUpdate()
            .set(SESSION_TOKENS.SESSION_TOKEN_HASH, hash)
            .set(SESSION_TOKENS.EXPIRES_AT, expiresAt)
            .execute();
        log.info("event=session_token_minted tenant={} session={}", tenant, sessionId);
        return new IssuedToken(raw, hash);
    }

    /**
     * Delete the session token for {@code (tenant, sessionId)} (session close). Idempotent:
     * a double-close returns 0, not an error.
     *
     * @return number of rows deleted (0 or 1)
     */
    public int closeSession(String tenant, String sessionId) {
        if (tenant == null || tenant.isBlank() || sessionId == null || sessionId.isBlank()) {
            return 0;
        }
        int deleted = dsl().deleteFrom(SESSION_TOKENS)
            .where(SESSION_TOKENS.TENANT_ID.eq(tenant))
            .and(SESSION_TOKENS.SESSION_ID.eq(sessionId))
            .execute();
        log.info("event=session_token_closed tenant={} session={} deleted={}",
                 tenant, sessionId, deleted);
        return deleted;
    }
}
