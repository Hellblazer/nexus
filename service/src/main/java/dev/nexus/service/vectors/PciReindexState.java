// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PciCatalog.Index;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import dev.nexus.service.vectors.PciReconciler.IndexReading;
import dev.nexus.service.vectors.PciReconcilePlanner.Trigger;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.OptionalDouble;
import java.util.OptionalLong;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;

/**
 * What the REINDEX half of the reconciler remembers (RDR-230 Technical Design 3), and the assembly of its candidates.
 * All of it is in memory, per engine, and lost on restart, like the build backoff it mirrors: the rules that need a
 * previous value (bulk delete) have none after a boot and the rules that read the anchor (deletes since build, size) do
 * not need one. Keyed by index name, which is unique install-wide ({@link PciCatalog#indexName} hashes the model, tenant
 * and collection).
 *
 * <p>Held per index: the row count at the previous check (the bulk-delete rule), whether a bulk delete is still owed its
 * REINDEX (the per-pass limit can defer one, and the next check would see no fall), when the last red-canary REINDEX
 * finished (its once-a-day limit), whether its re-measure was still red ({@code red_after_reindex}), and the
 * failure backoff, which is RDR-227's ({@link PciReconciler#retryDelay}). Held for the engine: the number of REINDEXes
 * finished, when the last did, and the name of the one in flight.
 *
 * <p>Called from the reconciler's pass thread; the readers behind the status route ({@link #reindexes},
 * {@link #lastReindexAt}, {@link #reindexing}) are safe from any thread.
 */
final class PciReindexState {

    private record Failure(int consecutive, Instant nextRetryAt) { }

    /** One leaf's look this pass: the readings its counting transaction took, by index name. */
    record Look(Leaf leaf, int dimension, Map<String, IndexReading> readings) { }

    /** An index a REINDEX may be run on, with what the executor needs to run it. */
    record Subject(Leaf leaf, int dimension, Index index, IndexReading reading) { }

    /** A REINDEX that finished whose fresh anchor was not written: what the retry needs to write it. */
    record Owed(Leaf leaf, int dimension, String collection, Long collectionId) {
        Owed(Leaf leaf, int dimension, String collection) {
            this(leaf, dimension, collection, null);
        }
    }

    private final Map<String, Long> previousRows = new ConcurrentHashMap<>();
    private final Set<String> bulkOwed = ConcurrentHashMap.newKeySet();
    private final Map<String, Instant> lastRedReindex = new ConcurrentHashMap<>();
    private final Set<String> redAfterReindex = ConcurrentHashMap.newKeySet();
    private final Map<String, Failure> failures = new ConcurrentHashMap<>();
    private final Map<String, Owed> anchorOwed = new ConcurrentHashMap<>();
    private final AtomicInteger reindexes = new AtomicInteger();
    private final AtomicReference<String> reindexing = new AtomicReference<>();
    private volatile Instant lastReindexAt;
    /** The install reference per dimension as of the last {@link #candidates}: what a size bound is read against. */
    private volatile Map<Integer, OptionalDouble> references = Map.of();

    /**
     * The candidates of this pass: every valid parsed index the looks read an anchor for that the pass did not drop. The
     * bookkeeping reads (previous count, owed bulk delete, last red REINDEX, backoff) are taken before this look's row
     * count replaces the previous one. {@code subjects} receives each candidate's index by name, for the executor.
     *
     * @param snapshotIndexes every index name of the pass's catalog snapshot: the history of any other index is forgotten.
     *                        A leaf the pass skipped (a count timeout) has no look, and keeps its history.
     * @param canaryLatest the canary's latest result for an index name on this engine, or null
     */
    List<PciReindexPolicy.Candidate> candidates(List<Look> looks, Set<String> dropped, Set<String> snapshotIndexes,
                                                Function<String, PciCanary.Result> canaryLatest, Instant now,
                                                Map<String, Subject> subjects) {
        return candidates(looks, dropped, snapshotIndexes, Set.of(), canaryLatest, now, subjects);
    }

