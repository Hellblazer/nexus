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
     * Every tenant that has EVER held a service token (revoked included —
     * a revoked tenant's expired T1 scratch rows still need sweeping).
     * Drives the cross-tenant TTL sweep (nexus-4qq1m): the sweeper loops
     * these through {@code ScratchRepository.sweepTenant}, staying on the
     * RLS-scoped per-tenant path — no BYPASSRLS connection required. Any
     * tenant that wrote scratch necessarily presented a token, so this set
     * covers every tenant that can have rows.
     */
    public java.util.List<String> listKnownTenants() {
        return listKnownTenants(null);
    }

    /**
     * As {@link #listKnownTenants()}, bounded by {@code statementTimeout}
     * (nexus-lgiqw).
     *
     * <p>This one is easy to miss and bounding it is NOT optional: the scheduled
     * sweep calls it to build its tenant list BEFORE any arm runs, so an unbounded
     * SELECT here would stall the cycle at enumeration and every per-arm bound
     * downstream would never be reached. An {@code ACCESS EXCLUSIVE} lock on
     * {@code service_tokens} — a non-CONCURRENT index build, a VACUUM FULL — blocks
     * a plain SELECT, not just writes.
     *
     * @param statementTimeout per-statement ceiling, or null for none
     */
    public java.util.List<String> listKnownTenants(java.time.Duration statementTimeout) {
        return dsl().transactionResult(cfg -> {
            DSLContext tx = DSL.using(cfg);
            SweepBounds.applyStatementTimeout(tx, statementTimeout);
            return tx.selectDistinct(SERVICE_TOKENS.TENANT_ID)
                .from(SERVICE_TOKENS)
                .fetch(SERVICE_TOKENS.TENANT_ID);
        });
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

        // THE ROOT SLOT'S KEY IS THE LABEL, NOT THE HASH (nexus-kjjab).
        //
        // This used to be a bare ON CONFLICT (token_hash) DO NOTHING. That arbiter names
        // the PK — but the table ALSO carries idx_service_tokens_single_root, a partial
        // unique index on (label) WHERE label = 'bootstrap-legacy-token'. A ROTATED
        // NX_SERVICE_TOKEN has a NEW hash and the SAME label, so the arbiter missed
        // entirely and the label index fired as an unhandled 23505 — on the auth bootstrap
        // path, outside any try in Main, taking the whole boot down with a bare stack
        // trace. The index's own changeset comment names "a rotated NX_SERVICE_TOKEN
        // re-seed" as the case it guards; guarding it by aborting startup was an accident,
        // not a design.
        //
        // Resolve by label FIRST, then decide — the same prevent-rather-than-catch shape
        // the arbiter class (nexus-0ehwe) settled on, and it needs no constraint-name
        // extraction.
        var incumbent = dsl()
            .select(SERVICE_TOKENS.TOKEN_HASH, SERVICE_TOKENS.REVOKED_AT)
            .from(SERVICE_TOKENS)
            .where(SERVICE_TOKENS.LABEL.eq(ROOT_TOKEN_LABEL))
            .fetchOne();

        if (incumbent == null) {
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
            return;
        }

        String incumbentHash = incumbent.get(SERVICE_TOKENS.TOKEN_HASH);
        if (hash.equals(incumbentHash)) {
            return;                                  // idempotent re-seed: the common path
        }

        // A REVOKED incumbent holds the slot (the index predicate has no revoked_at term).
        // Neither silent option is acceptable: resurrecting it overrides a deliberate
        // revocation, and rotating onto it mints a root token that is dead on arrival, so
        // the service boots into an unusable state nobody is told about. Refuse, and name
        // the remedy.
        if (incumbent.get(SERVICE_TOKENS.REVOKED_AT) != null) {
            throw new BootstrapTokenConflict(
                "the root token slot (label '" + ROOT_TOKEN_LABEL + "') is held by a REVOKED "
                + "row, so the provisioned NX_SERVICE_TOKEN cannot be seeded. Delete that row "
                + "explicitly if the rotation is intended — this is not resolved silently "
                + "because doing so would either override a deliberate revocation or leave a "
                + "root token that authenticates nothing.");
        }

        // ROTATION. Replace the hash in place, which is what makes the OLD token stop
        // working — the entire point of rotating a credential. Leaving the incumbent valid
        // would mean a "rotated" compromised token still authenticates, silently: a worse
        // outcome than the crash this replaces. NX_SERVICE_TOKEN is the source of truth and
        // this row is its binding; an operator who changes the env and restarts has stated
        // their intent, and already controls the service either way.
        //
        // Guarded on the incumbent hash so it is a compare-and-swap, not a check-then-write:
        // two services booting against one database cannot both rotate and clobber.
        int rotated = dsl()
            .update(SERVICE_TOKENS)
            .set(SERVICE_TOKENS.TOKEN_HASH, hash)
            .set(SERVICE_TOKENS.TENANT_ID, tenantId)
            .set(SERVICE_TOKENS.SCOPE, SCOPE_ROOT)
            .where(SERVICE_TOKENS.LABEL.eq(ROOT_TOKEN_LABEL)
                .and(SERVICE_TOKENS.TOKEN_HASH.eq(incumbentHash)))
            .execute();

        if (rotated > 0) {
            // Loud by design. Replacing the root credential is exactly the event an
            // operator must be able to find afterwards; the hash itself is never logged.
            log.warn("event=root_token_rotated tenant={} reason=provisioned_token_changed",
                     tenantId);
            return;
        }

        // Lost the CAS: another boot rotated the slot concurrently. Converge if it landed
        // on the same token, refuse if it landed on a different one.
        String now = dsl()
            .select(SERVICE_TOKENS.TOKEN_HASH).from(SERVICE_TOKENS)
            .where(SERVICE_TOKENS.LABEL.eq(ROOT_TOKEN_LABEL))
            .fetchOne(SERVICE_TOKENS.TOKEN_HASH);
        if (hash.equals(now)) {
            return;                                  // same rotation, applied by the racer
        }
        throw new BootstrapTokenConflict(
            "the root token slot was rotated concurrently to a DIFFERENT token while this "
            + "service was starting. Refusing rather than overwriting: two services are "
            + "provisioned with different NX_SERVICE_TOKEN values against one database.");
    }

    /**
     * Root-token seeding could not reach a defined state (nexus-kjjab).
     *
     * <p>TYPED so {@code Main} can report it the way it reports the other two anticipated
     * startup failures — a logged error naming the remedy, then a non-zero exit — rather
     * than the bare stack trace an unhandled 23505 produced from the same call site.
     */
    public static final class BootstrapTokenConflict extends RuntimeException {
        public BootstrapTokenConflict(String message) { super(message); }
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

    /**
     * Delete every {@code session_tokens} row for {@code tenant} whose {@code
     * expires_at} is already in the past (nexus-t23zk). {@link #closeSession}
     * alone leaves a permanent row behind whenever a minting process dies without
     * calling it (crashed MCP, killed dispatch, machine reboot) — auth checks
     * {@code expires_at} live (see class javadoc), so an expired-but-unclosed
     * token is inert (no security exposure), but nothing else ever deletes the
     * row. This is the backstop: called from {@link
     * dev.nexus.service.NexusService}'s existing {@code t1-ttl-sweep} thread, in
     * the SAME per-tenant loop as {@link ScratchRepository#sweepTenant} — one
     * extra query on an existing schedule, not a new thread.
     *
     * <p>{@code session_tokens} carries no RLS (class javadoc: "Phase A leaves
     * these tables un-RLS'd"), so the {@code tenant_id} predicate here is
     * defense-in-depth scoping, not a GUC-stamped requirement — mirrors {@link
     * #closeSession}'s existing tenant-scoped shape rather than a single
     * unscoped sweep across every tenant at once, so a per-tenant sweep failure
     * (logged, not thrown, by the caller) never masks another tenant's rows.
     *
     * @param tenant the tenant to sweep (blank/null is a no-op, returns 0)
     * @param now    the sweep's reference instant — a row is swept when its
     *               {@code expires_at} is strictly before this
     * @return number of rows deleted (0 when none had expired)
     */
    public int sweepExpiredSessions(String tenant, Instant now) {
        return sweepExpiredSessions(tenant, now, null);
    }

    /**
     * As {@link #sweepExpiredSessions(String, Instant)}, bounded by {@code
     * statementTimeout} (nexus-lgiqw). The scheduled sweep task passes {@link
     * SweepBounds#STATEMENT_TIMEOUT}; a null timeout is the unbounded behaviour
     * this method had before the bound existed.
     *
     * @param statementTimeout per-statement ceiling, or null for none
     */
    public int sweepExpiredSessions(String tenant, Instant now, java.time.Duration statementTimeout) {
        if (tenant == null || tenant.isBlank()) {
            return 0;
        }
        OffsetDateTime cutoff = OffsetDateTime.ofInstant(now, ZoneOffset.UTC);
        int deleted = dsl().transactionResult(cfg -> {
            DSLContext tx = DSL.using(cfg);
            SweepBounds.applyStatementTimeout(tx, statementTimeout);
            return tx.deleteFrom(SESSION_TOKENS)
                .where(SESSION_TOKENS.TENANT_ID.eq(tenant))
                .and(SESSION_TOKENS.EXPIRES_AT.lt(cutoff))
                .execute();
        });
        log.info("event=session_token_sweep tenant={} cutoff={} deleted={}", tenant, cutoff, deleted);
        return deleted;
    }

    /**
     * Delete every {@code service_tokens} row for {@code tenant} whose scope is
     * {@link #SCOPE_DATA} and whose {@code expires_at} is more than {@code grace}
     * in the past (nexus-lgiqw). Rides the SAME {@code t1-ttl-sweep} thread and
     * per-tenant loop as {@link #sweepExpiredSessions} and {@link
     * ScratchRepository#sweepTenant} — one extra query on an existing schedule,
     * not a new thread.
     *
     * <p>WHY THIS EXISTS. The JIT mint path writes a short-TTL {@code scope=data}
     * row per (tenant, TTL window) and nothing ever deleted one; expiry was
     * read-time filtering only, so the table grew monotonically. Measured on the
     * live estate 2026-08-25: 14,308 data rows, 14,307 of them already expired,
     * accruing ~313/day. The producer is the EDGE, not the client — enabling or
     * disabling client-side minting does not change it.
     *
     * <p>WHY {@code grace}, AND WHY IT IS NOT OPTIONAL. Nothing else in this
     * database records that a token was ever minted: there is no token-audit
     * table (gc_audit is chash garbage collection, unrelated), so this row is the
     * only DB evidence of the mint. The one other record is the control plane's
     * {@code engine_data_token_mint_ok} CloudWatch line.
     *
     * <p>Stated honestly, because the first version of this comment claimed more
     * than it could support: there is NO forensic, compliance, or contractual
     * requirement on these rows — conexus confirmed that directly for the estate
     * that holds them. 7 days is a conservative default that leaves an operator
     * debugging a recent mint failure something to look at; it is not derived from
     * a requirement. The "keep it shorter than CloudWatch retention" comparison is
     * a sanity bound, not a derivation, and it is worth knowing that the bound
     * depends on a retention knob ANOTHER team owns — one that moved from 365 to
     * 30 days on 2026-08-25 as a side effect of unrelated cost work. If it is cut
     * again below this window, the comparison silently inverts and nobody here
     * finds out. Do not treat that inequality as load-bearing.
     *
     * <p>A null {@code grace} is refused rather than treated as zero. That much IS
     * load-bearing regardless of the window's size: silently defaulting to no
     * grace would turn a caller's omission into an immediate delete of everything
     * already expired.
     *
     * <p>WHY THE SCOPE FILTER IS EXPLICIT. {@code root}, {@code tenant}, {@code
     * mint} and {@code mint-locked} are long-lived operator artifacts and are
     * never swept. {@code root} and {@code mint-locked} happen to carry {@code
     * expires_at IS NULL} — {@code mint-locked} being the production credential
     * provisioned 2026-08-16, which has no expiry precisely so it cannot age out
     * — and a NULL never satisfies the cutoff comparison. That is a second,
     * independent reason they are safe, NOT the one relied on here: the safety of
     * a production credential must not rest on NULL comparison semantics.
     * {@code TokenStoreDataTokenSweepTest} fails if this filter is removed.
     *
     * <p>NOT BATCHED, and no statement timeout — deliberately, matching {@link
     * #sweepExpiredSessions}. Steady state is ~80 rows per 6h cycle: a periodic
     * sweep never sees cumulative growth, only what accrued since the last cycle,
     * so the delete does not grow with the table. The one-time backlog is ~12k
     * rows on a 4 MB table, sub-second. (Recorded because it was argued and
     * measured: a batched delete would ALSO have weakened the bound, since
     * statement_timeout resets per statement — see CatalogRepository's sweep-gate
     * constants.)
     *
     * <p>KNOWN, ACCEPTED, AND NOT PAPERED OVER: this DELETE has no statement
     * timeout, and the sweep loop that calls it is single-threaded across all
     * tenants and all three arms. A pathological tenant could therefore stall the
     * whole cycle with no ceiling. That risk pre-dates this arm and is shared with
     * both siblings; it is accepted here on the measured numbers (~80 rows/cycle,
     * 4 MB table, single engine, zero long-running transactions), not because it
     * was overlooked. The counter-argument that a single statement with one
     * timeout would be a genuinely stronger bound than the current none is real
     * and is recorded on nexus-lgiqw rather than settled unilaterally here.
     *
     * @param tenant the tenant to sweep (blank/null is a no-op, returns 0)
     * @param now    the sweep's reference instant
     * @param grace  how far past {@code expires_at} a row must be before it is
     *               eligible; null is refused (returns 0) rather than treated as
     *               zero grace
     * @return number of rows deleted (0 when none were eligible)
     */
    public int sweepExpiredDataTokens(String tenant, Instant now, java.time.Duration grace) {
        return sweepExpiredDataTokens(tenant, now, grace, null);
    }

    /**
     * As {@link #sweepExpiredDataTokens(String, Instant, java.time.Duration)},
     * bounded by {@code statementTimeout}. The scheduled sweep task passes {@link
     * SweepBounds#STATEMENT_TIMEOUT}; null is unbounded.
     *
     * @param statementTimeout per-statement ceiling, or null for none
     */
    public int sweepExpiredDataTokens(String tenant, Instant now, java.time.Duration grace,
                                      java.time.Duration statementTimeout) {
        if (tenant == null || tenant.isBlank()) {
            return 0;
        }
        if (grace == null) {
            log.warn("event=service_token_sweep_refused tenant={} reason=null_grace", tenant);
            return 0;
        }
        OffsetDateTime cutoff = OffsetDateTime.ofInstant(now.minus(grace), ZoneOffset.UTC);
        int deleted = dsl().transactionResult(cfg -> {
            DSLContext tx = DSL.using(cfg);
            SweepBounds.applyStatementTimeout(tx, statementTimeout);
            return tx.deleteFrom(SERVICE_TOKENS)
                .where(SERVICE_TOKENS.TENANT_ID.eq(tenant))
                .and(SERVICE_TOKENS.SCOPE.eq(SCOPE_DATA))
                .and(SERVICE_TOKENS.EXPIRES_AT.lt(cutoff))
                .execute();
        });
        log.info("event=service_token_sweep tenant={} cutoff={} deleted={}", tenant, cutoff, deleted);
        return deleted;
    }
}
