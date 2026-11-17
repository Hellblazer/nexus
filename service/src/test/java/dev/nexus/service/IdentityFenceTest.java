// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_RECORDS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 2 (nexus-hczdo.1.2): the mailbox and membership fences, through the real service. The mailbox
 * fence covers the owner's {@code in}, {@code inp}, {@code ack}, {@code nack}, {@code renew} and {@code release};
 * a send is open. The membership fence covers a join, a role change and a leave on the
 * {@code /v1/identity/<id>/memberships} routes. Ownership is checked before generation, a token of a superseded
 * generation is a 409 {@code generation_superseded} (never a 401, because the token stays resolvable), a write with
 * no token is refused, and the one exception is a write to an {@code unleased} seat's family, which is accepted and
 * labelled. The threads-entry and scratch fences are in {@code IdentityThreadsFenceTest} and
 * {@code IdentityScratchFenceTest}; the tokens themselves in {@code IdentityTokenTest}.
 */
class IdentityFenceTest extends IdentityFenceHarness {

    private static final List<String> CLAIM_OPS = List.of("ack", "nack", "renew", "release");

    /** A request body for ack, nack, renew or release of {@code claimId} by {@code claimant}. */
    private static Map<String, Object> claimBody(String claimId, String claimant) {
        return Map.of("claim_id", claimId, "claimant", claimant, "lease_s", 60);
    }

    /** Sends a message to the seat's mailbox and claims it as the seat's owner at its current generation. */
    private String liveClaimOn(Seat owner, String claimant) {
        assertThat(mailTo(owner.id(), "m-" + UUID.randomUUID(), null).status()).isEqualTo(200);
        Resp in = claim("inp", owner.id(), claimant, owner.token());
        assertThat(in.status()).as(in.raw()).isEqualTo(200);
        assertThat(in.text("claim_id")).as("the owner's claim of its own mail").isNotBlank();
        return in.text("claim_id");
    }

    @Test
    void aParkedInWhoseSeatChangedHandsWhileItWaitedIsRefusedAndTheMailStaysForTheNewHolder() throws Exception {
        Seat old = bindSeat("parked", 145);
        var parked = java.util.concurrent.CompletableFuture.supplyAsync(() -> post(old.token(), "/v1/tuples/in",
            Map.of("subspace", "mailbox/" + old.id(), "keys_pattern", Map.of("to", old.id()), "claimant", "old-process",
                "lease_s", 60, "timeout_s", 20)));
        Thread.sleep(1500); // the in is parked: no mail yet, and the fence passed when it arrived
        assertThat(parked.isDone()).as("still waiting for mail: " + (parked.isDone() ? parked.join().raw() : "")).isFalse();
        Seat second = resumeInNewProcess(old, 146, false);
        assertThat(mailTo(old.id(), "m-parked", null).status()).isEqualTo(200);

        Resp refused = parked.get(25, java.util.concurrent.TimeUnit.SECONDS);
        assertThat(refused.status()).as("the old process's claim is refused, not delivered: " + refused.raw()).isEqualTo(409);
        assertThat(refused.error()).isEqualTo("generation_superseded");

        Resp mine = claim("inp", old.id(), "new-process", second.token());
        assertThat(mine.status()).as(mine.raw()).isEqualTo(200);
        assertThat(mine.text("claim_id")).as("the mail the old process was refused is claimable by the new holder").isNotBlank();
    }

    // ── a token outlives its seat ────────────────────────────────────────────

    /** One write of each family at the seat's current generation, with {@code token}: the status and error of each. */
    private Map<String, Resp> writesOf(Seat s, String token) {
        Map<String, Resp> out = new java.util.LinkedHashMap<>();
        out.put("mailbox", claim("inp", s.id(), "c-" + UUID.randomUUID(), token));
        out.put("membership", join(token, s, "g-" + UUID.randomUUID().toString().substring(0, 8), "member"));
        out.put("threads", post(token, "/v1/memory/put", Map.of("project", "threads", "title", unique("held"),
            "content", "session: x\nidentity: " + s.id() + "\nwork\n")));
        out.put("scratch", post(token, "/v1/t1/put", Map.of("id", UUID.randomUUID().toString(),
            "session_id", "identity:" + s.id(), "content", "state")));
        return out;
    }

    private void assertAllRefusedAsNotHeld(Map<String, Resp> writes, String why) {
        for (var w : writes.entrySet()) {
            assertThat(w.getValue().status()).as(why + " " + w.getKey() + ": " + w.getValue().raw()).isEqualTo(409);
            assertThat(w.getValue().error()).as(why + " " + w.getKey()).isEqualTo("identity_not_held");
        }
    }

