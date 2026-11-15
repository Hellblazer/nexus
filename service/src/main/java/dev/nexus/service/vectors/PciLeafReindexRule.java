// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PciReconcilePlanner.Reindex;
import dev.nexus.service.vectors.PciReconcilePlanner.Trigger;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.OptionalDouble;
import java.util.TreeMap;
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
 * <p><b>The leaf's live rows</b> are {@code n_live_tup} from {@code liveRows}, but never less than the rows this pass
 * counted, exactly, in the leaf's indexed collections ({@link PciReconciler.IndexReading#rows}), which are rows the
 * leaf certainly holds. The statistics are an estimate that lags the writes (they are flushed about once a second, and
 * a leaf just loaded can read a small fraction of its rows), and a lag only ever errs low, which reads as a bloated
 * index; the exact lower bound stops a REINDEX that a fresh leaf would otherwise get. A leaf with no statistics row
 * has {@code n_live_tup} 0 (it has never been written). An empty leaf is quiet.
 *
 * <p>{@link Scan#unanchored()} counts the valid own indexes on counted leaves whose dimension has no reference: the
 * rule is inactive for them, whatever their size. It is recomputed every pass, so it falls back when an anchor appears.
 */
final class PciLeafReindexRule {

    private PciLeafReindexRule() { }

    /** The REINDEXes the rule calls for (in leaf order; the caller sorts) and the indexes it cannot judge. */
    record Scan(List<Reindex> candidates, int unanchored) { }

    /** {@code schema.name}, the key of a leaf in the statistics map and in logs. */
    static String leafId(PciCatalog.Leaf leaf) {
        return leaf.schema() + "." + leaf.name();
    }

    /**
     * @param looks    what the pass's counting transactions read, one per processed leaf
     * @param liveRows the leaf's {@code n_live_tup} by {@link #leafId}, or null when it has no statistics row (0)
     * @param backedOff whether an index name is in its REINDEX failure backoff
     */
    static Scan scan(List<PciReindexState.Look> looks, Function<String, Long> liveRows, Predicate<String> backedOff) {
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
            Long stats = liveRows.apply(leafId(look.leaf()));
            long counted = look.readings().values().stream().mapToLong(PciReconciler.IndexReading::rows).sum();
            long rows = Math.max(stats == null ? 0 : stats, counted);
            OptionalDouble reference = PciReindexPolicy.installReference(look.dimension(),
                anchorsByDimension.getOrDefault(look.dimension(), List.of()));
            switch (PciLeafSizeRule.evaluate(own.bytes(), rows, reference)) {
                case UNANCHORED -> unanchored++;
                case FIRE -> {
                    if (!backedOff.test(own.name())) {
                        out.add(new Reindex(own.name(), null, Trigger.LEAF_SIZE, own.bytes()));
                    }
                }
                case QUIET -> { }
            }
        }
        return new Scan(List.copyOf(out), unanchored);
    }
}
