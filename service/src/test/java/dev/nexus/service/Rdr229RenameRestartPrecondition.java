// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.datamove.RenameReadiness;
import dev.nexus.service.datamove.RenameReadiness.Missing;

import java.time.Duration;
import java.util.Optional;

/**
 * RDR-229 residual R9 (nexus-p2m1x.48): the precondition every rename test takes on a fresh or half-stamped fixture against the
 * P3.5/P3.7 cut, as the engine really walks it (measured on chas 2026-11-27 with the production {@code IdUniqueIndexPass} as gate 3's extra):
 * <ol>
 * <li><b>Boot 1.</b> The stamp moves complete (a fresh store's tables are empty), but gate 3 cannot hold: its runtime-built id unique indexes need
 *     changesets {@code vectors-078-8} and {@code vectors-084-1..3}, which are CONTINUE-gated on every stamp move being complete at walk time, so
 *     the boot-1 walk skipped them. No latch, no drop move. A rename in this process is the name path and succeeds (a new id); the engine only
 *     asks for a restart ({@code rdr229-restart} in {@code boot_work}, which nx doctor shows).</li>
 * <li><b>The restart (boot 2).</b> Its walk applies those changesets and every P3.5b CONTINUE changeset together. Gate 3 latches once the id-index
 *     pass has built what it waits for, an engine pass registers the name-key drop move, the worker completes it, and the readiness reads none.
 *     Until then a rename is refused naming {@code name_key_drop_move}.</li>
 * <li>Only then does a rename run the one-row path.</li>
 * </ol>
 * So {@link #reach}: boot; wait for the stamp moves; restart; wait for gate 3 to latch and readiness {@link Missing#NONE}; hand the engine back.
 * The wire view of the readiness is {@code GET /v1/status} {@code rdr229_completion.rename_missing}. Gate 3's PCI-coverage clause stays simulated
 * at its input ({@link Rdr229RenameEngine}): the harness has no vector repository, and a store with nothing to index satisfies it in production too.
 *
 * <p><b>What a wait that times out means.</b> The stamp wait is hard: the stamp moves run, so a timeout there is a fixture fault and fails with the
 * incomplete labels. The readiness wait is bounded and soft, and returns {@link Reached#note()} saying which state never came. A caller puts that note
 * in the description of its first assertion, or calls {@link Reached#require}.
 */
final class Rdr229RenameRestartPrecondition {

    /** How long the stamp moves may take on an empty store (about 25 moves, each a few hundred milliseconds). */
    static final Duration STAMP_WAIT = Duration.ofMinutes(3);
    /** How long a readiness state may take to appear after the event that causes it (a completion event re-reads the check at once). */
    static final Duration SIGNAL_WAIT = Duration.ofSeconds(30);
    /** How long the second boot may take to latch gate 3 and finish the drop move (the harness ticks the id-index pass every 200 ms; the move sleeps 8 s a chunk). */
    static final Duration LATCH_WAIT = Duration.ofSeconds(120);

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
     * Boots {@code engine} (not yet booted), waits for the stamp moves, restarts, and waits for readiness NONE with gate 3 latched: the state a
     * one-row rename needs. The engine's simulated PCI input must be open.
     */
    static Env reach(Rdr229RenameEngine engine) throws Exception {
        return reach(engine, Missing.NONE);
    }

    /** As {@link #reach(Rdr229RenameEngine)} but waits, after the restart, for readiness {@code wanted} with gate 3 latched (a test of a refusal window). */
    static Env reach(Rdr229RenameEngine engine, Missing wanted) throws Exception {
        engine.boot();
        awaitStampMovesComplete(engine);
        engine.restart();
        StringBuilder note = new StringBuilder();
        boolean ready = awaitLatchedReadiness(engine, wanted, note, "boot 2");
        return new Env(engine, new Reached(ready, ready ? "" : note.toString().strip()));
    }

    /**
     * Hard wait: every {@code rdr229-stamp-*} move has {@code completed_at} and gates 1 and 2 hold, within {@link #STAMP_WAIT}. On the P3.4 base
     * the boot read set includes P3.3c's swap moves, which the harness's LeafIndexPass registers once gate 1 holds.
     */
    static void awaitStampMovesComplete(Rdr229RenameEngine engine) throws InterruptedException {
        boolean done = Rdr229RenameEngine.await(STAMP_WAIT, () -> engine.completion().gate2());
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

    private static boolean awaitLatchedReadiness(Rdr229RenameEngine engine, Missing wanted, StringBuilder note, String step)
            throws InterruptedException {
        boolean ok = Rdr229RenameEngine.await(LATCH_WAIT,
            () -> engine.completion().gate3Latched() && engine.readiness().filter(wanted::equals).isPresent());
        if (!ok) {
            Optional<Missing> last = engine.readiness();
            note.append(step).append(": gate 3 ").append(engine.completion().gate3Latched() ? "is latched" : "is not latched").append(" [")
                .append(engine.completion().gate3Why()).append("] and readiness is ").append(last.map(Object::toString).orElse("?"))
                .append(", not ").append(wanted).append(" after ").append(LATCH_WAIT).append(". ");
        }
        return ok;
    }
}
