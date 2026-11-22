// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), vectors-079's step 2 picks the RIGHT chunk when others look alike (review S3b): the same chash and the
 * same collection id held by a second tenant, the same chash and id held under a second embedding model, and, after a one-row rename, a
 * re-index that wrote the same chash under both the old and the new label with one id. Each of the first two is also run against a mutant
 * of the function with the tenant (or the model) predicate dropped from step 2: it must go red, so the predicate is load-bearing and the
 * test would notice its removal. The mutant is installed over the real function and the real text restored after.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37ChildNameMismatchTest {

    private static final String FUNCTION = Vectors079Text.MANIFEST;
    private static final String STEP2_TENANT = "c.tenant_id = NEW.tenant_id AND c.collection_id = NEW.collection_id";
    private static final String STEP2_MODEL = "AND c.chash = NEW.chash AND c.embedding_model = NEW.embedding_model\n     LIMIT 1";

    private Rdr229RenameEngine engine;
    private String original;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine();
        Rdr229RenameRestartPrecondition.reach(engine).reached().require();
        original = functionSql();
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    private static String functionSql() throws Exception {
        return Vectors079Text.function(FUNCTION);
    }

    private void install(String sql) {
        engine.su(ctx -> ctx.execute(sql));
    }

    // ---- a second tenant -----------------------------------------------------------------------------------------------------

    /** The world of tenant "p37-cn-mt" plus tenant "p37-b4-mt" (sorting first) holding the same chash under the same collection id. */
    private Rdr229ChildNameWorld secondTenantWorld() throws Exception {
        var w = Rdr229ChildNameWorld.renamed(engine, "mt", Rdr229ChildNameWorld.MODEL_384, 384);
        String other = "p37-b4-mt";
        engine.newTenant(other);
        String otherName = Rdr229ChildNameWorld.collection("p37b4mt", w.model, 1);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, other, otherName);
            ctx.execute("UPDATE nexus.catalog_collections SET collection_id = ? WHERE tenant_id = ? AND name = ?", w.id, other, otherName);
            Rdr229ChildNameWorld.chunk(ctx, other, otherName, w.id, w.model, 384, "twin", w.ex[0]);
            return null;
        });
        return w;
    }

    @Test
    void aSecondTenantHoldingTheSameChashAndTheSameCollectionIdDoesNotConfuseTheLookup() throws Exception {
        var w = secondTenantWorld();
        w.run(w.manifestInsert("cn.mt.d0", 0, w.newName, w.ex[0]));
        assertThat(w.manifestNames("cn.mt.d0")).as("the chunk of THIS tenant: its stored name, not the other tenant's").containsExactly(w.oldName);
    }

    @Test
    void droppingTheTenantPredicateFromStep2TurnsTheSecondTenantTestRed() throws Exception {
        var w = secondTenantWorld();
        assertThat(original).as("non-vacuity: the text to mutate carries the predicate").contains(STEP2_TENANT);
        install(original.replace(STEP2_TENANT, "c.collection_id = NEW.collection_id"));
        try {
            boolean red;
            try {
                w.run(w.manifestInsert("cn.mt.d1", 0, w.newName, w.ex[0]));
                red = !w.manifestNames("cn.mt.d1").equals(List.of(w.oldName));
            } catch (RuntimeException refused) {
                red = true;   // the key refused the other tenant's name
            }
            assertThat(red).as("without the tenant predicate the lookup takes the other tenant's chunk").isTrue();
        } finally {
            install(original);
        }
    }

    // ---- a second embedding model ----------------------------------------------------------------------------------------------

    /** The world plus a chunk of the same chash and collection id under another model (768), stored under another name. */
    private Rdr229ChildNameWorld secondModelWorld(String tag) throws Exception {
        var w = Rdr229ChildNameWorld.renamed(engine, tag, Rdr229ChildNameWorld.MODEL_384, 384);
        engine.su(ctx -> {
            Rdr229ChildNameWorld.chunk(ctx, w.tenant, "ghost__" + tag, w.id, Rdr229ChildNameWorld.MODEL_768, 768, "ghost", w.ex[0]);
            return null;
        });
        return w;
    }

    @Test
    void aSecondEmbeddingModelHoldingTheSameChashAndIdDoesNotConfuseTheLookup() throws Exception {
        var w = secondModelWorld("mm");
        w.run(w.manifestInsert("cn.mm.d0", 0, w.newName, w.ex[0]));
        assertThat(w.manifestNames("cn.mm.d0")).as("the chunk of the row's own model").containsExactly(w.oldName);
    }

    @Test
    void droppingTheModelPredicateFromStep2TurnsTheSecondModelTestRed() throws Exception {
        var w = secondModelWorld("mn");
        assertThat(original).as("non-vacuity: the text to mutate carries the predicate").contains(STEP2_MODEL);
        install(original.replace(STEP2_MODEL, "AND c.chash = NEW.chash\n     LIMIT 1"));
        try {
            boolean red;
            try {
                w.run(w.manifestInsert("cn.mn.d0", 0, w.newName, w.ex[0]));
                red = !w.manifestNames("cn.mn.d0").equals(List.of(w.oldName));
            } catch (RuntimeException refused) {
                red = true;
            }
            assertThat(red).as("without the model predicate the lookup takes the other model's chunk").isTrue();
        } finally {
            install(original);
        }
    }

    // ---- a re-index that wrote one chash under both labels ----------------------------------------------------------------------

    @Test
    void afterARenameAReindexThatWroteTheSameChashUnderTheOldAndTheNewLabelWithOneId_eachRowKeepsTheLabelOfItsChunk() throws Exception {
        var w = Rdr229ChildNameWorld.renamed(engine, "dup", Rdr229ChildNameWorld.MODEL_384, 384);
        // the same chunk text again, written under the NEW label by a writer that did not look for the old-named row: one chash, one id, two rows
        engine.su(ctx -> {
            Rdr229ChildNameWorld.chunk(ctx, w.tenant, w.newName, w.id, w.model, w.dim, "dup", w.ex[0]);
            return null;
        });
        assertThat(w.column("SELECT collection FROM nexus.chunks WHERE tenant_id = '" + w.tenant + "' AND chash = decode('" + w.hex(w.ex[0]) + "','hex')"))
            .as("non-vacuity: two chunk rows, one per label").containsExactlyInAnyOrder(w.oldName, w.newName);

        // a writer under the new label: the chunk under the name it supplied is the child's own (same id), so the label stays
        w.run(w.manifestInsert("cn.dup.d0", 0, w.newName, w.ex[0]));
        // a writer that still holds the old label: likewise
        w.run(w.manifestInsert("cn.dup.d1", 0, w.oldName, w.ex[0]));

        assertThat(w.manifestNames("cn.dup.d0")).as("found by the supplied name: no replacement").containsExactly(w.newName);
        assertThat(w.manifestNames("cn.dup.d1")).containsExactly(w.oldName);
        assertThat(w.childrenNotOnTheirChunksName()).isZero();
    }
}
