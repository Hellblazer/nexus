// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.GroupTestEnv;
import dev.nexus.service.http.IdentityTestEnv;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.util.List;

import static dev.nexus.service.http.GroupTestEnv.body;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 1: a group record and a membership are permanent rows. A tuple row is clamped to its creation time
 * plus the template's retention however often it is re-sent, so they are not tuples; with no renewals at all, both
 * still read 60 days (and 400 days) after they were written, across the retention pass the tenant's sweep runs.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GroupPermanenceTest {

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
    void aMembershipAndAGroupRecordStillReadSixtyDaysAfterTheyWereWrittenWithNoRenewals() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        var formed = env.form(t, a, body("push-train", a, List.of("nexus-develop")));
        assertThat(formed.status()).isEqualTo(200);
        assertThat(env.join(t, b, "push-train", "member").status()).isEqualTo(200);
        String formedAt = formed.str("formed_at");

        for (int days : new int[] {60, 400}) {
            env.ids.clock.advance(Duration.ofDays(days == 60 ? 60 : 340));
            assertThat(env.ids.repo.runRetention(t)).containsEntry("tombstoned", 0);
            var read = env.read(t, "push-train");
            assertThat(read.status()).as("day %d", days).isEqualTo(200);
            assertThat(read.str("leader")).isEqualTo(a);
            assertThat(read.str("formed_at")).isEqualTo(formedAt);
            assertThat(read.body().get("ended_at")).isNull();
            assertThat(env.members(t, "push-train")).as("day %d", days).containsExactly(a, b);
            assertThat(env.roster(t, "push-train").get(0)).containsEntry("role", "leader").containsEntry("joined_at", formedAt);
        }
        assertThat(env.ids.get(t, "/" + b + "/memberships").list("memberships")).hasSize(1);
        assertThat(env.read(t, "push-train").body().get("serves")).isEqualTo(List.of("nexus-develop"));
    }
}
