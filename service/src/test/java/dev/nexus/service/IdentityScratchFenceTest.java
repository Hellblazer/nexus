// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS;
import static dev.nexus.service.jooq.t1.Tables.SCRATCH;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 2 (nexus-hczdo.1.2): the identity scratch scope ({@code session_id = identity:<id>}) and the session
 * scope check that accepts an identity token's sessions. The scope is writable ({@code put}, {@code flag},
 * {@code unflag}, {@code delete}) only by its owner at the current generation; reads are open; {@code session/close}
 * and {@code sweep} leave it alone, and so does the scheduled sweep; the retention removes it with its identity.
 */
class IdentityScratchFenceTest extends IdentityFenceHarness {

    private static String scopeOf(Seat s) {
        return "identity:" + s.id();
    }

    private Resp t1(String token, String op, Map<String, Object> body) {
        return post(token, "/v1/t1/" + op, body);
    }

    private static Map<String, Object> put(String scope, String id, String content) {
        return Map.of("id", id, "session_id", scope, "content", content);
    }

    private String putOk(String token, String scope, String content) {
        String id = UUID.randomUUID().toString();
        Resp r = t1(token, "put", put(scope, id, content));
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        return id;
    }

    private List<String> contents(String token, String scope) {
        Resp r = t1(token, "list", Map.of("session_id", scope));
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        List<String> out = new java.util.ArrayList<>();
        r.json().get("entries").forEach(e -> out.add(e.get("content").asText()));
        return out;
    }

    /** Each fenced operation against an entry {@code entryId} of {@code scope}. */
    private Map<String, java.util.function.BiFunction<String, String, Resp>> writes(String scope) {
        Map<String, java.util.function.BiFunction<String, String, Resp>> ops = new LinkedHashMap<>();
        ops.put("put", (token, entryId) -> t1(token, "put", put(scope, UUID.randomUUID().toString(), "another")));
        ops.put("flag", (token, entryId) -> t1(token, "flag", Map.of("id", entryId, "session_id", scope,
            "flush_project", "p", "flush_title", "t")));
        ops.put("unflag", (token, entryId) -> t1(token, "unflag", Map.of("id", entryId, "session_id", scope)));
        ops.put("delete", (token, entryId) -> call(TENANT_TOKEN, token, null, "DELETE", "/v1/t1/delete",
            Map.of("id", entryId, "session_id", scope)));
        return ops;
    }

    // ── the fence ────────────────────────────────────────────────────────────

    @Test
    void aNonOwnerIsRefusedOnEveryWriteAndTheOwnerPasses() {
        Seat owner = bindSeat("scratch-owner", 500);
        Seat peer = bindSeat("scratch-peer", 501);
        for (var op : writes(scopeOf(owner)).entrySet()) {
            String entryId = putOk(owner.token(), scopeOf(owner), "kept for " + op.getKey());
            Resp refused = op.getValue().apply(peer.token(), entryId);
            assertThat(refused.status()).as(op.getKey() + ": " + refused.raw()).isEqualTo(403);
            assertThat(refused.error()).as(op.getKey()).isEqualTo("not_owner");
            assertThat(contents(null, scopeOf(owner))).as(op.getKey() + " left the scope as it was").contains("kept for " + op.getKey());
            Resp ok = op.getValue().apply(owner.token(), entryId);
            assertThat(ok.status()).as(op.getKey() + " by the owner: " + ok.raw()).isEqualTo(200);
        }
    }

    @Test
    void aSupersededGenerationIsRefusedOnEveryWriteAndTheCurrentOnePasses() {
        Seat owner = bindSeat("scratch-sup", 510);
        Map<String, String> entries = new LinkedHashMap<>();
        for (String name : writes(scopeOf(owner)).keySet()) {
            entries.put(name, putOk(owner.token(), scopeOf(owner), "before " + name));
        }
        Seat second = resumeInNewProcess(owner, 511, false);
        for (var op : writes(scopeOf(owner)).entrySet()) {
            Resp refused = op.getValue().apply(owner.token(), entries.get(op.getKey()));
            assertThat(refused.status()).as(op.getKey() + ": " + refused.raw()).isEqualTo(409);
            assertThat(refused.error()).as(op.getKey()).isEqualTo("generation_superseded");
        }
        assertThat(contents(null, scopeOf(owner))).as("the superseded process changed nothing").hasSize(entries.size());
        for (var op : writes(scopeOf(owner)).entrySet()) {
            Resp ok = op.getValue().apply(second.token(), entries.get(op.getKey()));
            assertThat(ok.status()).as(op.getKey() + " at the current generation: " + ok.raw()).isEqualTo(200);
        }
    }

