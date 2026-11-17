// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.IdentityTestEnv;
import dev.nexus.service.http.IdentityTestEnv.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.Map;

import static dev.nexus.service.http.IdentityTestEnv.ev;
import static dev.nexus.service.http.IdentityTestEnv.evOn;
import static dev.nexus.service.http.IdentityTestEnv.verdictOf;
import static dev.nexus.service.http.IdentityTestEnv.with;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1): each bind event under the policy table. The generation rises only when a
 * different process takes the identity (start, resume, take-over); clear and fork keep it, add a session-index
 * row and revive a lapsed identity; refresh renews without a row; end and release close it. The evidence a caller
 * sends is recorded whole.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityBindEventsTest {

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
    void startRaisesTheGenerationAndRecordsEveryPieceOfEvidence() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "alpha");
        Map<String, Object> e = with(ev("sess-1", 4242, 1_700_000_000_000L), "prior_session_id", "sess-0");
        Resp r = env.bind(t, id, "start", 0, e);
        assertThat(r.status()).isEqualTo(200);
        assertThat(r.integer("generation")).isEqualTo(1);
        assertThat(r.str("state")).isEqualTo("live");
        assertThat(r.body().get("generation_changed")).isEqualTo(true);
        var row = env.log(t, id).get(0);
        assertThat(row).containsEntry("event", "start").containsEntry("generation", 1).containsEntry("lease", "leased")
            .containsEntry("session_id", "sess-1").containsEntry("host", "h1").containsEntry("pid", 4242)
            .containsEntry("started_at", 1_700_000_000_000L).containsEntry("proc_start", e.get("proc_start"))
            .containsEntry("pane", "%4242").containsEntry("client_version", "7.99.0")
            .containsEntry("prior_session_id", "sess-0");
        assertThat(row.get("at")).isNotNull();
        assertThat(env.show(t, id).get("holder")).isInstanceOf(Map.class);
        // The session index maps the session to the identity and generation.
        Resp s = env.get(t, "/sessions/sess-1");
        assertThat(s.str("identity")).isEqualTo(id);
        assertThat(s.integer("generation")).isEqualTo(1);
    }

    @Test
    void clearAndForkKeepTheGenerationAndIndexTheNewSession() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "beta");
        env.bind(t, id, "start", 0, ev("sess-a", 50, 500));
        Resp clear = env.bind(t, id, "clear", 1, ev("sess-b", 50, 500));
        assertThat(clear.status()).isEqualTo(200);
        assertThat(clear.integer("generation")).isEqualTo(1);
        assertThat(clear.body().get("generation_changed")).isEqualTo(false);
        // A clear from the hook carries no process fields at all; the holder's are filled in.
        Resp fork = env.post(t, "/" + id + "/bind", Map.of("event", "fork", "expected_generation", 1,
            "session_id", "sess-c", "prior_session_id", "sess-b"));
        assertThat(fork.status()).isEqualTo(200);
        assertThat(fork.integer("generation")).isEqualTo(1);
        assertThat(env.events(t, id)).containsExactly("start", "clear", "fork");
        assertThat(env.log(t, id).get(2)).containsEntry("pid", 50).containsEntry("session_id", "sess-c");
        for (String sid : new String[] {"sess-a", "sess-b", "sess-c"}) {
            Resp s = env.get(t, "/sessions/" + sid);
            assertThat(s.str("identity")).as(sid).isEqualTo(id);
            assertThat(s.integer("generation")).as(sid).isEqualTo(1);
        }
        assertThat(env.bind(t, id, "clear", 1, Map.of()).status()).as("a clear names its session").isEqualTo(400);
    }

    @Test
    void resumeInTheSameProcessKeepsTheGenerationAndInANewProcessRaisesIt() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "gamma");
        env.bind(t, id, "start", 0, ev("s1", 60, 600));
        Resp same = env.bind(t, id, "resume", 1, ev("s1-again", 60, 600));
        assertThat(same.integer("generation")).isEqualTo(1);
        assertThat(same.body().get("generation_changed")).isEqualTo(false);
        // The process ends cleanly; a resume in a new process takes it with a new generation.
        assertThat(env.bind(t, id, "end", 1, Map.of()).status()).isEqualTo(200);
        assertThat(env.show(t, id)).containsEntry("state", "ended");
        Resp again = env.bind(t, id, "resume", 1, ev("s1", 61, 601));
        assertThat(again.status()).isEqualTo(200);
        assertThat(again.integer("generation")).isEqualTo(2);
        assertThat(again.str("state")).isEqualTo("live");
        assertThat(env.events(t, id)).containsExactly("start", "resume", "end", "resume");
    }

    @Test
    void aSecondProcessIsRefusedWhileTheHolderIsLiveAndTakeOverSupersedesIt() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "delta");
        env.bind(t, id, "start", 0, ev("s1", 70, 700));
        Resp refused = env.bind(t, id, "start", 1, ev("s2", 71, 701));
        assertThat(refused.status()).isEqualTo(409);
        assertThat(refused.error()).isEqualTo("held");
        assertThat(refused.map("holder")).containsEntry("pid", 70).containsEntry("host", "h1");
        assertThat(env.show(t, id).get("generation")).isEqualTo(1);
        Resp take = env.bind(t, id, "takeover", 1, ev("s2", 71, 701));
        assertThat(take.status()).isEqualTo(200);
        assertThat(take.integer("generation")).isEqualTo(2);
        assertThat(take.map("holder")).containsEntry("pid", 71);
        // The superseded process, still at generation 1, can no longer end or renew it.
        assertThat(env.bind(t, id, "end", 1, Map.of()).error()).isEqualTo("generation_conflict");
        assertThat(env.post(t, "/" + id + "/renew", Map.of("generation", 1)).error()).isEqualTo("generation_conflict");
    }

    @Test
    void aVerdictThatTheHolderIsGoneFreesALiveSeatAndIsRecordedInTheRow() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "epsilon");
        Map<String, Object> old = ev("s1", 80, 800);
        env.bind(t, id, "start", 0, old);
        Resp r = env.bind(t, id, "start", 1, with(ev("s2", 81, 801), "holder_gone", verdictOf(old)));
        assertThat(r.status()).isEqualTo(200);
        assertThat(r.integer("generation")).isEqualTo(2);
        @SuppressWarnings("unchecked")
        Map<String, Object> recorded = (Map<String, Object>) env.log(t, id).get(1).get("holder_gone");
        assertThat(recorded).containsEntry("host", "h1").containsEntry("pid", 80).containsEntry("proc_start", old.get("proc_start"));
        assertThat(((Number) recorded.get("started_at")).longValue()).isEqualTo(800L);
        // A verdict that names some other process than the current holder is refused.
        Resp wrong = env.bind(t, id, "start", 2, with(ev("s3", 82, 802), "holder_gone", verdictOf(old)));
        assertThat(wrong.status()).isEqualTo(409);
        assertThat(wrong.error()).isEqualTo("holder_mismatch");
    }

    @Test
    void anInPlaceRestartRebindsWithAFreshStartedAtAfterTheClientFoundTheOldOneGone() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "zeta");
        Map<String, Object> before = ev("s1", 90, 900);
        env.bind(t, id, "start", 0, before);
        // Same pid and proc_start, a new started_at: a new process by the RDR's definition.
        Map<String, Object> after = with(before, "session_id", "s2", "started_at", 901L);
        Resp r = env.bind(t, id, "resume", 1, with(after, "holder_gone", verdictOf(before)));
        assertThat(r.integer("generation")).isEqualTo(2);
    }

    @Test
    void releaseFreesAGoneHoldersSeatAndEndClosesTheHoldersOwn() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "eta");
        Map<String, Object> holder = ev("s1", 95, 950);
        env.bind(t, id, "start", 0, holder);
        // A stranger without a verdict is refused; with one it frees the seat.
        Resp refused = env.bind(t, id, "release", 1, ev("s9", 96, 960));
        assertThat(refused.status()).isEqualTo(409);
        Resp freed = env.bind(t, id, "release", 1, with(ev("s9", 96, 960), "holder_gone", verdictOf(holder)));
        assertThat(freed.status()).isEqualTo(200);
        assertThat(freed.str("state")).isEqualTo("unheld");
        assertThat(freed.integer("generation")).isEqualTo(1);
        assertThat(freed.body().get("holder")).isNull();
        // Releasing a free seat is a no-op.
        assertThat(env.bind(t, id, "release", 1, Map.of()).body().get("noop")).isEqualTo(true);
        // A clear or an end on an unheld seat names nobody to act for.
        assertThat(env.bind(t, id, "clear", 1, Map.of("session_id", "s5")).error()).isEqualTo("not_held");
        assertThat(env.bind(t, id, "end", 1, Map.of()).error()).isEqualTo("not_held");
        // The holder itself releases its own seat with no verdict.
        env.bind(t, id, "start", 1, ev("s2", 97, 970));
        assertThat(env.bind(t, id, "release", 2, ev("s2", 97, 970)).str("state")).isEqualTo("unheld");
        // end is idempotent: a second end of the same generation is a no-op.
        env.bind(t, id, "start", 2, ev("s3", 98, 980));
        assertThat(env.bind(t, id, "end", 3, Map.of()).body().get("noop")).isEqualTo(false);
        assertThat(env.bind(t, id, "end", 3, Map.of()).body().get("noop")).isEqualTo(true);
    }

    @Test
    void anUnleasedBindingIsRecordedAsUnleased() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "theta");
        Resp r = env.bind(t, id, "start", 0, with(ev("s1", 5, 50), "lease", "unleased"));
        assertThat(r.str("lease")).isEqualTo("unleased");
        assertThat(env.log(t, id).get(0)).containsEntry("lease", "unleased");
        assertThat(env.bind(t, id, "clear", 1, Map.of("session_id", "s2")).str("lease")).isEqualTo("unleased");
        assertThat(env.bind(t, id, "start", 1, with(ev("s3", 6, 60), "lease", "forever")).status()).isEqualTo(400);
    }

    @Test
    void ephemeralAndDelegatedIdentitiesAreCreatedAlreadyBoundAndEphemeralsPromoteToSeats() {
        String t = IdentityTestEnv.freshTenant();
        Resp eph = env.post(t, "", Map.of("kind", "ephemeral", "display_name", "scratch42", "created_by", "session",
            "bind", ev("e1", 300, 3000)));
        assertThat(eph.status()).isEqualTo(200);
        assertThat(eph.str("kind")).isEqualTo("ephemeral");
        assertThat(eph.integer("generation")).isEqualTo(1);
        assertThat(eph.str("state")).isEqualTo("live");
        assertThat(eph.body().get("name")).as("an ephemeral identity takes no alias").isNull();
        String id = eph.str("id");
        assertThat(env.get(t, "/alias/scratch42").status()).isEqualTo(404);

        Resp child = env.post(t, "/delegated", Map.of("parent", id, "display_name", "child", "bind", evOn("h1", "c1", 301, 3001)));
        assertThat(child.status()).isEqualTo(200);
        assertThat(child.str("kind")).isEqualTo("delegated");
        assertThat(child.str("parent")).isEqualTo(id);
        assertThat(child.integer("generation")).isEqualTo(1);
        assertThat(env.events(t, child.str("id"))).containsExactly("start");
        assertThat(env.post(t, "/delegated", Map.of("parent", "i424242", "bind", ev("c2", 302, 3002))).status()).isEqualTo(404);
        assertThat(env.post(t, "/" + child.str("id") + "/promote", Map.of("name", "nope")).error()).isEqualTo("not_ephemeral");

        Resp promoted = env.post(t, "/" + id + "/promote", Map.of("name", "scratch42"));
        assertThat(promoted.status()).isEqualTo(200);
        assertThat(promoted.str("kind")).isEqualTo("seat");
        assertThat(promoted.str("name")).isEqualTo("scratch42");
        assertThat(env.get(t, "/alias/scratch42").str("identity")).isEqualTo(id);
        assertThat(env.post(t, "/" + id + "/promote", Map.of("name", "again")).error()).isEqualTo("not_ephemeral");
    }
}
