// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.4: the sweep keeps, beside the name-keyed since-history, an id-keyed one, so the plan check's first trigger ("a new or recovered index
 * is checked at once", {@link PciPlanCheck}) fires for an id-predicate index once the latch makes it the serving one. Without it every id index
 * reports {@code Instant.EPOCH} and a recovered one is never checked again at once.
 */
class PciIndexSweepIdSinceTest {

    private static final String M = "minilm-l6-v2-384";
    private static final String T = "t1";
    private static final String HASH = "pci_" + "a".repeat(24);
    private static final PciCatalog.Snapshot WITH_ID = snapshot(PciCatalog.Index.byId(HASH, true, 7L, null));
    private static final PciCatalog.Snapshot WITHOUT = snapshot();

    private PciIndexSweep sweep;

    @AfterEach
    void stop() {
        if (sweep != null) {
            sweep.stop();
        }
    }

    private static PciCatalog.Snapshot snapshot(PciCatalog.Index... indexes) {
        return new PciCatalog.Snapshot(List.of(new PciCatalog.Leaf("nexus", "chunks_leaf", M, T, List.of(indexes))));
    }

    private static final class MovingClock extends Clock {
        private volatile Instant now;

        MovingClock(Instant now) {
            this.now = now;
        }

        void set(Instant t) {
            now = t;
        }

        @Override public java.time.ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override public Clock withZone(java.time.ZoneId zone) {
            return this;
        }

        @Override public Instant instant() {
            return now;
        }
    }

    @Test
    void aNewIdIndexReportsARecentSince_aSteadyOneKeepsIt_andARecoveredOneIsNewAgain_soThePlanCheckIsDueAtOnce() {
        Instant t0 = Instant.parse("2026-11-23T10:00:00Z");
        var clock = new MovingClock(t0);
        var reading = new AtomicReference<>(WITH_ID);
        sweep = new PciIndexSweep(reading::get, PciIndexSweepTest.SETTINGS, clock);
        PciPlanCheck check = new PciPlanCheck(1_000_000, Duration.ofMinutes(15), clock, (ctx, q) -> "");

        assertThat(sweep.refresh()).isTrue();
        PciIndexSet.ValidIndex first = sweep.validIdIndex(M, T, 7L).orElseThrow();
        assertThat(first.since()).as("a new id index is listed from now, not from the epoch").isEqualTo(t0);
        assertThat(first.name()).isEqualTo(PciCatalog.indexNameById(M, T, 7L));
        assertThat(check.due(first)).as("the first eligible arm of a new index").isTrue();
        assertThat(check.due(first)).as("and not again in the same quarter hour").isFalse();

        clock.set(t0.plusSeconds(60));
        assertThat(sweep.refresh()).isTrue();
        assertThat(sweep.validIdIndex(M, T, 7L).orElseThrow().since()).as("listed without a break: the date is kept").isEqualTo(t0);

        clock.set(t0.plusSeconds(120));
        reading.set(WITHOUT);
        assertThat(sweep.refresh()).isTrue();
        assertThat(sweep.validIdIndex(M, T, 7L)).as("gone while INVALID or dropped").isEmpty();

        Instant back = t0.plusSeconds(180);
        clock.set(back);
        reading.set(WITH_ID);
        assertThat(sweep.refresh()).isTrue();
        PciIndexSet.ValidIndex recovered = sweep.validIdIndex(M, T, 7L).orElseThrow();
        assertThat(recovered.since()).as("a recovered index counts as a new run").isEqualTo(back);
        assertThat(check.due(recovered)).as("and is checked at once").isTrue();

        assertThat(sweep.validIdIndex(M, T, 8L)).as("another id has none").isEmpty();
    }
}
