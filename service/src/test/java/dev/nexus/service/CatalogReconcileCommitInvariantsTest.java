// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository.ReconcileCommitException;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.DriverManager;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-193 Part A1 (nexus-u5m5g.3): the invariants {@code nexus.catalog_reconcile_commit} carries from the repository
 * methods it replaces (research-29), one test each: an alias target, the {@code chunk_count} re-derive and its
 * metadata-only exception, {@code indexed_at} moving only on a {@code head_hash} change, {@code NoteUri} spelling,
 * {@code ensureOwnerRow}, the containment refusal, sensitivity inheritance and its audit, two new rows sharing one
 * tumbler, a lost insert race, and the minting drift test against {@code claimNextSeq}'s contract.
 */
class CatalogReconcileCommitInvariantsTest extends CatalogReconcileTestBase {

    @BeforeAll
    void boot() throws Exception {
        migrate();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, "svc_rc048_invariants", "svc_rc048_invariants_pass");
        }
        connect("svc_rc048_invariants", "svc_rc048_invariants_pass");
    }

    private static final String OWNER = "1.1";

    private String tenantWithOwner() {
        String t = newTenant();
        owner(t, OWNER, "/r");
        return t;
    }

    // ── an update addressed at an alias lands on its canonical ──────────

    @Test
    void anUpdateAddressedAtAnAlias_landsOnTheCanonicalRow() {
        String t = tenantWithOwner();
        String canon = seed(t, OWNER, "canonical.py");
        String alias = seed(t, OWNER, "alias.py");
        setDoc(t, alias, CATALOG_DOCUMENTS.ALIAS_OF, canon);

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", present(0, "alias.py").set("head_hash", "H9"));

        assertThat(tumblerOf(out, "alias.py")).as("the outcome names the row the client addressed").isEqualTo(alias);
        assertThat(actionOf(out, "alias.py")).isEqualTo("updated");
        assertThat(col(t, canon, "head_hash")).as("the write lands on the canonical target").isEqualTo("H9");
        assertThat(col(t, alias, "head_hash")).as("the alias row is not written").isEqualTo("H0");
    }

    // ── chunk_count ─────────────────────────────────────────────────────

    private static String hex(String seed) {
        return dev.nexus.service.db.Chash.ofText(seed).toHex();
    }

    private void manifest(String tenant, String tumbler, String collection, String... seeds) throws Exception {
        List<String> hexes = new ArrayList<>();
        for (String s : seeds) hexes.add(hex(tenant + s));
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, collection);
            PgContainerHelper.insertChunks(ctx, tenant, collection, hexes,
                hexes.stream().map(h -> "text " + h.substring(0, 8)).toList(),
                hexes.stream().map(h -> new float[384]).toList(),
                hexes.stream().map(h -> Map.<String, Object>of()).toList());
        }
        var rows = new ArrayList<Map<String, Object>>();
        for (int i = 0; i < hexes.size(); i++) {
            rows.add(Map.<String, Object>of("position", i, "chash", hexes.get(i), "chunk_index", i));
        }
        repo.writeManifest(tenant, tumbler, collection, rows);
    }

    @Test
    void chunkCount_isReDerivedFromTheManifestOnAnUpdate_exceptAnEmptyManifestNeverZeroesAPositiveCount() throws Exception {
        String t = tenantWithOwner();
        String coll = "knowledge__rc048cc__minilm-l6-v2-384__v1";
        String withManifest = seed(t, OWNER, "m.py", Map.of("physical_collection", coll));
        manifest(t, withManifest, coll, "a", "b", "c");
        setDoc(t, withManifest, CATALOG_DOCUMENTS.CHUNK_COUNT, 0);
        String noManifest = seed(t, OWNER, "n.py", Map.of("physical_collection", coll));
        setDoc(t, noManifest, CATALOG_DOCUMENTS.CHUNK_COUNT, 5);

        reconcile(t, OWNER, "r1", "partial",
            present(0, "m.py").set("physical_collection", coll), present(1, "n.py").set("physical_collection", coll));

        assertThat(col(t, withManifest, "chunk_count")).as("re-derived from the manifest").isEqualTo("3");
        assertThat(col(t, noManifest, "chunk_count")).as("the H2 guard: an empty manifest never zeroes a positive count")
            .isEqualTo("5");
    }

    @Test
    void chunkCount_isLeftAloneOnAMetadataOnlyTouch() throws Exception {
        String t = tenantWithOwner();
        String coll = "knowledge__rc048cm__minilm-l6-v2-384__v1";
        String missing = seed(t, OWNER, "gone.py", Map.of("physical_collection", coll));
        manifest(t, missing, coll, "x", "y");
        setDoc(t, missing, CATALOG_DOCUMENTS.CHUNK_COUNT, 9);
        seed(t, OWNER, "here.py");

        reconcile(t, OWNER, "r1", "complete", present(0, "here.py"));

        assertThat(metaOf(t, missing, "miss_count")).as("the miss-count touch happened").isEqualTo("1");
        assertThat(col(t, missing, "chunk_count")).as("and left chunk_count alone, though the manifest holds 2").isEqualTo("9");
    }

    // ── indexed_at ──────────────────────────────────────────────────────

    @Test
    void indexedAt_movesOnlyWhenHeadHashChanges() {
        String t = tenantWithOwner();
        String a = seed(t, OWNER, "a.py", Map.of("head_hash", "H1"));
        setDoc(t, a, CATALOG_DOCUMENTS.INDEXED_AT, java.time.OffsetDateTime.parse("2020-01-01T00:00:00Z"));
        String old = col(t, a, "indexed_at");

        // same head, different mtime: updated, but indexed_at stays
        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", present(0, "a.py").set("head_hash", "H1"));
        assertThat(actionOf(out, "a.py")).isEqualTo("updated");
        assertThat(col(t, a, "indexed_at")).as("no head_hash change").isEqualTo(old);

        reconcile(t, OWNER, "r2", "partial", present(0, "a.py").set("head_hash", "H2"));
        assertThat(col(t, a, "head_hash")).isEqualTo("H2");
        assertThat(col(t, a, "indexed_at")).as("head_hash changed").isNotEqualTo(old);
        assertThat(indexedAt(t, a).isAfter(java.time.OffsetDateTime.parse("2020-01-02T00:00:00Z"))).isTrue();
    }

    // ── NoteUri spelling ────────────────────────────────────────────────

    @Test
    void aDerivedSourceUri_isAFileUriInCanonicalSpelling_andNothingElseIsWritten() {
        String t = tenantWithOwner();

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial",
            present(0, "src/./a.py"), present(1, "src/../b.py"), present(2, "/r/abs.py"), present(3, "d//e.py"));

        assertThat(col(t, tumblerOf(out, "src/./a.py"), "source_uri")).isEqualTo("file:///r/src/a.py");
        assertThat(col(t, tumblerOf(out, "src/../b.py"), "source_uri")).isEqualTo("file:///r/b.py");
        assertThat(col(t, tumblerOf(out, "/r/abs.py"), "source_uri")).as("an absolute path passes through").isEqualTo("file:///r/abs.py");
        assertThat(col(t, tumblerOf(out, "d//e.py"), "source_uri")).isEqualTo("file:///r/d/e.py");
        assertThat(col(t, tumblerOf(out, "src/./a.py"), "file_path")).as("file_path is stored as staged").isEqualTo("src/./a.py");
    }

    @Test
    void anOwnerWithoutARepoRoot_derivesNothingForARelativePath_andAFileUriForAnAbsoluteOne() {
        String t = newTenant();
        owner(t, OWNER, "");

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", present(0, "rel.py"), present(1, "/abs/x.py"));

        assertThat(col(t, tumblerOf(out, "rel.py"), "source_uri")).as("shapeless, like the legacy entry rule").isEqualTo("");
        assertThat(col(t, tumblerOf(out, "/abs/x.py"), "source_uri")).isEqualTo("file:///abs/x.py");
    }

    @Test
    void aCollectionMove_rewritesANoteUriInItsOwnSpelling_andRefusesACollision() {
        String t = tenantWithOwner();
        String note = seed(t, OWNER, "thing", Map.of("source_uri", "nx-note://knowledge__a/thing",
            "physical_collection", "knowledge__a"));
        String legacy = seed(t, OWNER, "other", Map.of("source_uri", "nx-note://knowledge__a/other",
            "physical_collection", "knowledge__a"));
        // register stores the canonical spelling; a legacy row from before RDR-231 is written directly
        setDoc(t, legacy, CATALOG_DOCUMENTS.SOURCE_URI, "chroma://knowledge__a/other");
        String file = seed(t, OWNER, "f.py", Map.of("physical_collection", "knowledge__a"));

        reconcile(t, OWNER, "r1", "partial",
            present(0, "thing").set("physical_collection", "knowledge__b"),
            present(1, "other").set("physical_collection", "knowledge__b"),
            present(2, "f.py").set("physical_collection", "knowledge__b"));

        assertThat(col(t, note, "source_uri")).isEqualTo("nx-note://knowledge__b/thing");
        assertThat(col(t, legacy, "source_uri")).as("the row's own spelling is kept").isEqualTo("chroma://knowledge__b/other");
        assertThat(col(t, file, "source_uri")).as("a file:// URI is untouched").isEqualTo("file:///r/f.py");

        // a live document already holds the rewritten URI: the whole commit is refused
        String t2 = tenantWithOwner();
        seed(t2, OWNER, "taken", Map.of("source_uri", "nx-note://knowledge__b/clash", "physical_collection", "knowledge__b"));
        seed(t2, OWNER, "clash", Map.of("source_uri", "nx-note://knowledge__a/clash", "physical_collection", "knowledge__a"));
        String before = snapshot(t2);
        stage(t2, OWNER, "r1", "partial", present(0, "clash").set("physical_collection", "knowledge__b"));
        assertThatThrownBy(() -> repo.reconcileCommit(t2, OWNER, "r1", COMMIT_TIMEOUT))
            .isInstanceOf(ReconcileCommitException.class).hasMessageContaining("source_uri_collision")
            .hasMessageContaining("clash");
        assertThat(snapshot(t2)).isEqualTo(before);
    }

    // ── ensureOwnerRow ──────────────────────────────────────────────────

    @Test
    void anAbsentOwnerRow_isCreatedBehindTheIdentityGuard_beforeMinting() {
        String t = newTenant();

        Map<String, Object> out = reconcile(t, "1.7", "r1", "partial", present(0, "/x/a.py"));

        assertThat(tumblerOf(out, "/x/a.py")).isEqualTo("1.7.1");
        var created = ownerRow(t, "1.7");
        assertThat(created.get(CATALOG_OWNERS.NAME) + "/" + created.get(CATALOG_OWNERS.OWNER_TYPE) + "/"
            + created.get(CATALOG_OWNERS.REPO_ROOT) + "/" + created.get(CATALOG_OWNERS.NEXT_SEQ))
            .as("the row register would have made: name = the prefix, type repo, no root, the cursor advanced")
            .isEqualTo("1.7/repo//1");

        // the identity (name, type) already belongs to another address: refused, nothing written
        String t2 = newTenant();
        repo.upsertOwner(t2, Map.of("tumbler_prefix", "1.5", "name", "1.9", "owner_type", "repo", "repo_hash", "hh"));
        String before = snapshot(t2);
        stage(t2, "1.9", "r1", "partial", present(0, "/x/a.py"));
        assertThatThrownBy(() -> repo.reconcileCommit(t2, "1.9", "r1", COMMIT_TIMEOUT))
            .isInstanceOf(ReconcileCommitException.class).hasMessageContaining("owner_identity_conflict");
        assertThat(snapshot(t2)).isEqualTo(before);
    }

    // ── containment ─────────────────────────────────────────────────────

    @Test
    void aDerivedUriOutsideARepoOwnersRoot_isRefused_butACuratorOwnerIsNotGuarded() {
        String t = tenantWithOwner();
        repo.upsertOwner(t, Map.of("tumbler_prefix", "1.3", "name", "cur", "owner_type", "curator", "repo_root", "/r"));

        Map<String, Object> repoOut = reconcile(t, OWNER, "r1", "partial",
            present(0, "../up.py"), present(1, "/elsewhere/x.py"), present(2, "/r/inside.py"), present(3, "/rr/sibling.py"));
        Map<String, Object> curOut = reconcile(t, "1.3", "r2", "partial", present(0, "../up.py"));

        assertThat(actionOf(repoOut, "../up.py")).isEqualTo("refused");
        assertThat(actionOf(repoOut, "/elsewhere/x.py")).isEqualTo("refused");
        assertThat(actionOf(repoOut, "/rr/sibling.py")).as("/rr is not under /r").isEqualTo("refused");
        assertThat(actionOf(repoOut, "/r/inside.py")).isEqualTo("created");
        assertThat(liveCount(t, OWNER)).isEqualTo(1);
        assertThat(actionOf(curOut, "../up.py")).as("only repo owners enforce it").isEqualTo("created");
    }

    // ── sensitivity ─────────────────────────────────────────────────────

    @Test
    void sensitivity_isInheritedFromATombstonedPredecessor_andEveryFlaggedCreatedRowIsAudited() {
        String t = tenantWithOwner();
        String pred = seed(t, OWNER, "p.py");
        tombstone(t, pred, true);
        String plainPred = seed(t, OWNER, "plain.py");
        tombstone(t, plainPred, false);

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial",
            present(0, "p.py"), present(1, "q.py").sensitive(), present(2, "plain.py"), present(3, "r.py"));

        String p = tumblerOf(out, "p.py");
        assertThat(col(t, p, "sensitive")).as("delete-then-re-index keeps the protection").isEqualTo("true");
        assertThat(col(t, tumblerOf(out, "q.py"), "sensitive")).isEqualTo("true");
        assertThat(col(t, tumblerOf(out, "plain.py"), "sensitive")).as("a plain predecessor passes nothing on").isEqualTo("false");
        assertThat(col(t, tumblerOf(out, "r.py"), "sensitive")).isEqualTo("false");
        assertThat(audit(t)).containsExactlyInAnyOrder(
            p + "|false|true|register_inherit predecessor=" + pred + "|engine",
            tumblerOf(out, "q.py") + "|false|true|register_set|engine");
    }

    // ── two new rows, one tumbler ───────────────────────────────────────

    @Test
    void twoNewRowsDerivingOneSourceUri_shareOneTumbler_andConsumeOneSequenceNumber() {
        String t = tenantWithOwner();

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial",
            present(0, "a.py"), present(1, "./a.py").sensitive(), present(2, "b.py"));

        String shared = tumblerOf(out, "a.py");
        assertThat(tumblerOf(out, "./a.py")).isEqualTo(shared);
        assertThat(actionOf(out, "a.py")).isEqualTo("created");
        assertThat(actionOf(out, "./a.py")).as("an alias mirrors its first occurrence").isEqualTo("created");
        assertThat(tumblerOf(out, "b.py")).as("the alias used no sequence number").isEqualTo(OWNER + ".2");
        assertThat(liveCount(t, OWNER)).isEqualTo(2);
        assertThat(col(t, shared, "sensitive")).as("the flag is an OR over everything that names the row").isEqualTo("true");
        assertThat(audit(t)).containsExactly(shared + "|false|true|register_set|engine");
        assertThat(nextSeq(t, OWNER)).isEqualTo(2L);
    }

    // ── a lost insert race ──────────────────────────────────────────────

    @Test
    void aLostInsertRace_handsBackTheWinner_andInsertsNothing() throws Exception {
        String t = tenantWithOwner();
        stage(t, OWNER, "r1", "partial", present(0, "race.py"), present(1, "other.py"));

        try (Connection racer = DriverManager.getConnection(pg.getJdbcUrl(), svcUser, svcPassword)) {
            racer.setAutoCommit(false);
            var racerCtx = DSL.using(racer, SQLDialect.POSTGRES);
            racerCtx.select(DSL.function("set_config", String.class, DSL.val("nexus.tenant"), DSL.val(t), DSL.val(true))).fetch();
            racerCtx.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, t).set(CATALOG_DOCUMENTS.TUMBLER, OWNER + ".50")
                .set(CATALOG_DOCUMENTS.TITLE, "race.py").set(CATALOG_DOCUMENTS.FILE_PATH, "elsewhere/race.py")
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///r/race.py").set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, "code__seed")
                .execute();
            // the racer's row is uncommitted, so the commit does not see a live row and plans an insert that then waits on the key
            CompletableFuture<Map<String, Object>> commit = CompletableFuture.supplyAsync(
                () -> repo.reconcileCommit(t, OWNER, "r1", COMMIT_TIMEOUT));
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(30);
            while (!commitIsWaitingOnALock() && System.nanoTime() < deadline) {
                assertThat(commit.isDone()).as("the commit must be waiting on the racer's key, not finished").isFalse();
                Thread.sleep(50);
            }
            assertThat(commitIsWaitingOnALock()).as("the commit reached the conflicting insert").isTrue();
            racer.commit();

            Map<String, Object> out = commit.get(60, TimeUnit.SECONDS);
            assertThat(actionOf(out, "race.py")).isEqualTo("same_owner_by_uri");
            assertThat(tumblerOf(out, "race.py")).isEqualTo(OWNER + ".50");
            assertThat(actionOf(out, "other.py")).isEqualTo("created");
        }
        assertThat(scope.withTenant(t, ctx -> ctx.fetchCount(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(t)
            .and(CATALOG_DOCUMENTS.SOURCE_URI.eq("file:///r/race.py")))).intValue())
            .as("one document holds the URI").isEqualTo(1);
        assertThat(col(t, OWNER + ".50", "title")).as("the winner's row is untouched").isEqualTo("race.py");
    }

    private boolean commitIsWaitingOnALock() throws Exception {
        var activity = DSL.table(DSL.name("pg_stat_activity"));
        try (Connection su = pg.createConnection("")) {
            return suCtx(su).fetchCount(activity,
                DSL.field(DSL.name("wait_event_type"), String.class).eq("Lock")
                    .and(DSL.field(DSL.name("query"), String.class).like("%catalog_reconcile_commit%"))
                    .and(DSL.field(DSL.name("pid"), Integer.class).ne(DSL.function("pg_backend_pid", Integer.class)))) > 0;
        }
    }

    // ── the minting drift test ──────────────────────────────────────────

    @Test
    void minting_followsClaimNextSeqExactly_onADriftedOwnerWithATombstoneAtTheTop() {
        String t = newTenant();
        owner(t, "1.1", "/r");
        owner(t, "1.2", "/r2");
        for (String o : List.of("1.1", "1.2")) {
            // next_seq lags its children (drift), and the highest child is tombstoned
            insertDoc(t, o + ".5", "five-" + o, Map.of());
            insertDoc(t, o + ".7", "seven-" + o, Map.of(CATALOG_DOCUMENTS.DELETED_AT, java.time.OffsetDateTime.now()));
            setOwner(t, o, CATALOG_OWNERS.NEXT_SEQ, 2L);
        }
        List<String> viaRegister = repo.registerDocumentMany(t, "1.1", List.of(
            Map.of("title", "a", "file_path", "a.py"), Map.of("title", "b", "file_path", "b.py"),
            Map.of("title", "c", "file_path", "c.py")));

        Map<String, Object> out = reconcile(t, "1.2", "r1", "partial", present(0, "a.py"), present(1, "b.py"), present(2, "c.py"));

        assertThat(viaRegister).containsExactly("1.1.8", "1.1.9", "1.1.10");
        assertThat(List.of(tumblerOf(out, "a.py"), tumblerOf(out, "b.py"), tumblerOf(out, "c.py")))
            .as("the commit mints what claimNextSeq's contract mints, in seq order")
            .containsExactly("1.2.8", "1.2.9", "1.2.10");
        for (String o : List.of("1.1", "1.2")) {
            assertThat(nextSeq(t, o)).as("next_seq of " + o).isEqualTo(10L);
        }
    }
}
