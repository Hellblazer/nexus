// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.TenantExport.Column;
import dev.nexus.service.db.TenantExport.ExportClass;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExport.Reference;
import org.junit.jupiter.api.Test;

import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** The registry's own rules, without a database (RDR-248, bead nexus-csayy.1). */
class TenantExportRegistryTest {

    private static ExportTable table(String name, Reference... refs) {
        return new ExportTable("nexus", name, ExportClass.DATA, null,
            List.of(new Column("id", "bigint")), "id", List.of(refs), Set.of(), Set.of(), Set.of());
    }

    @Test
    void aTableLoadedBeforeTheTableItRefersToWithNoForeignKeyIsAViolation() {
        // The spike's first staged load failed with relation "map_18" does not exist: nx_answer_runs.plan_id
        // refers to plans with no foreign key, and nx_answer_runs was loaded first.
        List<ExportTable> spikeOrder = List.of(
            table("nx_answer_runs", Reference.soft("plan_id", "plans", "id")),
            table("plans"));
        assertThat(TenantExport.loadOrderViolations(spikeOrder))
            .containsExactly("nx_answer_runs.plan_id refers to plans, which loads after it");
        assertThat(TenantExport.loadOrderViolations(List.of(spikeOrder.get(1), spikeOrder.get(0)))).isEmpty();
    }

    @Test
    void aSelfReferenceIsNotAnOrderingEdge() {
        assertThat(TenantExport.loadOrderViolations(
            List.of(table("topics", Reference.fk(List.of("parent_id"), "topics", List.of("id")))))).isEmpty();
    }

    @Test
    void theRegistryLoadsInOrder() {
        assertThat(TenantExport.loadOrderViolations(TenantExport.TABLES)).isEmpty();
    }

    @Test
    void namesAreUnique() {
        Set<String> seen = new HashSet<>();
        TenantExport.TABLES.forEach(t -> assertThat(seen.add(t.name())).as(t.name()).isTrue());
    }

    @Test
    void anExcludedTableNeedsAReasonAndOnlyAnExcludedTableHasOne() {
        assertThatThrownBy(() -> new ExportTable("nexus", "x", ExportClass.EXCLUDED, null,
            List.of(), null, List.of(), Set.of(), Set.of(), Set.of()))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new ExportTable("nexus", "x", ExportClass.DATA, "why",
            List.of(), null, List.of(), Set.of(), Set.of(), Set.of()))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void theWhitelistAnswersOnlyDataAndDerivedTables() {
        assertThat(TenantExport.exportable("chunks")).isPresent();
        assertThat(TenantExport.exportable("memory")).isPresent();
        assertThat(TenantExport.exportable("frecency")).isPresent();
        assertThat(TenantExport.exportable("tuples")).isEmpty();
        assertThat(TenantExport.exportable("t1.scratch")).isEmpty();
        assertThat(TenantExport.exportable("embedding_models")).as("a global table").isEmpty();
        assertThat(TenantExport.exportable("pg_authid")).isEmpty();
        assertThat(TenantExport.entry("tuples")).isPresent();
    }

    @Test
    void theClassesMatchTheRdr() {
        // RDR-248 Technical Design, "The classes, from the spike".
        Set<String> data = Set.of("catalog_owners", "catalog_collections", "catalog_documents",
            "catalog_document_chunks", "catalog_links", "catalog_meta", "catalog_sensitivity_changes", "chunks",
            "embedding_profile", "memory", "memory_summaries", "plans", "document_aspects",
            "document_highlights", "topics", "topic_assignments", "topic_links", "taxonomy_meta",
            "claude_assisted_remediation_consents");
        Set<String> derived = Set.of("frecency", "taxonomy_centroids", "search_telemetry", "relevance_log",
            "nx_answer_runs", "nx_answer_steps", "chunk_orphaned_at");
        TenantExport.TABLES.forEach(t -> {
            ExportClass expected = data.contains(t.name()) ? ExportClass.DATA
                : derived.contains(t.name()) ? ExportClass.DERIVED : ExportClass.EXCLUDED;
            assertThat(t.exportClass()).as(t.name()).isEqualTo(expected);
        });
        Set<String> named = new HashSet<>();
        TenantExport.TABLES.forEach(t -> named.add(t.name()));
        assertThat(named).containsAll(data).containsAll(derived);
    }
}
