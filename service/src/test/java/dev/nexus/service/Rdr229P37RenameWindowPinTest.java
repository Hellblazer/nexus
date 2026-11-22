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
 * RDR-229 P3.7 (nexus-p2m1x.27), fix check A observation O-5: a name-path rename that is open when gate 3 flips to true in process. A
 * rename that chose the name path before gate 3 held must not commit after the id readers switched, because it gives the collection a
 * new id they have not seen. So the whole rename runs inside {@code RenameReadiness#pinned}, and the completion check holds gate 3's flip
 * until every pinned rename has returned: the readers keep reading gate 3 as false while one is open, and read it as true once none is.
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

    @Test
    void gate3DoesNotFlipToTrueWhileAPinnedRenameIsOpen_andFlipsOnceItReturns() throws Exception {
        assertThat(engine.completion().gate3()).as("non-vacuity: gate 3 does not hold before the flip").isFalse();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        CountDownLatch inside = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        try {
            // a rename that began under the name path: it reads gate 3 as false and is still in its transaction
            Future<Boolean> open = pool.submit(() -> engine.pinned(() -> {
                boolean sawGate3 = engine.gate3Holds();
                inside.countDown();
                try {
                    release.await(60, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                return sawGate3 || engine.gate3Holds();   // true if gate 3 flipped under it
            }));
            assertThat(inside.await(30, TimeUnit.SECONDS)).as("the pinned rename is open").isTrue();

            // P3.4 completes: the input of gate 3 opens and the check samples
            engine.gate3(true);
            Future<Boolean> flip = pool.submit(() -> engine.completion().gate3());

            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(2), () -> engine.gate3Holds() || flip.isDone()))
                .as("the flip waits for the open rename: gate 3 still reads false and the sample has not returned").isFalse();

            release.countDown();
            assertThat(open.get(30, TimeUnit.SECONDS)).as("the open rename never saw gate 3 hold").isFalse();
            assertThat(flip.get(30, TimeUnit.SECONDS)).as("the sample that waited saw gate 3 hold").isTrue();
            assertThat(engine.gate3Holds()).as("a reader now switches to the id").isTrue();
            assertThat(engine.<Boolean>pinned(engine::gate3Holds)).as("a rename that starts after the flip reads gate 3 as true").isTrue();
        } finally {
            release.countDown();
            pool.shutdownNow();
        }
    }

    @Test
    void theRenameRouteRunsInsideTheWindow() throws Exception {
        engine.gate3(false);
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> !engine.completion().gate3()))
            .as("gate 3 is closed for the name path").isTrue();
        String tenant = "p37-pin";
        String token = engine.newTenant(tenant);
        String oldName = "knowledge__p37pin__minilm-l6-v2-384__v1";
        engine.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, tenant, oldName);
            return null;
        });
        int before = engine.pinnedRenames();

        var r = engine.rename(token, oldName, "knowledge__p37pin__minilm-l6-v2-384__v2");

        assertThat(r.status()).as("the name path before gate 3: " + r).isEqualTo(200);
        assertThat(engine.pinnedRenames() - before).as("the rename ran inside the window, once").isEqualTo(1);
    }
}
