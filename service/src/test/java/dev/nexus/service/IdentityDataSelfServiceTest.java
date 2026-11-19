// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS;
import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_TOKENS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 amendment, Sam 2026-11-19 ("go with option 2, no bridge"; T2 nexus/sam-ruling-rdr253-cloud-seats-option2-2026-11-19):
 * a cloud install's laptop holds only the data credential, so the data credential may promote the ephemeral identity
 * its own identity token names, create a delegated identity whose parent its token names, and create a seat when it
 * carries a live token (the seat starts unheld, stamped {@code created_by identity:<token id>}). Each is fenced by the
 * token and touches no other identity. The tenant credential is unchanged on every route.
 */
class IdentityDataSelfServiceTest extends IdentityFenceHarness {

    private Resp data(String identityToken, String path, Object body) {
        return call(DATA_TOKEN, identityToken, null, "POST", path, body);
    }

    // ── promote ──────────────────────────────────────────────────────────────

    @Test
    void theDataCredentialPromotesTheEphemeralItsOwnTokenNames() {
        Seat e = bindEphemeral(201);
        String name = unique("self-promoted");
        Resp r = data(e.token(), "/v1/identity/" + e.id() + "/promote", Map.of("name", name));
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        Resp shown = get(null, "/v1/identity/" + e.id());
        assertThat(shown.text("kind")).isEqualTo("seat");
        assertThat(shown.text("name")).isEqualTo(name);
    }

    @Test
    void theDataCredentialMayNotPromoteAnotherIdentitysEphemeral() {
        Seat mine = bindEphemeral(202);
        Seat theirs = bindEphemeral(203);
        Resp r = data(mine.token(), "/v1/identity/" + theirs.id() + "/promote", Map.of("name", unique("stolen")));
        assertThat(r.status()).as(r.raw()).isEqualTo(403);
        assertThat(r.error()).isEqualTo("not_owner");
        assertThat(get(null, "/v1/identity/" + theirs.id()).text("kind")).as("untouched").isEqualTo("ephemeral");
    }

    @Test
    void aDataPromoteWithNoTokenIsRefused() {
        Seat e = bindEphemeral(204);
        Resp r = data(null, "/v1/identity/" + e.id() + "/promote", Map.of("name", unique("tokenless")));
        assertThat(r.status()).as(r.raw()).isEqualTo(403);
        assertThat(r.error()).isEqualTo("identity_token_required");
        assertThat(get(null, "/v1/identity/" + e.id()).text("kind")).isEqualTo("ephemeral");
    }

