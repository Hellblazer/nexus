// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;

import java.time.Duration;
import java.util.List;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), the rename's stages on one install, in the order the engine really reaches them (see
 * {@link Rdr229RenameRestartPrecondition}): before gate 3, which on a fresh or half-stamped store is the whole of boot 1 (the rename is the name path
 * and rewrites the fourteen tables, as today); then, after the restart, from the gate 3 latch until the name-key drop move completes (refused,
 * naming the move); and a dip of the live gate 3 inside that window (still refused, never the name path). The refusal value {@code restart} is
 * not a stage of this chain: it is reached only when a listed P3.5b changeset fails to apply, which {@link Rdr229P37RestartValueTest} builds.
 *
 * <p>How each stage is reached. Stage 1 is boot 1 (gate 3 cannot hold there: its id-unique-index changesets were skipped by the first walk). For stage 2
 * the engine restarts with the data-move worker not started, so the latch lands and the check sees gate 3 hold, the production pass registers the drop
 * move, and nothing runs it: no {@code completed_at}. Each stage renames a collection of its own, so a rename that the engine wrongly performs in one
 * stage cannot spoil the next.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class Rdr229P37RenameStagesTest {

    /** The tables the name path rewrites that this fixture seeds, by name column. */
    private static final List<Rdr229RenameChecks.Site> REWRITTEN = List.of(
        new Rdr229RenameChecks.Site("chunks", "collection", "collection_id"),
        new Rdr229RenameChecks.Site("catalog_document_chunks", "collection", "collection_id"),
        new Rdr229RenameChecks.Site("topics", "collection", "collection_id"),
        new Rdr229RenameChecks.Site("taxonomy_meta", "collection", "collection_id"),
        new Rdr229RenameChecks.Site("taxonomy_centroids", "collection", "collection_id"),
        new Rdr229RenameChecks.Site("topic_assignments", "source_collection", "source_collection_id"),
        new Rdr229RenameChecks.Site("document_aspects", "collection", "collection_id"),
        new Rdr229RenameChecks.Site("document_highlights", "collection", "collection_id"),
        new Rdr229RenameChecks.Site("catalog_documents", "physical_collection", "physical_collection_id"),
        new Rdr229RenameChecks.Site("relevance_log", "collection", "collection_id"),
        new Rdr229RenameChecks.Site("search_telemetry", "collection", "collection_id"),
        new Rdr229RenameChecks.Site("hook_failures", "collection", "collection_id"));

    private Rdr229RenameEngine engine;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine();
        engine.boot();
        Rdr229RenameRestartPrecondition.awaitStampMovesComplete(engine);
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

    /** The registry and every seeded child still hold the collection under {@code name}, as before the refused rename. */
    private void assertNothingWritten(String tenant, String name, long idBefore, long chunksBefore, String why) {
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, name)))
            .as("a refused rename writes nothing: the registry row is where it was" + why).isEqualTo(idBefore);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.naming(ctx, tenant, "chunks", "collection", name)))
            .as("and the chunks still name the old label" + why).isEqualTo(chunksBefore);
    }

    // ---- stage 1: before gate 3 (boot 1) ------------------------------------------------------------------------------------

    /** Until gate 3 the rename keeps its fourteen-table rewrite (item 6, "Readers during the stamp"); in boot 1 gate 3 cannot hold. */
    @Test
    @Order(10)
    void beforeGate3_aRenameStillDoesTheFourteenTableRewrite() throws Exception {
        String tenant = "p37-w1";
        String token = engine.newTenant(tenant);
        String oldName = coll("p37w1", 1);
        String newName = coll("p37w1", 2);
        engine.su(ctx -> {
            Rdr229RenameSeeds.full(ctx, tenant, oldName);
            return null;
        });
        long idBefore = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));
        assertThat(engine.completion().gate3()).as("non-vacuity: gate 3 does not hold in boot 1").isFalse();
        assertThat(engine.boots()).as("non-vacuity: this is boot 1").isEqualTo(1);

        var r = engine.rename(token, oldName, newName);

        assertThat(r.status()).as("the rename route: " + r).isEqualTo(200);
        for (var site : REWRITTEN) {
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.naming(ctx, tenant, site.table(), site.nameColumn(), oldName)))
                .as(site.table() + " no longer names the old label").isZero();
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.naming(ctx, tenant, site.table(), site.nameColumn(), newName)))
                .as(site.table() + " names the new label: the row was rewritten").isPositive();
        }
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, newName)))
            .as("the name path inserts a new registry row, so the collection has a new id").isNotNull().isNotEqualTo(idBefore);
        assertThat(engine.moveRows(Rdr229RenameEngine.DROP_LABEL))
            .as("nothing registers the name-key drop move before gate 3 (the production registration pass has run throughout)").isZero();
    }

    // ---- stage 2: gate 3 has latched, the name-key drop move has not completed ----------------------------------------------

    @Test
    @Order(20)
    void afterGate3AndBeforeTheNameKeyDropMoveCompletes_aRenameIsRefusedAndTheErrorNamesTheMove() throws Exception {
        engine.workerAtBoot(false);
        engine.restart();   // boot 2: the walk applies vectors-078/084 and P3.5b's changesets; no worker, so no move runs
        assertThat(Rdr229RenameEngine.await(Rdr229RenameRestartPrecondition.LATCH_WAIT, () -> engine.completion().gate3Latched()))
            .as("gate 3 latched in boot 2 [" + engine.completion().gate3Why() + "]").isTrue();
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> engine.moveRows(Rdr229RenameEngine.DROP_LABEL) == 1))
            .as("from the latch the registration pass registers the move: one row, and it stays one").isTrue();
        assertThat(engine.moveCompleted(Rdr229RenameEngine.DROP_LABEL))
            .as("non-vacuity: the name-key drop move has no completed_at (no worker)").isFalse();
        String tenant = "p37-w2";
        String token = engine.newTenant(tenant);
        String oldName = coll("p37w2", 1);
        engine.su(ctx -> {
            Rdr229RenameSeeds.full(ctx, tenant, oldName);
            return null;
        });
        long idBefore = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));
        long chunksBefore = engine.su(ctx -> Rdr229RenameChecks.naming(ctx, tenant, "chunks", "collection", oldName));
        String why = " [readiness " + engine.readiness().map(Object::toString).orElse("unreadable") + "]";

        var r = engine.rename(token, oldName, coll("p37w2", 2));

        assertThat(r.refused()).as("a rename from gate 3 until the move completes is refused 409 rename_refused: " + r + why).isTrue();
        assertThat(r.missing()).as("the error names the move" + why).isEqualTo("name_key_drop_move");
        assertNothingWritten(tenant, oldName, idBefore, chunksBefore, why);
    }

    // ---- stage 3: the live gate 3 dips inside that window -------------------------------------------------------------------

    /**
     * Gate 3 was latched: from then on a rename is never the name path, even if the live gate 3 drops back to false (an id index going INVALID). Before
     * the fix gate 3 false read as "the name path" and the rename ran it.
     */
    @Test
    @Order(30)
    void onceGate3IsLatched_aDipOfTheLiveGate3IsStillRefused_neverTheNamePath() throws Exception {
        assertThat(engine.completion().gate3Latched()).as("non-vacuity: gate 3 is latched").isTrue();
        assertThat(engine.moveCompleted(Rdr229RenameEngine.DROP_LABEL)).as("non-vacuity: the move is still open").isFalse();
        String tenant = "p37-w4";
        String token = engine.newTenant(tenant);
        String oldName = coll("p37w4", 1);
        engine.su(ctx -> {
            Rdr229RenameSeeds.full(ctx, tenant, oldName);
            return null;
        });
        long idBefore = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));
        long chunksBefore = engine.su(ctx -> Rdr229RenameChecks.naming(ctx, tenant, "chunks", "collection", oldName));
        engine.gate3(false);
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(10), () -> !engine.completion().gate3()))
            .as("non-vacuity: the live gate 3 dropped back [" + engine.completion().gate3Why() + "]").isTrue();
        assertThat(engine.completion().gate3Latched()).as("while the latch holds").isTrue();
        String why = " [readiness " + engine.readiness().map(Object::toString).orElse("unreadable") + "]";

        var r = engine.rename(token, oldName, coll("p37w4", 2));

        assertThat(r.refused()).as("gate 3 false after the latch is not the name path: refused " + r + why).isTrue();
        assertThat(r.missing()).as("naming the move, as before the dip" + why).isEqualTo("name_key_drop_move");
        assertNothingWritten(tenant, oldName, idBefore, chunksBefore, why);
    }
}
