// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_1024;
import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_384;
import static dev.nexus.service.jooq.nexus.Tables.ASSIGN_FROM_CHASHES_768;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.QUARANTINE_RESTORE_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.DSLContext;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.OffsetDateTime;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), vectors-079: each plpgsql writer of a child row that P3.1's writer census lists for
 * catalog_document_chunks, chunk_orphaned_at and topic_assignments, run after a one-row rename ({@link Rdr229ChildNameWorld}):
 * stamp_chunks_on_manifest_delete and _update, assign_from_chashes_384, _768 and _1024 (same-collection and cross branch), and
 * quarantine_restore_chunks. Each commits, and afterwards every child row of the tenant carries its chunk's stored name (the chunk name
 * keys' invariant). The trigger's own cases, with the per-table controls, are in {@link Rdr229P37ChildNameTriggerTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37ChildNameWritersTest {

    private Rdr229RenameEngine engine;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine();
        Rdr229RenameRestartPrecondition.reach(engine).reached().require();
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    // ---- stamp_chunks_on_manifest_delete / _update ----------------------------------------------------------------------------

    @Test
    void stampChunksOnManifestDelete_recordsAnExistingAndANewChunkUnderTheirOwnNames() throws Exception {
        var w = Rdr229ChildNameWorld.renamed(engine, "sdel", Rdr229ChildNameWorld.MODEL_384, 384);
        w.run(w.manifestInsert("cn.sdel.d0", 0, w.newName, w.ex[0], w.nw[0]));
        assertThat(w.manifestNames("cn.sdel.d0")).as("non-vacuity: a manifest row of each").containsExactlyInAnyOrder(w.oldName, w.newName);

        w.run("DELETE FROM nexus.catalog_document_chunks WHERE tenant_id = '" + w.tenant + "' AND doc_id = 'cn.sdel.d0'");

        assertThat(w.orphanedNames()).as("the delete trigger recorded both dropped chunks, each under its stored name")
            .containsExactlyInAnyOrder(w.oldName, w.newName);
        assertThat(w.column("SELECT collection_id::text FROM nexus.chunk_orphaned_at WHERE tenant_id = '" + w.tenant + "'"))
            .as("with the collection's one id").containsOnly(String.valueOf(w.id));
        assertThat(w.childrenNotOnTheirChunksName()).isZero();
    }

    @Test
    void stampChunksOnManifestUpdate_recordsTheDroppedChunksAndAMovedRowTakesTheNewChunksName() throws Exception {
        var w = Rdr229ChildNameWorld.renamed(engine, "supd", Rdr229ChildNameWorld.MODEL_384, 384);
        w.run(w.manifestInsert("cn.supd.d0", 0, w.newName, w.ex[0], w.nw[0]));   // one row per name
        // both rows move to another chunk, the old-named row to a new chunk and the new-named row to an old chunk; the writer keeps the label it holds
        w.run("UPDATE nexus.catalog_document_chunks SET chash = CASE position WHEN 0 THEN decode('" + w.hex(w.nw[1])
            + "','hex') ELSE decode('" + w.hex(w.ex[1]) + "','hex') END WHERE tenant_id = '" + w.tenant + "' AND doc_id = 'cn.supd.d0'");

        assertThat(w.manifestNames("cn.supd.d0")).as("each moved row follows the chunk it now references")
            .containsExactlyInAnyOrder(w.newName, w.oldName);
        assertThat(w.orphanedNames()).as("the update trigger recorded the two chunks the rows dropped, under their own names")
            .containsExactlyInAnyOrder(w.oldName, w.newName);
        assertThat(w.childrenNotOnTheirChunksName()).isZero();
    }

    // ---- assign_from_chashes_<dim> --------------------------------------------------------------------------------------------

    @Test
    void assignFromChashes384_sameCollectionAndCrossBranch() throws Exception {
        assignFromChashes("a384", Rdr229ChildNameWorld.MODEL_384, 384);
    }

    @Test
    void assignFromChashes768_sameCollectionAndCrossBranch() throws Exception {
        assignFromChashes("a768", Rdr229ChildNameWorld.MODEL_768, 768);
    }

    @Test
    void assignFromChashes1024_sameCollectionAndCrossBranch() throws Exception {
        assignFromChashes("a1024", Rdr229ChildNameWorld.MODEL_1024, 1024);
    }

    private void assignFromChashes(String tag, String model, int dim) throws Exception {
        var w = Rdr229ChildNameWorld.renamed(engine, tag, model, dim);
        String other = Rdr229ChildNameWorld.collection("p37cn" + tag + "o", model, 1);
        long[] otherTopic = new long[1];
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, w.tenant, other);
            long otherId = Rdr229RenameChecks.registryId(ctx, w.tenant, other);
            otherTopic[0] = ctx.insertInto(TOPICS).set(TOPICS.TENANT_ID, w.tenant).set(TOPICS.LABEL, "other").set(TOPICS.COLLECTION, other)
                .set(TOPICS.COLLECTION_ID, otherId).set(TOPICS.CREATED_AT, OffsetDateTime.now()).returning(TOPICS.ID).fetchOne().getId();
            Rdr229ChildNameWorld.centroid(ctx, w.tenant, other, otherId, model, dim, otherTopic[0], -0.1f);
            // the renamed collection's own centroid, written after the rename under the new label
            Rdr229ChildNameWorld.centroid(ctx, w.tenant, w.newName, w.id, model, dim, w.topic, 0.1f);
            return null;
        });
        String[] hexes = {w.hex(w.nw[0]), w.hex(w.nw[1]), w.hex(w.ex[0])};   // the old-named chunk is read by name: left to P3.5b's readers

        // same collection: the chunks of the new label are assigned to the collection's own centroid, under the new label
        engine.su(ctx -> {
            ctx.execute("SELECT set_config('nexus.tenant', ?, false)", w.tenant);
            call(ctx, dim, w.newName, hexes, false, model, w.tenant);
            return null;
        });
        assertThat(w.column("SELECT encode(doc_id,'hex') FROM nexus.topic_assignments WHERE tenant_id = '" + w.tenant + "' AND topic_id = " + w.topic))
            .as("the two chunks the function reads by name are assigned (non-vacuity: the writer wrote rows)")
            .contains(hexes[0], hexes[1]);
        assertThat(w.childrenNotOnTheirChunksName()).isZero();

        // cross branch: the nearest centroid of ANOTHER collection; a row for nw[1] exists already, so the conflict update runs
        engine.su(ctx -> {
            ctx.execute("SELECT set_config('nexus.tenant', ?, false)", w.tenant);
            ctx.execute("INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by, similarity, source_collection, "
                + "embedding_model, source_collection_id) VALUES ('" + w.tenant + "', decode('" + hexes[1] + "','hex'), " + otherTopic[0]
                + ", 'seed', -0.5, '" + w.newName + "', '" + model + "', " + w.id + ")");
            call(ctx, dim, w.newName, hexes, true, model, w.tenant);
            return null;
        });
        assertThat(w.column("SELECT encode(doc_id,'hex') FROM nexus.topic_assignments WHERE tenant_id = '" + w.tenant + "' AND topic_id = " + otherTopic[0]))
            .as("the cross branch assigned the new-named chunks to the other collection's topic").contains(hexes[0], hexes[1]);
        assertThat(w.childrenNotOnTheirChunksName()).as("every assignment carries its chunk's stored name").isZero();
    }

    private static void call(DSLContext ctx, int dim, String coll, String[] hexes, boolean cross, String model, String tenant) {
        switch (dim) {
            case 384 -> ctx.selectFrom(ASSIGN_FROM_CHASHES_384.call(coll, hexes, cross, model, tenant)).fetch();
            case 768 -> ctx.selectFrom(ASSIGN_FROM_CHASHES_768.call(coll, hexes, cross, model, tenant)).fetch();
            default -> ctx.selectFrom(ASSIGN_FROM_CHASHES_1024.call(coll, hexes, cross, model, tenant)).fetch();
        }
    }

    // ---- quarantine_restore_chunks --------------------------------------------------------------------------------------------

    @Test
    void quarantineRestoreChunks_reattachesAManifestRowToTheRestoredChunkOfTheRenamedCollection() throws Exception {
        var w = Rdr229ChildNameWorld.renamed(engine, "qr", Rdr229ChildNameWorld.MODEL_768, 768);
        String sibling = "quarantine-" + w.newName;
        String doc = "cn.qr.restore";
        byte[][] held = new byte[1][];
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, w.tenant, sibling);
            long siblingId = Rdr229RenameChecks.registryId(ctx, w.tenant, sibling);
            held[0] = Rdr229ChildNameWorld.chunk(ctx, w.tenant, sibling, siblingId, w.model, w.dim, "held");
            ctx.update(dev.nexus.service.jooq.nexus.Tables.CHUNKS)
                .set(dev.nexus.service.jooq.nexus.Tables.CHUNKS.METADATA, org.jooq.JSONB.valueOf("{\"origin_collection\": \"" + w.newName
                    + "\", \"quarantined_at\": \"2026-01-01T00:00:00Z\", \"catalog_doc_id\": \"" + doc + "\", \"chunk_index\": 0}"))
                .where(dev.nexus.service.jooq.nexus.Tables.CHUNKS.TENANT_ID.eq(w.tenant))
                .and(dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION.eq(sibling)).execute();
            ctx.insertInto(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.TENANT_ID, w.tenant).set(CATALOG_DOCUMENTS.TUMBLER, doc)
                .set(CATALOG_DOCUMENTS.TITLE, "Doc " + doc).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, w.newName)
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, w.id).execute();
            return null;
        });

        engine.su(ctx -> {
            ctx.selectFrom(QUARANTINE_RESTORE_CHUNKS.call(w.tenant, w.newName, sibling, new String[] {w.hex(held[0])}, "p37", null, false, true)).fetch();
            return null;
        });

        assertThat(w.column("SELECT collection FROM nexus.chunks WHERE tenant_id = '" + w.tenant + "' AND chash = decode('" + w.hex(held[0]) + "','hex')"))
            .as("non-vacuity: the chunk was restored into the renamed collection, under the new label").containsExactly(w.newName);
        assertThat(w.manifestNames(doc)).as("the reattached manifest row is there, under the label its chunk carries").containsExactly(w.newName);
        assertThat(w.childrenNotOnTheirChunksName()).isZero();
    }
}
