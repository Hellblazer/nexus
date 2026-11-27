// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession;
import dev.nexus.service.vectors.PciCanary.Hit;
import dev.nexus.service.vectors.PciCanary.Leg;
import dev.nexus.service.vectors.PciCanary.PageScore;
import dev.nexus.service.vectors.PciCanary.Scored;
import dev.nexus.service.vectors.PciCanary.Statements;
import dev.nexus.service.vectors.PciCanary.Target;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.function.Function;
import java.util.function.LongSupplier;

/**
 * What a red canary run says about an index the REINDEX left red (nexus-enew7 step 1): the same sample queries searched
 * at the widest ef, set beside the verdict's own readings, so a production read can tell an ef-limited index from a
 * graph-limited one. Diagnostic output only: it runs after the verdict, reads the verdict's own pages, exact pages and
 * recalls, changes nothing about the run's result, and never throws.
 *
 * <p><b>When.</b> On the builder-lock holder, in a canary run that reads RED, when the engine's reading of the index's
 * anchor carries {@code red_unfixed}: so on the next red run of an index already recorded {@code red_unfixed} (a boot, a
 * daily or a vacuum run), and not at the red-canary REINDEX's own re-measure, whose anchor does not yet carry it (the field
 * is written after that re-measure). A green run clears {@code red_unfixed} and ends the records. A red run that is not
 * diagnosed logs one {@code event=pci_canary_ef_diag_skipped} saying why ({@code no_reading}: the engine has not read the
 * index's anchor since it booted; {@code not_red_unfixed}).
 *
 * <p><b>The two sides.</b> The <i>verdict</i> side is the run's own HNSW pages (k = 40..120, as the serving statement
 * ran them), at the serving floor ef ({@code max(floor, k)}, so the env-resolved floor, 600 by default); nothing is
 * re-run, and its figures are the ones the red verdict was made of. The <i>wide</i> side is one statement per query at
 * page size {@link #WIDE_PAGE} (1000), which the serving statement walks at {@code hnsw.ef_search} 1000; its page is
 * scored at the same five k, by the same rule ({@link PciCanary#scorePage}), against the same exact pages. Both sides are
 * means over the verdict's own cells.
 *
 * <p><b>The record proves what it measured.</b> Every statement reports the ef its settings batch set
 * ({@link Leg#efSearch()}); a reading whose observed ef is not the intended one (the router's index set held no valid
 * index for the collection, so the statement walked at 1000 whatever its page size; or the source does not report it)
 * is not logged: the diagnosis ends with {@code event=pci_canary_ef_diag_failed stage=ef_mismatch} and both values. The
 * first wide statement is explained like the verdict's first one; a plan that does not name the index ends it with
 * {@code stage=plan_mismatch}. Nothing is logged for a run unless every reading is sound.
 *
 * <p><b>The records.</b> One line per query, then one summary line. {@code qid} is the query's stable id
 * ({@link PciQueryRing#queryIds}), {@code red} whether the query reads red by itself, {@code in_red_unfixed} whether it is
 * one of the queries the anchor's {@code red_unfixed} names; {@code rows_wide} the rows the wide page returned and
 * {@code miss_ranks_wide} the 1-based exact ranks, up to ten, absent from the wide page's first 120 rows:
 * <pre>
 * event=pci_canary_ef_diag index=pci_x collection=c tenant=t pos=3 of=10 qid=0123456789abcdef red=yes in_red_unfixed=yes ef_verdict=600 verdict_mean=0.9800 verdict_r120=0.9750 top1_verdict=hit ef_wide=1000 wide_mean=0.9810 wide_r120=0.9833 top1_wide=hit rows_wide=1000 misses_wide=2 miss_ranks_wide=17,88
 * event=pci_canary_ef_diag_summary index=pci_x collection=c tenant=t queries=10 ef_verdict=600 ef_wide=1000 verdict_mean=0.9700 verdict_min=0.8500 wide_mean=0.9712 wide_min=0.8667 top1_misses_verdict=0 top1_misses_wide=0 red_queries=4 in_red_unfixed=4
 * </pre>
 * {@code top1} is {@code hit} when, at every k, the exact nearest row (or one as near) is on the page.
 */
final class PciCanaryEfDiagnosis {

    private static final Logger log = LoggerFactory.getLogger(PciCanaryEfDiagnosis.class);

    /** The page size of the wide statement: the serving statement walks it at {@code PgSession.EF_SEARCH_WIDEST}. */
    static final int WIDE_PAGE = PgSession.EF_SEARCH_WIDEST;

