// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.RenameReadiness.Missing;
import org.junit.jupiter.api.Test;

import java.time.Duration;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), critic r2 item 1: the gate 3 latch is process-local until vectors-080, so a restart resets it. After a
 * restart with the name-key drop move complete and the live gate 3 dipped, a rename must still never take the name path (the name keys
 * are gone): the completed move counts as latched.
 */
class Rdr229P37RestartBeltTest {

    @Test
    void afterARestartWithTheMoveCompleteAndTheLiveGate3Dipped_aRenameIsOneRow_neverTheNamePath() throws Exception {
        try (var engine = new Rdr229RenameEngine()) {
            Rdr229RenameRestartPrecondition.reach(engine).reached().require();
            assertThat(engine.moveCompleted(Rdr229RenameEngine.DROP_LABEL)).as("non-vacuity: the drop move completed").isTrue();
            String tenant = "p37-belt";
            String token = engine.newTenant(tenant);
            String old = "knowledge__p37belt__minilm-l6-v2-384__v1";
            engine.su(ctx -> {
                PgContainerHelper.insertCollection(ctx, tenant, old);
                return null;
            });
            long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, old));

            engine.gate3(false);   // the input the next boot's check reads: P3.4's coverage is gone
            engine.restart();
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> engine.readiness().filter(Missing.NONE::equals).isPresent()))
                .as("readiness settles").isTrue();
            var r = engine.completion();
            assertThat(r.gate3()).as("non-vacuity: the live gate 3 does not hold after the restart").isFalse();
            assertThat(r.gate3Latched()).as("non-vacuity: the process-local latch was reset by the restart").isFalse();

            var renamed = engine.rename(token, old, "knowledge__p37belt__minilm-l6-v2-384__v2");

            assertThat(renamed.status()).as("the rename route: " + renamed).isEqualTo(200);
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, "knowledge__p37belt__minilm-l6-v2-384__v2")))
                .as("one-row: the collection keeps its id (the name path would give a new one)").isEqualTo(id);
        }
    }
}
