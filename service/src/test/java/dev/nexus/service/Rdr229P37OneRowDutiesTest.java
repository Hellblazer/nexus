// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27; folio's review of 19d19081dc): in the one-row regime the per-store renames are no-ops, so the catalog's one-row body owns every
 * duty. The aspect-task rewrite is pinned by {@code Rdr229P37OneRowRenameTest} (a queued task names the new label); this pins the quarantine retag
 * ({@code QuarantineOrigin.retagRowsOf}): a quarantine sibling's rows name their origin by label and by id, and the one-row rename retags them.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37OneRowDutiesTest {

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

    @Test
    void aOneRowRenameRetagsTheQuarantineSiblingsOfTheCollection() throws Exception {
        String tenant = "p37-duty-q";
        String token = engine.newTenant(tenant);
        String a = "knowledge__p37dutyqa__minilm-l6-v2-384__v1";
        String b = "knowledge__p37dutyqb__minilm-l6-v2-384__v1";
        String sibling = "quarantine-" + a;
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, a);
            PgContainerHelper.insertCollection(ctx, tenant, sibling);
            long siblingId = Rdr229RenameChecks.registryId(ctx, tenant, sibling);
            Rdr229ChildNameWorld.chunk(ctx, tenant, sibling, siblingId, "minilm-l6-v2-384", 384, "held");
            ctx.update(CHUNKS).set(CHUNKS.METADATA, org.jooq.JSONB.valueOf("{\"origin_collection\": \"" + a + "\", \"quarantined_at\": \"2026-01-01T00:00:00Z\"}"))
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(sibling)).execute();
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));

        var r = engine.rename(token, a, b);

        assertThat(r.status()).as("the one-row rename: " + r).isEqualTo(200);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b))).as("non-vacuity: one-row, the id is kept").isEqualTo(id);
        String metadata = engine.su(ctx -> ctx.select(CHUNKS.METADATA).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(sibling))
            .fetchOne(0, String.class));
        JsonNode tag = Rdr229RenameEngine.MAPPER.readTree(metadata);
        assertThat(tag.path("origin_collection").asText()).as("the sibling's rows name the new label as their origin").isEqualTo(b);
        assertThat(tag.path("quarantined_at").asText()).as("and keep every other key the move wrote").isEqualTo("2026-01-01T00:00:00Z");
    }

    /**
     * linda's ruling (2026-11-23): under the latched gate the one-row rename retags a label-tagged quarantine row by the origin's id it already knows,
     * because by then the registry row carries the new label and {@code CollectionIds.idOf(tenant, <old label>)} finds nothing. The row carries the
     * origin tag by label AND by id ({@code origin_collection_id}), the shape every quarantine write has from the latch on. The mutant run (the
     * {@code tagged} clause of {@code QuarantineOrigin.rowsOf} back at {@code taggedWith(byId, tenant, origin)}) is on the bead.
     *
     * <p>elsinore's note: from the latch a row with no {@code origin_collection_id} is no sibling of the label's holder, and there is no label fallback, so
     * the second row, tagged by label alone, is skipped and keeps naming A.
     */
    @Test
    void aOneRowRenameRetagsALabelTaggedQuarantineRowByTheKnownId_andSkipsOneTaggedByLabelAlone() throws Exception {
        String tenant = "p37-duty-i";
        String token = engine.newTenant(tenant);
        String a = "knowledge__p37dutyia__minilm-l6-v2-384__v1";
        String b = "knowledge__p37dutyib__minilm-l6-v2-384__v1";
        String sibling = "quarantine-" + a;
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, a);
            PgContainerHelper.insertCollection(ctx, tenant, sibling);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        long siblingId = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, sibling));
        byte[] tagged = engine.su(ctx -> {
            byte[] h = Rdr229ChildNameWorld.chunk(ctx, tenant, sibling, siblingId, "minilm-l6-v2-384", 384, "by-id");
            ctx.update(CHUNKS).set(CHUNKS.METADATA, org.jooq.JSONB.valueOf("{\"origin_collection\": \"" + a + "\", \"origin_collection_id\": " + id
                    + ", \"quarantined_at\": \"2026-01-01T00:00:00Z\"}"))
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION_ID.eq(siblingId)).and(CHUNKS.CHASH.eq(h)).execute();
            return h;
        });
        byte[] labelOnly = engine.su(ctx -> {
            byte[] h = Rdr229ChildNameWorld.chunk(ctx, tenant, sibling, siblingId, "minilm-l6-v2-384", 384, "label-only");
            ctx.update(CHUNKS).set(CHUNKS.METADATA, org.jooq.JSONB.valueOf("{\"origin_collection\": \"" + a + "\", \"quarantined_at\": \"2026-01-01T00:00:00Z\"}"))
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION_ID.eq(siblingId)).and(CHUNKS.CHASH.eq(h)).execute();
            return h;
        });
        assertThat(engine.gate3Holds()).as("non-vacuity: gate 3 is latched, so the id-keyed readers are the ones that run").isTrue();
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a))).as("non-vacuity: A is registered under the id the tag names").isEqualTo(id);

        var r = engine.rename(token, a, b);

        assertThat(r.status()).as("the one-row rename: " + r).isEqualTo(200);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b))).as("non-vacuity: one-row, the id is kept, A's label no longer resolves").isEqualTo(id);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a))).isNull();
        JsonNode retagged = Rdr229RenameEngine.MAPPER.readTree(engine.<String>su(ctx -> ctx.select(CHUNKS.METADATA).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
            .and(CHUNKS.COLLECTION_ID.eq(siblingId)).and(CHUNKS.CHASH.eq(tagged)).fetchOne(0, String.class)));
        assertThat(retagged.path("origin_collection").asText()).as("the row found by the known id names the new label as its origin").isEqualTo(b);
        assertThat(retagged.path(dev.nexus.service.db.QuarantineOrigin.ORIGIN_ID_KEY).asText()).as("its origin id is the renamed collection's, unchanged")
            .isEqualTo(String.valueOf(id));
        assertThat(retagged.path("quarantined_at").asText()).as("and every other key survives").isEqualTo("2026-01-01T00:00:00Z");
        JsonNode skipped = Rdr229RenameEngine.MAPPER.readTree(engine.<String>su(ctx -> ctx.select(CHUNKS.METADATA).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
            .and(CHUNKS.COLLECTION_ID.eq(siblingId)).and(CHUNKS.CHASH.eq(labelOnly)).fetchOne(0, String.class)));
        assertThat(skipped.path("origin_collection").asText()).as("a row tagged by label alone is no sibling from the latch: not retagged by label").isEqualTo(a);
        assertThat(skipped.has(dev.nexus.service.db.QuarantineOrigin.ORIGIN_ID_KEY)).isFalse();
        assertThat(engine.<Integer>su(ctx -> ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.GC_AUDIT,
                dev.nexus.service.jooq.nexus.Tables.GC_AUDIT.TENANT_ID.eq(tenant)
                    .and(dev.nexus.service.jooq.nexus.Tables.GC_AUDIT.OPERATION.eq(dev.nexus.service.db.QuarantineOrigin.OP_RETAG)))))
            .as("one retag audit row, for the one sibling that lost a row").isEqualTo(1);
    }

    /**
     * folio's r3 review F4 (census pass 2 item 3): a quarantine sibling found by its ATTRIBUTES, not by the {@code quarantine-<origin>} name or a tag. Its
     * untagged row is the origin's by the rule in QuarantineOrigin's class doc (same owner, model and version, content type {@code quarantine-<type>}, no
     * other live twin). The one-row rename has already moved the origin's registry row to B when it retags, so the sibling is found through the origin's
     * id under the latched gate; found by the old label A, which no registry row carries any more, the row stayed untagged.
     *
     * <p>folio's r3 confirmation review S1: the sibling has itself been renamed before (its registry row says {@code S2}), so its chunk still carries the
     * stale name {@code S1} in the name column and the id in the id column. Only the latched, id-keyed readers find that row; read by name
     * ({@code Gate3.never()}) the chunk matches no quarantine registry row. The mutant run is on the bead.
     */
    @Test
    void aOneRowRenameRetagsAnAttributeDerivedQuarantineSibling_whoseChunkCarriesAStaleName() throws Exception {
        String tenant = "p37-duty-r";
        String token = engine.newTenant(tenant);
        String a = "knowledge__p37dutyra__minilm-l6-v2-384__v1";
        String b = "knowledge__p37dutyrb__minilm-l6-v2-384__v1";
        String staleName = "knowledge__p37dutyrq__minilm-l6-v2-384__v1";    // what the sibling's chunk says: the label the sibling had when it was written
        String sibling = "knowledge__p37dutyrr__minilm-l6-v2-384__v1";      // what the registry says now: not quarantine-<a>, a sibling by attributes only
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, a);
            PgContainerHelper.insertCollection(ctx, tenant, staleName);
            var cc = dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
            var origin = ctx.selectFrom(cc).where(cc.TENANT_ID.eq(tenant).and(cc.NAME.eq(a))).fetchSingle();
            ctx.update(cc).set(cc.LIFECYCLE_STATE, "quarantine").set(cc.CONTENT_TYPE, dev.nexus.service.db.QuarantineOrigin.PREFIX + origin.getContentType())
                .set(cc.OWNER_ID, origin.getOwnerId()).set(cc.EMBEDDING_MODEL, origin.getEmbeddingModel()).set(cc.MODEL_VERSION, origin.getModelVersion())
                .where(cc.TENANT_ID.eq(tenant).and(cc.NAME.eq(staleName))).execute();
            long siblingId = Rdr229RenameChecks.registryId(ctx, tenant, staleName);
            Rdr229ChildNameWorld.chunk(ctx, tenant, staleName, siblingId, "minilm-l6-v2-384", 384, "held");   // untagged: no origin_collection key
            // the sibling is renamed one-row style: its registry row moves, its chunk keeps the old name
            ctx.update(cc).set(cc.NAME, sibling).where(cc.TENANT_ID.eq(tenant).and(cc.COLLECTION_ID.eq(siblingId))).execute();
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        long siblingId = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, sibling));
        var row = engine.su(ctx -> ctx.select(CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID, CHUNKS.METADATA).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant))
            .and(CHUNKS.COLLECTION_ID.eq(siblingId)).fetchSingle());
        assertThat(row.value1()).as("non-vacuity: the chunk's name column is stale, not the registry's name").isEqualTo(staleName).isNotEqualTo(sibling);
        assertThat(row.value3() == null ? "" : row.value3().data()).as("non-vacuity: the sibling's row carries no origin tag").doesNotContain("origin_collection");

        var r = engine.rename(token, a, b);

        assertThat(r.status()).as("the one-row rename: " + r).isEqualTo(200);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b))).as("non-vacuity: one-row, the id is kept").isEqualTo(id);
        String metadata = engine.su(ctx -> ctx.select(CHUNKS.METADATA).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION_ID.eq(siblingId))
            .fetchOne(0, String.class));
        assertThat(metadata).as("the attribute-derived sibling's row was retagged, though its chunk names a label no registry row carries").isNotNull();
        JsonNode tag = Rdr229RenameEngine.MAPPER.readTree(metadata);
        assertThat(tag.path("origin_collection").asText()).as("it names the new label as its origin").isEqualTo(b);
        assertThat(tag.path(dev.nexus.service.db.QuarantineOrigin.ORIGIN_ID_KEY).asLong()).as("and the renamed collection's id").isEqualTo(id);
    }

    /**
     * folio's r3 review F5, and its confirmation review S2: the one-row body remembers the row's tuple BEFORE its UPDATE. The UPDATE of a rename that
     * takes another live collection's (content type, owner, model, version) raises 23505 on the live-tuple index, which aborts the transaction; the
     * caller's race recheck ({@code CollectionMint#recheckOf}) then reads the remembered tuple in a new one and names the holder. Remembered after the
     * UPDATE, the tuple is empty when the 23505 escapes, the recheck finds nothing to check and the client gets the raw violation. The mutant run is on the bead.
     */
    @Test
    void aOneRowRenameWhoseUpdateHitsTheLiveTupleIndexIsRefusedNamingTheHolder() throws Exception {
        String tenant = "p37-duty-t";
        String token = engine.newTenant(tenant);
        String a = "knowledge__p37dutyta__minilm-l6-v2-384__v1";
        String b = "knowledge__p37dutytb__minilm-l6-v2-384__v1";
        String holder = "knowledge__p37dutyth__minilm-l6-v2-384__v1";
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, a);
            PgContainerHelper.insertCollection(ctx, tenant, holder);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        var cc = dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
        String holderOwner = engine.su(ctx -> ctx.select(cc.OWNER_ID).from(cc).where(cc.TENANT_ID.eq(tenant).and(cc.NAME.eq(holder))).fetchOne(0, String.class));
        assertThat(holderOwner).as("non-vacuity: the holder's owner is not A's").isNotEqualTo(engine.<String>su(ctx -> ctx.select(cc.OWNER_ID).from(cc)
            .where(cc.TENANT_ID.eq(tenant).and(cc.NAME.eq(a))).fetchOne(0, String.class)));

        // B is a free label, and the client names the holder's owner for it: the row's tuple would then be the holder's
        var r = engine.call("POST", "/v1/catalog/collections/rename", token,
            "{\"old_name\":\"" + a + "\",\"new_name\":\"" + b + "\",\"owner_id\":\"" + holderOwner + "\"}");

        assertThat(r.statusCode()).as("refused as a conflict, not a raw unique violation: " + r.body()).isEqualTo(409);
        assertThat(r.body()).as("naming both collections").contains(holder).contains("are both live for the tuple");
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a))).as("A was not renamed").isEqualTo(id);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b))).as("and nothing is registered under B").isNull();
    }
}