    @Test
    void aTokenOfASeatThatEndedOrWasReleasedOrPurgedFencesEveryFamily() {
        Seat live = bindSeat("held-live", 140);
        for (var w : writesOf(live, live.token()).entrySet()) {
            assertThat(w.getValue().status()).as("held " + w.getKey() + ": " + w.getValue().raw()).isEqualTo(200);
        }
        Seat ended = bindSeat("held-ended", 141);
        assertThat(seatEvent(ended, "end").status()).isEqualTo(200);
        assertAllRefusedAsNotHeld(writesOf(ended, ended.token()), "after end:");

        Seat released = bindSeat("held-released", 142);
        assertThat(seatEvent(released, "release").status()).isEqualTo(200);
        assertAllRefusedAsNotHeld(writesOf(released, released.token()), "after release:");

        // A purge leaves the record of a seat in the state it had: an ended one stays ended.
        Seat purged = bindSeat("held-purged", 143);
        assertThat(seatEvent(purged, "end").status()).isEqualTo(200);
        assertThat(post(null, "/v1/identity/" + purged.id() + "/purge", Map.of("operator", true)).status()).isEqualTo(200);
        assertAllRefusedAsNotHeld(writesOf(purged, purged.token()), "after purge:");
    }

    @Test
    void aLapsedSeatStillWritesUnlessItsHolderWasRecordedGone() {
        Seat seat = bindSeat("held-lapsed", 144);
        scope.withTenant(TENANT, ctx -> ctx.update(IDENTITY_RECORDS).set(IDENTITY_RECORDS.STATE, "lapsed")
            .set(IDENTITY_RECORDS.LAPSED_AT, OffsetDateTime.now(ZoneOffset.UTC))
            .where(IDENTITY_RECORDS.TENANT_ID.eq(TENANT)).and(IDENTITY_RECORDS.ID.eq(seat.id())).execute());
        for (var w : writesOf(seat, seat.token()).entrySet()) {
            assertThat(w.getValue().status()).as("lapsed " + w.getKey() + ": " + w.getValue().raw()).isEqualTo(200);
        }
        scope.withTenant(TENANT, ctx -> ctx.update(IDENTITY_RECORDS).set(IDENTITY_RECORDS.GONE_AT, OffsetDateTime.now(ZoneOffset.UTC))
            .where(IDENTITY_RECORDS.TENANT_ID.eq(TENANT)).and(IDENTITY_RECORDS.ID.eq(seat.id())).execute());
        assertAllRefusedAsNotHeld(writesOf(seat, seat.token()), "lapsed with the holder gone:");
    }

    // ── the named test ───────────────────────────────────────────────────────

    @Test
    void supersededGenerationClaimOnMailboxIsRefusedWith409() {
        Seat alpha = bindSeat("alpha", 100);
        assertThat(alpha.generation()).isEqualTo(1);
        // A new process takes the seat with the SAME session id (a restart): generation 2, a token of its own.
        Seat second = resumeInNewProcess(alpha, 101, true);
        assertThat(second.generation()).isEqualTo(2);
        assertThat(second.sessionId()).isEqualTo(alpha.sessionId());
        assertThat(mailTo(alpha.id(), "m-superseded", null).status()).isEqualTo(200);

        Resp old = claim("inp", alpha.id(), "old-process", alpha.token());
        assertThat(old.status()).as("not a 401: the superseded token still resolves, so the engine can say why").isEqualTo(409);
        assertThat(old.error()).isEqualTo("generation_superseded");
        assertThat(old.json().get("token_generation").asInt()).isEqualTo(1);
        assertThat(old.json().get("current_generation").asInt()).isEqualTo(2);
        assertThat(old.json().get("holder").get("pid").asInt()).as("the refusal names the holder").isEqualTo(101);

        Resp current = claim("inp", alpha.id(), "new-process", second.token());
        assertThat(current.status()).as(current.raw()).isEqualTo(200);
        assertThat(current.text("claim_id")).as("the mail the superseded process was refused is the new one's").isNotBlank();
    }

