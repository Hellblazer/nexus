// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.sql.SQLTransientConnectionException;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-00wsf residual, review round 2 (T2 [24238]/[24239]) — {@link
 * LocalOnnxAdmission}'s resolver logic, constructor validation, and the
 * typed-retryable timeout exception shape. The shared-across-wrappers
 * behaviour itself is proven by {@link AdmissionControlledEmbedderTest} and
 * {@link AdmissionControlledRerankerTest}, which exercise real acquire/
 * release traffic through this class.
 */
class LocalOnnxAdmissionTest {

    // ── permitsFromEnv ───────────────────────────────────────────────────

    @Test
    void permitsFromEnv_defaultsToHalfAvailableCores_minimumOne() {
        assertThat(LocalOnnxAdmission.permitsFromEnv(name -> null, 16)).isEqualTo(8);
        assertThat(LocalOnnxAdmission.permitsFromEnv(name -> null, 1)).isEqualTo(1);
        assertThat(LocalOnnxAdmission.permitsFromEnv(name -> null, 3)).isEqualTo(1);
    }

    @Test
    void permitsFromEnv_explicitOverrideWins() {
        Map<String, String> env = Map.of(LocalOnnxAdmission.PERMITS_ENV, "5");
        assertThat(LocalOnnxAdmission.permitsFromEnv(env::get, 16)).isEqualTo(5);
    }

    @Test
    void permitsFromEnv_refusesZeroOrNegativeOverride() {
        Map<String, String> zero = Map.of(LocalOnnxAdmission.PERMITS_ENV, "0");
        assertThatThrownBy(() -> LocalOnnxAdmission.permitsFromEnv(zero::get, 16))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(LocalOnnxAdmission.PERMITS_ENV);

        Map<String, String> negative = Map.of(LocalOnnxAdmission.PERMITS_ENV, "-3");
        assertThatThrownBy(() -> LocalOnnxAdmission.permitsFromEnv(negative::get, 16))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void permitsFromEnv_refusesNonNumericOverride() {
        Map<String, String> junk = Map.of(LocalOnnxAdmission.PERMITS_ENV, "not-a-number");
        assertThatThrownBy(() -> LocalOnnxAdmission.permitsFromEnv(junk::get, 16))
                .isInstanceOf(IllegalArgumentException.class);
    }

    // ── queryTimeoutMsFromEnv ────────────────────────────────────────────

    @Test
    void queryTimeoutMsFromEnv_defaultsToTheSuppliedSearchStatementTimeout() {
        assertThat(LocalOnnxAdmission.queryTimeoutMsFromEnv(name -> null, 30_000L))
                .isEqualTo(30_000L);
    }

    @Test
    void queryTimeoutMsFromEnv_explicitOverrideWins() {
        Map<String, String> env = Map.of(LocalOnnxAdmission.QUERY_TIMEOUT_ENV, "5000");
        assertThat(LocalOnnxAdmission.queryTimeoutMsFromEnv(env::get, 30_000L)).isEqualTo(5000L);
    }

    @Test
    void queryTimeoutMsFromEnv_refusesZeroOrNegativeOverride() {
        Map<String, String> zero = Map.of(LocalOnnxAdmission.QUERY_TIMEOUT_ENV, "0");
        assertThatThrownBy(() -> LocalOnnxAdmission.queryTimeoutMsFromEnv(zero::get, 30_000L))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(LocalOnnxAdmission.QUERY_TIMEOUT_ENV);

        Map<String, String> negative = Map.of(LocalOnnxAdmission.QUERY_TIMEOUT_ENV, "-1");
        assertThatThrownBy(() -> LocalOnnxAdmission.queryTimeoutMsFromEnv(negative::get, 30_000L))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void queryTimeoutMsFromEnv_refusesNonNumericOverride() {
        Map<String, String> junk = Map.of(LocalOnnxAdmission.QUERY_TIMEOUT_ENV, "soon");
        assertThatThrownBy(() -> LocalOnnxAdmission.queryTimeoutMsFromEnv(junk::get, 30_000L))
                .isInstanceOf(IllegalArgumentException.class);
    }

    // ── constructor validation ───────────────────────────────────────────

    @Test
    void constructor_refusesNonPositivePermits() {
        assertThatThrownBy(() -> new LocalOnnxAdmission(0, 1000))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new LocalOnnxAdmission(-1, 1000))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void constructor_refusesNonPositiveQueryTimeout() {
        assertThatThrownBy(() -> new LocalOnnxAdmission(4, 0))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new LocalOnnxAdmission(4, -1))
                .isInstanceOf(IllegalArgumentException.class);
    }

    // ── acquire/release primitives ───────────────────────────────────────

    @Test
    void acquireInterruptible_andRelease_roundTrip() throws Exception {
        LocalOnnxAdmission admission = new LocalOnnxAdmission(1, 1000);
        admission.acquireInterruptible();
        admission.release();
        // A second acquire must succeed promptly — proves release() actually freed the permit.
        assertThat(admission.tryAcquire(1000)).isTrue();
        admission.release();
    }

    @Test
    void tryAcquire_timesOutWhenNoPermitFree() throws Exception {
        LocalOnnxAdmission admission = new LocalOnnxAdmission(1, 1000);
        admission.acquireInterruptible();
        try {
            long start = System.nanoTime();
            boolean admitted = admission.tryAcquire(150);
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);
            assertThat(admitted).isFalse();
            assertThat(elapsedMs).isGreaterThanOrEqualTo(140); // small slack under 150ms budget
        } finally {
            admission.release();
        }
    }

    // ── admissionTimeoutException shape ──────────────────────────────────

    @Test
    void admissionTimeoutException_carriesSqlTransientConnectionExceptionInCauseChain() {
        RuntimeException e = LocalOnnxAdmission.admissionTimeoutException("interactive embed", 250);
        assertThat(e.getCause()).isInstanceOf(SQLTransientConnectionException.class);
        assertThat(e.getMessage()).contains("interactive embed").contains("250");
    }
}
