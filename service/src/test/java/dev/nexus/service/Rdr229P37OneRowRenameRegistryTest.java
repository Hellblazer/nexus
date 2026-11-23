// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.CollectionIds;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;
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
        String newName = coll("p37undob", 2);   // another owner: a tuple has one live collection, and the tombstone is seeded as a second row
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
        String ancient = coll("p37chaina", 1);   // another owner than current's: a tuple has one live collection
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

    /** The name chain the name readers follow until P4.0: from {@code from} through {@code superseded_by} while it is not '' . */
    private String walkByName(String tenant, String from) {
        String at = from;
        for (int hops = 0; hops < 10; hops++) {
            String name = at;
            String next = engine.su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).fetchOne(0, String.class));
            assertThat(next).as("every name on the chain is a registered row (a dangling name ends the walk): " + name).isNotNull();
            if (next.isEmpty()) {
                return at;
            }
            at = next;
        }
        throw new AssertionError("the chain from " + from + " does not end in ten hops");
    }

    /** The A -> B chain of the bead: A's tombstone names B, B is renamed one-row to C, and the chain walked by name reaches C. */
    @Test
    void aChainAToB_afterBIsRenamedToC_isWalkedByNameToC_andATombstoneNamingAnotherLabelIsUntouched() throws Exception {
        String tenant = "p37-chainabc";
        String token = engine.newTenant(tenant);
        String a = coll("p37chaina2", 1);   // each of A, B and the decoy under another owner: a tuple has one live collection
        String b = coll("p37chainb2", 1);
        String c = coll("p37chainb2", 2);
        String d = coll("p37chaind2", 1);   // the decoy: a tombstone whose superseded_by is NOT the label being renamed
        String e = coll("p37chaine2", 1);   // the decoy's live successor
        engine.su(ctx -> {
            for (String name : new String[] {a, b, d, e}) {
                PgContainerHelper.insertCollection(ctx, tenant, name);
            }
            tombstone(ctx, tenant, a, b);
            tombstone(ctx, tenant, d, e);
            return null;
        });
        long bId = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b));
        var decoyBefore = engine.su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_BY_ID,
                DSL.field(DSL.name("xmin"), String.class)).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(d)).fetchOne());
        assertThat(walkByName(tenant, a)).as("non-vacuity: before the rename the chain A -> B ends at B").isEqualTo(b);

        var r = engine.rename(token, b, c, Map.of("content_type", "knowledge"));

        assertThat(r.status()).as("B is renamed one-row to C: " + r).isEqualTo(200);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, c))).as("the one-row rename: B's row, same id").isEqualTo(bId);
        assertThat(walkByName(tenant, a)).as("the chain A -> B, walked by name, reaches the renamed collection C").isEqualTo(c);
        var decoyAfter = engine.su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_BY_ID,
                DSL.field(DSL.name("xmin"), String.class)).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(d)).fetchOne());
        assertThat(decoyAfter.value1()).as("a tombstone whose superseded_by is not the old label keeps it").isEqualTo(e);
        assertThat(decoyAfter.value3()).as("and its row was not rewritten at all (same xmin): the repoint is bounded to the old label")
            .isEqualTo(decoyBefore.value3());
    }

    private static void tombstone(DSLContext ctx, String tenant, String name, String successor) {
        ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, Rdr229RenameChecks.registryId(ctx, tenant, successor))
            .set(CATALOG_COLLECTIONS.SUPERSEDED_AT, OffsetDateTime.now())
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute();
    }

    /** S-3 rule 2: the drop move's row names the registering engine and its version, not the stage-1 'engine-pass:' value. */
    @Test
    void theDropMoveRowsChangesetIdNamesTheEngineVersionThatRegisteredIt() {
        String id = engine.su(ctx -> ctx.select(DSL.field(DSL.name("changeset_id"), String.class))
            .from(DSL.table(DSL.name("nexus", "data_moves")))
            .where(DSL.field(DSL.name("label"), String.class).eq("rdr229-name-key-drop")).fetchOne(0, String.class));
        assertThat(id).isEqualTo("engine:rdr229-name-key-drop:" + Rdr229RenameEngine.ENGINE_VERSION);
    }

    /** The latch is set and P3.5b applied, then the live gate 3 dips: the rename is still the one-row update (same id), never the name path. */
    @Test
    void aRenameAfterGate3DipsOnAnInstallWhoseDropMoveCompleted_isStillOneRow() throws Exception {
        String tenant = "p37-dip";
        String token = engine.newTenant(tenant);
        String oldName = coll("p37dip", 1);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, oldName);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));
        engine.gate3(false);
        try {
            assertThat(Rdr229RenameEngine.await(java.time.Duration.ofSeconds(10), () -> !engine.completion().gate3()))
                .as("non-vacuity: the live gate 3 dropped back").isTrue();
            assertThat(engine.completion().gate3Latched()).as("while the latch holds").isTrue();

            var r = engine.rename(token, oldName, coll("p37dip", 2));

            assertThat(r.status()).as("the rename route: " + r).isEqualTo(200);
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, coll("p37dip", 2))))
                .as("one-row: the registry row keeps its id (the name path would have given a new one)").isEqualTo(id);
        } finally {
            engine.gate3(true);
            Rdr229RenameEngine.await(java.time.Duration.ofSeconds(10), () -> engine.completion().gate3());
        }
    }

    /**
     * linda 2026-11-23, ruling 2: CollectionRegistry stays a static cache; the one-row rename evicts the OLD label under the rename lock, after
     * commit, through the existing {@code CollectionRegistry.evict}. A lookup by the new label resolves to the same collection, one by the old misses.
     */
    @Test
    void aOneRowRenameEvictsTheOldLabelFromTheRegistryCache_theNewLabelResolvesToTheSameCollection() throws Exception {
        String tenant = "p37-evict";
        String token = engine.newTenant(tenant);
        String oldName = coll("p37evict", 1);
        String newName = coll("p37evict", 2);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, oldName);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));
        var before = dev.nexus.service.db.CollectionRegistry.lookup(engine.scope(), tenant, oldName);
        assertThat(dev.nexus.service.db.CollectionRegistry.isKnown(tenant, oldName)).as("non-vacuity: the old label is cached").isTrue();

        var r = engine.rename(token, oldName, newName);

        assertThat(r.status()).as("the rename route: " + r).isEqualTo(200);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, newName)))
            .as("non-vacuity: one-row, the same collection_id under the new label").isEqualTo(id);
        assertThat(dev.nexus.service.db.CollectionRegistry.isKnown(tenant, oldName)).as("the old label's cache key was evicted").isFalse();
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> dev.nexus.service.db.CollectionRegistry.lookup(engine.scope(), tenant, oldName))
            .as("a lookup by the old label misses: the row is under the new label").isInstanceOf(RuntimeException.class);
        var resolved = dev.nexus.service.db.CollectionRegistry.lookup(engine.scope(), tenant, newName);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, newName))).as("a lookup by the new label resolves to the same collection id").isEqualTo(id);
        assertThat(resolved.embeddingModel()).as("its row carries what the old one did").isEqualTo(before.embeddingModel());
        assertThat(resolved.contentType()).isEqualTo(before.contentType());
        assertThat(resolved.ownerId()).isEqualTo(before.ownerId());
        assertThat(resolved.dimension()).isEqualTo(before.dimension());
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, newName))).isEqualTo(id);
    }

    private static String codeColl(String owner, int version) {
        return "code__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    private String ownerOf(String tenant, String name) {
        return engine.su(ctx -> ctx.select(CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).fetchOne(0, String.class));
    }

    /**
     * Review S3: the name path re-derives a replaceable owner from the collection's documents after it re-homes them; the one-row path
     * leaves the documents' name column at the old label, so catalog-044/051's triggers (keyed on the name) find none. A settled owner (a
     * tumbler segment, here 1-2, derived from the document) must survive a rename whose label carries another slug and names it as
     * owner_id; a slug owner takes the client's, as it does on the name path. See CollectionOneRowRename#ownerTheRenameMayChange.
     */
    @Test
    void aSettledTumblerOwnerOfACodeCollectionSurvivesARenameThatNamesASlugOwner_aSlugOwnerTakesTheClients() throws Exception {
        String tenant = "p37-owner";
        String token = engine.newTenant(tenant);
        String settled = codeColl("p37slugs", 1);
        String slugged = codeColl("p37slugt", 1);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, settled);
            PgContainerHelper.insertCollection(ctx, tenant, slugged);
            ctx.insertInto(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, "1.2.3")
                .set(CATALOG_DOCUMENTS.TITLE, "d").set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, settled)
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, Rdr229RenameChecks.registryId(ctx, tenant, settled)).execute();
            return null;
        });
        assertThat(ownerOf(tenant, settled)).as("non-vacuity: the document's trigger settled the owner on its tumbler segment").isEqualTo("1-2");
        assertThat(ownerOf(tenant, slugged)).as("non-vacuity: the other collection keeps its slug owner").isEqualTo("p37slugt");

        var r1 = engine.rename(token, settled, codeColl("p37slugu", 1), Map.of("content_type", "code", "owner_id", "p37slugu"));
        var r2 = engine.rename(token, slugged, codeColl("p37slugv", 1), Map.of("content_type", "code", "owner_id", "p37slugv"));

        assertThat(r1.status()).as("the rename route: " + r1).isEqualTo(200);
        assertThat(ownerOf(tenant, codeColl("p37slugu", 1))).as("the settled owner is not replaced by the label's slug").isEqualTo("1-2");
        assertThat(r2.status()).as("the rename route: " + r2).isEqualTo(200);
        assertThat(ownerOf(tenant, codeColl("p37slugv", 1))).as("a slug owner takes the client's").isEqualTo("p37slugv");
    }
}
