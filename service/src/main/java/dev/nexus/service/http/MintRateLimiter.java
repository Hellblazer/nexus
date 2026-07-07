package dev.nexus.service.http;

import java.time.Clock;
import java.time.Instant;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Function;

/**
 * nexus-x1h07 — token-bucket rate limiter for {@code POST /v1/data-tokens/mint}
 * (conexus RDR-005 pin ii). Two layers per mint CREDENTIAL (keyed by the bearer's
 * sha256 hash, never the raw secret):
 *
 * <ul>
 *   <li><b>per-(credential, tenant)</b>: burst {@code tenantBurst} (default 5),
 *       sustained {@code tenantSustainedPerMinute} (default 1/min) — the
 *       legitimate-edge shape is ~1 mint per TTL per tenant, then cached.</li>
 *   <li><b>per-credential GLOBAL</b>: {@code globalSustainedPerMinute} (default
 *       10/min; capacity == rate, no separate burst knob) — the anti-sweep
 *       backstop: hammering N different tenants with one credential still hits
 *       this cap, so a full-tenant sweep cannot outrun conexus's anomaly window.</li>
 * </ul>
 *
 * <p>All three bounds are env-overridable ({@code NX_MINT_RATE_TENANT_BURST},
 * {@code NX_MINT_RATE_TENANT_SUSTAINED_PER_MINUTE},
 * {@code NX_MINT_RATE_GLOBAL_SUSTAINED_PER_MINUTE}) so conexus can retune without
 * an engine release (pin ii: re-tune trigger at &gt;~50 tenants).
 *
 * <p>In-memory by design: the engine is a single instance; a restart resets the
 * buckets to full, which is acceptable (the limit is an abuse bound, not an
 * accounting ledger). Buckets refill lazily on access from the injected
 * {@link Clock} (the {@code TokenCache} clock-driven style — no scheduler thread).
 *
 * <p><b>Refund on partial failure</b>: {@code tryAcquire} debits the tenant bucket
 * first, then the global bucket; when the global refuses, the tenant debit is
 * refunded so global pressure never silently starves an innocent tenant's bucket.
 * Both debits happen under the per-credential lock, so two concurrent requests
 * for the same credential cannot interleave a debit/refund pair (virtual-thread
 * executor: contention is short and bounded by map-entry granularity).
 */
public final class MintRateLimiter {

    private static final org.slf4j.Logger log =
        org.slf4j.LoggerFactory.getLogger(MintRateLimiter.class);

    /** Flood backstops (Gate-B review H1): both maps are keyed by data an
     *  authenticated caller controls (its credential hash; the body tenant
     *  string), so both must be bounded — the TokenCache overflow pattern
     *  (evict ~25%, never clear-all). Losing a bucket to eviction merely
     *  re-grants a burst window; it never grants unlimited rate. */
    static final int MAX_CREDENTIALS = 1_000;
    static final int MAX_TENANT_BUCKETS_PER_CREDENTIAL = 10_000;

    private final Clock clock;
    private final int tenantBurst;
    private final int tenantSustainedPerMinute;
    private final int globalSustainedPerMinute;

    /** Mutable token bucket; guarded by the owning credential's Bucket lock object. */
    private static final class Bucket {
        double tokens;
        Instant lastRefill;

        Bucket(double tokens, Instant lastRefill) {
            this.tokens = tokens;
            this.lastRefill = lastRefill;
        }

        void refill(Instant now, double capacity, double perMinute) {
            double elapsedSeconds = Math.max(0,
                java.time.Duration.between(lastRefill, now).toMillis() / 1000.0);
            tokens = Math.min(capacity, tokens + elapsedSeconds * (perMinute / 60.0));
            lastRefill = now;
        }
    }

    /** Per-credential state: the global bucket + this credential's tenant buckets. */
    private static final class CredentialState {
        final Bucket global;
        final ConcurrentHashMap<String, Bucket> tenants = new ConcurrentHashMap<>();

        CredentialState(Bucket global) {
            this.global = global;
        }
    }

