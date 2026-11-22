// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.CollectionIds;
import org.jooq.DSLContext;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.OffsetDateTime;
import java.util.Map;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27): what the one-row rename does to the registry and to note URIs, beyond the bead's failing tests
 * ({@link Rdr229P37OneRowRenameTest}). Taken through the same restart precondition, so the drop move has run and P3.5b's changesets
 * (or the harness's fixture) have applied.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37OneRowRenameRegistryTest {

    private Rdr229RenameEngine engine;
    private Rdr229RenameRestartPrecondition.Reached pre;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine();
        pre = Rdr229RenameRestartPrecondition.reach(engine).reached();
        pre.require();
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    private String sourceUri(String tenant, String tumbler) {
        return engine.su(ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)).fetchOne(0, String.class));
    }

    private void document(DSLContext ctx, String tenant, String tumbler, String collection, String uri) {
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CATALOG_DOCUMENTS.SOURCE_URI)
            .values(tenant, tumbler, "Doc " + tumbler, collection, CollectionIds.require(ctx, tenant, collection), uri).execute();
    }

    @Test
    void aNoteUriFollowsTheLabel_aChromaUriOfTheSameCollectionDoesNot() throws Exception {
        String tenant = "p37-uri";
        String token = engine.newTenant(tenant);
        String oldName = coll("p37uri", 1);
        String newName = coll("p37uri", 2);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, oldName);
            document(ctx, tenant, "uri-note", oldName, "nx-note://" + oldName + "/a-note");
            document(ctx, tenant, "uri-chroma", oldName, "chroma://" + oldName + "/b.md");
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));

        var r = engine.rename(token, oldName, newName, Map.of("content_type", "knowledge"));

        assertThat(r.status()).as("the rename route: " + r).isEqualTo(200);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, newName))).as("the id is kept").isEqualTo(id);
        assertThat(sourceUri(tenant, "uri-note")).as("a note's URI follows the label (item 7)")
            .isEqualTo("nx-note://" + newName + "/a-note");
        assertThat(sourceUri(tenant, "uri-chroma")).as("chroma:// residue is never rewritten (P2.10)")
            .isEqualTo("chroma://" + oldName + "/b.md");
    }

    @Test
    void everyNoteOfTheCollectionIsFoundByItsIdAfterAnEarlierOneRowRename() throws Exception {
        String tenant = "p37-uri2";
        String token = engine.newTenant(tenant);
        String first = coll("p37uri2", 1);
        String second = coll("p37uri2", 2);
        String third = coll("p37uri2", 3);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, first);
            document(ctx, tenant, "uri2-a", first, "nx-note://" + first + "/a");
            return null;
        });
        assertThat(engine.rename(token, first, second, Map.of("content_type", "knowledge")).status()).isEqualTo(200);
        // a document written after the first rename carries the new label in its (dead) name column and in its URI
        engine.su(ctx -> {
            document(ctx, tenant, "uri2-b", second, "nx-note://" + second + "/b");
            return null;
        });

        var r = engine.rename(token, second, third, Map.of("content_type", "knowledge"));

        assertThat(r.status()).as("the second rename: " + r).isEqualTo(200);
        assertThat(sourceUri(tenant, "uri2-b")).as("the document written under the second label follows to the third")
            .isEqualTo("nx-note://" + third + "/b");
        assertThat(sourceUri(tenant, "uri2-a")).as("so does the first document, whose name column still holds the first label: both are the collection's by id")
            .isEqualTo("nx-note://" + third + "/a");
    }

    @Test
    void renamingBackOntoAnEmptyTombstoneOfThePreGateRename_deletesTheTombstoneAndKeepsTheId() throws Exception {
        String tenant = "p37-undo";
        String token = engine.newTenant(tenant);
        String oldName = coll("p37undo", 1);
        String newName = coll("p37undo", 2);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, oldName);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));
        // what a rename before gate 3 left at newName: an empty registry row that names oldName as its successor
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, newName);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, oldName)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, id).set(CATALOG_COLLECTIONS.SUPERSEDED_AT, OffsetDateTime.now())
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(newName)).execute();
            return null;
        });
        Long tombstone = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, newName));
        assertThat(tombstone).as("non-vacuity: a tombstone sits at the target label").isNotNull().isNotEqualTo(id);

        var r = engine.rename(token, oldName, newName, Map.of("content_type", "knowledge"));

        assertThat(r.status()).as("the undo is a one-row rename: " + r).isEqualTo(200);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, newName)))
            .as("the live row of the old id answers to the target label").isEqualTo(id);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName))).as("nothing is left at the old label").isNull();
    }

    @Test
    void aTombstoneThatNamedTheOldLabelNamesTheNewOneAndStillPointsAtTheSameRow() throws Exception {
        String tenant = "p37-chain";
        String token = engine.newTenant(tenant);
        String ancient = coll("p37chain", 1);
        String current = coll("p37chain", 2);
        String renamed = coll("p37chain", 3);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, current);
            PgContainerHelper.insertCollection(ctx, tenant, ancient);
            Long currentId = Rdr229RenameChecks.registryId(ctx, tenant, current);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, current)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, currentId).set(CATALOG_COLLECTIONS.SUPERSEDED_AT, OffsetDateTime.now())
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(ancient)).execute();
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, current));

        var r = engine.rename(token, current, renamed, Map.of("content_type", "knowledge"));

        assertThat(r.status()).as("the rename route: " + r).isEqualTo(200);
        var row = engine.su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_BY_ID)
            .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(ancient))
            .fetchOne());
        assertThat(row.value1()).as("the tombstone's name chain ends at a registered row").isEqualTo(renamed);
        assertThat(row.value2()).as("and its id still names the same collection").isEqualTo(id);
    }
}
