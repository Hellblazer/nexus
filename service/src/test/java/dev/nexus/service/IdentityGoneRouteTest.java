// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.IdentityTestEnv;
import dev.nexus.service.http.IdentityTestEnv.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.util.Map;

import static dev.nexus.service.http.IdentityTestEnv.ev;
import static dev.nexus.service.http.IdentityTestEnv.verdictOf;
import static dev.nexus.service.http.IdentityTestEnv.with;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1): the {@code gone} route. A client's verdict that the current holder is dead is
 * recorded only when it names the identity's current holder (host, pid, {@code started_at}, {@code proc_start})
 * under the same compare-and-set, so a sweep cannot mark a binding that was rebound a moment earlier. The row
 * carries the dead holder's fields; a repeat is a no-op; a lapsed holder can be reported gone; an ended or released
 * identity has no holder to mark.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityGoneRouteTest {

    IdentityTestEnv env;

    @BeforeAll
    void up() throws Exception {
        env = IdentityTestEnv.start();
    }

    @AfterAll
    void down() throws Exception {
        env.close();
    }

    private Resp gone(String t, String id, int generation, Map<String, Object> verdict) {
        return env.post(t, "/" + id + "/gone", Map.of("expected_generation", generation, "holder_gone", verdict));
    }

    @Test
    void goneIsRecordedForTheCurrentHolderAndSetsTheStateToLapsedWhateverTheLease() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "alpha");
        Map<String, Object> holder = with(ev("s1", 11, 110), "lease", "unleased");
        env.bind(t, id, "start", 0, holder);
        Resp r = gone(t, id, 1, verdictOf(holder));
        assertThat(r.status()).isEqualTo(200);
        assertThat(r.body().get("recorded")).isEqualTo(true);
        assertThat(r.str("state")).isEqualTo("lapsed");
        assertThat(r.str("gone_at")).isEqualTo(env.clock.instant().toString());
        assertThat(env.events(t, id)).containsExactly("start", "gone");
        var row = env.log(t, id).get(1);
        assertThat(row).containsEntry("event", "gone").containsEntry("host", "h1").containsEntry("pid", 11)
            .containsEntry("proc_start", holder.get("proc_start")).containsEntry("session_id", "s1")
            .containsEntry("generation", 1);
        assertThat(((Number) row.get("started_at")).longValue()).isEqualTo(110L);
        assertThat(row.get("holder_gone")).isNotNull();
    }

    @Test
    void aRepeatedVerdictForTheSameHolderIsANoOp() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "beta");
        Map<String, Object> holder = ev("s1", 12, 120);
        env.bind(t, id, "start", 0, holder);
        gone(t, id, 1, verdictOf(holder));
        env.clock.advance(Duration.ofHours(1));
        Resp again = gone(t, id, 1, verdictOf(holder));
        assertThat(again.status()).isEqualTo(200);
        assertThat(again.body().get("recorded")).isEqualTo(false);
        assertThat(env.events(t, id)).as("no second row").containsExactly("start", "gone");
        assertThat(again.str("gone_at")).as("the clock keeps its first start").isNotEqualTo(env.clock.instant().toString());
    }

    @Test
    void aVerdictIsRefusedOnceTheSeatWasRebound() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "gamma");
        Map<String, Object> old = ev("s1", 13, 130);
        env.bind(t, id, "start", 0, old);
        env.bind(t, id, "end", 1, Map.of());
        env.bind(t, id, "start", 1, ev("s2", 14, 140));
        // The sweep read generation 1 and is late: the compare-and-set refuses it.
        Resp stale = gone(t, id, 1, verdictOf(old));
        assertThat(stale.status()).isEqualTo(409);
        assertThat(stale.error()).isEqualTo("generation_conflict");
        // Even at the current generation the old holder is not the holder any more.
        Resp wrong = gone(t, id, 2, verdictOf(old));
        assertThat(wrong.status()).isEqualTo(409);
        assertThat(wrong.error()).isEqualTo("holder_mismatch");
        assertThat(env.show(t, id)).containsEntry("state", "live");
        assertThat(env.events(t, id)).containsExactly("start", "end", "start");
        // A verdict that differs from the holder in one field only (proc_start) is refused too.
        Resp field = gone(t, id, 2, with(verdictOf(ev("s2", 14, 140)), "proc_start", "2000-01-01T00:00:00Z"));
        assertThat(field.error()).isEqualTo("holder_mismatch");
    }

    @Test
    void aVerdictIsRefusedWhenTheCurrentRowIsAnEndOrARelease() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "delta");
        Map<String, Object> holder = ev("s1", 15, 150);
        env.bind(t, id, "start", 0, holder);
        env.bind(t, id, "release", 1, holder);
        assertThat(gone(t, id, 1, verdictOf(holder)).error()).as("after a release").isEqualTo("not_held");
        env.bind(t, id, "start", 1, holder);
        env.bind(t, id, "end", 2, Map.of());
        assertThat(gone(t, id, 2, verdictOf(holder)).error()).as("after an end").isEqualTo("not_held");
        assertThat(gone(t, "i987654", 1, verdictOf(holder)).status()).isEqualTo(404);
        assertThat(env.post(t, "/" + id + "/gone", Map.of("expected_generation", 2)).status()).isEqualTo(400);
        assertThat(env.post(t, "/" + id + "/gone", Map.of("holder_gone", verdictOf(holder))).status()).isEqualTo(400);
    }

    @Test
    void aLapsedHolderCanBeReportedGoneAndTheLapseRowComesFirst() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "epsilon");
        Map<String, Object> holder = ev("s1", 16, 160);
        env.bind(t, id, "start", 0, holder);
        env.clock.advance(Duration.ofSeconds(400));
        Resp r = gone(t, id, 1, verdictOf(holder));
        assertThat(r.status()).isEqualTo(200);
        assertThat(env.events(t, id)).containsExactly("start", "lapse", "gone");
    }

    @Test
    void aRecordedGoneFreesTheSeatForAnyNewProcessAndARenewalFromTheHolderRevivesIt() {
        String t = IdentityTestEnv.freshTenant();
        String freed = env.createSeat(t, "zeta");
        Map<String, Object> holder = with(ev("s1", 17, 170), "lease", "unleased");
        env.bind(t, freed, "start", 0, holder);
        gone(t, freed, 1, verdictOf(holder));
        Resp next = env.bind(t, freed, "start", 1, ev("s2", 18, 180));
        assertThat(next.status()).as("the recorded verdict frees an unleased seat").isEqualTo(200);
        assertThat(next.integer("generation")).isEqualTo(2);
        assertThat(next.body().get("gone_at")).isNull();

        String revived = env.createSeat(t, "eta");
        Map<String, Object> h2 = ev("s3", 19, 190);
        env.bind(t, revived, "start", 0, h2);
        gone(t, revived, 1, verdictOf(h2));
        Resp renewed = env.post(t, "/" + revived + "/renew", Map.of("generation", 1));
        assertThat(renewed.str("state")).isEqualTo("live");
        assertThat(renewed.body().get("gone_at")).isNull();
        assertThat(env.events(t, revived)).containsExactly("start", "gone", "revive");
    }
}
