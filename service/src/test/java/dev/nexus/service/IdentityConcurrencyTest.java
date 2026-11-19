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
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

import static dev.nexus.service.http.IdentityTestEnv.ev;
import static dev.nexus.service.http.IdentityTestEnv.verdictOf;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1), review round 1: races. Creations that run at once get distinct ids from the
 * counter (one number, one identity), a rename of one name by two callers has one winner, and a {@code gone} verdict
 * racing a take-over leaves a log that reads in one of the two legal orders and never a lapsed identity that a newer
 * process holds.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityConcurrencyTest {

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
    void concurrentCreationsOfSeatsAndEphemeralIdentitiesGetDistinctIds() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        int threads = 16;
        int each = 4;
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        try {
            CyclicBarrier gate = new CyclicBarrier(threads);
            List<Future<List<String>>> futures = new ArrayList<>();
            for (int n = 0; n < threads; n++) {
                int worker = n;
                futures.add(pool.submit(() -> {
                    gate.await();
                    List<String> ids = new ArrayList<>();
                    for (int k = 0; k < each; k++) {
                        Resp r = worker % 2 == 0
                            ? env.post(t, "", Map.of("kind", "seat", "name", "seat-" + worker + "-" + k, "created_by", "sam"))
                            : env.post(t, "", Map.of("kind", "ephemeral", "created_by", "session",
                                "bind", ev("sess-" + worker + "-" + k, 1000 + worker * 10 + k, 5000L + worker * 10 + k)));
                        assertThat(r.status()).as(r.toString()).isEqualTo(200);
                        ids.add(r.str("id"));
                    }
                    return ids;
                }));
            }
            Set<String> all = new HashSet<>();
            for (var f : futures) {
                all.addAll(f.get());
            }
            assertThat(all).as("every creation got its own id").hasSize(threads * each);
            assertThat(all).allMatch(id -> id.matches("i[0-9]+"));
            assertThat(all.stream().mapToInt(id -> Integer.parseInt(id.substring(1))).max().orElseThrow())
                .as("no gaps: the counter handed out exactly 1..n").isEqualTo(threads * each);
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void twoCallersRenamingOneNameHaveOneWinner() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            for (int round = 0; round < 15; round++) {
                String id = env.createSeat(t, "orig" + round);
                CyclicBarrier gate = new CyclicBarrier(2);
                List<Future<Resp>> futures = new ArrayList<>();
                for (int who = 0; who < 2; who++) {
                    String target = "new" + round + "x" + who;
                    String old = "orig" + round;
                    futures.add(pool.submit(() -> {
                        gate.await();
                        return env.post(t, "/rename", Map.of("old", old, "new", target));
                    }));
                }
                List<Resp> got = new ArrayList<>();
                for (var f : futures) {
                    got.add(f.get());
                }
                assertThat(got.stream().filter(r -> r.status() == 200).count()).as("winners, round %d", round).isEqualTo(1);
                Resp loser = got.stream().filter(r -> r.status() != 200).findFirst().orElseThrow();
                assertThat(loser.error()).as("the loser named a name that was no longer the seat's").isIn("unknown_name", "stale_name");
                String winner = got.stream().filter(r -> r.status() == 200).findFirst().orElseThrow().str("name");
                assertThat(env.show(t, id)).containsEntry("name", winner);
                assertThat(env.events(t, id)).as("one rename row").containsExactly("rename");
            }
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void aGoneVerdictRacingATakeOverLeavesALegalLogAndNeverALapsedNewHolder() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            int goneFirst = 0;
            int takeoverFirst = 0;
            for (int round = 0; round < 25; round++) {
                int rd = round;
                String id = env.createSeat(t, "race" + rd);
                var old = ev("old-" + rd, 100 + rd, 1000L + rd);
                assertThat(env.bind(t, id, "start", 0, old).status()).isEqualTo(200);
                CyclicBarrier gate = new CyclicBarrier(2);
                Future<Resp> gone = pool.submit(() -> {
                    gate.await();
                    return env.post(t, "/" + id + "/gone", Map.of("expected_generation", 1, "holder_gone", verdictOf(old)));
                });
                Future<Resp> take = pool.submit(() -> {
                    gate.await();
                    return env.bind(t, id, "takeover", 1, ev("new-" + rd, 500 + rd, 7000L + rd));
                });
                Resp g = gone.get();
                Resp k = take.get();
                assertThat(k.status()).as("the take-over expects generation 1, which a gone verdict does not change").isEqualTo(200);
                var rec = env.show(t, id);
                assertThat(rec).containsEntry("generation", 2).containsEntry("state", "live");
                assertThat(rec.get("gone_at")).as("the newer process is not marked gone").isNull();
                List<String> events = env.events(t, id);
                if (g.status() == 200) {
                    assertThat(events).containsExactly("start", "gone", "takeover");
                    goneFirst++;
                } else {
                    assertThat(g.error()).isIn("generation_conflict", "holder_mismatch");
                    assertThat(events).containsExactly("start", "takeover");
                    takeoverFirst++;
                }
            }
            assertThat(goneFirst + takeoverFirst).isEqualTo(25);
        } finally {
            pool.shutdownNow();
        }
    }
}
