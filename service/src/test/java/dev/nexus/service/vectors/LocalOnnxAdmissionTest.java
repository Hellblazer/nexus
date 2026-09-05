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
    void queryTimeoutMsFromEnv_defaultsToOneThirdOfTheSearchStatementTimeout() {
        // Review round 3: the admission wait precedes PgSession's
        // statement-timeout GUC on the later PG statement, so the two
        // windows are additive, not shared — defaulting to the FULL
        // search-statement-timeout (round 2's default) let a contended
        // search take ~2x the intended SLA. One third keeps
        // admission + embed + PG-statement within roughly the old envelope.
        assertThat(LocalOnnxAdmission.queryTimeoutMsFromEnv(name -> null, 30_000L))
                .isEqualTo(10_000L);
        assertThat(LocalOnnxAdmission.queryTimeoutMsFromEnv(name -> null, 9_000L))
                .isEqualTo(3_000L);
    }

    @Test
    void queryTimeoutMsFromEnv_defaultNeverGoesToZero_forATinySearchStatementTimeout() {
        // searchStatementTimeoutMs=2 -> 2/3=0 by integer division; the
        // Math.max(1, ...) floor keeps the derived default positive so the
        // constructor's own positivity check never rejects it.
        assertThat(LocalOnnxAdmission.queryTimeoutMsFromEnv(name -> null, 2L)).isEqualTo(1L);
    }

    @Test
    void queryTimeoutMsFromEnv_explicitOverrideWins_andIsNotDivided() {
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

    // ── real entry points ────────────────────────────────────────────────

    @Test
    void fromEnv_realEntryPoint_constructsAValidInstance() {
        // No overrides in the test process env: exercises the real
        // permits/query-timeout derivation (including the 1/3-of-search-
        // statement-timeout default) end to end.
        LocalOnnxAdmission admission = LocalOnnxAdmission.fromEnv();
        assertThat(admission.permits()).isPositive();
        assertThat(admission.queryTimeoutMs()).isPositive();
    }

    @Test
    void queryTimeoutMsFromEnv_realEntryPoint_returnsPositiveValue() {
        assertThat(LocalOnnxAdmission.queryTimeoutMsFromEnv()).isPositive();
    }

    // ── nexus-s71lr deliverable 1/2: queue depth + thread width, READ not
    //    computed, for the engine progress log line + GET /v1/status ───────

    @Test
    void queueLength_zeroWhenNoWaitersAndPermitsFree() throws InterruptedException {
        LocalOnnxAdmission admission = new LocalOnnxAdmission(2, 1000);
        assertThat(admission.queueLength()).isZero();
        admission.acquireInterruptible();
        // One permit held, one free -- still nobody WAITING (queueLength
        // counts blocked acquirers, not in-flight holders).
        assertThat(admission.queueLength()).isZero();
    }

    @Test
    void queueLength_countsThreadsBlockedOnAFullyHeldPermitSet() throws Exception {
        LocalOnnxAdmission admission = new LocalOnnxAdmission(1, 1000);
        admission.acquireInterruptible(); // the one permit is now held

        Thread waiter = new Thread(() -> {
            try {
                admission.acquireInterruptible();
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        });
        waiter.start();
        // Poll rather than a fixed sleep -- bounded, avoids a flaky race
        // against the waiter thread actually reaching the blocking acquire.
        long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(2);
        while (admission.queueLength() == 0 && System.nanoTime() < deadline) {
            Thread.onSpinWait();
        }
        assertThat(admission.queueLength()).isEqualTo(1);

        admission.release();
        waiter.join(2000);
        assertThat(waiter.isAlive()).isFalse();
    }

    @Test
    void inFlightCount_reflectsHeldPermitsNotQueueLength() throws InterruptedException {
        LocalOnnxAdmission admission = new LocalOnnxAdmission(3, 1000);
        assertThat(admission.inFlightCount()).isZero();
        admission.acquireInterruptible();
        admission.acquireInterruptible();
        assertThat(admission.inFlightCount()).isEqualTo(2);
        admission.release();
        assertThat(admission.inFlightCount()).isEqualTo(1);
        admission.release();
        assertThat(admission.inFlightCount()).isZero();
    }
}
