// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.List;
import java.util.OptionalDouble;

/**
 * The REINDEX triggers' thresholds and bounds in one place (RDR-230 Technical Design 3 and 8; Open Question 4: the
 * numbers are tuned from the {@code event=pci_canary} log and changed by an engine cut, so changing one is a change to
 * this file and no other). Nothing here reads configuration.
 *
 * <p>The values are the ones Sam confirmed on 2026-11-14 (T2 {@code nexus_rdr/230-thresholds-2026-11-14}, ask
 * {@code rdr230-thresholds}, nexus-4lnn1.1.9): the drafted values stand, plus a floor on the red-canary trigger
 * ({@link #RED_CANARY_MIN_QUERIES}).
 */
final class PciReindexPolicy {

    private PciReindexPolicy() { }

    /** Size rule for a per-collection index: bytes per row now over bytes per row at the anchor (or the install reference). */
    static final double SIZE_RATIO = 1.4;

    /** Size rule for a leaf's own HNSW index: bytes per row over the install reference for its dimension (P2.3). */
    static final double LEAF_SIZE_RATIO = 1.5;

    /** Deletes since build: {@code max(0, anchor.rows + written_since - rows_now)} as a fraction of {@code rows_now}. */
    static final double DELETES_SINCE_BUILD_FRACTION = 0.25;

    /** Bulk delete: {@code rows_now} fell by at least this fraction since this engine's previous check. */
    static final double BULK_DELETE_FRACTION = 0.10;

    /** The most REINDEX statements one pass runs, one at a time, largest index first. */
    static final int MAX_PER_PASS = 2;

    /** A red-canary REINDEX of one index runs at most once in this long; also how long red_after_reindex holds. */
    static final Duration RED_REINDEX_MIN_INTERVAL = Duration.ofHours(24);

    /**
     * A red result triggers a REINDEX only when the run had at least this many ring queries (Sam, 2026-11-14): a red
     * below it is recorded and shown and triggers nothing. It is the canary's own sample size
     * ({@link PciCanaryPolicy#MAX_QUERIES}), so a run that had ten queries had all it can use.
     */
    static final int RED_CANARY_MIN_QUERIES = PciCanaryPolicy.MAX_QUERIES;

    /** The dimension at which the install reference has a built-in value when no anchor says otherwise. */
    static final int FALLBACK_DIMENSION = 1024;

    /**
     * Bytes per row of a fresh build at {@link #FALLBACK_DIMENSION}: 8.2 KB, the 8 KB page each element takes at that
     * dimension (RDR-227 A3, measured on fresh builds).
     */
    static final double FALLBACK_BYTES_PER_ROW = 8_200.0;

    /**
     * The install reference for {@code dimension}: the median bytes per row over the anchors in
     * {@code anchorsAtDimension} whose origin is {@code built} (an adopted anchor is a snapshot of an index of unknown
     * age, not a build figure). The caller passes the anchors of this install's indexes at that dimension; an anchor
     * with no rows or no bytes carries no ratio and is skipped. With none left, {@link #FALLBACK_BYTES_PER_ROW} at
     * {@link #FALLBACK_DIMENSION}, and empty at any other dimension (the rule that needs it is then inactive).
     *
     * @param dimension          the embedding dimension the anchors belong to
     * @param anchorsAtDimension anchors of indexes at that dimension, any origin
     */
    static OptionalDouble installReference(int dimension, Collection<PciCatalog.Anchor> anchorsAtDimension) {
        List<Double> perRow = new ArrayList<>();
        for (PciCatalog.Anchor a : anchorsAtDimension) {
            if (PciCatalog.ORIGIN_BUILT.equals(a.origin()) && a.rows() > 0 && a.bytes() > 0) {
                perRow.add((double) a.bytes() / a.rows());
            }
        }
        if (perRow.isEmpty()) {
            return dimension == FALLBACK_DIMENSION ? OptionalDouble.of(FALLBACK_BYTES_PER_ROW) : OptionalDouble.empty();
        }
        Collections.sort(perRow);
        int n = perRow.size();
        double median = n % 2 == 1 ? perRow.get(n / 2) : (perRow.get(n / 2 - 1) + perRow.get(n / 2)) / 2.0;
        return OptionalDouble.of(median);
    }
}
