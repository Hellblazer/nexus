// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.List;

/**
 * The tables of {@code CatalogRepository.COLLECTION_SCOPED_TABLES} and their id columns, for a test outside this package (RDR-229 P3.7, nexus-p2m1x.27,
 * folio's r3 confirmation review S3): the registry is package-private, and a copy of its list in a test goes stale without a word.
 */
public final class CollectionScopedTableSites {

    /** One collection-scoped table: its name in schema {@code nexus} and the column that holds the registry's {@code collection_id}. */
    public record Site(String table, String idColumn) { }

    private CollectionScopedTableSites() { /* static */ }

    /** Every entry of {@code COLLECTION_SCOPED_TABLES}, in its order. */
    public static List<Site> all() {
        return CatalogRepository.COLLECTION_SCOPED_TABLES.stream()
            .map(t -> new Site(t.table().getName(), t.collectionId().getName())).toList();
    }
}
