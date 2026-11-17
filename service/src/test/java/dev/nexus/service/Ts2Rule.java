// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.util.List;

/**
 * RDR-225's TS2 rule, as {@link ReadPathLeafPruningIntegrationTest} applies it to the planning p95 of one search
 * family at 2 tenants and at 300: p95 at 300 at most 2x the 2-tenant p95 (counted only when the two differ by more
 * than {@link #RATIO_MIN_DIFF_MS}) and at most {@link #CAP_MS}. The rule is applied to each set's pair of figures,
 * and a family fails only when every set fails (see {@link #judge}); RDR-225 recorded the lowest p95 of five sets
 * per side. Kept apart from the test so the rule and the per-set verdict are tested on recorded figures without a
 * container.
 */
final class Ts2Rule {

    private Ts2Rule() {}

    /**
     * The 2x rule fails a family only when the 300-tenant p95 is also more than this many ms above the 2-tenant
     * p95 (RDR-225 amendment of 2026-10-12). A family that plans in 0.6 ms moves by 0.6 ms from one phase to the
     * next on a shared box, in either direction, and that alone is 2x: on develop aa226556f assign_from_chashes
     * 1024 read 1.232 ms against 0.600 ms (2.05x) while text_gate_probe 768 went from 1.326 ms DOWN to 0.669 ms.
     * A planning cost that grows with the tenant count is far above this: the control, plain_search_1024 with its
     * tenant predicate removed, reads 15.4 ms against 0.9 ms. The 25 ms cap has no such allowance.
     */
    static final double RATIO_MIN_DIFF_MS = 2.0;

    /** The ceiling on the 300-tenant p95, in ms, with no allowance. */
    static final double CAP_MS = 25.0;

    /** The rule on one pair of figures, in ms. */
    static boolean violates(double p2, double p300) {
        boolean overRatio = p300 > 2 * p2 && p300 - p2 > RATIO_MIN_DIFF_MS;
        return overRatio || p300 > CAP_MS;
    }

    /** One family at one dimension: the pair that decided it, in ms, the verdict, and every set's pair. */
    record Figures(double few, double many, boolean violates, String sets) {}

    /**
     * The verdict on one family's sets, each a {@code {2-tenant p95, 300-tenant p95}} pair in ms timed in
     * alternation: the family violates TS2 only when every set does. The figures are the best set's: the passing set
     * with the lowest ratio of 300-tenant to 2-tenant p95, or, when none passes, the set with the lowest ratio, so
     * a set that caught a burst of load on one side only is not the one shown. The old design took the lowest p95 of
     * the 2-tenant sets and the lowest of the 300-tenant sets, each column on its own, timed minutes apart; under
     * load that failed pruned families whose plans had not grown: plain_search 1024 read 1.41 ms at 2 tenants and
     * 4.63 ms at 300 (3.3x, 3.2 ms apart), a pair no set of the new design has produced. A planning cost that grows
     * with the tenant count is in every pair.
     */
    static Figures judge(List<double[]> pairs) {
        if (pairs.isEmpty()) {
            throw new IllegalArgumentException("no sets");
        }
        double[] best = null;
        boolean anyPasses = false;
        StringBuilder sets = new StringBuilder();
        for (double[] pair : pairs) {
            sets.append(String.format(" %.2f/%.2f", pair[0], pair[1]));
            boolean passes = !violates(pair[0], pair[1]);
            if (best == null || (passes && !anyPasses)
                    || (passes == anyPasses && pair[1] / pair[0] < best[1] / best[0])) {
                best = pair;
            }
            anyPasses |= passes;
        }
        return new Figures(best[0], best[1], !anyPasses, sets.toString().trim());
    }
}
