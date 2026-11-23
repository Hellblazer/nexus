// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOBS;

/**
 * Which {@code reembed_jobs} rows hold a collection (RDR-229 P3.5a, bead nexus-p2m1x.23; linda's FK-less ledger rule): the
 * guard a rename or a re-home takes ({@link CatalogRepository#refuseUnderReembedJob}) matches a job end by the row's id, so a
 * job whose source was moved on by a one-row rename still holds it under its new label; or, for an end whose id no longer
 * names a live registry row (the row was deleted, retired or never stamped), by the label the job recorded.
 *
 * <p>Unconditional, not gated on gate 3: every registry row has an id from the start, so an id match is right in both regimes
 * and a label that has no row simply contributes no id.
 */
final class ReembedJobHold {

    private ReembedJobHold() {}

    /** The job rows (any state; the caller adds the lock-state filter) one of whose ends is the collection {@code from} or {@code to}. */
    static Condition holding(DSLContext ctx, String tenant, String from, String to) {
        List<Long> ids = new ArrayList<>(2);
        for (String label : new String[] {from, to}) {
            Long id = CollectionPurgeScope.idOfLabel(ctx, tenant, label);
            if (id != null && !ids.contains(id)) ids.add(id);
        }
        return end(REEMBED_JOBS.SOURCE_COLLECTION_ID, REEMBED_JOBS.SOURCE_COLLECTION, tenant, ids, from, to)
            .or(end(REEMBED_JOBS.TARGET_COLLECTION_ID, REEMBED_JOBS.TARGET_COLLECTION, tenant, ids, from, to));
    }

    /**
     * One job end against ONE collection, the match {@link #holding} makes: the job's stored id is the collection's id, or, for an end
     * whose stored id names no live registry row (never stamped, deleted, retired), the label the job recorded is the collection's.
     * The caller has the collection's row (locked) and passes its id and CURRENT label.
     */
    static Condition endIs(Field<Long> idColumn, Field<String> labelColumn, String tenant, long id, String label) {
        return end(idColumn, labelColumn, tenant, List.of(id), label, label);
    }

    private static Condition end(Field<Long> idColumn, Field<String> labelColumn, String tenant, List<Long> ids,
                                 String from, String to) {
        Condition byId = ids.isEmpty() ? DSL.falseCondition() : idColumn.in(ids);
        var live = CATALOG_COLLECTIONS.as("rjh");
        Condition idHasNoLiveRow = DSL.notExists(DSL.selectOne().from(live)
            .where(live.TENANT_ID.eq(tenant)).and(live.COLLECTION_ID.eq(idColumn)).and(live.SUPERSEDED_BY.eq("")));
        return byId.or(labelColumn.in(from, to).and(idHasNoLiveRow));
    }
}
