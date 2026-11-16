// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.List;

/**
 * nexus-4lnn1.1.10 (RDR-230 Phase 2): the fields {@link TenantKeyedStateGateTest} lists as not tenant-keyed for the
 * REINDEX phase. They hold the phase's per-index memory (previous counts, owed bulk deletes and anchors, the
 * red-canary limit, backoff, counters) and the leaf-index catalog reads, per engine in memory, keyed by index or
 * leaf names that carry a hash of the tenant, and lost on restart like the reconciler's failures map. Kept apart
 * from the gate test so that file stays under its size ceiling.
 */
final class PciReindexNotKeyedFields {
    static final List<String> FIELDS = List.of(
        "PciReconciler.reindexPhaseHook",
        "PciReconciler.afterLeafReindexHook",
        "PciReconciler.Tally.looks",
        "PciLeafReindexRule.Scan.candidates",
        "PciLeafReindexRule.Scan.rebuilt",
        "PciLeafReindexRule.Scan.stamps",
        "PciReindexPolicy.Candidate.redQueries",
        "PciCatalog.RedUnfixed.queries",
        "PciCatalog.Leaf.hnswIndexes",
        "PciCatalog.Leaf.leafLeftovers",
        "PciCatalog.LeafBuilder.candidates",
        "PciBuilderSession.Pass.leafIds",
        "PciReindexState.Look.readings",
        "PciReindexState.previousRows",
        "PciReindexState.bulkOwed",
        "PciReindexState.lastRedReindex",
        "PciReindexState.redAfterReindex",
        "PciReindexState.failures",
        "PciReindexState.anchorOwed",
        "PciReindexState.reindexes",
        "PciReindexState.reindexing",
        "PciReindexState.lastReindexAt",
        "PciReindexState.references");

    private PciReindexNotKeyedFields() {
    }
}
