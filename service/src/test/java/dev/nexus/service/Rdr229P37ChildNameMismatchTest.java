// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
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

    private static final String STEP2_TENANT = "c.tenant_id = NEW.tenant_id AND c.collection_id = NEW.collection_id";
    private static final String STEP2_MODEL = "AND c.chash = NEW.chash AND c.embedding_model = NEW.embedding_model\n     LIMIT 1";

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

    private void withMutant(String from, String to, Runnable work) {
        Vectors079Text.withMutant(engine, from, to, work);
    }

    // ---- a second tenant -----------------------------------------------------------------------------------------------------

    /** The world of tenant "p37-cn-mt" plus tenant "p37-b4-mt" (sorting first) holding the same chash under the same collection id. */
    private Rdr229ChildNameWorld secondTenantWorld(String tag) throws Exception {
        var w = Rdr229ChildNameWorld.renamed(engine, tag, Rdr229ChildNameWorld.MODEL_384, 384);
        String other = "p37-b4-" + tag;
        engine.newTenant(other);
        String otherName = Rdr229ChildNameWorld.collection("p37b4" + tag, w.model, 1);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, other, otherName);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.COLLECTION_ID, w.id)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(other)).and(CATALOG_COLLECTIONS.NAME.eq(otherName)).execute();
            Rdr229ChildNameWorld.chunk(ctx, other, otherName, w.id, w.model, 384, "twin", w.ex[0]);
            return null;
        });
        return w;
    }

    @Test
    void aSecondTenantHoldingTheSameChashAndTheSameCollectionIdDoesNotConfuseTheLookup() throws Exception {
        var w = secondTenantWorld("mt");
        w.run(w.manifestInsert("cn.mt.d0", 0, w.newName, w.ex[0]));
        assertThat(w.manifestNames("cn.mt.d0")).as("the chunk of THIS tenant: its stored name, not the other tenant's").containsExactly(w.oldName);
    }

    @Test
    void droppingTheTenantPredicateFromStep2TurnsTheSecondTenantTestRed() throws Exception {
        var w = secondTenantWorld("mu");
        withMutant(STEP2_TENANT, "c.collection_id = NEW.collection_id", () -> {
            boolean red;
            try {
                w.run(w.manifestInsert("cn.mu.d1", 0, w.newName, w.ex[0]));
                red = !w.manifestNames("cn.mu.d1").equals(List.of(w.oldName));
            } catch (RuntimeException refused) {
                red = true;   // the key refused the other tenant's name
            }
            assertThat(red).as("without the tenant predicate the lookup takes the other tenant's chunk").isTrue();
        });
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
        withMutant(STEP2_MODEL, "AND c.chash = NEW.chash\n     LIMIT 1", () -> {
            boolean red;
            try {
                w.run(w.manifestInsert("cn.mn.d0", 0, w.newName, w.ex[0]));
                red = !w.manifestNames("cn.mn.d0").equals(List.of(w.oldName));
            } catch (RuntimeException refused) {
                red = true;
            }
            assertThat(red).as("without the model predicate the lookup takes the other model's chunk").isTrue();
        });
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
        assertThat(w.chunkNames(w.ex[0]))
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
