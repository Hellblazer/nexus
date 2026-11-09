// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.vectors.PciCanary.Hit;
import dev.nexus.service.vectors.PciCanary.Leg;
import dev.nexus.service.vectors.PciCanary.Result;
import dev.nexus.service.vectors.PciCanary.State;
import dev.nexus.service.vectors.PciCanary.Target;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.BiFunction;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-230 P1.3 (nexus-4lnn1.1.4): the canary's scoring and its unmeasured reasons, against scripted statements. No
 * database: the substrate half (the real serving path, the real planner, a real index) is
 * {@code PciPlanAndRecallIntegrationTest}.
 */
class PciCanaryTest {

    static final String MODEL = "minilm-l6-v2-384";
    static final Target TARGET = new Target("nexus.chunks_x", "t1", MODEL, "knowledge__c__minilm-l6-v2-384__v1",
        "pci_abc");
    static final String PLAN_USING = "Limit  (cost=1.00..2.00 rows=40 width=8)\n  ->  Index Scan using pci_abc on chunks_x";
    static final String PLAN_SEQ = "Limit  (cost=1.00..2.00 rows=40 width=8)\n  ->  Seq Scan on chunks_x";

    ListAppender<ILoggingEvent> logs;
    Logger logger;

    @BeforeEach
    void capture() {
        logger = (Logger) LoggerFactory.getLogger(PciCanary.class);
        logs = new ListAppender<>();
        logs.start();
        logger.addAppender(logs);
    }

    @AfterEach
    void release() {
        logger.detachAppender(logs);
        logs.stop();
    }

    static float[] vec(float seed) {
        return new float[] {seed, 1f, 2f};
    }

