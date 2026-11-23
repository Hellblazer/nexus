// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), vectors-079's step 2 picks the RIGHT chunk when others look alike (review S3b): the same chash and the
 * same collection id held by a second tenant (also run against a mutant of the function with the tenant predicate dropped from step 2: it must go
 * red, so the predicate is load-bearing; the mutant is installed over the real function and the real text restored after). The two look-alikes of
 * the pre-P3.3c base, the same chash and id under a second embedding model and, after a one-row rename, a re-index writing one chash under both
 * labels, are refused by P3.3c's keys on this base: those tests show the refusal.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37ChildNameMismatchTest {

    private static final String STEP2_TENANT = "c.tenant_id = NEW.tenant_id AND c.collection_id = NEW.collection_id";

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

    /**
     * On this base (P3.3c) a chunk's composite key (tenant, collection_id, embedding_model) must exist in the registry, and a collection id has one model,
     * so the look-alike a second model would give step 2 cannot be written: the database refuses it. The model predicate of step 2 stays as belt.
     */
    @Test
    void aSecondEmbeddingModelHoldingTheSameChashAndIdCannotBeWritten_theCompositeKeyRefusesIt() {
        assertThatThrownBy(() -> secondModelWorld("mm"))
            .as("the look-alike is unreachable: P3.3c's composite key into the registry refuses a chunk of another model under the same id")
            .hasMessageContaining("is not present in table \"catalog_collections\"");
    }

    // ---- a re-index that wrote one chash under both labels ----------------------------------------------------------------------

    /**
     * Before P3.3c a re-index after a one-row rename could write the same chash under the NEW label beside the old-named row, with one id: two
     * rows, and step 2 had to pick by the supplied name. The chunks' id key (tenant, collection_id, chash, embedding_model) now refuses that second row,
     * so there is one chunk and every writer, whatever label it supplies, takes the label of that chunk.
     */
    @Test
    void afterARenameAReindexCannotWriteTheSameChashUnderTheNewLabelWithOneId_andEveryWriterTakesTheLabelOfTheOneChunk() throws Exception {
        var w = Rdr229ChildNameWorld.renamed(engine, "dup", Rdr229ChildNameWorld.MODEL_384, 384);
        assertThatThrownBy(() -> engine.su(ctx -> {
            Rdr229ChildNameWorld.chunk(ctx, w.tenant, w.newName, w.id, w.model, w.dim, "dup", w.ex[0]);
            return null;
        })).as("the id key refuses a second row for one chash under one id").hasMessageContaining("duplicate key value violates unique constraint");
        assertThat(w.chunkNames(w.ex[0])).as("non-vacuity: one chunk row, under the old label").containsExactly(w.oldName);

        w.run(w.manifestInsert("cn.dup.d0", 0, w.newName, w.ex[0]));
        w.run(w.manifestInsert("cn.dup.d1", 0, w.oldName, w.ex[0]));

        assertThat(w.manifestNames("cn.dup.d0")).as("a writer under the new label takes the chunk's label").containsExactly(w.oldName);
        assertThat(w.manifestNames("cn.dup.d1")).containsExactly(w.oldName);
        assertThat(w.childrenNotOnTheirChunksName()).isZero();
    }
}
