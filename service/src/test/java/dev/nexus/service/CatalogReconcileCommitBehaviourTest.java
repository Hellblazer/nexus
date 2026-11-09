// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository.ReconcileCommitException;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-193 Part A1 (nexus-u5m5g.3): the behaviour of {@code nexus.catalog_reconcile_commit} through the repository,
 * on a migrated database as the NOBYPASSRLS service role. One test per behaviour the design's Step 2 names (miss,
 * second miss, trash; partial walk; keep rows; sensitive on created rows; a refused row; RLS isolation; landing rows
 * cleared; a re-commit; a short stage; a duplicate file_path at stage; the owner head_hash; the 20,000-row bar;
 * residual 4, the poison row), then the match arms, the five-arm predicate and the rename rules.
 */
class CatalogReconcileCommitBehaviourTest extends CatalogReconcileTestBase {

    @BeforeAll
    void boot() throws Exception {
        migrate();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, "svc_rc048_behaviour", "svc_rc048_behaviour_pass");
        }
        connect("svc_rc048_behaviour", "svc_rc048_behaviour_pass");
    }

    private static final String OWNER = "1.1";

    private String tenantWithOwner() {
        String t = newTenant();
        owner(t, OWNER, "/r");
        return t;
    }

    private static void assertFails(String code, Runnable r) {
        assertThatThrownBy(r::run).isInstanceOf(ReconcileCommitException.class)
            .satisfies(e -> assertThat(((ReconcileCommitException) e).code()).isEqualTo(code));
    }

    // ── miss, second miss, trash ────────────────────────────────────────

    @Test
    void missThenSecondMissThenTrash_onACompleteWalk() {
        String t = tenantWithOwner();
        String a = seed(t, OWNER, "a.py");
        String b = seed(t, OWNER, "b.py");

        Map<String, Object> first = reconcile(t, OWNER, "r1", "complete", present(0, "a.py"));
        assertThat(metaOf(t, b, "miss_count")).as("first miss").isEqualTo("1");
        assertThat(col(t, b, "deleted_at")).as("a first miss deletes nothing").isNull();
        assertThat(listOf(first, "swept")).isEmpty();
        assertThat(listOf(first, "trashed")).isEmpty();
        assertThat(metaOf(t, a, "miss_count")).as("a present row is never counted").isNull();

        Map<String, Object> second = reconcile(t, OWNER, "r2", "complete", present(0, "a.py"));
        assertThat(col(t, b, "deleted_at")).as("the second miss trashes").isNotNull();
        assertThat(listOf(second, "swept")).containsExactly(b);
        assertThat(listOf(second, "trashed")).containsExactly(b);
        assertThat(col(t, a, "deleted_at")).isNull();
    }

    @Test
    void aPresentRowWithAMissCountIsResetByAMetadataOnlyMerge() {
        String t = tenantWithOwner();
        String a = seed(t, OWNER, "a.py", Map.of("meta", Map.of("miss_count", 1, "other", "kept")));
        setDoc(t, a, CATALOG_DOCUMENTS.CHUNK_COUNT, 7);
        String indexedBefore = col(t, a, "indexed_at");

        // a.py is staged with the head, collection and mtime it already has, so the five-arm predicate does not select it
        setDoc(t, a, CATALOG_DOCUMENTS.HEAD_HASH, "H1");
        setDoc(t, a, CATALOG_DOCUMENTS.SOURCE_MTIME, 1.0);
        reconcile(t, OWNER, "r1", "complete", present(0, "a.py").set("physical_collection", "code__seed"));

        assertThat(metaOf(t, a, "miss_count")).isEqualTo("0");
        assertThat(metaOf(t, a, "other")).as("a merge, not a replacement").isEqualTo("\"kept\"");
        assertThat(col(t, a, "chunk_count")).as("a miss-count touch leaves chunk_count alone").isEqualTo("7");
        assertThat(col(t, a, "indexed_at")).isEqualTo(indexedBefore);
    }

    // ── partial walk ────────────────────────────────────────────────────

    @Test
    void aPartialWalk_touchesNoUnvisitedRow_andTrashesDeletedRows() {
        String t = tenantWithOwner();
        String a = seed(t, OWNER, "a.py");
        String b = seed(t, OWNER, "b.py");
        String c = seed(t, OWNER, "c.py");
        String bBefore = snapshotOf(t, b);

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", present(0, "a.py"), deleted(1, "c.py"));

        assertThat(snapshotOf(t, b)).as("an unvisited row is not touched, not even its miss count").isEqualTo(bBefore);
        assertThat(col(t, c, "deleted_at")).as("the deleted row is trashed").isNotNull();
        assertThat(actionOf(out, "c.py")).isEqualTo("deleted");
        assertThat(tumblerOf(out, "c.py")).isEqualTo(c);
        assertThat(actionOf(out, "a.py")).isEqualTo("updated");
        assertThat(col(t, a, "head_hash")).isEqualTo("H1");
        assertThat(listOf(out, "trashed")).containsExactly(c);
        assertThat(listOf(out, "swept")).isEmpty();
    }

    // ── keep rows ───────────────────────────────────────────────────────

    @Test
    void keepRowsCountAsPresent_andAreNeverWritten() {
        String t = tenantWithOwner();
        String k = seed(t, OWNER, "k.py", Map.of("meta", Map.of("miss_count", 1)));
        String before = col(t, k, "head_hash");

        Map<String, Object> out = reconcile(t, OWNER, "r1", "complete",
            keep(0, "k.py").set("head_hash", "NEWHEAD").set("physical_collection", "code__elsewhere"),
            keep(1, "never_registered.py"));

        assertThat(actionOf(out, "k.py")).isEqualTo("keep");
        assertThat(tumblerOf(out, "k.py")).isEqualTo(k);
        assertThat(col(t, k, "head_hash")).as("not updated").isEqualTo(before);
        assertThat(col(t, k, "physical_collection")).isEqualTo("code__seed");
        assertThat(col(t, k, "deleted_at")).as("counts as present: not aged toward the two-miss delete").isNull();
        assertThat(metaOf(t, k, "miss_count")).as("present rows get their miss count reset").isEqualTo("0");
        assertThat(actionOf(out, "never_registered.py")).isEqualTo("keep");
        assertThat(liveDoc(t, OWNER, "never_registered.py")).as("a keep row is never created").isNull();
    }

    // ── sensitivity ─────────────────────────────────────────────────────

    @Test
    void sensitive_isSetOnlyOnCreatedRows_andPendingNamesTheExistingOnes() {
        String t = tenantWithOwner();
        String s = seed(t, OWNER, "s.py");

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial",
            present(0, "s.py").sensitive(), present(1, "n.py").sensitive(), present(2, "p.py"));

        assertThat(col(t, s, "sensitive")).as("the upsert never flags an existing document").isEqualTo("false");
        String n = tumblerOf(out, "n.py");
        assertThat(col(t, n, "sensitive")).as("a created row takes the staged flag").isEqualTo("true");
        assertThat(col(t, tumblerOf(out, "p.py"), "sensitive")).isEqualTo("false");
        assertThat(listOf(out, "sensitivity_pending")).containsExactly(s);
    }

    // ── refusal ─────────────────────────────────────────────────────────

    @Test
    void aRefusedRow_isNamedWithItsReason_andTheRestCommits() {
        String t = tenantWithOwner();

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", present(0, "ok.py"), present(1, "../outside/x.py"));

        assertThat(actionOf(out, "ok.py")).isEqualTo("created");
        assertThat(actionOf(out, "../outside/x.py")).isEqualTo("refused");
        assertThat((String) outcomeRow(out, "../outside/x.py").get("reason")).contains("repo_root");
        assertThat(outcomeRow(out, "../outside/x.py").get("tumbler")).isNull();
        assertThat(liveCount(t, OWNER)).as("only the contained row was written").isEqualTo(1);
    }

    // ── isolation, landing rows, re-commit, short stage, duplicate ──────

    @Test
    void rls_keepsOneTenantsRunFromAnother() {
        String a = tenantWithOwner();
        String b = tenantWithOwner();
        stage(a, OWNER, "shared-run", "partial", present(0, "a.py"));

        assertThat(stagedRows(b, "shared-run")).as("tenant B reads none of A's landing rows").isZero();
        assertThat(runState(b, "shared-run")).isNull();
        assertFails("run_not_found", () -> repo.reconcileCommit(b, OWNER, "shared-run", COMMIT_TIMEOUT));

        stage(b, OWNER, "shared-run", "partial", present(0, "b.py"));
        repo.reconcileCommit(b, OWNER, "shared-run", COMMIT_TIMEOUT);
        assertThat(liveDoc(b, OWNER, "b.py")).isNotNull();
        assertThat(liveDoc(b, OWNER, "a.py")).as("B's commit wrote none of A's rows").isNull();
        assertThat(stagedRows(a, "shared-run")).as("and left A's run staged").isEqualTo(1);
        assertThat(runState(a, "shared-run")).isEqualTo("staged");

        repo.reconcileCommit(a, OWNER, "shared-run", COMMIT_TIMEOUT);
        assertThat(liveDoc(a, OWNER, "a.py")).isNotNull();
        assertThat(liveDoc(a, OWNER, "b.py")).isNull();
    }

    @Test
    void landingRowsAreCleared_andTheHeaderIsCommittedWithItsOutcome() {
        String t = tenantWithOwner();
        stage(t, OWNER, "r1", "partial", present(0, "a.py"), present(1, "b.py"));
        assertThat(stagedRows(t, "r1")).isEqualTo(2);

        Map<String, Object> out = repo.reconcileCommit(t, OWNER, "r1", COMMIT_TIMEOUT);

        assertThat(stagedRows(t, "r1")).as("landing rows cleared").isZero();
        assertThat(runState(t, "r1")).isEqualTo("committed");
        assertThat(storedOutcome(t, "r1"))
            .as("the outcome is stored on the header").contains("a.py").contains(tumblerOf(out, "a.py"));
    }

    @Test
    void aRecommit_returnsTheStoredOutcome_andWritesNothing() {
        String t = tenantWithOwner();
        seed(t, OWNER, "old.py");
        Map<String, Object> first = reconcile(t, OWNER, "r1", "complete", present(0, "a.py"));
        String after = snapshot(t);

        Map<String, Object> again = repo.reconcileCommit(t, OWNER, "r1", COMMIT_TIMEOUT);

        assertThat(again).isEqualTo(first);
        assertThat(snapshot(t)).as("nothing written by the second commit").isEqualTo(after);
    }

    @Test
    void aShortStage_failsIncompleteStage_withNoWrite() {
        String t = tenantWithOwner();
        seed(t, OWNER, "keepme.py");
        String before = snapshot(t);
        repo.reconcileStage(t, OWNER, "r1", "complete", 3, maps(present(0, "a.py"), present(1, "b.py")));

        assertFails("incomplete_stage", () -> repo.reconcileCommit(t, OWNER, "r1", COMMIT_TIMEOUT));

        assertThat(snapshot(t)).as("a lost stage page can never read as a set of missing files").isEqualTo(before);
        assertThat(runState(t, "r1")).isEqualTo("staged");
        assertThat(stagedRows(t, "r1")).isEqualTo(2);
    }

    @Test
    void aDuplicateFilePath_isRefusedAtStage_namingIt() {
        String t = tenantWithOwner();
        assertThatThrownBy(() -> repo.reconcileStage(t, OWNER, "r1", "partial", 2,
            maps(present(0, "dup.py"), present(1, "dup.py"))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("dup.py");
        assertThat(stagedRows(t, "r1")).as("the page is rolled back").isZero();

        repo.reconcileStage(t, OWNER, "r2", "partial", 3, maps(present(0, "a.py"), present(1, "dup.py")));
        assertThatThrownBy(() -> repo.reconcileStage(t, OWNER, "r2", "partial", 3, maps(present(2, "dup.py"))))
            .as("a duplicate across pages")
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("dup.py");
        assertThat(stagedRows(t, "r2")).isEqualTo(2);
    }

    @Test
    void laterStagePages_mustMatchTheHeader() {
        String t = tenantWithOwner();
        repo.reconcileStage(t, OWNER, "r1", "complete", 2, maps(present(0, "a.py")));
        assertThatThrownBy(() -> repo.reconcileStage(t, OWNER, "r1", "partial", 2, maps(present(1, "b.py"))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("walk");
        assertThatThrownBy(() -> repo.reconcileStage(t, OWNER, "r1", "complete", 5, maps(present(1, "b.py"))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("expected_rows");
        repo.reconcileStage(t, OWNER, "r1", "complete", 2, maps(present(1, "b.py")));
        assertThat(stagedRows(t, "r1")).isEqualTo(2);
    }

    @Test
    void theReap_dropsRunsOlderThanTheInterval_andEveryStageCallRunsItFirst() throws Exception {
        String t = tenantWithOwner();
        stage(t, OWNER, "old-run", "partial", present(0, "a.py"));
        stage(t, OWNER, "young-run", "partial", present(0, "b.py"));
        ageRun(t, "old-run", Duration.ofDays(2));

        assertThat(repo.reconcileReap(t, Duration.ofHours(24))).as("one run reaped").isEqualTo(1);
        assertThat(runState(t, "old-run")).isNull();
        assertThat(stagedRows(t, "old-run")).as("its rows go with it").isZero();
        assertThat(runState(t, "young-run")).isEqualTo("staged");

        ageRun(t, "young-run", Duration.ofDays(3));
        stage(t, OWNER, "third-run", "partial", present(0, "c.py"));
        assertThat(runState(t, "young-run")).as("a stage call reaps first").isNull();
        assertThat(runState(t, "third-run")).isEqualTo("staged");
    }

    @Test
    void theOwnerHeadHash_isNotWrittenByTheCommit() {
        String t = tenantWithOwner();
        setOwner(t, OWNER, CATALOG_OWNERS.HEAD_HASH, "OWNER-HEAD-BEFORE");

        reconcile(t, OWNER, "r1", "complete", present(0, "a.py").set("head_hash", "RUN-HEAD"));

        assertThat(ownerRow(t, OWNER).get(CATALOG_OWNERS.HEAD_HASH))
            .isEqualTo("OWNER-HEAD-BEFORE");
    }

    // ── the accept bar ──────────────────────────────────────────────────

    @Test
    void aTwentyThousandRowCommit_runsUnderThirtySeconds() {
        String t = tenantWithOwner();
        final int n = 20_000;
        for (int run = 1; run <= 2; run++) {
            String id = "bar-" + run;
            for (int start = 0; start < n; start += 1000) {
                var page = new ArrayList<Map<String, Object>>();
                for (int i = start; i < start + 1000; i++) {
                    page.add(present(i, "src/pkg" + (i % 50) + "/file" + i + ".py").hash("hash" + i)
                        .set("head_hash", "HEAD" + run).set("source_mtime", 100.0 + run).m);
                }
                repo.reconcileStage(t, OWNER, id, "complete", n, page);
            }
            long t0 = System.nanoTime();
            Map<String, Object> out = repo.reconcileCommit(t, OWNER, id, Duration.ofMinutes(5));
            long ms = (System.nanoTime() - t0) / 1_000_000;
            System.out.println("reconcile 20000-row commit run " + run + ": " + ms + " ms");
            assertThat(ms).as("run " + run + " (" + (run == 1 ? "20,000 inserts" : "20,000 updates") + ")").isLessThan(30_000);
            assertThat(outcomeRows(out)).hasSize(n);
            assertThat(actionOf(out, "src/pkg1/file1.py")).isEqualTo(run == 1 ? "created" : "updated");
        }
        assertThat(liveCount(t, OWNER)).isEqualTo(n);
    }

    // ── residual 4: a poison row ────────────────────────────────────────

    @Test
    void anUnclassifiedErrorOnOneRow_failsTheCommit_writesNothing_andNamesThatRowsFilePath_onAnInsert() throws Exception {
        String t = tenantWithOwner();
        seed(t, OWNER, "existing.py");
        String before = snapshot(t);
        poisonConstraint(true);
        try {
            stage(t, OWNER, "r1", "complete",
                present(0, "good1.py"), present(1, "poison.py").set("head_hash", "BAD"), present(2, "good2.py"));

            assertThatThrownBy(() -> repo.reconcileCommit(t, OWNER, "r1", COMMIT_TIMEOUT))
                .isInstanceOf(ReconcileCommitException.class)
                .hasMessageContaining("poison.py")
                .hasMessageContaining("fix or remove that file, then re-run nx index repo");
        } finally {
            poisonConstraint(false);
        }
        assertThat(snapshot(t)).as("the commit is all or nothing").isEqualTo(before);
        assertThat(runState(t, "r1")).isEqualTo("staged");
        assertThat(stagedRows(t, "r1")).isEqualTo(3);
    }

    @Test
    void anUnclassifiedErrorOnOneRow_failsTheCommit_writesNothing_andNamesThatRowsFilePath_onAnUpdate() throws Exception {
        String t = tenantWithOwner();
        seed(t, OWNER, "good1.py");
        seed(t, OWNER, "poison.py");
        seed(t, OWNER, "good2.py");
        String before = snapshot(t);
        poisonConstraint(true);
        try {
            stage(t, OWNER, "r1", "complete",
                present(0, "good1.py"), present(1, "poison.py").set("head_hash", "BAD"), present(2, "good2.py"));

            assertThatThrownBy(() -> repo.reconcileCommit(t, OWNER, "r1", COMMIT_TIMEOUT))
                .isInstanceOf(ReconcileCommitException.class)
                .hasMessageContaining("poison.py")
                .hasMessageContaining("fix or remove that file, then re-run nx index repo");
        } finally {
            poisonConstraint(false);
        }
        assertThat(snapshot(t)).isEqualTo(before);
    }

    /** A CHECK that refuses exactly the row (file_path poison.py, head_hash BAD), on insert and on update alike. */
    private void poisonConstraint(boolean on) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            if (on) {
                suCtx(su).alterTable(CATALOG_DOCUMENTS).add(org.jooq.impl.DSL.constraint("rc048_poison").check(
                    org.jooq.impl.DSL.not(CATALOG_DOCUMENTS.FILE_PATH.eq("poison.py").and(CATALOG_DOCUMENTS.HEAD_HASH.eq("BAD")))))
                    .execute();
            } else {
                suCtx(su).alterTable(CATALOG_DOCUMENTS).dropConstraintIfExists("rc048_poison").execute();
            }
        }
    }

    // ── the match arms ──────────────────────────────────────────────────

    @Test
    void sameOwnerByUri_repointsAStaleCollection_andWritesNothingElse() {
        String t = tenantWithOwner();
        // registered under another file_path, but holding the source_uri the staged path derives
        String d = seed(t, OWNER, "old/place.py", Map.of("source_uri", "file:///r/moved/place.py",
            "physical_collection", "code__old", "head_hash", "HX", "title", "kept title"));

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", present(0, "moved/place.py"));

        assertThat(actionOf(out, "moved/place.py")).isEqualTo("same_owner_by_uri");
        assertThat(tumblerOf(out, "moved/place.py")).isEqualTo(d);
        assertThat(col(t, d, "physical_collection")).as("a stale collection is repointed").isEqualTo("code__seed");
        assertThat(col(t, d, "head_hash")).as("nothing else is written").isEqualTo("HX");
        assertThat(col(t, d, "title")).isEqualTo("kept title");
        assertThat(col(t, d, "file_path")).isEqualTo("old/place.py");
        assertThat(liveCount(t, OWNER)).isEqualTo(1);
    }

    @Test
    void foreignOwner_returnsTheTumbler_writesNothing_andNamesBothOwners() {
        String t = tenantWithOwner();
        owner(t, "1.2", "/r");
        String theirs = seed(t, "1.2", "shared.py", Map.of("physical_collection", "code__theirs"));
        String before = snapshotOf(t, theirs);

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", present(0, "shared.py"));

        assertThat(actionOf(out, "shared.py")).isEqualTo("foreign_owner");
        assertThat(tumblerOf(out, "shared.py")).isEqualTo(theirs);
        assertThat(outcomeRow(out, "shared.py").get("other_owner")).isEqualTo("1.2");
        assertThat(outcomeRow(out, "shared.py").get("owner")).as("both owners are named").isEqualTo(OWNER);
        assertThat(snapshotOf(t, theirs)).as("nothing on that row is written").isEqualTo(before);
        assertThat(liveCount(t, OWNER)).as("no document of its own").isZero();
    }

    @Test
    void duplicatePath_updatesTheGreatestTumbler_andReportsTheOthersUntouched() {
        String t = tenantWithOwner();
        String first = seed(t, OWNER, "dup.py");
        // a second live row with the same file_path under the owner (no source_uri, so the unique index allows it)
        insertDoc(t, OWNER + ".2", "dup.py", Map.of(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, "code__seed",
            CATALOG_DOCUMENTS.HEAD_HASH, "H0"));
        String secondBefore = snapshotOf(t, OWNER + ".2");

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", present(0, "dup.py"));

        assertThat(tumblerOf(out, "dup.py")).as("the target is the one documentsByOwner lists last").isEqualTo(OWNER + ".2");
        assertThat(col(t, OWNER + ".2", "head_hash")).isEqualTo("H1");
        assertThat(col(t, first, "head_hash")).as("the other is not touched").isEqualTo("H0");
        assertThat(listOf(out, "duplicate_path")).hasSize(1);
        @SuppressWarnings("unchecked")
        Map<String, Object> dup = (Map<String, Object>) listOf(out, "duplicate_path").get(0);
        assertThat(dup.get("file_path")).isEqualTo("dup.py");
        assertThat(dup.get("tumbler")).isEqualTo(first);
        assertThat(secondBefore).isNotEqualTo(snapshotOf(t, OWNER + ".2"));
    }

    @Test
    void aLegacyRowWithAnEmptySourceUri_isMatchedByPath_notBackfilled_andNotDuplicated() {
        String t = tenantWithOwner();
        insertDoc(t, OWNER + ".1", "legacy.py", Map.of(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, "code__seed",
            CATALOG_DOCUMENTS.HEAD_HASH, "H0", CATALOG_DOCUMENTS.SOURCE_URI, ""));

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", present(0, "legacy.py"));

        assertThat(actionOf(out, "legacy.py")).isEqualTo("updated");
        assertThat(tumblerOf(out, "legacy.py")).isEqualTo(OWNER + ".1");
        assertThat(col(t, OWNER + ".1", "source_uri")).as("the commit does not backfill").isEqualTo("");
        assertThat(liveCount(t, OWNER)).isEqualTo(1);
    }

    @Test
    void aDeletedPath_heldOnlyByAnotherOwner_isSkipped() {
        String t = tenantWithOwner();
        owner(t, "1.2", "/r2");
        String theirs = seed(t, "1.2", "gone.py");

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", deleted(0, "gone.py"));

        assertThat(col(t, theirs, "deleted_at")).as("left live deliberately").isNull();
        @SuppressWarnings("unchecked")
        Map<String, Object> skipped = (Map<String, Object>) listOf(out, "deleted_skipped_foreign_owner").get(0);
        assertThat(skipped.get("file_path")).isEqualTo("gone.py");
        assertThat(skipped.get("tumbler")).isEqualTo(theirs);
        assertThat(listOf(out, "trashed")).isEmpty();
    }

    // ── the five-arm predicate ──────────────────────────────────────────

    @Test
    void aRowTheFivePredicatesDoNotSelect_isNotRewritten() {
        String t = tenantWithOwner();
        String a = seed(t, OWNER, "a.py", Map.of("meta", Map.of("content_hash", "h1", "source_format", "python"),
            "head_hash", "H1", "physical_collection", "code__seed"));
        setDoc(t, a, CATALOG_DOCUMENTS.SOURCE_MTIME, 1.0);
        String before = snapshotOf(t, a);

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial",
            present(0, "a.py").hash("h1").stamp(Map.of("source_format", "python")));

        assertThat(actionOf(out, "a.py")).isEqualTo("unchanged");
        assertThat(snapshotOf(t, a)).isEqualTo(before);
    }

    @Test
    void aHeadBumpAlone_isUpdated_withContentHashChangedFalse_andAContentChangeIsTrue() {
        String t = tenantWithOwner();
        String a = seed(t, OWNER, "a.py", Map.of("meta", Map.of("content_hash", "h1")));
        String b = seed(t, OWNER, "b.py", Map.of("meta", Map.of("content_hash", "h1")));

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial",
            present(0, "a.py").hash("h1"), present(1, "b.py").hash("h2"));

        assertThat(actionOf(out, "a.py")).isEqualTo("updated");
        assertThat(outcomeRow(out, "a.py").get("content_hash_changed")).isEqualTo(false);
        assertThat(actionOf(out, "b.py")).isEqualTo("updated");
        assertThat(outcomeRow(out, "b.py").get("content_hash_changed")).isEqualTo(true);
        assertThat(metaOf(t, b, "content_hash")).isEqualTo("\"h2\"");
        assertThat(metaOf(t, a, "content_hash")).isEqualTo("\"h1\"");
    }

    @Test
    void anEmptyFileHash_isAReadFailure_notAChange() {
        String t = tenantWithOwner();
        String a = seed(t, OWNER, "a.py", Map.of("meta", Map.of("content_hash", "h1"), "head_hash", "H1"));
        setDoc(t, a, CATALOG_DOCUMENTS.SOURCE_MTIME, 1.0);

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", present(0, "a.py").hash(""));

        assertThat(actionOf(out, "a.py")).isEqualTo("unchanged");
        assertThat(outcomeRow(out, "a.py").get("content_hash_changed")).isEqualTo(false);
        assertThat(metaOf(t, a, "content_hash")).isEqualTo("\"h1\"");
    }

    @Test
    void tagsAndCategoryAreFillOnly_whileTheRestOfTheStampOverwrites() {
        String t = tenantWithOwner();
        String set = seed(t, OWNER, "set.py", Map.of("head_hash", "H1",
            "meta", Map.of("tags", List.of("curated"), "category", "mine", "source_format", "old")));
        String unsetNull = seed(t, OWNER, "n.py", Map.of("head_hash", "H1", "meta", Map.of("tags", "")));
        String unsetEmpty = seed(t, OWNER, "e.py", Map.of("head_hash", "H1", "meta", Map.of("tags", List.of())));
        String absent = seed(t, OWNER, "x.py", Map.of("head_hash", "H1"));
        setDocs(t, CATALOG_DOCUMENTS.SOURCE_MTIME, 1.0);
        Map<String, Object> stamp = Map.of("tags", List.of("py", "code"), "category", "code", "source_format", "python");

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial",
            present(0, "set.py").stamp(stamp), present(1, "n.py").stamp(stamp), present(2, "e.py").stamp(stamp),
            present(3, "x.py").stamp(stamp));

        assertThat(metaOf(t, set, "tags")).as("a held value is neither stale nor rewritten").isEqualTo("[\"curated\"]");
        assertThat(metaOf(t, set, "category")).isEqualTo("\"mine\"");
        assertThat(metaOf(t, set, "source_format")).as("the rest of the stamp compares and overwrites").isEqualTo("\"python\"");
        assertThat(actionOf(out, "set.py")).as("source_format differs, so the stamp is stale").isEqualTo("updated");
        for (String filled : List.of(unsetNull, unsetEmpty, absent)) {
            assertThat(metaOf(t, filled, "tags")).isEqualTo("[\"py\",\"code\"]");
            assertThat(metaOf(t, filled, "category")).isEqualTo("\"code\"");
        }
    }

    @Test
    void aNullStampValue_overwritesAnRdrId_andAnAbsentOneReadsAsStale() {
        String t = tenantWithOwner();
        String withId = seed(t, OWNER, "r1.md", Map.of("head_hash", "H1", "content_type", "rdr", "meta", Map.of("rdr_id", "RDR-1")));
        String without = seed(t, OWNER, "r2.md", Map.of("head_hash", "H1", "content_type", "rdr"));
        setDocs(t, CATALOG_DOCUMENTS.SOURCE_MTIME, 1.0);
        Map<String, Object> nullId = new java.util.LinkedHashMap<>();
        nullId.put("rdr_id", null);

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial",
            present(0, "r1.md").stamp(nullId), present(1, "r2.md").stamp(nullId));

        assertThat(actionOf(out, "r1.md")).isEqualTo("updated");
        assertThat(metaOf(t, withId, "rdr_id")).isEqualTo("null");
        assertThat(actionOf(out, "r2.md")).as("absent against null is stale").isEqualTo("updated");
        assertThat(metaOf(t, without, "rdr_id")).isEqualTo("null");
        // the next run finds them equal and writes nothing
        Map<String, Object> again = reconcile(t, OWNER, "r2", "partial",
            present(0, "r1.md").stamp(nullId), present(1, "r2.md").stamp(nullId));
        assertThat(actionOf(again, "r1.md")).isEqualTo("unchanged");
        assertThat(actionOf(again, "r2.md")).isEqualTo("unchanged");
    }

    @Test
    void anIndexStateAndHashAreReadBeforeTheUpdate() {
        String t = tenantWithOwner();
        String a = seed(t, OWNER, "a.py");
        setDoc(t, a, CATALOG_DOCUMENTS.INDEX_STATE, "complete");
        setDoc(t, a, CATALOG_DOCUMENTS.INDEX_CONTENT_HASH, "ICH");

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", present(0, "a.py"));

        assertThat(outcomeRow(out, "a.py").get("index_state")).isEqualTo("complete");
        assertThat(outcomeRow(out, "a.py").get("index_content_hash")).isEqualTo("ICH");
    }

    // ── rename ──────────────────────────────────────────────────────────

    @Test
    void aRename_movesTheLinks_carriesNonEvidentiary_andTrashesTheOldRow() {
        String t = tenantWithOwner();
        String old = seed(t, OWNER, "old.py", Map.of("meta", Map.of("content_hash", "H", "non_evidentiary", true)));
        String partner = seed(t, OWNER, "partner.py", Map.of("meta", Map.of("content_hash", "P"), "head_hash", "H1"));
        link(t, old, partner, "cites", "alice", "1-2", "3-4");
        link(t, partner, old, "relates", "bob", "5-6", "7-8");

        Map<String, Object> out = reconcile(t, OWNER, "r1", "complete",
            present(0, "new.py").hash("H"), present(1, "partner.py").hash("P"));

        String neu = tumblerOf(out, "new.py");
        assertThat(actionOf(out, "new.py")).isEqualTo("created");
        assertThat(col(t, old, "deleted_at")).as("the old row is trashed").isNotNull();
        assertThat((List<?>) out.get("renamed")).hasSize(1);
        @SuppressWarnings("unchecked")
        Map<String, Object> pair = ((List<Map<String, Object>>) out.get("renamed")).get(0);
        assertThat(pair).containsEntry("old", old).containsEntry("new", neu);
        assertThat(metaOf(t, neu, "non_evidentiary")).as("the stamp is carried").isEqualTo("true");
        assertThat(links(t)).contains(
            neu + ">" + partner + ":cites:alice:1-2:3-4:",
            partner + ">" + neu + ":relates:bob:5-6:7-8:");
        assertThat(links(t)).as("the new edges carry created_by and the spans but not the link's metadata")
            .allSatisfy(l -> { if (l.startsWith(neu) || l.contains(">" + neu)) assertThat(l).doesNotContain("note"); });
        assertThat(links(t)).as("the old rows' links stay, as a tombstone leaves them").hasSize(4);
    }

    @Test
    void twoMissingRowsRenameOntoOneTarget_aLinkBetweenThemBecomesASelfLinkAndIsDropped() {
        String t = tenantWithOwner();
        String a = seed(t, OWNER, "a.py", Map.of("meta", Map.of("content_hash", "H")));
        String b = seed(t, OWNER, "b.py", Map.of("meta", Map.of("content_hash", "H")));
        String ext = seed(t, OWNER, "ext.py", Map.of("meta", Map.of("content_hash", "E"), "head_hash", "H1"));
        link(t, a, b, "relates", "u", "", "");
        link(t, b, ext, "cites", "u", "", "");

        Map<String, Object> out = reconcile(t, OWNER, "r1", "complete",
            present(0, "c.py").hash("H"), present(1, "ext.py").hash("E"));

        String c = tumblerOf(out, "c.py");
        assertThat(col(t, a, "deleted_at")).isNotNull();
        assertThat(col(t, b, "deleted_at")).isNotNull();
        assertThat((List<?>) out.get("renamed")).as("several missing rows may rename onto one target").hasSize(2);
        assertThat(links(t)).anyMatch(l -> l.startsWith(c + ">" + ext + ":cites:"));
        assertThat(links(t)).as("a link that becomes a self-link is not written").noneMatch(l -> l.startsWith(c + ">" + c));
    }

    @Test
    void aLinkToASweptOrphan_orToATombstonedPartner_isNotMoved() {
        String t = tenantWithOwner();
        String old = seed(t, OWNER, "old.py", Map.of("meta", Map.of("content_hash", "H")));
        String orphan = seed(t, OWNER, "orphan.py", Map.of("meta", Map.of("miss_count", 1)));
        String dead = seed(t, OWNER, "dead.py");
        tombstone(t, dead, false);
        link(t, old, orphan, "relates", "u", "", "");
        link(t, old, dead, "relates", "u", "", "");

        Map<String, Object> out = reconcile(t, OWNER, "r1", "complete", present(0, "new.py").hash("H"));

        assertThat(listOf(out, "swept")).containsExactly(orphan);
        String neu = tumblerOf(out, "new.py");
        assertThat(links(t)).noneMatch(l -> l.startsWith(neu + ">"));
    }

    @Test
    void presentRowsSharingAHash_thePlaceTheLastTumblerInTextOrderIsTheRenameTarget() {
        String t = tenantWithOwner();
        for (String tumbler : List.of(OWNER + ".9", OWNER + ".10")) {
            insertDoc(t, tumbler, "p" + tumbler.substring(4) + ".py", Map.of(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, "code__seed",
                CATALOG_DOCUMENTS.HEAD_HASH, "H1", CATALOG_DOCUMENTS.METADATA, org.jooq.JSONB.valueOf("{\"content_hash\":\"SAME\"}")));
        }
        setDocs(t, CATALOG_DOCUMENTS.SOURCE_MTIME, 1.0);
        String old = seed(t, OWNER, "old.py", Map.of("meta", Map.of("content_hash", "SAME")));

        Map<String, Object> out = reconcile(t, OWNER, "r1", "complete",
            present(0, "p9.py").hash("SAME"), present(1, "p10.py").hash("SAME"));

        @SuppressWarnings("unchecked")
        Map<String, Object> pair = ((List<Map<String, Object>>) out.get("renamed")).get(0);
        assertThat(pair).containsEntry("old", old).containsEntry("new", OWNER + ".9");
    }

    @Test
    void aRenameThatIsAlsoADeletedRow_onACompleteWalk_isRenamedOnce() {
        String t = tenantWithOwner();
        String old = seed(t, OWNER, "old.py", Map.of("meta", Map.of("content_hash", "H")));

        Map<String, Object> out = reconcile(t, OWNER, "r1", "complete", present(0, "new.py").hash("H"), deleted(1, "old.py"));

        assertThat((List<?>) out.get("renamed")).hasSize(1);
        assertThat(col(t, old, "deleted_at")).isNotNull();
        assertThat(actionOf(out, "old.py")).isEqualTo("deleted");
        assertThat(listOf(out, "trashed")).containsExactly(old);
    }

    @Test
    void aDeletedRowOnACompleteWalk_goesThroughHousekeepingFirst_thenIsTrashedWhenStillLive() {
        String t = tenantWithOwner();
        String gone = seed(t, OWNER, "gone.py");
        String stay = seed(t, OWNER, "stay.py");

        Map<String, Object> out = reconcile(t, OWNER, "r1", "complete", present(0, "stay.py"), deleted(1, "gone.py"));

        assertThat(col(t, gone, "deleted_at")).as("a first miss would only count; the explicit deletion trashes").isNotNull();
        assertThat(col(t, stay, "deleted_at")).isNull();
        assertThat(listOf(out, "swept")).as("not a sweep: it was deleted explicitly").isEmpty();
        assertThat(listOf(out, "trashed")).containsExactly(gone);
    }

    // ══ fix round r1 (nexus-u5m5g.3) ════════════════════════════════════

    // ── a deleted path with live duplicates under the owner ─────────────

    @Test
    void aDeletedPathWithLiveDuplicates_trashesTheRowTheOldPathTrashes_andReportsTheOthers() {
        String t = tenantWithOwner();
        // tumblers 9 and 10: the text order and the numeric order disagree, so "lowest" and "greatest" are different rows
        for (String tumbler : List.of(OWNER + ".9", OWNER + ".10")) {
            insertDoc(t, tumbler, "dup.py", Map.of(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, "code__seed",
                CATALOG_DOCUMENTS.HEAD_HASH, "H0"));
        }
        // the old path (indexer._delete_docs_for_paths -> by_file_path) trashes match[0] of this lookup, which the
        // database orders by tumbler: the first in its text order
        @SuppressWarnings("unchecked")
        String trashedByTheOldPath = (String) repo.documentsByOwnerAndFilePath(t, OWNER, "dup.py", 0, 0).get(0).get("tumbler");
        String other = trashedByTheOldPath.equals(OWNER + ".9") ? OWNER + ".10" : OWNER + ".9";
        assertThat(trashedByTheOldPath).as("non-vacuity: the old lookup's first row is not the one the match arm prefers")
            .isEqualTo(OWNER + ".10");

        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", deleted(0, "dup.py"));

        assertThat(col(t, trashedByTheOldPath, "deleted_at")).as("the row the old path trashes").isNotNull();
        assertThat(col(t, other, "deleted_at")).as("the other duplicate is left live").isNull();
        assertThat(actionOf(out, "dup.py")).isEqualTo("deleted");
        assertThat(tumblerOf(out, "dup.py")).isEqualTo(trashedByTheOldPath);
        assertThat(listOf(out, "trashed")).containsExactly(trashedByTheOldPath);
        @SuppressWarnings("unchecked")
        Map<String, Object> dup = (Map<String, Object>) listOf(out, "duplicate_path").get(0);
        assertThat(listOf(out, "duplicate_path")).as("the others are reported, as before").hasSize(1);
        assertThat(dup.get("file_path")).isEqualTo("dup.py");
        assertThat(dup.get("tumbler")).isEqualTo(other);
    }

    @Test
    void aPresentPathWithLiveDuplicates_stillUpdatesTheGreatestTumbler() {
        String t = tenantWithOwner();
        for (String tumbler : List.of(OWNER + ".9", OWNER + ".10")) {
            insertDoc(t, tumbler, "dup.py", Map.of(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, "code__seed",
                CATALOG_DOCUMENTS.HEAD_HASH, "H0"));
        }
        Map<String, Object> out = reconcile(t, OWNER, "r1", "partial", present(0, "dup.py"));
        assertThat(tumblerOf(out, "dup.py")).as("update_many's row: the one documentsByOwner lists last").isEqualTo(OWNER + ".9");
        assertThat(((Map<?, ?>) listOf(out, "duplicate_path").get(0)).get("tumbler")).isEqualTo(OWNER + ".10");
    }

    // ── the link write helper, and a rename move that follows alias_of ──

    private String linkWrite(String tenant, String from, String to, String type, String fromSpan, String toSpan,
                             String by, boolean ifAbsent) {
        return scope.withTenant(tenant, ctx -> ctx.select(dev.nexus.service.jooq.nexus.Routines.catalogLinkWrite(
                from, to, type, fromSpan, toSpan, by, ifAbsent))
            .fetchOne(0, String.class));
    }

    @Test
    void theLinkWriteHelper_resolvesAliases_checksLiveness_dropsSelfLinks_andFoldsAnExistingEdge() {
        String t = tenantWithOwner();
        String x = seed(t, OWNER, "x.py");
        String alias = seed(t, OWNER, "alias.py");
        String canon = seed(t, OWNER, "canon.py");
        String dead = seed(t, OWNER, "dead.py");
        setDoc(t, alias, CATALOG_DOCUMENTS.ALIAS_OF, canon);
        tombstone(t, dead, false);

        assertThat(linkWrite(t, x, alias, "cites", "1-2", "3-4", "alice", true)).isEqualTo("created");
        assertThat(links(t)).as("the edge lands on the canonical row").containsExactly(x + ">" + canon + ":cites:alice:1-2:3-4:");

        assertThat(linkWrite(t, x, canon, "cites", "9-9", "9-9", "bob", true))
            .as("if-absent: the raw edge exists, nothing is touched").isEqualTo("exists");
        assertThat(linkWrite(t, x, alias, "cites", "5-6", "7-8", "bob", true))
            .as("if-absent keys on the raw edge; the resolved one exists, so it merges as POST /link does").isEqualTo("merged");
        assertThat(links(t)).hasSize(1).first().asString().startsWith(x + ">" + canon + ":cites:alice:5-6:7-8:").contains("bob");

        assertThat(linkWrite(t, x, x, "relates", "", "", "u", false)).as("a self-link").isEqualTo("self");
        assertThat(linkWrite(t, x, dead, "relates", "", "", "u", false)).as("a tombstoned endpoint").isEqualTo("dangling");
        assertThat(linkWrite(t, x, OWNER + ".777", "relates", "", "", "u", false)).as("an absent endpoint").isEqualTo("dangling");
        assertThat(links(t)).as("none of those wrote").hasSize(1);
    }

    @Test
    void aRenameMove_followsAliasOf_likeTodaysPostLink() {
        String t = tenantWithOwner();
        String old = seed(t, OWNER, "old.py", Map.of("meta", Map.of("content_hash", "H")));
        String canon = seed(t, OWNER, "canon.py", Map.of("head_hash", "H1"));
        String aliasRow = seed(t, OWNER, "alias.py", Map.of("head_hash", "H1"));
        String incoming = seed(t, OWNER, "incoming.py", Map.of("head_hash", "H1"));
        link(t, old, aliasRow, "cites", "alice", "1-2", "3-4");
        link(t, incoming, old, "relates", "bob", "", "");
        setDoc(t, aliasRow, CATALOG_DOCUMENTS.ALIAS_OF, canon);
        setDoc(t, incoming, CATALOG_DOCUMENTS.ALIAS_OF, canon);

        Map<String, Object> out = reconcile(t, OWNER, "r1", "complete",
            present(0, "new.py").hash("H"), present(1, "canon.py"));

        String neu = tumblerOf(out, "new.py");
        assertThat(links(t)).contains(neu + ">" + canon + ":cites:alice:1-2:3-4:", canon + ">" + neu + ":relates:bob:::")
            .as("no edge to or from the alias rows themselves")
            .noneMatch(l -> l.startsWith(neu + ">" + aliasRow) || l.startsWith(incoming + ">" + neu));
    }

    // ── a failure outside the three upsert writes names its SQLSTATE, its message and, per row, the file ──

    /** Runs {@code body} with a CHECK constraint on {@code table}, added and dropped as the superuser (RLS does not apply to it). */
    private void withConstraint(org.jooq.Table<?> table, String name, org.jooq.Condition check, Runnable body) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            suCtx(su).alterTable(table).add(org.jooq.impl.DSL.constraint(name).check(check)).execute();
            try {
                body.run();
            } finally {
                suCtx(su).alterTable(table).dropConstraintIfExists(name).execute();
            }
        }
    }

    @Test
    void aHousekeepingMergeTheDatabaseRefuses_failsWithTheSqlStateAndTheMessage_writingNothing() throws Exception {
        String t = tenantWithOwner();
        seed(t, OWNER, "a.py");
        seed(t, OWNER, "poison.py");
        String before = snapshot(t);
        stage(t, OWNER, "r1", "complete", present(0, "a.py").set("head_hash", "H0"));
        // poison.py may not carry a miss_count (the first miss of a complete walk writes one)
        withConstraint(CATALOG_DOCUMENTS, "rc048_miss_poison",
            org.jooq.impl.DSL.not(CATALOG_DOCUMENTS.FILE_PATH.eq("poison.py")
                .and(CATALOG_DOCUMENTS.METADATA.cast(org.jooq.impl.SQLDataType.VARCHAR).like("%miss_count%"))), () ->
            assertThatThrownBy(() -> repo.reconcileCommit(t, OWNER, "r1", COMMIT_TIMEOUT))
                .isInstanceOf(ReconcileCommitException.class)
                .satisfies(e -> assertThat(((ReconcileCommitException) e).code()).isEqualTo("write_failed"))
                .hasMessageContaining("SQLSTATE 23514").hasMessageContaining("rc048_miss_poison"));
        assertThat(snapshot(t)).as("all or nothing").isEqualTo(before);
        assertThat(runState(t, "r1")).isEqualTo("staged");
    }

    @Test
    void anExplicitDeletionTheDatabaseRefuses_failsNamingTheRowAndTheSqlState() throws Exception {
        String t = tenantWithOwner();
        seed(t, OWNER, "ok.py");
        seed(t, OWNER, "poison.py");
        String before = snapshot(t);
        stage(t, OWNER, "r1", "partial", deleted(0, "ok.py"), deleted(1, "poison.py"));
        withConstraint(CATALOG_DOCUMENTS, "rc048_trash_poison",
            org.jooq.impl.DSL.not(CATALOG_DOCUMENTS.FILE_PATH.eq("poison.py").and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull())), () ->
            assertThatThrownBy(() -> repo.reconcileCommit(t, OWNER, "r1", COMMIT_TIMEOUT))
                .isInstanceOf(ReconcileCommitException.class)
                .satisfies(e -> assertThat(((ReconcileCommitException) e).code()).isEqualTo("write_failed"))
                .hasMessageContaining("file_path poison.py").hasMessageContaining("23514")
                .hasMessageContaining("rc048_trash_poison"));
        assertThat(snapshot(t)).as("the trash of ok.py is rolled back with it").isEqualTo(before);
    }

    @Test
    void aLinkInsertTheDatabaseRefuses_failsNamingTheOldRowsFilePath() throws Exception {
        String t = tenantWithOwner();
        String old = seed(t, OWNER, "old.py", Map.of("meta", Map.of("content_hash", "H")));
        String partner = seed(t, OWNER, "partner.py", Map.of("head_hash", "H1"));
        link(t, old, partner, "poison", "u", "", "");
        String before = snapshot(t);
        stage(t, OWNER, "r1", "complete", present(0, "new.py").hash("H"), present(1, "partner.py"));
        // the new row mints as OWNER.3 (two seeded rows before it); no 'poison' link may start there, so the seeded link
        // (from OWNER.1) stands and the copy the move inserts is refused
        withConstraint(dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS, "rc048_link_poison",
            org.jooq.impl.DSL.not(dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS.LINK_TYPE.eq("poison")
                .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS.FROM_TUMBLER.eq(OWNER + ".3"))), () ->
            assertThatThrownBy(() -> repo.reconcileCommit(t, OWNER, "r1", COMMIT_TIMEOUT))
                .isInstanceOf(ReconcileCommitException.class)
                .satisfies(e -> assertThat(((ReconcileCommitException) e).code()).isEqualTo("write_failed"))
                .hasMessageContaining("file_path old.py").hasMessageContaining("23514")
                .hasMessageContaining("rc048_link_poison"));
        assertThat(snapshot(t)).isEqualTo(before);
    }

    // ── row-level security, probed with no tenant predicate ─────────────

    private int unfilteredRows(String tenant, org.jooq.Table<?> table) {
        return scope.withTenant(tenant, ctx -> ctx.fetchCount(table));
    }

    @Test
    void rls_aSelectWithNoTenantPredicateSeesOnlyTheCallersRows_andSeesAllWithRlsOff() throws Exception {
        String a = tenantWithOwner();
        String b = tenantWithOwner();
        stage(a, OWNER, "rls-run", "partial", present(0, "a.py"), present(1, "b.py"));

        assertThat(unfilteredRows(b, dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_ROWS))
            .as("tenant B, rows table, no tenant predicate").isZero();
        assertThat(unfilteredRows(b, dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS))
            .as("tenant B, runs table, no tenant predicate").isZero();
        assertThat(unfilteredRows(a, dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_ROWS))
            .as("the same query as the owning tenant finds its rows").isEqualTo(2);

        // the probe can fail: with row-level security off, the same query as tenant B sees A's rows
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var rowsTable = dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_ROWS;
            var runsTable = dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS;
            PgContainerHelper.setRowSecurity(su, rowsTable, PgContainerHelper.RowSecurityMode.DISABLE);
            PgContainerHelper.setRowSecurity(su, runsTable, PgContainerHelper.RowSecurityMode.DISABLE);
            try {
                assertThat(unfilteredRows(b, dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_ROWS))
                    .as("control, RLS off: B reads A's landing rows").isGreaterThanOrEqualTo(2);
                assertThat(unfilteredRows(b, dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS))
                    .as("control, RLS off: B reads A's run header").isGreaterThanOrEqualTo(1);
            } finally {
                PgContainerHelper.setRowSecurity(su, rowsTable, PgContainerHelper.RowSecurityMode.ENABLE);
                PgContainerHelper.setRowSecurity(su, runsTable, PgContainerHelper.RowSecurityMode.ENABLE);
            }
        }
        assertThat(unfilteredRows(b, dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_ROWS)).as("RLS back on").isZero();
        assertThat(unfilteredRows(b, dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS)).isZero();
    }

    // ── a commit in flight, and two commits of one run ──────────────────

    /** Locks a run's header row FOR UPDATE in {@code su}'s open transaction, as the commit's first statement does. */
    private void holdRunHeader(Connection su, String tenant, String run) {
        suCtx(su).selectOne().from(dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS)
            .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS.TENANT_ID.eq(tenant)
                .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_RECONCILE_RUNS.RUN_ID.eq(run)))
            .forUpdate().fetch();
    }

    @Test
    void aStagePageForARunWhoseCommitHoldsItsHeader_isRefusedWithoutWaiting() throws Exception {
        String t = tenantWithOwner();
        repo.reconcileStage(t, OWNER, "busy", "partial", 2, maps(present(0, "a.py")));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            holdRunHeader(su, t, "busy");
            long t0 = System.nanoTime();
            assertThatThrownBy(() -> repo.reconcileStage(t, OWNER, "busy", "partial", 2, maps(present(1, "b.py"))))
                .isInstanceOf(dev.nexus.service.db.CatalogRepository.ReconcileRunBusyException.class)
                .hasMessageContaining("busy");
            assertThat((System.nanoTime() - t0) / 1_000_000).as("refused at once, not after waiting out the commit").isLessThan(5_000);
            assertThat(stagedRows(t, "busy")).as("the refused page staged nothing").isEqualTo(1);
            su.rollback();
        }
        repo.reconcileStage(t, OWNER, "busy", "partial", 2, maps(present(1, "b.py")));
        assertThat(stagedRows(t, "busy")).as("the same page lands once the header is free").isEqualTo(2);
    }

    @Test
    void twoCommitsOfOneRunRacing_oneDoesTheWork_theOtherGetsTheStoredOutcome_nothingIsWrittenTwice() throws Exception {
        String t = tenantWithOwner();
        stage(t, OWNER, "race", "complete", present(0, "a.py"), present(1, "b.py"), present(2, "c.py"));
        Map<String, Object> first;
        Map<String, Object> second;
        var pool = java.util.concurrent.Executors.newFixedThreadPool(2);   // two threads of our own, not the common pool's
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            // both commits queue on the run header's lock, as two submissions of one run would
            holdRunHeader(su, t, "race");
            var one = pool.submit(() -> repo.reconcileCommit(t, OWNER, "race", COMMIT_TIMEOUT));
            var two = pool.submit(() -> repo.reconcileCommit(t, OWNER, "race", COMMIT_TIMEOUT));
            awaitCommitsBlocked(su, 2);
            su.rollback();
            first = one.get(60, java.util.concurrent.TimeUnit.SECONDS);
            second = two.get(60, java.util.concurrent.TimeUnit.SECONDS);
        } finally {
            pool.shutdownNow();
        }
        assertThat(second).as("both answers are the one stored outcome").isEqualTo(first);
        assertThat(outcomeRows(first)).hasSize(3);
        assertThat(outcomeRows(first)).allSatisfy(r -> assertThat(r.get("action")).isEqualTo("created"));
        assertThat(liveCount(t, OWNER)).as("three documents, not six").isEqualTo(3);
        assertThat(nextSeq(t, OWNER)).as("three sequence numbers claimed, once").isEqualTo(3L);
        assertThat(runState(t, "race")).isEqualTo("committed");
    }
}
