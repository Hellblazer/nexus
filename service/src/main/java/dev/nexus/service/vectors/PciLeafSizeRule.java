// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.OptionalDouble;

/**
 * RDR-230 Technical Design 3, the size rule for a leaf's own HNSW index (nexus-4lnn1.1.10), as a pure function.
 *
 * <p>The rule fires when {@code bytes / rows >= 1.5 x reference}: {@code bytes} is {@code pg_relation_size} of the
 * leaf's index, {@code rows} is the leaf's {@code pg_stat_user_tables.n_live_tup} (an estimate is enough for a ratio)
 * and {@code reference} is the install reference for the index's dimension, in bytes per row. Where the reference
 * comes from (the median bytes per row over this install's {@code origin:"built"} anchors at that dimension, else
 * 8.2 KB per row at 1024 dimensions only) is {@code PciReindexPolicy.installReference}'s business, not this class's;
 * this class only compares.
 *
 * <ul>
 *   <li>{@link Verdict#UNANCHORED}: there is no reference for the dimension (any dimension but 1024 with no built
 *       anchor). The rule is inactive and the status says so. Checked first, so a leaf with no reference is
 *       {@code UNANCHORED} whatever its rows.</li>
 *   <li>{@link Verdict#FIRE}: the ratio is at or above 1.5 times the reference. The comparison is on the products
 *       ({@code bytes >= 1.5 x reference x rows}), so no division happens.</li>
 *   <li>{@link Verdict#QUIET}: anything else, and a leaf with {@code rows == 0}. An empty leaf has no ratio, and an
 *       index with no live rows to serve is not worth a REINDEX; a leaf whose estimate is zero because statistics
 *       have not run yet gets its verdict on the pass after they do.</li>
 * </ul>
 */
public final class PciLeafSizeRule {

    /**
     * The multiple of the install reference at or above which the rule fires (RDR-230 Technical Design 3). It is
     * {@link PciReindexPolicy#LEAF_SIZE_RATIO}, the one place the number lives.
     */
    public static final double LEAF_SIZE_RATIO = PciReindexPolicy.LEAF_SIZE_RATIO;

    /** What the rule says about one leaf index. */
    public enum Verdict {
        /** Reindex the leaf index. */
        FIRE,
        /** The index is within the multiple, or the leaf has no live rows. */
        QUIET,
        /** No install reference for the dimension: the rule is inactive. */
        UNANCHORED
    }

    private PciLeafSizeRule() { }

    /**
     * @param bytes     {@code pg_relation_size} of the leaf's HNSW index, not negative
     * @param rows      the leaf's {@code n_live_tup}, not negative
     * @param reference the install reference in bytes per row for the index's dimension, or empty when there is none;
     *                  when present it must be a positive, finite number
     * @throws IllegalArgumentException for a negative {@code bytes} or {@code rows}, or a present reference that is not
     *                                  positive and finite
     */
    public static Verdict evaluate(long bytes, long rows, OptionalDouble reference) {
        if (bytes < 0 || rows < 0) {
            throw new IllegalArgumentException("bytes and rows must not be negative: " + bytes + ", " + rows);
        }
        if (reference.isEmpty()) {
            return Verdict.UNANCHORED;
        }
        double ref = reference.getAsDouble();
        if (!(ref > 0) || Double.isInfinite(ref)) {
            throw new IllegalArgumentException("the install reference must be positive and finite: " + ref);
        }
        if (rows == 0) {
            return Verdict.QUIET;
        }
        return (double) bytes >= LEAF_SIZE_RATIO * ref * (double) rows ? Verdict.FIRE : Verdict.QUIET;
    }
}
