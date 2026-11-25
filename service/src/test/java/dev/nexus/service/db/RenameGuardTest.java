// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.RenameReadiness;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import dev.nexus.service.db.RenameGuard.RenamePath;
import org.junit.jupiter.api.Test;

import java.lang.reflect.Modifier;
import java.util.Arrays;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.5a, review SB1 and linda's R-A ruling: the rename decision over the two inputs, as Main builds them. The latch row is the
 * one signal (it is read inside the rename's transaction; the database half is {@code RenameGuardLatchRecheckTest}), and the
 * {@code RenameReadiness} answers "the name-key drop move is missing" until the completion check exists, so a latched install that has
 * not yet got a check is refused, never allowed. The readiness lambda below is the shape {@code MainPciWiringTest} pins in
 * {@code Main}'s text.
 */
class RenameGuardTest {

    /** The check's answer Main reads once it exists. */
    private final AtomicReference<RenameReadiness> check = new AtomicReference<>();

    private RenameReadiness mainReadiness() {
        return () -> {
            var c = check.get();
            return c == null ? Missing.NAME_KEY_DROP_MOVE : c.missing();
        };
    }

    @Test
    void aLatchRowWithTheReadinessUnset_refusesNamingTheDropMove() {
        assertThatThrownBy(() -> RenameGuard.decide(true, mainReadiness())).isInstanceOf(RenameRefusedException.class)
            .extracting(e -> ((RenameRefusedException) e).missing()).isEqualTo(Missing.NAME_KEY_DROP_MOVE);
    }

    /** Before the latch (the readiness still waits for something) there is no row, and the rename is the name path. */
    @Test
    void noLatchRowWithTheReadinessNotComplete_isTheNamePath() {
        assertThat(RenameGuard.decide(false, mainReadiness())).isEqualTo(RenamePath.NAME_PATH);
        assertThat(RenameGuard.decide(false, () -> Missing.P35B_RESTART)).isEqualTo(RenamePath.NAME_PATH);
    }

    /**
     * linda's Q-B: the readiness says the move is complete but there is no latch row, an inconsistency (as after a partial restore):
     * the typed 409 {@code rename_refused} whose {@code missing} is {@code latch_row_missing}, never the name path (it would mint a new id).
     */
    @Test
    void noLatchRowWithTheReadinessComplete_isRefusedNamingTheMissingRow() {
        assertThatThrownBy(() -> RenameGuard.decide(false, RenameReadiness.ready())).isInstanceOf(RenameRefusedException.class)
            .satisfies(e -> {
                var r = (RenameRefusedException) e;
                assertThat(r.missing()).isEqualTo(Missing.LATCH_ROW_MISSING);
                assertThat(r.wireValue()).isEqualTo("latch_row_missing");
                assertThat(r.getMessage()).contains("latch row");
            });
    }

    /** A rename that names no body for the one-row regime is a construction error, not a null pointer in the one-row regime only. */
    @Test
    void aNullOneRowBodyFailsAtOnce_namingTheArgument() {
        var guard = new RenameGuard(mainReadiness(), OneRowRename.failClosed());
        assertThatThrownBy(() -> guard.runRename(work -> work.apply(null), ctx -> "name", null))
            .isInstanceOf(NullPointerException.class).hasMessageContaining("oneRowBody");
        assertThatThrownBy(() -> RenameGuard.open().runRename(work -> work.apply(null), ctx -> "name", null))
            .as("the open guard too").isInstanceOf(NullPointerException.class);
    }

    @Test
    void onceTheCheckExistsItsAnswerDecides_andARowStaysRefusingWhileAConditionIsMissing() {
        check.set(() -> Missing.P35B_RESTART);
        assertThatThrownBy(() -> RenameGuard.decide(true, mainReadiness())).isInstanceOf(RenameRefusedException.class)
            .extracting(e -> ((RenameRefusedException) e).missing()).isEqualTo(Missing.P35B_RESTART);

        check.set(RenameReadiness.ready());
        assertThat(RenameGuard.decide(true, mainReadiness())).as("the row and the readiness complete: the one-row regime")
            .isEqualTo(RenamePath.ONE_ROW);
    }

    @Test
    void theOpenGuardRunsTheNameBodyAtOnce_withNoTransactionContextOfItsOwn() {
        String ran = RenameGuard.open().runRename(work -> work.apply(null), ctx -> "name", ctx -> "one-row");
        assertThat(ran).isEqualTo("name");
        assertThat(RenameGuard.open().idsStable()).isFalse();
    }

    /** The default one-row catalog rename refuses, writing nothing: P3.7 replaces it where the guard is built. */
    @Test
    void theDefaultOneRowRenameIsFailClosed() {
        assertThatThrownBy(() -> OneRowRename.failClosed().rename(null, "t", "a", "b", OneRowRename.Request.bare()))
            .isInstanceOf(RenamePathMismatchException.class).hasMessageContaining("'a'").hasMessageContaining("'b'");
        var guard = new RenameGuard(RenameReadiness.ready(), OneRowRename.failClosed());
        assertThatThrownBy(() -> guard.oneRow(null, "t", "a", "b", OneRowRename.Request.bare(), new RenameOutcome[1])).isInstanceOf(RenamePathMismatchException.class);
        assertThatCode(() -> new RenameGuard(RenameReadiness.ready(), (ctx, t, a, b, r) -> new RenameOutcome(java.util.Map.of(), 1L, "e", true)))
            .as("P3.7's own implementation is a constructor argument").doesNotThrowAnyException();
    }

    /** One public constructor, and no decision outside the transaction: the entry checks are gone. */
    @Test
    void theGuardHasOneConstructorAndNoCheckOutsideTheTransaction() {
        assertThat(Arrays.stream(RenameGuard.class.getConstructors()).count()).as("one public constructor").isEqualTo(1);
        assertThat(Arrays.stream(RenameGuard.class.getDeclaredMethods()).filter(m -> Modifier.isPublic(m.getModifiers()))
            .map(m -> m.getName()).distinct().toList())
            .as("the public surface: open, runRename, idsStable (the one-row body and its commit hook are package-private)")
            .containsExactlyInAnyOrder("open", "runRename", "idsStable");
    }
}