    @Test
    void generationSupersededIsADifferentCodeFromTheEnginesOther409s() {
        Seat s = bindSeat("distinct", 110);
        Seat second = resumeInNewProcess(s, 111, false);
        // A lost compare-and-set on start is generation_conflict; a refused claim by a superseded holder is not.
        Map<String, Object> lost = new java.util.LinkedHashMap<>(evidence("s-x", 112, null));
        lost.put("event", "start");
        lost.put("expected_generation", 0);
        Resp conflict = post(null, "/v1/identity/" + s.id() + "/bind", lost);
        assertThat(conflict.status()).isEqualTo(409);
        assertThat(conflict.error()).isEqualTo("generation_conflict");
        Resp fenced = claim("inp", s.id(), "c", s.token());
        assertThat(fenced.status()).isEqualTo(409);
        assertThat(fenced.error()).isEqualTo("generation_superseded").isNotEqualTo(conflict.error());
        assertThat(second.generation()).isEqualTo(2);
    }

    // ── mailbox ──────────────────────────────────────────────────────────────

    @Test
    void anInAndAnInpOnAnotherSeatsMailboxAreRefusedAndTheOwnersPass() {
        Seat owner = bindSeat("owner", 120);
        Seat peer = bindSeat("peer", 121);
        assertThat(mailTo(owner.id(), "m-1", null).status()).isEqualTo(200);
        for (String op : List.of("in", "inp")) {
            Resp refused = claim(op, owner.id(), "peer-claimant", peer.token());
            assertThat(refused.status()).as(op).isEqualTo(403);
            assertThat(refused.error()).as(op).isEqualTo("not_owner");
        }
        assertThat(claim("inp", owner.id(), "owner-claimant", owner.token()).status()).isEqualTo(200);
        assertThat(claim("in", owner.id(), "owner-claimant", owner.token()).status()).isEqualTo(200);
    }

    @Test
    void ackNackRenewAndReleaseOfAClaimOnAnotherSeatsMailboxAreRefusedForAPeer() {
        Seat owner = bindSeat("owner-ops", 130);
        Seat peer = bindSeat("peer-ops", 131);
        for (String op : CLAIM_OPS) {
            String claimId = liveClaimOn(owner, "owner-claimant");
            Resp refused = post(peer.token(), "/v1/tuples/" + op, claimBody(claimId, "owner-claimant"));
            assertThat(refused.status()).as(op).isEqualTo(403);
            assertThat(refused.error()).as(op + " is refused by the fence, before the claim's own ownership check").isEqualTo("not_owner");
            // The claim is untouched: the owner still holds it and may act on it.
            Resp ok = post(owner.token(), "/v1/tuples/" + op, claimBody(claimId, "owner-claimant"));
            assertThat(ok.status()).as(op + " by the owner: " + ok.raw()).isEqualTo(200);
        }
    }

    @Test
    void ackNackRenewAndReleaseByASupersededGenerationAreRefusedWith409() {
        for (String op : CLAIM_OPS) {
            Seat owner = bindSeat("owner-sup-" + op, 140);
            String claimId = liveClaimOn(owner, "old-process");
            Seat second = resumeInNewProcess(owner, 141, false);
            Resp refused = post(owner.token(), "/v1/tuples/" + op, claimBody(claimId, "old-process"));
            assertThat(refused.status()).as(op).isEqualTo(409);
            assertThat(refused.error()).as(op).isEqualTo("generation_superseded");
            // The current generation passes (its claimant is the process that made the claim, so the claim's own
            // check agrees: the fence is about who may act on the mailbox, not about whose claim it was).
            Resp ok = post(second.token(), "/v1/tuples/" + op, claimBody(claimId, "old-process"));
            assertThat(ok.status()).as(op + ": " + ok.raw()).isEqualTo(200);
        }
    }

    @Test
    void aSendToAMailboxPassesForAPeerWithATokenWithOneOfAnotherTenantsDataCredentialAndWithNone() {
        Seat owner = bindSeat("recv", 150);
        Seat peer = bindSeat("sender", 151);
        assertThat(mailTo(owner.id(), "m-peer", peer.token()).status()).as("a peer with a token").isEqualTo(200);
        assertThat(mailTo(owner.id(), "m-none", null).status()).as("no token (nx ask answers, the console)").isEqualTo(200);
        Seat superseded = bindSeat("old-sender", 152);
        resumeInNewProcess(superseded, 153, false);
        assertThat(mailTo(owner.id(), "m-old", superseded.token()).status()).as("a superseded sender").isEqualTo(200);
        Resp data = call(DATA_TOKEN, null, null, "POST", "/v1/tuples/out", Map.of("subspace", "mailbox/" + owner.id(),
            "keys", Map.of("to", owner.id()), "dims", Map.of("from", "d"), "body", "x", "nonce", "m-data"));
        assertThat(data.status()).as("a data credential, no token").isEqualTo(200);
    }