    @Test
    void aTokenlessWriteIsRefusedExceptToAnUnleasedSeatsScopeWhichIsLabelledUnattested() {
        Seat owner = bindSeat("scratch-tokenless", 520);
        String entryId = putOk(owner.token(), scopeOf(owner), "kept");
        for (var op : writes(scopeOf(owner)).entrySet()) {
            Resp refused = op.getValue().apply(null, entryId);
            assertThat(refused.status()).as(op.getKey()).isEqualTo(403);
            assertThat(refused.error()).as(op.getKey()).isEqualTo("identity_token_required");
        }
        Seat desktop = bindSeat("scratch-desktop", 521, "unleased");
        for (var op : writes(scopeOf(desktop)).entrySet()) {
            String id = putOk(null, scopeOf(desktop), "desk " + op.getKey());
            Resp ok = op.getValue().apply(null, id);
            assertThat(ok.status()).as(op.getKey() + ": " + ok.raw()).isEqualTo(200);
            assertThat(ok.unattested()).as(op.getKey()).isTrue();
        }
    }

    @Test
    void readsOfTheIdentityScopeAreNeverCheckedADelegatedChildsIncluded() {
        Seat parent = bindSeat("scratch-parent", 530);
        String entryId = putOk(parent.token(), scopeOf(parent), "the parent's working state");
        Resp kid = post(null, "/v1/identity/delegated", Map.of("parent", parent.id(), "display_name", "kid",
            "bind", evidence("kid-sess", 531, null)));
        String kidToken = kid.text("identity_token");
        for (String token : new String[] {kidToken, null, parent.token()}) {
            assertThat(contents(token, scopeOf(parent))).containsExactly("the parent's working state");
            assertThat(t1(token, "get", Map.of("id", entryId, "session_id", scopeOf(parent))).text("content"))
                .isEqualTo("the parent's working state");
            assertThat(t1(token, "search", Map.of("query", "working", "session_id", scopeOf(parent))).json()
                .get("results").size()).isEqualTo(1);
            assertThat(t1(token, "flagged", Map.of("session_id", scopeOf(parent))).status()).isEqualTo(200);
            assertThat(t1(token, "resolve_prefix", Map.of("prefix", entryId.substring(0, 8), "session_id", scopeOf(parent)))
                .json().get("ids").size()).isEqualTo(1);
        }
        assertThat(t1(kidToken, "put", put(scopeOf(parent), UUID.randomUUID().toString(), "x")).status())
            .as("and does not write it").isEqualTo(403);
        // A minted session token on the request does not stand in the way of a read of the identity scope either.
        String minted = mint("minted-" + UUID.randomUUID());
        Resp read = call(TENANT_TOKEN, null, minted, "POST", "/v1/t1/list", Map.of("session_id", scopeOf(parent)));
        assertThat(read.status()).as(read.raw()).isEqualTo(200);
    }

    @Test
    void aReadWithAnIdentityTokenIsNotCheckedAgainstTheSessionIndex() {
        Seat seat = bindSeat("scratch-read", 590);
        String unindexed = "sess-unindexed-" + UUID.randomUUID();
        String entryId = UUID.randomUUID().toString();
        assertThat(t1(seat.token(), "put", put(unindexed, entryId, "x")).status()).as("the same session is refused as a write")
            .isEqualTo(403);
        assertThat(t1(seat.token(), "list", Map.of("session_id", unindexed)).status()).isEqualTo(200);
        assertThat(t1(seat.token(), "get", Map.of("id", entryId, "session_id", unindexed)).status()).isEqualTo(200);
        assertThat(t1(seat.token(), "search", Map.of("query", "x", "session_id", unindexed)).status()).isEqualTo(200);
        assertThat(t1(seat.token(), "flagged", Map.of("session_id", unindexed)).status()).isEqualTo(200);
        assertThat(t1(seat.token(), "resolve_prefix", Map.of("prefix", "abcd", "session_id", unindexed)).status()).isEqualTo(200);
    }

    @Test
    void anIdentityScopeNamesAnIdentity() {
        Resp r = t1(null, "list", Map.of("session_id", "identity:not-an-id"));
        assertThat(r.status()).isEqualTo(400);
        assertThat(r.error()).isEqualTo("bad_scope");
    }

    // ── never closed, never swept ────────────────────────────────────────────

