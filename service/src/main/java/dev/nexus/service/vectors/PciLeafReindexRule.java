// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PciReconcilePlanner.Reindex;
import dev.nexus.service.vectors.PciReconcilePlanner.Trigger;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.OptionalDouble;
import java.util.TreeMap;
import java.util.function.BiFunction;
import java.util.function.Function;
import java.util.function.Predicate;

/**
 * RDR-230 Technical Design 3, the leaf size rule applied to a pass (nexus-4lnn1.1.10): which leaves' own HNSW indexes
 * call for a {@link Trigger#LEAF_SIZE} REINDEX, and how many cannot be judged. A pure function of the pass's looks, the
 * leaves' live row counts and the backoff; it reads nothing. The decision for one index is {@link PciLeafSizeRule}.
 *
 * <p>For every leaf the pass counted ({@code looks}), the index is the leaf's own HNSW index on the leaf's dimension
 * ({@link PciCatalog.Leaf#hnswIndex}), which must be valid. The install reference for the dimension is
 * {@link PciReindexPolicy#installReference} over every built anchor the looks read at that dimension, so the leaf
 * rule and the per-collection size rule share one reference. An index in its failure backoff is not a candidate.
 *
 * <p><b>The index's size and anchor are read lazily</b>, by the caller's {@code facts} function, and only for an index
 * that has a reference at its dimension and is not in backoff: {@code pg_relation_size} takes a lock on the index, so
 * it is not part of the shared catalog read (see {@link PciCatalog.LeafIndex}). A {@code null} answer (a lock timeout,
 * a failed read, an index gone) skips the index for this pass.
 *
 * <p><b>The leaf's rows</b> are the caller's estimate ({@code max(n_live_tup, reltuples)}, see
 * {@link PciAnchorReads#leafRowEstimates}) but never less than the rows this pass counted, exactly, in the leaf's
 * indexed collections ({@link PciReconciler.IndexReading#rows}), which are rows the leaf certainly holds. The
 * statistics lag the writes (a leaf just loaded can read a small fraction of its rows) and are zeroed by an unclean
 * restart; a low estimate reads as a bloated index, and the exact lower bound stops the REINDEX that would follow. A
 * leaf the estimate has no row for has 0. An empty leaf is quiet.
 *
 * <p><b>Cooldown.</b> One LEAF_SIZE REINDEX per index per {@link #LEAF_SIZE_MIN_INTERVAL}, measured from the
 * {@code built_at} of the anchor on the index itself (written by the previous REINDEX), so it survives a restart
 * and a change of holder. With no parseable anchor there is no cooldown. A rule that kept firing would rebuild a
 * large index every pass for as long as the rows it is divided by stayed low.
 *
 * <p>{@link Scan#unanchored()} counts the valid own indexes on counted leaves whose dimension has no reference: the
 * rule is inactive for them, whatever their size. It is recomputed every pass, so it falls back when an anchor appears.
 */
final class PciLeafReindexRule {

    private PciLeafReindexRule() { }

    /** At most one LEAF_SIZE REINDEX of an index in this long, counted from the anchor its last REINDEX wrote. */
    static final Duration LEAF_SIZE_MIN_INTERVAL = Duration.ofHours(24);

    /** The REINDEXes the rule calls for (in leaf order; the caller sorts) and the indexes it cannot judge. */
    record Scan(List<Reindex> candidates, int unanchored) { }

    /** {@code schema.name}, the key of a leaf in the statistics map and in logs. */
    static String leafId(PciCatalog.Leaf leaf) {
        return leaf.schema() + "." + leaf.name();
    }

    /**
     * @param looks        what the pass's counting transactions read, one per processed leaf
     * @param rowEstimates the leaf's row estimate by {@link #leafId}, or null when there is none (0)
     * @param facts        the index's size and comment now, or null to skip it this pass; called only for an index that
     *                     can be judged
     * @param backedOff    whether an index name is in its REINDEX failure backoff
     * @param now          the pass's clock, for the cooldown
     */
    static Scan scan(List<PciReindexState.Look> looks, Function<String, Long> rowEstimates,
                     BiFunction<PciCatalog.Leaf, PciCatalog.LeafIndex, PciAnchorReads.IndexFacts> facts,
                     Predicate<String> backedOff, Instant now) {
        Map<Integer, List<PciCatalog.Anchor>> anchorsByDimension = new TreeMap<>();
        for (PciReindexState.Look look : looks) {
            for (PciReconciler.IndexReading reading : look.readings().values()) {
                anchorsByDimension.computeIfAbsent(look.dimension(), d -> new ArrayList<>()).add(reading.anchor());
            }
        }
        List<Reindex> out = new ArrayList<>();
        int unanchored = 0;
        for (PciReindexState.Look look : looks) {
            PciCatalog.LeafIndex own = look.leaf().hnswIndex(look.dimension()).orElse(null);
            if (own == null || !own.valid()) {
                continue;
            }
            OptionalDouble reference = PciReindexPolicy.installReference(look.dimension(),
                anchorsByDimension.getOrDefault(look.dimension(), List.of()));
            if (reference.isEmpty()) {
                unanchored++;
                continue;
            }
            if (backedOff.test(own.name())) {
                continue;
            }
            PciAnchorReads.IndexFacts read = facts.apply(look.leaf(), own);
            if (read == null) {
                continue;
            }
            Long estimate = rowEstimates.apply(leafId(look.leaf()));
            long counted = look.readings().values().stream().mapToLong(PciReconciler.IndexReading::rows).sum();
            long rows = Math.max(estimate == null ? 0 : estimate, counted);
            if (PciLeafSizeRule.evaluate(read.bytes(), rows, reference) != PciLeafSizeRule.Verdict.FIRE) {
                continue;
            }
            boolean cooling = PciCatalog.parseAnchor(read.comment())
                .map(a -> now.isBefore(a.builtAt().plus(LEAF_SIZE_MIN_INTERVAL))).orElse(false);
            if (!cooling) {
                out.add(new Reindex(own.name(), null, Trigger.LEAF_SIZE, read.bytes()));
            }
        }
        return new Scan(List.copyOf(out), unanchored);
    }
}