    /** The most missed ranks one query line names. */
    static final int MAX_MISS_RANKS = 10;

    private PciCanaryEfDiagnosis() { }

    /**
     * What the run held when it ended red, query by query and aligned: the sample, the HNSW legs it ran (one per k in
     * {@link PciCanaryPolicy#KS}), its scoring, and its exact pages.
     */
    record Evidence(List<float[]> queries, List<List<Leg>> legs, List<Scored> scored, List<List<Hit>> exacts) { }

    /** The anchor the engine last read of the target's index, or null: the gate of {@link PciCanary#diagnoseRedWith}. */
    static Function<Target, PciCatalog.Anchor> anchors(PciAnchorLedger ledger) {
        return target -> {
            PciReconciler.IndexReading reading = ledger.reading(target.leaf(), target.index());
            return reading == null ? null : reading.anchor();
        };
    }

    private record Row(int pos, String qid, boolean red, boolean listed, int efVerdict, double[] verdict,
                       boolean verdictTop1Miss, int efWide, double[] wide, boolean wideTop1Miss, int rowsWide,
                       List<Integer> missRanks) { }

    /**
     * Search each measurable query of {@code ev} at the widest ef and log the readings beside the verdict's.
     * {@code startedNanos} is the run's own start, so the diagnosis spends what is left of {@link PciCanaryPolicy#RUN_LIMIT}.
     * Never throws: any unsound reading ends it with {@code event=pci_canary_ef_diag_failed} and no record.
     */
    static void run(Statements statements, Target target, Evidence ev, List<String> redUnfixedQueries,
                    long startedNanos, LongSupplier nanos) {
        int n = ev.queries().size();
        List<Row> rows = new ArrayList<>();
        boolean planned = false;
        try {
            for (int qi = 0; qi < n; qi++) {
                Scored scored = ev.scored().get(qi);
                if (!scored.measurable()) {
                    continue;   // the exact statement returned nothing: the verdict skipped this query too
                }
                if (nanos.getAsLong() - startedNanos > PciCanaryPolicy.RUN_LIMIT.toNanos()) {
                    fail(target, "run_limit", "queries_done=" + qi + " of=" + n);
                    return;
                }
                int efVerdict = -1;
                List<Leg> legs = ev.legs().get(qi);
                for (int ki = 0; ki < legs.size(); ki++) {
                    int expected = PgSession.efSearchFor(PciCanaryPolicy.KS.get(ki));
                    Leg leg = legs.get(ki);
                    if (leg.efSearch() != expected) {
                        mismatch(target, "verdict", qi, expected, leg);
                        return;
                    }
                    efVerdict = leg.efSearch();
                }
                Leg wide = statements.hnsw(target, ev.queries().get(qi), WIDE_PAGE, !planned);
                if (wide.routedExact()) {
                    fail(target, "routed_exact", "side=wide pos=" + qi);
                    return;
                }
                if (!planned) {
                    if (wide.plan() == null) {
                        fail(target, "plan_unavailable", "side=wide pos=" + qi);
                        return;
                    }
                    if (!wide.plan().contains(target.index())) {
                        fail(target, "plan_mismatch", "side=wide pos=" + qi + " "
                            + PciPlanCheck.describePlan(wide.plan()));
                        return;
                    }
                    planned = true;
                }
                if (wide.efSearch() != PgSession.EF_SEARCH_WIDEST) {
                    mismatch(target, "wide", qi, PgSession.EF_SEARCH_WIDEST, wide);
                    return;
                }
                List<Hit> exact = ev.exacts().get(qi);
                List<Hit> page = wide.hits();
                double[] wideRecalls = new double[PciCanaryPolicy.KS.size()];
                boolean wideTop1Miss = false;
                for (int ki = 0; ki < wideRecalls.length; ki++) {
                    int k = PciCanaryPolicy.KS.get(ki);
                    PageScore s = PciCanary.scorePage(exact, page.subList(0, Math.min(k, page.size())), k);
                    wideRecalls[ki] = s.recall();
                    wideTop1Miss |= !s.sawNearest();
                }
                String qid = PciQueryRing.queryIds(List.of(ev.queries().get(qi))).get(0);
                rows.add(new Row(qi, qid, scored.readsRed(), redUnfixedQueries.contains(qid), efVerdict,
                    scored.recalls(), scored.top1Miss(), wide.efSearch(), wideRecalls, wideTop1Miss, page.size(),
                    missRanks(exact, page)));
            }
        } catch (RuntimeException e) {
            fail(target, "statement", "error=" + PciQueryRing.sanitize(e));
            return;
        }
        if (!rows.isEmpty()) {
            emit(target, n, rows);
        }
    }

