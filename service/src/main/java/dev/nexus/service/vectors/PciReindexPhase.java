// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PciBuilderSession.DdlOutcome;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import dev.nexus.service.vectors.PciReconcilePlanner.Build;
import dev.nexus.service.vectors.PciReconcilePlanner.Reindex;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;

/**
 * The execution of one per-collection REINDEX (RDR-230 Technical Design 4), moved out of {@link PciReconciler} so that
 * file stays under its size cap: the statement, the bookkeeping of its outcome ({@link PciReindexState}), the fresh
 * anchor and its retry, the leaf's VACUUM after a bulk delete, and the canary again. What to run, and in what order, is
 * the reconciler's ({@code runReindexes}); a leaf's own HNSW index is its {@code reindexLeafIndex}.
 *
 * <p>It logs under {@link PciReconciler}'s logger name, so one filter sees the whole REINDEX story.
 */
final class PciReindexPhase {

    private static final Logger log = LoggerFactory.getLogger(PciReconciler.class);

    /** Writes the fresh anchor of a built index; the reconciler's counting transaction and COMMENT. */
    @FunctionalInterface
    interface Anchorer {
        /** @return whether the anchor is written; false leaves the index's old anchor in place */
        boolean write(PciBuilderSession.Pass pass, Leaf leaf, int dimension, Build build, boolean afterReindex);
    }

    private final PciReindexState state;
    private final PciCanarySchedule canary;
    private final Clock clock;
    private final Anchorer anchorer;

    /** @param canary the canary's schedule, or null (a reconciler built without one measures nothing) */
    PciReindexPhase(PciReindexState state, PciCanarySchedule canary, Clock clock, Anchorer anchorer) {
        this.state = Objects.requireNonNull(state, "state");
        this.canary = canary;
        this.clock = Objects.requireNonNull(clock, "clock");
        this.anchorer = Objects.requireNonNull(anchorer, "anchorer");
    }

    /**
     * Retry every fresh anchor a finished REINDEX left unwritten, for the indexes the snapshot still lists: a count
     * timeout, the COMMENT's lock timeout or an ended pass can leave the old anchor in place, which would re-fire the
     * size and deletes rules every pass. {@link PciReindexState#candidates} skips an owed index until this lands.
     */
    void retryOwedAnchors(PciBuilderSession.Pass pass, Set<String> snapshotIndexes) {
        for (Map.Entry<String, PciReindexState.Owed> e : state.owedAnchors().entrySet()) {
            if (!pass.live() || Thread.currentThread().isInterrupted()) {
                return;
            }
            PciReindexState.Owed owed = e.getValue();
            if (!snapshotIndexes.contains(e.getKey())) {
                continue;   // gone: the state forgets it with the snapshot
            }
            if (anchorer.write(pass, owed.leaf(), owed.dimension(), new Build(e.getKey(), owed.collection()), true)) {
                state.anchorWritten(e.getKey());
                log.info("event=pci_anchor_retried index={} leaf={}", e.getKey(), PciReconciler.leafId(owed.leaf()));
            }
        }
    }

    /**
     * REINDEX {@code subject}'s index for {@code r}, then its fresh anchor, then (a bulk delete) the leaf's VACUUM, then
     * the canary again.
     *
     * @return whether the REINDEX finished
     */
    boolean reindexOne(PciBuilderSession.Pass pass, PciReindexState.Subject subject, Reindex r) {
        Leaf leaf = subject.leaf();
        String name = r.name();
        boolean wasRed = canary != null && canary.latest(name) != null
            && canary.latest(name).state() == PciCanary.State.RED;
        log.info("event=pci_reindex_started leaf={} index={} collection={} trigger={} bytes={}",
            PciReconciler.leafId(leaf), name, r.collection(), r.trigger().label(), r.bytes());
        state.running(name);
        DdlOutcome outcome;
        try {
            outcome = pass.reindex(leaf, subject.index());
        } finally {
            state.running(null);
        }
        switch (outcome) {
            case DONE -> {
                state.finished(name, r.trigger(), clock.instant(), wasRed);
                log.info("event=pci_index_reindexed leaf={} index={} collection={} trigger={} bytes_before={}",
                    PciReconciler.leafId(leaf), name, r.collection(), r.trigger().label(), r.bytes());
                // A fresh graph, a fresh anchor (built, rows and bytes now). One that is not written is retried at the
                // start of the next pass, and the index is not judged against its old anchor meanwhile.
                boolean anchored = pass.live()
                    && anchorer.write(pass, leaf, subject.dimension(), new Build(name, r.collection()), true);
                if (!anchored) {
                    state.anchorOwed(name, leaf, subject.dimension(), r.collection());
                }
                if (r.vacuumAfter() && pass.live()) {
                    vacuumLeaf(pass, leaf);
                }
                remeasure(leaf, name, r, wasRed);
                return true;
            }
            case FAILED, REJECTED -> {
                int n = state.failed(name, clock.instant());
                log.warn("event=pci_reindex_failed leaf={} index={} collection={} trigger={} consecutive_failures={} "
                    + "next_retry_at={}", PciReconciler.leafId(leaf), name, r.collection(), r.trigger().label(), n,
                    state.nextRetryAt(name));
            }
            // The pass ended before the statement went out, or the backend was ended under it (a migration walk): not
            // this index's fault, so no backoff. The invalid copy it may have left is the leftover rule's.
            default -> log.info("event=pci_reindex_not_done leaf={} index={} outcome={}", PciReconciler.leafId(leaf),
                name, outcome);
        }
        return false;
    }

    private void vacuumLeaf(PciBuilderSession.Pass pass, Leaf leaf) {
        DdlOutcome outcome = pass.vacuum(leaf);
        if (outcome == DdlOutcome.DONE) {
            log.info("event=pci_leaf_vacuumed leaf={}", PciReconciler.leafId(leaf));
        } else {
            log.info("event=pci_leaf_vacuum_not_done leaf={} outcome={} detail=\"autovacuum reaps it in time\"",
                PciReconciler.leafId(leaf), outcome);
        }
    }

    /**
     * The canary again on the REINDEXed index, with a red run's own queries when there are some (the same in-memory
     * queries, so before and after are comparable). When the canary was red at the start, whatever the trigger's label, a
     * re-measure that is still red is {@code red_after_reindex}: reported, and not rebuilt until a later green run or a
     * day.
     */
    private void remeasure(Leaf leaf, String name, Reindex r, boolean wasRed) {
        if (canary == null) {
            return;
        }
        try {
            PciCanary.Result before = canary.latest(name);
            List<float[]> sample = before != null && before.state() == PciCanary.State.RED ? before.redSample() : null;
            var target = new PciCanary.Target(PciReconciler.leafId(leaf), leaf.tenant(), leaf.model(), r.collection(),
                name);
            PciCanary.Result after = canary.remeasure(target, sample);
            if (wasRed && after.state() == PciCanary.State.RED) {
                state.stillRed(name);
                log.warn("event=pci_red_after_reindex leaf={} index={} collection={} queries={} detail=\"still red "
                    + "after the REINDEX; not rebuilt until a green run or a day\"", PciReconciler.leafId(leaf), name,
                    r.collection(), after.queries());
            }
        } catch (RuntimeException e) {
            log.warn("event=pci_canary_failed stage=after_reindex index={} error={}", name, PciQueryRing.sanitize(e));
        }
    }
}
