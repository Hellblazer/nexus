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
import static dev.nexus.service.http.IdentityTestEnv.verdictOf;
import static dev.nexus.service.http.IdentityTestEnv.with;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_ALIASES;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_BINDINGS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_COUNTERS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_WORK;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1): retention, on a clock the test sets. Ephemeral and delegated identities are
 * tombstoned 30 days after they end, or after 30 days lapsed with the engine holding a {@code gone} verdict;
 * seats are never tombstoned; a seat's binding rows go after 180 days but never its newest; the counter keeps its
 * numbers. The board entries and tuples are fixtures: no client writes the identity line until RDR-253 Step 5.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityRetentionTest {

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

    private String ephemeral(String t, String sid, int pid, Map<String, Object> extra) {
        Resp r = env.post(t, "", Map.of("kind", "ephemeral", "created_by", "session",
            "bind", with(ev(sid, pid, 10_000L + pid), extra.isEmpty() ? new Object[0] : flatten(extra))));
        assertThat(r.status()).isEqualTo(200);
        return r.str("id");
    }

    private static Object[] flatten(Map<String, Object> m) {
        return m.entrySet().stream().flatMap(e -> java.util.stream.Stream.of(e.getKey(), e.getValue())).toArray();
    }

    private int count(String t, org.jooq.Table<?> table, org.jooq.Field<String> col) {
        return env.rows(t, table, col);
    }

    @Test
    void anEndedEphemeralIsTombstonedAtThirtyDaysWithEverythingItOwned() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = ephemeral(t, "e1", 1, Map.of());
        String keep = ephemeral(t, "e2", 2, Map.of());
        env.bind(t, id, "clear", 1, Map.of("session_id", "e1b"));
        env.bind(t, id, "end", 1, Map.of());
        env.putThreadsEntry(t, "scratch42 (" + id + ")", id);
        env.putThreadsEntry(t, "kept entry", keep);
        env.putThreadsEntry(t, "no identity", null);
        env.putTuple(t, "mailbox/" + id);
        env.putTuple(t, "mailbox/" + keep);
        for (String who : new String[] {id, keep}) {
            assertThat(env.post(t, "/" + who + "/memberships", Map.of("group", "crew", "role", "leader")).status()).isEqualTo(200);
            assertThat(env.post(t, "/" + who + "/work", Map.of("kind", "worktree", "ref", "/tmp/wt-" + who, "state", "running")).status()).isEqualTo(200);
        }

        env.clock.set(T0.plus(Duration.ofDays(30)).minusSeconds(1));
        assertThat(env.repo.runRetention(t)).containsEntry("tombstoned", 0);
        assertThat(env.get(t, "/" + id).status()).isEqualTo(200);

        env.clock.set(T0.plus(Duration.ofDays(30)));
        assertThat(env.repo.runRetention(t)).containsEntry("tombstoned", 1);
        assertThat(env.get(t, "/" + id).status()).isEqualTo(404);
        assertThat(env.get(t, "/sessions/e1").status()).as("its session index rows").isEqualTo(404);
        assertThat(env.get(t, "/sessions/e1b").status()).isEqualTo(404);
        assertThat(env.threadsTitles(t)).containsExactly("kept entry", "no identity");
        assertThat(env.tupleCount(t, "mailbox/" + id)).as("mailbox").isZero();
        assertThat(env.tupleCount(t, "mailbox/" + keep)).as("mailbox of another identity").isEqualTo(1);
        assertThat(env.get(t, "/" + keep + "/work").list("work")).as("the other identity's work stays").hasSize(1);
        assertThat(env.rows(t, IDENTITY_WORK, IDENTITY_WORK.TENANT_ID)).as("the tombstone took its work").isEqualTo(1);
        assertThat(env.get(t, "/" + keep).status()).isEqualTo(200);
        assertThat(env.get(t, "/groups/crew").list("members")).extracting(m -> m.get("identity")).as("the tombstone took its memberships").containsExactly(keep);
        assertThat(count(t, IDENTITY_BINDINGS, IDENTITY_BINDINGS.TENANT_ID)).as("only the other identity's rows are left: its start and the lapse the pass applied").isEqualTo(2);
        var audit = env.audit(t, "identity_tombstone");
        assertThat(audit).hasSize(1);
        assertThat(audit.get(0).get(0)).isEqualTo("engine");
        assertThat(audit.get(0).get(1)).contains(id).contains("board_entries");
        // A second pass finds nothing more.
        assertThat(env.repo.runRetention(t)).containsEntry("tombstoned", 0);
        assertThat(count(t, IDENTITY_COUNTERS, IDENTITY_COUNTERS.TENANT_ID)).as("the counter stays").isEqualTo(1);
    }

    @Test
    void anIdentityLapsedWithItsHolderGoneIsTombstonedThirtyDaysAfterTheGoneRowAndOneWithoutIsKept() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String gone = ephemeral(t, "g1", 3, Map.of());
        String silent = ephemeral(t, "g2", 4, Map.of());
        String unleased = ephemeral(t, "g3", 5, Map.of("lease", "unleased"));
        env.clock.set(T0.plusSeconds(1000));
        // g1 and g3 are reported gone at T0+1000s; g2 simply stopped renewing.
        assertThat(env.post(t, "/" + gone + "/gone", Map.of("expected_generation", 1,
            "holder_gone", verdictOf(ev("g1", 3, 10_003L)))).status()).isEqualTo(200);
        assertThat(env.post(t, "/" + unleased + "/gone", Map.of("expected_generation", 1,
            "holder_gone", verdictOf(ev("g3", 5, 10_005L)))).status()).isEqualTo(200);

        env.clock.set(T0.plusSeconds(1000).plus(Duration.ofDays(30)).minusSeconds(1));
        assertThat(env.repo.runRetention(t)).containsEntry("tombstoned", 0);
        env.clock.set(T0.plusSeconds(1000).plus(Duration.ofDays(30)));
        assertThat(env.repo.runRetention(t)).containsEntry("tombstoned", 2);
        assertThat(env.get(t, "/" + gone).status()).isEqualTo(404);
        assertThat(env.get(t, "/" + unleased).status()).isEqualTo(404);
        env.clock.set(T0.plus(Duration.ofDays(400)));
        assertThat(env.get(t, "/" + silent).str("state")).as("lapsed, never reported gone").isEqualTo("lapsed");
        assertThat(env.repo.runRetention(t)).containsEntry("tombstoned", 0);
        assertThat(env.get(t, "/" + silent).status()).isEqualTo(200);
    }

    @Test
    void aDelegatedIdentityWhoseDispatcherDiedIsTombstonedThirtyDaysAfterItLapsed() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String parent = env.createSeat(t, "parent");
        Resp child = env.post(t, "/delegated", Map.of("parent", parent, "bind", ev("c1", 7, 70)));
        String id = child.str("id");
        // Nothing reads the dead child, so its lapse is applied by the retention pass itself, 300 seconds after T0.
        env.clock.set(T0.plusSeconds(300).plus(Duration.ofDays(30)).minusSeconds(1));
        assertThat(env.repo.runRetention(t)).as("lapsed 30 days minus a second ago").containsEntry("tombstoned", 0);
        env.clock.set(T0.plusSeconds(300).plus(Duration.ofDays(30)));
        assertThat(env.repo.runRetention(t)).containsEntry("tombstoned", 1);
        assertThat(env.get(t, "/" + id).status()).isEqualTo(404);
        assertThat(env.get(t, "/" + parent).status()).as("the seat is permanent").isEqualTo(200);
    }

    @Test
    void aSeatIsNeverTombstonedAndKeepsItsLastBindingRowWhileOlderOnesGoAfter180Days() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "oldseat");
        String lone = env.createSeat(t, "loneseat");
        env.bind(t, lone, "start", 0, ev("lone-s", 9, 90));
        env.bind(t, id, "start", 0, ev("s1", 8, 80));
        env.clock.set(T0.plus(Duration.ofDays(1)));
        env.bind(t, id, "end", 1, Map.of());
        env.clock.set(T0.plus(Duration.ofDays(2)));
        env.bind(t, id, "start", 1, ev("s2", 10, 100));
        env.clock.set(T0.plus(Duration.ofDays(100)));
        env.bind(t, id, "clear", 2, Map.of("session_id", "s3"));
        // The seat lapsed on day 1 (nobody renewed), before it ended; the second process lapsed before the clear.
        assertThat(env.events(t, id)).containsExactly("start", "lapse", "end", "start", "lapse", "clear");

        env.clock.set(T0.plus(Duration.ofDays(179)));
        assertThat(env.repo.runRetention(t)).containsEntry("bindings_pruned", 0);
        env.clock.set(T0.plus(Duration.ofDays(183)));
        Map<String, Integer> pass = env.repo.runRetention(t);
        assertThat(pass).containsEntry("tombstoned", 0);
        assertThat(pass.get("bindings_pruned")).as("everything but the clear; lone's only row stays").isEqualTo(5);
        assertThat(env.events(t, id)).as("the newest row stays").containsExactly("clear");
        assertThat(env.events(t, lone)).as("never the last one").containsExactly("start");
        assertThat(env.get(t, "/sessions/s1").status()).isEqualTo(404);
        assertThat(env.get(t, "/sessions/s2").status()).isEqualTo(404);
        assertThat(env.get(t, "/sessions/s3").str("identity")).isEqualTo(id);
        assertThat(env.get(t, "/sessions/lone-s").status()).isEqualTo(200);
        // Years later the seat is still there, with its alias, its generation and its newest row.
        env.clock.set(T0.plus(Duration.ofDays(1500)));
        assertThat(env.repo.runRetention(t)).containsEntry("tombstoned", 0);
        assertThat(env.events(t, id)).isNotEmpty();
        assertThat(count(t, IDENTITY_ALIASES, IDENTITY_ALIASES.TENANT_ID)).isEqualTo(2);
        assertThat(count(t, IDENTITY_RECORDS, IDENTITY_RECORDS.TENANT_ID)).isEqualTo(2);
        assertThat(env.show(t, id)).containsEntry("generation", 2).containsEntry("name", "oldseat");
    }
}