    /**
     * As above, and {@code justAnchored}: indexes whose owed anchor was written at the start of this pass. The looks
     * were taken before that write and still carry the old anchor, which would fire the deletes rule once more, so
     * they are no candidates until the next pass reads the new one.
     */
    List<PciReindexPolicy.Candidate> candidates(List<Look> looks, Set<String> dropped, Set<String> snapshotIndexes,
                                                Set<String> justAnchored,
                                                Function<String, PciCanary.Result> canaryLatest, Instant now,
                                                Map<String, Subject> subjects) {
        Map<Integer, List<PciCatalog.Anchor>> anchorsByDimension = new TreeMap<>();
        List<Subject> read = new ArrayList<>();
        for (Look look : looks) {
            for (Index index : look.leaf().indexes()) {
                IndexReading reading = look.readings().get(index.name());
                // RDR-229 P3.4: an id-predicate index has the same lifecycle as a name one (its anchor, its REINDEX rules).
                if (reading == null || !(index.parsed() || index.idParsed()) || !index.valid()
                    || dropped.contains(index.name())) {
                    continue;
                }
                read.add(new Subject(look.leaf(), look.dimension(), index, reading));
                anchorsByDimension.computeIfAbsent(look.dimension(), d -> new ArrayList<>()).add(reading.anchor());
            }
        }
        Map<Integer, OptionalDouble> references = new TreeMap<>();
        anchorsByDimension.forEach((dim, anchors) -> references.put(dim, PciReindexPolicy.installReference(dim, anchors)));
        this.references = references;
        List<PciReindexPolicy.Candidate> out = new ArrayList<>();
        for (Subject s : read) {
            String name = s.index().name();
            subjects.put(name, s);
            PciCanary.Result result = canaryLatest.apply(name);
            if (result != null && result.state() == PciCanary.State.GREEN) {
                redAfterReindex.remove(name);   // a later green run ends red_after_reindex
            }
            IndexReading reading = s.reading();
            // A fall seen while the index is backed off, or owed an anchor, must not be lost with the overwrite below:
            // it stays owed until its REINDEX has run.
            if (PciReindexPolicy.bulkFall(previousRows(name), reading.rows())) {
                bulkOwed.add(name);
            }
            if (anchorOwed.containsKey(name) || justAnchored.contains(name)) {
                // Its REINDEX finished and the fresh anchor is not written: the old anchor would re-fire the size and
                // deletes rules every pass. Not a candidate until the anchor lands.
                previousRows.put(name, reading.rows());
                continue;
            }
            out.add(new PciReindexPolicy.Candidate(name, collectionLabel(s.index()), s.dimension(), reading.anchor(),
                reading.bytes(), reading.rows(),
                reading.writtenSince(), previousRows(name), bulkOwed.contains(name),
                result == null ? null : result.state(), result == null ? 0 : result.queries(),
                lastRedReindex.get(name), backedOff(name, now),
                PciReindexPolicy.installReference(s.dimension(), anchorsByDimension.get(s.dimension())),
                result != null && result.state() == PciCanary.State.RED && result.redQueries() != null
                    ? PciQueryRing.queryIds(result.redQueries()) : null));
            previousRows.put(name, reading.rows());
        }
        forgetAllBut(snapshotIndexes);
        return out;
    }

    /** Forget every index not in the snapshot: a dropped or renamed index has no history to keep. */
    private void forgetAllBut(Set<String> present) {
        previousRows.keySet().retainAll(present);
        bulkOwed.retainAll(present);
        lastRedReindex.keySet().retainAll(present);
        redAfterReindex.retainAll(present);
        failures.keySet().retainAll(present);
        anchorOwed.keySet().retainAll(present);
    }

    /**
     * What a REINDEX names as the index's collection: the collection of a name index, and for an id index (RDR-229 P3.4, which knows no
     * name) {@code "#id:<id>"}. Never null, so it is never read as a leaf's own HNSW index.
     */
    static String collectionLabel(Index index) {
        return index.idParsed() ? PciReconcilePlanner.idBackoffKey(index.collectionId()) : index.collection();
    }

    /** The REINDEX of {@code index} finished and its fresh anchor was not written: retry it before judging the index. */
    void anchorOwed(String index, Leaf leaf, int dimension, String collection) {
        anchorOwed.put(index, new Owed(leaf, dimension, collection));
    }

