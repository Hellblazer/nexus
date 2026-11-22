// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.datamove.RenameReadiness;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import org.junit.jupiter.api.Test;

import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), the rename guard's decisions, without a database: gate 3 is read once inside the pin and the path is
 * handed down (review item 6), the drop move's completion is sticky (item 2), and {@code idsStable} is gate 3 holding with nothing
 * missing (item 7).
 */
class RenameGuardTest {

    /** A readiness with a fixed answer and a drop-move fact. */
    private static RenameReadiness readiness(Missing missing, boolean dropComplete) {
        return new RenameReadiness() {
            @Override
            public Missing missing() {
                return missing;
            }

            @Override
            public boolean dropMoveComplete() {
                return dropComplete;
            }
        };
    }

    @Test
    void gate3IsReadOnceInsideThePin_andThePathItGaveIsTheOneTheRenameRuns() {
        AtomicInteger reads = new AtomicInteger();
        // a flap: the first read says false, every later one true. Before the fix the refusal and the path read gate 3 separately.
        var guard = new RenameGuard(() -> reads.incrementAndGet() > 1, readiness(Missing.NAME_KEY_DROP_MOVE, false));

        Boolean path = guard.pinned(oneRow -> oneRow);

        assertThat(reads.get()).as("gate 3 is read exactly once per rename").isEqualTo(1);
        assertThat(path).as("the rename ran on the path of that one read (false: the name path), not refused by the later true").isFalse();

        var other = new RenameGuard(() -> reads.incrementAndGet() % 2 == 0, readiness(Missing.NONE, false));
        reads.set(0);
        assertThat(other.<Boolean>pinned(oneRow -> oneRow)).as("a first read of false is the name path whatever follows").isFalse();
    }

    @Test
    void fromGate3ARenameIsRefusedUntilNothingIsMissing_thenItRunsOneRow() {
        for (Missing missing : new Missing[] {Missing.NAME_KEY_DROP_MOVE, Missing.P35B_LIST_MISSING, Missing.P35B_RESTART}) {
            var guard = new RenameGuard(() -> true, readiness(missing, missing != Missing.NAME_KEY_DROP_MOVE));
            assertThatThrownBy(() -> guard.pinned(oneRow -> "ran")).isInstanceOf(RenameRefusedException.class)
                .extracting(e -> ((RenameRefusedException) e).missing()).isEqualTo(missing);
        }
        assertThat(new RenameGuard(() -> true, readiness(Missing.NONE, true)).<Boolean>pinned(oneRow -> oneRow)).isTrue();
    }

    @Test
    void onceTheDropMoveCompleted_aDipOfGate3NeverMakesARenameTheNamePathAgain() {
        // gate 3 dropped back to false, the move completed, P3.5b not applied: refused (the restart), not the name path
        var refused = new RenameGuard(() -> false, readiness(Missing.P35B_RESTART, true));
        assertThatThrownBy(() -> refused.pinned(oneRow -> "name path ran")).isInstanceOf(RenameRefusedException.class)
            .extracting(e -> ((RenameRefusedException) e).missing()).isEqualTo(Missing.P35B_RESTART);
        assertThatThrownBy(refused::requireRenameAllowed).isInstanceOf(RenameRefusedException.class);

        // gate 3 dropped back, the move completed, everything applied: the one-row rename
        var oneRow = new RenameGuard(() -> false, readiness(Missing.NONE, true));
        assertThat(oneRow.<Boolean>pinned(path -> path)).as("sticky: one-row, never the name path").isTrue();

        // control: no completed move and gate 3 false is the name path, as before
        var before = new RenameGuard(() -> false, readiness(Missing.NONE, false));
        assertThat(before.<Boolean>pinned(path -> path)).isFalse();
        before.requireRenameAllowed();
    }

    @Test
    void idsStableIsGate3HoldingWithNothingMissing() {
        assertThat(new RenameGuard(() -> true, readiness(Missing.NONE, true)).idsStable()).isTrue();
        assertThat(new RenameGuard(() -> false, readiness(Missing.NONE, true)).idsStable())
            .as("gate 3 does not hold: the ids are not known stable").isFalse();
        for (Missing missing : new Missing[] {Missing.NAME_KEY_DROP_MOVE, Missing.P35B_LIST_MISSING, Missing.P35B_RESTART}) {
            assertThat(new RenameGuard(() -> true, readiness(missing, true)).idsStable()).as("missing " + missing).isFalse();
        }
        assertThat(RenameGuard.open().idsStable()).as("an open guard has no gate 3").isFalse();
    }

    @Test
    void theWireValuesOfEveryMissingConditionAreDistinct() {
        assertThat(RenameRefusedException.wireValue(Missing.NAME_KEY_DROP_MOVE)).isEqualTo("name_key_drop_move");
        assertThat(RenameRefusedException.wireValue(Missing.P35B_LIST_MISSING)).isEqualTo("p35b_list_missing");
        assertThat(RenameRefusedException.wireValue(Missing.P35B_RESTART)).isEqualTo("restart");
        assertThat(new RenameRefusedException(Missing.P35B_LIST_MISSING).getMessage()).contains("p35b-continue-changesets.txt")
            .doesNotContain("restarts to apply");
    }
}
