// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Objects;
import java.util.Set;
import java.util.function.LongSupplier;

/**
 * The recall canary (RDR-230 Technical Design 5, bead nexus-4lnn1.1.4): the RDR-227 Gap 1 method on one per-collection
 * HNSW index, run through the serving statement path and never a copy of it.
 *
 * <p><b>A run.</b> It reads the collection's query ring ({@link PciQueryRingReader}, newest first, ten at most) and
 * keeps the distinct vectors of the collection's own model. For each vector it runs the serving search statement at
 * each k in {@link PciCanaryPolicy#KS}, then the same statement exact (index scans off) at
 * {@link PciCanaryPolicy#EXACT_K}, whose first k rows are the exact top-k. The statements are {@link Statements}: in
 * production {@link PgVectorRepository#canaryStatements}, which sends them through {@code runPlainSearchStatement} (the
 * settings batch, the router, the bound parameters) under the tenant's scope as {@code nexus_svc}, one search permit
 * apiece. The first HNSW statement is also explained: if the plan does not name the collection's index (or the router
 * sent the statement exact) the run measured something else, so it is discarded as {@code unmeasured}, reason
 * {@code index_not_used}, and logged at WARN with the plan's top node.
 *
 * <p><b>Scoring.</b> A row of the HNSW page is a hit when it is in the exact top-k or its distance is no greater than
 * the k-th exact distance (a tie at the boundary is not a miss). Recall at k is hits over {@code min(k, rows the exact
 * statement returned)}. A query has a <i>top-1 miss</i> when, at some k, the exact nearest row is absent from the HNSW
 * page and no HNSW row is as near. The run reports the minimum and the mean recall over queries and k and the number of
 * queries with a top-1 miss, and is {@link State#RED} when the mean is below {@link PciCanaryPolicy#RED_MEAN_BELOW} or
 * any query has one, {@link State#GREEN} otherwise.
 *
 * <p><b>Unmeasured.</b> {@code no_queries}: fewer than {@link PciCanaryPolicy#MIN_QUERIES} distinct queries.
 * {@code index_not_used}: above. {@code error}: a ring read, a statement or the permit wait failed, or the run passed
 * {@link PciCanaryPolicy#RUN_LIMIT}. An unmeasured result is never an input to anything that acts on an index; every
 * result, unmeasured included, carries {@code queries}, the count of distinct queries the ring gave this run.
 *
 * <p><b>The empty-page repair is not measured through.</b> The serving statement's nexus-bq06h belt re-runs a search
 * exact when the HNSW walk returns no row at all. The canary's HNSW statement does not take that step: a page the walk
 * left empty would otherwise read as exact and the index as perfect.
 */
final class PciCanary implements PciCanarySchedule.Runner {

    private static final Logger log = LoggerFactory.getLogger(PciCanary.class);

    /** The index a run measures. {@code index} is the name the router's set holds for it. */
    record Target(String leaf, String tenant, String model, String collection, String index) { }

    /** One row of a page: the chunk's id (its chash, hex) and its distance from the query. */
    record Hit(String id, double distance) { }

    /**
     * One HNSW statement's page.
     *
     * @param hits        the rows, nearest first
     * @param plan        the statement's plan text when it was explained, else {@code null}; {@code null} also when the
     *                    explain failed (the caller asked for one, so that is an error)
     * @param routedExact whether the router sent the statement exact: it then walked no index
     */
    record Leg(List<Hit> hits, String plan, boolean routedExact) { }

    /** The statements of a run. */
    interface Statements {
        /** The serving statement for {@code query} at page size {@code k}; explained first when {@code explain}. */
        Leg hnsw(Target target, float[] query, int k, boolean explain);

        /** The same statement with index scans off, top {@code k}. */
        List<Hit> exact(Target target, float[] query, int k);
    }

    /** The query ring, newest first. */
    @FunctionalInterface
    interface RingSource {
        List<PciQueryRingReader.RingEntry> load(String tenant, String collection);
    }

    enum State {
        GREEN("green"), RED("red"), UNMEASURED("unmeasured");

        private final String wire;

        State(String wire) {
            this.wire = wire;
        }

        String wire() {
            return wire;
        }
    }

    static final String NO_QUERIES = "no_queries";
    static final String INDEX_NOT_USED = "index_not_used";
    static final String ERROR = "error";