    /** As above for an id-predicate index. */
    void anchorOwed(String index, Leaf leaf, int dimension, String collection, Long collectionId) {
        anchorOwed.put(index, new Owed(leaf, dimension, collection, collectionId));
    }

    /** The anchors still owed, by index name. */
    Map<String, Owed> owedAnchors() {
        return Map.copyOf(anchorOwed);
    }

    /** The owed anchor of {@code index} was written. */
    void anchorWritten(String index) {
        anchorOwed.remove(index);
    }

    /**
     * The install reference for {@code dimension} as of the last pass, or the built-in one when none has been taken (a
     * pass that has not reached its REINDEX phase yet, a boot): {@link PciReindexPolicy#installReference}.
     */
    OptionalDouble reference(int dimension) {
        OptionalDouble known = references.get(dimension);
        return known != null ? known : PciReindexPolicy.installReference(dimension, List.of());
    }

    OptionalLong previousRows(String index) {
        Long v = previousRows.get(index);
        return v == null ? OptionalLong.empty() : OptionalLong.of(v);
    }

    /**
     * {@code index} was rebuilt by someone else and re-anchored at {@code rows} (nexus-4lnn1.1.10): its row count is the
     * new baseline, so the next look does not read the fall since the old build as a bulk delete, and a bulk delete still
     * owed its REINDEX is no longer owed: the rebuild did that work.
     */
    void rebaseline(String index, long rows) {
        previousRows.put(index, rows);
        bulkOwed.remove(index);   // a fall seen before the rebuild (while backed off, or past the limit) is moot: it is rebuilt
    }

    /** A bulk delete was chosen for {@code index}; it stays owed until its REINDEX has run. */
    void bulkDeleteOwed(String index) {
        bulkOwed.add(index);
    }

    boolean backedOff(String index, Instant now) {
        Failure f = failures.get(index);
        return f != null && f.nextRetryAt().isAfter(now);
    }

    /** A REINDEX of {@code index} failed: the RDR-227 build backoff, per index. Returns the consecutive failures. */
    int failed(String index, Instant now) {
        return failures.compute(index, (k, old) -> {
            int n = old == null ? 1 : old.consecutive() + 1;
            return new Failure(n, now.plus(PciReconciler.retryDelay(n)));
        }).consecutive();
    }

    Instant nextRetryAt(String index) {
        Failure f = failures.get(index);
        return f == null ? null : f.nextRetryAt();
    }

    /** A REINDEX of {@code index} finished for {@code trigger}, with no red canary behind it (a leaf index). */
    void finished(String index, Trigger trigger, Instant now) {
        finished(index, trigger, now, false);
    }

    /**
     * A REINDEX of {@code index} finished for {@code trigger}. {@code canaryWasRed}: the canary's latest result for the
     * index was red when it started, whatever the trigger's label (a bulk delete that fires with a red canary is labelled
     * bulk delete, and the red's once-a-day limit and {@code red_after_reindex} still apply).
     */
    void finished(String index, Trigger trigger, Instant now, boolean canaryWasRed) {
        failures.remove(index);
        bulkOwed.remove(index);
        redAfterReindex.remove(index);   // a new REINDEX is a new measurement; stillRed sets it again
        if (canaryWasRed || trigger == Trigger.RED_CANARY) {
            lastRedReindex.put(index, now);
        }
        reindexes.incrementAndGet();
        lastReindexAt = now;
    }

    /** The re-measure after a red-canary REINDEX of {@code index} was still red. */
    void stillRed(String index) {
        redAfterReindex.add(index);
    }

    /** Whether the index's last red-canary REINDEX re-measured red, and neither a green run nor a day has passed. */
    boolean redAfterReindex(String index, Instant now) {
        Instant last = lastRedReindex.get(index);
        return redAfterReindex.contains(index) && last != null
            && now.isBefore(last.plus(PciReindexPolicy.RED_REINDEX_MIN_INTERVAL));
    }

    /** The index whose REINDEX is running now, or null. */
    void running(String index) {
        reindexing.set(index);
    }

    int reindexes() {
        return reindexes.get();
    }

    Instant lastReindexAt() {
        return lastReindexAt;
    }

    String reindexing() {
        return reindexing.get();
    }
}
