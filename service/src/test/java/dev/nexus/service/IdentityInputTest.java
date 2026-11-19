// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.IdentityTestEnv;
import dev.nexus.service.http.IdentityTestEnv.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.Map;

import static dev.nexus.service.http.IdentityTestEnv.DATA;
import static dev.nexus.service.http.IdentityTestEnv.TENANT;
import static dev.nexus.service.http.IdentityTestEnv.ev;
import static dev.nexus.service.http.IdentityTestEnv.with;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 1 (nexus-hczdo.1.1), review round 1: input the routes must answer with a 400 and never a 500 (a bind
 * with no event, a body that is not a JSON object, a delegated identity with no holder), the free text a data
 * credential may write is bounded, and the data credential may create an ephemeral identity (the MCP server's pending
 * bind) but no seat and no delegated identity without its own identity token (RDR-253 amendment, 2026-11-19).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class IdentityInputTest {

    IdentityTestEnv env;

    @BeforeAll
    void up() throws Exception {
        env = IdentityTestEnv.start();
    }

    @AfterAll
    void down() throws Exception {
        env.close();
    }

    private static String longText() {
        return "x".repeat(257);
    }

    @Test
    void aBindWithNoEventIsABadRequestForEveryCredential() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "noevent");
        for (String scope : new String[] {TENANT, DATA}) {
            Resp r = env.call(scope, t, "POST", "/" + id + "/bind", with(ev("s1", 1, 10), "expected_generation", 0));
            assertThat(r.status()).as(scope).isEqualTo(400);
            assertThat(r.error()).as(scope).isEqualTo("bad_event");
        }
        Resp nulled = env.callRaw(DATA, t, "POST", "/" + id + "/bind", "{\"event\":null,\"expected_generation\":0}");
        assertThat(nulled.status()).isEqualTo(400);
        assertThat(env.show(t, id)).containsEntry("generation", 0);
    }

    @Test
    void aBodyThatIsNotAJsonObjectIsABadRequestOnEveryRouteThatReadsOne() {
        String t = IdentityTestEnv.freshTenant();
        String id = env.createSeat(t, "garbled");
        String[][] routes = {
            {"POST", ""}, {"POST", "/delegated"}, {"POST", "/rename"}, {"POST", "/" + id + "/promote"},
            {"POST", "/" + id + "/bind"}, {"POST", "/" + id + "/gone"}, {"POST", "/" + id + "/renew"},
            {"POST", "/" + id + "/purge"}, {"POST", "/" + id + "/memberships"}, {"POST", "/" + id + "/work"}};
        int examined = 0;
        for (String[] route : routes) {
            for (String body : new String[] {"{not json", "[1,2,3]", "\"text\"", "null", "42", ""}) {
                Resp r = env.callRaw(TENANT, t, route[0], route[1], body);
                assertThat(r.status()).as("%s %s with body '%s'", route[0], route[1], body).isEqualTo(400);
                examined++;
            }
        }
        assertThat(examined).as("non-vacuity").isEqualTo(60);
        assertThat(env.show(t, id)).as("nothing changed").containsEntry("generation", 0).containsEntry("state", "unheld");
    }

    @Test
    void aDelegatedIdentityNeedsAHolderProcess() {
        String t = IdentityTestEnv.freshTenant();
        String parent = env.createSeat(t, "parent");
        for (Map<String, Object> bind : java.util.List.<Map<String, Object>>of(Map.of(), Map.of("lease", "leased"),
                Map.of("host", "h1"), Map.of("host", "h1", "pid", 3))) {
            Resp r = env.post(t, "/delegated", Map.of("parent", parent, "bind", bind));
            assertThat(r.status()).as("bind %s", bind).isEqualTo(400);
            assertThat(r.error()).isEqualTo("missing_evidence");
            assertThat(r.str("message")).isEqualTo("host, pid and started_at are required");
        }
        assertThat(env.post(t, "/delegated", Map.of("parent", parent)).status()).as("no bind object at all").isEqualTo(400);
        // No identity was made by the refusals: the next one is the seat's successor.
        Resp ok = env.post(t, "/delegated", Map.of("parent", parent, "bind", ev("c1", 5, 50)));
        assertThat(ok.status()).isEqualTo(200);
        assertThat(ok.str("id")).isEqualTo("i2");
    }

    @Test
    void freeTextIsBoundedAtTwoHundredFiftySixCharacters() {
        String t = IdentityTestEnv.freshTenant();
        Map<String, Object> bind = ev("s1", 1, 10);
        // display_name on every kind of creation
        assertThat(env.post(t, "", Map.of("kind", "seat", "name", "bounded", "display_name", longText(), "created_by", "sam")).status()).isEqualTo(400);
        assertThat(env.post(t, "", Map.of("kind", "ephemeral", "display_name", longText(), "created_by", "session", "bind", bind)).status()).isEqualTo(400);
        String parent = env.createSeat(t, "p");
        assertThat(env.post(t, "/delegated", Map.of("parent", parent, "display_name", longText(), "bind", bind)).status()).isEqualTo(400);
        // the evidence fields, on a data credential's pending bind and a tenant bind alike
        for (String field : new String[] {"host", "pane", "client_version", "session_id", "proc_start", "prior_session_id"}) {
            for (String scope : new String[] {TENANT, DATA}) {
                Resp r = env.call(scope, t, "POST", "/", with(Map.<String, Object>of("kind", "ephemeral", "created_by", "session"), "bind", with(bind, field, longText())));
                assertThat(r.status()).as("%s on %s", field, scope).isEqualTo(400);
            }
        }
        String id = env.createSeat(t, "okbound");
        assertThat(env.call(DATA, t, "POST", "/" + id + "/renew", Map.of("generation", 0, "client_version", longText())).status())
            .as("a renewal's client version").isEqualTo(400);
        assertThat(env.post(t, "/" + id + "/work", Map.of("kind", "wait", "ref", longText(), "state", "running")).status()).isEqualTo(400);
        // exactly 256 is fine
        String fits = "y".repeat(256);
        Resp ok = env.call(DATA, t, "POST", "", with(Map.<String, Object>of("kind", "ephemeral", "created_by", "session", "display_name", fits), "bind", with(bind, "host", fits, "pane", fits)));
        assertThat(ok.status()).isEqualTo(200);
        assertThat(env.rows(t, dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS, dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS.TENANT_ID))
            .as("the refusals made no identity: the seats p and okbound and the one that fit").isEqualTo(3);
    }

    @Test
    void anyScopeThatIsNeitherTenantNorDataIsRefusedOnCreateBeforeTheBodyIsRead() {
        String t = IdentityTestEnv.freshTenant();
        for (String scope : new String[] {"mint", "mint-locked", "board-ci", "nonsense"}) {
            for (String body : new String[] {"{not json", "[1]", "", "{\"kind\":\"seat\",\"name\":\"x\",\"created_by\":\"sam\"}"}) {
                assertThat(env.callRaw(scope, t, "POST", "", body).status()).as("%s with '%s'", scope, body).isEqualTo(403);
            }
        }
        assertThat(env.callRaw(TENANT, t, "POST", "", "{not json").status()).as("a tenant credential gets the 400").isEqualTo(400);
        assertThat(env.callRaw(DATA, t, "POST", "", "{not json").status()).isEqualTo(400);
    }

    @Test
    void theDataCredentialCreatesAnEphemeralButNoSeatOrDelegationWithoutItsOwnToken() {
        String t = IdentityTestEnv.freshTenant();
        Resp eph = env.call(DATA, t, "POST", "", Map.of("kind", "ephemeral", "created_by", "restart", "bind", ev("p1", 7, 70)));
        assertThat(eph.status()).isEqualTo(200);
        assertThat(eph.str("kind")).isEqualTo("ephemeral");
        assertThat(eph.integer("generation")).isEqualTo(1);
        assertThat(env.call(DATA, t, "POST", "", Map.of("kind", "ephemeral", "created_by", "sam", "bind", ev("p2", 8, 80))).error())
            .as("an ephemeral identity is made by a session or the restart script").isEqualTo("bad_created_by");
        // RDR-253 amendment (Sam 2026-11-19): the data credential creates a seat or delegates only with its own identity
        // token (IdentityDataSelfServiceTest); with none, the fence refuses before any write.
        Resp seat = env.call(DATA, t, "POST", "", Map.of("kind", "seat", "name", "dataseat", "created_by", "sam"));
        assertThat(seat.status()).isEqualTo(403);
        assertThat(seat.error()).isEqualTo("identity_token_required");
        assertThat(env.get(t, "/alias/dataseat").status()).isEqualTo(404);
        Resp delegated = env.call(DATA, t, "POST", "/delegated", Map.of("parent", eph.str("id"), "bind", ev("c1", 9, 90)));
        assertThat(delegated.status()).isEqualTo(403);
        assertThat(delegated.error()).as("no token, no delegation").isEqualTo("identity_token_required");
    }
}
