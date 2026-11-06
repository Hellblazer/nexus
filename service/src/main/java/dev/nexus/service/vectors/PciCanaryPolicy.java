// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.time.Duration;
import java.util.List;

/**
 * The recall canary's thresholds and bounds in one place (RDR-230 Technical Design 5; Open Question 4: the numbers are
 * provisional and are tuned from the {@code event=pci_canary} log, so changing one is a change to this file and no
 * other). Nothing here reads configuration.
 *
 * <p>The red bar, the three-query minimum and the single-collection ring admission are decided (Sam, decision 2, and
 * gate finding S4): changing {@link #MIN_QUERIES}, {@link #RED_MEAN_BELOW} or the rule that any top-1 miss is red
 * changes a decided bar and is Sam's call.
 */
final class PciCanaryPolicy {

    private PciCanaryPolicy() { }

    /** The HNSW statement's page sizes, one statement per query per entry (the RDR-227 Gap 1 method). */
    static final List<Integer> KS = List.of(40, 60, 80, 100, 120);

    /** The exact statement's page size: the largest of {@link #KS}, so its first k rows are the exact top-k. */
    static final int EXACT_K = 120;

    /** The most ring queries one run uses. */
    static final int MAX_QUERIES = 10;

    /** Fewer distinct queries than this and the index stays unmeasured (reason no_queries). */
    static final int MIN_QUERIES = 3;

    /** Red when the mean recall over queries and k is below this. */
    static final double RED_MEAN_BELOW = 0.99;

    /** Two distances this close are one distance: a row at the k-th exact distance is a tie at the boundary. */
    static final double TIE_EPSILON = 1e-12;

    /** A vacuum-keyed run happens at most once per index in this long. */
    static final Duration VACUUM_MIN_INTERVAL = Duration.ofHours(6);

    /** An index whose last run on this engine is older than this (or that has none) is run. */
    static final Duration STALE_AFTER = Duration.ofHours(24);

    /**
     * An index whose run ended unmeasured with reason {@code error} keeps its trigger and is not tried again before
     * this has passed. The pass period defaults to ten minutes, so this is three passes: the causes seen in design (a
     * permit wait that timed out under load, a ring read that blipped, a statement timeout just after a build) clear in
     * minutes. This is the wait after the first error; each further consecutive error doubles it, up to
     * {@link #ERROR_RETRY_CAP}, because a deterministic error does not clear and a flat floor would re-run it 48 times
     * a day per index. See {@link #errorRetryAfter}.
     */
    static final Duration ERROR_RETRY_FLOOR = Duration.ofMinutes(30);

    /** The longest the wait after a run of consecutive errors grows to. */
    static final Duration ERROR_RETRY_CAP = Duration.ofHours(24);

    /**
     * The wait after the {@code consecutiveErrors}-th consecutive errored run of one index (1 for the first):
     * {@link #ERROR_RETRY_FLOOR} doubled once per earlier error, never more than {@link #ERROR_RETRY_CAP}.
     */
    static Duration errorRetryAfter(int consecutiveErrors) {
        int doublings = Math.min(Math.max(consecutiveErrors, 1) - 1, 16);   // 30 min * 2^16 is past the cap; no overflow
        Duration wait = ERROR_RETRY_FLOOR.multipliedBy(1L << doublings);
        return wait.compareTo(ERROR_RETRY_CAP) > 0 ? ERROR_RETRY_CAP : wait;
    }

    /** The most canary time one pass spends before it carries the remaining indexes to the next pass (provisional). */
    static final Duration PASS_BUDGET = Duration.ofSeconds(60);

    /** One run's own bound: a run past this between two queries ends as unmeasured (reason error). */
    static final Duration RUN_LIMIT = Duration.ofMinutes(2);

    /** How long a canary statement waits for its search permit before the run ends as unmeasured (reason error). */
    static final Duration PERMIT_WAIT = Duration.ofSeconds(10);
}
