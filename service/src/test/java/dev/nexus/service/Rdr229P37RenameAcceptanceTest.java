// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.ByIdRestoreSupport.NO_REGISTRY_ROW;
import static dev.nexus.service.ByIdRestoreSupport.registryIds;
import static dev.nexus.service.ByIdRestoreSupport.rowsByCollection;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.LinkedHashMap;
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

    /** (b) A manifest write referencing an existing chunk after the rename commits, and the chunk's orphan row is cleared. */
    @Test
    void legB_aManifestWriteOfAnExistingChunkCommits_andClearsItsOrphanRow() throws Exception {
        var w = renamedCollection("b");
        String chash = Rdr229RenameSeeds.hexChash("rn-doc-2");   // an existing chunk of B (named A), referenced by no manifest row
        env.su(ctx -> {
            ctx.insertInto(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "chunk_orphaned_at")),
                    org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("tenant_id"), String.class), org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("collection"), String.class),
                    org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("chash"), byte[].class), org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("collection_id"), Long.class),
                    org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("orphaned_at"), java.time.OffsetDateTime.class))
                .values(w.tenant(), w.a(), java.util.HexFormat.of().parseHex(chash), w.id(), java.time.OffsetDateTime.now()).execute();
            return null;
        });
        long orphansBefore = env.su(ctx -> (long) ctx.fetchCount(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "chunk_orphaned_at")),
            org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("tenant_id"), String.class).eq(w.tenant())));
        assertThat(orphansBefore).as("non-vacuity: an orphan row for the chunk").isEqualTo(1L);

        var r = env.callJson("POST", "/v1/catalog/manifest/write", w.token(),
            "{\"doc_id\":\"rn-doc-1\",\"collection\":\"" + w.b() + "\",\"rows\":[{\"position\":0,\"chash\":\"" + Rdr229RenameSeeds.hexChash("rn-doc-1")
                + "\"},{\"position\":1,\"chash\":\"" + chash + "\"}]}");

        assertThat(r.statusCode()).as("no 23503: the manifest row of an existing chunk commits: " + r.body()).isEqualTo(200);
        long orphansAfter = env.su(ctx -> (long) ctx.fetchCount(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "chunk_orphaned_at")),
            org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("tenant_id"), String.class).eq(w.tenant())));
        assertThat(orphansAfter).as("the orphan row of the now-referenced chunk is cleared").isZero();
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

    /** (d) each per-store rename route, called with A to B after a real one-row rename: success, zero rows changed, rows unchanged (the no-op is P3.5a slice C's). */
    @Disabled("waits for P3.5a slice C per-store no-op")
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
        assertThat(env.su(ctx -> Rdr229RenameChecks.snapshots(ctx, w.tenant(), w.a()))).as("the rows are the same rows (xmin)").isEqualTo(before);
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
