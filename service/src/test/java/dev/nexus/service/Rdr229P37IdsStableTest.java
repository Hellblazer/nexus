// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.RenameReadiness.Missing;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;

/**
 * RDR-229 residual S-7 (nexus-p2m1x.45, R6): the rename route's {@code ids_stable} is true exactly when the rename that answered it was the one-row
 * rename (the id survived), and false when it was the name path (a new id). Walked through the stages on the rename harness's engine with REAL renames,
 * read from the route's own answer: before gate 3 the name path says false; latched with the move or a P3.5b changeset missing the rename is refused
 * (there is no answer); after the restart that applies the changesets the one-row rename says true, and a live dip of gate 3 (an id index going INVALID)
 * does not turn it back, because the rename path follows the latch row.
 *
 * <p>The pairing with {@code catalog_epoch}: every rename answer that carries {@code ids_stable} carries {@code catalog_epoch} where {@code /v1/status}
 * says {@code catalog_epoch_available}, and where it does not carries {@code ids_stable} with no {@code catalog_epoch} key (omitted, never null).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37IdsStableTest {

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

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    /** A tenant with one seeded collection {@code owner} v1; renames it to v2 and answers the route's reply. */
    private Rdr229RenameEngine.Renamed renameOne(String tenant, String owner) throws Exception {
        String token = engine.newTenant(tenant);
        engine.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, tenant, coll(owner, 1));
            return null;
        });
        return engine.rename(token, coll(owner, 1), coll(owner, 2));
    }

    @Test
    void idsStableFollowsThePathTheRenameTook_andAPairedCatalogEpochTravelsWithIt() throws Exception {
        var before = renameOne("ids-a", "idsa");
        assertThat(before.status()).as("before gate 3 the rename is the name path: " + before).isEqualTo(200);
        assertThat(engine.body(before).path("ids_stable").asBoolean(true)).as("a new id: not stable").isFalse();
        assertThat(engine.body(before).has("collection_id")).as("the route still names the renamed row").isTrue();

        engine.stopWorker();   // gate 3 latches, the drop move registers but cannot run
        engine.gate3(true);
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> engine.completion().gate3Latched())).as("gate 3 latched [" + engine.completion().gate3Why() + "]").isTrue();
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> engine.moveRows(Rdr229RenameEngine.DROP_LABEL) == 1)).as("the move registered").isTrue();
        engine.completion();
        assertThat(engine.readiness().orElseThrow()).isEqualTo(Missing.NAME_KEY_DROP_MOVE);
        var waiting = renameOne("ids-b", "idsb");
        assertThat(waiting.refused()).as("latched, the drop move incomplete: refused, so there is no ids_stable at all: " + waiting).isTrue();
        assertThat(waiting.missing()).isEqualTo("name_key_drop_move");

        engine.startWorker();   // the move completes; P3.5b's changesets still wait for the restart
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(60), () -> engine.moveCompleted(Rdr229RenameEngine.DROP_LABEL))).isTrue();
        engine.completion();
        assertThat(engine.readiness().orElseThrow()).isEqualTo(Missing.P35B_RESTART);

        engine.restart();   // the restart applies P3.5b's changesets
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(60), () -> engine.readiness().filter(Missing.NONE::equals).isPresent())).isTrue();
        var stable = renameOne("ids-c", "idsc");
        assertThat(stable.status()).as("the one-row regime holds: " + stable).isEqualTo(200);
        assertThat(engine.body(stable).path("ids_stable").asBoolean(false)).as("the id survived: stable").isTrue();
        assertCatalogEpochPairing(stable);

        engine.gate3(false);   // an id index goes INVALID: the live gate 3 dips, the latch stays
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> !engine.completion().gate3())).isTrue();
        var dipped = renameOne("ids-d", "idsd");
        assertThat(dipped.status()).as("a live dip does not send the rename back to the name path: " + dipped).isEqualTo(200);
        assertThat(engine.body(dipped).path("ids_stable").asBoolean(false)).isTrue();
        assertCatalogEpochPairing(dipped);
    }

    private void assertCatalogEpochPairing(Rdr229RenameEngine.Renamed r) throws Exception {
        var body = engine.body(r);
        assertThat(body.has("ids_stable")).as("non-vacuity: the answer carries ids_stable").isTrue();
        if (engine.catalogEpochAvailable()) {
            assertThat(body.hasNonNull("catalog_epoch")).as("catalog_epoch_available: the answer carries the epoch beside ids_stable: " + r).isTrue();
        } else {
            assertThat(body.has("catalog_epoch")).as("the epoch is unavailable: the key is omitted, never null: " + r).isFalse();
        }
    }
}
