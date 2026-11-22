// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.ByIdRestoreSupport.bigint;
import static dev.nexus.service.ByIdRestoreSupport.editRows;
import static dev.nexus.service.ByIdRestoreSupport.registryIds;
import static dev.nexus.service.ByIdRestoreSupport.rowsByCollection;
import static dev.nexus.service.ByIdRestoreSupport.withField;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * RDR-229 P3.11 (nexus-p2m1x.31): a restore maps a file's collection ids by id, not by name. From P3.7 a rename is one registry
 * row's update and the referencing tables' name columns are stale until P4.1, so a backup taken then holds rows whose name is a
 * label that no longer names their collection, or names another one. Restoring by name refuses such a file
 * ({@code restore_orphan_collection}) or files the renamed collection's rows under the collection that took its old label.
 *
 * <p>This class's database is in P3.7's state: every foreign key that references {@code catalog_collections} is dropped (the
 * name-key drop move), so a registry row can be renamed with {@code UPDATE} and its children keep the old label. That is the
 * only way to build the state until P3.7 ships its rename, and why the class has its own container. Every table's rows are
 * real; nothing is mocked.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229RestoreByIdTest {

    private ImportTestEnv env;

    @BeforeAll
    void startAll() throws Exception {
        env = ImportTestEnv.start();
        List<String> dropped = ByIdRestoreSupport.dropRegistryNameKeys(env);
        assertThat(dropped).as("non-vacuity: the name keys that P3.7's rename would trip on").isNotEmpty();
    }

    @AfterAll
    void stopAll() {
        if (env != null) env.close();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────

    private static String status(JsonNode end) {
        return end.get("status").asText();
    }

    /** The label after the rename; no foreign key refuses it, the children keep the old name. */
    private static String renamed(String tenant) {
        return "knowledge__" + tenant.replace("-", "") + "renamed768__" + ByIdRestoreSupport.BGE + "__v1";
    }

    /**
     * A source in P3.7's state: the battery seed, then the 768-wide collection A renamed to B by one registry row's update, its
     * children (and the history rows and the tombstone's {@code superseded_by}) still naming A. Returns the token.
     */
    private String renamedSource(String tenant) throws Exception {
        String token = ByIdRestoreSupport.batterySource(env, tenant);
        String a = TenantExportBatteryTest.Seed.collection(tenant, 768);
        int renamed = env.su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, renamed(tenant))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(a)).execute());
        assertThat(renamed).as("the one-row rename").isEqualTo(1);
        long stillNamingA = env.su(ctx -> (long) ctx.fetchCount(DSL.table(DSL.name("nexus", "chunks")),
            DSL.field(DSL.name("tenant_id"), String.class).eq(tenant), DSL.field(DSL.name("collection"), String.class).eq(a)));
        assertThat(stillNamingA).as("non-vacuity: the children still name the old label").isPositive();
        return token;
    }

    private static Map<String, Map<String, Long>> without(Map<String, Map<String, Long>> by, String key) {
        Map<String, Map<String, Long>> out = new java.util.LinkedHashMap<>();
        by.forEach((site, m) -> {
            Map<String, Long> c = new java.util.TreeMap<>(m);
            c.remove(key);
            out.put(site, c);
        });
        return out;
    }

    // ── T1: the renamed collection and a new one that took its label ────────────────────────────

    @Test
    void t1_aRenamedCollectionsRowsStayOnItWhenAnotherCollectionTookTheOldLabel() throws Exception {
        String src = "t-byid-t1-src", dst = "t-byid-t1-dst";
        String token = renamedSource(src);
        String a = TenantExportBatteryTest.Seed.collection(src, 768), b = renamed(src);
        // a new collection registered under the old label, with its own rows
        env.su(ctx -> {
            ByIdRestoreSupport.seedCollectionTakingTheLabel(ctx, src, a);
            return null;
        });
        Map<String, Long> srcIds = registryIds(env, src);
        assertThat(srcIds).as("the source registry: the renamed collection and the new one that holds its old label")
            .containsKeys(a, b).hasSize(4);
        assertThat(srcIds.get(a)).isNotEqualTo(srcIds.get(b));
        // origin tags by id: one chunk of each collection, both tagged with the same label and their own ids
        String bChunk = env.su(ctx -> ByIdRestoreSupport.tagOrigin(ctx, src, srcIds.get(b), a, srcIds.get(b)));
        String aChunk = env.su(ctx -> ByIdRestoreSupport.tagOrigin(ctx, src, srcIds.get(a), a, srcIds.get(a)));

        Map<String, Map<String, Long>> srcRows = rowsByCollection(env, src);
        assertThat(ByIdRestoreSupport.sitesWithoutRowsFor(srcRows, b)).as("non-vacuity: every exported site holds rows of the renamed collection").isEmpty();
        List<String> newSites = new ArrayList<>(List.of("chunks.collection_id", "catalog_documents.physical_collection_id",
            "catalog_document_chunks.collection_id", "topics.collection_id", "document_aspects.collection_id",
            "relevance_log.collection_id"));
        Map<String, Map<String, Long>> seeded = new java.util.LinkedHashMap<>(srcRows);
        seeded.keySet().retainAll(newSites);
        assertThat(ByIdRestoreSupport.sitesWithoutRowsFor(seeded, a)).as("non-vacuity: the new collection has rows at the sites seeded for it").isEmpty();
        assertThat(seeded.keySet()).as("the sites seeded for the new collection").containsExactlyInAnyOrderElementsOf(newSites);
        Map<String, Set<String>> srcChunks = ByIdRestoreSupport.chunkHashesByCollection(env, src);
        assertThat(srcChunks.get(a)).as("the new collection's chunks").hasSize(3);
        assertThat(srcChunks.get(b)).as("the renamed collection's chunks").hasSizeGreaterThan(3);

        ImportTestEnv.BackupFile backup = env.export(token);
        JsonNode end = env.restore(env.newTenant(dst), backup);

        assertThat(status(end)).as("a file taken in P3.7's state is not refused: " + end).isEqualTo("committed");
        Map<String, Long> dstIds = registryIds(env, dst);
        assertThat(dstIds.keySet()).as("the registry's labels are kept").isEqualTo(srcIds.keySet());
        assertThat(dstIds.values()).doesNotContainNull().doesNotContainAnyElementsOf(srcIds.values());
        Map<String, Map<String, Long>> dstRows = rowsByCollection(env, dst);
        assertThat(without(dstRows, ByIdRestoreSupport.NO_REGISTRY_ROW))
            .as("per exported site, the rows each registry row holds are the source's: nothing of the renamed collection moved to the one that took its label")
            .isEqualTo(without(srcRows, ByIdRestoreSupport.NO_REGISTRY_ROW));
        dstRows.forEach((site, m) -> {
            if (!site.startsWith("relevance_log") && !site.startsWith("search_telemetry")) {
                assertThat(m).as("%s names no registry row it should not", site).doesNotContainKey(ByIdRestoreSupport.NO_REGISTRY_ROW);
            }
        });
        assertThat(ByIdRestoreSupport.namedRowsWithoutId(env, dst)).as("named rows that restored with no id").isEmpty();
        assertThat(ByIdRestoreSupport.chunkHashesByCollection(env, dst))
            .as("the chunks each collection holds, by chash: none of the renamed collection's is on the one with its old label")
            .isEqualTo(srcChunks);
        // the tombstone's successor id is the renamed collection's restored id, not the new collection's whose label it still names
        Long tombstoneSuccessor = env.su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(dst)).and(CATALOG_COLLECTIONS.NAME.eq(TenantExportBatteryTest.Seed.collection(src, 1024)))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID));
        assertThat(tombstoneSuccessor).as("the tombstone follows its successor's id through the rename").isEqualTo(dstIds.get(b));
        // the quarantine origin tag is mapped by id: both tags name the same label, each ends on its own collection's id
        assertThat(ByIdRestoreSupport.originIdOf(env, dst, bChunk)).as("the renamed collection's chunk's origin id")
            .isEqualTo(String.valueOf(dstIds.get(b)));
        assertThat(ByIdRestoreSupport.originIdOf(env, dst, aChunk)).as("the new collection's chunk's origin id")
            .isEqualTo(String.valueOf(dstIds.get(a)));
    }

    // ── T2: the same state with no new collection ───────────────────────────────────────────────

    @Test
    void t2_aBackupWhoseChildrenNameALabelNoRegistryRowHoldsRestoresOnTheRenamedCollection() throws Exception {
        String src = "t-byid-t2-src", dst = "t-byid-t2-dst";
        String token = renamedSource(src);
        String a = TenantExportBatteryTest.Seed.collection(src, 768), b = renamed(src);
        assertThat(registryIds(env, src)).as("no registry row holds the old label").doesNotContainKey(a).containsKey(b);
        Map<String, Map<String, Long>> srcRows = rowsByCollection(env, src);
        assertThat(ByIdRestoreSupport.sitesWithoutRowsFor(srcRows, b)).as("non-vacuity: every exported site holds rows of the renamed collection").isEmpty();

        JsonNode end = env.restore(env.newTenant(dst), env.export(token));

        assertThat(status(end)).as("by name this file is restore_orphan_collection: " + end).isEqualTo("committed");
        assertThat(registryIds(env, dst).keySet()).isEqualTo(registryIds(env, src).keySet());
        Map<String, Map<String, Long>> dstRows = rowsByCollection(env, dst);
        assertThat(without(dstRows, ByIdRestoreSupport.NO_REGISTRY_ROW)).isEqualTo(without(srcRows, ByIdRestoreSupport.NO_REGISTRY_ROW));
        assertThat(ByIdRestoreSupport.namedRowsWithoutId(env, dst)).isEmpty();
        assertThat(ByIdRestoreSupport.chunkHashesByCollection(env, dst)).isEqualTo(ByIdRestoreSupport.chunkHashesByCollection(env, src));
    }

    // ── T3: a twin that names no registry row of the file ───────────────────────────────────────

    @Test
    void t3_aTwinThatNamesNoRegistryRowOfTheFileIsRefusedBeforeAnyWrite() throws Exception {
        String src = "t-byid-t3-src", dst = "t-byid-t3-dst";
        ImportTestEnv.BackupFile file = env.export(ByIdRestoreSupport.batterySource(env, src));
        int idAt = file.columnIndex("chunks", "collection_id");
        int[] edited = {0};
        ImportTestEnv.BackupFile orphaned = editRows(file, "chunks", rows -> rows.stream().map(r -> {
            if (edited[0]++ < 2) return withField(r, idAt, bigint(987654321L));
            return r;
        }).toList());

        JsonNode end = env.restore(env.newTenant(dst), orphaned);

        assertThat(status(end)).startsWith("failed: restore_orphan_collection_id").contains("chunks.collection_id")
            .contains("987654321").containsPattern("chunks\\.collection_id: \\d+ row");
        assertThat(env.count("catalog_collections", dst)).as("the refusal writes nothing").isZero();
        assertThat(env.count("chunks", dst)).isZero();
        assertThat(env.count("catalog_documents", dst)).isZero();
    }

    @Test
    void t3b_aHistoryRowWhoseIdIsUnmappedKeepsItsLabelAndLosesTheId() throws Exception {
        String src = "t-byid-t3b-src", dst = "t-byid-t3b-dst";
        ImportTestEnv.BackupFile file = env.export(ByIdRestoreSupport.batterySource(env, src));
        int idAt = file.columnIndex("relevance_log", "collection_id");
        long logRows = file.rows("relevance_log");
        assertThat(logRows).as("non-vacuity: the file holds relevance_log rows").isPositive();
        ImportTestEnv.BackupFile unmapped = editRows(file, "relevance_log",
            rows -> rows.stream().map(r -> withField(r, idAt, bigint(987654321L))).toList());

        JsonNode end = env.restore(env.newTenant(dst), unmapped);

        assertThat(status(end)).as("a history table has no CHECK, so its unmapped id is dropped, not refused: " + end).isEqualTo("committed");
        assertThat(env.count("relevance_log", dst)).isEqualTo(logRows);
        Field<Long> id = DSL.field(DSL.name("collection_id"), Long.class);
        long withId = env.su(ctx -> (long) ctx.fetchCount(DSL.table(DSL.name("nexus", "relevance_log")),
            DSL.field(DSL.name("tenant_id"), String.class).eq(dst), id.isNotNull()));
        assertThat(withId).as("no relevance_log row keeps an id that no restored registry row holds").isZero();
        assertThat(rowsByCollection(env, dst).get("chunks.collection_id")).as("the other tables are mapped as before")
            .isEqualTo(rowsByCollection(env, src).get("chunks.collection_id"));
    }

    // ── T4: a file with some twins still NULL ───────────────────────────────────────────────────

    /**
     * A post-P3.1 file in P3.7's state whose 384-wide collection (never renamed) has rows with a NULL twin, as a row stamped
     * late is: those rows are filled by name (their name is valid, a null twin exists only before P3.7), and every other row,
     * the renamed collection's included, by id.
     */
    @Test
    void t4_aFileWithSomeNullTwinsFillsThoseRowsByNameAndTheRestById() throws Exception {
        String src = "t-byid-t4-src", dst = "t-byid-t4-dst";
        String small = TenantExportBatteryTest.Seed.collection(src, 384);
        ImportTestEnv.BackupFile file = env.export(renamedSource(src));
        ImportTestEnv.BackupFile late = file;
        Map<String, Integer> nulled = new java.util.LinkedHashMap<>();
        for (String table : List.of("chunks", "taxonomy_meta", "taxonomy_centroids")) {
            int nameAt = file.columnIndex(table, "collection"), idAt = file.columnIndex(table, "collection_id");
            int[] n = {0};
            late = editRows(late, table, rows -> rows.stream().map(r -> {
                if (!small.equals(ByIdRestoreSupport.text(r, nameAt))) return r;
                n[0]++;
                return withField(r, idAt, null);
            }).toList());
            nulled.put(table, n[0]);
        }
        assertThat(nulled.values()).as("non-vacuity: rows of the 384-wide collection were nulled in each table " + nulled)
            .allMatch(n -> n > 0);

        JsonNode end = env.restore(env.newTenant(dst), late);

        assertThat(status(end)).as(end.toString()).isEqualTo("committed");
        assertThat(ByIdRestoreSupport.namedRowsWithoutId(env, dst)).as("every late-stamp row got its id by name").isEmpty();
        assertThat(without(rowsByCollection(env, dst), ByIdRestoreSupport.NO_REGISTRY_ROW))
            .isEqualTo(without(rowsByCollection(env, src), ByIdRestoreSupport.NO_REGISTRY_ROW));
        assertThat(ByIdRestoreSupport.chunkHashesByCollection(env, dst)).isEqualTo(ByIdRestoreSupport.chunkHashesByCollection(env, src));
    }
}
