// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.RenameReadiness.Missing;
import org.junit.jupiter.api.Test;

import java.util.Set;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), stage-1 review items 4 and 9, on a booted engine: a build whose P3.5b changeset list is empty is refused
 * with a cause of its own ({@code p35b_list_missing}, never "restart": a restart cannot fix it), spelled the same on the 409, in
 * {@code GET /v1/status} {@code rename_missing} and in the in-process readiness. (That a {@code databasechangelog} row that is FAILED or SKIPPED does not
 * count as applied is {@link Rdr229P37RestartValueTest}'s.) The state is reached through the real chain: boot, restart, latch, drop move complete; only
 * then is the list the one thing missing.
 */
class Rdr229P37P35bListTest {

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    @Test
    void anEmptyP35bList_isRefusedAsP35bListMissing_notAsARestart_onTheRouteTheStatusAndInProcess() throws Exception {
        try (var engine = new Rdr229RenameEngine().p35b(Set.of())) {
            var env = Rdr229RenameRestartPrecondition.reach(engine, Missing.P35B_LIST_MISSING);
            env.reached().require();
            String tenant = "p37-lm";
            String token = engine.newTenant(tenant);
            String old = coll("p37lm", 1);
            engine.su(ctx -> {
                Rdr229RenameSeeds.chunksAndRing(ctx, tenant, old);
                return null;
            });
            assertThat(engine.moveCompleted(Rdr229RenameEngine.DROP_LABEL)).as("non-vacuity: the drop move completed, so the list is what is missing").isTrue();
            String why = " [readiness " + engine.readiness().map(Object::toString).orElse("?") + "]";

            var r = engine.rename(token, old, coll("p37lm", 2));

            assertThat(r.refused()).as("refused 409 rename_refused: " + r + why).isTrue();
            assertThat(r.missing()).as("the error names the missing list, not the restart" + why).isEqualTo("p35b_list_missing");
            assertThat(engine.statusCompletion().path("rename_missing").asText("absent"))
                .as("GET /v1/status spells it as the 409 does" + why).isEqualTo("p35b_list_missing");
            assertThat(engine.restartEntryPresent()).as("no restart entry: a restart cannot fix a missing list" + why).isFalse();
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, old))).as("nothing was renamed").isNotNull();
        }
    }
}