    /** The 1-based exact ranks, within the first {@link PciCanaryPolicy#EXACT_K}, whose row is absent from the page's first as many rows. */
    private static List<Integer> missRanks(List<Hit> exact, List<Hit> page) {
        Set<String> ids = new HashSet<>();
        for (Hit h : page.subList(0, Math.min(PciCanaryPolicy.EXACT_K, page.size()))) {
            ids.add(h.id());
        }
        List<Integer> out = new ArrayList<>();
        for (int i = 0; i < Math.min(PciCanaryPolicy.EXACT_K, exact.size()); i++) {
            if (!ids.contains(exact.get(i).id())) {
                out.add(i + 1);
            }
        }
        return out;
    }

    private static void emit(Target target, int n, List<Row> rows) {
        double sumV = 0;
        double sumW = 0;
        double minV = 1.0;
        double minW = 1.0;
        int missV = 0;
        int missW = 0;
        int red = 0;
        int listed = 0;
        for (Row r : rows) {
            log.info("event=pci_canary_ef_diag index={} collection={} tenant={} pos={} of={} qid={} red={} "
                + "in_red_unfixed={} ef_verdict={} verdict_mean={} verdict_r120={} top1_verdict={} ef_wide={} "
                + "wide_mean={} wide_r120={} top1_wide={} rows_wide={} misses_wide={} miss_ranks_wide={}",
                target.index(), target.collection(), target.tenant(), r.pos(), n, r.qid(), yes(r.red()),
                yes(r.listed()), r.efVerdict(), format(mean(r.verdict())), format(last(r.verdict())),
                r.verdictTop1Miss() ? "miss" : "hit", r.efWide(), format(mean(r.wide())), format(last(r.wide())),
                r.wideTop1Miss() ? "miss" : "hit", r.rowsWide(), r.missRanks().size(), ranks(r.missRanks()));
            sumV += mean(r.verdict());
            sumW += mean(r.wide());
            minV = Math.min(minV, min(r.verdict()));
            minW = Math.min(minW, min(r.wide()));
            missV += r.verdictTop1Miss() ? 1 : 0;
            missW += r.wideTop1Miss() ? 1 : 0;
            red += r.red() ? 1 : 0;
            listed += r.listed() ? 1 : 0;
        }
        Row first = rows.get(0);
        log.info("event=pci_canary_ef_diag_summary index={} collection={} tenant={} queries={} ef_verdict={} ef_wide={} "
            + "verdict_mean={} verdict_min={} wide_mean={} wide_min={} top1_misses_verdict={} top1_misses_wide={} "
            + "red_queries={} in_red_unfixed={}", target.index(), target.collection(), target.tenant(), rows.size(),
            first.efVerdict(), first.efWide(), format(sumV / rows.size()), format(minV), format(sumW / rows.size()),
            format(minW), missV, missW, red, listed);
    }

    private static void mismatch(Target target, String side, int pos, int expected, Leg leg) {
        fail(target, "ef_mismatch", "side=" + side + " pos=" + pos + " expected_ef=" + expected + " observed_ef="
            + leg.efSearch() + " walked_wide=" + leg.walkedWide());
    }

    private static void fail(Target target, String stage, String detail) {
        log.warn("event=pci_canary_ef_diag_failed index={} collection={} stage={} {}", target.index(),
            target.collection(), stage, detail);
    }

    private static String yes(boolean b) {
        return b ? "yes" : "no";
    }

    private static String ranks(List<Integer> ranks) {
        if (ranks.isEmpty()) {
            return "none";
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < Math.min(MAX_MISS_RANKS, ranks.size()); i++) {
            sb.append(i == 0 ? "" : ",").append(ranks.get(i));
        }
        return ranks.size() > MAX_MISS_RANKS ? sb.append(",...").toString() : sb.toString();
    }

    private static double mean(double[] v) {
        double s = 0;
        for (double d : v) {
            s += d;
        }
        return s / v.length;
    }

    private static double min(double[] v) {
        double m = 1.0;
        for (double d : v) {
            m = Math.min(m, d);
        }
        return m;
    }

    private static double last(double[] v) {
        return v[v.length - 1];
    }

    private static String format(double v) {
        return String.format(Locale.ROOT, "%.4f", v);
    }
}
