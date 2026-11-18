// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.BoardTopicLock;
import dev.nexus.service.http.GroupTestEnv;
import dev.nexus.service.http.IdentityTestEnv;
import dev.nexus.service.http.IdentityTestEnv.Resp;
import org.jooq.JSONB;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;

import static dev.nexus.service.http.GroupTestEnv.body;
import static dev.nexus.service.jooq.nexus.Tables.GROUP_RECORDS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_MEMBERSHIPS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 1, the interleavings the race tests only hope to hit, forced. A transaction held open from outside
 * stands for the other writer: it holds the lock a group write must wait for, does what that writer would do, and
 * commits; the write under test must have waited (it is not done while the lock is held) and must then see the
 * other writer's commit.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GroupInterleavingTest {

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
    void aFormThatAddsATopicWaitsForTheTopicLockAndThenSeesTheOtherGroupThatServesIt() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        CompletableFuture<Resp>[] write = new CompletableFuture[1];

        env.ids.scope.withTenant(t, ctx -> {
            // The other writer: holds the topic lock, and has made its group serve the topic, uncommitted.
            BoardTopicLock.lock(ctx, t, "nexus-develop");
            ctx.insertInto(GROUP_RECORDS).set(GROUP_RECORDS.TENANT_ID, t).set(GROUP_RECORDS.NAME, "holder")
                .set(GROUP_RECORDS.LEADER, "i99").set(GROUP_RECORDS.FORMED_AT, OffsetDateTime.now())
                .set(GROUP_RECORDS.SERVES, JSONB.valueOf("[\"nexus-develop\"]")).execute();
            write[0] = CompletableFuture.supplyAsync(() -> env.form(t, a, body("push-train", a, List.of("nexus-develop"))));
            sleep(1500);
            assertThat(write[0]).as("the form waits for the topic lock the other writer holds").isNotDone();
            return null;
        });

        Resp r = write[0].get();
        assertThat(r.status()).as("after the other writer committed: %s", r).isEqualTo(409);
        assertThat(r.error()).isEqualTo("topic_served");
        assertThat(env.read(t, "push-train").status()).as("no second group serves the topic").isEqualTo(404);
        assertThat(env.groups.servingGroup(t, "nexus-develop")).hasValueSatisfying(g -> assertThat(g).containsEntry("name", "holder"));
    }

    @Test
    void aSetThatAddsATopicWaitsForTheTopicLockToo() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);
        CompletableFuture<Resp>[] write = new CompletableFuture[1];

        env.ids.scope.withTenant(t, ctx -> {
            BoardTopicLock.lock(ctx, t, "nexus-main");
            ctx.insertInto(GROUP_RECORDS).set(GROUP_RECORDS.TENANT_ID, t).set(GROUP_RECORDS.NAME, "holder")
                .set(GROUP_RECORDS.LEADER, "i99").set(GROUP_RECORDS.FORMED_AT, OffsetDateTime.now())
                .set(GROUP_RECORDS.SERVES, JSONB.valueOf("[\"nexus-main\"]")).execute();
            write[0] = CompletableFuture.supplyAsync(() -> env.as(t, a, "POST", "/push-train/set", Map.of("serves", List.of("nexus-main"))));
            sleep(1500);
            assertThat(write[0]).as("the set waits for the topic lock").isNotDone();
            return null;
        });

        Resp r = write[0].get();
        assertThat(r.status()).as(r.toString()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("topic_served");
        assertThat(env.read(t, "push-train").body().get("serves")).isEqualTo(List.of());
    }

    @Test
    void aJoinThatRacesADisbandWaitsForItAndIsRefusedInsteadOfLeavingAMembershipInADisbandedGroup() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);
        CompletableFuture<Resp>[] join = new CompletableFuture[1];

        env.ids.scope.withTenant(t, ctx -> {
            // The disband, in flight: the group row locked, ended_at set, the memberships removed, not committed.
            ctx.selectFrom(GROUP_RECORDS).where(GROUP_RECORDS.TENANT_ID.eq(t)).and(GROUP_RECORDS.NAME.eq("push-train")).forUpdate().fetch();
            ctx.update(GROUP_RECORDS).set(GROUP_RECORDS.ENDED_AT, OffsetDateTime.now())
                .where(GROUP_RECORDS.TENANT_ID.eq(t)).and(GROUP_RECORDS.NAME.eq("push-train")).execute();
            ctx.deleteFrom(IDENTITY_MEMBERSHIPS).where(IDENTITY_MEMBERSHIPS.TENANT_ID.eq(t))
                .and(IDENTITY_MEMBERSHIPS.GROUP_NAME.eq("push-train")).execute();
            join[0] = CompletableFuture.supplyAsync(() -> env.join(t, b, "push-train", "member"));
            sleep(1500);
            assertThat(join[0]).as("the join waits for the disband that holds the group row").isNotDone();
            return null;
        });

        Resp r = join[0].get();
        assertThat(r.status()).as(r.toString()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("group_disbanded");
        assertThat(env.members(t, "push-train")).as("no membership behind the cascade").isEmpty();
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(e);
        }
    }
}
