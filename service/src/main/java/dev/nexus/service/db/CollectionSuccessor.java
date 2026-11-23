// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Record4;

import java.util.HashSet;
import java.util.Optional;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * The live successor of a retired row, found by id (RDR-229 P3.5a, bead nexus-p2m1x.23; the review of f041ecc0cb): what the 409
 * {@code collection_superseded} for a retired {@code collection_id} names. A tombstone's {@code superseded_by} is label TEXT, and a
 * label can be freed and taken again (a deleted successor, a re-registered name) or renamed on, so the text can name an unrelated
 * collection; {@code superseded_by_id} cannot. So the walk follows ids:
 * <ul>
 *   <li>a row whose {@code superseded_by_id} is its own id names no successor (the delete of a successor sets it, and the label
 *       text stays): none, even if another row now bears that text;</li>
 *   <li>a row whose id names another row is followed to the live end, a hop at a time, up to {@link #MAX_HOPS}; a cycle or a row
 *       with no row behind its id answers none, and so does a chain that ends at a self-referencing row;</li>
 *   <li>the successor is named by that row's id and its CURRENT label, read by id, never by the tombstone's text;</li>
 *   <li>a row with a NULL {@code superseded_by_id} (a row older than the stamp) falls back to the text: the live row that bears it.</li>
 * </ul>
 * "Live" is {@link CollectionRegistry#notSuperseded()}.
 */
public final class CollectionSuccessor {

    /** The most hops the walk follows before it answers none (a chain this long is a defect, not a history). */
    static final int MAX_HOPS = 32;

    private CollectionSuccessor() {
    }

    /** A live row named by a tombstone: its id and its current label. */
    public record Successor(long id, String label) {
    }

    /** {@link #of(DSLContext, String, long)} in its own short read transaction, for a caller with no open one. */
    public static Optional<Successor> of(TenantScope scope, String tenant, long retiredId) {
        return scope.withTenant(tenant, ctx -> of(ctx, tenant, retiredId));
    }

    /** The live successor of the retired row {@code retiredId}, or empty when it names none. */
    public static Optional<Successor> of(DSLContext ctx, String tenant, long retiredId) {
        Set<Long> seen = new HashSet<>();
        long current = retiredId;
        for (int hop = 0; hop <= MAX_HOPS; hop++) {
            if (!seen.add(current)) {
                return Optional.empty();   // a cycle
            }
            Record4<Long, String, String, Long> row = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.NAME,
                    CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_BY_ID)
                .from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(current))
                .fetchOne();
            if (row == null) {
                return Optional.empty();
            }
            String text = row.value3() == null ? "" : row.value3();
            Long next = row.value4();
            boolean live = text.isEmpty() && next == null;
            if (live) {
                return hop == 0 ? Optional.empty() : Optional.of(new Successor(row.value1(), row.value2()));
            }
            if (next == null) {
                return liveByText(ctx, tenant, text);
            }
            if (next == current) {
                return Optional.empty();   // names no successor
            }
            current = next;
        }
        return Optional.empty();
    }

    /** A row older than the stamp: the live row that bears the tombstone's text, the newest if several do. */
    private static Optional<Successor> liveByText(DSLContext ctx, String tenant, String label) {
        return ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label))
            .and(CollectionRegistry.notSuperseded())
            .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID.desc())
            .limit(1)
            .fetchOptional(r -> new Successor(r.get(CATALOG_COLLECTIONS.COLLECTION_ID), r.get(CATALOG_COLLECTIONS.NAME)));
    }
}
