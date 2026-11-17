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
import java.util.Map;

import static dev.nexus.service.http.IdentityTestEnv.ev;
import static dev.nexus.service.http.IdentityTestEnv.with;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1), the requirement RDR-252's fix check raised: a membership must last as long as
 * its seat does, so it is a permanent row (a tuple's expiry cannot be extended), kept until the identity leaves the
 * group or is tombstoned, and a group's roster reads in join order. Also the liveness the routes report: a due
 * lapse is applied on read, and an unleased binding's liveness is unknown; and the session lookup other engine
 * components call.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityMembershipTest {

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

    @Test
    void aMembershipStillReadsSixtyDaysAfterItWasWrittenWithNoRenewals() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "member1");
        env.bind(t, id, "start", 0, ev("s1", 1, 10));
        assertThat(env.asOwner(t, id, "POST", "/" + id + "/memberships", Map.of("group", "crew", "role", "leader")).status()).isEqualTo(200);
        // Nothing renews: the seat lapses on day one and a tuple with a retention would have purged long ago.
        for (int days : new int[] {60, 400}) {
            env.clock.set(T0.plus(Duration.ofDays(days)));
            assertThat(env.repo.runRetention(t)).containsEntry("tombstoned", 0);
            var roster = env.get(t, "/groups/crew").list("members");
            assertThat(roster).as("day %d", days).hasSize(1);
            assertThat(roster.get(0)).containsEntry("identity", id).containsEntry("role", "leader")
                .containsEntry("joined_at", T0.toString());
            assertThat(env.get(t, "/" + id + "/memberships").list("memberships")).as("day %d", days).hasSize(1);
        }
    }

    @Test
    void aRosterReadsInJoinOrderARoleChangeKeepsTheJoinTimeAndLeavingRemovesTheRow() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String a = env.createSeat(t, "alpha");
        String b = env.createSeat(t, "bravo");
        String c = env.createSeat(t, "charlie");
        int pid = 20;
        for (String who : new String[] {a, b, c}) {
            env.bind(t, who, "start", 0, ev("s-" + who, pid, pid * 10L)); // a membership write needs a seat that is held
            pid++;
        }
        env.clock.set(T0.plusSeconds(10));
        env.asOwner(t, b, "POST", "/" + b + "/memberships", Map.of("group", "crew", "role", "member"));
        env.clock.set(T0.plusSeconds(20));
        env.asOwner(t, c, "POST", "/" + c + "/memberships", Map.of("group", "crew", "role", "member"));
        env.clock.set(T0.plusSeconds(30));
        env.asOwner(t, a, "POST", "/" + a + "/memberships", Map.of("group", "crew", "role", "member"));
        env.asOwner(t, a, "POST", "/" + a + "/memberships", Map.of("group", "other", "role", "leader"));
        assertThat(env.get(t, "/groups/crew").list("members")).extracting(m -> m.get("identity"))
            .as("join order, not id order").containsExactly(b, c, a);

        // A duplicate add is idempotent: one row, the first join time, whatever the clock says now.
        env.clock.set(T0.plusSeconds(50));
        var again = env.asOwner(t, c, "POST", "/" + c + "/memberships", Map.of("group", "crew", "role", "member"));
        assertThat(again.status()).isEqualTo(200);
        assertThat(again.str("joined_at")).isEqualTo(T0.plusSeconds(20).toString());
        assertThat(env.get(t, "/groups/crew").list("members")).hasSize(3);

        env.clock.set(T0.plusSeconds(99));
        var changed = env.asOwner(t, b, "POST", "/" + b + "/memberships", Map.of("group", "crew", "role", "leader"));
        assertThat(changed.str("role")).isEqualTo("leader");
        assertThat(changed.str("joined_at")).isEqualTo(T0.plusSeconds(10).toString());
        assertThat(env.get(t, "/groups/crew").list("members")).extracting(m -> m.get("identity")).containsExactly(b, c, a);
        assertThat(env.get(t, "/" + a + "/memberships").list("memberships")).extracting(m -> m.get("group"))
            .containsExactly("crew", "other");

        var left = env.asOwner(t, c, "DELETE", "/" + c + "/memberships/crew", null);
        assertThat(left.status()).isEqualTo(200);
        assertThat(left.integer("removed")).isEqualTo(1);
        assertThat(env.asOwner(t, c, "DELETE", "/" + c + "/memberships/crew", null).integer("removed")).isZero();
        assertThat(env.get(t, "/groups/crew").list("members")).extracting(m -> m.get("identity")).containsExactly(b, a);
        assertThat(env.get(t, "/groups/nobody").list("members")).isEmpty();
    }

    @Test
    void membershipsRefuseABadRoleGroupOrIdentityAndAdmitTheDataCredential() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "checked");
        env.bind(t, id, "start", 0, ev("s-checked", 30, 300));
        assertThat(env.asOwner(t, id, "POST", "/" + id + "/memberships", Map.of("group", "crew", "role", "boss")).error()).isEqualTo("bad_role");
        assertThat(env.asOwner(t, id, "POST", "/" + id + "/memberships", Map.of("group", "-x", "role", "member")).error()).isEqualTo("bad_group");
        assertThat(env.post(t, "/i424242/memberships", Map.of("group", "crew", "role", "member")).status()).isEqualTo(404);
        assertThat(env.get(t, "/i424242/memberships").status()).isEqualTo(404);
        // The MCP server's data credential may join, list and leave; the same routes are shared with the tenant one.
        assertThat(env.asOwner(IdentityTestEnv.DATA, t, id, "POST", "/" + id + "/memberships",
            Map.of("group", "crew", "role", "member")).status()).isEqualTo(200);
        assertThat(env.call(IdentityTestEnv.DATA, t, "GET", "/groups/crew", null).list("members")).hasSize(1);
        assertThat(env.asOwner(IdentityTestEnv.DATA, t, id, "DELETE", "/" + id + "/memberships/crew", null).integer("removed")).isEqualTo(1);
        assertThat(env.call("mint", t, "GET", "/groups/crew", null).status()).as("any other scope").isEqualTo(403);
    }

    @Test
    void workIsRegisteredAndItsStateChangesInPlaceAndItLastsPastAnyRetention() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "worker");
        env.bind(t, id, "start", 0, ev("w1", 1, 10));
        var first = env.post(t, "/" + id + "/work", Map.of("kind", "worktree", "ref", "/tmp/wt-a", "state", "running"));
        assertThat(first.status()).isEqualTo(200);
        env.post(t, "/" + id + "/work", Map.of("kind", "tmux", "ref", "srv", "state", "running"));
        env.clock.set(T0.plus(Duration.ofDays(90)));
        var again = env.call(IdentityTestEnv.DATA, t, "POST", "/" + id + "/work", Map.of("kind", "worktree", "ref", "/tmp/wt-a", "state", "done"));
        assertThat(again.str("state")).isEqualTo("done");
        assertThat(again.str("updated_at")).isEqualTo(env.clock.instant().toString());
        var rows = env.get(t, "/" + id + "/work").list("work");
        assertThat(rows).as("one row per (kind, ref), the state changed in place").hasSize(2);
        assertThat(rows).filteredOn(m -> "worktree".equals(m.get("kind"))).singleElement()
            .satisfies(m -> assertThat(m).containsEntry("state", "done").containsEntry("ref", "/tmp/wt-a"));
        assertThat(rows).filteredOn(m -> "tmux".equals(m.get("kind"))).singleElement()
            .satisfies(m -> assertThat(m).containsEntry("state", "running"));
        env.clock.set(T0.plus(Duration.ofDays(400)));
        assertThat(env.repo.runRetention(t)).containsEntry("tombstoned", 0);
        assertThat(env.get(t, "/" + id + "/work").list("work")).as("no expiry, unlike a tuple").hasSize(2);

        var gone = env.call(IdentityTestEnv.TENANT, t, "DELETE", "/" + id + "/work?kind=tmux&ref=srv", null);
        assertThat(gone.integer("removed")).isEqualTo(1);
        assertThat(env.call(IdentityTestEnv.TENANT, t, "DELETE", "/" + id + "/work?kind=tmux&ref=srv", null).integer("removed")).isZero();
        assertThat(env.get(t, "/" + id + "/work").list("work")).hasSize(1);

        assertThat(env.post(t, "/" + id + "/work", Map.of("kind", "bogus", "ref", "x", "state", "running")).error()).isEqualTo("bad_kind");
        assertThat(env.post(t, "/" + id + "/work", Map.of("kind", "wait", "ref", "x", "state", "stuck")).error()).isEqualTo("bad_state");
        assertThat(env.post(t, "/" + id + "/work", Map.of("kind", "wait", "ref", " ", "state", "running")).error()).isEqualTo("bad_ref");
        assertThat(env.post(t, "/i424242/work", Map.of("kind", "wait", "ref", "x", "state", "running")).status()).isEqualTo(404);
        assertThat(env.get(t, "/i424242/work").status()).isEqualTo(404);
    }

    @Test
    void theReverseLookupFindsEveryIdentityThatOwnsAResource() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.createSeat(t, "owner-a");
        String b = env.createSeat(t, "owner-b");
        String c = env.createSeat(t, "owner-c");
        env.post(t, "/" + b + "/work", Map.of("kind", "worktree", "ref", "/tmp/shared", "state", "running"));
        env.post(t, "/" + a + "/work", Map.of("kind", "worktree", "ref", "/tmp/shared", "state", "done"));
        env.post(t, "/" + c + "/work", Map.of("kind", "worktree", "ref", "/tmp/other", "state", "running"));
        env.post(t, "/" + c + "/work", Map.of("kind", "tmux", "ref", "/tmp/shared", "state", "running"));
        var r = env.get(t, "/work?kind=worktree&ref=/tmp/shared");
        assertThat(r.status()).isEqualTo(200);
        assertThat(r.list("owners")).extracting(m -> m.get("identity")).as("several owners, by identity id").containsExactly(a, b);
        assertThat(r.list("owners")).extracting(m -> m.get("state")).containsExactly("done", "running");
        assertThat(env.call(IdentityTestEnv.DATA, t, "GET", "/work?kind=tmux&ref=/tmp/shared", null).list("owners"))
            .extracting(m -> m.get("identity")).containsExactly(c);
        assertThat(env.get(t, "/work?kind=wait&ref=nobody").list("owners")).isEmpty();
        assertThat(env.get(t, "/work?kind=bogus&ref=x").error()).isEqualTo("bad_kind");
        assertThat(env.get(t, "/work").status()).isEqualTo(400);
        assertThat(env.get(IdentityTestEnv.freshTenant(), "/work?kind=worktree&ref=/tmp/shared").list("owners")).as("another tenant").isEmpty();
        assertThat(env.call("mint", t, "GET", "/work?kind=worktree&ref=/tmp/shared", null).status()).isEqualTo(403);
    }

    @Test
    void anIdentityHoldsAtMostTwoHundredWorkRows() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "busy");
        for (int n = 1; n <= 200; n++) {
            assertThat(env.post(t, "/" + id + "/work", Map.of("kind", "watcher", "ref", "w" + n, "state", "running")).status())
                .as("row %d", n).isEqualTo(200);
        }
        var over = env.post(t, "/" + id + "/work", Map.of("kind", "watcher", "ref", "w201", "state", "running"));
        assertThat(over.status()).isEqualTo(409);
        assertThat(over.error()).isEqualTo("work_limit");
        assertThat(env.get(t, "/" + id + "/work").list("work")).hasSize(200);
        // A change of state to a row that exists is not a new row, so it is allowed at the limit.
        assertThat(env.post(t, "/" + id + "/work", Map.of("kind", "watcher", "ref", "w7", "state", "done")).status()).isEqualTo(200);
        // Forgetting one makes room for exactly one.
        env.call(IdentityTestEnv.TENANT, t, "DELETE", "/" + id + "/work?kind=watcher&ref=w1", null);
        assertThat(env.post(t, "/" + id + "/work", Map.of("kind", "watcher", "ref", "w201", "state", "running")).status()).isEqualTo(200);
        assertThat(env.post(t, "/" + id + "/work", Map.of("kind", "watcher", "ref", "w202", "state", "running")).error()).isEqualTo("work_limit");
        // The limit is per identity.
        String other = env.createSeat(t, "idle");
        assertThat(env.post(t, "/" + other + "/work", Map.of("kind", "watcher", "ref", "w1", "state", "running")).status()).isEqualTo(200);
    }

    @Test
    void aDueLapseIsAppliedOnEveryReadAndAnUnleasedBindingsLivenessIsUnknown() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String leased = env.createSeat(t, "leasedseat");
        String unleased = env.createSeat(t, "unleasedseat");
        env.bind(t, leased, "start", 0, ev("l1", 5, 50));
        env.bind(t, unleased, "start", 0, with(ev("u1", 6, 60), "lease", "unleased"));
        assertThat(env.show(t, leased)).containsEntry("liveness", "live");
        assertThat(env.show(t, unleased)).containsEntry("liveness", "unknown").containsEntry("state", "live");

        env.clock.set(T0.plusSeconds(301));
        // The stored state is still live; every read route reports what the rule says, not the stale row.
        assertThat(env.show(t, leased)).containsEntry("state", "lapsed").containsEntry("liveness", "lapsed");
        String other = env.createSeat(t, "second");
        env.clock.set(T0.plusSeconds(1000));
        env.bind(t, other, "start", 0, ev("o1", 7, 70));
        env.clock.set(T0.plusSeconds(1000 + 301));
        assertThat(env.get(t, "/sessions/o1")).extracting(r -> r.str("state"), r -> r.str("liveness"))
            .containsExactly("lapsed", "lapsed");
        assertThat(env.get(t, "/sessions/u1").str("liveness")).isEqualTo("unknown");
        var hostRows = env.get(t, "/bindings?host=h1").list("bindings");
        assertThat(hostRows).filteredOn(m -> unleased.equals(m.get("id"))).singleElement()
            .satisfies(m -> assertThat(m).containsEntry("liveness", "unknown"));
    }

    @Test
    void theSessionLookupIsAPlainEngineSideCallForOtherComponents() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "stamped");
        env.bind(t, id, "start", 0, ev("sender-session", 8, 80));
        env.bind(t, id, "clear", 1, Map.of("session_id", "sender-session-2"));
        var found = env.repo.sessionIdentity(t, "sender-session");
        assertThat(found).isPresent();
        assertThat(found.get().identity()).isEqualTo(id);
        assertThat(found.get().generation()).isEqualTo(1);
        assertThat(env.repo.sessionIdentity(t, "sender-session-2")).isPresent();
        assertThat(env.repo.sessionIdentity(t, "nobody")).isEmpty();
        assertThat(env.repo.sessionIdentity(IdentityTestEnv.freshTenant(), "sender-session")).as("another tenant").isEmpty();
    }
}
