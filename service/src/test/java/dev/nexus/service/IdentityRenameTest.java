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
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1): rename. The old alias retires with {@code renamed_to}, a {@code rename} row is
 * appended and the seat's board entry is retitled in one transaction; the old name resolves for 7 days with a
 * notice and is then refused with where the seat went; a retired name is never reissued. The board entries here are
 * fixtures that carry the {@code identity:} line, which no client writes until RDR-253 Step 5.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityRenameTest {

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
    void renameRetitlesTheSeatsBoardEntryAndRetiresTheOldAlias() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "marginalia");
        String other = env.createSeat(t, "halcyon");
        env.bind(t, id, "start", 0, ev("s1", 41, 410));
        env.putThreadsEntry(t, "marginalia", id);
        env.putThreadsEntry(t, "halcyon", other);
        env.putThreadsEntry(t, "unrelated notes", null);

        Resp r = env.post(t, "/rename", Map.of("old", "marginalia", "new", "margin"));
        assertThat(r.status()).isEqualTo(200);
        assertThat(r.str("name")).isEqualTo("margin");
        assertThat(r.str("renamed_from")).isEqualTo("marginalia");
        assertThat(r.integer("board_entries_retitled")).isEqualTo(1);
        assertThat(env.threadsTitles(t)).containsExactly("halcyon", "margin", "unrelated notes");
        assertThat(env.events(t, id)).containsExactly("start", "rename");
        assertThat(env.log(t, id).get(1)).containsEntry("detail", "marginalia -> margin").containsEntry("generation", 1);
        assertThat(env.show(t, id)).containsEntry("name", "margin").containsEntry("state", "live");
        assertThat(env.get(t, "/alias/margin").str("identity")).isEqualTo(id);
        // A rename by the seat's id works too, and an identity with no board entry retitles none.
        Resp byId = env.post(t, "/rename", Map.of("old", other, "new", "halc"));
        assertThat(byId.status()).isEqualTo(200);
        assertThat(env.threadsTitles(t)).contains("halc").doesNotContain("halcyon");
    }

    @Test
    void theOldNameResolvesForSevenDaysWithANoticeThenIsRefusedWithWhereItWent() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "oldname");
        env.post(t, "/rename", Map.of("old", "oldname", "new", "newname"));

        env.clock.set(T0.plus(Duration.ofDays(7)).minusSeconds(1));
        Resp within = env.get(t, "/alias/oldname");
        assertThat(within.status()).isEqualTo(200);
        assertThat(within.str("identity")).isEqualTo(id);
        assertThat(within.str("state")).isEqualTo("retired");
        assertThat(within.str("renamed_to")).isEqualTo("newname");
        assertThat(within.str("notice")).contains("newname");

        env.clock.set(T0.plus(Duration.ofDays(7)));
        Resp after = env.get(t, "/alias/oldname");
        assertThat(after.status()).isEqualTo(410);
        assertThat(after.error()).isEqualTo("renamed");
        assertThat(after.str("renamed_to")).isEqualTo("newname");
        assertThat(after.str("message")).isEqualTo("renamed to newname on 2026-11-16");
        assertThat(env.get(t, "/alias/newname").status()).isEqualTo(200);
    }

    @Test
    void aRetiredNameIsNeverReissuedAndAChainOfRenamesNamesTheCurrentAlias() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "a1");
        env.post(t, "/rename", Map.of("old", "a1", "new", "b1"));
        env.post(t, "/rename", Map.of("old", "b1", "new", "c1"));
        Resp reuse = env.post(t, "", Map.of("kind", "seat", "name", "a1", "created_by", "sam"));
        assertThat(reuse.status()).isEqualTo(409);
        assertThat(reuse.error()).isEqualTo("name_taken");
        String second = env.createSeat(t, "other");
        assertThat(env.post(t, "/rename", Map.of("old", "other", "new", "b1")).error()).isEqualTo("name_taken");
        // Renaming back to a retired name of the same seat is also refused: the tombstone is permanent.
        assertThat(env.post(t, "/rename", Map.of("old", "c1", "new", "a1")).error()).isEqualTo("name_taken");
        Resp viaFirst = env.get(t, "/alias/a1");
        assertThat(viaFirst.status()).isEqualTo(200);
        assertThat(viaFirst.str("renamed_to")).as("the notice names the alias it has now").isEqualTo("c1");
        assertThat(env.show(t, id)).containsEntry("name", "c1");
        assertThat(env.show(t, second)).containsEntry("name", "other");
        // Nothing but a seat can be renamed, and the new name must be a usable one.
        assertThat(env.post(t, "/rename", Map.of("old", "nobody", "new", "x1")).status()).isEqualTo(404);
        assertThat(env.post(t, "/rename", Map.of("old", "c1", "new", "c1")).error()).isEqualTo("same_name");
    }

    @Test
    void namesThatLookLikeIdentityIdsAreNeverAnAlias() {
        String t = IdentityTestEnv.freshTenant();
        for (String bad : new String[] {"i7", "i12345", "I9"}) {
            Resp r = env.post(t, "", Map.of("kind", "seat", "name", bad, "created_by", "sam"));
            assertThat(r.status()).as(bad).isEqualTo(400);
            assertThat(r.error()).as(bad).isEqualTo("reserved_name");
        }
        String id = env.createSeat(t, "fine");
        assertThat(env.post(t, "/rename", Map.of("old", "fine", "new", "i77")).error()).isEqualTo("reserved_name");
        Resp eph = env.post(t, "", Map.of("kind", "ephemeral", "created_by", "session", "bind", ev("e1", 1, 10)));
        assertThat(env.post(t, "/" + eph.str("id") + "/promote", Map.of("name", "i5")).error()).isEqualTo("reserved_name");
        assertThat(env.post(t, "", Map.of("kind", "seat", "name", "-dash", "created_by", "sam")).error()).isEqualTo("bad_name");
        assertThat(env.post(t, "", Map.of("kind", "seat", "name", "has space", "created_by", "sam")).error()).isEqualTo("bad_name");
        assertThat(env.show(t, id)).containsEntry("name", "fine");
    }

    @Test
    void aBoardTitleAlreadyTakenRollsTheWholeRenameBack() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "wren");
        env.bind(t, id, "start", 0, ev("s1", 43, 430));
        env.putThreadsEntry(t, "wren", id);
        // Another entry already holds the title the rename would give: the retitle fails after the alias
        // rows were written, in the same transaction, so nothing of the rename remains.
        env.putThreadsEntry(t, "raven", null);
        Resp r = env.post(t, "/rename", Map.of("old", "wren", "new", "raven"));
        assertThat(r.status()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("board_title_taken");
        assertThat(env.show(t, id)).containsEntry("name", "wren");
        assertThat(env.events(t, id)).containsExactly("start");
        assertThat(env.get(t, "/alias/wren").str("state")).isEqualTo("live");
        assertThat(env.get(t, "/alias/raven").status()).isEqualTo(404);
        assertThat(env.threadsTitles(t)).containsExactly("raven", "wren");
    }

    @Test
    void theBoardLineMatchesOnlyItsOwnIdentityNotOneThatSharesAPrefix() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String[] ids = new String[13];
        for (int n = 1; n <= 12; n++) {
            ids[n] = env.createSeat(t, "seat" + n);
        }
        assertThat(ids[1]).isEqualTo("i1");
        assertThat(ids[10]).isEqualTo("i10");
        assertThat(ids[12]).isEqualTo("i12");
        env.putThreadsEntry(t, "entry-one", ids[1]);
        env.putThreadsEntry(t, "entry-ten", ids[10]);
        env.putThreadsEntry(t, "entry-twelve", ids[12]);

        Resp r = env.post(t, "/rename", Map.of("old", "seat1", "new", "uno"));
        assertThat(r.integer("board_entries_retitled")).isEqualTo(1);
        assertThat(env.threadsTitles(t)).as("only i1's entry was retitled").containsExactly("entry-ten", "entry-twelve", "uno");

        // The purge of i10 (ended) deletes i10's entry and neither i1's (now 'uno') nor i12's.
        env.bind(t, ids[10], "start", 0, ev("s10", 10, 100));
        env.bind(t, ids[10], "end", 1, Map.of());
        assertThat(env.post(t, "/" + ids[10] + "/purge", Map.of("operator", true)).map("removed")).containsEntry("board_entries", 1);
        assertThat(env.threadsTitles(t)).containsExactly("entry-twelve", "uno");
    }

    @Test
    void aQuarantinedBoardEntryIsNotRetitledButAPurgeStillRemovesIt() {
        String t = IdentityTestEnv.freshTenant();
        env.clock.set(T0);
        String id = env.createSeat(t, "hidden");
        env.putThreadsEntry(t, "hidden entry", id);
        env.quarantineThreadsEntry(t, "hidden entry");
        Resp r = env.post(t, "/rename", Map.of("old", "hidden", "new", "shown"));
        assertThat(r.status()).isEqualTo(200);
        assertThat(r.integer("board_entries_retitled")).as("a hidden entry is not the board entry").isZero();
        assertThat(env.threadsTitles(t)).containsExactly("hidden entry");
        // An explicit removal is a hard delete, as MemoryRepository#delete is: no hidden copy is left behind.
        env.bind(t, id, "start", 0, ev("s1", 1, 10));
        env.bind(t, id, "end", 1, Map.of());
        assertThat(env.post(t, "/" + id + "/purge", Map.of("operator", true)).map("removed")).containsEntry("board_entries", 1);
        assertThat(env.threadsTitles(t)).isEmpty();
    }
}
