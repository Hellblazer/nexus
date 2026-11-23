// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.ByIdRestoreSupport.NO_REGISTRY_ROW;
import static dev.nexus.service.ByIdRestoreSupport.registryIds;
import static dev.nexus.service.ByIdRestoreSupport.rowsByCollection;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.CollectionScopedTableSites;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * RDR-229 P3.7's acceptance (nexus-p2m1x.27), written with the by-id restore (nexus-p2m1x.31) and enabled with P3.7's one-row rename: a rename
 * through the engine's own route updates the registry row and nothing else, so a tenant backed up after it holds children that still name the
 * old label, and its restore keeps every one of them on the renamed collection. The engine is {@link ImportTestEnv#startOneRow}'s: the state
 * P3.7 leaves an install in, so the route is the one-row rename (the rename harness, {@code Rdr229RenameEngine}, runs the completion check, the
 * worker and the latch themselves).
 *
 * <p>Legs after a real one-row rename A to B (linda 2026-11-23, addenda 9 and the legs of the stage-2 brief): (b) a manifest write of an existing
 * chunk commits (no 23503) and clears its orphan row; (c) a delete by the old label A through the public route leaves B's rows alone; (d) each of
 * the five per-store rename routes, called with A to B, answers success and changes no row; (e) a rehome naming B moves exactly B's rows and a rehome
 * onto B keeps one registry row and B's id. A leg that waits for another slice's code is {@code @Disabled} with its reason, never weakened.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37RenameAcceptanceTest {

    private ImportTestEnv env;

    @BeforeAll
    void startAll() throws Exception {
        env = ImportTestEnv.startOneRow();   // the state P3.7 leaves an install in: name keys dropped, the guard latched and nothing missing
    }

    @AfterAll
    void stopAll() {
        if (env != null) env.close();
    }

    @Test
    void p37Acceptance_aOneRowRenameBackupRestoreKeepsEveryRowOnTheRenamedCollection() throws Exception {
        String src = "t-byid-p37-src", dst = "t-byid-p37-dst";
        String token = ByIdRestoreSupport.batterySource(env, src);
        String old = TenantExportBatteryTest.Seed.collection(src, 384);
        String renamedTo = "knowledge__tbyidp37srcrenamed__minilm-l6-v2-384__v1";
        Map<String, Long> before = registryIds(env, src);
        assertThat(before).containsKey(old).doesNotContainKey(renamedTo);
        long chunksBefore = env.su(ctx -> (long) ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(src).and(CHUNKS.COLLECTION.eq(old))));
        assertThat(chunksBefore).as("non-vacuity: chunks under the collection to rename").isPositive();

        var renamed = env.callJson("POST", "/v1/catalog/collections/rename", token,
            "{\"old_name\":\"" + old + "\",\"new_name\":\"" + renamedTo + "\"}");
        assertThat(renamed.statusCode()).as("the rename: " + renamed.body()).isEqualTo(200);

        long stillNamingOld = env.su(ctx -> (long) ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(src).and(CHUNKS.COLLECTION.eq(old))));
        assertThat(stillNamingOld).as("P3.7's precondition: the one-row rename leaves the children naming the old label")
            .isEqualTo(chunksBefore);
        Map<String, Long> after = registryIds(env, src);
        assertThat(after).as("the registry row is renamed in place").containsKey(renamedTo).doesNotContainKey(old);
        assertThat(after.get(renamedTo)).as("a rename keeps the collection's id").isEqualTo(before.get(old));

        Map<String, Map<String, Long>> srcRows = rowsByCollection(env, src);
        JsonNode end = env.restore(env.newTenant(dst), env.export(token));

        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");
        Map<String, Long> restored = registryIds(env, dst);
        assertThat(restored.keySet()).as("the renamed label is the restored registry's").isEqualTo(after.keySet());
        Map<String, Map<String, Long>> dstRows = rowsByCollection(env, dst);
        assertThat(without(dstRows)).as("every row that was on the renamed collection is on its restored id")
            .isEqualTo(without(srcRows));
        assertThat(dstRows.get("chunks.collection_id").get(renamedTo)).isEqualTo(chunksBefore);
    }

    // ---- legs after a real one-row rename --------------------------------------------------------------------------------------

    private record Renamed(String tenant, String token, String a, String b, long id) { }

    private static final java.util.concurrent.atomic.AtomicInteger SEQ = new java.util.concurrent.atomic.AtomicInteger();

    private static String label(String owner, int n) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + n;
    }

    /** A fresh tenant holding a fully seeded collection A, renamed through the route to B (one-row: B keeps A's id). */
    private Renamed renamedCollection(String tag) throws Exception {
        int n = SEQ.incrementAndGet();
        String tenant = "t-p37acc-" + tag + n;
        String token = env.newTenant(tenant);
        String a = label("p37acc" + tag + n + "a", 1);
        String b = label("p37acc" + tag + n + "b", 1);
        env.su(ctx -> {
            Rdr229RenameSeeds.full(ctx, tenant, a);
            return null;
        });
        long id = env.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        var r = env.callJson("POST", "/v1/catalog/collections/rename", token, "{\"old_name\":\"" + a + "\",\"new_name\":\"" + b + "\"}");
        assertThat(r.statusCode()).as("the rename: " + r.body()).isEqualTo(200);
        assertThat(env.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b))).as("non-vacuity: one-row, B keeps A's id").isEqualTo(id);
        assertThat(env.<Long>su(ctx -> Rdr229RenameChecks.naming(ctx, tenant, "chunks", "collection", a)))
            .as("non-vacuity: B's chunks still carry the name A").isPositive();
        return new Renamed(tenant, token, a, b, id);
    }

    /**
     * (f) In the one-row regime (latch row present, readiness NONE) a catalog rename through the PUBLIC route against the production-built NexusService and
     * RenameGuard answers 200, not 409 {@code rename_path_mismatch}, and its effect is the one-row path: the same collection_id under the new label. That
     * proves P3.7's {@code OneRowRename} is wired in place of {@code OneRowRename.failClosed()}.
     */
    @Test
    void legF_aCatalogRenameInTheOneRowRegimeAnswers200NotRenamePathMismatch_andKeepsTheId() throws Exception {
        int n = SEQ.incrementAndGet();
        String tenant = "t-p37acc-f" + n;
        String token = env.newTenant(tenant);
        String a = label("p37accf" + n + "a", 1);
        String b = label("p37accf" + n + "b", 1);
        env.su(ctx -> {
            Rdr229RenameSeeds.full(ctx, tenant, a);
            return null;
        });
        long id = env.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));

        var r = env.callJson("POST", "/v1/catalog/collections/rename", token, "{\"old_name\":\"" + a + "\",\"new_name\":\"" + b + "\"}");

        assertThat(r.statusCode()).as("200, not 409 rename_path_mismatch: " + r.body()).isEqualTo(200);
        assertThat(r.body()).doesNotContain("rename_path_mismatch");
        assertThat(env.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b))).as("the one-row path: the same collection_id under the new label").isEqualTo(id);
        // folio's r3 critique F4: the route omits collection_id when the outcome's id is not positive, so a 0 or a wrong id would pass a has() check silently
        var answer = new com.fasterxml.jackson.databind.ObjectMapper().readTree(r.body());
        assertThat(answer.has("collection_id")).as("the answer carries the renamed row's id: " + r.body()).isTrue();
        assertThat(answer.path("collection_id").asLong()).as("and it is the registry's id of the renamed row").isEqualTo(id);
        assertThat(answer.path("ids_stable").asBoolean()).as("one-row: ids survive").isTrue();
    }

    /**
     * (g) A sweep holding the sweep gate under the live label B (the gate's key is the label: {@code sweepgate:<tenant>/<label>}, taken here through
     * the same advisory-lock function the engine takes it with) blocks (i) a rehome naming B and (ii) a rename named B: each waits for the gate and no
     * row moves while it is held; released, it completes. The sweep-gate key moving to collection_id is not P3.7's (linda): no changeset here.
     */
    @Test
    void legG_aSweepHoldingTheGateUnderTheNewLabelBlocksARehomeAndARenameNamingIt() throws Exception {
        var w = renamedCollection("g");
        String other = label("p37accgo" + SEQ.incrementAndGet(), 1);
        String renameTarget = label("p37accgr" + SEQ.incrementAndGet(), 1);
        env.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, w.tenant(), other);
            return null;
        });
        var before = env.su(ctx -> Rdr229RenameChecks.snapshots(ctx, w.tenant(), w.a()));
        var pool = java.util.concurrent.Executors.newFixedThreadPool(3);
        try (java.sql.Connection holder = env.pg.createConnection("")) {
            holder.setAutoCommit(false);
            var ctx = org.jooq.impl.DSL.using(holder, org.jooq.SQLDialect.POSTGRES);
            ctx.select(org.jooq.impl.DSL.function("pg_advisory_xact_lock", Object.class,
                org.jooq.impl.DSL.function("hashtext", Integer.class, org.jooq.impl.DSL.val("sweepgate:" + w.tenant() + "/" + w.b())))).fetch();   // the sweep, EXCLUSIVE
            var rehome = pool.submit(() -> env.callJson("POST", "/v1/catalog/collections/rehome", w.token(),
                "{\"source\":\"" + other + "\",\"target\":\"" + w.b() + "\"}"));
            var rename = pool.submit(() -> env.callJson("POST", "/v1/catalog/collections/rename", w.token(),
                "{\"old_name\":\"" + w.b() + "\",\"new_name\":\"" + renameTarget + "\"}"));

            // both requests are in flight and parked on the gate: two backends wait for an ADVISORY lock (pg_locks), counted, not slept for
            assertThat(awaitAdvisoryWaiters(2)).as("(i) and (ii) both wait on the sweep gate").isTrue();
            assertThat(rehome.isDone()).as("(i) a rehome naming B waits for the sweep").isFalse();
            assertThat(rename.isDone()).as("(ii) a rename named B waits for the sweep").isFalse();
            java.util.Map<String, java.util.List<String>> during = env.su(c -> Rdr229RenameChecks.snapshots(c, w.tenant(), w.a()));
            assertThat(during).as("no row of B moved while the gate is held").isEqualTo(before);
            assertThat(env.<Long>su(c -> Rdr229RenameChecks.registryId(c, w.tenant(), w.b()))).as("B is still B").isEqualTo(w.id());

            holder.commit();   // the sweep ends
            assertThat(rehome.get(60, java.util.concurrent.TimeUnit.SECONDS).statusCode()).as("released, the rehome completes").isIn(200, 409);
            assertThat(rename.get(60, java.util.concurrent.TimeUnit.SECONDS).statusCode()).as("released, the rename completes").isIn(200, 409, 404);
        } finally {
            pool.shutdownNow();
        }
    }

    /** Waits, bounded, until at least {@code n} backends wait for an advisory lock (pg_locks, not granted). */
    private boolean awaitAdvisoryWaiters(int n) throws InterruptedException {
        long deadline = System.nanoTime() + java.time.Duration.ofSeconds(30).toNanos();
        while (System.nanoTime() < deadline) {
            int waiters = env.su(ctx -> ctx.fetchCount(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("pg_catalog", "pg_locks")),
                org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("locktype"), String.class).eq("advisory")
                    .and(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("granted"), Boolean.class).isFalse())));
            if (waiters >= n) {
                return true;
            }
            Thread.sleep(50);
        }
        return false;
    }

    // ---- the shared checks run in the one-row regime (folio's review of f5da5f0025, item 2) ---------------------------------------

    private record Seeded(String tenant, String token, String a, String b) { }

    private Seeded seeded(String tag) throws Exception {
        int n = SEQ.incrementAndGet();
        String tenant = "t-p37acc-" + tag + n;
        String token = env.newTenant(tenant);
        String a = label("p37acc" + tag + n + "a", 1);
        String b = label("p37acc" + tag + n + "b", 1);
        env.su(ctx -> {
            Rdr229RenameSeeds.full(ctx, tenant, a);
            return null;
        });
        return new Seeded(tenant, token, a, b);
    }

    private void assertNotRenamed(Seeded w, String why) {
        assertThat(env.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), w.a()))).as("the source is still registered " + why).isNotNull();
        assertThat(env.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), w.b()))).as("the target was never created " + why).isNull();
    }

    /** (i) A rename is refused while a re-embed job holds the collection, in the one-row regime as on the name path. */
    @Test
    void sharedCheck_aRenameIsRefusedWhileAReembedJobHoldsTheCollection() throws Exception {
        var w = seeded("ri");
        String jobTarget = label("p37accri" + SEQ.incrementAndGet() + "t", 1);
        env.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, w.tenant(), jobTarget);
            return null;
        });
        new dev.nexus.service.db.ReembedJobRepository(new dev.nexus.service.db.TenantScope(env.ds)).start(w.tenant(), w.a(), jobTarget, null, false, false);

        var r = env.callJson("POST", "/v1/catalog/collections/rename", w.token(), "{\"old_name\":\"" + w.a() + "\",\"new_name\":\"" + w.b() + "\"}");

        assertThat(r.statusCode()).as("refused: " + r.body()).isEqualTo(409);
        assertThat(r.body()).contains("cannot rename");
        assertNotRenamed(w, "(a re-embed job holds it)");
    }

    /** (ii) A rename naming another model token is refused (409 collection_model_mismatch), as on the name path. */
    @Test
    void sharedCheck_aRenameOnAModelTokenMismatchIsRefused() throws Exception {
        var w = seeded("rm");

        var r = env.callJson("POST", "/v1/catalog/collections/rename", w.token(),
            "{\"old_name\":\"" + w.a() + "\",\"new_name\":\"" + w.b() + "\",\"embedding_model\":\"bge-base-en-v15-768\"}");

        assertThat(r.statusCode()).as("refused: " + r.body()).isEqualTo(409);
        assertThat(r.body()).contains("collection_model_mismatch");
        assertNotRenamed(w, "(the model token differs)");
    }

    /**
     * (iii) A rename onto a live target is refused, as on the name path. The repository is called directly, wired as the engine wires it (the one-row
     * rename behind a ready RenameGuard, gate 3 held): the route's handler answers a live target with the same text before the repository is reached
     * (folio's r3 review F1), so only a direct call exercises the repository's own check. The exact message is the shared check's: the one-row body's
     * belt ("it became live while this rename ran") would refuse too, so the text tells the two apart.
     */
    @Test
    void sharedCheck_aRenameOntoALiveTargetIsRefused() throws Exception {
        var w = seeded("rl");
        env.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, w.tenant(), w.b());
            return null;
        });
        var repo = new dev.nexus.service.db.CatalogRepository(new dev.nexus.service.db.TenantScope(env.ds), () -> true,
            new dev.nexus.service.db.RenameGuard(dev.nexus.service.datamove.RenameReadiness.ready(), new dev.nexus.service.db.CollectionOneRowRename()));

        assertThatThrownBy(() -> repo.renameCollectionOutcome(w.tenant(), w.a(), w.b(), null, null, null, null))
            .as("the repository's own live-target refusal")
            .isInstanceOf(dev.nexus.service.db.CatalogRepository.CollectionMergeRefused.class)
            .hasMessage("target collection already exists: " + w.b());
        assertThat(env.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), w.a()))).as("the source is still registered").isNotNull();
    }

    /** (h) The aspect-task rewrite in the one-row regime (one claimed and one available task): after the rename the tasks name B and none names A. */
    @Test
    void legH_theQueuedAspectTasksNameTheNewLabelAfterAOneRowRename() throws Exception {
        int n = SEQ.incrementAndGet();
        String tenant = "t-p37acc-h" + n;
        String token = env.newTenant(tenant);
        String a = label("p37acch" + n + "a", 1);
        String b = label("p37acch" + n + "b", 1);
        env.su(ctx -> {
            Rdr229RenameSeeds.full(ctx, tenant, a);
            return null;
        });
        long id = env.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        var tuples = Rdr229RenameChecks.tuples(new dev.nexus.service.db.TenantScope(env.ds));
        tuples.out(tenant, "aspects", Map.of("queue", "aspects"), Map.of(), Rdr229RenameChecks.taskBody(a, "t.1"), "t.1", null);
        tuples.inp(tenant, "aspects", Map.of("queue", "aspects"), "worker-1", 600L).orElseThrow();   // t.1 is claimed
        tuples.out(tenant, "aspects", Map.of("queue", "aspects"), Map.of(), Rdr229RenameChecks.taskBody(a, "t.2"), "t.2", null);   // t.2 is available
        assertThat(env.<Integer>su(ctx -> dev.nexus.service.db.AspectTaskRefs.countNaming(ctx, tenant, a))).as("non-vacuity: two tasks name A").isEqualTo(2);

        var r = env.callJson("POST", "/v1/catalog/collections/rename", token, "{\"old_name\":\"" + a + "\",\"new_name\":\"" + b + "\"}");

        assertThat(r.statusCode()).as("the rename: " + r.body()).isEqualTo(200);
        assertThat(env.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b))).as("non-vacuity: one-row").isEqualTo(id);
        assertThat(env.<Integer>su(ctx -> dev.nexus.service.db.AspectTaskRefs.countNaming(ctx, tenant, b))).as("both tasks, the claimed and the available, name B").isEqualTo(2);
        assertThat(env.<Integer>su(ctx -> dev.nexus.service.db.AspectTaskRefs.countNaming(ctx, tenant, a))).as("none names A").isZero();
    }

    /** (b) A manifest write referencing an existing chunk after the rename commits, and the chunk's orphan row is cleared. */
    @Test
    void legB_aManifestWriteOfAnExistingChunkCommits_andClearsItsOrphanRow() throws Exception {
        var w = renamedCollection("b");
        String chash = Rdr229RenameSeeds.hexChash("rn-doc-2");   // an existing chunk of B (named A), referenced by no manifest row
        env.su(ctx -> {
            ctx.insertInto(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "chunk_orphaned_at")),
                    org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("tenant_id"), String.class), org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("collection"), String.class),
                    org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("chash"), byte[].class), org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("collection_id"), Long.class),
                    org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("orphaned_at"), java.time.OffsetDateTime.class),
                    org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("embedding_model"), String.class))
                .values(w.tenant(), w.a(), java.util.HexFormat.of().parseHex(chash), w.id(), java.time.OffsetDateTime.now(),
                    PgContainerHelper.collectionModel(ctx, w.tenant(), w.b())).execute();
            return null;
        });
        long orphansBefore = env.su(ctx -> (long) ctx.fetchCount(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "chunk_orphaned_at")),
            org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("tenant_id"), String.class).eq(w.tenant())));
        assertThat(orphansBefore).as("non-vacuity: an orphan row for the chunk").isEqualTo(1L);

        // position 0 keeps the chunk the seed put there: replacing it would drop that chunk's only manifest reference and stamp an orphan row of its own
        String kept = env.su(ctx -> java.util.HexFormat.of().formatHex(ctx.select(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("chash"), byte[].class))
            .from(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "catalog_document_chunks")))
            .where(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("tenant_id"), String.class).eq(w.tenant())
                .and(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("doc_id"), String.class).eq("rn-doc-1"))
                .and(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("position"), Integer.class).eq(0)))
            .fetchSingle().value1()));
        var r = env.callJson("POST", "/v1/catalog/manifest/write", w.token(),
            "{\"doc_id\":\"rn-doc-1\",\"collection\":\"" + w.b() + "\",\"rows\":[{\"position\":0,\"chash\":\"" + kept
                + "\"},{\"position\":1,\"chash\":\"" + chash + "\"}]}");

        assertThat(r.statusCode()).as("no 23503: the manifest row of an existing chunk commits: " + r.body()).isEqualTo(200);
        List<String> orphansAfter = env.su(ctx -> ctx.select(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("chash"), byte[].class))
            .from(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "chunk_orphaned_at")))
            .where(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("tenant_id"), String.class).eq(w.tenant()))
            .fetch(0, byte[].class).stream().map(java.util.HexFormat.of()::formatHex).toList());
        assertThat(orphansAfter).as("the orphan row of the now-referenced chunk " + chash + " is cleared, and the write orphaned nothing (kept " + kept + ")")
            .isEmpty();
    }

    /** (c) addendum 9: a delete by the old label A leaves B's chunks, topic_assignments and manifest rows untouched, and deletes nothing. */
    @Test
    void legC_aDeleteByTheOldLabelTouchesNoRowOfTheRenamedCollection() throws Exception {
        var w = renamedCollection("c");
        var before = env.su(ctx -> Rdr229RenameChecks.snapshots(ctx, w.tenant(), w.a()));
        assertThat(before.values().stream().mapToInt(java.util.List::size).sum()).as("non-vacuity: B's rows are there").isPositive();

        var r = env.callJson("POST", "/v1/catalog/collections/delete", w.token(), "{\"name\":\"" + w.a() + "\"}");

        assertThat(r.statusCode()).as("the delete by the retired label answers: " + r.body()).isIn(200, 404);
        var after = env.su(ctx -> Rdr229RenameChecks.snapshots(ctx, w.tenant(), w.a()));
        assertThat(after).as("B's chunks, topic_assignments and manifest rows are the same rows (xmin), none deleted").isEqualTo(before);
        assertThat(env.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), w.b()))).as("and B is still registered").isEqualTo(w.id());
    }

    /**
     * (c2) folio 2026-11-23: deleting the renamed collection B itself, after a real one-row rename A to B, leaves no row under B's id in any child
     * table that the delete purges, although B's rows still carry the name A. The base carries P3.5a slice B's CollectionPurgeScope (replayed as 05305cb5f6
     * and 45f6b8fb29), which resolves the delete to B's id from gate 3 and purges every content table by that id, so the rows that still carry the name A go
     * too. All fourteen tables of the registry are read (folio's r3 confirmation review S3); the four audit tables stay, by design, and are pinned as they were.
     */
    @Test
    void legC2_deletingTheRenamedCollectionLeavesNoRowUnderItsIdInAnyChildTable() throws Exception {
        var w = renamedCollection("c2");
        env.su(ctx -> {
            Rdr229RenameSeeds.ring(ctx, w.tenant(), w.b());   // the seeds file no ring row of B's: its id is the registry's, the name B is the current one
            return null;
        });
        env.su(ctx -> ctx.insertInto(dev.nexus.service.jooq.nexus.Tables.GC_AUDIT)
            .set(dev.nexus.service.jooq.nexus.Tables.GC_AUDIT.TENANT_ID, w.tenant())
            .set(dev.nexus.service.jooq.nexus.Tables.GC_AUDIT.OPERATION, "p37_legc2")
            .set(dev.nexus.service.jooq.nexus.Tables.GC_AUDIT.COLLECTION, w.a())      // the name it was written under, before the rename
            .set(dev.nexus.service.jooq.nexus.Tables.GC_AUDIT.COLLECTION_ID, w.id())
            .set(dev.nexus.service.jooq.nexus.Tables.GC_AUDIT.ACTOR, "test")
            .set(dev.nexus.service.jooq.nexus.Tables.GC_AUDIT.DRY_RUN, false)
            .set(dev.nexus.service.jooq.nexus.Tables.GC_AUDIT.CHASH_COUNT, 0).execute());   // the seeds file no audit row of gc_audit
        var seeded = childRowsUnderId(w.tenant(), w.id());
        assertThat(seeded).as("non-vacuity: every collection-scoped table, all of them read from the registry, holds a row under B's id")
            .hasSize(CollectionScopedTableSites.all().size()).allSatisfy((table, n) -> assertThat(n).as(table).isPositive());

        var r = env.callJson("POST", "/v1/catalog/collections/delete", w.token(), "{\"name\":\"" + w.b() + "\"}");

        assertThat(r.statusCode()).as("the delete of B answers: " + r.body()).isEqualTo(200);
        var left = childRowsUnderId(w.tenant(), w.id());
        // The four audit tables are the delete's to leave, by design and on the base's name path too: CollectionPurgeScope purges the content tables and the
        // collection's registry row (the ring goes with it, by its foreign key), and the audit rows stay as the record of what happened to the collection
        // (gc_audit's own reason, and AUDIT_ONLY_TABLES' "an audit breadcrumb, no data"). They are read, so a change in that is a visible decision.
        var auditLeftByDesign = java.util.Set.of("relevance_log", "search_telemetry", "hook_failures", "gc_audit");
        assertThat(left.keySet().stream().filter(t -> !auditLeftByDesign.contains(t)).toList())
            .as("non-vacuity: the content tables are the ten of the fourteen the delete purges").hasSize(CollectionScopedTableSites.all().size() - auditLeftByDesign.size());
        assertThat(left).as("no row is left under B's id in any content table, whatever name it carries: " + left)
            .allSatisfy((table, n) -> {
                if (auditLeftByDesign.contains(table)) {
                    assertThat(n).as(table + " (audit, left by the delete on purpose)").isEqualTo(seeded.get(table));
                } else {
                    assertThat(n).as(table).isZero();
                }
            });
    }

    /**
     * Rows per table of {@code CatalogRepository.COLLECTION_SCOPED_TABLES} (every one, read through {@link CollectionScopedTableSites}, not a copy of the list)
     * whose id column is {@code id}, whatever the table's name column says (folio's r3 confirmation review S3: the nine of
     * {@code Rdr229RenameChecks.CHILD_SITES} left five out).
     */
    private Map<String, Long> childRowsUnderId(String tenant, long id) {
        return env.su(ctx -> {
            Map<String, Long> out = new TreeMap<>();
            for (var s : CollectionScopedTableSites.all()) {
                out.put(s.table(), ctx.selectCount().from(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", s.table())))
                    .where(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("tenant_id"), String.class).eq(tenant))
                    .and(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name(s.idColumn()), Long.class).eq(id))
                    .fetchOne(0, Long.class));
            }
            return out;
        });
    }

    /** (d) each per-store rename route, called with A to B after a real one-row rename: success, zero rows changed, rows unchanged (the no-op is P3.5a slice C's). */
    @Test
    void legD_theFivePerStoreRenameRoutesAreANoOpAfterAOneRowRename() throws Exception {
        var w = renamedCollection("d");
        var before = env.su(ctx -> Rdr229RenameChecks.snapshots(ctx, w.tenant(), w.a()));
        for (String path : java.util.List.of("/v1/chash/rename_collection", "/v1/taxonomy/rename_collection", "/v1/telemetry/rename_collection",
                "/v1/aspects/rename_collection", "/v1/aspects/highlights/rename_collection")) {
            var r = env.callJson("POST", path, w.token(), "{\"old\":\"" + w.a() + "\",\"new\":\"" + w.b() + "\"}");
            assertThat(r.statusCode()).as(path + ": " + r.body()).isEqualTo(200);
            assertThat(r.body()).as(path + " changes no row").doesNotContainPattern("\"updated\":[1-9]");
        }
        java.util.Map<String, java.util.List<String>> after = env.su(ctx -> Rdr229RenameChecks.snapshots(ctx, w.tenant(), w.a()));
        assertThat(after).as("the rows are the same rows (xmin)").isEqualTo(before);
    }

    /** (e)(i) a rehome naming B moves exactly B's rows, by id, and none of another collection's. */
    @Test
    void legE_aRehomeNamingTheNewLabelMovesExactlyThatCollectionsRows() throws Exception {
        var w = renamedCollection("e1");
        String decoy = label("p37acce1d" + SEQ.incrementAndGet(), 1);
        String target = label("p37acce1t" + SEQ.incrementAndGet(), 1);
        env.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, w.tenant(), decoy);
            PgContainerHelper.insertCollection(ctx, w.tenant(), target);
            return null;
        });
        long decoyId = env.su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), decoy));
        long targetId = env.su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), target));
        long chunksOfB = env.su(ctx -> (long) ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.CHUNKS,
            dev.nexus.service.jooq.nexus.Tables.CHUNKS.TENANT_ID.eq(w.tenant()).and(dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION_ID.eq(w.id()))));
        long decoyChunks = env.su(ctx -> (long) ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.CHUNKS,
            dev.nexus.service.jooq.nexus.Tables.CHUNKS.TENANT_ID.eq(w.tenant()).and(dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION_ID.eq(decoyId))));

        var r = env.callJson("POST", "/v1/catalog/collections/rehome", w.token(), "{\"source\":\"" + w.b() + "\",\"target\":\"" + target + "\"}");

        assertThat(r.statusCode()).as("the rehome: " + r.body()).isEqualTo(200);
        assertThat(env.<Long>su(ctx -> (long) ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.CHUNKS,
            dev.nexus.service.jooq.nexus.Tables.CHUNKS.TENANT_ID.eq(w.tenant()).and(dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION_ID.eq(targetId)))))
            .as("exactly B's chunks moved onto the target").isEqualTo(chunksOfB);
        assertThat(env.<Long>su(ctx -> (long) ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.CHUNKS,
            dev.nexus.service.jooq.nexus.Tables.CHUNKS.TENANT_ID.eq(w.tenant()).and(dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION_ID.eq(decoyId)))))
            .as("none of another collection's").isEqualTo(decoyChunks);
    }

    /** (e)(ii) a rehome onto the renamed target keeps one registry row for it and its id. */
    @Test
    void legE_aRehomeOntoTheRenamedTargetKeepsOneRegistryRowAndItsId() throws Exception {
        var w = renamedCollection("e2");
        String source = label("p37acce2s" + SEQ.incrementAndGet(), 1);
        env.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, w.tenant(), source);
            return null;
        });

        var r = env.callJson("POST", "/v1/catalog/collections/rehome", w.token(), "{\"source\":\"" + source + "\",\"target\":\"" + w.b() + "\"}");

        assertThat(r.statusCode()).as("the rehome: " + r.body()).isEqualTo(200);
        long rowsNamedB = env.su(ctx -> (long) ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS,
            dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(w.tenant()).and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME.eq(w.b()))));
        assertThat(rowsNamedB).as("one registry row for B, no second").isEqualTo(1L);
        assertThat(env.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), w.b()))).as("B's id is unchanged").isEqualTo(w.id());
    }

    /**
     * The whole-tenant restore is exempt from the reservation of labels ({@code LabelReserved}) because it only loads into an EMPTY tenant: into one that holds a
     * row it refuses ("tenant not empty"), so it can never meet a stale row.
     */
    @Test
    void theWholeTenantRestoreRefusesATenantThatAlreadyHoldsData_soItIsExemptFromTheLabelReservation() throws Exception {
        String src = "t-byid-p37-ne-src", dst = "t-byid-p37-ne-dst";
        String token = ByIdRestoreSupport.batterySource(env, src);
        String dstToken = env.newTenant(dst);
        env.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, dst, label("p37accnonempty", 1));
            return null;
        });

        var open = env.open(dstToken, env.export(token));

        assertThat(open.statusCode()).as("a restore into a tenant with data is refused at open: " + open.body()).isEqualTo(409);
        assertThat(open.body()).as("it names why").contains("tenant_not_empty").containsIgnoringCase("empty tenant only");
    }

    private static Map<String, Map<String, Long>> without(Map<String, Map<String, Long>> by) {
        Map<String, Map<String, Long>> out = new LinkedHashMap<>();
        by.forEach((site, m) -> {
            Map<String, Long> c = new TreeMap<>(m);
            c.remove(NO_REGISTRY_ROW);
            out.put(site, c);
        });
        return out;
    }
}
