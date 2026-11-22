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
 * RDR-229 P3.3b (nexus-p2m1x.20): the behavioural half of the placeholder guard, beside {@code MainPciWiringTest}'s text pin. While
 * {@code Rdr229SwapMove.PLACEHOLDER} is true, {@code Main.startLeafIndexPass} must not start a pass, even with a completion check in
 * hand: a pass that registers the placeholder swap row would leave a store that never gets the real one (one INSERT per label). The
 * constant is read by reflection so that P3.3c's commit, which deletes it, does not have to touch this test: with the constant gone the
 * guard is gone with it and the method must start the pass.
 */
class MainLeafIndexPassGuardTest {

    private static final AdminConnection UNREACHABLE = new AdminConnection("jdbc:postgresql://127.0.0.1:1/none", "u", "p");

    private static boolean placeholder() {
        try {
            return Rdr229SwapMove.class.getField("PLACEHOLDER").getBoolean(null);
        } catch (NoSuchFieldException e) {
            return false;   // P3.3c deleted it
        } catch (IllegalAccessException e) {
            throw new IllegalStateException(e);
        }
    }

    @Test
    void whileTheSwapMoveIsThePlaceholder_noPassIsStarted_evenWithACompletionCheck() {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var check = CompletionBootWork.atBoot(registry, UNREACHABLE);   // built, not started: no connection is made
        LeafIndexPass pass = Main.startLeafIndexPass(registry, UNREACHABLE, check);
        try {
            if (placeholder()) {
                assertThat(pass).as("the guard: Rdr229SwapMove.PLACEHOLDER is true, so no pass is started").isNull();
            } else {
                assertThat(pass).as("P3.3c deleted the placeholder: the pass starts").isNotNull();
            }
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
    void withoutACompletionCheck_noPassIsStarted() {
        assertThat(Main.startLeafIndexPass(new BootWorkRegistry(Clock.systemUTC()), UNREACHABLE, null))
            .as("its gate is the check's latest read; there is none to read").isNull();
    }
}
