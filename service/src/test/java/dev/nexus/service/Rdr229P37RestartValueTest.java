// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.RenameReadiness.Missing;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;

import java.time.Duration;
import java.util.Set;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), the only home of the rename refusal's {@code restart} value (RDR item 6; fix check 92fbbaa966, critic 1).
 * <b>This is the failed-changeset case.</b> On the real chain a fresh or half-stamped store never shows {@code restart}: the restart's walk applies
 * every listed P3.5b changeset together with the vectors-078/084 changesets that unlock gate 3, so after the latch the drop move is the only thing
 * left ({@link Rdr229FreshInstallRenameRestartTest}, {@link Rdr229P37RenameStagesTest}). The value is reachable only when one listed changeset
 * fails to apply on its own. It is made here on a store that has had its restart and whose drop move completed, by taking one real changeset
 * back to FAILED (or SKIPPED) in {@code databasechangelog}. The refusal, the status field and the {@code rdr229-restart} {@code boot_work} entry
 * must all say restart, and writing the row back to EXECUTED must clear all three with no restart. Only EXECUTED, RERAN and MARK_RAN count as applied.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class Rdr229P37RestartValueTest {

    private Rdr229RenameEngine engine;
    private String one;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine();
        Rdr229RenameRestartPrecondition.reach(engine).reached().require();
        assertThat(engine.moveCompleted(Rdr229RenameEngine.DROP_LABEL)).as("non-vacuity: the drop move completed").isTrue();
        assertThat(engine.completion().gate3Latched()).as("non-vacuity: gate 3 latched").isTrue();
        Set<String> ids = Rdr229RenameEngine.p35bIds();
        assertThat(ids).as("non-vacuity: the real P3.5b list").isNotEmpty();
        one = ids.stream().sorted().findFirst().orElseThrow();
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

    private void setExecType(String execType) {
        var log = org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("public", "databasechangelog"));
        int n = engine.su(ctx -> ctx.update(log).set(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("exectype"), String.class), execType)
            .where(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("id"), String.class).eq(one)).execute());
        assertThat(n).as("non-vacuity: the changeset " + one + " has a databasechangelog row").isPositive();
    }

    private boolean readinessIs(Missing wanted) {
        return engine.readiness().filter(wanted::equals).isPresent();
    }

    @Test
    @Order(1)
    void afterTheLatchAndTheDropMove_oneUnappliedP35bChangeset_isRefusedNamingTheRestart_andClearsWhenItApplies() throws Exception {
        String tenant = "p37-rv";
        String token = engine.newTenant(tenant);
        String old = coll("p37rv", 1);
        engine.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, tenant, old);
            return null;
        });
        assertThat(engine.readiness()).as("non-vacuity: the store is ready").contains(Missing.NONE);

        setExecType("FAILED");
        try {
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> readinessIs(Missing.P35B_RESTART) && engine.restartEntryPresent()))
                .as("readiness says restart and boot_work lists the restart entry; readiness "
                    + engine.readiness().map(Object::toString).orElse("?")).isTrue();
            var refused = engine.rename(token, old, coll("p37rv", 2));
            assertThat(refused.refused()).as("refused 409 rename_refused: " + refused).isTrue();
            assertThat(refused.missing()).as("the error names the restart, not the move").isEqualTo("restart");
            assertThat(engine.statusCompletion().path("rename_missing").asText("absent")).as("GET /v1/status says restart").isEqualTo("restart");
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, old))).as("nothing was renamed").isNotNull();
        } finally {
            setExecType("EXECUTED");
        }

        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> readinessIs(Missing.NONE) && !engine.restartEntryPresent()))
            .as("the changeset applied: readiness is none and the restart entry is gone").isTrue();
        var renamed = engine.rename(token, old, coll("p37rv", 2));
        assertThat(renamed.status()).as("and the rename succeeds with no restart: " + renamed).isEqualTo(200);
    }

    @Test
    @Order(2)
    void aFailedOrSkippedChangelogRowIsNotApplied_onlyExecutedRerunAndMarkRanAre() throws Exception {
        for (String execType : new String[] {"FAILED", "SKIPPED"}) {
            setExecType(execType);
            try {
                assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> readinessIs(Missing.P35B_RESTART)))
                    .as("a " + execType + " row for a listed changeset is not an applied changeset").isTrue();
                assertThat(engine.completion().rename().missing()).as(execType + ": that id is missing").containsExactly(one);
            } finally {
                setExecType("EXECUTED");
            }
        }
        for (String execType : new String[] {"EXECUTED", "RERAN", "MARK_RAN"}) {
            setExecType(execType);
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> readinessIs(Missing.NONE)))
                .as("control: a " + execType + " row counts as applied").isTrue();
        }
        setExecType("EXECUTED");
    }
}
