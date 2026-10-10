// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PciIndexSet.ValidIndex;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-227 Step 2 (nexus-43ulx.25 fix round): when the plan check samples and what it says about a plan that did not
 * use the index. No database: the decision ({@link PciPlanCheck#due}) and the plan reader
 * ({@link PciPlanCheck#describePlan}) are plain methods. The path through a real arm and a real EXPLAIN is in
 * {@code PciPlanAndRecallIntegrationTest}.
 */
class PciPlanCheckTest {

    static final Instant T0 = Instant.parse("2026-10-10T08:00:00Z");
    static final Duration INTERVAL = Duration.ofMinutes(15);
    static final PciPlanCheck.PlanSource NO_PLAN = (ctx, statement) -> "";

    static ValidIndex index(String name, Instant since) {
        return new ValidIndex(name, since);
    }

    private static final class MutableClock extends Clock {
        private volatile Instant now;

        MutableClock(Instant now) {
            this.now = now;
        }

        void advance(Duration d) {
            now = now.plus(d);
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

    // ── what production ships with ────────────────────────────────────────────────────────────────────────────

    @Test
    void production_shipsTheDefaultCadence_oneInAThousandAndOnePerIndexPerFifteenMinutes() {
        PciPlanCheck production = PciPlanCheck.production();

        assertThat(PciPlanCheck.DEFAULT_EVERY).isEqualTo(1000);
        assertThat(PciPlanCheck.DEFAULT_INTERVAL).isEqualTo(Duration.ofMinutes(15));
        assertThat(production.every()).isEqualTo(PciPlanCheck.DEFAULT_EVERY);
        assertThat(production.interval()).isEqualTo(PciPlanCheck.DEFAULT_INTERVAL);
    }

    @Test
    void production_samplesTheFirstArmOfAnIndex_thenTheThousandthArmOverall_andNothingBetween() {
        PciPlanCheck production = PciPlanCheck.production();
        ValidIndex idx = index("pci_a", Instant.EPOCH);

        assertThat(production.due(idx)).as("arm 1: the first eligible arm of an index").isTrue();
        for (int i = 2; i < 1000; i++) {
            assertThat(production.due(idx)).as("arm %d, same index, same quarter hour", i).isFalse();
        }
        assertThat(production.due(idx)).as("arm 1000: the process-wide counter").isTrue();
        assertThat(production.due(idx)).as("arm 1001").isFalse();
    }

    // ── the three triggers ────────────────────────────────────────────────────────────────────────────────────

    @Test
    void theFirstEligibleArmOfEachIndexIsSampled_andOnlyTheFirst() {
        var clock = new MutableClock(T0);
        PciPlanCheck check = new PciPlanCheck(1_000_000, INTERVAL, clock, NO_PLAN);

        assertThat(check.due(index("pci_a", Instant.EPOCH))).isTrue();
        assertThat(check.due(index("pci_a", Instant.EPOCH))).isFalse();
        assertThat(check.due(index("pci_b", Instant.EPOCH))).as("another index has its own first arm").isTrue();
        assertThat(check.due(index("pci_b", Instant.EPOCH))).isFalse();
        assertThat(check.due(index("pci_a", Instant.EPOCH))).isFalse();
    }

    @Test
    void anIndexIsSampledAtMostOncePerInterval() {
        var clock = new MutableClock(T0);
        PciPlanCheck check = new PciPlanCheck(1_000_000, INTERVAL, clock, NO_PLAN);
        ValidIndex a = index("pci_a", Instant.EPOCH);
        assertThat(check.due(a)).isTrue();

        clock.advance(INTERVAL.minusSeconds(1));
        assertThat(check.due(a)).as("14m59s later").isFalse();
        clock.advance(Duration.ofSeconds(1));
        assertThat(check.due(a)).as("15m later").isTrue();
        assertThat(check.due(a)).as("the interval restarts at the sample").isFalse();
        clock.advance(INTERVAL);
        assertThat(check.due(a)).isTrue();
    }

    @Test
    void anIndexTheSetGainedAfterItsLastSampleIsSampledAtOnce() {
        var clock = new MutableClock(T0);
        PciPlanCheck check = new PciPlanCheck(1_000_000, INTERVAL, clock, NO_PLAN);
        assertThat(check.due(index("pci_a", Instant.EPOCH))).isTrue();

        // Dropped and rebuilt a minute later: the set now lists it since T0 + 60 s, after the last sample.
        clock.advance(Duration.ofSeconds(90));
        Instant regained = T0.plusSeconds(60);
        assertThat(check.due(index("pci_a", regained))).as("a new run of the same name").isTrue();
        assertThat(check.due(index("pci_a", regained))).as("and then once, not on every arm").isFalse();
    }

    @Test
    void theGlobalCounterStillSamplesEveryNth_whateverTheIndex() {
        var clock = new MutableClock(T0);
        PciPlanCheck check = new PciPlanCheck(5, INTERVAL, clock, NO_PLAN);
        ValidIndex a = index("pci_a", Instant.EPOCH);
        ValidIndex b = index("pci_b", Instant.EPOCH);

        assertThat(check.due(a)).as("1: first of a").isTrue();
        assertThat(check.due(b)).as("2: first of b").isTrue();
        assertThat(check.due(a)).isFalse();      // 3
        assertThat(check.due(b)).isFalse();      // 4
        assertThat(check.due(a)).as("5: the counter").isTrue();
        assertThat(check.due(a)).isFalse();      // 6
        assertThat(check.due(b)).isFalse();      // 7
    }

    @Test
    void aCounterOnlyCheck_hasNoFirstArmAndNoInterval() {
        PciPlanCheck check = new PciPlanCheck(3, NO_PLAN);
        assertThat(check.interval()).isNull();
        ValidIndex a = index("pci_a", Instant.EPOCH);

        assertThat(check.due(a)).isFalse();
        assertThat(check.due(a)).isFalse();
        assertThat(check.due(a)).as("the 3rd").isTrue();
        assertThat(check.due(a)).isFalse();
    }

    @Test
    void concurrentArmsOfOneIndexSampleOnce() throws Exception {
        var clock = new MutableClock(T0);
        PciPlanCheck check = new PciPlanCheck(1_000_000, INTERVAL, clock, NO_PLAN);
        ValidIndex a = index("pci_a", Instant.EPOCH);
        var sampled = new java.util.concurrent.atomic.AtomicInteger();
        var pool = java.util.concurrent.Executors.newFixedThreadPool(8);
        try {
            var start = new java.util.concurrent.CountDownLatch(1);
            var done = new java.util.ArrayList<java.util.concurrent.Future<?>>();
            for (int i = 0; i < 8; i++) {
                done.add(pool.submit(() -> {
                    start.await();
                    for (int j = 0; j < 500; j++) {
                        if (check.due(a)) {
                            sampled.incrementAndGet();
                        }
                    }
                    return null;
                }));
            }
            start.countDown();
            for (var f : done) {
                f.get();
            }
        } finally {
            pool.shutdownNow();
        }
        assertThat(sampled).as("4000 arms inside one interval: the first one only").hasValue(1);
    }

    @Test
    void theConstructorRejectsABadCadence() {
        assertThatThrownBy(() -> new PciPlanCheck(0, NO_PLAN)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new PciPlanCheck(1, Duration.ZERO, Clock.systemUTC(), NO_PLAN))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new PciPlanCheck(1, Duration.ofSeconds(-1), Clock.systemUTC(), NO_PLAN))
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── what a plan that did not use the index says ───────────────────────────────────────────────────────────

    static final String SHARED_INDEX_PLAN = """
        Limit  (cost=12.50..80.10 rows=40 width=96)
          ->  Index Scan using chunks_x_embedding_384_idx on chunks_x  (cost=12.50..9000.00 rows=5000 width=96)
                Order By: (embedding_384 <=> '[0.1,0.2]'::vector)
                Filter: (collection = ANY ('{c1}'::text[]))
        """;

    @Test
    void describePlan_namesTheTopNodeAndTheIndexThePlannerChose() {
        assertThat(PciPlanCheck.describePlan(SHARED_INDEX_PLAN)).isEqualTo(
            "top=\"Limit\" scans=\"Index Scan using chunks_x_embedding_384_idx on chunks_x\"");
    }

    @Test
    void describePlan_namesASeqScan() {
        String plan = """
            Limit  (cost=1.00..2.00 rows=40 width=96)
              ->  Sort  (cost=1.00..2.00 rows=100 width=96)
                    ->  Seq Scan on chunks_x  (cost=0.00..1.00 rows=100 width=96)
            """;
        assertThat(PciPlanCheck.describePlan(plan)).isEqualTo(
            "top=\"Limit\" scans=\"Seq Scan on chunks_x\"");
    }

    @Test
    void describePlan_listsEachScanOnce_andBoundsTheList() {
        StringBuilder plan = new StringBuilder("Append  (cost=0.00..9.00 rows=1 width=1)\n");
        for (int i = 0; i < 8; i++) {
            plan.append("  ->  Seq Scan on leaf_").append(i).append("  (cost=0.00..1.00 rows=1 width=1)\n");
            plan.append("  ->  Seq Scan on leaf_0  (cost=0.00..1.00 rows=1 width=1)\n");
        }
        String described = PciPlanCheck.describePlan(plan.toString());
        assertThat(described).startsWith("top=\"Append\"");
        assertThat(described.split("Seq Scan on", -1).length - 1).isEqualTo(5);
    }

    @Test
    void describePlan_neverThrowsOnATextItDoesNotKnow() {
        assertThat(PciPlanCheck.describePlan(null)).isEqualTo("top=\"\" scans=\"\"");
        assertThat(PciPlanCheck.describePlan("")).isEqualTo("top=\"\" scans=\"\"");
        assertThat(PciPlanCheck.describePlan("stub plan")).isEqualTo("top=\"\" scans=\"\"");
    }
}
