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
import java.time.Instant;
import java.util.Map;

import static dev.nexus.service.http.IdentityTestEnv.ev;
import static dev.nexus.service.http.IdentityTestEnv.evOn;
import static dev.nexus.service.http.IdentityTestEnv.with;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1): renewal, lazy lapse and revive, on a clock the test sets. A leased holder that
 * has not renewed for more than 300 seconds is lapsed when the engine next reads it, under the same generation;
 * the next renewal from the same process revives it; another host may take a lapsed seat 10 minutes after the
 * lapse was due; an {@code unleased} binding never lapses.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityLapseTest {

    static final Instant T0 = Instant.parse("2026-11-16T12:00:00Z");

    IdentityTestEnv env;

    @BeforeAll
    void up() throws Exception {
        env = IdentityTestEnv.start();
    }

    @AfterAll
    void down() throws Exception {
        env.close();
    }

    private String boundSeat(String t, String name, Map<String, Object> evidence) {
        env.clock.set(T0);
        String id = env.createSeat(t, name);
        assertThat(env.bind(t, id, "start", 0, evidence).status()).isEqualTo(200);
        return id;
    }

    @Test
    void aHolderLapsesOnlyAfterMoreThan300SecondsWithoutRenewal() {
        String t = IdentityTestEnv.freshTenant();
        String id = boundSeat(t, "alpha", ev("s1", 21, 210));
        env.clock.set(T0.plusSeconds(300));
        assertThat(env.show(t, id)).as("exactly 300 seconds is not more than 300").containsEntry("state", "live");
        env.clock.set(T0.plusSeconds(301));
        var lapsed = env.show(t, id);
        assertThat(lapsed).containsEntry("state", "lapsed").containsEntry("generation", 1);
        assertThat(lapsed.get("lapsed_at")).as("the instant the lapse was due").isEqualTo(T0.plusSeconds(300).toString());
        assertThat(lapsed.get("reserved_until")).isEqualTo(T0.plusSeconds(900).toString());
        assertThat(env.events(t, id)).containsExactly("start", "lapse");
        var row = env.log(t, id).get(1);
        assertThat(row).containsEntry("generation", 1).containsEntry("at", T0.plusSeconds(300).toString())
            .containsEntry("pid", 21);
        // Reading again writes nothing more.
        env.show(t, id);
        assertThat(env.events(t, id)).hasSize(2);
    }

    @Test
    void aRenewalKeepsTheSeatLiveAndStoresTheClientVersion() {
        String t = IdentityTestEnv.freshTenant();
        String id = boundSeat(t, "beta", ev("s1", 22, 220));
        for (int i = 1; i <= 10; i++) {
            env.clock.set(T0.plusSeconds(60L * i));
            Resp r = env.post(t, "/" + id + "/renew", Map.of("generation", 1, "client_version", "7.100." + i));
            assertThat(r.status()).isEqualTo(200);
            assertThat(r.str("state")).isEqualTo("live");
        }
        env.clock.set(T0.plusSeconds(600 + 299));
        var s = env.show(t, id);
        assertThat(s).containsEntry("state", "live").containsEntry("client_version", "7.100.10");
        assertThat(env.events(t, id)).as("renewals write no row").containsExactly("start");
    }

    @Test
    void aRenewalAfterALapseRevivesWithTheSameGeneration() {
        String t = IdentityTestEnv.freshTenant();
        String id = boundSeat(t, "gamma", ev("s1", 23, 230));
        env.clock.set(T0.plusSeconds(700));
        assertThat(env.show(t, id)).containsEntry("state", "lapsed");
        env.clock.set(T0.plusSeconds(720));
        Resp r = env.post(t, "/" + id + "/renew", Map.of("generation", 1));
        assertThat(r.str("state")).isEqualTo("live");
        assertThat(r.integer("generation")).isEqualTo(1);
        assertThat(r.body().get("lapsed_at")).isNull();
        assertThat(env.events(t, id)).containsExactly("start", "lapse", "revive");
        assertThat(env.log(t, id).get(2)).containsEntry("generation", 1).containsEntry("pid", 23);
    }

    @Test
    void aSleepWithNoReadInBetweenStillLeavesALapseRowBeforeTheRevive() {
        String t = IdentityTestEnv.freshTenant();
        String id = boundSeat(t, "delta", ev("s1", 24, 240));
        env.clock.set(T0.plusSeconds(3600));
        env.post(t, "/" + id + "/renew", Map.of("generation", 1));
        assertThat(env.events(t, id)).containsExactly("start", "lapse", "revive");
        assertThat(env.log(t, id).get(1)).containsEntry("at", T0.plusSeconds(300).toString());
    }

    @Test
    void aClearOrAForkOnALapsedSeatRevivesIt() {
        String t = IdentityTestEnv.freshTenant();
        String id = boundSeat(t, "epsilon", ev("s1", 25, 250));
        env.clock.set(T0.plusSeconds(1000));
        Resp r = env.bind(t, id, "clear", 1, Map.of("session_id", "s2"));
        assertThat(r.str("state")).isEqualTo("live");
        assertThat(env.events(t, id)).containsExactly("start", "lapse", "clear");
    }

    @Test
    void anotherHostWaitsTenMinutesAfterTheLapseWasDueAndTheSameHostIsRefusedWithoutAVerdict() {
        String t = IdentityTestEnv.freshTenant();
        String id = boundSeat(t, "zeta", evOn("h1", "s1", 26, 260));
        // The lapse is due at T0+300; the reservation ends at T0+900.
        env.clock.set(T0.plusSeconds(899));
        Resp early = env.bind(t, id, "start", 1, evOn("h2", "s2", 90, 900));
        assertThat(early.status()).isEqualTo(409);
        assertThat(early.error()).isEqualTo("held");
        assertThat(early.str("reserved_until")).isEqualTo(T0.plusSeconds(900).toString());
        env.clock.set(T0.plusSeconds(900));
        Resp onTime = env.bind(t, id, "start", 1, evOn("h2", "s2", 90, 900));
        assertThat(onTime.status()).isEqualTo(200);
        assertThat(onTime.integer("generation")).isEqualTo(2);

        // On the holder's own host the engine cannot tell a stuck process from a dead one: it needs the verdict.
        String same = boundSeat(t, "eta", evOn("h1", "s5", 27, 270));
        env.clock.set(T0.plus(Duration.ofDays(2)));
        Resp strayLaunch = env.bind(t, same, "start", 1, evOn("h1", "s6", 91, 910));
        assertThat(strayLaunch.status()).isEqualTo(409);
        assertThat(strayLaunch.error()).isEqualTo("held");
        // The holder, still running, revives its own seat whenever it next renews.
        assertThat(env.post(t, "/" + same + "/renew", Map.of("generation", 1)).str("state")).isEqualTo("live");
    }

    @Test
    void anUnleasedBindingNeverLapses() {
        String t = IdentityTestEnv.freshTenant();
        String id = boundSeat(t, "theta", with(ev("s1", 28, 280), "lease", "unleased"));
        env.clock.set(T0.plus(Duration.ofDays(30)));
        assertThat(env.show(t, id)).containsEntry("state", "live").containsEntry("lease", "unleased");
        assertThat(env.events(t, id)).containsExactly("start");
        // And a second launch is refused for as long as nothing shows the holder gone.
        assertThat(env.bind(t, id, "start", 1, evOn("h2", "s2", 29, 290)).error()).isEqualTo("held");
    }

    @Test
    void bindingsByHostListsLiveLapsedAndUnleasedHoldersAndNoOthers() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String live = env.createSeat(t, "live");
        String lapsed = env.createSeat(t, "lapsedseat");
        String unleased = env.createSeat(t, "unleasedseat");
        String ended = env.createSeat(t, "endedseat");
        String elsewhere = env.createSeat(t, "elsewhere");
        env.bind(t, lapsed, "start", 0, evOn("hostA", "l1", 31, 310));
        env.bind(t, unleased, "start", 0, with(evOn("hostA", "u1", 32, 320), "lease", "unleased"));
        env.bind(t, ended, "start", 0, evOn("hostA", "e1", 33, 330));
        env.bind(t, ended, "end", 1, Map.of());
        env.bind(t, elsewhere, "start", 0, evOn("hostB", "x1", 34, 340));
        env.clock.set(T0.plusSeconds(200));
        env.bind(t, live, "start", 0, evOn("hostA", "v1", 35, 350));
        env.clock.set(T0.plusSeconds(400));
        // lapsed and elsewhere last renewed at T0 and have lapsed; live renewed at T0+200 and has not.
        Resp r = env.get(t, "/bindings?host=hostA");
        assertThat(r.status()).isEqualTo(200);
        var rows = r.list("bindings");
        assertThat(rows).extracting(m -> m.get("id")).as("the default mode lists live and lapsed holders and excludes ended ones")
            .containsExactlyInAnyOrder(live, lapsed, unleased);
        assertThat(rows).filteredOn(m -> m.get("id").equals(lapsed)).singleElement().satisfies(m ->
            assertThat(m).containsEntry("state", "lapsed"));
        assertThat(rows).filteredOn(m -> m.get("id").equals(unleased)).singleElement().satisfies(m ->
            assertThat(m).containsEntry("state", "live").containsEntry("lease", "unleased"));
        assertThat(rows).filteredOn(m -> m.get("id").equals(live)).singleElement().satisfies(m ->
            assertThat(m).containsEntry("state", "live"));
        var byPid = env.get(t, "/bindings?host=hostA&pid=35").list("bindings");
        assertThat(byPid).extracting(m -> m.get("id")).containsExactly(live);
        assertThat(env.get(t, "/bindings").status()).isEqualTo(400);
        assertThat(env.get(t, "/bindings?host=hostB").list("bindings")).extracting(m -> m.get("id"))
            .containsExactly(elsewhere);
    }

    @Test
    void theHistoryModeFindsEndedAndReleasedIdentitiesByTheirProcessNewestRowFirst() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        var proc = evOn("hostH", "p1", 41, 410);
        String ended = env.createSeat(t, "endedseat");
        String released = env.createSeat(t, "releasedseat");
        String live = env.createSeat(t, "liveseat");
        String other = env.createSeat(t, "otherproc");
        env.bind(t, ended, "start", 0, proc);
        env.bind(t, ended, "clear", 1, Map.of("session_id", "p1b"));
        env.clock.set(T0.plusSeconds(10));
        env.bind(t, ended, "end", 1, Map.of());
        env.clock.set(T0.plusSeconds(20));
        env.bind(t, released, "start", 0, with(proc, "session_id", "p2"));
        env.bind(t, released, "release", 1, with(proc, "session_id", "p2"));
        env.clock.set(T0.plusSeconds(30));
        env.bind(t, live, "start", 0, with(proc, "session_id", "p3", "started_at", 411L));
        env.bind(t, other, "start", 0, evOn("hostH", "p4", 42, 420));

        // The in-place /restart rebind asks for its own process's rows: the default mode cannot see an ended seat.
        assertThat(env.get(t, "/bindings?host=hostH&pid=41").list("bindings")).extracting(m -> m.get("id"))
            .as("holders only: ended and released are not listed").containsExactly(live);
        Resp r = env.get(t, "/bindings?host=hostH&pid=41&proc_start=" + proc.get("proc_start") + "&history=1");
        assertThat(r.status()).isEqualTo(200);
        var rows = r.list("bindings");
        assertThat(rows).extracting(m -> m.get("id")).as("newest first").containsExactly(live, released, ended);
        var endedRow = rows.stream().filter(m -> ended.equals(m.get("id"))).findFirst().orElseThrow();
        assertThat(endedRow).containsEntry("state", "ended").containsEntry("generation", 1);
        @SuppressWarnings("unchecked")
        Map<String, Object> newest = (Map<String, Object>) endedRow.get("binding");
        assertThat(newest).as("the newest row of the identity on that process").containsEntry("event", "end")
            .containsEntry("pid", 41).containsEntry("proc_start", proc.get("proc_start"));
        var releasedRow = rows.stream().filter(m -> released.equals(m.get("id"))).findFirst().orElseThrow();
        assertThat(releasedRow).containsEntry("state", "unheld");
        // A different proc_start or pid matches nothing; pid and host are required.
        assertThat(env.get(t, "/bindings?host=hostH&pid=41&proc_start=1999-01-01T00:00:00Z&history=1").list("bindings")).isEmpty();
        assertThat(env.get(t, "/bindings?host=hostH&pid=42&proc_start=" + evOn("hostH", "p4", 42, 420).get("proc_start") + "&history=1")
            .list("bindings")).extracting(m -> m.get("id")).containsExactly(other);
        // A pid alone can name a reused one: the history mode needs the proc_start too.
        Resp pidOnly = env.get(t, "/bindings?host=hostH&pid=42&history=1");
        assertThat(pidOnly.status()).isEqualTo(400);
        assertThat(pidOnly.error()).isEqualTo("missing_process");
        assertThat(env.get(t, "/bindings?host=hostH&history=1").status()).isEqualTo(400);
        assertThat(env.get(t, "/bindings?host=elsewhere&pid=41&proc_start=" + proc.get("proc_start") + "&history=1").list("bindings")).isEmpty();
    }
}