    @Test
    void aDataPromoteOfAHeldSeatIsNotEphemeral() {
        Seat s = bindSeat("already-seat", 205);
        Resp r = data(s.token(), "/v1/identity/" + s.id() + "/promote", Map.of("name", unique("again")));
        assertThat(r.status()).as(r.raw()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("not_ephemeral");
    }

    @Test
    void aDataPromoteWithASupersededTokenIsRefused() {
        Seat first = bindSeat("superseded", 206);
        resumeInNewProcess(first, 207, true);
        Resp r = data(first.token(), "/v1/identity/" + first.id() + "/promote", Map.of("name", unique("late")));
        assertThat(r.status()).as(r.raw()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("generation_superseded");
    }

    @Test
    void aDataPromoteAfterTheEphemeralEndedIsRefused() {
        Seat e = bindEphemeral(208);
        assertThat(seatEvent(e, "end").status()).isEqualTo(200);
        Resp r = data(e.token(), "/v1/identity/" + e.id() + "/promote", Map.of("name", unique("ended")));
        assertThat(r.status()).as(r.raw()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("identity_not_held");
    }

    // ── delegated ────────────────────────────────────────────────────────────

    private static Map<String, Object> childBind(int pid) {
        return evidence("child-" + pid, pid, null);
    }

    @Test
    void theDataCredentialDelegatesFromTheIdentityItsTokenNames() {
        Seat parent = bindSeat("parent", 211);
        Resp r = data(parent.token(), "/v1/identity/delegated", Map.of("parent", parent.id(), "bind", childBind(212)));
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        assertThat(get(null, "/v1/identity/" + r.text("id")).text("parent")).isEqualTo(parent.id());
        assertThat(r.text("identity_token")).as("the child gets its own token").isNotBlank();
    }

    @Test
    void theDataCredentialMayNotDelegateFromAnotherIdentity() {
        Seat mine = bindSeat("mine", 213);
        Seat other = bindSeat("other", 214);
        Resp r = data(mine.token(), "/v1/identity/delegated", Map.of("parent", other.id(), "bind", childBind(215)));
        assertThat(r.status()).as(r.raw()).isEqualTo(403);
        assertThat(r.error()).isEqualTo("not_owner");
    }

    @Test
    void aDataDelegationWithNoTokenIsRefused() {
        Seat parent = bindSeat("tokenless-parent", 216);
        Resp r = data(null, "/v1/identity/delegated", Map.of("parent", parent.id(), "bind", childBind(217)));
        assertThat(r.status()).as(r.raw()).isEqualTo(403);
        assertThat(r.error()).isEqualTo("identity_token_required");
    }

    // ── create a seat ────────────────────────────────────────────────────────

    @Test
    void theDataCredentialCreatesAnUnheldSeatStampedWithItsTokensIdentity() {
        Seat caller = bindEphemeral(221);
        String name = unique("made-by-data");
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("kind", "seat");
        body.put("name", name);
        body.put("created_by", "identity:i999999");      // the body may not name another creator
        Resp r = data(caller.token(), "/v1/identity", body);
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        Resp shown = get(null, "/v1/identity/" + r.text("id"));
        assertThat(shown.text("created_by")).isEqualTo("identity:" + caller.id());
        assertThat(shown.text("state")).as("it starts unheld: it touches no identity").isEqualTo("unheld");
    }

    @Test
    void aDataSeatCreateWithNoTokenIsRefused() {
        Resp r = data(null, "/v1/identity", Map.of("kind", "seat", "name", unique("tokenless-seat"), "created_by", "sam"));
        assertThat(r.status()).as(r.raw()).isEqualTo(403);
        assertThat(r.error()).isEqualTo("identity_token_required");
    }

    @Test
    void aDataSeatCreateWithATokenWhoseIdentityWasLetGoIsRefused() {
        Seat caller = bindEphemeral(222);
        assertThat(seatEvent(caller, "end").status()).isEqualTo(200);
        Resp r = data(caller.token(), "/v1/identity", Map.of("kind", "seat", "name", unique("after-end"), "created_by", "x"));
        assertThat(r.status()).as(r.raw()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("identity_not_held");
    }

    @Test
    void aDataSeatCreateOrDelegationWithASupersededTokenIsRefused() {
        Seat first = bindSeat("superseded-create", 223);
        resumeInNewProcess(first, 224, true);
        Resp seat = data(first.token(), "/v1/identity", Map.of("kind", "seat", "name", unique("late-seat"), "created_by", "x"));
        assertThat(seat.status()).as(seat.raw()).isEqualTo(409);
        assertThat(seat.error()).isEqualTo("generation_superseded");
        Resp child = data(first.token(), "/v1/identity/delegated", Map.of("parent", first.id(), "bind", childBind(225)));
        assertThat(child.status()).as(child.raw()).isEqualTo(409);
        assertThat(child.error()).isEqualTo("generation_superseded");
    }

    @Test
    void aDataDelegationNamingNoParentIsABadRequestNotAWrite() {
        Seat caller = bindSeat("no-parent", 226);
        Resp r = data(caller.token(), "/v1/identity/delegated", Map.of("bind", childBind(227)));
        assertThat(r.status()).as(r.raw()).isEqualTo(400);
    }

    @Test
    void aDataDelegationMayNotTakeASessionAnotherIdentityHolds() {
        Seat parent = bindSeat("session-parent", 228);
        Seat other = bindEphemeral(229);
        Map<String, Object> bind = new LinkedHashMap<>(childBind(230));
        bind.put("session_id", other.sessionId());          // the other identity's session
        Resp r = data(parent.token(), "/v1/identity/delegated", Map.of("parent", parent.id(), "bind", bind));
        assertThat(r.status()).as(r.raw()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("session_held");
        assertThat(get(null, "/v1/identity/sessions/" + other.sessionId()).text("identity")).as("unchanged").isEqualTo(other.id());
    }

    @Test
    void theTenantCredentialStillCannotStampAnIdentityCreator() {
        Resp r = post(null, "/v1/identity", Map.of("kind", "seat", "name", unique("forged"), "created_by", "identity:i77"));
        assertThat(r.status()).as("only a data credential's own token writes an identity: creator").isEqualTo(400);
        assertThat(r.error()).isEqualTo("bad_created_by");
    }

    // ── the cap (Sam 2026-11-19, engine 0.1.198: "cap 5") ──────────────────────

    private Resp dataSeat(String token, String stem) {
        return data(token, "/v1/identity", Map.of("kind", "seat", "name", unique(stem), "created_by", "x"));
    }

    @Test
    void anIdentityCreatesAtMostFiveSeatsThroughTheDataCredential() {
        Seat caller = bindEphemeral(251);
        for (int i = 0; i < 5; i++) {
            Resp r = dataSeat(caller.token(), "capped-" + i);
            assertThat(r.status()).as("seat " + (i + 1) + ": " + r.raw()).isEqualTo(200);
        }
        Resp sixth = dataSeat(caller.token(), "capped-6");
        assertThat(sixth.status()).as(sixth.raw()).isEqualTo(409);
        assertThat(sixth.error()).isEqualTo("seat_limit");
    }

    @Test
    void theCapIsPerIdentityAndTheTenantCredentialIsUncapped() {
        Seat full = bindEphemeral(252);
        for (int i = 0; i < 5; i++) {
            assertThat(dataSeat(full.token(), "full-" + i).status()).isEqualTo(200);
        }
        assertThat(dataSeat(full.token(), "full-over").error()).isEqualTo("seat_limit");
        Seat other = bindEphemeral(253);
        assertThat(dataSeat(other.token(), "other").status()).as("another identity has its own five").isEqualTo(200);
        for (int i = 0; i < 6; i++) {
            Resp t = post(null, "/v1/identity", Map.of("kind", "seat", "name", unique("tenant-uncapped-" + i), "created_by", "sam"));
            assertThat(t.status()).as("the tenant credential has no cap: " + t.raw()).isEqualTo(200);
        }
    }

    // ── refresh and fork need the identity's own live token (Sam 2026-11-19, option A; engine 0.1.198) ──

    private Resp dataBind(String token, Seat s, String event, Map<String, Object> extra) {
        Map<String, Object> body = new LinkedHashMap<>(s.evidence());
        body.put("event", event);
        body.put("expected_generation", s.generation());
        body.putAll(extra);
        return data(token, "/v1/identity/" + s.id() + "/bind", body);
    }

    @Test
    void aDataRefreshWithTheIdentitysOwnLiveTokenMintsANewOne() {
        Seat s = bindSeat("refresh-own", 261);
        Resp r = dataBind(s.token(), s, "refresh", Map.of());
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        assertThat(r.text("identity_token")).isNotBlank();
    }

    @Test
    void aDataRefreshWithNoTokenIsRefusedEvenNamingTheHolderProcess() {
        Seat s = bindSeat("refresh-tokenless", 262);
        Resp r = dataBind(null, s, "refresh", Map.of());          // s.evidence() names the holder's host, pid and start
        assertThat(r.status()).as(r.raw()).isEqualTo(403);
        assertThat(r.error()).isEqualTo("identity_token_required");
    }

    @Test
    void aDataRefreshWithAnotherIdentitysTokenIsRefused() {
        Seat victim = bindSeat("refresh-victim", 263);
        Seat other = bindSeat("refresh-other", 264);
        Resp r = dataBind(other.token(), victim, "refresh", Map.of());
        assertThat(r.status()).as(r.raw()).isEqualTo(403);
        assertThat(r.error()).isEqualTo("not_owner");
    }

    @Test
    void aDataRefreshWithASupersededTokenIsRefused() {
        Seat first = bindSeat("refresh-superseded", 265);
        Seat second = resumeInNewProcess(first, 266, true);
        Resp r = data(first.token(), "/v1/identity/" + first.id() + "/bind",
            new LinkedHashMap<>(Map.of("event", "refresh", "expected_generation", second.generation())));
        assertThat(r.status()).as(r.raw()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("generation_superseded");
    }

    @Test
    void aDataForkNeedsTheIdentitysOwnLiveTokenToo() {
        Seat s = bindSeat("fork-guard", 267);
        Map<String, Object> fork = Map.of("session_id", "sess-fork-" + s.pid(), "prior_session_id", s.sessionId());
        Resp tokenless = dataBind(null, s, "fork", fork);
        assertThat(tokenless.status()).as(tokenless.raw()).isEqualTo(403);
        assertThat(tokenless.error()).isEqualTo("identity_token_required");
        Resp own = dataBind(s.token(), s, "fork", fork);
        assertThat(own.status()).as(own.raw()).isEqualTo(200);
    }

    @Test
    void theTenantCredentialStillRefreshesWithoutAToken() {
        Seat s = bindSeat("tenant-refresh", 268);
        assertThat(seatEvent(s, "refresh").status()).as("the tenant credential is the trust boundary").isEqualTo(200);
    }

    @Test
    void aDataRefreshCannotReviveASeatWhoseHolderWasRecordedGone() {
        Seat s = bindSeat("refresh-lapsed-gone", 271);
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        scope.withTenant(TENANT, ctx -> ctx.update(IDENTITY_RECORDS).set(IDENTITY_RECORDS.STATE, "lapsed")
            .set(IDENTITY_RECORDS.LAPSED_AT, now).set(IDENTITY_RECORDS.GONE_AT, now)
            .where(IDENTITY_RECORDS.TENANT_ID.eq(TENANT)).and(IDENTITY_RECORDS.ID.eq(s.id())).execute());
        Resp r = dataBind(s.token(), s, "refresh", Map.of());
        assertThat(r.status()).as(r.raw()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("identity_not_held");
        assertThat(get(null, "/v1/identity/" + s.id()).text("state")).as("not revived by a stale token").isEqualTo("lapsed");
    }

    @Test
    void aDataRefreshOfAnEndedIdentityIsRefused() {
        Seat e = bindEphemeral(272);
        assertThat(seatEvent(e, "end").status()).isEqualTo(200);
        Resp r = dataBind(e.token(), e, "refresh", Map.of());
        assertThat(r.status()).as(r.raw()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("identity_not_held");
    }

    @Test
    void aDataRefreshWithAnExpiredTokenIsRefusedWithNoGrace() {
        Seat s = bindSeat("refresh-expired", 273);
        scope.withTenant(TENANT, ctx -> ctx.update(IDENTITY_TOKENS)
            .set(IDENTITY_TOKENS.EXPIRES_AT, OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(1))
            .where(IDENTITY_TOKENS.IDENTITY_ID.eq(s.id())).execute());
        Resp r = dataBind(s.token(), s, "refresh", Map.of());
        assertThat(r.status()).as("the bind route ignores an expired token, so the fence sees none: " + r.raw()).isEqualTo(403);
        assertThat(r.error()).isEqualTo("identity_token_required");
    }

    @Test
    void theCapCountsACreatedSeatThatWasLaterBound() {
        Seat caller = bindEphemeral(274);
        String firstId = null;
        for (int i = 0; i < 5; i++) {
            Resp r = dataSeat(caller.token(), "bound-later-" + i);
            assertThat(r.status()).isEqualTo(200);
            if (firstId == null) firstId = r.text("id");
        }
        Map<String, Object> start = new LinkedHashMap<>(evidence("sess-bound-" + firstId, 275, null));
        start.put("event", "start");
        start.put("expected_generation", 0);
        assertThat(post(null, "/v1/identity/" + firstId + "/bind", start).status()).isEqualTo(200);
        assertThat(dataSeat(caller.token(), "bound-later-6").error()).as("counted in every state").isEqualTo("seat_limit");
    }

    @Test
    void concurrentCreatesOfOneIdentityCannotPassTheCap() throws Exception {
        Seat caller = bindEphemeral(276);
        for (int i = 0; i < 4; i++) {
            assertThat(dataSeat(caller.token(), "race-pre-" + i).status()).isEqualTo(200);
        }
        int racers = 8;
        ExecutorService pool = Executors.newFixedThreadPool(racers);
        CountDownLatch go = new CountDownLatch(1);
        List<Future<Resp>> results = new ArrayList<>();
        try {
            for (int i = 0; i < racers; i++) {
                int n = i;
                results.add(pool.submit(() -> {
                    go.await();
                    return dataSeat(caller.token(), "race-" + n);
                }));
            }
            go.countDown();
            int ok = 0;
            int limited = 0;
            for (Future<Resp> f : results) {
                Resp r = f.get();
                if (r.status() == 200) ok++;
                else if ("seat_limit".equals(r.error())) limited++;
            }
            assertThat(ok).as("exactly one create takes the fifth place").isEqualTo(1);
            assertThat(limited).isEqualTo(racers - 1);
        } finally {
            pool.shutdownNow();
        }
    }

    // ── what stays as it was ─────────────────────────────────────────────────

    @Test
    void renameGoneAndPurgeStayTenantOnlyEvenWithAToken() {
        Seat s = bindSeat("tenant-only", 231);
        assertThat(data(s.token(), "/v1/identity/rename", Map.of("old", s.name(), "new", unique("renamed"))).error())
            .isEqualTo("forbidden");
        assertThat(data(s.token(), "/v1/identity/" + s.id() + "/purge", Map.of("operator", true)).error())
            .isEqualTo("forbidden");
        assertThat(data(s.token(), "/v1/identity/" + s.id() + "/gone", Map.of("expected_generation", s.generation(),
            "holder_gone", Map.of("host", "h1", "pid", 1, "started_at", 1))).error()).isEqualTo("forbidden");
    }

    @Test
    void theTenantCredentialStillPromotesCreatesAndDelegatesWithoutAToken() {
        Seat e = bindEphemeral(241);
        assertThat(post(null, "/v1/identity/" + e.id() + "/promote", Map.of("name", unique("tenant-promoted"))).status())
            .isEqualTo(200);
        Resp seat = post(null, "/v1/identity", Map.of("kind", "seat", "name", unique("tenant-seat"), "created_by", "sam"));
        assertThat(seat.status()).isEqualTo(200);
        assertThat(get(null, "/v1/identity/" + seat.text("id")).text("created_by")).isEqualTo("sam");
        Seat parent = bindSeat("tenant-parent", 242);
        assertThat(post(null, "/v1/identity/delegated", Map.of("parent", parent.id(), "bind", childBind(243))).status())
            .isEqualTo(200);
    }

    @Test
    void theVersionBodyAdvertisesSelfServiceSeats() {
        Resp v = call(TENANT_TOKEN, null, null, "GET", "/version", null);
        assertThat(v.status()).isEqualTo(200);
        assertThat(v.json().path("identity_self_seat_supported").asBoolean(false)).isTrue();
    }
}
