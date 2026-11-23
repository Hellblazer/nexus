// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.CollectionIds;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), the one-row rename, from the bead's "Failing tests first". One booted engine for the class, taken
 * through {@link Rdr229RenameRestartPrecondition} (boot, the restart signal, restart, readiness NONE) because the store is fresh, so
 * the stamp moves and the name-key drop move have run for real and P3.5b's changesets have applied. Gate 3's one input is simulated
 * ({@link Rdr229RenameEngine}). Every rename goes through the route; every row is seeded and read as the superuser.
 *
 * <p>Each test asserts first that the rename answered 200 and then that it was a one-row update: the registry row keeps its
 * {@code collection_id} under the new label (the name path inserts a new row, so a new id), and no row of a child table is
 * rewritten (same name, same id, same xmin). The name path passes the first and fails the second, so these are red today for
 * the rename's behaviour. When the precondition was not met the description says so.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37OneRowRenameTest {

    private Rdr229RenameEngine engine;
    private Rdr229RenameRestartPrecondition.Reached pre;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine();
        pre = Rdr229RenameRestartPrecondition.reach(engine).reached();
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    private String why() {
        return pre.suffix();
    }

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    /** The rename answered 200 and the registry row was updated in place: same id under the new label, no row under the old one. */
    private void assertRenamedInPlace(String tenant, String oldName, String newName, long idBefore,
                                      Rdr229RenameEngine.Renamed r) {
        assertThat(r.status()).as("the rename route: " + r + why()).isEqualTo(200);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, newName)))
            .as("a one-row rename updates the registry row in place: the collection keeps its id under the new label" + why())
            .isEqualTo(idBefore);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName)))
            .as("and no row is left under the old label (no tombstone, no second row)" + why()).isNull();
    }

    // ---- (a) ---------------------------------------------------------------------------------------------------------------

    @Test
    void aOneRowRename_leavesChunkManifestAndTaxonomyRowsAndThePerCollectionIndexInPlace_andAQueuedTaskNamesTheNewLabel()
            throws Exception {
        String tenant = "p37-a";
        String token = engine.newTenant(tenant);
        String oldName = coll("p37a", 1);
        String newName = coll("p37a", 2);
        engine.su(ctx -> {
            Rdr229RenameSeeds.full(ctx, tenant, oldName);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));
        // the per-collection index P3.4 builds: a partial index on the collection's id
        String index = "rdr229_p37a_chunks_by_id";
        engine.su(ctx -> ctx.createIndex(index).on(CHUNKS, CHUNKS.CHASH)
            .where(DSL.field(DSL.name("collection_id"), Long.class).eq(id)).execute());
        var indexBefore = engine.su(ctx -> Rdr229RenameChecks.index(ctx, index));
        assertThat(indexBefore).as("non-vacuity: the per-collection index exists before the rename").hasSize(1);
        var before = engine.su(ctx -> Rdr229RenameChecks.snapshots(ctx, tenant, oldName));
        assertThat(before.get("chunks")).as("non-vacuity: chunks to leave alone").hasSize(4);
        assertThat(before.get("catalog_document_chunks")).as("non-vacuity: a manifest row").hasSize(1);
        assertThat(before.get("topics")).as("non-vacuity: a taxonomy row").isNotEmpty();
        assertThat(before.get("taxonomy_centroids")).as("non-vacuity: centroids").hasSize(3);
        // a task queued before the rename: one available, one claimed
        var tuples = Rdr229RenameChecks.tuples(engine.scope());
        tuples.out(tenant, "aspects", Map.of("queue", "aspects"), Map.of(), Rdr229RenameChecks.taskBody(oldName, "t.1"), "t.1", null);
        tuples.inp(tenant, "aspects", Map.of("queue", "aspects"), "worker-1", 600L).orElseThrow();
        tuples.out(tenant, "aspects", Map.of("queue", "aspects"), Map.of(), Rdr229RenameChecks.taskBody(oldName, "t.2"), "t.2", null);
        assertThat(engine.<Map<String, String>>su(ctx -> Rdr229RenameChecks.taskCollections(ctx, tenant)).values())
            .as("non-vacuity: two tasks name the old label").containsExactly(oldName, oldName);

        var r = engine.rename(token, oldName, newName);

        assertRenamedInPlace(tenant, oldName, newName, id, r);
        assertThat(engine.<java.util.Map<String, java.util.List<String>>>su(ctx -> Rdr229RenameChecks.snapshots(ctx, tenant, oldName)))
            .as("chunk, manifest and taxonomy rows are not rewritten: same name, same id, same xmin" + why()).isEqualTo(before);
        assertThat(engine.<java.util.List<String>>su(ctx -> Rdr229RenameChecks.index(ctx, index)))
            .as("the per-collection index is the same index with the same predicate: not dropped, not rebuilt" + why())
            .isEqualTo(indexBefore);
        assertThat(engine.<Map<String, String>>su(ctx -> Rdr229RenameChecks.taskCollections(ctx, tenant)).values())
            .as("queued aspect tasks are still rewritten: both name the new label" + why()).containsExactly(newName, newName);
    }

    // ---- (b) ---------------------------------------------------------------------------------------------------------------

    @Test
    void aOneRowRename_succeedsOnACollectionWithChunksAndCanaryQueryRingRows() throws Exception {
        String tenant = "p37-b1";
        String token = engine.newTenant(tenant);
        String oldName = coll("p37b1", 1);
        String newName = coll("p37b1", 2);
        engine.su(ctx -> {
            Rdr229RenameSeeds.chunksAndRing(ctx, tenant, oldName);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));
        var before = engine.su(ctx -> Rdr229RenameChecks.snapshots(ctx, tenant, oldName));
        assertThat(before.get("chunks")).as("non-vacuity: chunks").hasSize(2);
        assertThat(before.get("canary_query_ring")).as("non-vacuity: a canary_query_ring row").hasSize(1);

        var r = engine.rename(token, oldName, newName);

        assertRenamedInPlace(tenant, oldName, newName, id, r);
        assertThat(engine.<java.util.Map<String, java.util.List<String>>>su(ctx -> Rdr229RenameChecks.snapshots(ctx, tenant, oldName)))
            .as("the chunks and the ring row are left as they were" + why()).isEqualTo(before);
        assertThat(engine.<java.util.List<String>>su(Rdr229RenameChecks::nameKeysPresent))
            .as("the name-key drop move dropped the seven name keys that refuse a one-row rename" + why()).isEmpty();
    }

    @Test
    void aOneRowRename_succeedsOnACollectionWithTopicAssignmentsAspectsTopicsTaxonomyMetaAndHighlights() throws Exception {
        String tenant = "p37-b2";
        String token = engine.newTenant(tenant);
        String oldName = coll("p37b2", 1);
        String newName = coll("p37b2", 2);
        engine.su(ctx -> {
            Rdr229RenameSeeds.full(ctx, tenant, oldName);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));
        var before = engine.su(ctx -> Rdr229RenameChecks.snapshots(ctx, tenant, oldName));
        for (String table : List.of("topic_assignments", "document_aspects", "topics", "taxonomy_meta", "document_highlights")) {
            assertThat(before.get(table)).as("non-vacuity: rows in " + table).isNotEmpty();
        }

        var r = engine.rename(token, oldName, newName);

        assertRenamedInPlace(tenant, oldName, newName, id, r);
        assertThat(engine.<java.util.Map<String, java.util.List<String>>>su(ctx -> Rdr229RenameChecks.snapshots(ctx, tenant, oldName)))
            .as("none of the five FK-bearing tables' rows is rewritten" + why()).isEqualTo(before);
    }

    // ---- (c) ---------------------------------------------------------------------------------------------------------------

    @Test
    void aOneRowRenameOfAFileRoutedCollectionToAPrefixFreeLabel_keepsTheChromaResidueOnItsOldSegment() throws Exception {
        String tenant = "p37-c";
        String token = engine.newTenant(tenant);
        String oldName = "docs__p37c__minilm-l6-v2-384__v1";
        String newName = coll("p37c", 1);
        String residue = "chroma://" + oldName + "/docs/a.md";
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, oldName);
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                    CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CATALOG_DOCUMENTS.SOURCE_URI)
                .values(tenant, "p37c-doc-1", "Doc 1", oldName, CollectionIds.require(ctx, tenant, oldName), residue).execute();
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));
        assertThat(sourceUri(tenant)).as("non-vacuity: the residue is seeded").isEqualTo(residue);

        var r = engine.rename(token, oldName, newName, Map.of("content_type", "knowledge"));

        assertRenamedInPlace(tenant, oldName, newName, id, r);
        assertThat(sourceUri(tenant)).as("the chroma:// residue stays on its old segment: it is not a note" + why()).isEqualTo(residue);
    }

    private String sourceUri(String tenant) {
        return engine.su(ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("p37c-doc-1")).fetchOne(0, String.class));
    }
}
