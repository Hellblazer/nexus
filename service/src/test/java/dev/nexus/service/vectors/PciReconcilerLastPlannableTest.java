// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PciCatalog.Leaf;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd.3: which leaf completes the {@code boot_work} total. Hermetic: the selection is a pure function of the
 * snapshot's leaves.
 */
class PciReconcilerLastPlannableTest {

    private static Leaf leaf(String name, String model, String tenant) {
        return new Leaf("nexus", name, model, tenant, List.of());
    }

    @Test
    void isTheLastLeafWithAParsedModelAndTenant_ignoringUnparsedLeavesAfterIt() {
        Leaf a = leaf("a", "m1", "t1");
        Leaf b = leaf("b", "m1", "t2");
        Leaf noTenant = leaf("c", "m1", null);
        Leaf noModel = leaf("d", null, "t3");

        assertThat(PciReconciler.lastPlannable(List.of(a, b, noTenant, noModel))).isSameAs(b);
        assertThat(PciReconciler.lastPlannable(List.of(a, b))).isSameAs(b);
        assertThat(PciReconciler.lastPlannable(List.of(a))).as("a single-leaf install").isSameAs(a);
    }

    @Test
    void isNullWhenNoLeafCanPlan() {
        assertThat(PciReconciler.lastPlannable(List.of())).isNull();
        assertThat(PciReconciler.lastPlannable(List.of(leaf("x", null, "t"), leaf("y", "m", null)))).isNull();
    }
}
