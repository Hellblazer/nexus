// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-253 Step 2 (nexus-hczdo.1.2): the threads-entry fence. A T2 entry in project {@code threads} that carries an
 * {@code identity: <id>} line is writable only by that identity at its current generation, for every operation the
 * RDR lists: {@code put} (with {@code replaces}), {@code put_or_merge}, {@code merge}, {@code import},
 * {@code import_batch}, {@code delete}, {@code restore} and {@code summaries}. The check reads the stored entry,
 * so a write that drops the line does not escape it; a new entry may name only the writer's own identity;
 * {@code expire} and {@code reap} skip such entries; a row written with a token records the identity.
 */
class IdentityThreadsFenceTest extends IdentityFenceHarness {

    private static String entry(String identityId, String body) {
        return "session: sess-x\n" + (identityId == null ? "" : "identity: " + identityId + "\n") + body + "\n";
    }

    private long put(String token, String title, String content) {
        Resp r = post(token, "/v1/memory/put", Map.of("project", "threads", "title", title, "content", content));
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        return r.json().get("id").asLong();
    }

    private Resp putResp(String token, String title, String content) {
        return post(token, "/v1/memory/put", Map.of("project", "threads", "title", title, "content", content));
    }

    private String contentOf(String title) {
        Resp r = get(null, "/v1/memory/get?project=threads&title=" + title);
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        return r.text("content");
    }

    private String stampOf(long id) {
        return scope.withTenant(TENANT, ctx -> ctx.select(MEMORY.IDENTITY).from(MEMORY)
            .where(MEMORY.TENANT_ID.eq(TENANT)).and(MEMORY.ID.eq(id)).fetchOne(MEMORY.IDENTITY));
    }

    private void quarantine(long id) {
        scope.withTenant(TENANT, ctx -> ctx.update(MEMORY).set(MEMORY.QUARANTINED_AT, OffsetDateTime.now(ZoneOffset.UTC))
            .where(MEMORY.TENANT_ID.eq(TENANT)).and(MEMORY.ID.eq(id)).execute());
    }

    /** One operation that would change the entry {@code title} ({@code id}); the caller supplies the token. */
    interface Op {
        Resp run(String token, String title, long id, String contentWithLine);
    }

    private Map<String, Op> ops() {
        Map<String, Op> ops = new LinkedHashMap<>();
        ops.put("put", (t, title, id, c) -> putResp(t, title, c + "put"));
        ops.put("put_or_merge", (t, title, id, c) -> post(t, "/v1/memory/put_or_merge",
            Map.of("project", "threads", "title", title, "content", c + "pom")));
        ops.put("put with replaces", (t, title, id, c) -> post(t, "/v1/memory/put",
            Map.of("project", "threads", "title", unique("new"), "content", "no identity line here", "replaces", title)));
        ops.put("import", (t, title, id, c) -> post(t, "/v1/memory/import", Map.of("project", "threads", "title", title,
            "content", c + "imp", "timestamp", "2026-11-01T00:00:00Z")));
        ops.put("import_batch", (t, title, id, c) -> post(t, "/v1/memory/import_batch", Map.of("rows", List.of(Map.of(
            "project", "threads", "title", title, "content", c + "batch", "timestamp", "2026-11-01T00:00:00Z")))));
        ops.put("delete by title", (t, title, id, c) -> call(TENANT_TOKEN, t, null, "DELETE",
            "/v1/memory/delete?project=threads&title=" + title, null));
        ops.put("delete by id", (t, title, id, c) -> call(TENANT_TOKEN, t, null, "DELETE", "/v1/memory/delete?id=" + id, null));
        ops.put("merge (the kept row)", (t, title, id, c) -> post(t, "/v1/memory/merge",
            Map.of("keep_id", id, "delete_ids", List.of(), "merged_content", c + "merged")));
        ops.put("merge (a deleted row)", (t, title, id, c) -> {
            long keep = put(null, unique("keep"), "an entry with no identity line");
            return post(t, "/v1/memory/merge", Map.of("keep_id", keep, "delete_ids", List.of(id), "merged_content", "merged"));
        });
        ops.put("summaries (a source)", (t, title, id, c) -> post(t, "/v1/memory/summaries",
            Map.of("project", "threads", "content", "a rollup", "source_ids", List.of(id), "model", "m")));
        return ops;
    }

