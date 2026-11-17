// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.IdentityTestEnv;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.http.IdentityTestEnv.ev;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_BINDINGS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_SESSIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1), review round 2: the retention prune deletes in batches inside a per-pass bound,
 * so a backlog larger than one batch progresses over passes and no statement outruns the sweep's statement timeout.
 * The environment runs with a batch of 10 rows and 2 batches a pass (production: 5000 and 20).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityRetentionBatchTest {

    static final Instant T0 = Instant.parse("2026-11-16T12:00:00Z");

    IdentityTestEnv env;

    @BeforeAll
    void up() throws Exception {
        env = IdentityTestEnv.start(10, 2);
    }

    @AfterAll
    void down() throws Exception {
        env.close();
    }

    @Test
    void aBacklogLargerThanOneBatchIsPrunedOverPassesAndTheNewestRowStays() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "backlog");
        env.bind(t, id, "start", 0, ev("s1", 1, 10));
        OffsetDateTime old = T0.atOffset(ZoneOffset.UTC);
        // 66 more old rows, each with its own session in the index: 67 rows and 67 sessions in all.
        env.scope.withTenant(t, ctx -> {
            for (int seq = 2; seq <= 67; seq++) {
                ctx.insertInto(IDENTITY_BINDINGS).set(IDENTITY_BINDINGS.TENANT_ID, t).set(IDENTITY_BINDINGS.IDENTITY_ID, id)
                    .set(IDENTITY_BINDINGS.SEQ, seq).set(IDENTITY_BINDINGS.EVENT, "clear").set(IDENTITY_BINDINGS.GENERATION, 1)
                    .set(IDENTITY_BINDINGS.SESSION_ID, "bs-" + seq).set(IDENTITY_BINDINGS.AT, old).execute();
                ctx.insertInto(IDENTITY_SESSIONS).set(IDENTITY_SESSIONS.TENANT_ID, t).set(IDENTITY_SESSIONS.SESSION_ID, "bs-" + seq)
                    .set(IDENTITY_SESSIONS.IDENTITY_ID, id).set(IDENTITY_SESSIONS.GENERATION, 1).set(IDENTITY_SESSIONS.AT, old).execute();
            }
            ctx.update(IDENTITY_RECORDS).set(IDENTITY_RECORDS.LAST_BINDING_SEQ, 67)
                .where(IDENTITY_RECORDS.TENANT_ID.eq(t)).and(IDENTITY_RECORDS.ID.eq(id)).execute();
            return null;
        });
        assertThat(env.rows(t, IDENTITY_BINDINGS, IDENTITY_BINDINGS.TENANT_ID)).isEqualTo(67);
        assertThat(env.rows(t, IDENTITY_SESSIONS, IDENTITY_SESSIONS.TENANT_ID)).isEqualTo(67);

        env.clock.set(T0.plus(Duration.ofDays(181)));
        // 66 rows are prunable (all but the newest), 20 a pass: 20, 20, 20, 6, then nothing.
        List<Integer> bindings = new java.util.ArrayList<>();
        List<Integer> sessions = new java.util.ArrayList<>();
        for (int pass = 0; pass < 5; pass++) {
            Map<String, Integer> r = env.repo.runRetention(t);
            bindings.add(r.get("bindings_pruned"));
            sessions.add(r.get("sessions_pruned"));
        }
        assertThat(bindings).as("binding rows pruned per pass").containsExactly(20, 20, 20, 6, 0);
        assertThat(sessions).as("session-index rows pruned per pass").containsExactly(20, 20, 20, 6, 0);
        assertThat(env.rows(t, IDENTITY_BINDINGS, IDENTITY_BINDINGS.TENANT_ID)).as("never the newest row").isEqualTo(1);
        assertThat(env.rows(t, IDENTITY_SESSIONS, IDENTITY_SESSIONS.TENANT_ID)).as("the newest row's session stays").isEqualTo(1);
        assertThat(env.get(t, "/sessions/bs-67").str("identity")).isEqualTo(id);
        assertThat(env.get(t, "/sessions/bs-2").status()).isEqualTo(404);
        assertThat(env.show(t, id)).containsEntry("generation", 1);
    }

    @Test
    void aBacklogOfOrphanedSessionsAloneIsAlsoBatched() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "orphans");
        env.bind(t, id, "start", 0, ev("keep-me", 2, 20));
        OffsetDateTime at = T0.atOffset(ZoneOffset.UTC);
        env.scope.withTenant(t, ctx -> {
            for (int n = 1; n <= 45; n++) {
                ctx.insertInto(IDENTITY_SESSIONS).set(IDENTITY_SESSIONS.TENANT_ID, t).set(IDENTITY_SESSIONS.SESSION_ID, "orphan-" + n)
                    .set(IDENTITY_SESSIONS.IDENTITY_ID, id).set(IDENTITY_SESSIONS.GENERATION, 1).set(IDENTITY_SESSIONS.AT, at).execute();
            }
            return null;
        });
        List<Integer> sessions = new java.util.ArrayList<>();
        for (int pass = 0; pass < 4; pass++) {
            sessions.add(env.repo.runRetention(t).get("sessions_pruned"));
        }
        assertThat(sessions).containsExactly(20, 20, 5, 0);
        assertThat(env.get(t, "/sessions/keep-me").str("identity")).isEqualTo(id);
    }
}
