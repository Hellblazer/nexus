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
 * RDR-229 P3.7 (nexus-p2m1x.27), the rename's three stages on one install, in the order they come: before gate 3 (the rename is
 * the name path and rewrites the fourteen tables, as today), from gate 3 until the name-key drop move completes (refused, naming
 * the move) and from that move's completion until a restart applies P3.5b's changesets (refused, naming the restart). One booted
 * engine, boot 1 only: the restart is the very thing the last stage waits for, so this class never takes it
 * ({@link Rdr229FreshInstallRenameRestartTest} does).
 *
 * <p>How each stage is reached. Gate 3's input is the engine's simulated P3.4 answer ({@link Rdr229RenameEngine#gate3}): closed
 * for stage 1. For stage 2 the data-move worker is stopped and the input opened, so the check sees gate 3 hold and whatever
 * registers the drop move can register it, but nothing runs it: no {@code completed_at}. For stage 3 the worker runs again and the
 * test waits (bounded) for the move's {@code completed_at}; the changesets are still unapplied because this is still boot 1.
 * Each stage renames a collection of its own, so a rename that the engine wrongly performs in one stage cannot spoil the next.
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
        engine = new Rdr229RenameEngine().gate3(false);
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

    // ---- stage 1: before gate 3 ---------------------------------------------------------------------------------------------

    /** Passes today and must keep passing: until gate 3 the rename keeps its fourteen-table rewrite (item 6, "Readers during the stamp"). */
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
        assertThat(engine.completion().gate3()).as("non-vacuity: gate 3 does not hold").isFalse();

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
            .as("nothing registers the name-key drop move before gate 3 (the harness's registrar has run throughout)").isZero();
    }

    // ---- stage 2: gate 3 holds, the name-key drop move has not completed -----------------------------------------------------

    @Test
    @Order(20)
    void afterGate3AndBeforeTheNameKeyDropMoveCompletes_aRenameIsRefusedAndTheErrorNamesTheMove() throws Exception {
        engine.stopWorker();
        engine.gate3(true);
        String tenant = "p37-w2";
        String token = engine.newTenant(tenant);
        String oldName = coll("p37w2", 1);
        engine.su(ctx -> {
            Rdr229RenameSeeds.full(ctx, tenant, oldName);
            return null;
        });
        long idBefore = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));
        long chunksBefore = engine.su(ctx -> Rdr229RenameChecks.naming(ctx, tenant, "chunks", "collection", oldName));
        assertThat(engine.completion().gate3()).as("non-vacuity: gate 3 holds [" + engine.completion().gate3Why() + "]").isTrue();
        assertThat(engine.moveCompleted(Rdr229RenameEngine.DROP_LABEL))
            .as("non-vacuity: the name-key drop move has no completed_at (the worker is stopped)").isFalse();
        assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> engine.moveRows(Rdr229RenameEngine.DROP_LABEL) == 1))
            .as("from gate 3 the registration operation registers the move: one row, and it stays one").isTrue();
        String why = " [readiness " + engine.readiness().map(Object::toString).orElse("unreadable") + "]";

        var r = engine.rename(token, oldName, coll("p37w2", 2));

        assertThat(r.refused()).as("a rename from gate 3 until the move completes is refused 409 rename_refused: " + r + why).isTrue();
        assertThat(r.missing()).as("the error names the move" + why).isEqualTo("name_key_drop_move");
        assertNothingWritten(tenant, oldName, idBefore, chunksBefore, why);
    }

    // ---- stage 3: the move completed, no restart has applied P3.5b's changesets ---------------------------------------------

    @Test
    @Order(30)
    void afterTheMoveCompletesAndBeforeEveryP35bChangesetHasApplied_aRenameIsRefusedAndTheErrorNamesTheRestart() throws Exception {
        String tenant = "p37-w3";
        String token = engine.newTenant(tenant);
        String oldName = coll("p37w3", 1);
        engine.su(ctx -> {
            Rdr229RenameSeeds.full(ctx, tenant, oldName);
            return null;
        });
        long idBefore = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, oldName));
        long chunksBefore = engine.su(ctx -> Rdr229RenameChecks.naming(ctx, tenant, "chunks", "collection", oldName));
        engine.startWorker();
        // soft: before P3.7 nothing registers the move, so this wait times out and the rename below fails on its behaviour
        boolean moved = Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> engine.moveCompleted(Rdr229RenameEngine.DROP_LABEL));
        String why = " [drop move " + (moved ? "completed" : "never completed within 30 s") + "; readiness "
            + engine.readiness().map(Object::toString).orElse("unreadable") + "]";

        var r = engine.rename(token, oldName, coll("p37w3", 2));

        assertThat(r.refused()).as("a rename after the move and before the restart is refused 409 rename_refused: " + r + why).isTrue();
        assertThat(r.missing()).as("the error names the restart" + why).isEqualTo("restart");
        assertNothingWritten(tenant, oldName, idBefore, chunksBefore, why);
        assertThat(engine.moveRows(Rdr229RenameEngine.DROP_LABEL)).as("the registration is idempotent: still one row").isEqualTo(1);
    }

    // ---- stage 4: the move completed and gate 3 dips ------------------------------------------------------------------------

    /**
     * Gate 3 was latched (the drop move has completed, the name keys are gone): from then on a rename is never the name path, even if the
     * live gate 3 drops back to false (an id index going INVALID). Here P3.5b's changesets are still unapplied (boot 1), so it is refused naming the restart; before
     * the fix gate 3 false read as "the name path" and the rename ran it.
     */
    @Test
    @Order(40)
    void onceGate3IsLatched_aDipOfTheLiveGate3IsStillRefused_neverTheNamePath() throws Exception {
        assertThat(engine.moveCompleted(Rdr229RenameEngine.DROP_LABEL)).as("non-vacuity: the move completed in stage 3").isTrue();
        assertThat(engine.completion().gate3Latched()).as("non-vacuity: gate 3 was latched when it held in stage 2").isTrue();
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

        assertThat(r.refused()).as("gate 3 false after the move completed is not the name path: refused " + r + why).isTrue();
        assertThat(r.missing()).as("naming the restart, as before the dip" + why).isEqualTo("restart");
        assertNothingWritten(tenant, oldName, idBefore, chunksBefore, why);
    }
}
