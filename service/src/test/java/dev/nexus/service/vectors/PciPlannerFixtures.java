// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciCatalog.Index;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import dev.nexus.service.vectors.PciReconcilePlanner.Action;
import dev.nexus.service.vectors.PciReconcilePlanner.Build;
import dev.nexus.service.vectors.PciReconcilePlanner.Drop;
import dev.nexus.service.vectors.PciReconcilePlanner.DropReason;
import dev.nexus.service.vectors.PciReconcilePlanner.RegistryRow;

import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * The builders shared by {@link PciReconcilePlannerTest} and {@link PciReconcilePlannerIdTest}: a leaf, its indexes and its
 * registry rows, and the plan call with the default settings. Static-imported by both.
 */
final class PciPlannerFixtures {

    private PciPlannerFixtures() { }

    static final String MODEL = "m";
    static final String TENANT = "t";
    static final int B = 100;

    static PciSettings settings(int maxPerLeaf) {
        return new PciSettings(true, B, 600, maxPerLeaf);
    }

    static final PciSettings DEFAULT = settings(16);

    static String name(String collection) {
        return PciCatalog.indexName(MODEL, TENANT, collection);
    }

    static Index valid(String collection) {
        return new Index(name(collection), true, collection);
    }

    static Index invalid(String collection) {
        return new Index(name(collection), false, collection);
    }

    static Leaf leaf(Index... indexes) {
        return new Leaf("nexus", "chunks_leaf", MODEL, TENANT, List.of(indexes));
    }

    static RegistryRow live(String collection) {
        return new RegistryRow(collection, "live", "");
    }

    static RegistryRow state(String collection, String lifecycleState) {
        return new RegistryRow(collection, lifecycleState, "");
    }

    static RegistryRow superseded(String collection, String by) {
        return new RegistryRow(collection, "live", by);
    }

    static List<Action> plan(Leaf leaf, Map<String, Integer> counts, List<RegistryRow> registry) {
        return PciReconcilePlanner.plan(leaf, counts, registry, Set.of(), null, DEFAULT);
    }

    static List<Action> plan(Leaf leaf, Map<String, Integer> counts, List<RegistryRow> registry,
                                     PciSettings settings) {
        return PciReconcilePlanner.plan(leaf, counts, registry, Set.of(), null, settings);
    }

    static Build build(String collection) {
        return new Build(name(collection), collection);
    }

    static Drop drop(String collection, DropReason reason) {
        return new Drop(name(collection), collection, reason);
    }
}
