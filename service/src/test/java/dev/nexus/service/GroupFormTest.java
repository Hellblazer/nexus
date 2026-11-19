// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.GroupTestEnv;
import dev.nexus.service.http.IdentityTestEnv;
import dev.nexus.service.http.IdentityTestEnv.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

import static dev.nexus.service.http.GroupTestEnv.body;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 1 (nexus-igea9.5), the first write: an insert that fails when a live record has the name, so two
 * concurrent formations leave one record, and in the same transaction the leader's membership row. Written first,
 * before any group route existed.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GroupFormTest {

    GroupTestEnv env;

    @BeforeAll
    void up() throws Exception {
        env = GroupTestEnv.start();
    }

    @AfterAll
    void down() throws Exception {
        env.close();
    }

    @Test
    void twoConcurrentFirstWritesLeaveOneRecordAndAddTheLeaderMembership() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        var pool = Executors.newFixedThreadPool(2);
        try {
            // Five names, push-train first: a race that one round happens to lose is not a pass.
            for (String name : List.of("push-train", "push-train-1", "push-train-2", "push-train-3", "push-train-4")) {
                CountDownLatch go = new CountDownLatch(1);
                Future<Resp> fa = pool.submit(() -> {
                    go.await();
                    return env.form(t, a, body(name, a));
                });
                Future<Resp> fb = pool.submit(() -> {
                    go.await();
                    return env.form(t, b, body(name, b));
                });
                go.countDown();
                Resp ra = fa.get();
                Resp rb = fb.get();

                boolean aWon = ra.status() == 200;
                Resp winner = aWon ? ra : rb;
                Resp loser = aWon ? rb : ra;
                String winnerId = aWon ? a : b;
                assertThat(winner.status()).as("%s: %s / %s", name, ra, rb).isEqualTo(200);
                assertThat(loser.status()).as("%s: the loser", name).isEqualTo(409);
                assertThat(loser.error()).isEqualTo("group_exists");

                Resp read = env.read(t, name);
                assertThat(read.status()).isEqualTo(200);
                assertThat(read.str("leader")).as(name).isEqualTo(winnerId);

                var roster = env.roster(t, name);
                assertThat(roster).as("%s: the roster holds the winner only", name).hasSize(1);
                assertThat(roster.get(0)).containsEntry("identity", winnerId).containsEntry("role", "leader");
            }
        } finally {
            pool.shutdownNow();
        }
    }
}
