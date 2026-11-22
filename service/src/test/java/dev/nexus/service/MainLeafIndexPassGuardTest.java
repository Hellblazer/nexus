// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.datamove.CompletionBootWork;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.vectors.LeafIndexPass;
import dev.nexus.service.vectors.Rdr229SwapMove;
import org.junit.jupiter.api.Test;

import java.time.Clock;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.3b and P3.3c (nexus-p2m1x.20, nexus-p2m1x.21): the behavioural half of {@code MainPciWiringTest}'s pin of
 * {@code Main.startLeafIndexPass}. P3.3b guarded the pass on {@code Rdr229SwapMove.PLACEHOLDER}, because the swap row it would register
 * was an inert placeholder and one INSERT per label would leave a store that never got the real one. P3.3c replaced the placeholder with
 * the real text and deleted the constant and the guard, so the pass starts under its other conditions only: a completion check to read
 * gate 1 from.
 */
class MainLeafIndexPassGuardTest {

    private static final AdminConnection UNREACHABLE = new AdminConnection("jdbc:postgresql://127.0.0.1:1/none", "u", "p");

    @Test
    void withACompletionCheckThePassStarts_andReportsNothingAsInProgressYet() {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var check = CompletionBootWork.atBoot(registry, UNREACHABLE);   // built, not started: no connection is made
        LeafIndexPass pass = Main.startLeafIndexPass(registry, UNREACHABLE, check);
        try {
            assertThat(pass).as("P3.3c deleted the placeholder guard: the pass starts").isNotNull();
            assertThat(registry.snapshot()).as("and nothing is reported as in progress")
                .noneMatch(e -> LeafIndexPass.BOOT_WORK_ID.equals(e.id()));
        } finally {
            if (pass != null) {
                pass.close();
            }
            check.close();
        }
    }

    @Test
    void withoutACompletionCheckNoPassIsStarted() {
        assertThat(Main.startLeafIndexPass(new BootWorkRegistry(Clock.systemUTC()), UNREACHABLE, null))
            .as("its gate is the check's latest read; there is none to read").isNull();
    }

    /**
     * The net under the guard's removal: the swap move of each table has an {@code on_complete}, so a pass that registers it registers the
     * real move. A merge that put the placeholder body back fails here, where nothing else would, since the pass would start and register
     * an inert row.
     */
    @Test
    void theSwapMoveOfBothTablesHasAnOnComplete() {
        for (String table : new String[] {"chunks", "taxonomy_centroids"}) {
            assertThat(Rdr229SwapMove.spec(table).onComplete()).as("%s: the swap move's on_complete", table).isNotNull().isNotBlank();
        }
    }
}
