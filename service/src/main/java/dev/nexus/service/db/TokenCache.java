package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

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

    // nexus-e4130 / nexus-868dq: isRoot and scope are cached alongside the tenant. Safe
    // to cache because a row's scope (like its label) is immutable for a given
    // token_hash — no API updates it (issueToken sets it at insert; rotateTokens
    // inserts a NEW row; ensureBootstrapToken is ON CONFLICT DO NOTHING on the hash).
    // If a future API ever mutates a live row's scope it MUST call invalidate(hash)
    // so the privilege bits are re-read.
    private record Entry(String tenantId, boolean isRoot, String scope,
                         Instant expiresAt, Instant cachedAt) {
    }

    /**
     * Resolved bearer principal: its tenant, whether it is the root/operator token
     * (nexus-e4130), and its server-assigned scope (nexus-868dq). The {@code isRoot}
     * bit carries the cross-tenant admin privilege; {@code scope} carries the
     * mint/data route restrictions.
     */
    public record Resolved(String tenantId, boolean isRoot, String scope) {
    }

    private final TokenStore store;
    private final Clock clock;
    private final Duration ttl;
    private final int maxEntries;
    private final ConcurrentHashMap<String, Entry> map = new ConcurrentHashMap<>();
    /**
     * Bumped by every {@link #invalidateTenant} (bead nexus-0bl77, critic S2). {@link #resolve} reads it BEFORE
     * its store read and skips its cache put when it moved, so a resolve that read a token row before a tenant
     * reset deleted it cannot store a positive after the eviction and shadow the delete for the cache TTL. One
     * counter rather than one per tenant because the tenant is only known after the store read; the cost of the
     * coarseness is that a resolve overlapping ANY tenant's invalidation skips one cache fill, and the next
     * request fills it.
     */
    private final AtomicLong tenantInvalidations = new AtomicLong();
    /** Test-only (nexus-0bl77): runs between the store read and the cache put of {@link #resolve}, so a test can
     *  place an invalidation exactly in that window. Null in production. */
    volatile Runnable testOnlyAfterStoreRead;

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
                return Optional.of(new Resolved(cached.tenantId(), cached.isRoot(), cached.scope()));
            }
            // Stale by TTL — drop and re-resolve from the store (revocation backstop).
            map.remove(tokenHash, cached);
        }

        long invalidationsBeforeRead = tenantInvalidations.get();
        Optional<TokenStore.ServiceToken> loaded = store.lookupServiceToken(tokenHash);
        Runnable afterRead = testOnlyAfterStoreRead;
        if (afterRead != null) {
            afterRead.run();
        }
        if (loaded.isEmpty()) {
            return Optional.empty();  // missing/revoked — do NOT cache negatives
        }
        TokenStore.ServiceToken st = loaded.get();
        if (isExpired(st.expiresAt(), now)) {
            return Optional.empty();  // expired — do not cache
        }
        if (map.size() >= maxEntries) {
            // Wave review: was map.clear() — evicting EVERY cached principal at once
            // meant the next request from every active tenant paid a synchronous
            // TokenStore round-trip simultaneously (a self-inflicted thundering herd
            // right when the system is busiest). Evict ~25% instead: the flood
            // backstop still bounds memory, but hot tokens survive the trim.
            int toEvict = Math.max(1, maxEntries / 4);
            log.warn("event=token_cache_overflow max={} action=evict count={}", maxEntries, toEvict);
            var it = map.keySet().iterator();
            for (int i = 0; i < toEvict && it.hasNext(); i++) {
                it.next();
                it.remove();
            }
        }
        Entry fresh = new Entry(st.tenantId(), st.isRoot(), st.scope(), st.expiresAt(), now);
        map.put(tokenHash, fresh);
        if (tenantInvalidations.get() != invalidationsBeforeRead) {
            // A tenant was invalidated while this read was in flight, so the row it saw may be one a reset has
            // since deleted. Checked AFTER the put, not before it: invalidateTenant bumps the counter and then
            // sweeps, so either its sweep sees this entry or this check sees the bump; a check before the put
            // would leave a window between the two. The request is answered from what was read (it was in
            // flight before the reset) but the entry is not kept; the next request reads the store again.
            map.remove(tokenHash, fresh);
        }
        return Optional.of(new Resolved(st.tenantId(), st.isRoot(), st.scope()));
    }

    /** Remove a token from the cache (called on revoke/rotate for immediate effect). */
    public void invalidate(String tokenHash) {
        if (tokenHash != null) {
            map.remove(tokenHash);
        }
    }

    /**
     * Remove every cached principal of {@code tenant} (bead nexus-0bl77, the test-only tenant reset route). The
     * reset deletes the tenant's token rows, and this cache would otherwise keep answering for them up to its
     * positive TTL, so a late writer holding the dead test's bearer would still reach the tenant.
     *
     * @return the number of entries removed
     */
    public int invalidateTenant(String tenant) {
        if (tenant == null) {
            return 0;
        }
        tenantInvalidations.incrementAndGet();   // before the sweep: a resolve that starts now sees it moved
        int[] removed = {0};
        map.entrySet().removeIf(e -> {
            boolean mine = tenant.equals(e.getValue().tenantId());
            if (mine) {
                removed[0]++;
            }
            return mine;
        });
        return removed[0];
    }

    /** Test/operational visibility into the current cache size. */
    public int size() {
        return map.size();
    }

    private static boolean isExpired(Instant expiresAt, Instant now) {
        return expiresAt != null && !expiresAt.isAfter(now);
    }
}
