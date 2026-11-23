// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), fix check A observation O-5: a name-path rename that is open when gate 3 latches. A rename that chose
 * the name path before the latch must not commit after it, because it gives the collection a new id the id readers have not seen. So the
 * whole rename runs inside {@code RenameReadiness#pinned}, and the latch writer ({@code RenameWindow#latchWriter}, P3.4's seam) waits for every
 * pinned rename to return before it writes the row and switches the one-row regime: while one is open there is no row and no regime.
 *
 * <p>The window is exercised directly (a pinned unit that waits on a latch, as a rename in its transaction would) and the rename route is
 * shown to go through it: every rename the route runs, name path or refused, counts one pinned call.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37RenameWindowPinTest {

    private Rdr229RenameEngine engine;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine().gate3(false);
        engine.boot();
        Rdr229RenameRestartPrecondition.awaitStampMovesComplete(engine);
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    private int latchRows() {
        return engine.su(ctx -> ctx.fetchCount(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "rdr229_gate_latch"))));
    }

    @Test
    void theGate3LatchIsNotWrittenWhileAPinnedRenameIsOpen_andIsWrittenOnceItReturns() throws Exception {
        assertThat(engine.completion().gate3()).as("non-vacuity: gate 3 does not hold before the flip").isFalse();
        assertThat(latchRows()).as("non-vacuity: no latch row yet").isZero();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        CountDownLatch inside = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        var readiness = engine.completionCheck().renameReadiness();
        try {
            // a rename that began under the name path: the regime is off and it is still in its transaction
            Future<Boolean> open = pool.submit(() -> engine.pinned(() -> {
                boolean sawLatched = readiness.gate3Latched();
                inside.countDown();
                try {
                    release.await(60, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                return sawLatched || readiness.gate3Latched();   // true if the latch was written under it
            }));
            assertThat(inside.await(30, TimeUnit.SECONDS)).as("the pinned rename is open").isTrue();

            // P3.4 completes: the input of gate 3 opens and the check samples; its latch writer waits for the open rename
            engine.gate3(true);
            Future<Boolean> flip = pool.submit(() -> engine.completion().gate3Latched());

            Rdr229RenameEngine.await(Duration.ofSeconds(2), () -> latchRows() > 0 || flip.isDone() || readiness.gate3Latched());
            assertThat(latchRows()).as("the latch waits for the open rename: no row").isZero();
            // linda 2026-11-23 (a): the drop move is registered only once the latch is written, so with the live gate 3 holding and no row there is no move
            // and the readiness is not NONE
            assertThat(engine.completionCheck().latest().gate3()).as("non-vacuity: the live gate 3 holds").isTrue();
            assertThat(engine.moveRows(Rdr229RenameEngine.DROP_LABEL)).as("no drop move is registered without the latch row").isZero();
            assertThat(engine.completionCheck().renameReadiness().missing()).as("and the readiness is not NONE").isNotEqualTo(dev.nexus.service.datamove.RenameReadiness.Missing.NONE);
            assertThat(readiness.gate3Latched()).as("no regime").isFalse();
            assertThat(flip.isDone()).as("and the sample has not returned: " + (flip.isDone() ? flip.resultNow() : "")).isFalse();

            release.countDown();
            assertThat(open.get(30, TimeUnit.SECONDS)).as("the open rename never saw the latch").isFalse();
            assertThat(flip.get(30, TimeUnit.SECONDS)).as("the sample that waited saw the latch written").isTrue();
            assertThat(latchRows()).as("one row, written once the rename returned").isEqualTo(1);
            assertThat(engine.<Boolean>pinned(readiness::gate3Latched)).as("a rename that starts after the latch is in the one-row regime").isTrue();
        } finally {
            release.countDown();
            pool.shutdownNow();
        }
    }

    @Test
    void theRenameRouteRunsInsideTheWindow_evenWhenItIsRefusedFromTheLatch() throws Exception {
        // the first test flipped gate 3 and so latched it: closing the live input now does not return a rename to the name path
        // (that path, and its pinning, is Rdr229P37RenameWindowTest stage 1, on an engine whose gate 3 never opened)
        engine.gate3(false);
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> !engine.completion().gate3()))
            .as("the live gate 3 is closed").isTrue();
        assertThat(engine.completion().gate3Latched()).as("but the latch holds").isTrue();
        String tenant = "p37-pin";
        String token = engine.newTenant(tenant);
        String oldName = "knowledge__p37pin__minilm-l6-v2-384__v1";
        engine.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, tenant, oldName);
            return null;
        });
        int before = engine.pinnedRenames();

        var r = engine.rename(token, oldName, "knowledge__p37pin__minilm-l6-v2-384__v2");

        assertThat(r.refused()).as("from the latch a rename is one-row or refused, never the name path; here the drop move or the restart is awaited: " + r)
            .isTrue();
        assertThat(engine.pinnedRenames() - before).as("the rename ran inside the window, once, refused or not").isEqualTo(1);
    }
}
