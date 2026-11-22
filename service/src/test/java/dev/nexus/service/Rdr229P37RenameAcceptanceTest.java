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
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.TreeMap;

/**
 * RDR-229 P3.7's acceptance for the backup (nexus-p2m1x.27), written with the by-id restore (nexus-p2m1x.31) and red until
 * P3.7's one-row rename exists: a rename through the engine's own route must update the registry row and nothing else, so a
 * tenant backed up after it holds children that still name the old label, and its restore must keep every one of them on the
 * renamed collection. Today the route still rewrites the children's name columns, so the precondition below fails; that is the
 * failure this test is expected to show until P3.7 lands, not a defect of the by-id restore (the simulated P3.7 state is
 * {@link Rdr229RestoreByIdTest}'s, which is green).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37RenameAcceptanceTest {

    private ImportTestEnv env;

    @BeforeAll
    void startAll() throws Exception {
        env = ImportTestEnv.start();
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
