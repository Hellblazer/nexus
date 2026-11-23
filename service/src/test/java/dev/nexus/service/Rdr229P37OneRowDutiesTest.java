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
     * folio's r3 review F4 (census pass 2 item 3): a quarantine sibling found by its ATTRIBUTES, not by the {@code quarantine-<origin>} name or a tag. Its
     * untagged row is the origin's by the rule in QuarantineOrigin's class doc (same owner, model and version, content type {@code quarantine-<type>}, no
     * other live twin). The one-row rename has already moved the origin's registry row to B when it retags, so the sibling is found through the origin's
     * id under the latched gate; found by the old label A, which no registry row carries any more, the row stayed untagged.
     */
    @Test
    void aOneRowRenameRetagsAnAttributeDerivedQuarantineSibling() throws Exception {
        String tenant = "p37-duty-r";
        String token = engine.newTenant(tenant);
        String a = "knowledge__p37dutyra__minilm-l6-v2-384__v1";
        String b = "knowledge__p37dutyrb__minilm-l6-v2-384__v1";
        String sibling = "knowledge__p37dutyrq__minilm-l6-v2-384__v1";   // not quarantine-<a>: a sibling by attributes only
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, a);
            PgContainerHelper.insertCollection(ctx, tenant, sibling);
            var origin = ctx.selectFrom(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS)
                .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                    .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME.eq(a))).fetchSingle();
            var cc = dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
            ctx.update(cc).set(cc.LIFECYCLE_STATE, "quarantine").set(cc.CONTENT_TYPE, dev.nexus.service.db.QuarantineOrigin.PREFIX + origin.getContentType())
                .set(cc.OWNER_ID, origin.getOwnerId()).set(cc.EMBEDDING_MODEL, origin.getEmbeddingModel()).set(cc.MODEL_VERSION, origin.getModelVersion())
                .where(cc.TENANT_ID.eq(tenant).and(cc.NAME.eq(sibling))).execute();
            long siblingId = Rdr229RenameChecks.registryId(ctx, tenant, sibling);
            Rdr229ChildNameWorld.chunk(ctx, tenant, sibling, siblingId, "minilm-l6-v2-384", 384, "held");   // untagged: no origin_collection key
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        String before = engine.su(ctx -> ctx.select(CHUNKS.METADATA).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(sibling))
            .fetchOne(0, String.class));
        assertThat(before == null ? "" : before).as("non-vacuity: the sibling's row carries no origin tag").doesNotContain("origin_collection");

        var r = engine.rename(token, a, b);

        assertThat(r.status()).as("the one-row rename: " + r).isEqualTo(200);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b))).as("non-vacuity: one-row, the id is kept").isEqualTo(id);
        String metadata = engine.su(ctx -> ctx.select(CHUNKS.METADATA).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(sibling))
            .fetchOne(0, String.class));
        assertThat(metadata).as("the attribute-derived sibling's row was retagged").isNotNull();
        JsonNode tag = Rdr229RenameEngine.MAPPER.readTree(metadata);
        assertThat(tag.path("origin_collection").asText()).as("it names the new label as its origin").isEqualTo(b);
        assertThat(tag.path(dev.nexus.service.db.QuarantineOrigin.ORIGIN_ID_KEY).asLong()).as("and the renamed collection's id").isEqualTo(id);
    }
}
