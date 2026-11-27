// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import org.junit.jupiter.api.Test;

import java.util.Set;

/**
 * RDR-229 residual R9 (nexus-p2m1x.48), step 1: what a fresh install does with a rename across its first two boots, as the engine really walks it
 * (the harness runs the production id-unique-index pass as gate 3's extra; see {@link Rdr229RenameEngine}, which also states the one simulated
 * clause).
 *
 * <p><b>Boot 1.</b> The stamp moves complete (the tables are empty) but gate 3 cannot hold: the runtime-built id unique indexes wait for changesets
 * {@code vectors-078-8} and {@code vectors-084-1..3}, which are CONTINUE-gated on every stamp move being complete at walk time, and the boot-1 walk
 * skipped them. So there is no latch and no drop move, and a rename is the name path: it succeeds, with a new collection id and {@code ids_stable}
 * false. A fresh install is NOT refused in its first process. What the engine does is ask for the restart: {@code boot_work} lists
 * {@code rdr229-restart} (nx doctor shows it), while {@code rename_missing} reads {@code none} until gate 3 has latched.
 *
 * <p><b>Boot 2.</b> The walk applies those changesets and every P3.5b CONTINUE changeset together; gate 3 latches, an engine pass registers the
 * name-key drop move and the worker completes it. From the latch until then a rename is refused naming {@code name_key_drop_move}
 * ({@link Rdr229P37RenameStagesTest} holds the engine in that window). After it the rename is the one-row path: same id, {@code ids_stable} true.
 * The refusal value {@code restart} is never reached on this path; it is {@link Rdr229P37RestartValueTest}'s (a listed changeset that fails to apply).
 */
class Rdr229FreshInstallRenameRestartTest {

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    @Test
    void aFreshInstall_renamesOnTheNamePathInBoot1_asksForARestart_andRenamesOneRowAfterIt() throws Exception {
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
            long idBefore = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, first));
            Set<String> ids = Rdr229RenameEngine.p35bIds();
            assertThat(ids).as("non-vacuity: the P3.5b changeset list is not empty").isNotEmpty();

            // boot 1: gate 3 cannot hold, and the engine asks for the restart
            assertThat(Rdr229RenameEngine.await(Rdr229RenameRestartPrecondition.SIGNAL_WAIT, engine::restartEntryPresent))
                .as("boot 1: boot_work lists the restart entry (nx doctor shows it)").isTrue();
            var boot1 = engine.completion();
            assertThat(boot1.gate3()).as("boot 1: gate 3 does not hold [" + boot1.gate3Why() + "]").isFalse();
            assertThat(boot1.gate3Latched()).as("boot 1: no latch").isFalse();
            assertThat(boot1.gate3Why()).as("boot 1: gate 3 waits for the changesets the boot-1 walk skipped").contains("vectors-084-1");
            assertThat(engine.appliedChangesets().containsAll(ids)).as("boot 1 skipped P3.5b's CONTINUE changesets").isFalse();
            assertThat(engine.moveRows(Rdr229RenameEngine.DROP_LABEL)).as("boot 1: nothing registers the drop move before the latch").isZero();
            JsonNode status = engine.statusCompletion();
            assertThat(status.path("rename_missing").asText("absent"))
                .as("boot 1: rename_missing reads none until gate 3 has latched: " + status).isEqualTo("none");

            // boot 1: a rename is the name path and succeeds
            var renamed = engine.rename(token, first, coll("r9a", 2));
            assertThat(renamed.status()).as("boot 1: a fresh install's rename succeeds on the name path: " + renamed).isEqualTo(200);
            assertThat(engine.body(renamed).path("ids_stable").asBoolean(true)).as("boot 1: a new id, not stable").isFalse();
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, coll("r9a", 2))))
                .as("boot 1: the name path gives the collection a new id").isNotNull().isNotEqualTo(idBefore);

            // boot 2
            engine.restart();
            assertThat(Rdr229RenameEngine.await(Rdr229RenameRestartPrecondition.LATCH_WAIT,
                () -> engine.completion().gate3Latched() && engine.readiness().filter(Missing.NONE::equals).isPresent()))
                .as("boot 2: gate 3 latched, the drop move done, nothing missing; readiness " + engine.readiness().map(Object::toString)
                    .orElse("unreadable") + " [" + engine.completion().gate3Why() + "]").isTrue();
            assertThat(engine.appliedChangesets()).as("boot 2: databasechangelog holds every P3.5b CONTINUE changeset").containsAll(ids);
            assertThat(engine.appliedChangesets()).as("and the vectors-084 changesets gate 3 waited for")
                .contains("vectors-084-1", "vectors-084-2", "vectors-084-3");
            assertThat(engine.statusCompletion().path("rename_missing").asText("absent")).as("boot 2: nothing is missing").isEqualTo("none");
            assertThat(engine.restartEntryPresent()).as("boot 2: the restart entry has left boot_work").isFalse();
            assertThat(engine.moveRows(Rdr229RenameEngine.DROP_LABEL)).as("boot 2: the drop move was registered exactly once").isEqualTo(1);

            String second = coll("r9b", 1);
            engine.su(ctx -> {
                Rdr229RenameSeeds.chunksAndRing(ctx, tenant, second);
                return null;
            });
            long id2 = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, second));
            var one = engine.rename(token, second, coll("r9b", 2));
            assertThat(one.status()).as("boot 2: the rename succeeds: " + one).isEqualTo(200);
            assertThat(engine.body(one).path("ids_stable").asBoolean(false)).as("boot 2: the id survived").isTrue();
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, coll("r9b", 2))))
                .as("boot 2: the same id under the new label (one-row rename)").isEqualTo(id2);
        }
    }

    /**
     * The tripwire: the resource P3.5b's code writes exists and names at least one changeset. An empty list leaves the rename refused for ever
     * ({@code p35b_list_missing}, {@link Rdr229P37P35bListTest}).
     */
    @Test
    void theP35bChangesetListIsShippedAndNamesAtLeastOneChangeset() {
        assertThat(dev.nexus.service.datamove.P35bChangesets.resourceAbsent())
            .as("db/rdr229/p35b-continue-changesets.txt is on the classpath (P3.5b's code writes it)").isFalse();
        assertThat(dev.nexus.service.datamove.P35bChangesets.fromResource())
            .as("and it lists at least one P3.5b changeset id: an empty list leaves the rename refused for ever").isNotEmpty();
    }
}
