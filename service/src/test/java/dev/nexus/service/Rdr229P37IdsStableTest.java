// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.RenameReadiness.Missing;
import dev.nexus.service.db.RenameGuard;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;

/**
 * RDR-229 residual S-7 (nexus-p2m1x.45, R6): the id-stability signal {@code ids_stable} is true exactly when the one-row regime holds: gate 3 is
 * latched and nothing is missing (the name-key drop move completed, every P3.5b changeset applied). It is derived from the same in-process state the
 * rename refusal reads ({@link RenameGuard#idsStable()}), so the field and the refusal cannot disagree, and it flips with no restart where none is
 * needed. Walked through the stages on the rename harness's engine.
 *
 * <p>The pairing with {@code catalog_epoch} on a response (the key is omitted, never null, when the epoch is unavailable) is carried by P3.5a's route
 * boundary, which is not on this base: no response emits either key here ({@link #idsStableAndCatalogEpochTravelTogetherOnAResponse}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37IdsStableTest {

    private Rdr229RenameEngine engine;
    private RenameGuard guard;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine().gate3(false);
        engine.boot();
        Rdr229RenameRestartPrecondition.awaitStampMovesComplete(engine);
        guard = engine.guard();
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    @Test
    void idsStableIsTrueOnlyOnceTheOneRowRegimeHolds_andStaysTrueThroughALiveDip() throws Exception {
        assertThat(guard.idsStable()).as("before gate 3 the rename is the name path: a new id").isFalse();

        engine.stopWorker();   // gate 3 latches, the drop move registers but cannot run
        engine.gate3(true);
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> engine.completionCheck().gate3Latched())).as("gate 3 latched").isTrue();
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> engine.moveRows(Rdr229RenameEngine.DROP_LABEL) == 1)).as("the move registered").isTrue();
        engine.completion();
        assertThat(engine.readiness().orElseThrow()).isEqualTo(Missing.NAME_KEY_DROP_MOVE);
        assertThat(guard.idsStable()).as("latched, the drop move incomplete: ids are not stable yet").isFalse();

        engine.startWorker();   // the move completes; P3.5b's changesets still wait for the restart
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(60), () -> engine.moveCompleted(Rdr229RenameEngine.DROP_LABEL))).isTrue();
        engine.completion();
        assertThat(engine.readiness().orElseThrow()).isEqualTo(Missing.P35B_RESTART);
        assertThat(guard.idsStable()).as("the move complete, a P3.5b changeset unapplied: not yet").isFalse();

        engine.restart();   // the restart applies P3.5b's changesets
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(60), () -> engine.readiness().filter(Missing.NONE::equals).isPresent())).isTrue();
        var restartedGuard = engine.guard();
        assertThat(restartedGuard.idsStable()).as("the one-row regime holds: ids are stable").isTrue();

        engine.gate3(false);   // an id index goes INVALID: the live gate 3 dips, the latch stays
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> !engine.completion().gate3())).isTrue();
        assertThat(restartedGuard.idsStable()).as("a live dip never makes ids unstable: the rename stays one-row").isTrue();
    }

    @Disabled("waits for P3.5a route boundary b97476687c: no response carries ids_stable or catalog_epoch on this base")
    @Test
    void idsStableAndCatalogEpochTravelTogetherOnAResponse() {
        // Wire contract (bead nexus-p2m1x.27, S-7; linda 2026-11-22): every authenticated per-collection response that carries ids_stable also carries
        // catalog_epoch where /v1/status catalog_epoch_available is true; where it is false the response carries ids_stable with no catalog_epoch
        // (the key omitted, never null). Both cases run against db/changelog-test/db.changelog-test-pgcontrol-*.xml
        // (PgContainerHelper.runSuperuserTestChangelog). Written when the route boundary lands.
        throw new AssertionError("enabled at the replay rebase");
    }
}
