// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.time.Instant;
import java.util.Comparator;
import java.util.List;
import java.util.Optional;

/**
 * The {@code maintenance} object of {@code GET /v1/status} {@code per_collection_indexes.this_engine} (RDR-230
 * Technical Design 7, bead nexus-4lnn1.1.5), as the reconciler holds it: values in memory, so the route runs no catalog
 * query (RDR-227's rule). Phase 1 reports the canary half; the REINDEX half is fixed at {@code off}, no index in
 * flight, no reindex yet, because nothing reindexes until Phase 2.
 *
 * <p><b>Who may read it.</b> The route is unauthenticated. An entry is keyed by the opaque {@code pci_<24 hex>} name and
 * has no field that carries a collection, a tenant or a leaf name; the log lines carry those for operators.
 *
 * <p><b>Order and cap.</b> {@link #MAX_ENTRIES} entries: red first, then unmeasured (an index nobody could measure is
 * more worth a look than a green one), then green; newest {@code measured_at} first within a state, then index name.
 * The counts cover every result, not only the entries listed.
 *
 * @param reindex       {@code on} or {@code off}; {@code off} until Phase 2
 * @param reindexing    the index being reindexed on the holder, or null
 * @param lastReindexAt when this engine last finished a reindex, or null
 * @param reindexes     reindexes this engine has finished since boot
 * @param canary        the canary's counts and entries
 */
public record PciMaintenanceStatus(String reindex, String reindexing, Instant lastReindexAt, int reindexes,
                                   Canary canary) {

    /** The most entries the list carries. */
    public static final int MAX_ENTRIES = 64;

    /**
     * @param green      indexes whose latest result on this engine is green
     * @param red        indexes whose latest result is red
     * @param unmeasured indexes whose latest result is unmeasured
     * @param lastRunAt  the latest {@code measuredAt} of any result, or null when this engine has none
     * @param indexes    at most {@link #MAX_ENTRIES} entries, in the order above
     */
    public record Canary(int green, int red, int unmeasured, Instant lastRunAt, List<Entry> indexes) { }

    /**
     * One index's latest canary result.
     *
     * @param index           the opaque {@code pci_<24 hex>} name
     * @param state           {@code green}, {@code red} or {@code unmeasured}
     * @param reason          for an unmeasured entry only: {@code no_queries}, {@code index_not_used} or {@code error}
     * @param queries         the distinct ring queries the run had (an unmeasured entry carries it too)
     * @param minRecall       null unless measured
     * @param meanRecall      null unless measured
     * @param top1Misses      queries with a top-1 miss; 0 unless measured
     * @param trigger         {@code after_build}, {@code vacuum} or {@code daily}
     * @param measuredAt      when the run ended
     * @param anchor          {@code built} or {@code adopted}, or null when the index carries no anchor this engine can
     *                        read
     * @param sizeRatio       bytes per row now over bytes per row at the anchor; null for an adopted index (it is judged
     *                        against the install's reference, which Phase 2 builds), without a reading, or at zero rows
     * @param deletesSinceBuild {@code max(0, anchor.rows + written_since - rows_now) / rows_now}, the estimate of RDR-230
     *                        Technical Design 3; null without a reading or at zero rows
     */
    public record Entry(String index, String state, String reason, int queries, Double minRecall, Double meanRecall,
                        int top1Misses, String trigger, Instant measuredAt, String anchor, Double sizeRatio,
                        Double deletesSinceBuild) { }

    /** The Phase 1 status: the canary half over {@code entries} (any order, any number), the reindex half off. */
    public static PciMaintenanceStatus phase1(List<Entry> entries) {
        int green = 0;
        int red = 0;
        int unmeasured = 0;
        Instant lastRun = null;
        for (Entry e : entries) {
            switch (e.state()) {
                case "green" -> green++;
                case "red" -> red++;
                default -> unmeasured++;
            }
            if (e.measuredAt() != null && (lastRun == null || e.measuredAt().isAfter(lastRun))) {
                lastRun = e.measuredAt();
            }
        }
        List<Entry> shown = entries.stream().sorted(ORDER).limit(MAX_ENTRIES).toList();
        return new PciMaintenanceStatus("off", null, null, 0, new Canary(green, red, unmeasured, lastRun, shown));
    }

    private static int rank(String state) {
        return switch (state) {
            case "red" -> 0;
            case "unmeasured" -> 1;
            default -> 2;
        };
    }

    private static final Comparator<Entry> ORDER = Comparator
        .comparingInt((Entry e) -> rank(e.state()))
        .thenComparing(Entry::measuredAt, Comparator.nullsLast(Comparator.reverseOrder()))
        .thenComparing(Entry::index);

    /**
     * One canary result as an entry.
     *
     * @param reading     what the reconciler last read of this index (its anchor, bytes, and the row counts), or null
     * @param sweepOrigin the origin of the anchor the router's set holds for the index, used when there is no reading
     */
    static Entry entry(PciCanary.Result r, PciReconciler.IndexReading reading, Optional<String> sweepOrigin) {
        String origin = reading != null ? reading.anchor().origin() : sweepOrigin.orElse(null);
        // The word is an operator-writable comment; only the two the engine writes are published.
        String anchor = PciCatalog.ORIGIN_BUILT.equals(origin) || PciCatalog.ORIGIN_ADOPTED.equals(origin) ? origin : null;
        return new Entry(r.target().index(), r.state().wire(), r.reason(), r.queries(), r.minRecall(), r.meanRecall(),
            r.top1Misses(), r.trigger(), r.at(), anchor, sizeRatio(anchor, reading), deletesSinceBuild(reading));
    }

    private static Double sizeRatio(String anchor, PciReconciler.IndexReading reading) {
        if (reading == null || !PciCatalog.ORIGIN_BUILT.equals(anchor)) {
            return null;
        }
        PciCatalog.Anchor a = reading.anchor();
        if (reading.rows() <= 0 || a.rows() <= 0 || a.bytes() <= 0) {
            return null;
        }
        return ((double) reading.bytes() / reading.rows()) / ((double) a.bytes() / a.rows());
    }

    private static Double deletesSinceBuild(PciReconciler.IndexReading reading) {
        if (reading == null || reading.rows() <= 0) {
            return null;
        }
        long deleted = Math.max(0, reading.anchor().rows() + reading.writtenSince() - reading.rows());
        return (double) deleted / reading.rows();
    }
}