    private static boolean passed(Resp r) {
        return r.status() == 200 || r.status() == 204;
    }

    // ── ownership ────────────────────────────────────────────────────────────

    @Test
    void aNonOwnerIsRefusedOnEveryOperationAndTheOwnerPasses() {
        Seat owner = bindSeat("thread-owner", 300);
        Seat peer = bindSeat("thread-peer", 301);
        for (var op : ops().entrySet()) {
            String title = unique("entry");
            String original = entry(owner.id(), "the owner's work");
            long id = put(owner.token(), title, original);
            Resp refused = op.getValue().run(peer.token(), title, id, entry(owner.id(), "x"));
            assertThat(refused.status()).as(op.getKey() + " by a non-owner: " + refused.raw()).isEqualTo(403);
            assertThat(refused.error()).as(op.getKey()).isEqualTo("not_owner");
            assertThat(contentOf(title)).as(op.getKey() + " left the entry as it was").isEqualTo(original);
            // A peer's token that names ITS identity does not let it claim the entry by writing its own line.
            Resp owned = op.getValue().run(owner.token(), title, id, entry(owner.id(), "the owner's next"));
            assertThat(passed(owned)).as(op.getKey() + " by the owner: " + owned.raw()).isTrue();
        }
    }

    @Test
    void aSupersededGenerationIsRefusedOnEveryOperationAndTheCurrentOnePasses() {
        Seat owner = bindSeat("thread-sup", 310);
        List<Long> ids = new java.util.ArrayList<>();
        List<String> titles = new java.util.ArrayList<>();
        List<String> names = new java.util.ArrayList<>();
        Map<String, Op> ops = ops();
        for (String name : ops.keySet()) {
            String title = unique("entry");
            titles.add(title);
            names.add(name);
            ids.add(put(owner.token(), title, entry(owner.id(), "work")));
        }
        Seat second = resumeInNewProcess(owner, 311, false);
        for (int i = 0; i < names.size(); i++) {
            Resp refused = ops.get(names.get(i)).run(owner.token(), titles.get(i), ids.get(i), entry(owner.id(), "late"));
            assertThat(refused.status()).as(names.get(i) + ": " + refused.raw()).isEqualTo(409);
            assertThat(refused.error()).as(names.get(i)).isEqualTo("generation_superseded");
            assertThat(contentOf(titles.get(i))).as(names.get(i)).isEqualTo(entry(owner.id(), "work"));
            Resp ok = ops.get(names.get(i)).run(second.token(), titles.get(i), ids.get(i), entry(owner.id(), "current"));
            assertThat(passed(ok)).as(names.get(i) + " at the current generation: " + ok.raw()).isTrue();
        }
    }

    @Test
    void aRestoreOfAQuarantinedEntryIsFencedLikeTheRest() {
        Seat owner = bindSeat("thread-restore", 320);
        Seat peer = bindSeat("thread-restore-peer", 321);
        long id = put(owner.token(), unique("entry"), entry(owner.id(), "work"));
        quarantine(id);
        Resp byPeer = post(peer.token(), "/v1/memory/" + id + "/restore", Map.of());
        assertThat(byPeer.status()).isEqualTo(403);
        assertThat(byPeer.error()).isEqualTo("not_owner");
        assertThat(post(null, "/v1/memory/" + id + "/restore", Map.of()).error()).isEqualTo("identity_token_required");
        Seat second = resumeInNewProcess(owner, 322, false);
        assertThat(post(owner.token(), "/v1/memory/" + id + "/restore", Map.of()).error()).isEqualTo("generation_superseded");
        Resp ok = post(second.token(), "/v1/memory/" + id + "/restore", Map.of());
        assertThat(ok.status()).isEqualTo(200);
        assertThat(ok.json().get("restored").asBoolean()).as("the owner's restore did restore it").isTrue();
    }

