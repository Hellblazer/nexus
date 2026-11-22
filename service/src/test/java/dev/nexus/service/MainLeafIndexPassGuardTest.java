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
 * hand: a pass that registers the placeholder swap row would leave a store that never gets the real one (one INSERT per label).
 *
 * <p><b>What P3.3c edits.</b> The constant is read by reflection so that this test still compiles once P3.3c deletes it; with the
 * constant gone the method must start the pass. P3.3c's commit still edits two other places in the same breath: {@code Main} (it stops
 * compiling until its guard goes) and the guard assertions of {@code MainPciWiringTest}'s pin of {@code startLeafIndexPass} (the pin
 * requires the guard's text). This test needs no edit, and its last test is the net under all three: the constant must be true exactly
 * while the spec is the placeholder (no {@code on_complete}), so a three-way merge that keeps {@code PLACEHOLDER = true} beside the
 * real body (the hunk holding the constant can conflict, and either resolution compiles) turns it red instead of leaving a pass that
 * never starts.
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

    /**
     * Armed by P3.3c: while the placeholder guard is in {@code Main} it returns first, so this reaches the no-completion-check branch
     * only once the guard is gone. Until then it holds for the guard's reason, not the check's.
     */
    @Test
    void withoutACompletionCheck_noPassIsStarted_reachedOnlyOnceP33cRemovesThePlaceholderGuard() {
        assertThat(Main.startLeafIndexPass(new BootWorkRegistry(Clock.systemUTC()), UNREACHABLE, null))
            .as("its gate is the check's latest read; there is none to read").isNull();
    }

    /**
     * N1 (review r2): the guard constant and the swap move's body cannot drift apart. P3.3c replaces the placeholder (no
     * {@code on_complete}: inert, its verification finds no row) with a body whose swap move has one, for both tables, and deletes the
     * constant with it. A merge that keeps the constant true beside the real body, or drops the constant beside the placeholder body,
     * fails here, where nothing else would: with the constant true the pass never starts and no other test notices.
     */
    @Test
    void thePlaceholderConstantIsTrueExactlyWhileTheSpecHasNoOnComplete_forBothTables() {
        for (String table : new String[] {"chunks", "taxonomy_centroids"}) {
            boolean inert = Rdr229SwapMove.spec(table).onComplete() == null;
            assertThat(placeholder()).as("%s: PLACEHOLDER is true exactly while the swap move has no on_complete (inert=%s)", table, inert)
                .isEqualTo(inert);
        }
    }
}
