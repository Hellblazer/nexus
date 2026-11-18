// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.GroupTestEnv;
import dev.nexus.service.http.IdentityTestEnv;
import dev.nexus.service.http.IdentityTestEnv.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Map;

import static dev.nexus.service.http.GroupTestEnv.body;
import static org.assertj.core.api.Assertions.assertThat;

/** RDR-252 Step 1: a group name is the address-segment pattern, at most 64 characters; a {@code serves} entry is the same with no {@code --}. */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GroupNameTest {

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
    void aBadNameIsRefused() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        for (String bad : new String[] {"x".repeat(65), ".lead", "-lead", "_lead", "a/b", "a b", "", "a\\b", "push train", "é"}) {
            Resp r = env.form(t, a, body(bad, a));
            assertThat(r.status()).as("name '%s'", bad).isEqualTo(400);
            assertThat(r.error()).as("name '%s'", bad).isEqualTo("bad_group");
        }
        Resp noName = env.form(t, a, Map.of("leader", a));
        assertThat(noName.status()).isEqualTo(400);
        assertThat(noName.error()).isEqualTo("bad_group");
    }

    @Test
    void aGoodNameIsAccepted() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String longest = "g" + "x".repeat(63);
        assertThat(longest).hasSize(64);
        for (String ok : new String[] {longest, "push-train", "a.b_c-d", "7", "Review.Pair"}) {
            assertThat(env.form(t, a, body(ok, a)).status()).as("name '%s'", ok).isEqualTo(200);
            assertThat(env.read(t, ok).str("name")).isEqualTo(ok);
        }
    }

    @Test
    void aServesEntryWithTwoDashesOrABadShapeIsRefused() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        for (Object bad : new Object[] {"nexus--develop", "nexus-develop--done", ".x", "a/b", "x".repeat(65), "", 7}) {
            Resp r = env.form(t, a, body("push-train", a, List.of("nexus-main", bad)));
            assertThat(r.status()).as("serves entry '%s'", bad).isEqualTo(400);
            assertThat(r.error()).as("serves entry '%s'", bad).isEqualTo("bad_serves");
        }
        assertThat(env.read(t, "push-train").status()).isEqualTo(404);
        assertThat(env.form(t, a, IdentityTestEnv.with(body("push-train", a), "serves", "nexus-develop")).error())
            .as("a string, not a list").isEqualTo("bad_serves");

        assertThat(env.form(t, a, body("push-train", a, List.of("nexus-develop", "nexus.main_2"))).status()).isEqualTo(200);
        Resp set = env.as(t, a, "POST", "/push-train/set", Map.of("serves", List.of("nexus--develop")));
        assertThat(set.status()).isEqualTo(400);
        assertThat(set.error()).isEqualTo("bad_serves");
        assertThat(env.read(t, "push-train").body().get("serves")).isEqualTo(List.of("nexus-develop", "nexus.main_2"));
    }
}
