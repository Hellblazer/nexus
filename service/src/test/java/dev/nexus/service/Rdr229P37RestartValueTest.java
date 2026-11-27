// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.RenameReadiness.Missing;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.Set;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), the rename refusal's {@code restart} value after the gate 3 latch (RDR item 6; fix check
 * 92fbbaa966, critic 1): once gate 3 has latched and the name-key drop move has {@code completed_at}, the one thing left is a listed P3.5b
 * changeset that has not applied. All twelve apply in one boot, so the state is made here by taking one real changeset back to FAILED in
 * {@code databasechangelog} on a store that has had its restart. The refusal, the status field and the restart {@code boot_work} entry must all
 * say restart, and writing the row back to EXECUTED must clear all three with no restart.
 */
class Rdr229P37RestartValueTest {

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    private static void setExecType(Rdr229RenameEngine engine, String id, String execType) {
        int n = engine.su(ctx -> ctx.execute("update public.databasechangelog set exectype = ? where id = ?", execType, id));
        assertThat(n).as("non-vacuity: the changeset " + id + " has a databasechangelog row").isPositive();
    }

    @Test
    void afterTheLatchAndTheDropMove_oneUnappliedP35bChangeset_isRefusedNamingTheRestart_andClearsWhenItApplies() throws Exception {
        try (var engine = new Rdr229RenameEngine()) {
            var env = Rdr229RenameRestartPrecondition.reach(engine);
            env.reached().require();
            assertThat(engine.moveCompleted(Rdr229RenameEngine.DROP_LABEL)).as("non-vacuity: the drop move completed").isTrue();
            assertThat(engine.completion().gate3Latched()).as("non-vacuity: gate 3 latched").isTrue();
            Set<String> ids = Rdr229RenameEngine.p35bIds();
            assertThat(ids).as("non-vacuity: the real P3.5b list, not the harness fixture").isNotEmpty();
            assertThat(Rdr229RenameEngine.usingFixtureList()).isFalse();
            String one = ids.stream().sorted().findFirst().orElseThrow();

            String tenant = "p37-rv";
            String token = engine.newTenant(tenant);
            String old = coll("p37rv", 1);
            engine.su(ctx -> {
                Rdr229RenameSeeds.chunksAndRing(ctx, tenant, old);
                return null;
            });
            assertThat(engine.readiness()).as("non-vacuity: the store is ready").contains(Missing.NONE);

            setExecType(engine, one, "FAILED");
            try {
                assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> engine.readiness().filter(Missing.P35B_RESTART::equals).isPresent()
                    && engine.restartEntryPresent())).as("readiness says restart and boot_work lists the restart entry; readiness "
                        + engine.readiness().map(Object::toString).orElse("?")).isTrue();
                var refused = engine.rename(token, old, coll("p37rv", 2));
                assertThat(refused.refused()).as("refused 409 rename_refused: " + refused).isTrue();
                assertThat(refused.missing()).as("the error names the restart, not the move").isEqualTo("restart");
                assertThat(engine.statusCompletion().path("rename_missing").asText("absent")).as("GET /v1/status says restart").isEqualTo("restart");
                assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, old))).as("nothing was renamed").isNotNull();
            } finally {
                setExecType(engine, one, "EXECUTED");
            }

            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> engine.readiness().filter(Missing.NONE::equals).isPresent()
                && !engine.restartEntryPresent())).as("the changeset applied: readiness is none and the restart entry is gone").isTrue();
            var renamed = engine.rename(token, old, coll("p37rv", 2));
            assertThat(renamed.status()).as("and the rename succeeds with no restart: " + renamed).isEqualTo(200);
        }
    }
}
