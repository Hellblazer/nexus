// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.exception.DataAccessException;
import org.junit.jupiter.api.Test;

import java.sql.SQLException;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Unit coverage for the {@link DeadlockRetry} belt (bead nexus-ps9wb). The
 * integration-level proof that the per-site chash sort prevents the real deadlock
 * lives in {@code PgVectorUpsertDeadlockTest}; this suite exercises the RETRY path
 * itself — the piece that guards residual cross-path deadlocks the sort cannot rule
 * out — with a fabricated 40P01, deterministically and without a database.
 */
class DeadlockRetryTest {

    /** A 40P01 wrapped exactly as jOOQ surfaces it: DataAccessException over SQLException. */
    private static RuntimeException deadlock() {
        return new DataAccessException("deadlock detected",
                new SQLException("deadlock detected", DeadlockRetry.SQLSTATE_DEADLOCK));
    }

    @Test
    void succeedsFirstTryRunsOnce() {
        AtomicInteger calls = new AtomicInteger();
        String out = DeadlockRetry.run("ctx", () -> {
            calls.incrementAndGet();
            return "ok";
        });
        assertThat(out).isEqualTo("ok");
        assertThat(calls).hasValue(1);
    }

    @Test
    void retriesDeadlockThenSucceeds() {
        AtomicInteger calls = new AtomicInteger();
        String out = DeadlockRetry.run("ctx", () -> {
            // Deadlock on the first two attempts, succeed on the third.
            if (calls.incrementAndGet() < 3) throw deadlock();
            return "recovered";
        });
        assertThat(out).isEqualTo("recovered");
        assertThat(calls).hasValue(3);
    }

    @Test
    void rethrowsAfterExhaustingAttempts() {
        AtomicInteger calls = new AtomicInteger();
        assertThatThrownBy(() -> DeadlockRetry.run("ctx", () -> {
            calls.incrementAndGet();
            throw deadlock();
        })).isInstanceOf(DataAccessException.class);
        // Bounded: initial try + retries == MAX_ATTEMPTS total invocations.
        assertThat(calls).hasValue(DeadlockRetry.MAX_ATTEMPTS);
    }

    @Test
    void nonDeadlockPropagatesImmediatelyWithoutRetry() {
        AtomicInteger calls = new AtomicInteger();
        assertThatThrownBy(() -> DeadlockRetry.run("ctx", () -> {
            calls.incrementAndGet();
            throw new IllegalStateException("not a deadlock");
        })).isInstanceOf(IllegalStateException.class);
        assertThat(calls).hasValue(1);  // no retry on a non-40P01 error
    }

    @Test
    void isDeadlockWalksCauseChain() {
        assertThat(DeadlockRetry.isDeadlock(deadlock())).isTrue();
        // Nested one level deeper (TenantScope may re-wrap).
        assertThat(DeadlockRetry.isDeadlock(new RuntimeException("wrap", deadlock()))).isTrue();
        assertThat(DeadlockRetry.isDeadlock(
                new RuntimeException(new SQLException("fk violation", "23503")))).isFalse();
        assertThat(DeadlockRetry.isDeadlock(new RuntimeException("no sql cause"))).isFalse();
        assertThat(DeadlockRetry.isDeadlock(null)).isFalse();
    }
}
