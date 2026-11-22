// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession;
import dev.nexus.service.vectors.PciBuilderSession.DdlOutcome;
import dev.nexus.service.vectors.PciCatalog.Index;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import dev.nexus.service.vectors.PciReconciler.IndexReading;
import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.OptionalDouble;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.IntFunction;
import java.util.function.Predicate;

/**
 * What the DDL half of the reconciler knows of each index's anchor (RDR-230 Technical Design 2, nexus-4lnn1.1.1), moved
 * out of {@link PciReconciler} so that file stays under its size cap: the reads its counting transaction makes
 * ({@link #read}), the engine's memory of what it last read ({@link IndexReading}, the leaf statistics), and the
 * anchors the pass writes on what it found ({@link #write}).
 *
 * <p>An index's anchor is its PostgreSQL comment. A valid parsed index whose comment does not parse is <b>adopted</b>
 * (the current rows and bytes, the time as {@code built_at}, {@code origin=adopted}). One whose anchor has a
 * {@code relfilenode} that is not the index's now was <b>rebuilt</b> by someone else ({@code REINDEX CONCURRENTLY} keeps
 * the comment): it is anchored as built now (current rows and bytes, {@code built_at} now, {@code origin=built}) and
 * logged as {@code event=pci_anchor_rebuilt_detected}. One whose anchor has no relfilenode (written before the field
 * existed) is <b>stamped</b> once: the same {@code built_at}, rows, bytes and origin, plus the relfilenode now
 * ({@code event=pci_anchor_relfilenode_stamped}); a stamp is not a rebuild and moves no floor. An index whose REINDEX
 * finished and whose fresh anchor is still owed is neither rebuilt nor stamped: the engine did that rebuild, and the
 * retry writes the anchor.
 *
 * <p>Every write passes the relfilenode the read saw to {@link PciBuilderSession.Pass#write}, which refuses it when the
 * index has another one by the time of the COMMENT: figures counted on one build are never stamped on a later one.
 *
 * <p>Nothing here changes the engine's memory while the counting transaction runs: {@link #apply} puts what it read in
 * once the transaction has committed, so a rolled-back or timed-out one cannot move a baseline past deletes it never
 * counted. Logs under {@link PciReconciler}'s logger name, so one filter sees the whole story.
 */
final class PciAnchorLedger {

    private static final Logger log = LoggerFactory.getLogger(PciReconciler.class);

    /** Why an index is written: no parseable anchor, rebuilt by someone else, or an anchor with no relfilenode. */
    enum Kind { ADOPT, REBUILT, STAMP }

    /**
     * An index whose anchor the pass writes, and what to write.
     *
     * @param reason          {@code missing} or {@code unparsable} for an adoption, else the kind's lower-case name
     * @param anchor          the anchor read (a stamp writes it again with the relfilenode); null for the others
     * @param oldRelfilenode  the anchor's, for a rebuild
     * @param relfilenode     the index's when read: the write is refused if it has changed
     */
    record Write(Index index, Kind kind, String reason, long rows, long bytes, PciCatalog.Anchor anchor,
                 Long oldRelfilenode, Long relfilenode) { }

    /** What the counting transaction read; applied only once the transaction has committed. */
    record Reads(PciAnchorReads.LeafStats stats, Map<String, IndexReading> readings, List<Write> writes) {
        static final Reads NONE = new Reads(null, Map.of(), List.of());
    }

    private record Key(String leaf, String index) { }

    private final Clock clock;
    private final IntFunction<OptionalDouble> reference;
    private final Predicate<String> owed;
    /** Per leaf id, the statistics at the last committed look; per (leaf, index name), what that look read. */
    private final Map<String, PciAnchorReads.LeafStats> lastStats = new ConcurrentHashMap<>();
    private final Map<Key, IndexReading> readings = new ConcurrentHashMap<>();

    /**
     * @param reference the install reference (bytes per row) at a dimension, for the size bound of a recount
     * @param owed      whether an index name is one whose REINDEX finished and whose fresh anchor is not written yet
     */
    PciAnchorLedger(Clock clock, IntFunction<OptionalDouble> reference, Predicate<String> owed) {
        this.clock = clock;
        this.reference = reference;
        this.owed = owed;
    }

    /** What the last committed look read of {@code indexName} on the leaf with this id, or {@code null}. */
    IndexReading reading(String leafId, String indexName) {
        return readings.get(new Key(leafId, indexName));
    }

    /** The engine's reading of an index after a write of its anchor. */
    void put(String leafId, String indexName, IndexReading reading) {
        readings.put(new Key(leafId, indexName), reading);
    }

