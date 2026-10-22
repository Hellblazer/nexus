// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.CatalogRepository.CollectionScopedTable;
import dev.nexus.service.db.CatalogRepository.CrossModelDisposition;
import dev.nexus.service.jooq.nexus.Nexus;
import org.jooq.Field;
import org.jooq.Table;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Phase 6 Step 1 (bead nexus-0khi2): every table in {@link CatalogRepository#COLLECTION_SCOPED_TABLES}
 * has a cross-model disposition ({@link CatalogRepository#CROSS_MODEL_DISPOSITIONS}), so a new
 * collection-scoped table cannot reach a re-embed with "REPOINT, REBUILD or LEAVE?" unanswered.
 *
 * <p>What this gates, and what it does not. It gates the registry against the disposition list, the same
 * shape as the schema-parity gate ({@code tests/catalog/test_collection_scoped_tables_schema_parity.py})
 * gates the registry against the schema. A table that is in the schema and in neither list is that gate's
 * to catch (it fails the registry first); this one starts where the registry ends. It does not check that
 * the move code does what a REPOINT row says: {@code DocumentMoveTest} pins the five REPOINT tables'
 * behaviour, and {@link #theRepointTablesAreTheFiveTheMoveReHomes} pins that the two lists name the same
 * five.
 *
 * <p>The check is a pure function over a registry and a map, so the planted-table test runs the SAME
 * function over a registry with one extra entry and sees it fail. No database.
 */
class CrossModelDispositionParityTest {

    /** Registry tables (by count key) that have no disposition. */
    static List<String> missingDispositions(List<CollectionScopedTable> registry,
                                            Map<String, CrossModelDisposition> dispositions) {
        List<String> missing = new ArrayList<>();
        for (CollectionScopedTable t : registry) {
            if (!dispositions.containsKey(t.countKey())) missing.add(t.countKey());
        }
        return missing;
    }

    /** Disposition keys that name neither a registry table nor a documented outside-the-registry table. */
    static List<String> staleDispositions(List<CollectionScopedTable> registry,
                                          Map<String, CrossModelDisposition> dispositions,
                                          Set<String> outsideRegistry) {
        Set<String> known = new LinkedHashSet<>(outsideRegistry);
        for (CollectionScopedTable t : registry) known.add(t.countKey());
        List<String> stale = new ArrayList<>();
        for (String key : dispositions.keySet()) {
            if (!known.contains(key)) stale.add(key);
        }
        return stale;
    }

    @Test
    void everyRegisteredTableHasADisposition() {
        assertThat(CatalogRepository.COLLECTION_SCOPED_TABLES).as("non-vacuity: the registry is not empty").isNotEmpty();
        assertThat(CatalogRepository.CROSS_MODEL_DISPOSITIONS).as("non-vacuity: the list is not empty").isNotEmpty();
        assertThat(missingDispositions(CatalogRepository.COLLECTION_SCOPED_TABLES, CatalogRepository.CROSS_MODEL_DISPOSITIONS))
            .as("a table in COLLECTION_SCOPED_TABLES with no line in CROSS_MODEL_DISPOSITIONS")
            .isEmpty();
    }

    @Test
    void noDispositionNamesATableTheRegistryLacks() {
        Set<String> outside = CatalogRepository.CROSS_MODEL_DISPOSITIONS_OUTSIDE_REGISTRY.keySet();
        // the registry's list and the outside-the-registry list are kept apart on purpose
        for (CollectionScopedTable t : CatalogRepository.COLLECTION_SCOPED_TABLES) {
            assertThat(outside).as("registered table also listed as outside the registry").doesNotContain(t.countKey());
        }
        assertThat(staleDispositions(CatalogRepository.COLLECTION_SCOPED_TABLES,
                CatalogRepository.CROSS_MODEL_DISPOSITIONS, outside))
            .as("a CROSS_MODEL_DISPOSITIONS line for a table that is no longer registered")
            .isEmpty();
        // The previous third assertion here ran staleDispositions over OUTSIDE_REGISTRY with that same map's
        // own key set as the known set, so it could not fail; it is removed. This one compares against an
        // independent source, the jOOQ schema generated from the migrations: every outside-the-registry
        // entry must name a table that exists and carries a collection-named column. It does not compare
        // against the Python _DOCUMENTED_EXCLUSIONS (a different language; the hand-kept agreement of the
        // two lists is not checked by this class).
        Set<String> schemaTables = new TreeSet<>();
        for (Table<?> t : Nexus.NEXUS.getTables()) schemaTables.add(t.getName());
        for (String key : outside) {
            assertThat(schemaTables).as("outside-the-registry entry '" + key + "' names a table in the schema")
                .contains(key);
            Table<?> table = Nexus.NEXUS.getTables().stream().filter(t -> t.getName().equals(key)).findFirst().orElseThrow();
            assertThat(Arrays.stream(table.fields()).map(Field::getName).toList())
                .as("outside-the-registry table '" + key + "' has a collection-named column")
                .anyMatch(n -> n.contains("collection"));
        }
        assertThat(outside).as("non-vacuity: the outside-the-registry list is not empty").isNotEmpty();
    }

    @Test
    void aTableAddedToTheRegistryWithoutADispositionIsReported() {
        // Plant a registry entry exactly as a developer adding a table to COLLECTION_SCOPED_TABLES would,
        // and run the same check the real registry passes.
        List<CollectionScopedTable> planted = new ArrayList<>(CatalogRepository.COLLECTION_SCOPED_TABLES);
        planted.add(new CollectionScopedTable("planted_table", CHUNKS, CHUNKS.COLLECTION));

        assertThat(missingDispositions(planted, CatalogRepository.CROSS_MODEL_DISPOSITIONS))
            .containsExactly("planted_table");
        // and the real registry still passes the identical check, so the failure above is the plant's
        assertThat(missingDispositions(CatalogRepository.COLLECTION_SCOPED_TABLES, CatalogRepository.CROSS_MODEL_DISPOSITIONS))
            .isEmpty();
    }

    @Test
    void aDispositionForAnUnregisteredTableIsReported() {
        Map<String, CrossModelDisposition> planted = new java.util.HashMap<>(CatalogRepository.CROSS_MODEL_DISPOSITIONS);
        planted.put("ghost_table", CrossModelDisposition.LEAVE);

        assertThat(staleDispositions(CatalogRepository.COLLECTION_SCOPED_TABLES, planted,
                CatalogRepository.CROSS_MODEL_DISPOSITIONS_OUTSIDE_REGISTRY.keySet()))
            .containsExactly("ghost_table");
    }

    @Test
    void theRepointTablesAreTheFiveTheMoveReHomes() {
        // CatalogRepository#completeMove and writeManifestRows re-point exactly these. A sixth REPOINT line
        // needs move code (and a DocumentMoveTest case); fewer means the move does something the list denies.
        Set<String> repoint = new TreeSet<>();
        CatalogRepository.CROSS_MODEL_DISPOSITIONS.forEach((table, d) -> {
            if (d == CrossModelDisposition.REPOINT) repoint.add(table);
        });
        assertThat(repoint).containsExactly("aspect_extraction_queue", "catalog_document_chunks", "catalog_documents",
            "document_aspects", "document_highlights");
    }
}
