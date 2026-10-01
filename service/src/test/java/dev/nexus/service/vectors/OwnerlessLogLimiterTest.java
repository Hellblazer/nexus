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
}
