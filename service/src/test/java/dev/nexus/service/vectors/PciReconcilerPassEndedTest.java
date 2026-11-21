// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.PgSession.PciSettings;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.util.Random;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), item (B): a reconciler pass tells the completion check when it ends
 * ({@link PciReconciler#onPassEnded}), however it ends. Hermetic: the builder's database is unreachable, so each pass ends
 * inactive at once, and a pass that ends that way still counts.
 */
class PciReconcilerPassEndedTest {

    private static PciReconciler reconciler() {
        PciSettings s = new PciSettings(true, 20_000, 600, 16);
        var builder = new PciBuilderSession("jdbc:postgresql://127.0.0.1:1/none", "u", "p", "n1", s);
        return new PciReconciler(() -> {
            throw new IllegalStateException("no catalog in this test");
        }, builder, new PciIndexSweep(() -> null, s), s, Clock.systemUTC(), PciReconciler.SET_LOCAL_TENANT,
            PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(600), new Random(7));
    }

    @Test
    void theListenerRunsOnceAtTheEndOfEachPass() {
        var reconciler = reconciler();
        var ended = new AtomicInteger();
        reconciler.onPassEnded(ended::incrementAndGet);

        reconciler.reconcileOnce();
        assertThat(ended.get()).isEqualTo(1);
        reconciler.reconcileOnce();
        assertThat(ended.get()).as("one per pass").isEqualTo(2);
        assertThat(reconciler.passes()).as("non-vacuity: two passes ran").isEqualTo(2);
    }

    @Test
    void aListenerThatThrowsDoesNotFailThePass() {
        var reconciler = reconciler();
        reconciler.onPassEnded(() -> {
            throw new IllegalStateException("the listener is broken");
        });

        var report = reconciler.reconcileOnce();

        assertThat(report).isNotNull();
    }

    @Test
    void noListenerIsTheDefault() {
        assertThat(reconciler().reconcileOnce()).isNotNull();
    }
}
