// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.IdentityTokenStore;
import dev.nexus.service.db.TokenHashing;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_TOKENS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 2 (nexus-hczdo.1.2): the identity tokens. A token names an identity and one generation, lives one hour
 * (the store's clock decides), is minted by every bind that leaves the caller holding the seat, and is refreshed by
 * a bind with event {@code refresh} at the same generation. An expired token is a 401 (the client re-reads its token
 * file); a superseded generation's token stays resolvable until it expires, so the fence can answer 409. Every mint
 * is an insert in {@code identity_tokens}, so no mint invalidates an earlier token.
 */
class IdentityTokenTest extends IdentityFenceHarness {

    private static final Instant T0 = Instant.parse("2026-11-17T12:00:00Z");

    private static Clock at(Instant t) {
        return Clock.fixed(t, ZoneOffset.UTC);
    }

    private org.jooq.DSLContext db() {
        return DSL.using(ds, SQLDialect.POSTGRES);
    }

    private void expire(String rawToken) {
        db().update(IDENTITY_TOKENS).set(IDENTITY_TOKENS.EXPIRES_AT, OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(1))
            .where(IDENTITY_TOKENS.TOKEN_HASH.eq(TokenHashing.sha256Hex(rawToken))).execute();
    }

    private Resp readWith(String token) {
        return call(TENANT_TOKEN, token, null, "GET", "/v1/identity/sessions/none", null);
    }

    // ── lifetime, on a fixed clock ───────────────────────────────────────────

    @Test
    void aTokenLivesExactlyOneHourOnAFixedClock() {
        Seat seat = bindSeat("token-life", 600);
        var minted = new IdentityTokenStore(ds, at(T0)).issue(TENANT, seat.id(), 1, "p");
        assertThat(minted.expiresInSeconds()).isEqualTo(3600);
        assertThat(minted.expiresAt()).isEqualTo(T0.plusSeconds(3600));
        String hash = TokenHashing.sha256Hex(minted.rawToken());
        assertThat(new IdentityTokenStore(ds, at(T0.plusSeconds(1))).resolve(hash)).as("just minted")
            .contains(new IdentityTokenStore.Principal(TENANT, seat.id(), 1));
        assertThat(new IdentityTokenStore(ds, at(T0.plusSeconds(3599))).resolve(hash)).as("a second before").isPresent();
        assertThat(new IdentityTokenStore(ds, at(T0.plusSeconds(3600))).resolve(hash)).as("at the hour").isEmpty();
        assertThat(new IdentityTokenStore(ds, at(T0.plus(Duration.ofHours(2)))).resolve(hash)).isEmpty();
        assertThat(new IdentityTokenStore(ds, at(T0)).resolve(TokenHashing.sha256Hex("never-minted"))).isEmpty();
        assertThat(new IdentityTokenStore(ds, at(T0)).resolve(null)).isEmpty();
    }

    @Test
    void everyMintIsAnInsertSoAnEarlierTokenOfTheBindingKeepsWorkingUntilItExpires() {
        Seat seat = bindSeat("token-insert", 601);
        var first = new IdentityTokenStore(ds, at(T0)).issue(TENANT, seat.id(), 1, "p");
        var second = new IdentityTokenStore(ds, at(T0.plusSeconds(1800))).issue(TENANT, seat.id(), 1, "p");
        assertThat(second.rawToken()).isNotEqualTo(first.rawToken());
        var reader = new IdentityTokenStore(ds, at(T0.plusSeconds(1800)));
        assertThat(reader.resolve(TokenHashing.sha256Hex(first.rawToken()))).as("the refreshed-from token still resolves").isPresent();
        assertThat(reader.resolve(TokenHashing.sha256Hex(second.rawToken()))).isPresent();
        // A later mint clears this identity's tokens that have expired, so a long-lived seat does not pile them up.
        new IdentityTokenStore(ds, at(T0.plusSeconds(3700))).issue(TENANT, seat.id(), 1, "p");
        assertThat(db().fetchCount(IDENTITY_TOKENS, IDENTITY_TOKENS.TOKEN_HASH.eq(TokenHashing.sha256Hex(first.rawToken()))))
            .as("the first one expired at T0+1h and went at the next mint").isZero();
        assertThat(db().fetchCount(IDENTITY_TOKENS, IDENTITY_TOKENS.TOKEN_HASH.eq(TokenHashing.sha256Hex(second.rawToken()))))
            .as("the second is still live").isEqualTo(1);
    }

    // ── what the filter does with a token ────────────────────────────────────

    @Test
    void anExpiredTokenIsA401AndAnUnknownOneToo() {
        Seat seat = bindSeat("token-expired", 610);
        assertThat(readWith(seat.token()).status()).as("live").isNotEqualTo(401);
        expire(seat.token());
        Resp expired = readWith(seat.token());
        assertThat(expired.status()).as("an expired token is a 401, which the client answers by re-reading its token file").isEqualTo(401);
        assertThat(expired.error()).isEqualTo("unauthorized");
        assertThat(claim("inp", seat.id(), "c", seat.token()).status()).as("on a fenced write too").isEqualTo(401);
        assertThat(readWith("no-such-token").status()).isEqualTo(401);
    }

    @Test
    void anExpiredTokenDoesNotStandInTheWayOfTheBindThatRefreshesIt() {
        Seat seat = bindSeat("token-asleep", 612);
        expire(seat.token());
        // A client woken from a long sleep refreshes through bind with whatever header it holds: the route that
        // mints the replacement does not read the token, so the expired one is ignored there and nowhere else.
        Resp refreshed = call(TENANT_TOKEN, seat.token(), null, "POST", "/v1/identity/" + seat.id() + "/bind",
            Map.of("event", "refresh", "expected_generation", 1));
        assertThat(refreshed.status()).as(refreshed.raw()).isEqualTo(200);
        assertThat(refreshed.text("identity_token")).isNotBlank();
        Resp renewed = call(TENANT_TOKEN, seat.token(), null, "POST", "/v1/identity/" + seat.id() + "/renew",
            Map.of("generation", 1));
        assertThat(renewed.status()).as(renewed.raw()).isEqualTo(200);
        assertThat(claim("inp", seat.id(), "c", seat.token()).status()).as("any other route still refuses it").isEqualTo(401);
        assertThat(claim("inp", seat.id(), "c", refreshed.text("identity_token")).status()).isEqualTo(200);
    }

    @Test
    void aTokenOfAnotherTenantIsA401() {
        Seat seat = bindSeat("token-tenant", 611);
        Resp r = call(OTHER_TENANT_TOKEN, seat.token(), null, "GET", "/v1/identity/sessions/none", null);
        assertThat(r.status()).isEqualTo(401);
    }

    @Test
    void aSupersededGenerationsTokenStaysResolvableUntilItExpires() {
        Seat seat = bindSeat("token-superseded", 620);
        Seat second = resumeInNewProcess(seat, 621, true);
        assertThat(readWith(seat.token()).status()).as("a read with the superseded token is not a 401").isNotEqualTo(401);
        Resp fenced = claim("inp", seat.id(), "c", seat.token());
        assertThat(fenced.status()).as("the same session id, a new process: the old token is a 409, not a 401").isEqualTo(409);
        assertThat(fenced.error()).isEqualTo("generation_superseded");
        assertThat(second.token()).isNotEqualTo(seat.token());
        expire(seat.token());
        assertThat(claim("inp", seat.id(), "c", seat.token()).status()).as("after it expires it is a 401").isEqualTo(401);
        assertThat(claim("inp", seat.id(), "c", second.token()).status()).as("the new process is unaffected").isEqualTo(200);
    }

    // ── mint and refresh ─────────────────────────────────────────────────────

    private Resp bindEvent(Seat s, String event, int expected, Map<String, Object> extra) {
        Map<String, Object> body = new LinkedHashMap<>(extra);
        body.put("event", event);
        body.put("expected_generation", expected);
        return post(null, "/v1/identity/" + s.id() + "/bind", body);
    }

    @Test
    void aRefreshMintsANewTokenForTheSameGenerationAndTheOldOneKeepsWorking() {
        Seat seat = bindSeat("token-refresh", 630);
        Resp refreshed = bindEvent(seat, "refresh", 1, Map.of());
        assertThat(refreshed.status()).as(refreshed.raw()).isEqualTo(200);
        assertThat(refreshed.json().get("generation_changed").asBoolean()).as("a refresh is not a new binding").isFalse();
        assertThat(refreshed.json().get("generation").asInt()).isEqualTo(1);
        String renewed = refreshed.text("identity_token");
        assertThat(renewed).isNotBlank().isNotEqualTo(seat.token());
        assertThat(refreshed.json().get("identity_token_expires_in_seconds").asLong()).isEqualTo(3600);
        assertThat(claim("inp", seat.id(), "c", renewed).status()).as("the refreshed token").isEqualTo(200);
        assertThat(claim("inp", seat.id(), "c", seat.token()).status()).as("and the one before it").isEqualTo(200);
        // Refresh writes no binding row unless it revives a lapsed identity.
        assertThat(get(null, "/v1/identity/" + seat.id() + "/bindings").json().get("bindings").size()).isEqualTo(1);
    }

    @Test
    void aRefreshRenewAndEndFromASupersededGenerationAreRefusedWithGenerationSuperseded() {
        Seat seat = bindSeat("token-late", 640);
        resumeInNewProcess(seat, 641, false);
        for (String event : new String[] {"refresh", "end"}) {
            Resp r = bindEvent(seat, event, 1, Map.of());
            assertThat(r.status()).as(event).isEqualTo(409);
            assertThat(r.error()).as(event).isEqualTo("generation_superseded");
            assertThat(r.text("identity_token")).as("a refused refresh mints nothing").isNull();
        }
        Resp renew = post(null, "/v1/identity/" + seat.id() + "/renew", Map.of("generation", 1));
        assertThat(renew.status()).isEqualTo(409);
        assertThat(renew.error()).isEqualTo("generation_superseded");
        assertThat(renew.json().get("current_generation").asInt()).isEqualTo(2);
        assertThat(db().fetchCount(IDENTITY_TOKENS, IDENTITY_TOKENS.TENANT_ID.eq(TENANT).and(IDENTITY_TOKENS.IDENTITY_ID.eq(seat.id()))
            .and(IDENTITY_TOKENS.GENERATION.eq(1)))).as("the refused calls added no token for generation 1").isEqualTo(1);
    }

    @Test
    void aClearAndAForkMintATokenForTheSameGenerationAndTheOldTokenKeepsWorking() {
        Seat seat = bindSeat("token-clear", 650);
        Seat cleared = clear(seat, "sess-clear-" + java.util.UUID.randomUUID());
        assertThat(cleared.generation()).isEqualTo(1);
        assertThat(cleared.token()).isNotEqualTo(seat.token());
        assertThat(claim("inp", seat.id(), "c", seat.token()).status()).as("the MCP server's token across /clear").isEqualTo(200);
        assertThat(claim("inp", seat.id(), "c", cleared.token()).status()).isEqualTo(200);
        Map<String, Object> fork = new LinkedHashMap<>(seat.evidence());
        fork.put("session_id", "sess-fork-" + java.util.UUID.randomUUID());
        Resp forked = bindEvent(seat, "fork", 1, fork);
        assertThat(forked.status()).as(forked.raw()).isEqualTo(200);
        assertThat(forked.text("identity_token")).isNotBlank();
        assertThat(forked.json().get("generation").asInt()).isEqualTo(1);
    }

    @Test
    void aBindThatEndsOrReleasesTheSeatReturnsNoToken() {
        Seat seat = bindSeat("token-end", 660);
        Resp ended = bindEvent(seat, "end", 1, Map.of());
        assertThat(ended.status()).as(ended.raw()).isEqualTo(200);
        assertThat(ended.text("identity_token")).isNull();
        Seat other = bindSeat("token-release", 661);
        Map<String, Object> self = new LinkedHashMap<>(other.evidence());
        Resp released = bindEvent(other, "release", 1, self);
        assertThat(released.status()).as(released.raw()).isEqualTo(200);
        assertThat(released.text("identity_token")).isNull();
    }

    @Test
    void anEphemeralAndADelegatedIdentityAreCreatedHoldingGenerationOneWithAToken() {
        Seat eph = bindEphemeral(670);
        assertThat(eph.generation()).isEqualTo(1);
        assertThat(claim("inp", eph.id(), "c", eph.token()).status()).isEqualTo(200);
        Resp child = post(null, "/v1/identity/delegated", Map.of("parent", eph.id(), "bind", evidence("kid", 671, null)));
        assertThat(child.json().get("generation").asInt()).isEqualTo(1);
        assertThat(claim("inp", child.text("id"), "c", child.text("identity_token")).status()).isEqualTo(200);
        assertThat(claim("inp", child.text("id"), "c", eph.token()).status()).as("the parent's token is not the child's").isEqualTo(403);
    }

    // ── sweep, cascade ───────────────────────────────────────────────────────

    @Test
    void theScheduledSweepRemovesExpiredTokensAndLeavesLiveOnes() {
        Seat seat = bindSeat("token-sweep", 680);
        Seat dead = bindSeat("token-sweep-dead", 681);
        expire(dead.token());
        service.runScheduledSweep(OffsetDateTime.now(ZoneOffset.UTC));
        assertThat(db().fetchCount(IDENTITY_TOKENS, IDENTITY_TOKENS.TOKEN_HASH.eq(TokenHashing.sha256Hex(dead.token()))))
            .as("non-vacuity: the sweep did remove the expired token").isZero();
        assertThat(db().fetchCount(IDENTITY_TOKENS, IDENTITY_TOKENS.TOKEN_HASH.eq(TokenHashing.sha256Hex(seat.token()))))
            .as("and kept the live one").isEqualTo(1);
    }

    @Test
    void anIdentitysTokensGoWithItsRecordAndTheTokenTableHoldsNoSecret() {
        Seat eph = bindEphemeral(690);
        assertThat(db().fetchCount(IDENTITY_TOKENS, IDENTITY_TOKENS.IDENTITY_ID.eq(eph.id()))).isEqualTo(1);
        scope.withTenant(TENANT, ctx -> ctx.deleteFrom(dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS)
            .where(dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS.TENANT_ID.eq(TENANT))
            .and(dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS.ID.eq(eph.id())).execute());
        assertThat(db().fetchCount(IDENTITY_TOKENS, IDENTITY_TOKENS.IDENTITY_ID.eq(eph.id()))).isZero();
        Seat other = bindEphemeral(691);
        assertThat(db().select(IDENTITY_TOKENS.TOKEN_HASH).from(IDENTITY_TOKENS).where(IDENTITY_TOKENS.IDENTITY_ID.eq(other.id()))
            .fetchOne(IDENTITY_TOKENS.TOKEN_HASH)).as("only the hash is stored").isNotEqualTo(other.token())
            .isEqualTo(TokenHashing.sha256Hex(other.token()));
    }
}