    static List<PciQueryRingReader.RingEntry> ring(int n) {
        List<PciQueryRingReader.RingEntry> out = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            out.add(new PciQueryRingReader.RingEntry(MODEL, vec(i), OffsetDateTime.now()));
        }
        return out;
    }

    /** Hits id0..id(n-1) at distance 0.001 * (index + 1): the exact order. */
    static List<Hit> exactPage(int n) {
        List<Hit> hits = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            hits.add(new Hit("id" + i, 0.001 * (i + 1)));
        }
        return hits;
    }

    /** Scripted statements: the HNSW page for (query, k) comes from {@code hnsw}, the exact page is {@link #exactPage}. */
    static final class Script implements PciCanary.Statements {
        final BiFunction<Integer, Integer, List<Hit>> hnsw;
        final AtomicInteger hnswCalls = new AtomicInteger();
        final AtomicInteger exactCalls = new AtomicInteger();
        final List<Boolean> explained = new ArrayList<>();
        String plan = PLAN_USING;
        boolean routedExact;
        int exactRows = 120;
        RuntimeException failAt;

        Script(BiFunction<Integer, Integer, List<Hit>> hnsw) {
            this.hnsw = hnsw;
        }

        @Override
        public Leg hnsw(Target target, float[] query, int k, boolean explain) {
            if (failAt != null) {
                throw failAt;
            }
            hnswCalls.incrementAndGet();
            explained.add(explain);
            int q = (int) query[0];
            return new Leg(hnsw.apply(q, k), explain ? plan : null, routedExact);
        }

        @Override
        public List<Hit> exact(Target target, float[] query, int k) {
            exactCalls.incrementAndGet();
            return exactPage(Math.min(exactRows, k));
        }
    }

    static List<Hit> perfect(int q, int k) {
        return exactPage(k);
    }

    PciCanary canary(int queriesInRing, PciCanary.Statements statements) {
        return new PciCanary((t, c) -> ring(queriesInRing), statements);
    }

    PciCanary canary(PciCanary.RingSource ring, PciCanary.Statements statements) {
        return new PciCanary(ring, statements);
    }

    // -- scoring -----------------------------------------------------------------------------------------

    @Test
    void perfectPages_areGreen_withRecallOne_andOneStatementPerQueryPerK() {
        Script script = new Script(PciCanaryTest::perfect);

        Result r = canary(10, script).run(TARGET, "after_build");

        assertThat(r.state()).isEqualTo(State.GREEN);
        assertThat(r.queries()).isEqualTo(10);
        assertThat(r.minRecall()).isEqualTo(1.0);
        assertThat(r.meanRecall()).isEqualTo(1.0);
        assertThat(r.top1Misses()).isZero();
        assertThat(r.redSample()).as("only a red run keeps its sample").isNull();
        assertThat(script.hnswCalls.get()).as("five k per query").isEqualTo(50);
        assertThat(script.exactCalls.get()).as("one exact statement per query").isEqualTo(10);
        assertThat(script.explained.get(0)).as("the first HNSW statement is explained").isTrue();
        assertThat(script.explained.subList(1, 50)).as("and no other").doesNotContain(true);
    }

    @Test
    void aTieAtTheKthDistance_isNotAMiss() {
        // The page returns id(k) in place of id(k-1) at the same distance as the k-th exact row.
        Script script = new Script((q, k) -> {
            List<Hit> page = new ArrayList<>(exactPage(k));
            page.set(k - 1, new Hit("tied-" + k, 0.001 * k));
            return page;
        });

        Result r = canary(3, script).run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.GREEN);
        assertThat(r.meanRecall()).isEqualTo(1.0);
    }

    @Test
    void aRowPastTheKthDistance_isAMiss_andLowersRecallBy1OverK() {
        Script script = new Script((q, k) -> {
            List<Hit> page = new ArrayList<>(exactPage(k));
            page.set(k - 1, new Hit("far-" + k, 0.001 * k + 0.5));
            return page;
        });

        Result r = canary(3, script).run(TARGET, "daily");

        // One miss in every page: recall (k-1)/k at k = 40, 60, 80, 100, 120.
        double expected = (39d / 40 + 59d / 60 + 79d / 80 + 99d / 100 + 119d / 120) / 5;
        assertThat(r.meanRecall()).isCloseTo(expected, org.assertj.core.data.Offset.offset(1e-12));
        assertThat(r.minRecall()).isCloseTo(39d / 40, org.assertj.core.data.Offset.offset(1e-12));
        assertThat(r.top1Misses()).isZero();
        assertThat(r.state()).as("one miss per page of k=40..120 is a mean of %s, below the 0.99 bar", r.meanRecall())
            .isEqualTo(State.RED);
    }

    @Test
    void aMeanAboveTheBarButBelowOne_isGreen() {
        // One row past the k-th distance in the k=120 page only: recall 119/120 there, 1.0 at every other k.
        Script script = new Script((q, k) -> {
            List<Hit> page = new ArrayList<>(exactPage(k));
            if (k == 120) {
                page.set(k - 1, new Hit("far-" + k, 0.001 * k + 0.5));
            }
            return page;
        });

        Result r = canary(3, script).run(TARGET, "daily");

        double expected = (4 + 119d / 120) / 5;
        assertThat(r.meanRecall()).isCloseTo(expected, org.assertj.core.data.Offset.offset(1e-12));
        assertThat(r.meanRecall()).as("between the bar and 1").isGreaterThan(0.99).isLessThan(1.0);
        assertThat(r.top1Misses()).isZero();
        assertThat(r.state()).as("a mean of %s is at or above the 0.99 bar", r.meanRecall()).isEqualTo(State.GREEN);
    }

    @Test
    void aTop1Miss_isRedEvenWhenTheMeanIsAbove099() {
        // Every page has the exact top-k except the nearest row, replaced by a row at the k-th distance (a tie, so
        // recall stays 1.0), but the NEAREST row is absent and nothing is as near.
        Script script = new Script((q, k) -> {
            List<Hit> page = new ArrayList<>(exactPage(k).subList(1, k));
            page.add(new Hit("tied-" + k, 0.001 * k));
            return page;
        });

        Result r = canary(3, script).run(TARGET, "vacuum");

        assertThat(r.top1Misses()).as("each of the three queries").isEqualTo(3);
        assertThat(r.state()).isEqualTo(State.RED);
        assertThat(r.meanRecall()).as("the tie fills the page: the mean alone would not turn this red")
            .isEqualTo(1.0);
        assertThat(r.redSample()).hasSize(3);
    }

    @Test
    void aMeanBelowTheBar_isRed_withoutAnyTop1Miss() {
        // The nearest row is there; a tenth of every page is replaced by far rows.
        Script script = new Script((q, k) -> {
            List<Hit> page = new ArrayList<>(exactPage(k));
            for (int i = k - k / 10; i < k; i++) {
                page.set(i, new Hit("far-" + i, 5.0 + i));
            }
            return page;
        });

        Result r = canary(4, script).run(TARGET, "after_build");

        assertThat(r.top1Misses()).isZero();
        assertThat(r.meanRecall()).isCloseTo(0.9, org.assertj.core.data.Offset.offset(1e-9));
        assertThat(r.state()).isEqualTo(State.RED);
        assertThat(r.minRecall()).isCloseTo(0.9, org.assertj.core.data.Offset.offset(1e-9));
    }

    @Test
    void aShortPage_countsAgainstRecall() {
        Script script = new Script((q, k) -> exactPage(k / 2));

        Result r = canary(3, script).run(TARGET, "daily");

        assertThat(r.meanRecall()).isCloseTo(0.5, org.assertj.core.data.Offset.offset(1e-9));
        assertThat(r.state()).isEqualTo(State.RED);
        assertThat(r.top1Misses()).isZero();
    }

    @Test
    void aCollectionSmallerThanK_isScoredAgainstTheRowsItHas() {
        Script script = new Script((q, k) -> exactPage(Math.min(k, 50)));
        script.exactRows = 50;

        Result r = canary(3, script).run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.GREEN);
        assertThat(r.meanRecall()).isEqualTo(1.0);
    }

    // -- the unmeasured reasons ----------------------------------------------------------------------------

    @Test
    void twoQueries_areUnmeasuredNoQueries_andTheResultCarriesTwo() {
        Script script = new Script(PciCanaryTest::perfect);

        Result r = canary(2, script).run(TARGET, "after_build");

        assertThat(r.state()).isEqualTo(State.UNMEASURED);
        assertThat(r.reason()).isEqualTo("no_queries");
        assertThat(r.queries()).isEqualTo(2);
        assertThat(r.minRecall()).isNull();
        assertThat(r.meanRecall()).isNull();
        assertThat(script.hnswCalls.get()).as("no statement ran").isZero();
        assertThat(PciCanary.line(r)).contains("queries=2").contains("state=unmeasured").contains("reason=no_queries");
    }

    @Test
    void identicalVectors_countOnce_soThreeEntriesOfTwoDistinctQueriesAreNoQueries() {
        List<PciQueryRingReader.RingEntry> entries = new ArrayList<>();
        entries.add(new PciQueryRingReader.RingEntry(MODEL, vec(1), OffsetDateTime.now()));
        entries.add(new PciQueryRingReader.RingEntry(MODEL, vec(1), OffsetDateTime.now()));
        entries.add(new PciQueryRingReader.RingEntry(MODEL, vec(2), OffsetDateTime.now()));
        entries.add(new PciQueryRingReader.RingEntry("another-model", vec(3), OffsetDateTime.now()));
        entries.add(new PciQueryRingReader.RingEntry(MODEL, null, OffsetDateTime.now()));

        Result r = canary((t, c) -> entries, new Script(PciCanaryTest::perfect)).run(TARGET, "daily");

        assertThat(r.reason()).isEqualTo("no_queries");
        assertThat(r.queries()).isEqualTo(2);
    }

    @Test
    void thePlanThatDoesNotNameTheIndex_isUnmeasuredIndexNotUsed_andWarnsWithTheTopNode() {
        Script script = new Script(PciCanaryTest::perfect);
        script.plan = PLAN_SEQ;

        Result r = canary(5, script).run(TARGET, "after_build");

        assertThat(r.state()).isEqualTo(State.UNMEASURED);
        assertThat(r.reason()).isEqualTo("index_not_used");
        assertThat(r.queries()).isEqualTo(5);
        assertThat(script.hnswCalls.get()).as("stopped at the first statement").isEqualTo(1);
        assertThat(script.exactCalls.get()).isZero();
        assertThat(logs.list).anySatisfy(e -> {
            assertThat(e.getLevel().toString()).isEqualTo("WARN");
            assertThat(e.getFormattedMessage()).contains("event=pci_canary_index_not_used")
                .contains("index=pci_abc").contains("top=\"Limit\"").contains("Seq Scan on chunks_x");
        });
    }

    @Test
    void aStatementTheRouterSentExact_isIndexNotUsed() {
        Script script = new Script(PciCanaryTest::perfect);
        script.routedExact = true;

        Result r = canary(5, script).run(TARGET, "daily");

        assertThat(r.reason()).isEqualTo("index_not_used");
        assertThat(r.detail()).isEqualTo("routed_exact");
    }

    @Test
    void aMissingPlan_isAnError_notAGreen() {
        Script script = new Script(PciCanaryTest::perfect);
        script.plan = null;

        Result r = canary(5, script).run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.UNMEASURED);
        assertThat(r.reason()).isEqualTo("error");
        assertThat(r.queries()).isEqualTo(5);
    }

    @Test
    void aFailingStatement_isUnmeasuredError_andNeverThrows() {
        Script script = new Script(PciCanaryTest::perfect);
        script.failAt = new IllegalStateException("no search permit within 10000 ms");

        Result r = canary(5, script).run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.UNMEASURED);
        assertThat(r.reason()).isEqualTo("error");
        assertThat(r.queries()).isEqualTo(5);
        assertThat(r.top1Misses()).isZero();
    }

    /** A driver error can quote the value that failed, and in the canary's statements that value is a query vector. */
    @Test
    void anErrorLine_neverCarriesAVectorTheDriverQuoted() {
        String quoted = "[0.1234567,0.2345678,0.3456789,0.4567891,0.5678912]";
        Script script = new Script(PciCanaryTest::perfect);
        script.failAt = new IllegalStateException("invalid input syntax for type vector: " + quoted);

        canary(5, script).run(TARGET, "daily");
        canary((t, c) -> {
            throw new IllegalStateException("could not read ring near " + quoted);
        }, new Script(PciCanaryTest::perfect)).run(TARGET, "daily");

        var failed = logs.list.stream().map(ILoggingEvent::getFormattedMessage)
            .filter(m -> m.contains("event=pci_canary_failed")).toList();
        assertThat(failed).as("both failures were logged").hasSize(2);
        assertThat(failed).allSatisfy(m -> assertThat(m).doesNotContain("0.1234567").doesNotContain("0.5678912")
            .contains("[vector]"));
    }

    @Test
    void aFailingRingRead_isUnmeasuredError_withZeroQueries() {
        Result r = canary((t, c) -> {
            throw new IllegalStateException("db down");
        }, new Script(PciCanaryTest::perfect)).run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.UNMEASURED);
        assertThat(r.reason()).isEqualTo("error");
        assertThat(r.queries()).isZero();
    }

    @Test
    void anEmptyExactPage_isAnError_notAGreen() {
        Script script = new Script((q, k) -> List.of());
        script.exactRows = 0;

        Result r = canary(4, script).run(TARGET, "daily");

        assertThat(r.reason()).isEqualTo("error");
        assertThat(r.detail()).isEqualTo("exact_empty");
    }

    @Test
    void aRunThatPassesItsLimit_endsAsAnError() {
        AtomicLong clock = new AtomicLong();
        Script script = new Script((q, k) -> {
            clock.addAndGet(PciCanaryPolicy.RUN_LIMIT.toNanos() + 1);
            return exactPage(k);
        });

        Result r = new PciCanary((t, c) -> ring(5), script, java.time.Clock.systemUTC(), clock::get).run(TARGET, "daily");

        assertThat(r.reason()).isEqualTo("error");
        assertThat(r.detail()).isEqualTo("run_limit");
        assertThat(r.queries()).isEqualTo(5);
    }

    // -- the log line ---------------------------------------------------------------------------------------

    @Test
    void theLogLine_hasEveryField() {
        Result green = canary(4, new Script(PciCanaryTest::perfect)).run(TARGET, "vacuum");

        assertThat(PciCanary.line(green)).isEqualTo("event=pci_canary index=pci_abc collection="
            + TARGET.collection() + " tenant=t1 trigger=vacuum queries=4 min_recall=1.0000 mean_recall=1.0000 "
            + "top1_misses=0 state=green");
    }

    @Test
    void policyConstants_areTheDecidedOnes() {
        assertThat(PciCanaryPolicy.KS).containsExactly(40, 60, 80, 100, 120);
        assertThat(PciCanaryPolicy.EXACT_K).isEqualTo(120);
        assertThat(PciCanaryPolicy.MAX_QUERIES).isEqualTo(10);
        assertThat(PciCanaryPolicy.MIN_QUERIES).as("Sam, decision 2 and gate finding S4").isEqualTo(3);
        assertThat(PciCanaryPolicy.RED_MEAN_BELOW).isEqualTo(0.99);
        assertThat(PciCanaryPolicy.VACUUM_MIN_INTERVAL).hasHours(6);
        assertThat(PciCanaryPolicy.STALE_AFTER).hasHours(24);
        assertThat(PciCanaryPolicy.PASS_BUDGET).hasSeconds(60);
    }
}