    @Test
    void aWriteToAQuarantinedEntryIsStillCheckedAgainstTheStoredRow() {
        Seat owner = bindSeat("thread-q", 330);
        Seat peer = bindSeat("thread-q-peer", 331);
        String title = unique("entry");
        long id = put(owner.token(), title, entry(owner.id(), "work"));
        quarantine(id);
        // A put on the unique key lands on the quarantined row: the fence reads it though every read path hides it.
        Resp byPeer = putResp(peer.token(), title, "overwrite without a line");
        assertThat(byPeer.status()).isEqualTo(403);
        assertThat(byPeer.error()).isEqualTo("not_owner");
    }

    // ── no token ─────────────────────────────────────────────────────────────

    @Test
    void aTokenlessWriteToAFencedEntryIsRefusedOnEveryOperation() {
        Seat owner = bindSeat("thread-tokenless", 340);
        for (var op : ops().entrySet()) {
            String title = unique("entry");
            String original = entry(owner.id(), "work");
            long id = put(owner.token(), title, original);
            Resp refused = op.getValue().run(null, title, id, entry(owner.id(), "x"));
            assertThat(refused.status()).as(op.getKey() + ": " + refused.raw()).isEqualTo(403);
            assertThat(refused.error()).as(op.getKey()).isEqualTo("identity_token_required");
            assertThat(contentOf(title)).as(op.getKey()).isEqualTo(original);
        }
    }

    @Test
    void anEntryWithNoIdentityLineKeepsTodaysRuleAndATokenlessEntryMayNotNameAnIdentity() {
        Seat owner = bindSeat("thread-legacy", 350);
        String title = unique("legacy");
        long id = put(null, title, entry(null, "a board entry from before identity"));
        assertThat(putResp(null, title, entry(null, "rewritten")).status()).as("tokenless, no line, no fence").isEqualTo(200);
        Resp named = putResp(null, title, entry(owner.id(), "now it names a seat"));
        assertThat(named.status()).as("a tokenless write cannot give an entry an identity").isEqualTo(403);
        assertThat(named.error()).isEqualTo("identity_token_required");
        // The owner can give it one: a new entry may name the writer's own identity.
        assertThat(putResp(owner.token(), title, entry(owner.id(), "now the owner's")).status()).isEqualTo(200);
        assertThat(contentOf(title)).contains("identity: " + owner.id());
        assertThat(id).isPositive();
    }

    @Test
    void aTokenlessWriteToAnUnleasedSeatsEntryIsAcceptedAndLabelledUnattested() {
        Seat desktop = bindSeat("thread-desktop", 360, "unleased");
        String title = unique("desk");
        Resp created = putResp(null, title, entry(desktop.id(), "desktop work"));
        assertThat(created.status()).as(created.raw()).isEqualTo(200);
        assertThat(created.unattested()).as("labelled").isTrue();
        assertThat(stampOf(created.json().get("id").asLong())).as("nothing attests who wrote it").isNull();
        Resp again = putResp(null, title, entry(desktop.id(), "desktop work, later"));
        assertThat(again.status()).isEqualTo(200);
        assertThat(again.unattested()).isTrue();
        Resp dropped = putResp(null, title, "no line");
        assertThat(dropped.status()).as("the line is still immutable").isEqualTo(403);
        assertThat(dropped.error()).isEqualTo("identity_line_immutable");
    }

    // ── the identity line ────────────────────────────────────────────────────

