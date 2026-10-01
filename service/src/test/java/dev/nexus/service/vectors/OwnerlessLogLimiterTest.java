// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.concurrent.atomic.AtomicLong;

import static org.assertj.core.api.Assertions.assertThat;

class OwnerlessLogLimiterTest {

    @Test
    void oneLinePerKeyPerWindow_withTheSuppressedCountOnTheNext() {
        var now = new AtomicLong(1_000_000L);
        var limiter = new OwnerlessLogLimiter(now::get);

        assertThat(limiter.tryAcquire("upsert-chunks|c")).as("first line, nothing suppressed yet").isEqualTo(0);
        assertThat(limiter.tryAcquire("upsert-chunks|c")).isEqualTo(-1);
        now.addAndGet(30_000);
        assertThat(limiter.tryAcquire("upsert-chunks|c")).isEqualTo(-1);
        now.addAndGet(OwnerlessLogLimiter.WINDOW_MS);
        assertThat(limiter.tryAcquire("upsert-chunks|c")).as("window elapsed: logs, naming the two suppressed").isEqualTo(2);
        assertThat(limiter.tryAcquire("upsert-chunks|c")).isEqualTo(-1);
    }

    @Test
    void keysAreIndependent() {
        var now = new AtomicLong(5L);
        var limiter = new OwnerlessLogLimiter(now::get);
        assertThat(limiter.tryAcquire("upsert-chunks|a")).isEqualTo(0);
        assertThat(limiter.tryAcquire("upsert-chunks|b")).as("another collection").isEqualTo(0);
        assertThat(limiter.tryAcquire("store-put|a")).as("another route").isEqualTo(0);
        assertThat(limiter.tryAcquire("upsert-chunks|a")).isEqualTo(-1);
    }

    @Test
    void aClockAtZeroStillLogsTheFirstLine() {
        var limiter = new OwnerlessLogLimiter(() -> 0L);
        assertThat(limiter.tryAcquire("k")).isEqualTo(0);
        assertThat(limiter.tryAcquire("k")).isEqualTo(-1);
    }

    @Test
    void aKeyThatNamesAnotherTenantIsItsOwnKey() {
        var limiter = new OwnerlessLogLimiter(() -> 5L);
        assertThat(limiter.tryAcquire("upsert-chunks|t1|c")).isEqualTo(0);
        assertThat(limiter.tryAcquire("upsert-chunks|t2|c")).as("same route and collection, another tenant").isEqualTo(0);
        assertThat(limiter.tryAcquire("upsert-chunks|t1|c")).isEqualTo(-1);
    }

    @Test
    void pastTheKeyCapWithEveryKeyLive_newKeysShareOneBucket_soTheLogDoesNotFlood() {
        var now = new AtomicLong(5L);
        var limiter = new OwnerlessLogLimiter(now::get);
        for (int i = 0; i < OwnerlessLogLimiter.MAX_KEYS; i++) {
            assertThat(limiter.tryAcquire("k" + i)).isEqualTo(0);
        }
        assertThat(limiter.size()).isEqualTo(OwnerlessLogLimiter.MAX_KEYS);

        assertThat(limiter.tryAcquire("overflow-a")).as("the shared bucket's first line").isEqualTo(0);
        int logged = 0;
        for (int i = 0; i < 1_000; i++) {
            if (limiter.tryAcquire("overflow-" + i) >= 0) {
                logged++;
            }
        }
        assertThat(logged).as("a thousand more distinct new keys in the same window log nothing").isZero();
        assertThat(limiter.size()).as("memory does not grow past the cap").isEqualTo(OwnerlessLogLimiter.MAX_KEYS);
        assertThat(limiter.tryAcquire("k0")).as("a remembered key is still limited").isEqualTo(-1);

        now.addAndGet(OwnerlessLogLimiter.WINDOW_MS);
        assertThat(limiter.tryAcquire("overflow-late"))
            .as("next window: every key's window has elapsed, so the sweep makes room and the key logs")
            .isEqualTo(0);
        assertThat(limiter.size()).as("the stale keys were evicted").isEqualTo(1);
    }

    @Test
    void atTheCap_keysWhoseWindowHasElapsedAreEvictedSoANewKeyGetsItsOwnBucket() {
        var now = new AtomicLong(5L);
        var limiter = new OwnerlessLogLimiter(now::get);
        for (int i = 0; i < OwnerlessLogLimiter.MAX_KEYS; i++) {
            limiter.tryAcquire("k" + i);
        }
        now.addAndGet(OwnerlessLogLimiter.WINDOW_MS);

        assertThat(limiter.tryAcquire("fresh")).as("a new key logs").isEqualTo(0);
        assertThat(limiter.size()).as("the expired keys were swept, the new key remembered").isEqualTo(1);
        assertThat(limiter.tryAcquire("fresh")).as("and is limited as its own key").isEqualTo(-1);
        assertThat(limiter.tryAcquire("another-fresh")).as("with room again, another key is its own bucket").isEqualTo(0);
        assertThat(limiter.size()).isEqualTo(2);
    }

    @Test
    void theExpiredKeySweepIsThrottled_soAFullMapOfLiveKeysIsNotRescannedPerWrite() {
        var now = new AtomicLong(5L);
        var limiter = new OwnerlessLogLimiter(now::get);
        for (int i = 0; i < OwnerlessLogLimiter.MAX_KEYS; i++) {
            limiter.tryAcquire("k" + i);
        }
        limiter.tryAcquire("first-overflow");                       // a sweep runs (nothing expired yet); overflow
        now.addAndGet(OwnerlessLogLimiter.WINDOW_MS - 1);           // keys still live
        limiter.tryAcquire("second-overflow");                      // a second sweep, finds none
        assertThat(limiter.size()).isEqualTo(OwnerlessLogLimiter.MAX_KEYS);
        now.addAndGet(1);                                           // every key is stale now, 1 ms after the last sweep
        limiter.tryAcquire("third");
        assertThat(limiter.size()).as("a sweep 1 ms after the last one is skipped").isEqualTo(OwnerlessLogLimiter.MAX_KEYS);
        now.addAndGet(OwnerlessLogLimiter.SWEEP_INTERVAL_MS);
        assertThat(limiter.tryAcquire("fourth")).isEqualTo(0);
        assertThat(limiter.size()).as("once the interval has passed the stale keys are gone").isEqualTo(1);
    }
}
