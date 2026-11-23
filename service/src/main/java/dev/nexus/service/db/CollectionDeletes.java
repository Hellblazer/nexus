// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Record2;
import org.jooq.Result;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * What a collection delete would do to the row it names, decided before it runs (RDR-229 P3.5a, bead nexus-p2m1x.23; the
 * delete contract agreed with bob for P3.12, bead comment 2026-11-22, with point 4 changed by linda's rulings of 2026-11-23: a
 * retired label is deleted, not refused; a retired id is refused 409 at the route boundary, D3). The delete route asks for a {@link Verdict} and acts only
 * on {@link Kind#PROCEED}.
 *
 * <p><b>The delete by id ({@code CatalogRepository#deleteCollectionById}, F11).</b> It purges what names the row's label, and
 * removes the registry row with the named id, so a label held by more than one live row loses only the row the operator named.
 * <i>The purge is label-scoped until gate 3:</i> before gate 3 every content table is purged by its name column
 * ({@link CollectionPurgeScope#byName}), so a label that two rows hold would lose the content of both and only the named registry
 * row; from gate 3 the content is purged by the named row's {@code collection_id}. The case needs two rows under one label, which
 * the registry's name keys and the live-label index forbid until P4.1, so before P4.1 the delete by id and the delete by label act
 * on the same single row; a test builds the state by dropping the keys as the superuser.
 * <i>It runs as two transactions:</i> the route asks {@link #verdict} first, one read transaction that decides (delete, unknown
 * id, ambiguous label), and then calls the repository, whose purge runs in its own transaction (retried whole on a deadlock). A
 * row can change between the two: an id deleted in the gap purges nothing and answers the 200 of an already-empty collection, and
 * a second live row registered under the label in the gap is not seen by the verdict. Neither removes a registry row the request
 * did not name, because the purge transaction deletes the registry row by the named id (and, from gate 3, scopes the purge by
 * it). Closing the gap would put the verdict inside the purge transaction; P4.1's live-label index makes that unnecessary for the
 * ambiguity, and nothing needs it for the id.
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
        UNKNOWN,
        /**
         * An id given with the request is the id of a row retired since the route boundary read it: the delete answers the same
         * 409 {@code collection_superseded} the boundary does (linda's D3), and deletes nothing.
         */
        SUPERSEDED
    }

    /**
     * @param kind         the decision
     * @param collectionId the id of the row the delete acts on; 0 when the label has no row
     * @param ids          every live row's id, for {@link Kind#AMBIGUOUS}
     * @param successor    for {@link Kind#SUPERSEDED}, the live row the tombstone names by {@code superseded_by_id}, else null
     */
    public record Verdict(Kind kind, long collectionId, long[] ids, CollectionSuccessor.Successor successor) {

        static Verdict of(Kind kind, long collectionId) {
            return new Verdict(kind, collectionId, new long[0], null);
        }
    }

    /**
     * @param resolvedId the id the route boundary resolved to {@code label}, or null when the request named the label
     */
    public static Verdict verdict(TenantScope scope, String tenant, String label, Long resolvedId) {
        return scope.withTenant(tenant, ctx -> verdict(ctx, tenant, label, resolvedId));
    }

    static Verdict verdict(DSLContext ctx, String tenant, String label, Long resolvedId) {
        // live is CollectionRegistry.notSuperseded(); the rows come live first, then the newest tombstone first (linda's corrected Q3)
        Result<Record2<Long, Integer>> rows = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID,
                DSL.when(CollectionRegistry.notSuperseded(), 1).otherwise(0))
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label))
            .orderBy(CollectionRegistry.labelResolutionOrder())
            .fetch();
        Record2<Long, Integer> row = null;
        if (resolvedId != null) {
            for (Record2<Long, Integer> r : rows) {
                if (r.value1().equals(resolvedId)) {
                    row = r;
                }
            }
            if (row == null) {
                return Verdict.of(Kind.UNKNOWN, resolvedId);
            }
            if (row.value2() == 0) {
                // retired between the route boundary's read and this one: the boundary's refusal, from the same facts
                return new Verdict(Kind.SUPERSEDED, resolvedId, new long[0],
                    CollectionSuccessor.of(ctx, tenant, resolvedId).orElse(null));
            }
        } else {
            List<Long> live = new ArrayList<>();
            for (Record2<Long, Integer> r : rows) {
                if (r.value2() == 1) {
                    live.add(r.value1());
                    row = r;
                }
            }
            if (live.size() > 1) {
                return new Verdict(Kind.AMBIGUOUS, 0L, live.stream().mapToLong(Long::longValue).toArray(), null);
            }
            if (row == null && !rows.isEmpty()) {
                row = rows.get(0);   // no live row: the first in resolution order is the newest tombstone
            }
            if (row == null) {
                return Verdict.of(Kind.PROCEED, 0L);
            }
        }
        // A retired LABEL is deleted like a live one (linda's ruling 2026-11-23, option (b)): the purge is scoped by this row's id
        // from gate 3 (CollectionPurgeScope), so a tombstone takes its own rows and never its successor's. With no live row
        // the row is the newest tombstone, as every other label read picks it. A retired ID never reaches here: the route
        // boundary answers it 409 collection_superseded (linda's D3).
        return Verdict.of(Kind.PROCEED, row.value1());
    }
}
