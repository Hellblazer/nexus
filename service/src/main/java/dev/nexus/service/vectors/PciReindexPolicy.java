// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.List;
import java.util.Optional;
import java.util.OptionalDouble;
import java.util.OptionalLong;

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

    /**
     * What the trigger rules read of one valid, parsed per-collection index (RDR-230 Technical Design 3).
     *
     * @param name           the index name
     * @param collection     the collection it serves
     * @param dimension      the embedding dimension of its leaf
     * @param anchor         the anchor on the index
     * @param bytes          {@code pg_relation_size} now
     * @param rows           the collection's uncapped row count now
     * @param writtenSince   rows written after {@code anchor.builtAt()}
     * @param previousRows   the row count at this engine's previous check of the index; empty after a boot or a change
     *                       of holder
     * @param bulkPending    a bulk delete was seen on an earlier pass and its REINDEX has not run yet
     * @param canary         the canary's latest state for the index on this engine, or null when it has none
     * @param canaryQueries  the distinct ring queries that result had
     * @param lastRedReindexAt when the last red-canary REINDEX of the index finished, or null
     * @param backedOff      the index's REINDEX is in its failure backoff
     * @param reference      the install reference (bytes per row) for the index's dimension, for an adopted anchor
     */
    record Candidate(String name, String collection, int dimension, PciCatalog.Anchor anchor, long bytes, long rows,
                     long writtenSince, OptionalLong previousRows, boolean bulkPending, PciCanary.State canary,
                     int canaryQueries, Instant lastRedReindexAt, boolean backedOff, OptionalDouble reference) { }

    /**
     * The trigger that fires for {@code c} at {@code now}, if any; a pure function of its arguments. When several fire
     * the label is the first of: bulk delete (its VACUUM must not be lost), red canary, deletes since build, size.
     * An index with no rows has nothing to rebuild, and a backed-off one waits for its retry time, whatever the rules say.
     */
    static Optional<PciReconcilePlanner.Trigger> trigger(Candidate c, Instant now) {
        if (c.backedOff() || c.rows() <= 0) {
            return Optional.empty();
        }
        if (bulkDelete(c)) {
            return Optional.of(PciReconcilePlanner.Trigger.BULK_DELETE);
        }
        if (redCanary(c, now)) {
            return Optional.of(PciReconcilePlanner.Trigger.RED_CANARY);
        }
        if (deletesSinceBuild(c)) {
            return Optional.of(PciReconcilePlanner.Trigger.DELETES_SINCE_BUILD);
        }
        if (size(c)) {
            return Optional.of(PciReconcilePlanner.Trigger.SIZE);
        }
        return Optional.empty();
    }

    /** {@code rows_now} fell by at least {@link #BULK_DELETE_FRACTION} since the previous check, or one is owed. */
    private static boolean bulkDelete(Candidate c) {
        return c.bulkPending() || bulkFall(c.previousRows(), c.rows());
    }

    /** Whether {@code rows} is at least {@link #BULK_DELETE_FRACTION} below the previous check's count (none: false). */
    static boolean bulkFall(OptionalLong previousRows, long rows) {
        if (previousRows.isEmpty() || previousRows.getAsLong() <= 0) {
            return false;
        }
        long previous = previousRows.getAsLong();
        return (double) (previous - rows) / previous >= BULK_DELETE_FRACTION;
    }

    /**
     * Red on at least {@link #RED_CANARY_MIN_QUERIES} queries, and not within a day of this index's last REINDEX or
     * build. The engine's memory of its last red-canary REINDEX ({@code lastRedReindexAt}) is lost on a restart or a change
     * of holder, so the day is also read from the anchor, which a REINDEX rewrites and which survives both: a persistent
     * red is not rebuilt on every restart. (An index built or adopted less than a day ago waits the same day.)
     */
    private static boolean redCanary(Candidate c, Instant now) {
        if (c.canary() != PciCanary.State.RED || c.canaryQueries() < RED_CANARY_MIN_QUERIES) {
            return false;
        }
        if (now.isBefore(c.anchor().builtAt().plus(RED_REINDEX_MIN_INTERVAL))) {
            return false;
        }
        return c.lastRedReindexAt() == null
            || !now.isBefore(c.lastRedReindexAt().plus(RED_REINDEX_MIN_INTERVAL));
    }

    /** {@code max(0, anchor.rows + written_since - rows_now)} is at least {@link #DELETES_SINCE_BUILD_FRACTION} of the rows. */
    private static boolean deletesSinceBuild(Candidate c) {
        long dead = Math.max(0, c.anchor().rows() + c.writtenSince() - c.rows());
        return (double) dead / c.rows() >= DELETES_SINCE_BUILD_FRACTION;
    }

    private static boolean size(Candidate c) {
        return c.rows() > 0 && sizeFires(c.anchor(), c.bytes(), c.rows(), c.reference());
    }

    /**
     * Bytes per row is at least {@link #SIZE_RATIO} times the reference: the anchor's own for an index this engine built,
     * the install reference for an adopted one (an adopted anchor is a snapshot of an index of unknown age). With no
     * reference the rule is off. {@code rows <= 0} is never a size.
     *
     * <p>Evaluated with a row count that can only be too small (ingest since the last count), this is an upper bound on
     * the rule: when it is false the true ratio is below the trigger too, so the count need not be retaken
     * ({@code PciReconciler#readAnchors}).
     */
    static boolean sizeFires(PciCatalog.Anchor a, long bytes, long rows, OptionalDouble reference) {
        if (rows <= 0) {
            return false;
        }
        boolean built = PciCatalog.ORIGIN_BUILT.equals(a.origin()) && a.rows() > 0 && a.bytes() > 0;
        OptionalDouble base = built ? OptionalDouble.of((double) a.bytes() / a.rows()) : reference;
        if (base.isEmpty() || base.getAsDouble() <= 0) {
            return false;
        }
        return ((double) bytes / rows) / base.getAsDouble() >= SIZE_RATIO;
    }
}