    @Test
    void sessionCloseAndSweepLeaveTheIdentityScopeAndStillSweepASession() {
        Seat owner = bindSeat("scratch-close", 540);
        putOk(owner.token(), scopeOf(owner), "survives");
        String session = "plain-" + UUID.randomUUID();
        putOk(null, session, "a session row, swept by idleness");

        Resp closeIdentity = t1(owner.token(), "session/close", Map.of("session_id", scopeOf(owner)));
        assertThat(closeIdentity.status()).isEqualTo(200);
        assertThat(closeIdentity.json().get("deleted").asInt()).as("session/close never closes the identity scope").isZero();
        assertThat(contents(null, scopeOf(owner))).containsExactly("survives");

        Resp sweep = t1(null, "sweep", Map.of("hours", 0));
        assertThat(sweep.status()).isEqualTo(200);
        assertThat(sweep.json().get("swept").asInt()).as("non-vacuity: the sweep did sweep the idle session").isGreaterThanOrEqualTo(1);
        assertThat(contents(null, session)).isEmpty();
        assertThat(contents(null, scopeOf(owner))).as("and left the identity scope").containsExactly("survives");

        // The scheduled sweep, past the idleness bound, the same.
        putOk(null, session, "again");
        service.runScheduledSweep(OffsetDateTime.now(ZoneOffset.UTC).plusDays(3));
        assertThat(contents(null, session)).as("non-vacuity: the scheduled sweep swept the session").isEmpty();
        assertThat(contents(null, scopeOf(owner))).containsExactly("survives");

        Resp closeSession = t1(null, "session/close", Map.of("session_id", session));
        assertThat(closeSession.status()).isEqualTo(200);
    }

    @Test
    void thePurgeOfAnIdentityRemovesItsScopeAndTheRetentionRemovesAnOrphanedOne() {
        Seat owner = bindSeat("scratch-purge", 550);
        putOk(owner.token(), scopeOf(owner), "goes with the purge");
        Resp ended = post(null, "/v1/identity/" + owner.id() + "/bind", Map.of("event", "end", "expected_generation", owner.generation()));
        assertThat(ended.status()).as(ended.raw()).isEqualTo(200);
        Resp purge = post(null, "/v1/identity/" + owner.id() + "/purge", Map.of("operator", true));
        assertThat(purge.status()).as(purge.raw()).isEqualTo(200);
        assertThat(purge.json().get("removed").get("scratch").asInt()).isEqualTo(1);
        assertThat(contents(null, scopeOf(owner))).isEmpty();

        // An identity that is gone with its scope still in the table (a crash after a tombstone's commit): retention takes it.
        Seat gone = bindEphemeral(551);
        putOk(gone.token(), scopeOf(gone), "orphaned");
        scope.withTenant(TENANT, ctx -> ctx.deleteFrom(IDENTITY_RECORDS).where(IDENTITY_RECORDS.TENANT_ID.eq(TENANT))
            .and(IDENTITY_RECORDS.ID.eq(gone.id())).execute());
        Seat kept = bindSeat("scratch-kept", 552);
        putOk(kept.token(), scopeOf(kept), "belongs to a live identity");
        assertThat(contents(null, scopeOf(gone))).as("the orphan is there before the pass").hasSize(1);
        new dev.nexus.service.db.IdentityRepository(scope, java.time.Clock.systemUTC()).runRetention(TENANT, null);
        assertThat(contents(null, scopeOf(gone))).as("and gone after it").isEmpty();
        assertThat(contents(null, scopeOf(kept))).as("a live identity's scope stays").hasSize(1);
        int left = scope.withTenant(TENANT, dev.nexus.service.db.TenantScope.T1_TENANT_GUC,
            ctx -> ctx.fetchCount(SCRATCH, SCRATCH.SESSION_ID.eq(scopeOf(gone))));
        assertThat(left).as("by a count of the table, not by the route").isZero();
    }

    @Test
    void oneRetentionPassReachesOrphanedScopesPastTheFirstPageOfScopes() {
        int orphans = 1001; // one more than the 1000 scopes a statement reads
        scope.withTenant(TENANT, dev.nexus.service.db.TenantScope.T1_TENANT_GUC, ctx -> {
            var insert = ctx.insertInto(SCRATCH, SCRATCH.ID, SCRATCH.TENANT_ID, SCRATCH.SESSION_ID, SCRATCH.CONTENT,
                SCRATCH.TAGS, SCRATCH.FLAGGED, SCRATCH.ACCESS_COUNT, SCRATCH.TS);
            for (int i = 0; i < orphans; i++) {
                insert = insert.values(UUID.randomUUID().toString(), TENANT, "identity:i8" + String.format("%06d", i),
                    "orphan " + i, "", false, 0, OffsetDateTime.now(ZoneOffset.UTC));
            }
            return insert.execute();
        });
        java.util.function.Supplier<Integer> left = () -> scope.withTenant(TENANT,
            dev.nexus.service.db.TenantScope.T1_TENANT_GUC,
            ctx -> ctx.fetchCount(SCRATCH, SCRATCH.SESSION_ID.like("identity:i8%")));
        assertThat(left.get()).isEqualTo(orphans);
        new dev.nexus.service.db.IdentityRepository(scope, java.time.Clock.systemUTC()).runRetention(TENANT, null);
        assertThat(left.get()).as("every orphan, the one past the first page included").isZero();
    }