    /**
     * Inside the counting transaction: for each of {@code subjects} (the leaf's valid parsed indexes the plan does not
     * drop) its comment, relfilenode and size now; for the leaf, its statistics row. The row count and
     * {@code written_since} are taken only when {@link PciReconciler}'s class comment says a count could change a
     * verdict.
     */
    Reads read(DSLContext tx, Leaf leaf, int dim, List<Index> subjects, Instant now) {
        // A lock held on an index (a non-concurrent REINDEX or DROP) must not stall the leaf to its statement bound.
        PgSession.setLocal(tx, "lock_timeout", PciReconciler.SIZE_LOCK_TIMEOUT);
        String id = PciReconciler.leafId(leaf);
        PciAnchorReads.LeafStats stats = PciAnchorReads.leafStats(tx, leaf.schema(), leaf.name());
        PciAnchorReads.LeafStats before = lastStats.get(id);
        boolean moved = before == null || stats.deleted() != before.deleted()
            || stats.nonHotUpdated() != before.nonHotUpdated();
        Map<String, IndexReading> read = new LinkedHashMap<>();
        List<Write> writes = new ArrayList<>();
        for (Index index : subjects) {
            PciAnchorReads.IndexFacts facts = PciAnchorReads.indexFacts(tx, leaf.schema(), index.name());
            if (facts == null) {
                continue;   // dropped since the snapshot
            }
            Optional<PciCatalog.Anchor> anchor = PciCatalog.parseAnchor(facts.comment());
            if (anchor.isEmpty()) {
                long rows = rowsOf(tx, dim, leaf, index);
                writes.add(new Write(index, Kind.ADOPT, facts.comment() == null ? "missing" : "unparsable", rows,
                    facts.bytes(), null, null, null));
                continue;
            }
            PciCatalog.Anchor a = anchor.get();
            boolean mine = owed.test(index.name());   // the engine's own REINDEX; its anchor is owed
            if (!mine && a.rebuiltSince(facts.relfilenode())) {
                // Rebuilt by someone else (a hand REINDEX CONCURRENTLY keeps the comment): anchored as built now. It is
                // no reading this pass: the next one reads the new anchor.
                long rows = rowsOf(tx, dim, leaf, index);
                writes.add(new Write(index, Kind.REBUILT, "rebuilt", rows, facts.bytes(), null, a.relfilenode(),
                    facts.relfilenode()));
                continue;
            }
            if (!mine && a.relfilenode() == null && facts.relfilenode() != null && a.writable()) {
                writes.add(new Write(index, Kind.STAMP, "stamp", a.rows(), a.bytes(), a, null, facts.relfilenode()));
            }
            IndexReading last = readings.get(new Key(id, index.name()));
            // A size change with no delete is ingest: the true row count is at least the stale one, so bytes over the stale
            // count is an UPPER bound on bytes per row. The size rule divides bytes by rows, so the count is retaken only
            // when that bound reaches the trigger: ingest that cannot have made the index a bigger one per row costs no
            // count, and ingest that might have does (it reads as the index it is, never as two-fold growth).
            boolean count = moved || last == null || !last.anchor().sameFigures(a)
                || (last.bytes() != facts.bytes()
                    && (last.rows() <= 0 || PciReindexPolicy.sizeFires(a, facts.bytes(), last.rows(),
                        reference.apply(dim))))
                || Duration.between(last.countedAt(), now).compareTo(PciReconciler.RECOUNT_AFTER) > 0;
            if (count) {
                long rows = rowsOf(tx, dim, leaf, index);
                long written = index.idParsed()
                    ? PciAnchorReads.writtenSinceById(tx, leaf.model(), leaf.tenant(), index.collectionId(), a.builtAt())
                    : PciAnchorReads.writtenSince(tx, dim, leaf.model(), leaf.tenant(), index.collection(), a.builtAt());
                read.put(index.name(), new IndexReading(a, facts.bytes(), rows, written, now));
            } else {
                read.put(index.name(), new IndexReading(a, facts.bytes(), last.rows(), last.writtenSince(),
                    last.countedAt(), last.countedBytes()));
            }
        }
        return new Reads(stats, read, writes);
    }

    /** The rows the index's predicate selects on the leaf: by name, or (RDR-229 P3.4) by collection id. */
    private static long rowsOf(DSLContext tx, int dim, Leaf leaf, Index index) {
        return index.idParsed()
            ? PciAnchorReads.countRowsById(tx, leaf.model(), leaf.tenant(), index.collectionId())
            : PciAnchorReads.countRows(tx, dim, leaf.model(), leaf.tenant(), index.collection());
    }

    /** Make a committed transaction's reads the engine's last look at the leaf. */
    void apply(Leaf leaf, Reads reads) {
        String id = PciReconciler.leafId(leaf);
        if (reads.stats() != null) {
            lastStats.put(id, reads.stats());
        }
        readings.keySet().removeIf(k -> k.leaf().equals(id) && !reads.readings().containsKey(k.index()));
        reads.readings().forEach((name, reading) -> readings.put(new Key(id, name), reading));
    }

