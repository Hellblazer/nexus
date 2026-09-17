// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.tuples.TemplateSchema;

/**
 * RDR-211 typed error (Scale and Limits item 2, "a runaway writer"): {@code
 * out} refused because the subspace already holds its template's {@code
 * max_live_rows} ceiling of live rows (RDR-211 defines "live" as {@link
 * TemplateSchema}'s own javadoc does — the same {@code consumed_at IS NULL
 * AND expires_at > now()} predicate the read path already uses). Never
 * raised for an idempotent refire of an existing identity ({@link
 * dev.nexus.service.tuples.TemplateSchema.IdFrom#KEYS}) — that path adds no
 * row, so it is exempt from the count (see {@code TupleRepository#writeOut}).
 *
 * <p>HTTP 429, the same status {@link ParkCapExceededException} uses: like a
 * full park queue, a full subspace is a capacity ceiling that clears over
 * time as existing rows are consumed or expire, not a permanent conflict
 * the caller must resolve by changing its request (which is what HTTP 409
 * would imply) — retrying later, after other rows leave the subspace, is
 * exactly the right response, so 429 ("too many requests"/over capacity)
 * fits this refusal better than 409 ("conflict").
 */
public final class MaxLiveRowsExceededException extends TupleException {

    private final String subspace;
    private final long maxLiveRows;

    public MaxLiveRowsExceededException(String subspace, long maxLiveRows) {
        super("MaxLiveRowsExceeded", 429,
                "subspace '" + subspace + "' already holds its max_live_rows ceiling of "
                        + maxLiveRows + " live rows; back off and retry once rows are consumed or expire");
        this.subspace = subspace;
        this.maxLiveRows = maxLiveRows;
    }

    public String subspace() {
        return subspace;
    }

    public long maxLiveRows() {
        return maxLiveRows;
    }
}
