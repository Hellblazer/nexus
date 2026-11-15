// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession;
import org.jooq.exception.DataAccessException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Collection;
import java.util.Map;

/**
 * The reads the leaf size rule makes on the REINDEX phase's pass session (RDR-230 Technical Design 3), apart from the
 * shared catalog read: that read locks nothing ({@link PciCatalog.LeafIndex}), and these do.
 *
 * <ul>
 *   <li>{@link #rowEstimates}: one statement for every counted leaf, catalog and statistics views only.</li>
 *   <li>{@link #facts}: the size ({@code pg_relation_size} takes an {@code AccessShareLock} on the index) and the anchor
 *       comment of one index, in a transaction of its own under a {@value #LOCK_TIMEOUT} {@code lock_timeout}, so a lock
 *       held on one index (a migration's {@code AccessExclusiveLock}) ends that read alone, with a log line, and the
 *       rest of the pass goes on. A failed statement aborts its transaction, which is why it is one per index.</li>
 * </ul>
 */
final class PciLeafIndexReads {

    private static final Logger log = LoggerFactory.getLogger(PciLeafIndexReads.class);

    /** The anchor's events are the reconciler's story, under its logger name, as {@link PciReindexPhase}'s are. */
    private static final Logger anchorLog = LoggerFactory.getLogger(PciReconciler.class);

    /** How long a read of one leaf index waits for a lock held on it. */
    static final String LOCK_TIMEOUT = "5s";

    private static final String LOCK_NOT_AVAILABLE = "55P03";

    private PciLeafIndexReads() { }

    /**
     * The row estimates of the tables in {@code schemas} ({@link PciAnchorReads#leafRowEstimates}), or {@code null}
     * when the read failed or the pass is over (logged): the caller then leaves the pass's candidates as they were.
     */
    static Map<String, Long> rowEstimates(PciBuilderSession.Pass pass, Collection<String> schemas,
                                          Duration statementTimeout) {
        try {
            return pass.transaction(tx -> {
                PgSession.setLocal(tx, "statement_timeout", Long.toString(statementTimeout.toMillis()));
                return PciAnchorReads.leafRowEstimates(tx, schemas);
            });
        } catch (DataAccessException | IllegalStateException e) {
            log.warn("event=pci_leaf_size_rule_skipped reason=stats_read_failed error=\"{}\"", e.getMessage());
            return null;
        }
    }

    /**
     * The size and the comment (the anchor) of {@code own} on {@code leaf}, or {@code null} when it cannot be read now:
     * a lock held on the index (logged as {@code index_lock_timeout}), any other failure, an index that is gone, or a
     * pass that is over. Nothing is retried; the next pass judges the index again.
     */
    static PciAnchorReads.IndexFacts facts(PciBuilderSession.Pass pass, PciCatalog.Leaf leaf,
                                           PciCatalog.LeafIndex own, Duration statementTimeout) {
        if (!pass.live()) {
            return null;
        }
        try {
            return pass.transaction(tx -> {
                PgSession.setLocal(tx, "statement_timeout", Long.toString(statementTimeout.toMillis()));
                PgSession.setLocal(tx, "lock_timeout", LOCK_TIMEOUT);
                return PciAnchorReads.indexFacts(tx, leaf.schema(), own.name());
            });
        } catch (DataAccessException e) {
            log.warn("event=pci_leaf_size_rule_skipped reason={} index={} leaf={}.{} sqlstate={} detail=\"the next "
                + "pass judges it again\"", LOCK_NOT_AVAILABLE.equals(e.sqlState()) ? "index_lock_timeout"
                : "index_read_failed", own.name(), leaf.schema(), leaf.name(), e.sqlState());
            return null;
        } catch (IllegalStateException e) {
            return null;   // the pass is over
        }
    }

    /**
     * The fresh anchor of a leaf's own HNSW index right after its REINDEX (RDR-230 Technical Design 3): the live rows are
     * read (the leaf's {@code n_live_tup}, {@code built_at} taken before it, as for a build) and the anchor is written
     * through {@link PciBuilderSession.Pass#anchorAfterLeafReindex}. {@code leaf} is the snapshot the REINDEX ran on, and
     * {@code indexName} must be one of its {@code hnswIndexes()}.
     *
     * <p>Anything that goes wrong, an outcome other than DONE or any {@link RuntimeException} (a statement failure, a
     * pass that is over, the size read inside the write), is logged and answers {@code false}: the index stands with its
     * old comment, and the caller owes it the anchor ({@link PciReindexState#anchorOwed}), because the 24 h LEAF_SIZE
     * cooldown reads that anchor and a rule that stays true on a wrong estimate would rebuild the index every pass.
     *
     * @return whether the anchor was written
     */
    static boolean anchorAfterReindex(PciBuilderSession.Pass pass, PciCatalog.Leaf leaf, String indexName, Clock clock,
                                      Duration statementTimeout) {
        String where = leaf.schema() + "." + leaf.name();
        PciCatalog.LeafIndex own = leaf.hnswIndexes().stream().filter(i -> i.name().equals(indexName)).findFirst()
            .orElse(null);
        if (own == null) {
            anchorLog.warn("event=pci_anchor_not_written origin=built index={} leaf={} reason=not_the_leafs_own_index",
                indexName, where);
            return false;
        }
        try {
            Instant builtAt = clock.instant();
            long rows = pass.transaction(tx -> {
                PgSession.setLocal(tx, "statement_timeout", Long.toString(statementTimeout.toMillis()));
                return PciAnchorReads.leafStats(tx, leaf.schema(), leaf.name()).live();
            });
            PciBuilderSession.Pass.AnchorOutcome anchored = pass.anchorAfterLeafReindex(leaf, own, rows, builtAt);
            if (anchored.outcome() == PciBuilderSession.DdlOutcome.DONE) {
                anchorLog.info("event=pci_anchor_written origin=built index={} leaf={} rows={} bytes={}", indexName,
                    where, anchored.anchor().rows(), anchored.anchor().bytes());
                return true;
            }
            anchorLog.info("event=pci_anchor_not_written origin=built index={} leaf={} outcome={} detail=\"the index "
                + "stands; the next pass retries it\"", indexName, where, anchored.outcome());
        } catch (RuntimeException e) {
            anchorLog.warn("event=pci_anchor_not_written origin=built index={} leaf={} reason=write_failed error=\"{}\" "
                + "detail=\"the index stands; the next pass retries it\"", indexName, where, PciQueryRing.sanitize(e));
        }
        return false;
    }
}
