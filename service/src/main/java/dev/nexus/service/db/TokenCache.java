package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;

/**
 * RDR-152 bead nexus-gmiaf.32.2 — bounded positive cache for bearer token →
 * tenant resolution, in front of {@link TokenStore}.
 *
 * <p><b>The cache/revocation seam (the boundary's weakest point) is handled by
 * separating the two staleness axes:</b>
 * <ul>
 *   <li><b>Expiry is never stale.</b> Each entry stores the row's {@code expiresAt};
 *       every cache hit re-evaluates it against the injected {@link Clock}. A token
 *       that expires mid-window is rejected at the exact instant, no DB read.</li>
 *   <li><b>Revocation has bounded staleness.</b> {@code revoked_at} is set out of
 *       band, so a hit cannot observe it without a DB read. Two layers mitigate:
 *       (1) {@link #invalidate(String)} called by the in-process revoke/rotate
 *       endpoint (Phase C) gives immediate effect; (2) a bounded positive TTL is the
 *       backstop for any revocation that did not go through {@code invalidate}
 *       (a direct DB edit, or a future second process). Worst-case revocation
 *       latency = {@code ttl}; typical = immediate.</li>
 * </ul>
 *
 * <p>ONLY positives are cached. A miss (missing/revoked/expired) is never cached, so
 * a freshly issued token is usable immediately rather than shadowed for a TTL.
 *
 * <p>Bound: at most {@code maxEntries}; on overflow the map is cleared (the next
 * requests re-warm from the DB). Since negatives are never cached, the working set is
 * the active-token count, so overflow is a flood backstop, not a steady-state path.
 * The map is concurrency-safe ({@link ConcurrentHashMap}); the clear-on-overflow
 * guard is best-effort under races, which is acceptable for a flood backstop.
 */
public final class TokenCache {

    private static final Logger log = LoggerFactory.getLogger(TokenCache.class);

    /** Default positive TTL: bounds worst-case revocation latency. */
    public static final Duration DEFAULT_TTL = Duration.ofSeconds(30);
    /** Default flood backstop. */
    public static final int DEFAULT_MAX_ENTRIES = 10_000;

    // nexus-e4130: isRoot is cached alongside the tenant. It is safe to cache because a
    // row's label is immutable for a given token_hash — no API updates the label
    // (issueToken/rotateTokens never touch it; ensureBootstrapToken is ON CONFLICT DO
    // NOTHING on the hash). If a future API ever mutates a live row's label it MUST call
    // invalidate(hash) so the operator bit is re-read.
    private record Entry(String tenantId, boolean isRoot, Instant expiresAt, Instant cachedAt) {
    }

    /**
     * Resolved bearer principal: its tenant and whether it is the root/operator token
     * (nexus-e4130). The {@code isRoot} bit carries the cross-tenant admin privilege.
     */
    public record Resolved(String tenantId, boolean isRoot) {
    }

    private final TokenStore store;
    private final Clock clock;
    private final Duration ttl;
    private final int maxEntries;
    private final ConcurrentHashMap<String, Entry> map = new ConcurrentHashMap<>();

    public TokenCache(TokenStore store, Clock clock) {
        this(store, clock, DEFAULT_TTL, DEFAULT_MAX_ENTRIES);
    }

    public TokenCache(TokenStore store, Clock clock, Duration ttl, int maxEntries) {
        this.store = store;
        this.clock = clock;
        this.ttl = ttl;
        this.maxEntries = maxEntries;
    }

    /**
     * Resolve a bearer token hash to its tenant, via cache then store.
     *
     * @param tokenHash {@code sha256Hex} of the presented bearer
     * @return the tenant, or empty if missing/revoked/expired
     */
    public Optional<String> resolveTenant(String tokenHash) {
        return resolve(tokenHash).map(Resolved::tenantId);
    }

    /**
     * Resolve a bearer token hash to its {@link Resolved} principal (tenant + root flag),
     * via cache then store. Same expiry/revocation semantics as {@link #resolveTenant};
     * the root flag rides along so a single resolution carries the operator privilege
     * (nexus-e4130) without a second lookup.
     *
     * @param tokenHash {@code sha256Hex} of the presented bearer
     * @return the resolved principal, or empty if missing/revoked/expired
     */
    public Optional<Resolved> resolve(String tokenHash) {
        if (tokenHash == null || tokenHash.isBlank()) {
            return Optional.empty();
        }
        Instant now = clock.instant();
        Entry cached = map.get(tokenHash);
        if (cached != null) {
            if (Duration.between(cached.cachedAt(), now).compareTo(ttl) < 0) {
                // Within TTL: expiry is still re-checked precisely.
                if (isExpired(cached.expiresAt(), now)) {
                    map.remove(tokenHash, cached);
                    return Optional.empty();
                }
                return Optional.of(new Resolved(cached.tenantId(), cached.isRoot()));
            }
            // Stale by TTL — drop and re-resolve from the store (revocation backstop).
            map.remove(tokenHash, cached);
        }

        Optional<TokenStore.ServiceToken> loaded = store.lookupServiceToken(tokenHash);
        if (loaded.isEmpty()) {
            return Optional.empty();  // missing/revoked — do NOT cache negatives
        }
        TokenStore.ServiceToken st = loaded.get();
        if (isExpired(st.expiresAt(), now)) {
            return Optional.empty();  // expired — do not cache
        }
        if (map.size() >= maxEntries) {
            log.warn("event=token_cache_overflow max={} action=clear", maxEntries);
            map.clear();
        }
        map.put(tokenHash, new Entry(st.tenantId(), st.isRoot(), st.expiresAt(), now));
        return Optional.of(new Resolved(st.tenantId(), st.isRoot()));
    }

    /** Remove a token from the cache (called on revoke/rotate for immediate effect). */
    public void invalidate(String tokenHash) {
        if (tokenHash != null) {
            map.remove(tokenHash);
        }
    }

    /** Test/operational visibility into the current cache size. */
    public int size() {
        return map.size();
    }

    private static boolean isExpired(Instant expiresAt, Instant now) {
        return expiresAt != null && !expiresAt.isAfter(now);
    }
}
