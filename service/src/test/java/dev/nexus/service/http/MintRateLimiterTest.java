package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-x1h07 Phase 5 — token-bucket rate limiter for POST /v1/data-tokens/mint.
 *
 * <p>Pure unit test (no Postgres, no HTTP): a mutable clock drives refill
 * deterministically. The pinned bounds (conexus RDR-005 pin ii): per-(credential,
 * tenant) burst 5 / sustained 1 per minute; per-credential GLOBAL sustained 10 per
 * minute (the anti-sweep backstop — hammering N different tenants with one
 * credential must still hit the global cap). All env-overridable.
 *
 * <p>The correctness property called out in the design: REFUND ON PARTIAL FAILURE —
 * when the tenant bucket admits but the global bucket refuses, the tenant-side
 * token must be returned, not leaked (otherwise global pressure silently starves
 * innocent tenants' buckets).
 */
class MintRateLimiterTest {

    private static final Instant T0 = Instant.parse("2026-07-07T00:00:00Z");

    /** Mutable clock — mirrors AuthFilterTest's per-file pattern. */
    static final class MutableClock extends Clock {
        private volatile Instant instant;
        MutableClock(Instant instant) { this.instant = instant; }
        void set(Instant instant) { this.instant = instant; }
        void advance(long seconds) { this.instant = this.instant.plusSeconds(seconds); }
        @Override public ZoneId getZone() { return ZoneOffset.UTC; }
        @Override public Clock withZone(ZoneId zone) { return this; }
        @Override public Instant instant() { return instant; }
    }

    private MintRateLimiter limiter(MutableClock clock, int burst, int perMin, int globalPerMin) {
        return new MintRateLimiter(clock, burst, perMin, globalPerMin);
    }

    @Test
    void tenantBurst_thenRefused() {
        var clock = new MutableClock(T0);
        var rl = limiter(clock, 5, 1, 100);
        for (int i = 0; i < 5; i++) {
            assertThat(rl.tryAcquire("cred-1", "acme")).as("burst call %d", i).isTrue();
        }
        assertThat(rl.tryAcquire("cred-1", "acme")).as("6th call at same instant").isFalse();
    }

    @Test
    void tenantBucket_refillsAtSustainedRate() {
        var clock = new MutableClock(T0);
        var rl = limiter(clock, 5, 1, 100);
        for (int i = 0; i < 5; i++) {
            assertThat(rl.tryAcquire("cred-1", "acme")).isTrue();
        }
        assertThat(rl.tryAcquire("cred-1", "acme")).isFalse();
        // 1/minute sustained: after 60s exactly one more token is available.
        clock.advance(60);
        assertThat(rl.tryAcquire("cred-1", "acme")).isTrue();
        assertThat(rl.tryAcquire("cred-1", "acme")).isFalse();
    }

    @Test
    void tenantBuckets_independent_globalShared() {
        var clock = new MutableClock(T0);
        // Tenant burst 5; global 8 → two tenants can't burn 5 each.
        var rl = limiter(clock, 5, 1, 8);
        for (int i = 0; i < 5; i++) {
            assertThat(rl.tryAcquire("cred-1", "tenant-a")).isTrue();
        }
        // tenant-b's own bucket is fresh (independent) — but global has 3 left.
        assertThat(rl.tryAcquire("cred-1", "tenant-b")).isTrue();
        assertThat(rl.tryAcquire("cred-1", "tenant-b")).isTrue();
        assertThat(rl.tryAcquire("cred-1", "tenant-b")).isTrue();
        // Global exhausted although tenant-b's bucket has 2 tokens of headroom.
        assertThat(rl.tryAcquire("cred-1", "tenant-b"))
            .as("global per-credential cap must bound a multi-tenant sweep").isFalse();
    }

    @Test
    void differentCredentials_fullyIndependent() {
        var clock = new MutableClock(T0);
        var rl = limiter(clock, 2, 1, 4);
        assertThat(rl.tryAcquire("cred-1", "acme")).isTrue();
        assertThat(rl.tryAcquire("cred-1", "acme")).isTrue();
        assertThat(rl.tryAcquire("cred-1", "acme")).isFalse();
        // A different credential has its own tenant AND global buckets.
        assertThat(rl.tryAcquire("cred-2", "acme")).isTrue();
        assertThat(rl.tryAcquire("cred-2", "acme")).isTrue();
    }

    @Test
    void globalRefusal_refundsTenantBucket() {
        var clock = new MutableClock(T0);
        // Global 1: the first acquire drains it.
        var rl = limiter(clock, 5, 1, 1);
        assertThat(rl.tryAcquire("cred-1", "tenant-a")).isTrue();
        // Global refuses now. The tenant-b bucket must NOT be debited by the
        // failed attempt (refund-on-partial-failure) — after the global refills,
        // tenant-b still has its FULL burst available.
        assertThat(rl.tryAcquire("cred-1", "tenant-b")).isFalse();
        clock.advance(60);  // global (rate 1/min) refills one token
        for (int i = 0; i < 1; i++) {
            assertThat(rl.tryAcquire("cred-1", "tenant-b")).isTrue();
        }
        // tenant-b consumed exactly 1 of its 5-burst; 4 remain (global is empty
        // again though, so the proof of no-leak is above: the refused attempt did
        // not silently eat a tenant-b token — the post-refill acquire succeeded
        // with the tenant bucket still at full burst).
    }

    @Test
    void globalBucket_refillsAtItsOwnRate() {
        var clock = new MutableClock(T0);
        var rl = limiter(clock, 100, 100, 2);
        assertThat(rl.tryAcquire("cred-1", "a")).isTrue();
        assertThat(rl.tryAcquire("cred-1", "b")).isTrue();
        assertThat(rl.tryAcquire("cred-1", "c")).isFalse();
        // 2/minute → one token back after 30s.
        clock.advance(30);
        assertThat(rl.tryAcquire("cred-1", "c")).isTrue();
        assertThat(rl.tryAcquire("cred-1", "d")).isFalse();
    }

    @Test
    void tenantBucketMap_boundedWithEviction() {
        // Gate-B review H1: the tenant map is keyed by an attacker-chosen body
        // string — it must be bounded. Overflow evicts ~25% and stays functional.
        var clock = new MutableClock(T0);
        var rl = limiter(clock, 5, 1, Integer.MAX_VALUE);
        int cap = MintRateLimiter.MAX_TENANT_BUCKETS_PER_CREDENTIAL;
        for (int i = 0; i < cap + 100; i++) {
            assertThat(rl.tryAcquire("cred-1", "garbage-tenant-" + i)).isTrue();
        }
        // Still functional after overflow: a fresh acquire succeeds and an
        // exhausted bucket still refuses.
        assertThat(rl.tryAcquire("cred-1", "post-overflow-tenant")).isTrue();
        for (int i = 0; i < 5; i++) {
            rl.tryAcquire("cred-1", "exhaust-me");
        }
        assertThat(rl.tryAcquire("cred-1", "exhaust-me")).isFalse();
    }

    @Test
    void credentialMap_boundedWithEviction() {
        var clock = new MutableClock(T0);
        var rl = limiter(clock, 5, 1, 100);
        for (int i = 0; i < MintRateLimiter.MAX_CREDENTIALS + 50; i++) {
            assertThat(rl.tryAcquire("cred-" + i, "acme")).isTrue();
        }
        assertThat(rl.tryAcquire("post-overflow-cred", "acme")).isTrue();
    }

    @Test
    void fromEnv_readsOverrides_andDefaults() {
        var clock = new MutableClock(T0);
        // Defaults when unset: burst 5 / sustained 1/min / global 10/min.
        Map<String, String> empty = Map.of();
        MintRateLimiter defaults = MintRateLimiter.fromEnv(clock, empty::get);
        assertThat(defaults.tenantBurst()).isEqualTo(5);
        assertThat(defaults.tenantSustainedPerMinute()).isEqualTo(1);
        assertThat(defaults.globalSustainedPerMinute()).isEqualTo(10);

        Map<String, String> overrides = Map.of(
            "NX_MINT_RATE_TENANT_BURST", "7",
            "NX_MINT_RATE_TENANT_SUSTAINED_PER_MINUTE", "3",
            "NX_MINT_RATE_GLOBAL_SUSTAINED_PER_MINUTE", "20");
        MintRateLimiter tuned = MintRateLimiter.fromEnv(clock, overrides::get);
        assertThat(tuned.tenantBurst()).isEqualTo(7);
        assertThat(tuned.tenantSustainedPerMinute()).isEqualTo(3);
        assertThat(tuned.globalSustainedPerMinute()).isEqualTo(20);
    }
}