    @Test
    void aTokenlessClaimOnAnIdentityMailboxIsRefused() {
        Seat owner = bindSeat("tokenless", 160);
        for (String op : List.of("in", "inp")) {
            Resp refused = claim(op, owner.id(), "no-token", null);
            assertThat(refused.status()).as(op).isEqualTo(403);
            assertThat(refused.error()).as(op).isEqualTo("identity_token_required");
        }
        String claimId = liveClaimOn(owner, "c");
        for (String op : CLAIM_OPS) {
            Resp refused = post(null, "/v1/tuples/" + op, claimBody(claimId, "c"));
            assertThat(refused.status()).as(op).isEqualTo(403);
            assertThat(refused.error()).as(op).isEqualTo("identity_token_required");
        }
    }

    @Test
    void aTokenlessClaimOnAnUnleasedSeatsMailboxIsAcceptedAndLabelledUnattested() {
        Seat desktop = bindSeat("desktop", 170, "unleased");
        assertThat(mailTo(desktop.id(), "m-desk", null).status()).isEqualTo(200);
        Resp in = claim("inp", desktop.id(), "desktop-mcp", null);
        assertThat(in.status()).as(in.raw()).isEqualTo(200);
        assertThat(in.unattested()).as("accepted without a token, and labelled").isTrue();
        assertThat(in.text("claim_id")).isNotBlank();
        Resp ack = post(null, "/v1/tuples/ack", claimBody(in.text("claim_id"), "desktop-mcp"));
        assertThat(ack.status()).as(ack.raw()).isEqualTo(200);
        assertThat(ack.unattested()).isTrue();
        // A leased seat's tokenless claim carries no such label, because it is refused.
        Seat leased = bindSeat("leased", 171);
        Resp refused = claim("inp", leased.id(), "x", null);
        assertThat(refused.status()).isEqualTo(403);
        assertThat(refused.unattested()).isFalse();
        // A token that names another identity is not the exception: the exception is for a write with no token.
        Seat peer = bindSeat("peer-of-desktop", 172);
        Resp byPeer = claim("inp", desktop.id(), "peer", peer.token());
        assertThat(byPeer.status()).isEqualTo(403);
        assertThat(byPeer.error()).isEqualTo("not_owner");
    }

    @Test
    void aMailboxThatIsNotAnIdentitysIsNotFenced() {
        String sessionAddress = "sess-" + UUID.randomUUID();
        assertThat(mailTo(sessionAddress, "m-s", null).status()).isEqualTo(200);
        Resp in = claim("inp", sessionAddress, "whoever", null);
        assertThat(in.status()).as("a session-addressed mailbox keeps today's rule").isEqualTo(200);
        assertThat(in.text("claim_id")).isNotBlank();
        assertThat(in.unattested()).isFalse();
    }

    @Test
    void aReadOfAnIdentityMailboxIsNeverChecked() {
        Seat owner = bindSeat("reader", 180);
        assertThat(mailTo(owner.id(), "m-rd", null).status()).isEqualTo(200);
        Resp rd = post(null, "/v1/tuples/rd", Map.of("subspace", "mailbox/" + owner.id(), "keys_pattern", Map.of("to", owner.id())));
        assertThat(rd.status()).as("rd needs no token").isEqualTo(200);
        assertThat(rd.json().get("tuples").size()).isEqualTo(1);
        Seat second = resumeInNewProcess(owner, 181, false);
        Resp byOld = post(owner.token(), "/v1/tuples/rd", Map.of("subspace", "mailbox/" + owner.id(), "keys_pattern", Map.of("to", owner.id())));
        assertThat(byOld.status()).as("nor a superseded token's read").isEqualTo(200);
        assertThat(second.generation()).isEqualTo(2);
    }

    // ── membership ───────────────────────────────────────────────────────────

    private Resp join(String token, Seat who, String group, String role) {
        return post(token, "/v1/identity/" + who.id() + "/memberships", Map.of("group", group, "role", role));
    }

