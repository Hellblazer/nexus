// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/**
 * The registry check of a re-embed job that leaves {@code done} (RDR-229 P3.5a row 14, nexus-p2m1x.23). A done job holds no lock
 * state, so nothing stopped its target from being deleted and its label registered again, or renamed one-row, while it sat there;
 * a re-entry that ran on the stored label would then move documents into whatever collection bears it. The ends are read here
 * {@code FOR KEY SHARE} by the ids the job stored, in id order after the start guard, as {@code start} reads its two.
 */
final class ReembedJobEnds {

    private ReembedJobEnds() {}

    /** The rows of a job's two ends, as locked. */
    record Ends(CollectionRow source, CollectionRow target) {}

    /**
     * Locks the source's and the target's registry rows by their stored ids (by label for an end that never stored one) and checks them
     * for a re-entry: a re-entered job keeps a source the cutover superseded (its row stays), so the source may be retired; both
     * rows must exist, and the target must be live.
     *
     * @throws ReembedJobRepository.EndRefused when an end's row is gone, or the target is retired
     */
    static Ends lockForReentry(DSLContext ctx, String tenant, UUID jobId, Long sourceId, Long targetId, String sourceLabel,
                               String targetLabel) {
        List<Long> ids = new ArrayList<>(2);
        List<String> fallback = new ArrayList<>(2);
        for (Object[] end : new Object[][] {{sourceId, sourceLabel}, {targetId, targetLabel}}) {
            if (end[0] != null) {
                ids.add((Long) end[0]);
            } else {
                fallback.add((String) end[1]);
            }
        }
        List<CollectionRow> rows = RegistryRowLocks.keyShareIfPresent(ctx, tenant, ids, fallback);
        CollectionRow source = endRow(rows, sourceId, sourceLabel);
        CollectionRow target = endRow(rows, targetId, targetLabel);
        if (source == null) {
            throw ReembedJobRepository.EndRefused.of(jobId, "its source " + sourceLabel + " is no longer registered");
        }
        if (target == null) {
            throw ReembedJobRepository.EndRefused.of(jobId, "its target " + targetLabel + " is no longer registered");
        }
        if (target.isSuperseded()) {
            throw ReembedJobRepository.EndRefused.of(jobId, "its target " + target.label() + " is itself superseded by "
                + target.supersededBy());
        }
        return new Ends(source, target);
    }

    /** The end's row: the one of its stored id, or, for an end with no stored id, the live row of its label (the newest retired one when none is live). */
    private static CollectionRow endRow(List<CollectionRow> rows, Long id, String label) {
        CollectionRow chosen = null;
        for (CollectionRow row : rows) {
            if (id != null ? row.collectionId() == id : row.label().equals(label)) {
                if (chosen == null || (chosen.isSuperseded() && !row.isSuperseded())
                    || (chosen.isSuperseded() == row.isSuperseded() && row.collectionId() > chosen.collectionId())) {
                    chosen = row;
                }
            }
        }
        return chosen;
    }
}