    /**
     * A red-canary REINDEX of {@code indexName} was re-measured still red: write its anchor again with
     * {@code red_unfixed} (the relfilenode of the fresh storage, the rows its fresh anchor counted, and {@code queries}, the
     * ids of the queries that read red), a second COMMENT after the one the REINDEX wrote. Nothing is written when the engine holds no anchor for the index or the index was rebuilt since.
     *
     * @return whether the field was written
     */
    boolean markRedUnfixed(PciBuilderSession.Pass pass, Leaf leaf, String indexName, String collection, Instant at,
                           java.util.List<String> queries) {
        String id = PciReconciler.leafId(leaf);
        IndexReading r = readings.get(new Key(id, indexName));
        if (r == null || r.anchor().relfilenode() == null) {
            return false;   // its fresh anchor is not written (owed): the retry writes it, and the next red marks it
        }
        var red = new PciCatalog.RedUnfixed(at, r.anchor().relfilenode(), r.anchor().rows(), queries);
        var written = pass.write(leaf, new Index(indexName, true, collection), r.anchor().withRedUnfixed(red),
            r.anchor().relfilenode());
        if (written.outcome() != DdlOutcome.DONE) {
            log.info("event=pci_red_unfixed_not_written index={} leaf={} outcome={}", indexName, id, written.outcome());
            return false;
        }
        readings.put(new Key(id, indexName), reading(r, written.anchor()));
        log.info("event=pci_red_unfixed_recorded index={} leaf={} relfilenode={} detail=\"still red after the REINDEX; "
            + "no red-canary REINDEX while the index, the red queries and the collection's size are unchanged\"",
            indexName, id, red.relfilenode());
        return true;
    }

    /**
     * A green canary run ends {@code red_unfixed}: write the anchor again without it (one COMMENT). Refused if the index
     * was rebuilt since the engine read it; a rebuild lifts the field on its own.
     *
     * @return whether the field was cleared
     */
    boolean clearRedUnfixed(PciBuilderSession.Pass pass, Leaf leaf, Index index) {
        String id = PciReconciler.leafId(leaf);
        IndexReading r = readings.get(new Key(id, index.name()));
        if (r == null || r.anchor().redUnfixed() == null) {
            return false;
        }
        var written = pass.write(leaf, index, r.anchor().withoutRedUnfixed(), r.anchor().relfilenode());
        if (written.outcome() != DdlOutcome.DONE) {
            log.info("event=pci_red_unfixed_not_cleared index={} leaf={} outcome={}", index.name(), id,
                written.outcome());
            return false;
        }
        readings.put(new Key(id, index.name()), reading(r, written.anchor()));
        log.info("event=pci_red_unfixed_cleared index={} leaf={} detail=\"a green canary run\"", index.name(), id);
        return true;
    }

    private static IndexReading reading(IndexReading r, PciCatalog.Anchor anchor) {
        return new IndexReading(anchor, r.bytes(), r.rows(), r.writtenSince(), r.countedAt(), r.countedBytes());
    }

    /** The statistics at the last committed look of the leaf with this id, or {@code null}. */
    PciAnchorReads.LeafStats lastStats(String leafId) {
        return lastStats.get(leafId);
    }

    /**
     * Write one anchor the read called for.
     *
     * @return whether it was written; a refusal or a failure leaves the old comment, and the next pass reads it again
     */
    boolean write(PciBuilderSession.Pass pass, Leaf leaf, Write w) {
        Index index = w.index();
        String id = PciReconciler.leafId(leaf);
        PciCatalog.Anchor anchor = switch (w.kind()) {
            case STAMP -> w.anchor();
            case REBUILT -> new PciCatalog.Anchor(clock.instant(), w.rows(), w.bytes(), PciCatalog.ORIGIN_BUILT);
            case ADOPT -> new PciCatalog.Anchor(clock.instant(), w.rows(), w.bytes(), PciCatalog.ORIGIN_ADOPTED);
        };
        if (w.kind() == Kind.REBUILT) {
            log.info("event=pci_anchor_rebuilt_detected index={} leaf={} old_relfilenode={} new_relfilenode={} rows={} "
                + "bytes={}", index.name(), id, w.oldRelfilenode(), w.relfilenode(), w.rows(), w.bytes());
        }
        var written = pass.write(leaf, index, anchor, w.relfilenode());
        if (written.outcome() != DdlOutcome.DONE) {
            log.info("event=pci_anchor_not_written origin={} index={} leaf={} outcome={} detail=\"the next pass reads it "
                + "again\"", anchor.origin(), index.name(), id, written.outcome());
            return false;
        }
        switch (w.kind()) {
            case STAMP -> {
                // The figures are the ones the reading holds; only the relfilenode is new.
                readings.computeIfPresent(new Key(id, index.name()), (k, r) -> reading(r, written.anchor()));
                log.info("event=pci_anchor_relfilenode_stamped index={} leaf={} relfilenode={}", index.name(), id,
                    written.anchor().relfilenode());
            }
            case REBUILT -> readings.put(new Key(id, index.name()),
                new IndexReading(written.anchor(), w.bytes(), w.rows(), 0, written.anchor().builtAt()));
            case ADOPT -> {
                readings.put(new Key(id, index.name()),
                    new IndexReading(written.anchor(), w.bytes(), w.rows(), 0, written.anchor().builtAt()));
                log.info("event=pci_anchor_adopted reason={} index={} leaf={} rows={} bytes={}", w.reason(),
                    index.name(), id, w.rows(), w.bytes());
            }
        }
        return true;
    }
}