    @Test
    void aJoinARoleChangeAndALeaveAreCheckedForOwnershipAndGeneration() {
        Seat owner = bindSeat("member", 190);
        Seat peer = bindSeat("not-member", 191);
        String group = unique("crew");
        Resp joined = join(owner.token(), owner, group, "member");
        assertThat(joined.status()).as(joined.raw()).isEqualTo(200);
        assertThat(join(owner.token(), owner, group, "leader").status()).as("a role change by the owner").isEqualTo(200);

        Resp byPeer = join(peer.token(), owner, group, "leader");
        assertThat(byPeer.status()).isEqualTo(403);
        assertThat(byPeer.error()).isEqualTo("not_owner");
        Resp leaveByPeer = delete(peer.token(), "/v1/identity/" + owner.id() + "/memberships/" + group, null);
        assertThat(leaveByPeer.status()).isEqualTo(403);
        assertThat(leaveByPeer.error()).isEqualTo("not_owner");
        Resp tokenless = join(null, owner, group, "member");
        assertThat(tokenless.status()).isEqualTo(403);
        assertThat(tokenless.error()).isEqualTo("identity_token_required");
        assertThat(delete(null, "/v1/identity/" + owner.id() + "/memberships/" + group, null).error())
            .isEqualTo("identity_token_required");

        // The membership is still the owner's, in the role the owner gave it.
        Resp list = get(null, "/v1/identity/" + owner.id() + "/memberships");
        assertThat(list.json().get("memberships").size()).isEqualTo(1);
        assertThat(list.json().get("memberships").get(0).get("role").asText()).isEqualTo("leader");

        Seat second = resumeInNewProcess(owner, 192, false);
        Resp superseded = join(owner.token(), owner, group, "member");
        assertThat(superseded.status()).isEqualTo(409);
        assertThat(superseded.error()).isEqualTo("generation_superseded");
        Resp leaveSuperseded = delete(owner.token(), "/v1/identity/" + owner.id() + "/memberships/" + group, null);
        assertThat(leaveSuperseded.status()).isEqualTo(409);
        assertThat(leaveSuperseded.error()).isEqualTo("generation_superseded");
        assertThat(get(null, "/v1/identity/" + owner.id() + "/memberships").json().get("memberships").size())
            .as("the superseded process left nothing").isEqualTo(1);

        assertThat(delete(second.token(), "/v1/identity/" + owner.id() + "/memberships/" + group, null).status())
            .as("the current generation leaves").isEqualTo(200);
        assertThat(get(null, "/v1/identity/" + owner.id() + "/memberships").json().get("memberships").size()).isZero();
    }

    @Test
    void aTokenlessMembershipWriteToAnUnleasedSeatIsAcceptedAndLabelledUnattested() {
        Seat desktop = bindSeat("desktop-member", 195, "unleased");
        String group = unique("desk");
        Resp joined = join(null, desktop, group, "member");
        assertThat(joined.status()).as(joined.raw()).isEqualTo(200);
        assertThat(joined.unattested()).isTrue();
        Resp left = delete(null, "/v1/identity/" + desktop.id() + "/memberships/" + group, null);
        assertThat(left.status()).isEqualTo(200);
        assertThat(left.unattested()).isTrue();
    }

    @Test
    void aDelegatedChildReadsItsParentsMembershipsAndDoesNotWriteThem() {
        Seat parent = bindSeat("parent", 200);
        String group = unique("fam");
        assertThat(join(parent.token(), parent, group, "member").status()).isEqualTo(200);
        Map<String, Object> bind = evidence("child-sess", 201, null);
        Resp child = post(null, "/v1/identity/delegated", Map.of("parent", parent.id(), "display_name", "child", "bind", bind));
        assertThat(child.status()).as(child.raw()).isEqualTo(200);
        String childToken = child.text("identity_token");
        assertThat(childToken).as("a delegated identity is created holding generation 1, with a token").isNotBlank();

        Resp read = get(childToken, "/v1/identity/" + parent.id() + "/memberships");
        assertThat(read.status()).as("a delegated child reads").isEqualTo(200);
        assertThat(read.json().get("memberships").size()).isEqualTo(1);
        Resp write = join(childToken, parent, group, "leader");
        assertThat(write.status()).as("and does not write its parent's state").isEqualTo(403);
        assertThat(write.error()).isEqualTo("not_owner");
        Resp leave = delete(childToken, "/v1/identity/" + parent.id() + "/memberships/" + group, null);
        assertThat(leave.status()).isEqualTo(403);
    }

    @Test
    void theWorkRoutesAreNotFenced() {
        Seat owner = bindSeat("work-owner", 210);
        Resp tokenless = post(null, "/v1/identity/" + owner.id() + "/work", Map.of("kind", "wait", "ref", "w1", "state", "running"));
        assertThat(tokenless.status()).as("work is a label, not a fenced family: " + tokenless.raw()).isEqualTo(200);
        Seat peer = bindSeat("work-peer", 211);
        Resp byPeer = post(peer.token(), "/v1/identity/" + owner.id() + "/work", Map.of("kind", "wait", "ref", "w2", "state", "running"));
        assertThat(byPeer.status()).isEqualTo(200);
    }
}
