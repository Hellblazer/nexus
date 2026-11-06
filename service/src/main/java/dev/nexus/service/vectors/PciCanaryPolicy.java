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

    /** The most canary time one pass spends before it carries the remaining indexes to the next pass (provisional). */
    static final Duration PASS_BUDGET = Duration.ofSeconds(60);

    /** One run's own bound: a run past this between two queries ends as unmeasured (reason error). */
    static final Duration RUN_LIMIT = Duration.ofMinutes(2);

    /** How long a canary statement waits for its search permit before the run ends as unmeasured (reason error). */
    static final Duration PERMIT_WAIT = Duration.ofSeconds(10);
}