    private final ConcurrentHashMap<String, CredentialState> credentials = new ConcurrentHashMap<>();

    public MintRateLimiter(Clock clock, int tenantBurst, int tenantSustainedPerMinute,
                           int globalSustainedPerMinute) {
        if (tenantBurst <= 0 || tenantSustainedPerMinute <= 0 || globalSustainedPerMinute <= 0) {
            throw new IllegalArgumentException("rate-limit bounds must be positive");
        }
        this.clock = clock;
        this.tenantBurst = tenantBurst;
        this.tenantSustainedPerMinute = tenantSustainedPerMinute;
        this.globalSustainedPerMinute = globalSustainedPerMinute;
    }

    /** Env-driven factory (production). */
    public static MintRateLimiter fromEnv(Clock clock) {
        return fromEnv(clock, System::getenv);
    }

    /** Env-injectable factory (tests never mutate real process env). */
    static MintRateLimiter fromEnv(Clock clock, Function<String, String> env) {
        return new MintRateLimiter(
            clock,
            envInt(env, "NX_MINT_RATE_TENANT_BURST", 5),
            envInt(env, "NX_MINT_RATE_TENANT_SUSTAINED_PER_MINUTE", 1),
            envInt(env, "NX_MINT_RATE_GLOBAL_SUSTAINED_PER_MINUTE", 10));
    }

    private static int envInt(Function<String, String> env, String name, int dflt) {
        String raw = env.apply(name);
        if (raw == null || raw.isBlank()) {
            return dflt;
        }
        try {
            return Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(name + " must be an integer, got: " + raw, e);
        }
    }

    /**
     * Attempt one mint under both limits. True = admitted (both buckets debited);
     * false = refused (neither bucket net-debited — see refund note above).
     */
    public boolean tryAcquire(String credentialHash, String tenant) {
        Instant now = clock.instant();
        if (credentials.size() >= MAX_CREDENTIALS && !credentials.containsKey(credentialHash)) {
            evictQuarter(credentials, "credentials");
        }
        CredentialState state = credentials.computeIfAbsent(credentialHash, k ->
            new CredentialState(new Bucket(globalSustainedPerMinute, now)));
        if (state.tenants.size() >= MAX_TENANT_BUCKETS_PER_CREDENTIAL
                && !state.tenants.containsKey(tenant)) {
            evictQuarter(state.tenants, "tenant_buckets");
        }
        Bucket tenantBucket = state.tenants.computeIfAbsent(tenant, k ->
            new Bucket(tenantBurst, now));
        // One lock per credential (its global bucket object): the tenant debit and
        // the global debit/refund form one atomic decision, so concurrent requests
        // cannot leak a tenant token on a global refusal.
        synchronized (state.global) {
            tenantBucket.refill(now, tenantBurst, tenantSustainedPerMinute);
            if (tenantBucket.tokens < 1.0) {
                return false;
            }
            tenantBucket.tokens -= 1.0;
            state.global.refill(now, globalSustainedPerMinute, globalSustainedPerMinute);
            if (state.global.tokens < 1.0) {
                tenantBucket.tokens += 1.0;  // refund: global pressure must not starve the tenant bucket
                return false;
            }
            state.global.tokens -= 1.0;
            return true;
        }
    }

    /** TokenCache-style overflow trim: evict ~25%, never clear-all (thundering herd). */
    private static void evictQuarter(ConcurrentHashMap<String, ?> map, String which) {
        int toEvict = Math.max(1, map.size() / 4);
        log.warn("event=mint_rate_limiter_overflow map={} size={} evicting={}",
                 which, map.size(), toEvict);
        var it = map.keySet().iterator();
        for (int i = 0; i < toEvict && it.hasNext(); i++) {
            it.next();
            it.remove();
        }
    }

    int tenantBurst() {
        return tenantBurst;
    }

    int tenantSustainedPerMinute() {
        return tenantSustainedPerMinute;
    }

    int globalSustainedPerMinute() {
        return globalSustainedPerMinute;
    }
}