    @Test
    void aWriteThatDropsOrChangesTheIdentityLineIsRefused() {
        Seat owner = bindSeat("thread-line", 370);
        Seat peer = bindSeat("thread-line-peer", 371);
        String title = unique("entry");
        String original = entry(owner.id(), "work");
        put(owner.token(), title, original);
        Resp dropped = putResp(owner.token(), title, "session: sess-x\nthe owner forgot the line\n");
        assertThat(dropped.status()).isEqualTo(403);
        assertThat(dropped.error()).isEqualTo("identity_line_immutable");
        Resp changed = putResp(owner.token(), title, entry(peer.id(), "handed to a peer"));
        assertThat(changed.status()).isEqualTo(403);
        assertThat(changed.error()).isEqualTo("identity_line_immutable");
        Resp merged = post(owner.token(), "/v1/memory/put_or_merge",
            Map.of("project", "threads", "title", title, "content", "no line at all"));
        assertThat(merged.error()).isEqualTo("identity_line_immutable");
        assertThat(contentOf(title)).isEqualTo(original);
        // A peer cannot take the entry over by writing its own line over it, either: ownership first.
        Resp taken = putResp(peer.token(), title, entry(peer.id(), "mine now"));
        assertThat(taken.error()).isEqualTo("not_owner");
        assertThat(contentOf(title)).isEqualTo(original);
    }

    @Test
    void aNewEntryMayNameOnlyTheWritersOwnIdentity() {
        Seat owner = bindSeat("thread-new", 380);
        Seat other = bindSeat("thread-new-other", 381);
        Resp named = putResp(owner.token(), unique("entry"), entry(other.id(), "an entry for somebody else"));
        assertThat(named.status()).isEqualTo(403);
        assertThat(named.error()).isEqualTo("not_owner");
        Resp both = putResp(owner.token(), unique("entry"), entry(owner.id(), "one") + "identity: " + other.id() + "\n");
        assertThat(both.status()).as("two identity lines, one of them another's").isEqualTo(403);
        assertThat(putResp(owner.token(), unique("entry"), entry(owner.id(), "its own")).status()).isEqualTo(200);
        // import and import_batch create entries by the same rule.
        Resp imported = post(owner.token(), "/v1/memory/import", Map.of("project", "threads", "title", unique("entry"),
            "content", entry(other.id(), "x"), "timestamp", "2026-11-01T00:00:00Z"));
        assertThat(imported.error()).isEqualTo("not_owner");
        Resp batch = post(owner.token(), "/v1/memory/import_batch", Map.of("rows", List.of(
            Map.of("project", "threads", "title", unique("ok"), "content", entry(owner.id(), "ok"), "timestamp", "2026-11-01T00:00:00Z"),
            Map.of("project", "threads", "title", unique("bad"), "content", entry(other.id(), "bad"), "timestamp", "2026-11-01T00:00:00Z"))));
        assertThat(batch.error()).as("one bad row refuses the batch").isEqualTo("not_owner");
    }

    @Test
    void aSimilarWriteInProjectThreadsNeverMergesIntoAnotherIdentitysEntry() {
        Seat bravo = bindSeat("thread-merge-b", 390);
        Seat alpha = bindSeat("thread-merge-a", 391);
        String words = "deploy the migration runbook rollback staging database schema verification checklist";
        String bravoTitle = unique("bravo");
        String original = entry(bravo.id(), words);
        put(bravo.token(), bravoTitle, original);
        Resp r = post(alpha.token(), "/v1/memory/put_or_merge", Map.of("project", "threads", "title", unique("alpha"),
            "content", entry(alpha.id(), words), "min_similarity", 0.5));
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        assertThat(r.text("action")).as("a fenced entry is not a merge candidate").isEqualTo("inserted");
        assertThat(contentOf(bravoTitle)).as("bravo's entry was not written by alpha").isEqualTo(original);
        // Outside project threads put_or_merge still merges.
        post(null, "/v1/memory/put", Map.of("project", "elsewhere", "title", "e1", "content", words));
        Resp m = post(null, "/v1/memory/put_or_merge", Map.of("project", "elsewhere", "title", "e2", "content", words,
            "min_similarity", 0.5));
        assertThat(m.text("action")).isEqualTo("merged");
    }

