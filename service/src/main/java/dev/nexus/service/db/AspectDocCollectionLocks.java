// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;

import java.util.LinkedHashSet;
import java.util.Set;
import java.util.TreeSet;

import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23, row 14): the registry check of a write that names an aspect or highlight row by {@code doc_id} alone. The
 * row's collection is whatever the row says it is, so the writer reads the collections of the doc's rows {@code FOR KEY SHARE} in its own
 * write transaction before it writes, by each row's STORED {@code collection_id} (an unstamped row, by its label), in one {@code
 * collection_id}-ordered statement (the order a retiring verb locks in). The write then touches only the rows of the collections it
 * holds: a row a verb re-homed while the writer waited is not written, and the read repeats until the doc's rows are all held.
 *
 * <p>The read is TOLERANT ({@link RegistryRowLocks#keyShareIfPresent}): a row stamped to a collection that is retired or gone is locked if
 * it is there and the write goes ahead, so these routes answer what they answered before the read existed (supersede does not re-home
 * children, so such rows persist; a 409 or 404 here would be a new refusal on a released route).
 */
final class AspectDocCollectionLocks {

    /** Rounds of read-lock-reread before the write goes ahead restricted to the rows held (a re-home storm cannot spin it). */
    private static final int MAX_ROUNDS = 4;

    private AspectDocCollectionLocks() {}

    /** The collections held: stored ids and, for rows not stamped yet, labels. */
    record Held(Set<Long> ids, Set<String> labels) {
        /** The rows of the held collections: by stored id, or an unstamped row by its label. */
        Condition owns(Field<Long> idColumn, Field<String> collectionColumn) {
            return idColumn.in(ids).or(idColumn.isNull().and(collectionColumn.in(labels)));
        }

        boolean isEmpty() {
            return ids.isEmpty() && labels.isEmpty();
        }
    }

    static Held requireForWriteDocAspects(DSLContext ctx, String tenant, String docId) {
        return lockCollectionsOfDoc(ctx, tenant, DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.COLLECTION_ID,
            DOCUMENT_ASPECTS.COLLECTION, docId);
    }

    static Held requireForWriteDocHighlight(DSLContext ctx, String tenant, String docId) {
        return lockCollectionsOfDoc(ctx, tenant, DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.DOC_ID, DOCUMENT_HIGHLIGHTS.COLLECTION_ID,
            DOCUMENT_HIGHLIGHTS.COLLECTION, docId);
    }

    private static Held lockCollectionsOfDoc(DSLContext ctx, String tenant, Table<?> table, Field<String> docIdColumn,
                                     Field<Long> idColumn, Field<String> collectionColumn, String docId) {
        Set<Long> ids = new TreeSet<>();
        Set<String> labels = new LinkedHashSet<>();
        for (int round = 0; round < MAX_ROUNDS; round++) {
            boolean grew = false;
            for (var row : ctx.selectDistinct(idColumn, collectionColumn).from(table).where(docIdColumn.eq(docId)).fetch()) {
                Long id = row.get(idColumn);
                grew |= id != null ? ids.add(id) : labels.add(row.get(collectionColumn));
            }
            if (!grew) {
                break;   // every collection the doc's rows are in is held
            }
            RegistryRowLocks.keyShareIfPresent(ctx, tenant, ids, labels);   // tolerant: a row stamped to a retired or gone collection is still written
        }
        return new Held(ids, labels);
    }
}