    // ── the session scope check ──────────────────────────────────────────────

    private String mint(String sessionId) {
        Resp r = post(null, "/v1/sessions/start", Map.of("session_id", sessionId));
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        return r.text("session_token");
    }

    @Test
    void anIdentityTokenAcceptsEverySessionTheIndexMapsToItsIdentityAndGeneration() {
        Seat seat = bindSeat("scratch-session", 560);
        assertThat(t1(seat.token(), "put", put(seat.sessionId(), UUID.randomUUID().toString(), "before clear")).status())
            .as("the session the seat is bound with").isEqualTo(200);
        Seat cleared = clear(seat, "sess-after-clear-" + UUID.randomUUID());
        assertThat(cleared.generation()).isEqualTo(seat.generation());
        // The first write after /clear, with the token the MCP server already holds, is not refused.
        Resp afterClear = t1(seat.token(), "put", put(cleared.sessionId(), UUID.randomUUID().toString(), "after clear"));
        assertThat(afterClear.status()).as("the post-/clear session id is mapped to the same identity and generation: "
            + afterClear.raw()).isEqualTo(200);
        assertThat(t1(seat.token(), "put", put(seat.sessionId(), UUID.randomUUID().toString(), "a Bash child from before")).status())
            .as("a Bash child from before the /clear still writes").isEqualTo(200);
        assertThat(contents(null, cleared.sessionId())).containsExactly("after clear");

        Resp other = t1(seat.token(), "put", put("sess-nobody-" + UUID.randomUUID(), UUID.randomUUID().toString(), "x"));
        assertThat(other.status()).as("a session the index does not map to this identity").isEqualTo(403);
        String foreign = bindSeat("scratch-foreign", 561).sessionId();
        assertThat(t1(seat.token(), "put", put(foreign, UUID.randomUUID().toString(), "x")).status())
            .as("a session another identity holds").isEqualTo(403);
    }

    @Test
    void aSupersededGenerationsTokenDoesNotAcceptTheSessionTheNewProcessIndexed() {
        Seat seat = bindSeat("scratch-session-sup", 570);
        Seat second = resumeInNewProcess(seat, 571, true);
        assertThat(t1(second.token(), "put", put(seat.sessionId(), UUID.randomUUID().toString(), "current")).status()).isEqualTo(200);
        Resp old = t1(seat.token(), "put", put(seat.sessionId(), UUID.randomUUID().toString(), "late"));
        assertThat(old.status()).as("the index maps the session to generation 2 now").isEqualTo(403);
    }

    @Test
    void aMintedSessionTokenAndAnIdentityTokenTogetherAcceptEitherSession() {
        Seat seat = bindSeat("scratch-both", 580);
        String minted = mint(seat.sessionId());
        Seat cleared = clear(seat, "sess-both-after-" + UUID.randomUUID());
        Resp mintedSession = call(TENANT_TOKEN, seat.token(), minted, "POST", "/v1/t1/put",
            put(seat.sessionId(), UUID.randomUUID().toString(), "the minted session"));
        assertThat(mintedSession.status()).as(mintedSession.raw()).isEqualTo(200);
        Resp mappedSession = call(TENANT_TOKEN, seat.token(), minted, "POST", "/v1/t1/put",
            put(cleared.sessionId(), UUID.randomUUID().toString(), "the post-clear session"));
        assertThat(mappedSession.status()).as("the old minted token with the post-/clear id the identity maps").isEqualTo(200);
        Resp stranger = call(TENANT_TOKEN, seat.token(), minted, "POST", "/v1/t1/put",
            put("sess-stranger-" + UUID.randomUUID(), UUID.randomUUID().toString(), "x"));
        assertThat(stranger.status()).isEqualTo(403);
        // With a minted token and no identity token the rule is the one it always was.
        Resp noIdentity = call(TENANT_TOKEN, null, minted, "POST", "/v1/t1/put",
            put(cleared.sessionId(), UUID.randomUUID().toString(), "x"));
        assertThat(noIdentity.status()).as("a minted session token alone still pins its own session").isEqualTo(403);
        Resp bootstrap = t1(null, "put", put("sess-bootstrap-" + UUID.randomUUID(), UUID.randomUUID().toString(), "x"));
        assertThat(bootstrap.status()).as("and no token at all is today's rule").isEqualTo(200);
    }
}