    @Test
    void aSimilarWriteInProjectThreadsStillMergesIntoALegacyEntryWithNoIdentityLine() {
        Seat alpha = bindSeat("thread-legacy-merge", 392);
        String words = "rotate the signing keys nightly then verify every downstream consumer picked them up";
        String legacyTitle = unique("legacy");
        put(null, legacyTitle, "session: old\n" + words + "\n");
        // A fenced entry that is just as alike is passed over; the legacy one is the target.
        put(alpha.token(), unique("fenced"), entry(alpha.id(), words + " today"));
        Resp r = post(alpha.token(), "/v1/memory/put_or_merge", Map.of("project", "threads", "title", unique("new"),
            "content", "session: new\n" + words + "\n", "min_similarity", 0.5));
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        assertThat(r.text("action")).as("a legacy entry keeps today's put_or_merge").isEqualTo("merged");
        assertThat(contentOf(legacyTitle)).contains("merged from");
    }

    @Test
    void anOverwriteWithoutATokenClearsTheStampOfTheRowItOverwrites() {
        Seat owner = bindSeat("thread-restamp", 423);
        String title = unique("restamp");
        long id = put(owner.token(), title, "first");
        assertThat(stampOf(id)).isEqualTo(owner.id());
        // Project threads with no identity line is a legacy entry a tokenless writer may overwrite.
        long afterPut = put(null, title, "second");
        assertThat(afterPut).isEqualTo(id);
        assertThat(stampOf(id)).as("put").isNull();

        Map<String, Object> row = Map.of("project", "threads", "title", title, "content", "third",
            "timestamp", "2026-11-01T00:00:00Z");
        put(owner.token(), title, "again");
        assertThat(stampOf(id)).isEqualTo(owner.id());
        assertThat(post(null, "/v1/memory/import", row).status()).isEqualTo(200);
        assertThat(stampOf(id)).as("import").isNull();

        put(owner.token(), title, "once more");
        assertThat(post(null, "/v1/memory/import_batch", Map.of("rows", List.of(row))).status()).isEqualTo(200);
        assertThat(stampOf(id)).as("import_batch").isNull();
    }

    @Test
    void aDelegatedChildReadsItsParentsEntryAndDoesNotWriteIt() {
        Seat parent = bindSeat("thread-parent", 400);
        String title = unique("entry");
        String original = entry(parent.id(), "the parent's work");
        long id = put(parent.token(), title, original);
        Resp child = post(null, "/v1/identity/delegated", Map.of("parent", parent.id(), "display_name", "kid",
            "bind", evidence("kid-sess", 401, null)));
        String childToken = child.text("identity_token");
        assertThat(call(TENANT_TOKEN, childToken, null, "GET", "/v1/memory/get?project=threads&title=" + title, null).status())
            .as("reads are never checked").isEqualTo(200);
        Resp write = putResp(childToken, title, original + "the child's line\n");
        assertThat(write.status()).isEqualTo(403);
        assertThat(write.error()).isEqualTo("not_owner");
        assertThat(call(TENANT_TOKEN, childToken, null, "DELETE", "/v1/memory/delete?id=" + id, null).status()).isEqualTo(403);
        assertThat(contentOf(title)).isEqualTo(original);
    }

    // ── the sweeps ───────────────────────────────────────────────────────────

    private long insertOld(String title, String content, boolean quarantinedAndRolledUp) {
        OffsetDateTime old = OffsetDateTime.now(ZoneOffset.UTC).minusDays(400);
        return scope.withTenant(TENANT, ctx -> {
            var step = ctx.insertInto(MEMORY).set(MEMORY.TENANT_ID, TENANT).set(MEMORY.PROJECT, "threads")
                .set(MEMORY.TITLE, title).set(MEMORY.CONTENT, content).set(MEMORY.TIMESTAMP, old)
                .set(MEMORY.TTL_DAYS, 1);
            if (quarantinedAndRolledUp) {
                step = step.set(MEMORY.QUARANTINED_AT, old).set(MEMORY.ROLLED_UP_AT, old);
            }
            return step.returning(MEMORY.ID).fetchOne().getId();
        });
    }

