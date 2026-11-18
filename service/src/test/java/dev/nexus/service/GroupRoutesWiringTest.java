// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 1: {@code /v1/group} as production wires it, through the real {@code NexusService}: the real
 * {@code AuthFilter}, the real {@code TenantDataRoutes} registration and the real identity tokens a bind returns.
 * The group tests mount the handler behind a stand-in principal filter; this is the one that fails when the route
 * is not registered, or registered outside the auth filter.
 */
class GroupRoutesWiringTest extends IdentityFenceHarness {

    private static Map<String, Object> form(String name, String leader) {
        return Map.of("name", name, "leader", leader, "purpose", "wiring", "serves", List.of(unique("wiring-topic")));
    }

    @Test
    void theRouteIsReachableWithADataTokenAndAnIdentityTokenAndRefusedWithoutThem() {
        Seat lead = bindSeat("wiring-lead", 801);
        String name = unique("wired");

        try {
            var req = TestHttp.request("http://127.0.0.1:" + port + "/v1/group/" + name).GET().build();
            assertThat(http.send(req, java.net.http.HttpResponse.BodyHandlers.ofString()).statusCode())
                .as("no credential: the auth filter refuses before the handler").isIn(401, 403);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }

        // The data credential reaches the handler, which says there is no such group (not 404 from an unregistered route
        // with an HTML body: the error is the handler's).
        Resp unknown = call(DATA_TOKEN, null, null, "GET", "/v1/group/" + name, null);
        assertThat(unknown.status()).as(unknown.raw()).isEqualTo(404);
        assertThat(unknown.error()).isEqualTo("unknown_group");

        // A write with the data credential and no identity token is refused by the level-1 check, not by the route.
        Resp tokenless = call(DATA_TOKEN, null, null, "POST", "/v1/group", form(name, lead.id()));
        assertThat(tokenless.status()).as(tokenless.raw()).isEqualTo(403);
        assertThat(tokenless.error()).isEqualTo("identity_token_required");

        // With the identity token its bind returned, the same credential forms the group.
        Resp formed = call(DATA_TOKEN, lead.token(), null, "POST", "/v1/group", form(name, lead.id()));
        assertThat(formed.status()).as(formed.raw()).isEqualTo(200);
        assertThat(formed.text("leader")).isEqualTo(lead.id());

        Resp read = call(DATA_TOKEN, null, null, "GET", "/v1/group/" + name, null);
        assertThat(read.status()).isEqualTo(200);
        assertThat(read.text("name")).isEqualTo(name);
        // The roster is RDR-253's route, and holds the leader.
        Resp roster = call(DATA_TOKEN, null, null, "GET", "/v1/identity/groups/" + name, null);
        assertThat(roster.json().get("members").size()).isEqualTo(1);

        // A write by another seat's token is refused; the group is another tenant's business to nobody.
        Seat other = bindSeat("wiring-other", 802);
        Resp notLeader = call(DATA_TOKEN, other.token(), null, "POST", "/v1/group/" + name + "/set", Map.of("ack_deadline", 30));
        assertThat(notLeader.status()).isEqualTo(403);
        assertThat(notLeader.error()).isEqualTo("not_owner");
        Resp otherTenant = call(OTHER_TENANT_TOKEN, null, null, "GET", "/v1/group/" + name, null);
        assertThat(otherTenant.status()).as("another tenant's credential does not see the record").isEqualTo(404);
    }

    @Test
    void theOverrideIsTheTenantCredentialsAloneThroughTheRealFilter() {
        Seat lead = bindSeat("wiring-ovr-lead", 803);
        String name = unique("ovr");
        assertThat(call(DATA_TOKEN, lead.token(), null, "POST", "/v1/group", form(name, lead.id())).status()).isEqualTo(200);

        Resp data = call(DATA_TOKEN, null, null, "POST", "/v1/group/" + name + "/override", Map.of("ended_at", true));
        assertThat(data.status()).as("a data credential may not override").isEqualTo(403);
        // The tenant credential reaches the override, which refuses while the leader is live and leased.
        Resp tenant = call(TENANT_TOKEN, null, null, "POST", "/v1/group/" + name + "/override", Map.of("ended_at", true));
        assertThat(tenant.status()).as(tenant.raw()).isEqualTo(409);
        assertThat(tenant.error()).isEqualTo("leader_not_overridable");
    }
}
