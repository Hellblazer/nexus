// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.vectors.PciCanary.Hit;
import dev.nexus.service.vectors.PciCanary.Leg;
import dev.nexus.service.vectors.PciCanary.Result;
import dev.nexus.service.vectors.PciCanary.State;
import dev.nexus.service.vectors.PciCanary.Target;
import dev.nexus.service.vectors.PciCanaryTest.Script;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.OptionalDouble;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Function;
import java.util.function.IntUnaryOperator;

import static dev.nexus.service.vectors.PciCanaryTest.PLAN_SEQ;
import static dev.nexus.service.vectors.PciCanaryTest.PLAN_USING;
import static dev.nexus.service.vectors.PciCanaryTest.TARGET;
import static dev.nexus.service.vectors.PciCanaryTest.exactPage;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-enew7 step 1: for an index recorded {@code red_unfixed}, a red canary run also records, per sample query, the
 * verdict's own recall and the recall at the widest ef, so a production read can tell an ef-limited index from a
 * graph-limited one. Diagnostic output only: the verdict, the statements of the run and the serving path are what they
 * were. Scripted statements, no database; the substrate half (the observed ef and the plan of the real statement) is
 * {@code PciPlanAndRecallIntegrationTest}.
 */
class PciCanaryEfDiagnosisTest {

    static final String QID0 = PciQueryRing.queryIds(List.of(PciCanaryTest.vec(0))).get(0);
    static final PciCatalog.Anchor PLAIN = new PciCatalog.Anchor(Instant.parse("2026-11-20T00:00:00Z"), 100, 1000,
        PciCatalog.ORIGIN_BUILT);
    /** The anchor of an index recorded red_unfixed on query 0 only. */
    static final PciCatalog.Anchor UNFIXED = PLAIN.withRedUnfixed(
        new PciCatalog.RedUnfixed(Instant.parse("2026-11-21T00:00:00Z"), 7, 100, List.of(QID0)));

    ListAppender<ILoggingEvent> logs;
    final List<Logger> loggers = new ArrayList<>();

    @BeforeEach
    void capture() {
        logs = new ListAppender<>();
        logs.start();
        for (Class<?> c : List.of(PciCanary.class, PciCanaryEfDiagnosis.class)) {
            Logger l = (Logger) LoggerFactory.getLogger(c);
            l.addAppender(logs);
            loggers.add(l);
        }
    }

    @AfterEach
    void release() {
        loggers.forEach(l -> l.detachAppender(logs));
        logs.stop();
    }

    List<String> lines(String event) {
        return logs.list.stream().map(ILoggingEvent::getFormattedMessage)
            .filter(m -> m.startsWith("event=" + event + " ")).toList();
    }

    /** Past the k-th exact distance: a miss at k. */
    static Hit far(int i) {
        return new Hit("far-" + i, 10 + i);
    }

    /** A page of {@code k} rows that starts with {@code first} and is padded with the exact order. */
    static List<Hit> page(List<Hit> first, int k) {
        List<Hit> out = new ArrayList<>(first);
        List<Hit> exact = exactPage(k);
        for (int i = first.size(); i < k; i++) {
            out.add(exact.get(i));
        }
        return out;
    }

    /** The verdict's page (k up to 120) has one miss per page: recall (k-1)/k, a mean of 0.9855, red. */
    static List<Hit> servingPage(int k) {
        List<Hit> p = new ArrayList<>(exactPage(k));
        p.set(k - 1, far(k));
        return p;
    }

    /**
     * The wide page (1000 rows). Query 0: the rows of exact ranks 118 to 120 missed. Query 1: perfect. Query 2: the nearest
     * row absent (a row at the 120th distance in its place).
     */
    static List<Hit> widePage(int q) {
        List<Hit> base = new ArrayList<>(exactPage(PciCanaryPolicy.EXACT_K));
        if (q == 0) {
            for (int i = 117; i < 120; i++) {
                base.set(i, far(i));
            }
        } else if (q == 2) {
            base.remove(0);
            base.add(new Hit("tied", 0.001 * 120));
        }
        return page(base, PciCanaryEfDiagnosis.WIDE_PAGE);
    }

    static List<Hit> redThenWide(int q, int k) {
        return k <= PciCanaryPolicy.EXACT_K ? servingPage(k) : widePage(q);
    }

    /** The scripted statements as the serving path reports them: every leg carries the ef its page size sets. */
    static final class Observing implements PciCanary.Statements {
        final Script script;
        IntUnaryOperator efOf = PgSession::efSearchFor;
        boolean walkedWide;
        boolean wideRoutedExact;
        String widePlan = PLAN_USING;
        Runnable onWide = () -> { };

