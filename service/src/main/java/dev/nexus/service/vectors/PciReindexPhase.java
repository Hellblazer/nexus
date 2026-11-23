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
import java.time.Instant;
import java.util.HashSet;
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

    /** Records {@code red_unfixed} in the anchor of an index whose red-canary REINDEX re-measured still red. */
    @FunctionalInterface
    interface RedMarker {
        /** @return whether the field is written */
        boolean mark(PciBuilderSession.Pass pass, Leaf leaf, String index, String collection, Instant at,
                     List<String> queries);
    }

    private final PciReindexState state;
    private final PciCanarySchedule canary;
    private final Clock clock;
    private final Anchorer anchorer;
    private final RedMarker redMarker;
    /** Whether the canary measures id indexes now: the reconciler's latch (gate 3 latched, id readers present). Before it an id REINDEX is not re-measured. */
    private volatile java.util.function.BooleanSupplier idCanary = () -> false;

    /** @param canary the canary's schedule, or null (a reconciler built without one measures nothing) */
    PciReindexPhase(PciReindexState state, PciCanarySchedule canary, Clock clock, Anchorer anchorer) {
        this(state, canary, clock, anchorer, (pass, leaf, index, collection, at, ring) -> false);
    }

    PciReindexPhase(PciReindexState state, PciCanarySchedule canary, Clock clock, Anchorer anchorer,
                    RedMarker redMarker) {
        this.state = Objects.requireNonNull(state, "state");
        this.canary = canary;
        this.clock = Objects.requireNonNull(clock, "clock");
        this.anchorer = Objects.requireNonNull(anchorer, "anchorer");
        this.redMarker = Objects.requireNonNull(redMarker, "redMarker");
    }

    void idCanaryWhen(java.util.function.BooleanSupplier latched) {
        this.idCanary = Objects.requireNonNull(latched, "latched");
    }

    /**
     * Retry every fresh anchor a finished REINDEX left unwritten, for the indexes the snapshot still lists: a count
     * timeout, the COMMENT's lock timeout or an ended pass can leave the old anchor in place, which would re-fire the
     * deletes rule every pass (the size rule would not: a fresh graph is no bigger per row than the old anchor's).
     * {@link PciReindexState#candidates} skips an owed index until this lands, and the pass whose retry lands.
     *
     * @return the names whose anchor was written now
     */
    Set<String> retryOwedAnchors(PciBuilderSession.Pass pass, Set<String> snapshotIndexes) {
        Set<String> written = new HashSet<>();
        for (Map.Entry<String, PciReindexState.Owed> e : state.owedAnchors().entrySet()) {
            if (!pass.live() || Thread.currentThread().isInterrupted()) {
                break;
            }
            PciReindexState.Owed owed = e.getValue();
            if (!snapshotIndexes.contains(e.getKey())) {
                continue;   // gone: the state forgets it with the snapshot
            }
            if (writeAnchor(pass, owed.leaf(), owed.dimension(),
                new Build(e.getKey(), owed.collection(), owed.collectionId()))) {
                state.anchorWritten(e.getKey());
                written.add(e.getKey());
                log.info("event=pci_anchor_retried index={} leaf={}", e.getKey(), PciReconciler.leafId(owed.leaf()));
            }
        }
        return written;
    }

    /** The anchor write; a runtime failure (a pass that ended between the liveness check and the transaction) is "not written". */
    private boolean writeAnchor(PciBuilderSession.Pass pass, Leaf leaf, int dimension, Build build) {
        try {
            return pass.live() && anchorer.write(pass, leaf, dimension, build, true);
        } catch (RuntimeException e) {
            log.warn("event=pci_anchor_not_written origin=built index={} leaf={} reason=error error={} "
                + "detail=\"the next pass retries it\"", build.name(), PciReconciler.leafId(leaf),
                PciQueryRing.sanitize(e));
            return false;
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
        // Red counts only when it met the floor that makes a red actionable: a red on fewer queries is shown, and is
        // neither a reason to REINDEX nor a reason to hold the 24 hour limit or red_after_reindex against the index.
        PciCanary.Result latest = canary == null ? null : canary.latest(name);
        boolean wasRed = latest != null && latest.state() == PciCanary.State.RED
            && latest.queries() >= PciReindexPolicy.RED_CANARY_MIN_QUERIES;
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
                Long id = subject.index().collectionId();
                boolean anchored = writeAnchor(pass, leaf, subject.dimension(), new Build(name, r.collection(), id));
                if (!anchored) {
                    state.anchorOwed(name, leaf, subject.dimension(), r.collection(), id);
                }
                if (r.vacuumAfter() && pass.live()) {
                    vacuumLeaf(pass, leaf);
                }
                // The canary measures the family the serving statement selects by: a name index before the latch, an id index once it holds.
                if (id == null || idCanary.getAsBoolean()) {
                    remeasure(pass, leaf, name, r, wasRed, id);
                }
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

    /** The second COMMENT: its own failure is logged as its own event and never charged to the canary. */
    private void markRedUnfixed(PciBuilderSession.Pass pass, Leaf leaf, String name, Reindex r, PciCanary.Result after) {
        try {
            if (after.redQueries() != null && pass.live()) {
                redMarker.mark(pass, leaf, name, r.collection(), clock.instant(),
                    PciQueryRing.queryIds(after.redQueries()));
            }
        } catch (RuntimeException e) {
            log.warn("event=pci_red_unfixed_failed index={} leaf={} error={} detail=\"the REINDEX and its anchor stand; "
                + "the next red-canary REINDEX records it\"", name, PciReconciler.leafId(leaf), PciQueryRing.sanitize(e));
        }
    }

    /**
     * The canary again on the REINDEXed index, with a red run's own queries when there are some (the same in-memory
     * queries, so before and after are comparable). When the canary was red at the start (on at least ten queries),
     * whatever the trigger's label, a re-measure that is still red is {@code red_after_reindex}: reported, and recorded in
     * the index's anchor as {@code red_unfixed} (a second COMMENT, after the fresh anchor the REINDEX wrote), which stops
     * the daily red-canary trigger while the index, the queries that read red and the collection's size stay as they
     * were. A later green run clears the flag and the field; the 24 hour floor, which holds the index's next red-canary
     * REINDEX whatever the canary reads (a REINDEX of a graph under a day old rebuilds essentially the same graph), is
     * independent of both.
     */
    private void remeasure(PciBuilderSession.Pass pass, Leaf leaf, String name, Reindex r, boolean wasRed, Long id) {
        if (canary == null) {
            return;
        }
        try {
            PciCanary.Result before = canary.latest(name);
            List<float[]> sample = before != null && before.state() == PciCanary.State.RED ? before.redSample() : null;
            var target = new PciCanary.Target(PciReconciler.leafId(leaf), leaf.tenant(), leaf.model(), r.collection(),
                name, id);
            PciCanary.Result after = canary.remeasure(target, sample);
            if (wasRed && after.state() == PciCanary.State.RED) {
                state.stillRed(name);
                log.warn("event=pci_red_after_reindex leaf={} index={} collection={} queries={} detail=\"still red "
                    + "after the REINDEX; no red-canary REINDEX while the index, the red queries and the collection's "
                    + "size are unchanged\"", PciReconciler.leafId(leaf), name, r.collection(), after.queries());
                markRedUnfixed(pass, leaf, name, r, after);
            }
        } catch (RuntimeException e) {
            log.warn("event=pci_canary_failed stage=after_reindex index={} error={}", name, PciQueryRing.sanitize(e));
        }
    }
}
