// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import dev.nexus.service.http.IdentityTestEnv;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 3: one evaluator at a time. Two {@code --done} writes evaluated at once, a hand-off racing a
 * {@code --done}, and a {@code serves} change racing a {@code --done}, post rows consistent with commit order and
 * never two rows from one state (real PostgreSQL, the per-topic advisory lock).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerStamperConcurrencyTest {

    StamperEnv se;

    @BeforeAll
    void up() throws Exception {
        se = new StamperEnv();
    }

    @AfterAll
    void down() throws Exception {
        se.close();
    }

    private String[] redOnY() {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        return new String[] {t, l, a, c};
    }

    private <T> List<T> together(List<java.util.concurrent.Callable<T>> jobs) throws Exception {
        var pool = Executors.newFixedThreadPool(jobs.size());
        try {
            CountDownLatch go = new CountDownLatch(1);
            List<Future<T>> fs = jobs.stream().map(j -> pool.submit(() -> {
                go.await();
                return j.call();
            })).toList();
            go.countDown();
            java.util.ArrayList<T> out = new java.util.ArrayList<>();
            for (Future<T> f : fs) {
                out.add(f.get());
            }
            return out;
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void twoDoneWritesEvaluatedAtOncePostOneRowForOneState() throws Exception {
        for (int round = 0; round < 5; round++) {
            String[] f = redOnY();
            se.doneRowOnly(f[0], StamperEnv.BASE, "X", "cancelled");
            se.doneRowOnly(f[0], StamperEnv.BASE, "Y", "failure");

            together(List.of(
                () -> { se.stamper.afterOut(f[0], "board/ci/nexus-develop--done",
                    Map.of("kind", "sha", "sha", "X", "outcome", "failure", "from", "github"), "{}"); return null; },
                () -> { se.stamper.afterOut(f[0], "board/ci/nexus-develop--done",
                    Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}"); return null; }));

            var rows = se.ownerRows(f[0], StamperEnv.BASE);
            assertThat(rows).as("round " + round + ": one row from one state").hasSize(1);
            assertThat(rows.getFirst().sha()).isEqualTo("Y");
            assertThat(rows.getFirst().body()).containsEntry("owner", f[2]);
        }
    }

    @Test
    void aRedAndAGreenEvaluatedAtOnceLeaveTheStateOfTheNewestTip() throws Exception {
        String[] f = redOnY();
        se.done(f[0], StamperEnv.BASE, "Y", "failure");
        se.push(f[0], StamperEnv.BASE, "Z", "Y", "sess-alpha");
        se.doneRowOnly(f[0], StamperEnv.BASE, "Z", "success");
        se.doneRowOnly(f[0], StamperEnv.BASE, "Y", "failure");

        together(List.of(
            () -> { se.stamper.afterOut(f[0], "board/ci/nexus-develop--done",
                Map.of("kind", "sha", "sha", "Z", "outcome", "success", "from", "github"), "{}"); return null; },
            () -> { se.stamper.afterOut(f[0], "board/ci/nexus-develop--done",
                Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}"); return null; }));

        var rows = se.ownerRows(f[0], StamperEnv.BASE);
        assertThat(rows).extracting(StamperEnv.Row::kind).containsExactly("owner", "clear");
    }

    @Test
    void aHandOffRacingADoneWritePostsRowsInCommitOrderAndNeverTwoFromOneState() throws Exception {
        for (int round = 0; round < 5; round++) {
            String[] f = redOnY();
            se.done(f[0], StamperEnv.BASE, "Y", "failure");
            se.env.ids.clock.advance(Duration.ofMinutes(21));
            se.env.renew(f[0], f[1]);
            se.env.renew(f[0], f[2]);
            se.env.renew(f[0], f[3]);
            se.push(f[0], StamperEnv.BASE, "Z", "Y", "sess-alpha");
            se.doneRowOnly(f[0], StamperEnv.BASE, "Z", "failure");

            together(List.of(
                () -> { se.stamper.onDeadline(f[0], StamperEnv.BASE); return null; },
                () -> { se.stamper.afterOut(f[0], "board/ci/nexus-develop--done",
                    Map.of("kind", "sha", "sha", "Z", "outcome", "failure", "from", "github"), "{}"); return null; }));

            // Whichever took the lock first, the deadline of a state that is already past is re-armed, and the timer
            // fires again: the hand-off that a refresh row pre-empted is the next evaluation's.
            se.stamper.onDeadline(f[0], StamperEnv.BASE);

            var rows = se.ownerRows(f[0], StamperEnv.BASE);
            assertThat(rows).as("round " + round + ": the first row, one for the refresh, one for the hand-off").hasSize(3);
            StamperEnv.Row last = rows.getLast();
            assertThat(last.sha()).isEqualTo("Z");
            assertThat(last.body()).containsEntry("owner", f[3]).containsEntry("handoffs", 1);
            assertThat(rows.stream().map(r -> r.sha() + "/" + r.get("reason") + "/" + r.get("handoffs") + "/" + r.get("refresh"))
                .distinct().count()).as("never two rows from one state").isEqualTo(3L);
            assertThat(rows.stream().filter(r -> "handoff".equals(r.get("reason"))).count()).isEqualTo(1L);
        }
    }

    @Test
    void aServesChangeRacingADoneWriteLeavesAtMostOneOwnerRow() throws Exception {
        String[] f = redOnY();
        se.doneRowOnly(f[0], StamperEnv.BASE, "Y", "failure");

        together(List.of(
            () -> { se.stamper.afterOut(f[0], "board/ci/nexus-develop--done",
                Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}"); return null; },
            () -> se.env.as(f[0], f[1], "POST", "/push-train/set", Map.of("serves", List.of(StamperEnv.BASE, "nexus-main"))).status()));

        assertThat(se.ownerRows(f[0], StamperEnv.BASE).size()).isLessThanOrEqualTo(1);
        assertThat(IdentityTestEnv.freshTenant()).isNotNull();
    }
}