    /**
     * One run's outcome; the engine keeps the latest per index.
     *
     * @param trigger   why the run happened: {@code after_build}, {@code vacuum} or {@code daily}
     * @param reason    for {@link State#UNMEASURED} only: {@link #NO_QUERIES}, {@link #INDEX_NOT_USED} or {@link #ERROR}
     * @param queries   the distinct queries the ring gave this run (every result carries it)
     * @param minRecall null unless measured
     * @param meanRecall null unless measured
     * @param top1Misses queries with a top-1 miss; 0 unless measured
     * @param at        when the run ended, on the engine's clock
     * @param redSample a red run's queries, kept for the confirmation a later phase makes; null otherwise
     * @param detail    for an unmeasured result, what happened, one line (the plan's top node, the error class); may be null
     */
    record Result(Target target, String trigger, State state, String reason, int queries, Double minRecall,
                  Double meanRecall, int top1Misses, Instant at, List<float[]> redSample, String detail) { }

    private final RingSource ring;
    private final Statements statements;
    private final Clock clock;
    private final LongSupplier nanos;

    PciCanary(RingSource ring, Statements statements) {
        this(ring, statements, Clock.systemUTC(), System::nanoTime);
    }

    PciCanary(RingSource ring, Statements statements, Clock clock, LongSupplier nanos) {
        this.ring = Objects.requireNonNull(ring, "ring");
        this.statements = Objects.requireNonNull(statements, "statements");
        this.clock = Objects.requireNonNull(clock, "clock");
        this.nanos = Objects.requireNonNull(nanos, "nanos");
    }

    /** Run the canary on {@code target}. Never throws: a failure is an unmeasured result. */
    @Override
    public Result run(Target target, String trigger) {
        List<float[]> queries;
        try {
            queries = distinctQueries(ring.load(target.tenant(), target.collection()), target.model());
        } catch (RuntimeException e) {
            log.warn("event=pci_canary_failed stage=ring_read index={} collection={} error={}", target.index(),
                target.collection(), describe(e));
            return unmeasured(target, trigger, ERROR, 0, "ring_read " + e.getClass().getSimpleName());
        }
        int n = queries.size();
        if (n < PciCanaryPolicy.MIN_QUERIES) {
            return unmeasured(target, trigger, NO_QUERIES, n, null);
        }
        long startedNanos = nanos.getAsLong();
        List<Scored> scored = new ArrayList<>(n);
        try {
            for (int qi = 0; qi < n; qi++) {
                if (qi > 0 && nanos.getAsLong() - startedNanos > PciCanaryPolicy.RUN_LIMIT.toNanos()) {
                    log.warn("event=pci_canary_failed stage=run_limit index={} collection={} queries_done={} of={}",
                        target.index(), target.collection(), qi, n);
                    return unmeasured(target, trigger, ERROR, n, "run_limit");
                }
                float[] q = queries.get(qi);
                List<List<Hit>> pages = new ArrayList<>(PciCanaryPolicy.KS.size());
                for (int ki = 0; ki < PciCanaryPolicy.KS.size(); ki++) {
                    boolean explain = qi == 0 && ki == 0;
                    Leg leg = statements.hnsw(target, q, PciCanaryPolicy.KS.get(ki), explain);
                    if (leg.routedExact()) {
                        return notUsed(target, trigger, n, "routed_exact");
                    }
                    if (explain) {
                        if (leg.plan() == null) {
                            log.warn("event=pci_canary_failed stage=plan_check index={} collection={}",
                                target.index(), target.collection());
                            return unmeasured(target, trigger, ERROR, n, "plan_unavailable");
                        }
                        if (!leg.plan().contains(target.index())) {
                            return notUsed(target, trigger, n, PciPlanCheck.describePlan(leg.plan()));
                        }
                    }
                    pages.add(leg.hits());
                }
                List<Hit> exact = statements.exact(target, q, PciCanaryPolicy.EXACT_K);
                scored.add(score(exact, pages));
            }
        } catch (RuntimeException e) {
            log.warn("event=pci_canary_failed stage=statement index={} collection={} error={}", target.index(),
                target.collection(), describe(e));
            return unmeasured(target, trigger, ERROR, n, "statement " + e.getClass().getSimpleName());
        }
        return verdict(target, trigger, n, queries, scored);
    }

    // -- scoring ---------------------------------------------------------------------------------------------

    /** One query's recalls (one per k, in {@link PciCanaryPolicy#KS} order) and whether it had a top-1 miss. */
    record Scored(double[] recalls, boolean top1Miss, boolean measurable) { }