        Observing(Script script) {
            this.script = script;
        }

        @Override
        public Leg hnsw(Target target, float[] query, int k, boolean explain) {
            Leg leg = script.hnsw(target, query, k, explain);
            boolean wide = k == PciCanaryEfDiagnosis.WIDE_PAGE;
            if (wide) {
                onWide.run();
            }
            return new Leg(leg.hits(), wide ? (explain ? widePlan : null) : leg.plan(),
                leg.routedExact() || wide && wideRoutedExact, efOf.applyAsInt(k), walkedWide);
        }

        @Override
        public List<Hit> exact(Target target, float[] query, int k) {
            return script.exact(target, query, k);
        }
    }

    static Observing redScript() {
        return new Observing(new Script(PciCanaryEfDiagnosisTest::redThenWide));
    }

    PciCanary canary(PciCanary.Statements statements, Function<Target, PciCatalog.Anchor> anchors) {
        PciCanary c = new PciCanary((t, k) -> PciCanaryTest.ring(3), statements);
        c.diagnoseRedWith(anchors);
        return c;
    }

    @Test
    void aRedRunOnARedUnfixedIndex_recordsTheVerdictsRecallAndTheWideRecallPerQuery() {
        Observing statements = redScript();
        PciCanary canary = canary(statements, t -> UNFIXED);

        Result r = canary.run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.RED);
        List<String> rows = lines("pci_canary_ef_diag");
        assertThat(rows).hasSize(3);
        assertThat(rows.get(0)).isEqualTo("event=pci_canary_ef_diag index=pci_abc collection=" + TARGET.collection()
            + " tenant=t1 pos=0 of=3 qid=" + QID0 + " red=yes in_red_unfixed=yes ef_verdict=600 verdict_mean=0.9855"
            + " verdict_r120=0.9917 top1_verdict=hit ef_wide=1000 wide_mean=0.9950 wide_r120=0.9750 top1_wide=hit"
            + " rows_wide=1000 misses_wide=3 miss_ranks_wide=118,119,120");
        assertThat(rows.get(1)).contains("pos=1 of=3").contains("red=yes in_red_unfixed=no")
            .contains("wide_mean=1.0000 wide_r120=1.0000 top1_wide=hit rows_wide=1000 misses_wide=0 miss_ranks_wide=none");
        assertThat(rows.get(2)).contains("pos=2 of=3").contains("top1_verdict=hit").contains("top1_wide=miss");
        List<String> summary = lines("pci_canary_ef_diag_summary");
        assertThat(summary).hasSize(1);
        assertThat(summary.get(0)).isEqualTo("event=pci_canary_ef_diag_summary index=pci_abc collection="
            + TARGET.collection() + " tenant=t1 queries=3 ef_verdict=600 ef_wide=1000 verdict_mean=0.9855"
            + " verdict_min=0.9750 wide_mean=0.9941 wide_min=0.9750 top1_misses_verdict=0 top1_misses_wide=1"
            + " red_queries=3 in_red_unfixed=1");
        // the verdict's own figures: the summary's verdict side is what the result reports
        assertThat(r.meanRecall()).isCloseTo(0.9855, org.assertj.core.data.Offset.offset(1e-4));
        assertThat(r.minRecall()).isCloseTo(0.9750, org.assertj.core.data.Offset.offset(1e-4));
        // five verdict k per query, then one wide statement per query; nothing re-run, no further exact statement
        assertThat(statements.script.hnswCalls.get()).isEqualTo(3 * 5 + 3);
        assertThat(statements.script.exactCalls.get()).isEqualTo(3);
        assertThat(statements.script.explained.stream().filter(e -> e).count())
            .as("the verdict's first statement is explained, and the wide side's first is explained too").isEqualTo(2);
    }

    @Test
    void theVerdictOfARunIsTheSameWithAndWithoutTheDiagnosis() {
        Result plain = new PciCanary((t, c) -> PciCanaryTest.ring(3), redScript()).run(TARGET, "daily");

        Result r = canary(redScript(), t -> UNFIXED).run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(plain.state());
        assertThat(r.minRecall()).isEqualTo(plain.minRecall());
        assertThat(r.meanRecall()).isEqualTo(plain.meanRecall());
        assertThat(r.top1Misses()).isEqualTo(plain.top1Misses());
        assertThat(r.queries()).isEqualTo(plain.queries());
        assertThat(r.redSample()).hasSameSizeAs(plain.redSample());
        assertThat(r.redQueries()).hasSameSizeAs(plain.redQueries());
        assertThat(PciCanary.line(r)).isEqualTo(PciCanary.line(plain));
    }

    @Test
    void moreThanTenMissedRanks_areCappedAndMarked() {
        Observing statements = new Observing(new Script((q, k) -> {
            if (k <= PciCanaryPolicy.EXACT_K) {
                return servingPage(k);
            }
            List<Hit> base = new ArrayList<>(exactPage(PciCanaryPolicy.EXACT_K));
            for (int i = 99; i < 120; i++) {
                base.set(i, far(i));
            }
            return page(base, PciCanaryEfDiagnosis.WIDE_PAGE);
        }));

        canary(statements, t -> UNFIXED).run(TARGET, "daily");

        assertThat(lines("pci_canary_ef_diag").get(0))
            .contains("misses_wide=21 miss_ranks_wide=100,101,102,103,104,105,106,107,108,109,...");
    }

    @Test
    void aRedRunOfAnIndexNotRecordedRedUnfixed_isNotDiagnosed_andSaysSo() {
        Observing statements = redScript();

        Result r = canary(statements, t -> PLAIN).run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.RED);
        assertThat(lines("pci_canary_ef_diag")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_skipped")).containsExactly("event=pci_canary_ef_diag_skipped index=pci_abc"
            + " collection=" + TARGET.collection() + " tenant=t1 reason=not_red_unfixed");
        assertThat(statements.script.hnswCalls.get()).isEqualTo(3 * 5);
    }

    @Test
    void aRedRunOfAnIndexTheEngineHasNotRead_isNotDiagnosed_andSaysSo() {
        Result r = canary(redScript(), t -> null).run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.RED);
        assertThat(lines("pci_canary_ef_diag")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_skipped")).hasSize(1).allMatch(l -> l.endsWith("reason=no_reading"));
    }

    @Test
    void theRedReMeasureAfterAReindex_isNotDiagnosed_whileTheAnchorDoesNotYetCarryRedUnfixed() {
        // The red-canary REINDEX's own re-measure runs before the second COMMENT that writes red_unfixed: its anchor is the fresh one.
        Observing statements = redScript();
        PciCanary canary = canary(statements, t -> PLAIN);

        Result r = canary.remeasure(TARGET, "after_reindex", PciCanaryTest.ring(3).stream()
            .map(PciQueryRingReader.RingEntry::vector).toList());

        assertThat(r.state()).isEqualTo(State.RED);
        assertThat(lines("pci_canary_ef_diag")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_summary")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_skipped")).hasSize(1).allMatch(l -> l.endsWith("reason=not_red_unfixed"));
        assertThat(statements.script.hnswCalls.get()).isEqualTo(3 * 5);
    }

    @Test
    void anUnconfiguredCanary_orAGreenRun_logsNothingOfTheDiagnosis() {
        Observing red = redScript();
        new PciCanary((t, c) -> PciCanaryTest.ring(3), red).run(TARGET, "daily");
        Observing green = new Observing(new Script(PciCanaryTest::perfect));
        Result r = canary(green, t -> UNFIXED).run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.GREEN);
        assertThat(lines("pci_canary_ef_diag")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_skipped")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_failed")).isEmpty();
        assertThat(red.script.hnswCalls.get()).isEqualTo(3 * 5);
        assertThat(green.script.hnswCalls.get()).isEqualTo(3 * 5);
    }

    // -- the record proves what it measured ----------------------------------------------------------------

    @Test
    void aVerdictLegThatWalkedWide_isAnEfMismatch_andClaimsNoRecall() {
        // The router's index set holds no valid index: the statement walks at 1000 whatever its page size.
        Observing statements = redScript();
        statements.efOf = k -> PgSession.EF_SEARCH_WIDEST;
        statements.walkedWide = true;

        Result r = canary(statements, t -> UNFIXED).run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.RED);
        assertThat(lines("pci_canary_ef_diag")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_summary")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_failed")).containsExactly("event=pci_canary_ef_diag_failed index=pci_abc"
            + " collection=" + TARGET.collection() + " stage=ef_mismatch side=verdict pos=0 expected_ef=600"
            + " observed_ef=1000 walked_wide=true");
    }

    @Test
    void aWideLegThatDidNotRunAtTheWidestEf_isAnEfMismatch_andClaimsNoRecall() {
        Observing statements = redScript();
        statements.efOf = k -> 600;

        canary(statements, t -> UNFIXED).run(TARGET, "daily");

        assertThat(lines("pci_canary_ef_diag")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_failed")).hasSize(1).allMatch(l -> l.contains("stage=ef_mismatch side=wide")
            && l.contains("expected_ef=1000 observed_ef=600 walked_wide=false"));
    }

    @Test
    void aStatementSourceThatDoesNotReportItsEf_isAnEfMismatch() {
        Script plain = new Script(PciCanaryEfDiagnosisTest::redThenWide);

        canary(plain, t -> UNFIXED).run(TARGET, "daily");

        assertThat(lines("pci_canary_ef_diag")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_failed")).hasSize(1).allMatch(l -> l.contains("stage=ef_mismatch side=verdict")
            && l.contains("observed_ef=-1"));
    }

    @Test
    void aWidePlanThatDoesNotNameTheIndex_isAPlanMismatch_andClaimsNoRecall() {
        Observing statements = redScript();
        statements.widePlan = PLAN_SEQ;

        Result r = canary(statements, t -> UNFIXED).run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.RED);
        assertThat(lines("pci_canary_ef_diag")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_failed")).hasSize(1)
            .allMatch(l -> l.contains("stage=plan_mismatch side=wide pos=0"));
    }

    @Test
    void aWideStatementWithNoPlan_isPlanUnavailable() {
        Observing statements = redScript();
        statements.widePlan = null;

        canary(statements, t -> UNFIXED).run(TARGET, "daily");

        assertThat(lines("pci_canary_ef_diag")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_failed")).hasSize(1).allMatch(l -> l.contains("stage=plan_unavailable"));
    }

    @Test
    void aWideStatementTheRouterSentExact_isNotRecorded() {
        Observing statements = redScript();
        statements.wideRoutedExact = true;

        canary(statements, t -> UNFIXED).run(TARGET, "daily");

        assertThat(lines("pci_canary_ef_diag")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_failed")).hasSize(1).allMatch(l -> l.contains("stage=routed_exact"));
    }

    @Test
    void aDiagnosisThatPassesTheRunsOwnLimit_stopsWithNoRecord() {
        AtomicLong now = new AtomicLong();
        Observing statements = redScript();
        statements.onWide = () -> now.addAndGet(PciCanaryPolicy.RUN_LIMIT.toNanos() + 1);
        PciCanary canary = new PciCanary((t, c) -> PciCanaryTest.ring(3), statements, Clock.systemUTC(), now::get);
        canary.diagnoseRedWith(t -> UNFIXED);

        Result r = canary.run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.RED);
        assertThat(lines("pci_canary_ef_diag")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_failed")).containsExactly("event=pci_canary_ef_diag_failed index=pci_abc"
            + " collection=" + TARGET.collection() + " stage=run_limit queries_done=1 of=3");
    }

    @Test
    void aFailingWideStatement_isLogged_andLeavesTheRedVerdictAlone() {
        Observing statements = new Observing(new Script((q, k) -> {
            if (k > PciCanaryPolicy.EXACT_K) {
                throw new IllegalStateException("no search permit within 10000 ms");
            }
            return servingPage(k);
        }));

        Result r = canary(statements, t -> UNFIXED).run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.RED);
        assertThat(lines("pci_canary_ef_diag")).isEmpty();
        assertThat(lines("pci_canary_ef_diag_failed")).hasSize(1).allMatch(l -> l.contains("stage=statement"));
    }

    @Test
    void aThrowingGate_isLogged_andLeavesTheRedVerdictAlone() {
        Result r = canary(redScript(), t -> {
            throw new IllegalStateException("ledger");
        }).run(TARGET, "daily");

        assertThat(r.state()).isEqualTo(State.RED);
        assertThat(lines("pci_canary_ef_diag_failed")).hasSize(1).allMatch(l -> l.contains("stage=gate"));
    }

    @Test
    void theGate_readsTheLedgersAnchorOfTheTargetsIndex() {
        var ledger = new PciAnchorLedger(Clock.systemUTC(), rows -> OptionalDouble.empty(), name -> false);
        var anchors = PciCanaryEfDiagnosis.anchors(ledger);
        assertThat(anchors.apply(TARGET)).as("no reading yet").isNull();

        ledger.put(TARGET.leaf(), TARGET.index(), new PciReconciler.IndexReading(PLAIN, 1000, 100, 0, Instant.EPOCH));
        assertThat(anchors.apply(TARGET).redUnfixed()).as("an anchor without the field").isNull();

        ledger.put(TARGET.leaf(), TARGET.index(), new PciReconciler.IndexReading(UNFIXED, 1000, 100, 0, Instant.EPOCH));
        assertThat(anchors.apply(TARGET).redUnfixed().queries()).containsExactly(QID0);
    }

    @Test
    void theWideStatement_isAPageOfTheWidestEf() {
        assertThat(PciCanaryEfDiagnosis.WIDE_PAGE).isEqualTo(PgSession.EF_SEARCH_WIDEST).isEqualTo(1000);
    }
}
