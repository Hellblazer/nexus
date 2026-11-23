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
 * handed down (review item 6), the gate 3 latch decides the path, never the live gate 3 (item 2), and {@code idsStable} is gate 3 holding with nothing
 * missing (item 7).
 */
class RenameGuardTest {

    /** A readiness with a fixed answer and a latch fact. */
    private static RenameReadiness readiness(Missing missing, boolean latched) {
        return new RenameReadiness() {
            @Override
            public Missing missing() {
                return missing;
            }

            @Override
            public boolean gate3Latched() {
                return latched;
            }

            @Override
            public boolean gate3LatchedUnderLock() {
                return latched;
            }

            @Override
            public Missing missingLatched() {
                return missing;
            }
        };
    }

    /** A readiness whose latch fact is read from {@code latched} on each call. */
    private static RenameReadiness flapping(Missing missing, java.util.function.BooleanSupplier latched) {
        return new RenameReadiness() {
            @Override
            public Missing missing() {
                return missing;
            }

            @Override
            public boolean gate3Latched() {
                return latched.getAsBoolean();
            }

            @Override
            public boolean gate3LatchedUnderLock() {
                return latched.getAsBoolean();
            }

            @Override
            public Missing missingLatched() {
                return missing;
            }
        };
    }

    @Test
    void theLatchIsReadOnceInsideThePin_andThePathItGaveIsTheOneTheRenameRuns() {
        AtomicInteger reads = new AtomicInteger();
        // a flap: the first read says not latched, every later one latched. The refusal and the path must follow one decision.
        var guard = new RenameGuard(() -> true, flapping(Missing.NAME_KEY_DROP_MOVE, () -> reads.incrementAndGet() > 1));

        Boolean path = guard.pinned(oneRow -> oneRow);

        assertThat(reads.get()).as("the regime is decided exactly once per rename").isEqualTo(1);
        assertThat(path).as("the rename ran on the path of that one read (false: the name path), not refused by the later latched").isFalse();

        var other = new RenameGuard(() -> true, flapping(Missing.NONE, () -> reads.incrementAndGet() % 2 == 0));
        reads.set(0);
        assertThat(other.<Boolean>pinned(oneRow -> oneRow)).as("a first read of not latched is the name path whatever follows").isFalse();
    }

    @Test
    void theLiveGate3AloneNeverStartsTheOneRowRegime_theLatchWaitsForTheRenamesInFlight() {
        // gate 3 holds live but the latch row is not written yet (the writer is waiting for a pinned rename): still the name path
        var guard = new RenameGuard(() -> true, readiness(Missing.NAME_KEY_DROP_MOVE, false));
        assertThat(guard.<Boolean>pinned(oneRow -> oneRow)).isFalse();
        guard.requireRenameAllowed();
        assertThat(guard.idsStable()).isFalse();
    }

    @Test
    void fromGate3ARenameIsRefusedUntilNothingIsMissing_thenItRunsOneRow() {
        for (Missing missing : new Missing[] {Missing.NAME_KEY_DROP_MOVE, Missing.P35B_LIST_MISSING, Missing.P35B_RESTART}) {
            var guard = new RenameGuard(() -> true, readiness(missing, true));
            assertThatThrownBy(() -> guard.pinned(oneRow -> "ran")).isInstanceOf(RenameRefusedException.class)
                .extracting(e -> ((RenameRefusedException) e).missing()).isEqualTo(missing);
        }
        assertThat(new RenameGuard(() -> true, readiness(Missing.NONE, true)).<Boolean>pinned(oneRow -> oneRow)).isTrue();
    }

    @Test
    void onceGate3IsLatched_aDipOfTheLiveGate3NeverMakesARenameTheNamePathAgain() {
        // the live gate 3 dropped back to false, the latch holds, P3.5b not applied: refused (the restart), not the name path
        var refused = new RenameGuard(() -> false, readiness(Missing.P35B_RESTART, true));
        assertThatThrownBy(() -> refused.pinned(oneRow -> "name path ran")).isInstanceOf(RenameRefusedException.class)
            .extracting(e -> ((RenameRefusedException) e).missing()).isEqualTo(Missing.P35B_RESTART);
        assertThatThrownBy(refused::requireRenameAllowed).isInstanceOf(RenameRefusedException.class);

        // the live gate 3 dropped back, latched, everything applied: the one-row rename
        var oneRow = new RenameGuard(() -> false, readiness(Missing.NONE, true));
        assertThat(oneRow.<Boolean>pinned(path -> path)).as("sticky: one-row, never the name path").isTrue();

        // control: not latched and the live gate 3 false is the name path, as before
        var before = new RenameGuard(() -> false, readiness(Missing.NONE, false));
        assertThat(before.<Boolean>pinned(path -> path)).isFalse();
        before.requireRenameAllowed();
    }

    @Test
    void idsStableIsGate3HoldingWithNothingMissing() {
        assertThat(new RenameGuard(() -> true, readiness(Missing.NONE, true)).idsStable()).isTrue();
        assertThat(new RenameGuard(() -> false, readiness(Missing.NONE, true)).idsStable())
            .as("the live gate 3 dropped back but the latch holds and nothing is missing: the rename stays one-row, the ids stay stable").isTrue();
        assertThat(new RenameGuard(() -> false, readiness(Missing.NONE, false)).idsStable())
            .as("gate 3 neither held nor latched: the ids are not known stable").isFalse();
        for (Missing missing : new Missing[] {Missing.NAME_KEY_DROP_MOVE, Missing.P35B_LIST_MISSING, Missing.P35B_RESTART}) {
            assertThat(new RenameGuard(() -> true, readiness(missing, true)).idsStable()).as("missing " + missing).isFalse();
        }
        assertThat(RenameGuard.open().idsStable()).as("an open guard has no gate 3").isFalse();
    }

    @Test
    void theWireValuesOfEveryMissingConditionAreDistinct() {
        assertThat(RenameRefusedException.wireValue(Missing.READINESS_UNREADABLE)).isEqualTo("readiness_unreadable");
        assertThat(RenameRefusedException.wireValue(Missing.NAME_KEY_DROP_MOVE)).isEqualTo("name_key_drop_move");
        assertThat(RenameRefusedException.wireValue(Missing.P35B_LIST_MISSING)).isEqualTo("p35b_list_missing");
        assertThat(RenameRefusedException.wireValue(Missing.P35B_RESTART)).isEqualTo("restart");
        assertThat(new RenameRefusedException(Missing.P35B_LIST_MISSING).getMessage()).contains("p35b-continue-changesets.txt")
            .doesNotContain("restarts to apply");
    }

    @Test
    void aCompletedDropMoveIsNotALatch_withoutTheLatchTheRenameIsTheNamePath() {
        // linda 2026-11-23: the belt is gone. The latch row alone decides; a restart keeps it (Rdr229P37RestartBeltTest), so nothing else is needed.
        var guard = new RenameGuard(() -> false, readiness(Missing.NONE, false));
        assertThat(guard.<Boolean>pinned(oneRow -> oneRow)).as("not latched: the name path, whatever else is true of the install").isFalse();
        guard.requireRenameAllowed();
    }
}
