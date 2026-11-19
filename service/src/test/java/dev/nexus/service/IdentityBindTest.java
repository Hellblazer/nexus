// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.IdentityTestEnv;
import dev.nexus.service.http.IdentityTestEnv.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.Future;
import java.util.concurrent.Executors;

import static dev.nexus.service.http.IdentityTestEnv.ev;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1): the compare-and-set of {@code bind}. Two binds that expect the same
 * generation cannot both win: the identity row is locked, the second reads the generation the first raised,
 * and is told so with a 409 {@code generation_conflict}. Real PostgreSQL; the race is run on many seats.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityBindTest {

    IdentityTestEnv env;

    @BeforeAll
    void up() throws Exception {
        env = IdentityTestEnv.start();
    }

    @AfterAll
    void down() throws Exception {
        env.close();
    }

    @Test
    void twoSimultaneousBindsWithOneExpectedGenerationOneWins() throws Exception {
        String tenant = IdentityTestEnv.freshTenant();
        int rounds = 25;
        var pool = Executors.newFixedThreadPool(2);
        try {
            for (int round = 0; round < rounds; round++) {
                String id = env.createSeat(tenant, "race" + round);
                CyclicBarrier gate = new CyclicBarrier(2);
                List<Future<Resp>> results = new ArrayList<>();
                for (int p = 1; p <= 2; p++) {
                    int pid = 1000 + p;
                    results.add(pool.submit(() -> {
                        gate.await();
                        return env.bind(tenant, id, "start", 0, ev("sess-" + pid + "-" + id, pid, 5_000L + pid));
                    }));
                }
                List<Resp> got = new ArrayList<>();
                for (var f : results) {
                    got.add(f.get());
                }
                assertThat(got.stream().filter(r -> r.status() == 200).count()).as("winners, round %d", round).isEqualTo(1);
                Resp loser = got.stream().filter(r -> r.status() != 200).findFirst().orElseThrow();
                assertThat(loser.status()).isEqualTo(409);
                assertThat(loser.error()).isIn("generation_conflict", "held");
                var rec = env.show(tenant, id);
                assertThat(rec.get("generation")).as("the generation rose by exactly one").isEqualTo(1);
                assertThat(env.events(tenant, id)).as("one start row").containsExactly("start");
            }
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void aBindThatExpectsAStaleGenerationIsRefusedWithTheCurrentOne() {
        String tenant = IdentityTestEnv.freshTenant();
        String id = env.createSeat(tenant, "stale");
        assertThat(env.bind(tenant, id, "start", 0, ev("s1", 10, 100)).status()).isEqualTo(200);
        Resp stale = env.bind(tenant, id, "takeover", 0, ev("s2", 11, 101));
        assertThat(stale.status()).isEqualTo(409);
        assertThat(stale.error()).isEqualTo("generation_conflict");
        assertThat(stale.body().get("current_generation")).isEqualTo(1);
        assertThat(stale.body().get("expected_generation")).isEqualTo(0);
        assertThat(env.show(tenant, id).get("generation")).isEqualTo(1);
    }

    @Test
    void aBindWithNoExpectedGenerationOrAnUnknownEventIsABadRequest() {
        String tenant = IdentityTestEnv.freshTenant();
        String id = env.createSeat(tenant, "bad");
        Resp none = env.post(tenant, "/" + id + "/bind", ev("s1", 10, 100));
        assertThat(none.status()).isEqualTo(400);
        Resp nonsense = env.bind(tenant, id, "teleport", 0, ev("s1", 10, 100));
        assertThat(nonsense.status()).isEqualTo(400);
        assertThat(nonsense.error()).isEqualTo("bad_event");
        assertThat(env.bind(tenant, "i999999", "start", 0, ev("s1", 10, 100)).status()).isEqualTo(404);
    }

    @Test
    void anotherTenantNeitherSeesNorBindsTheSeat() {
        String a = IdentityTestEnv.freshTenant();
        String b = IdentityTestEnv.freshTenant();
        String id = env.createSeat(a, "mine");
        assertThat(env.get(b, "/" + id).status()).isEqualTo(404);
        assertThat(env.bind(b, id, "start", 0, ev("s1", 10, 100)).status()).isEqualTo(404);
        assertThat(env.get(b, "/alias/mine").status()).isEqualTo(404);
    }
}
