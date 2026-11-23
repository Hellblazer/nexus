// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.Field;

import java.util.Optional;

/**
 * The rows of one collection, for a write that deletes or updates them by the collection the request named (RDR-229 P3.5a, linda's
 * ruling: the id is the identity). When the write's transaction has read the collection's registry row, the rows are the ones that
 * carry its {@code collection_id}, whatever their name column says (a one-row rename leaves child name columns at the old label, and the
 * old label may by now be another collection's); a row the P3.1 stamp has not reached yet (no id) is matched by the row's current
 * label. When no row exists (a label no row holds) the name reaches ONLY rows the stamp has not reached: a stamped row carries the id
 * of a collection whatever its name column says, and a label no row holds may be the OLD name of a collection that was renamed, whose
 * rows must not be touched by an operation that names the old label.
 */
final class CollectionOwnedRows {

    private CollectionOwnedRows() {
    }

    static Condition of(Optional<CollectionRow> row, Field<Long> idColumn, Field<String> nameColumn, String asked) {
        if (row.isEmpty()) {
            return idColumn.isNull().and(nameColumn.eq(asked));
        }
        return idColumn.eq(row.get().collectionId()).or(idColumn.isNull().and(nameColumn.eq(row.get().label())));
    }
}
