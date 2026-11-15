// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession;
import org.jooq.exception.DataAccessException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Duration;
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
}
