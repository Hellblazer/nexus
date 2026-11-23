// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.RenameReadiness.Missing;
import org.junit.jupiter.api.Test;

import java.time.Duration;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), critic r2 item 1, over P3.4's durable latch (vectors-080, {@code nexus.rdr229_gate_latch}). After a restart with
 * the name-key drop move complete and the live gate 3 dipped, a rename must still never take the name path (the name keys are gone).
 * {@link #afterARestart_theLatchRowAloneKeepsTheOneRowRegime_theBeltIsNotNeeded} is the restart test with vectors-080: the row survives the restart and the
 * rename is one-row. {@link #aLostLatchRowWithACompletedDropMove_doesNotKeepTheOneRowRegime} pins that the belt is gone: the
 * latch row alone decides.
 */
class Rdr229P37RestartBeltTest {

    @Test
    void afterARestart_theLatchRowAloneKeepsTheOneRowRegime_theBeltIsNotNeeded() throws Exception {
        try (var engine = new Rdr229RenameEngine()) {
            Rdr229RenameRestartPrecondition.reach(engine).reached().require();
            assertThat(engine.moveCompleted(Rdr229RenameEngine.DROP_LABEL)).as("non-vacuity: the drop move completed").isTrue();
            String tenant = "p37-restart";
            String token = engine.newTenant(tenant);
            String old = "knowledge__p37restart__minilm-l6-v2-384__v1";
            engine.su(ctx -> {
                PgContainerHelper.insertCollection(ctx, tenant, old);
                return null;
            });
            long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, old));
            assertThat(engine.completion().gate3Latched()).as("non-vacuity: the latch row was written before the restart").isTrue();

            engine.gate3(false);   // the input the next boot's check reads: P3.4's coverage is gone
            engine.restart();
            assertThat(engine.completionCheck().gate3Latched()).as("known at construction, from the row, before any sample").isTrue();
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> engine.readiness().filter(Missing.NONE::equals).isPresent()))
                .as("readiness settles").isTrue();
            var r = engine.completion();
            assertThat(r.gate3()).as("non-vacuity: the live gate 3 does not hold after the restart").isFalse();
            assertThat(r.gate3Latched()).as("the latch row survived the restart: the belt (the completed drop move) is not what carries the regime").isTrue();

            var renamed = engine.rename(token, old, "knowledge__p37restart__minilm-l6-v2-384__v2");

            assertThat(renamed.status()).as("the rename route: " + renamed).isEqualTo(200);
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, "knowledge__p37restart__minilm-l6-v2-384__v2")))
                .as("one-row: the collection keeps its id (the name path would give a new one)").isEqualTo(id);
        }
    }

    /**
     * NOTE (linda Q-B): when slice C's {@code runRename} lands, a lost latch row with a completed drop move is no longer the name path: the answer flips to the
     * fail-closed {@code latch_row_missing} refusal, and the final assertion below is re-pinned against {@code runRename} at the replay rebase.
     *
     * <p>linda 2026-11-23: the belt is removed. A latch row that is gone (a partial restore) with the drop move complete does NOT keep the one-row regime:
     * the latch row alone decides, so such an install is back at the name path until gate 3 latches again.
     */
    @Test
    void aLostLatchRowWithACompletedDropMove_doesNotKeepTheOneRowRegime() throws Exception {
        try (var engine = new Rdr229RenameEngine()) {
            Rdr229RenameRestartPrecondition.reach(engine).reached().require();
            assertThat(engine.moveCompleted(Rdr229RenameEngine.DROP_LABEL)).as("non-vacuity: the drop move completed").isTrue();
            engine.su(ctx -> ctx.deleteFrom(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "rdr229_gate_latch"))).execute());   // a restore that lost the row
            engine.gate3(false);   // so the next boot's check cannot latch again
            engine.restart();
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> engine.completion().rename().dropMoveComplete()))
                .as("non-vacuity: the completed move is seen again after the restart").isTrue();
            var r = engine.completion();
            assertThat(r.gate3()).as("non-vacuity: the live gate 3 does not hold").isFalse();
            assertThat(r.gate3Latched()).as("non-vacuity: the latch row is gone").isFalse();

            var readiness = engine.completionCheck().renameReadiness();
            assertThat(readiness.gate3LatchedUnderLock()).as("the row decides: absent").isFalse();
            assertThat(new dev.nexus.service.db.RenameGuard(dev.nexus.service.datamove.Gate3.never(), readiness).<Boolean>pinned(oneRow -> oneRow))
                .as("the completed drop move does not keep the one-row regime: the name path").isFalse();
        }
    }
}