    private boolean quarantined(long id) {
        return scope.withTenant(TENANT, ctx -> ctx.select(MEMORY.QUARANTINED_AT).from(MEMORY)
            .where(MEMORY.TENANT_ID.eq(TENANT)).and(MEMORY.ID.eq(id)).fetchOne(MEMORY.QUARANTINED_AT) != null);
    }

    private boolean exists(long id) {
        return scope.withTenant(TENANT, ctx -> ctx.fetchExists(MEMORY, MEMORY.TENANT_ID.eq(TENANT).and(MEMORY.ID.eq(id))));
    }

    @Test
    void expireAndReapLeaveAFencedEntryAloneAndStillSweepTheRest() {
        Seat owner = bindSeat("thread-sweep", 410);
        long fenced = insertOld(unique("fenced"), entry(owner.id(), "old work"), false);
        long plain = insertOld(unique("plain"), entry(null, "old work"), false);
        Resp expire = post(null, "/v1/memory/expire", Map.of());
        assertThat(expire.status()).isEqualTo(200);
        assertThat(quarantined(plain)).as("non-vacuity: expire does sweep an unfenced entry that is past its ttl").isTrue();
        assertThat(quarantined(fenced)).as("and leaves the fenced one").isFalse();

        long fencedRolled = insertOld(unique("fenced-rolled"), entry(owner.id(), "old work"), true);
        long plainRolled = insertOld(unique("plain-rolled"), entry(null, "old work"), true);
        Resp reap = post(null, "/v1/memory/reap", Map.of());
        assertThat(reap.status()).isEqualTo(200);
        assertThat(exists(plainRolled)).as("non-vacuity: reap deletes an unfenced entry that is quarantined and rolled up").isFalse();
        assertThat(exists(fencedRolled)).as("and keeps the fenced one").isTrue();
    }

    // ── the T2 stamp ─────────────────────────────────────────────────────────

    @Test
    void aRowWrittenWithATokenRecordsTheIdentityAndOneWrittenWithoutDoesNot() {
        Seat owner = bindSeat("thread-stamp", 420);
        long stamped = put(owner.token(), unique("entry"), entry(owner.id(), "work"));
        assertThat(stampOf(stamped)).isEqualTo(owner.id());
        long unstamped = put(null, unique("legacy"), entry(null, "legacy"));
        assertThat(stampOf(unstamped)).isNull();
        // Any T2 project is stamped, not only threads; the stamp is the token's, never the request's.
        Resp other = post(owner.token(), "/v1/memory/put", Map.of("project", "notes", "title", unique("n"), "content", "a note",
            "agent", "someone-else"));
        assertThat(stampOf(other.json().get("id").asLong())).isEqualTo(owner.id());
        // The stamp is the token's identity at each write, so a different seat that writes a row of its own restamps it.
        Seat peer = bindSeat("thread-stamp-peer", 422);
        String title = unique("note");
        long id = scope.withTenant(TENANT, ctx -> ctx.insertInto(MEMORY).set(MEMORY.TENANT_ID, TENANT).set(MEMORY.PROJECT, "notes")
            .set(MEMORY.TITLE, title).set(MEMORY.CONTENT, "first").set(MEMORY.TIMESTAMP, OffsetDateTime.now(ZoneOffset.UTC))
            .set(MEMORY.IDENTITY, owner.id()).returning(MEMORY.ID).fetchOne().getId());
        post(peer.token(), "/v1/memory/put", Map.of("project", "notes", "title", title, "content", "second"));
        assertThat(stampOf(id)).isEqualTo(peer.id());
    }
}
