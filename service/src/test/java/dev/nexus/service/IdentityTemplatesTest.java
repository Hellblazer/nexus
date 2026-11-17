// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.IdentityTestEnv;
import dev.nexus.service.http.IdentityTestEnv.Resp;
import dev.nexus.service.tuples.TemplateRegistry;
import dev.nexus.service.tuples.TemplateSchema;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.http.IdentityTestEnv.ev;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1): the template surface and the id counter. The templates with a tuple surface
 * (the mailbox's {@code identity} address kind and {@code work/<id>}) load and resolve; the
 * records with no expiry (identity, alias, binding, session) are engine tables, so there is no template for them.
 * The counter only grows: an id is never issued twice, not even after a tombstone removed the identity that held it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityTemplatesTest {

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
    void theMailboxTakesIdentityAddressesAndWorkResolves() {
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        TemplateSchema mailbox = registry.resolve("mailbox/i12");
        assertThat(mailbox.name()).isEqualTo("mailbox/<address>");
        assertThat(mailbox.dimensions().get("address_kind").values()).containsExactly("agent", "session", "identity");
        assertThat(registry.resolve("work/i12").name()).isEqualTo("work/<id>");
        assertThat(registry.resolve("work/i12").take().enabled()).isFalse();
        // The permanent records, memberships included, are tables, not templates: nothing resolves for them.
        for (String name : new String[] {"identity/i12", "alias/marginalia", "binding/i12", "session/s-1", "member/i12"}) {
            assertThat(registry.resolve(name)).as(name).isNull();
        }
    }

    @Test
    void anIdentityIdIsIFollowedByTheCounterAndAnIdentityNamedIDigitsIsRefusedAsAnAlias() {
        String t = IdentityTestEnv.freshTenant();
        String first = env.createSeat(t, "one");
        String second = env.createSeat(t, "two");
        assertThat(first).matches("i[0-9]+");
        assertThat(number(second)).isEqualTo(number(first) + 1);
        assertThat(env.post(t, "", Map.of("kind", "seat", "name", second, "created_by", "sam")).error())
            .isEqualTo("reserved_name");
        // Two tenants count separately.
        String other = IdentityTestEnv.freshTenant();
        assertThat(env.createSeat(other, "one")).isEqualTo("i1");
    }

    @Test
    void theCounterNeverReusesANumberAfterATombstone() {
        String t = IdentityTestEnv.freshTenant();
        Instant t0 = Instant.parse("2026-11-16T12:00:00Z");
        env.clock.set(t0);
        List<String> issued = new ArrayList<>();
        for (int i = 0; i < 3; i++) {
            Resp r = env.post(t, "", Map.of("kind", "ephemeral", "created_by", "session", "bind", ev("e" + i, 100 + i, 1000 + i)));
            issued.add(r.str("id"));
            env.bind(t, r.str("id"), "end", 1, Map.of());
        }
        env.clock.set(t0.plus(Duration.ofDays(31)));
        assertThat(env.repo.runRetention(t)).containsEntry("tombstoned", 3);
        for (String id : issued) {
            assertThat(env.get(t, "/" + id).status()).as(id).isEqualTo(404);
        }
        Resp next = env.post(t, "", Map.of("kind", "ephemeral", "created_by", "session", "bind", ev("e9", 109, 1009)));
        assertThat(number(next.str("id"))).isGreaterThan(issued.stream().mapToInt(IdentityTemplatesTest::number).max().orElseThrow());
        assertThat(issued).doesNotContain(next.str("id"));
        assertThat(env.createSeat(t, "after")).isNotIn(issued);
    }

    private static int number(String id) {
        return Integer.parseInt(id.substring(1));
    }
}
