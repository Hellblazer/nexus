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

import static dev.nexus.service.http.IdentityTestEnv.DATA;
import static dev.nexus.service.http.IdentityTestEnv.TENANT;
import static dev.nexus.service.http.IdentityTestEnv.ev;
import static dev.nexus.service.http.IdentityTestEnv.verdictOf;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1): the operator purge. The tenant credential may remove the fenced state of an
 * identity that is not live (ended, unheld, or lapsed with its holder recorded gone); it is refused for a live
 * identity and for a lapsed one nobody has reported gone, the data credential never reaches it, and every purge is
 * written to {@code gc_audit} with the caller and what it removed.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityPurgeTest {

    static final Instant T0 = Instant.parse("2026-11-16T12:00:00Z");
    static final Map<String, Object> OPERATOR = Map.of("operator", true);

    IdentityTestEnv env;

    @BeforeAll
    void up() throws Exception {
        env = IdentityTestEnv.start();
    }

    @AfterAll
    void down() throws Exception {
        env.close();
    }

    private void fixtures(String t, String id) {
        env.putThreadsEntry(t, "entry of " + id, id);
        env.putThreadsEntry(t, "somebody else", null);
        env.putTuple(t, "mailbox/" + id);
        env.putTuple(t, "mailbox/" + id);
        assertThat(env.post(t, "/" + id + "/memberships", Map.of("group", "crew", "role", "member")).status()).isEqualTo(200);
        env.putTuple(t, "work/" + id);
    }

    @Test
    void aLiveIdentityIsRefusedAndNothingIsRemoved() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "alive");
        env.bind(t, id, "start", 0, ev("s1", 1, 10));
        fixtures(t, id);
        Resp r = env.post(t, "/" + id + "/purge", OPERATOR);
        assertThat(r.status()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("identity_live");
        assertThat(env.threadsTitles(t)).contains("entry of " + id);
        assertThat(env.tupleCount(t, "mailbox/" + id)).isEqualTo(2);
        assertThat(env.audit(t, "identity_purge")).isEmpty();
    }

    @Test
    void aLapsedIdentityIsRefusedUntilItsHolderIsRecordedGone() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "asleep");
        var holder = ev("s1", 2, 20);
        env.bind(t, id, "start", 0, holder);
        fixtures(t, id);
        env.clock.set(T0.plus(Duration.ofHours(1)));
        Resp lapsed = env.post(t, "/" + id + "/purge", OPERATOR);
        assertThat(lapsed.status()).isEqualTo(409);
        assertThat(lapsed.error()).isEqualTo("holder_not_gone");
        env.post(t, "/" + id + "/gone", Map.of("expected_generation", 1, "holder_gone", verdictOf(holder)));
        Resp ok = env.post(t, "/" + id + "/purge", OPERATOR);
        assertThat(ok.status()).isEqualTo(200);
        assertThat(env.tupleCount(t, "mailbox/" + id)).isZero();
    }

    @Test
    void anEndedSeatsBoardEntryMailboxMembershipsAndWorkAreDeletedAndTheRecordStays() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "finished");
        String other = env.createSeat(t, "neighbour");
        env.bind(t, id, "start", 0, ev("s1", 3, 30));
        env.bind(t, id, "end", 1, Map.of());
        fixtures(t, id);
        env.putTuple(t, "mailbox/" + other);
        env.clock.set(T0.plusSeconds(5));

        Resp r = env.post(t, "/" + id + "/purge", OPERATOR);
        assertThat(r.status()).isEqualTo(200);
        assertThat(r.body().get("tombstoned")).isEqualTo(false);
        assertThat(r.map("removed")).containsEntry("board_entries", 1).containsEntry("mailbox", 2)
            .containsEntry("member", 1).containsEntry("work", 1);
        assertThat(env.threadsTitles(t)).containsExactly("somebody else");
        for (String fam : new String[] {"mailbox", "work"}) {
            assertThat(env.tupleCount(t, fam + "/" + id)).as(fam).isZero();
        }
        assertThat(env.tupleCount(t, "mailbox/" + other)).isEqualTo(1);
        assertThat(env.get(t, "/" + id + "/memberships").list("memberships")).as("the purge removed the memberships").isEmpty();
        // A seat's record, alias and binding rows are permanent history.
        assertThat(env.show(t, id)).containsEntry("state", "ended").containsEntry("name", "finished");
        assertThat(env.events(t, id)).containsExactly("start", "end");
        // The removal is logged with the caller and what it removed.
        var audit = env.audit(t, "identity_purge");
        assertThat(audit).hasSize(1);
        assertThat(audit.get(0).get(0)).isEqualTo("operator:tenant");
        assertThat(audit.get(0).get(1)).contains(id).contains("\"board_entries\"").contains("\"mailbox\"");
        // A second purge finds nothing and says so, and is logged too.
        Resp again = env.post(t, "/" + id + "/purge", OPERATOR);
        assertThat(again.map("removed")).containsEntry("board_entries", 0).containsEntry("mailbox", 0);
        assertThat(env.audit(t, "identity_purge")).hasSize(2);
    }

    @Test
    void anEphemeralIdentityIsTombstonedByAPurgeAndAnUnheldSeatMayBePurged() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        Resp eph = env.post(t, "", Map.of("kind", "ephemeral", "created_by", "session", "bind", ev("e1", 4, 40)));
        String id = eph.str("id");
        env.bind(t, id, "end", 1, Map.of());
        fixtures(t, id);
        Resp r = env.post(t, "/" + id + "/purge", OPERATOR);
        assertThat(r.body().get("tombstoned")).isEqualTo(true);
        assertThat(env.get(t, "/" + id).status()).isEqualTo(404);
        assertThat(env.get(t, "/sessions/e1").status()).isEqualTo(404);

        String unheld = env.createSeat(t, "never");
        env.putThreadsEntry(t, "entry of " + unheld, unheld);
        assertThat(env.post(t, "/" + unheld + "/purge", OPERATOR).status()).isEqualTo(200);
        assertThat(env.threadsTitles(t)).doesNotContain("entry of " + unheld);
    }

    @Test
    void thePurgeNeedsTheOperatorFlagAndTheDataCredentialNeverReachesIt() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "guarded");
        env.bind(t, id, "start", 0, ev("s1", 5, 50));
        env.bind(t, id, "end", 1, Map.of());
        fixtures(t, id);
        assertThat(env.post(t, "/" + id + "/purge", Map.of()).error()).isEqualTo("operator_required");
        assertThat(env.post(t, "/" + id + "/purge", Map.of("operator", false)).error()).isEqualTo("operator_required");
        Resp data = env.call(DATA, t, "POST", "/" + id + "/purge", OPERATOR);
        assertThat(data.status()).isEqualTo(403);
        assertThat(env.tupleCount(t, "mailbox/" + id)).isEqualTo(2);
        assertThat(env.call("root", t, "POST", "/" + id + "/purge", OPERATOR).status()).isEqualTo(200);
        assertThat(env.audit(t, "identity_purge").get(0).get(0)).isEqualTo("operator:root");
        assertThat(env.call(TENANT, t, "POST", "/i424242/purge", OPERATOR).status()).isEqualTo(404);
        assertThat(env.post(t, "/not-an-id/purge", OPERATOR).status()).isEqualTo(400);
    }
}