    /**
     * Score one query: {@code exact} is the exact page (at least as long as the largest k when the collection has the
     * rows), {@code pages} the HNSW page for each k in order.
     */
    static Scored score(List<Hit> exact, List<List<Hit>> pages) {
        if (exact.isEmpty()) {
            return new Scored(new double[0], false, false);
        }
        double[] recalls = new double[pages.size()];
        boolean top1Miss = false;
        Hit nearest = exact.get(0);
        for (int ki = 0; ki < pages.size(); ki++) {
            int k = PciCanaryPolicy.KS.get(ki);
            List<Hit> page = pages.get(ki);
            int denominator = Math.min(k, exact.size());
            Set<String> topK = new HashSet<>();
            for (int i = 0; i < denominator; i++) {
                topK.add(exact.get(i).id());
            }
            double kth = exact.get(denominator - 1).distance();
            int hits = 0;
            boolean sawNearest = false;
            Set<String> seen = new HashSet<>();
            for (Hit h : page) {
                if (!seen.add(h.id())) {
                    continue;
                }
                if (topK.contains(h.id()) || h.distance() <= kth + PciCanaryPolicy.TIE_EPSILON) {
                    hits++;
                }
                if (h.id().equals(nearest.id()) || h.distance() <= nearest.distance() + PciCanaryPolicy.TIE_EPSILON) {
                    sawNearest = true;
                }
            }
            recalls[ki] = Math.min(1.0, (double) hits / denominator);
            if (!sawNearest) {
                top1Miss = true;
            }
        }
        return new Scored(recalls, top1Miss, true);
    }

    private Result verdict(Target target, String trigger, int n, List<float[]> queries, List<Scored> scored) {
        double sum = 0;
        double min = 1.0;
        int cells = 0;
        int misses = 0;
        for (Scored s : scored) {
            if (!s.measurable()) {
                continue;
            }
            for (double r : s.recalls()) {
                sum += r;
                min = Math.min(min, r);
                cells++;
            }
            if (s.top1Miss()) {
                misses++;
            }
        }
        if (cells == 0) {
            // The exact statement returned no row for any query: there is nothing to score against.
            log.warn("event=pci_canary_failed stage=exact_empty index={} collection={}", target.index(),
                target.collection());
            return unmeasured(target, trigger, ERROR, n, "exact_empty");
        }
        double mean = sum / cells;
        boolean red = mean < PciCanaryPolicy.RED_MEAN_BELOW || misses > 0;
        return new Result(target, trigger, red ? State.RED : State.GREEN, null, n, min, mean, misses,
            clock.instant(), red ? List.copyOf(queries) : null, null);
    }

    // -- results ---------------------------------------------------------------------------------------------

    private Result unmeasured(Target target, String trigger, String reason, int queries, String detail) {
        return new Result(target, trigger, State.UNMEASURED, reason, queries, null, null, 0, clock.instant(), null,
            detail);
    }

    private Result notUsed(Target target, String trigger, int queries, String planDescription) {
        log.warn("event=pci_canary_index_not_used index={} collection={} {}", target.index(), target.collection(),
            planDescription.startsWith("top=") ? planDescription : "top=\"" + planDescription + "\"");
        return unmeasured(target, trigger, INDEX_NOT_USED, queries, planDescription);
    }

    /**
     * The distinct vectors of the ring, newest first, at most {@link PciCanaryPolicy#MAX_QUERIES}: an entry with no
     * vector or of another model is skipped, and so is a vector equal to one already taken.
     */
    static List<float[]> distinctQueries(List<PciQueryRingReader.RingEntry> entries, String model) {
        List<float[]> out = new ArrayList<>();
        Set<Integer> hashes = new HashSet<>();
        for (PciQueryRingReader.RingEntry e : entries) {
            if (out.size() >= PciCanaryPolicy.MAX_QUERIES) {
                break;
            }
            float[] v = e.vector();
            if (v == null || !model.equals(e.model())) {
                continue;
            }
            boolean duplicate = false;
            if (!hashes.add(Arrays.hashCode(v))) {
                for (float[] taken : out) {
                    if (Arrays.equals(taken, v)) {
                        duplicate = true;
                        break;
                    }
                }
            }
            if (!duplicate) {
                out.add(v);
            }
        }
        return out;
    }

    /** The {@code event=pci_canary} line of a result; INFO, and one line for every result. */
    static String line(Result r) {
        StringBuilder sb = new StringBuilder("event=pci_canary index=").append(r.target().index())
            .append(" collection=").append(r.target().collection())
            .append(" tenant=").append(r.target().tenant())
            .append(" trigger=").append(r.trigger())
            .append(" queries=").append(r.queries())
            .append(" min_recall=").append(format(r.minRecall()))
            .append(" mean_recall=").append(format(r.meanRecall()))
            .append(" top1_misses=").append(r.top1Misses())
            .append(" state=").append(r.state().wire());
        if (r.reason() != null) {
            sb.append(" reason=").append(r.reason());
        }
        return sb.toString();
    }

    private static String format(Double v) {
        return v == null ? "none" : String.format(java.util.Locale.ROOT, "%.4f", v);
    }

    /** The exception's class and a bounded message with every bracketed run of numbers masked (see the ring's own). */
    private static String describe(RuntimeException e) {
        return PciQueryRing.sanitize(e);
    }
}
