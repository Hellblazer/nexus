// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.GroupTestEnv;
import dev.nexus.service.http.IdentityTestEnv;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Map;

import static dev.nexus.service.http.GroupTestEnv.body;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 post-close audit S1c: {@code stall_factor}, a sibling of {@code ack_deadline} on the group record. Default
 * 3, whole numbers 1 to 10, set by the form and by {@code set}, and read by the
 * owner decision through {@code GroupView} (OwnerStamperAuditTest).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GroupStallFactorTest {

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
    void aGroupThatSetsNoStallFactorReadsThree() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        assertThat(env.form(t, a, body("push-train", a, List.of("nexus-develop"))).status()).isEqualTo(200);

        assertThat(env.read(t, "push-train").body()).containsEntry("stall_factor", 3);
        assertThat(env.groups.servingGroup(t, "nexus-develop").orElseThrow()).containsEntry("stall_factor", 3);
    }

    @Test
    void theFormAndSetTakeAWholeNumberFromOneToTen() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        Map<String, Object> form = body("push-train", a, List.of("nexus-develop"));
        form.put("stall_factor", 5);
        assertThat(env.form(t, a, form).status()).isEqualTo(200);
        assertThat(env.read(t, "push-train").body()).containsEntry("stall_factor", 5);

        assertThat(env.as(t, a, "POST", "/push-train/set", Map.of("stall_factor", 2)).status()).isEqualTo(200);
        assertThat(env.read(t, "push-train").body()).containsEntry("stall_factor", 2)
            .as("set leaves the deadline alone").containsEntry("ack_deadline", 20);

        for (Object bad : new Object[] {0, 11, -1, 2.5, "3", true}) {
            var r = env.as(t, a, "POST", "/push-train/set", Map.of("stall_factor", bad));
            assertThat(r.status()).as(String.valueOf(bad)).isEqualTo(400);
            assertThat(r.error()).contains("stall_factor");
        }
        assertThat(env.read(t, "push-train").body()).as("a refused write changed nothing").containsEntry("stall_factor", 2);
    }
}
