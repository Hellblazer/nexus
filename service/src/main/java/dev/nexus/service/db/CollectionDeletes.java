// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Record2;
import org.jooq.Result;

import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * What a collection delete would do to the row it names, decided before it runs (RDR-229 P3.5a, bead nexus-p2m1x.23; the
 * delete contract agreed with bob for P3.12, bead comment 2026-11-22, with point 4 changed by linda's ruling of 2026-11-23: a
 * retired label or id is deleted, not refused). The delete route asks for a {@link Verdict} and acts only
 * on {@link Kind#PROCEED}.
 *
 * <p>A label is read by its rows, never through {@link CollectionRegistry#require}, which would raise on a label that two live rows
 * hold; an id is read as the row of that id and that label together, so an id that does not belong to the label the request
 * names is an unknown id and cannot delete another row's collection.
 */
public final class CollectionDeletes {

    private CollectionDeletes() {
    }

    /** What the route does. */
    public enum Kind {
        /** Delete: the row (or no row, for a label nothing registered, which deletes what names it as before). */
        PROCEED,
        /** A label held by more than one live row: refuse, naming every id. Only an id reaches one of them. */
        AMBIGUOUS,
        /** An id given with the request has no row under the label the request names. */
        UNKNOWN
    }

    /**
     * @param kind         the decision
     * @param collectionId the id of the row the delete acts on; 0 when the label has no row
     * @param ids          every live row's id, for {@link Kind#AMBIGUOUS}
     */
    public record Verdict(Kind kind, long collectionId, long[] ids) {

        static Verdict of(Kind kind, long collectionId) {
            return new Verdict(kind, collectionId, new long[0]);
        }
    }

    /**
     * @param resolvedId the id the route boundary resolved to {@code label}, or null when the request named the label
     */
    public static Verdict verdict(TenantScope scope, String tenant, String label, Long resolvedId) {
        return scope.withTenant(tenant, ctx -> verdict(ctx, tenant, label, resolvedId));
    }

    static Verdict verdict(DSLContext ctx, String tenant, String label, Long resolvedId) {
        Result<Record2<Long, String>> rows = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label))
            .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID)
            .fetch();
        Record2<Long, String> row = null;
        if (resolvedId != null) {
            for (Record2<Long, String> r : rows) {
                if (r.value1().equals(resolvedId)) {
                    row = r;
                }
            }
            if (row == null) {
                return Verdict.of(Kind.UNKNOWN, resolvedId);
            }
        } else {
            List<Long> live = new ArrayList<>();
            for (Record2<Long, String> r : rows) {
                if (r.value2() == null || r.value2().isEmpty()) {
                    live.add(r.value1());
                    row = r;
                }
            }
            if (live.size() > 1) {
                return new Verdict(Kind.AMBIGUOUS, 0L, live.stream().mapToLong(Long::longValue).toArray());
            }
            if (row == null && !rows.isEmpty()) {
                row = rows.get(0);
            }
            if (row == null) {
                return Verdict.of(Kind.PROCEED, 0L);
            }
        }
        // A retired row is deleted like a live one (linda's ruling 2026-11-23, option (b)): the purge is scoped by this row's id
        // from gate 3 (CollectionPurgeScope), so a tombstone takes its own rows and never its successor's.
        return Verdict.of(Kind.PROCEED, row.value1());
    }
}
