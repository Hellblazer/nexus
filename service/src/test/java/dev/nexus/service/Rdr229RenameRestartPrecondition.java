// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.datamove.RenameReadiness;
import dev.nexus.service.datamove.RenameReadiness.Missing;

import java.time.Duration;
import java.util.Optional;

/**
 * RDR-229 residual R9 (nexus-p2m1x.48): the precondition every rename test takes on a fresh or half-stamped fixture against the
 * P3.5/P3.7 cut. On such a store the stamp moves complete during boot 1 (a fresh store's tables are empty), gate 3 can hold in that
 * process, and P3.5b's CONTINUE changesets were skipped at boot 1, so they apply only at boot 2; until then the engine refuses every
 * rename and says it needs a restart (RDR item 6, "the one restart the chain from a half-stamped store needs"). So: boot; wait until
 * the stamp moves have {@code completed_at} and {@link RenameReadiness} reports {@link Missing#P35B_RESTART}; restart the engine on
 * the same database; wait until gate 3 holds and readiness reports {@link Missing#NONE}; then hand the engine back.
 *
 * <p><b>Readiness is observed in process</b>, through {@code CompletionBootWork#renameReadiness()}, the object the engine hands the
 * rename primitive (so the wait and the refusal read one signal); the wire view of it is {@code GET /v1/status}
 * {@code rdr229_completion.rename_missing}, which {@code Rdr229FreshInstallRenameRestartTest} asserts. Until P3.7 the accessor is a
 * stub that says ready, so {@link #reach} never sees the restart signal.
 *
 * <p><b>What a wait that times out means.</b> The stamp wait is hard: the stamp moves run today, so a timeout there is a fixture
 * fault and fails with the incomplete labels. The two readiness waits are bounded and soft, and return {@link Reached#note()} saying
 * which step did not happen and why (a state that never came). A caller puts that note in the
 * description of its first rename assertion, so a red before P3.7 fails on the rename's behaviour and says why the precondition was
 * not met, instead of failing in the fixture. A caller that must not run without the precondition calls {@link Reached#require}.
 */
final class Rdr229RenameRestartPrecondition {

    /** How long the stamp moves may take on an empty store (about 25 moves, each a few hundred milliseconds). */
    static final Duration STAMP_WAIT = Duration.ofMinutes(3);
    /** How long a readiness state may take to appear after the event that causes it (a completion event re-reads the check at once). */
    static final Duration SIGNAL_WAIT = Duration.ofSeconds(30);

    private Rdr229RenameRestartPrecondition() { /* static */ }

    /** Whether the precondition was met, and when it was not, which step failed and why. */
    record Reached(boolean met, String note) {
        /** Fails unless the precondition was met. */
        void require() {
            if (!met) {
                throw new AssertionError("rename precondition not met: " + note);
            }
        }

        /** The text a caller appends to an assertion description: empty when met. */
        String suffix() {
            return met ? "" : " [precondition not met: " + note + "]";
        }
    }

    /** The engine after the precondition, with whether it was reached. */
    record Env(Rdr229RenameEngine engine, Reached reached) { }

    /**
     * Boots {@code engine} (not yet booted), waits for the stamp moves and for the restart signal, restarts, and waits for readiness
     * NONE with gate 3 holding. The engine's gate 3 input must be open.
     */
    static Env reach(Rdr229RenameEngine engine) throws Exception {
        engine.boot();
        awaitStampMovesComplete(engine);
        StringBuilder note = new StringBuilder();

        boolean sawRestart = awaitReadiness(engine, Missing.P35B_RESTART, note, "boot 1");
        engine.restart();
        boolean ready = awaitGate3AndReadiness(engine, Missing.NONE, note, "boot 2");

        boolean met = sawRestart && ready;
        return new Env(engine, new Reached(met, met ? "" : note.toString().strip()));
    }

    /** Hard wait: every {@code rdr229-stamp-*} move has {@code completed_at} and gate 1 holds, within {@link #STAMP_WAIT}. */
    static void awaitStampMovesComplete(Rdr229RenameEngine engine) throws InterruptedException {
        boolean done = Rdr229RenameEngine.await(STAMP_WAIT, () -> engine.completion().gate1());
        var result = engine.completion();
        long stamps = engine.su(ctx -> ctx.fetchCount(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "data_moves")),
            org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("label"), String.class).like("rdr229-stamp-%")));
        if (stamps == 0) {
            throw new AssertionError("non-vacuity: the walk registered no rdr229-stamp-* move, so there is nothing to wait for");
        }
        if (!done) {
            throw new AssertionError("the stamp moves did not complete within " + STAMP_WAIT + "; incomplete: "
                + result.incomplete() + "; " + result.gate3Why());
        }
    }

    private static boolean awaitReadiness(Rdr229RenameEngine engine, Missing wanted, StringBuilder note, String step)
            throws InterruptedException {
        boolean ok = Rdr229RenameEngine.await(SIGNAL_WAIT, () -> engine.readiness().filter(wanted::equals).isPresent());
        if (!ok) {
            note.append(step).append(": readiness never reported ").append(wanted).append(" within ").append(SIGNAL_WAIT)
                .append(" (last ").append(engine.readiness().map(Object::toString).orElse("?")).append(", gate 3 ")
                .append(engine.completion().gate3() ? "holds" : "does not hold").append("). ");
        }
        return ok;
    }

    private static boolean awaitGate3AndReadiness(Rdr229RenameEngine engine, Missing wanted, StringBuilder note, String step)
            throws InterruptedException {
        Optional<Missing> last = Optional.empty();
        boolean ok = Rdr229RenameEngine.await(SIGNAL_WAIT,
            () -> engine.completion().gate3() && engine.readiness().filter(wanted::equals).isPresent());
        if (!ok) {
            last = engine.readiness();
            note.append(step).append(": gate 3 ").append(engine.completion().gate3() ? "holds" : "does not hold")
                .append(" and readiness is ").append(last.map(Object::toString).orElse("?")).append(", not ").append(wanted)
                .append(" after ").append(SIGNAL_WAIT).append(". ");
        }
        return ok;
    }
}
