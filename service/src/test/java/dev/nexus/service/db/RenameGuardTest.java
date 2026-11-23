// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.datamove.RenameReadiness;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import org.junit.jupiter.api.Test;

import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.5a, review SB1 (linda 2026-11-23): the rename refusal over the two inputs Main builds, as Main builds them. Main's
 * {@code Gate3} is the LATCHED signal (the boot latch until the completion check exists, then the check's latch, never back to
 * false), and its {@code RenameReadiness} answers "the name-key drop move is missing" until the check exists, so a latched install
 * that has not yet got a check is refused, never allowed. The two lambdas below are the shapes {@code MainPciWiringTest} pins in
 * {@code Main}'s text.
 */
class RenameGuardTest {

    /** The check's two answers Main reads once it exists. */
    private record Check(boolean latched, RenameReadiness readiness) { }

    private final AtomicReference<Check> completionRef = new AtomicReference<>();

    private Gate3 mainGate3(boolean bootLatched) {
        return () -> {
            var c = completionRef.get();
            return c != null ? c.latched() : bootLatched;
        };
    }

    private RenameReadiness mainReadiness() {
        return () -> {
            var c = completionRef.get();
            return c == null ? Missing.NAME_KEY_DROP_MOVE : c.readiness().missing();
        };
    }

    @Test
    void aLatchedGateWithTheReadinessUnset_refusesNamingTheDropMove() {
        var guard = new RenameGuard(mainGate3(true), mainReadiness());

        assertThatThrownBy(guard::requireRenameAllowed).isInstanceOf(RenameRefusedException.class)
            .extracting(e -> ((RenameRefusedException) e).missing()).isEqualTo(Missing.NAME_KEY_DROP_MOVE);
    }

    @Test
    void anUnlatchedGateRefusesNothing_whateverTheReadinessSays() {
        var guard = new RenameGuard(mainGate3(false), mainReadiness());

        assertThatCode(guard::requireRenameAllowed).doesNotThrowAnyException();
    }

    @Test
    void onceTheCheckExistsItsAnswersDecide_andALatchedCheckStaysRefusingWhileTheMoveIsMissing() {
        var guard = new RenameGuard(mainGate3(true), mainReadiness());

        completionRef.set(new Check(true, () -> Missing.P35B_RESTART));
        assertThatThrownBy(guard::requireRenameAllowed).isInstanceOf(RenameRefusedException.class)
            .extracting(e -> ((RenameRefusedException) e).missing()).isEqualTo(Missing.P35B_RESTART);

        completionRef.set(new Check(true, RenameReadiness.ready()));
        assertThatCode(guard::requireRenameAllowed).as("latched and ready: the one-row rename may run").doesNotThrowAnyException();
    }

    @Test
    void theOpenGuardNeverRefuses() {
        assertThatCode(RenameGuard.open()::requireRenameAllowed).doesNotThrowAnyException();
        assertThat(RenameGuard.open().idsStable()).isFalse();
    }
}
