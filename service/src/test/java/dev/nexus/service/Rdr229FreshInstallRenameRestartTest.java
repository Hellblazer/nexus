// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import org.junit.jupiter.api.Test;

import java.util.Set;

/**
 * RDR-229 residual R9 (nexus-p2m1x.48), step 1: a fresh install refuses a rename until its second boot. Boot 1 on an empty
 * database: the stamp moves complete (their tables are empty), gate 3 holds in that process, the name-key drop move runs, and P3.5b's
 * CONTINUE changesets were skipped by the walk before any stamp had completed. A rename is then refused 409 {@code rename_refused}
 * naming the restart, and the restart signal is visible on {@code GET /v1/status}. After the restart the changelog holds every id of
 * {@code db/rdr229/p35b-continue-changesets.txt} and the rename succeeds.
 *
 * <p>One test, staged, because each stage is the state the next one starts from. The refusal is asserted before the signal and the
 * changelog list, so on a tree without P3.7 it fails on the rename's behaviour first.
 */
class Rdr229FreshInstallRenameRestartTest {

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    /** The P3.5b changeset ids the engine waits for: P3.5b's list, or the harness's fixture while that list is absent. */
    private static Set<String> p35bIds() {
        return Rdr229RenameEngine.p35bIds();
    }

    @Test
    void aFreshInstall_refusesARenameNamingTheRestart_untilItsSecondBoot() throws Exception {
        try (var engine = new Rdr229RenameEngine()) {
            engine.boot();
            Rdr229RenameRestartPrecondition.awaitStampMovesComplete(engine);
            String tenant = "r9-fresh";
            String token = engine.newTenant(tenant);
            String first = coll("r9a", 1);
            engine.su(ctx -> {
                Rdr229RenameSeeds.chunksAndRing(ctx, tenant, first);
                return null;
            });
            // soft: the signal and the move are P3.7's; a tree without them fails on the rename below
            boolean signalled = Rdr229RenameEngine.await(Rdr229RenameRestartPrecondition.SIGNAL_WAIT,
                () -> engine.readiness().filter(Missing.P35B_RESTART::equals).isPresent());
            String why = " [restart signal " + (signalled ? "seen" : "never seen within "
                + Rdr229RenameRestartPrecondition.SIGNAL_WAIT) + "; readiness " + engine.readiness().map(Object::toString)
                .orElse("unreadable") + "; gate 3 " + (engine.completion().gate3() ? "holds" : "does not hold") + "]"
               ;

            // boot 1: refused, naming the restart
            var refused = engine.rename(token, first, coll("r9a", 2));
            assertThat(refused.refused()).as("boot 1: a rename is refused 409 rename_refused: " + refused + why).isTrue();
            assertThat(refused.missing()).as("boot 1: the error names the restart" + why).isEqualTo("restart");
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, first)))
                .as("boot 1: the refused rename wrote nothing" + why).isNotNull();

            // boot_work tells the operator to restart (nx doctor), and the drop move was registered exactly once
            assertThat(engine.restartEntryPresent()).as("boot 1: boot_work lists the restart entry" + why).isTrue();
            assertThat(engine.moveRows(Rdr229RenameEngine.DROP_LABEL)).as("boot 1: the drop move is registered once" + why).isEqualTo(1);

            // the restart signal is visible where an operator reads it
            JsonNode status = engine.statusCompletion();
            assertThat(status.path("rename_missing").asText("absent"))
                .as("GET /v1/status rdr229_completion.rename_missing says a restart is needed: " + status + why)
                .isEqualTo("restart");

            // P3.5b's changesets: the list is not empty, and boot 1 applied none of them
            Set<String> ids = p35bIds();
            assertThat(ids).as("non-vacuity: the P3.5b changeset list is not empty").isNotEmpty();
            assertThat(engine.appliedChangesets().containsAll(ids))
                .as("boot 1 skipped P3.5b's CONTINUE changesets: not all of them are recorded").isFalse();

            // boot 2
            engine.restart();
            assertThat(Rdr229RenameEngine.await(Rdr229RenameRestartPrecondition.SIGNAL_WAIT,
                () -> engine.completion().gate3() && engine.readiness().filter(Missing.NONE::equals).isPresent()))
                .as("boot 2: gate 3 holds and nothing is missing; readiness " + engine.readiness().map(Object::toString)
                    .orElse("unreadable")).isTrue();
            assertThat(engine.appliedChangesets()).as("boot 2: databasechangelog holds every P3.5b CONTINUE changeset")
                .containsAll(ids);
            assertThat(engine.statusCompletion().path("rename_missing").asText("absent"))
                .as("boot 2: the status no longer asks for a restart").isEqualTo("none");
            assertThat(engine.restartEntryPresent()).as("boot 2: the restart entry has left boot_work").isFalse();
            assertThat(engine.moveRows(Rdr229RenameEngine.DROP_LABEL)).as("boot 2: the reboot registered nothing more").isEqualTo(1);

            String second = coll("r9b", 1);
            engine.su(ctx -> {
                Rdr229RenameSeeds.chunksAndRing(ctx, tenant, second);
                return null;
            });
            var renamed = engine.rename(token, second, coll("r9b", 2));
            assertThat(renamed.status()).as("boot 2: the rename succeeds: " + renamed).isEqualTo(200);
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, coll("r9b", 2))))
                .as("boot 2: the renamed collection is registered under its new label").isNotNull();
        }
    }
}
