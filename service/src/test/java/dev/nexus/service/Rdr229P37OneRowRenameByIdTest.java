// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.CollectionIdMismatchException;
import dev.nexus.service.db.CollectionOneRowRename;
import dev.nexus.service.db.CollectionRetiredIdException;
import dev.nexus.service.db.OneRowRename;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), the one-row rename of a source named by {@code collection_id}. The route boundary resolves the id to a label OUTSIDE the
 * rename's transaction, and the one-row body locks the registry rows and reads the source BY LABEL; a label freed and registered again in between must not
 * make it rename the new holder. The body therefore locks the id's row with the others and refuses, before it writes anything, an id that is unknown or
 * retired or that no longer bears the label ({@code CollectionRefusals.requireSourceById}).
 *
 * <p>One booted engine for the class, taken through {@link Rdr229RenameRestartPrecondition} (gate 3's latch set, the name-key drop move run, P3.5b's changesets
 * applied). The race is staged with the route boundary's {@code afterResolve} seam: it runs after the id was resolved to its label and before the handler, so
 * the registry is changed exactly where a concurrent request would change it. Each refusal test then asserts the registry rows are byte-for-byte as the seam left
 * them (id, label, owner, successor, xmin): nothing was written, the new holder of the label is untouched. A retired id, and a label freed and not registered
 * again, cannot be staged through the route, so those two drive the body as the rename transaction calls it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37OneRowRenameByIdTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

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

    @AfterEach
    void clearSeam() {
        engine.service().collectionSelectorFilter().testHooks(null, null, null, null);
    }

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    private static final java.util.concurrent.atomic.AtomicInteger SEQ = new java.util.concurrent.atomic.AtomicInteger();

    /** A fresh tenant holding one registry row, A, and the id it was given. */
    private record World(String tenant, String token, String a, String ownerA, long idA) { }

    private World world(String tag) throws Exception {
        int n = SEQ.incrementAndGet();
        String tenant = "p37-byid-" + tag + n;
        String token = engine.newTenant(tenant);
        String ownerA = "p37id" + tag + n + "a";
        String a = coll(ownerA, 1);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, a);
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        return new World(tenant, token, a, ownerA, id);
    }

    /** Every registry row of the tenant: id, label, owner, successor and xmin (which changes when the row is rewritten), sorted. */
    private List<String> registry(String tenant) {
        return engine.su(ctx -> {
            var cc = CATALOG_COLLECTIONS;
            var xmin = DSL.field(DSL.name("xmin"), String.class);
            return ctx.select(cc.COLLECTION_ID, cc.NAME, cc.OWNER_ID, cc.SUPERSEDED_BY, xmin).from(cc).where(cc.TENANT_ID.eq(tenant))
                .orderBy(cc.COLLECTION_ID).fetch(r -> r.value1() + "|" + r.value2() + "|" + r.value3() + "|" + r.value4() + "|" + r.value5());
        });
    }

    /** Runs {@code staging} after the route boundary resolved the id and before the handler, once per request; the registry it leaves is {@code afterStaging}. */
    private void stage(java.util.function.Consumer<DSLContext> staging, List<String>[] afterStaging, String tenant) {
        engine.service().collectionSelectorFilter().testHooks(() -> {
            engine.su(ctx -> {
                staging.accept(ctx);
                return null;
            });
            afterStaging[0] = registry(tenant);
        }, null, null, null);
    }

    // ---- (a) ---------------------------------------------------------------------------------------------------------------

    @Test
    void aRenameByCollectionId_succeedsAndTheRowKeepsItsId() throws Exception {
        World w = world("a");
        String newName = coll(w.ownerA() + "x", 2);

        var r = engine.renameById(w.token(), w.idA(), newName, Map.of("content_type", "knowledge", "owner_id", w.ownerA() + "x"));

        assertThat(r.status()).as("the rename route, by id: " + r).isEqualTo(200);
        assertThat(MAPPER.readTree(r.body()).path("collection_id").asLong(-1)).as("the answer names the renamed row's id").isEqualTo(w.idA());
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), newName))).as("the row is renamed in place").isEqualTo(w.idA());
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), w.a()))).as("nothing is left at the old label").isNull();
    }

    // ---- (b) ---------------------------------------------------------------------------------------------------------------

    @Test
    void theLabelFreedAndRegisteredAgainBetweenResolveAndTransaction_isRefusedAs409Mismatch_andNothingIsWritten() throws Exception {
        World w = world("b");
        String a2 = coll(w.ownerA() + "away", 1);
        String target = coll(w.ownerA() + "to", 2);
        @SuppressWarnings("unchecked") List<String>[] staged = new List[1];
        // between the id's resolution and the rename: A (id 1) is renamed away to A2 and a new collection is registered under A (id 2)
        stage(ctx -> {
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, a2).set(CATALOG_COLLECTIONS.OWNER_ID, w.ownerA() + "away")
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(w.tenant())).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(w.idA())).execute();
            PgContainerHelper.insertCollection(ctx, w.tenant(), w.a());
        }, staged, w.tenant());

        var r = engine.renameById(w.token(), w.idA(), target, Map.of());

        assertThat(staged[0]).as("non-vacuity: the seam ran: the first collection was renamed away and the label A has a new holder")
            .hasSize(2).anyMatch(row -> row.startsWith(w.idA() + "|" + a2 + "|")).anyMatch(row -> row.contains("|" + w.a() + "|") && !row.startsWith(w.idA() + "|"));
        assertThat(r.status()).as("a request by id 1 for the label A, which id 1 no longer bears: " + r).isEqualTo(409);
        assertThat(r.reason()).as(r.toString()).isEqualTo("collection_id_mismatch");
        var body = MAPPER.readTree(r.body());
        assertThat(body.path("collection_id").asLong(-1)).as(r.toString()).isEqualTo(w.idA());
        assertThat(body.path("requested_label").asText()).as(r.toString()).isEqualTo(w.a());
        assertThat(body.path("current_label").asText()).as(r.toString()).isEqualTo(a2);
        assertThat(registry(w.tenant())).as("nothing was written: the new holder of A was not renamed, and neither row changed").isEqualTo(staged[0]);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), target))).as("no row bears the rename's target").isNull();
    }

    // ---- (c) ---------------------------------------------------------------------------------------------------------------

    @Test
    void anIdWhoseRowIsGoneBetweenResolveAndTransaction_isRefusedAs404Unknown_andNothingIsWritten() throws Exception {
        World w = world("c");
        String target = coll(w.ownerA() + "to", 2);
        @SuppressWarnings("unchecked") List<String>[] staged = new List[1];
        // the row of id 1 is deleted and the label A is registered again (id 2)
        stage(ctx -> {
            ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(w.tenant()))
                .and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(w.idA())).execute();
            PgContainerHelper.insertCollection(ctx, w.tenant(), w.a());
        }, staged, w.tenant());

        var r = engine.renameById(w.token(), w.idA(), target, Map.of());

        assertThat(staged[0]).as("non-vacuity: the seam ran, and only the new A is left").hasSize(1);
        assertThat(r.status()).as("an id no row has: " + r).isEqualTo(404);
        assertThat(r.reason()).as(r.toString()).isEqualTo("unknown_collection_id");
        assertThat(MAPPER.readTree(r.body()).path("collection_id").asLong(-1)).as(r.toString()).isEqualTo(w.idA());
        assertThat(registry(w.tenant())).as("nothing was written: the new holder of A was not renamed").isEqualTo(staged[0]);
    }

    // ---- (d) and (e): the body called as the rename transaction calls it ---------------------------------------------------------
    // A retired id never reaches the body through the route (the handler reads the label's row first and refuses a retired source itself), and a label
    // that is freed and NOT registered again is a 404 from the handler's own read; both are in the window only between the handler's read and the
    // transaction, which the seam cannot stage, so the body is driven directly in a transaction of its own, as RenameGuard#oneRow calls it.

    private void bodyRefused(World w, long id, String label, String target, Class<? extends RuntimeException> refusal,
                             java.util.function.Consumer<RuntimeException> details) {
        List<String> before = registry(w.tenant());
        RuntimeException thrown = null;
        try {
            engine.scope().withTenant(w.tenant(), ctx -> new CollectionOneRowRename().rename(ctx, w.tenant(), label, target,
                new OneRowRename.Request(id, null, null, new String[5], c -> false)));
        } catch (RuntimeException e) {
            thrown = e;
        }
        assertThat(thrown).as("the one-row body refused the stale pair").isInstanceOf(refusal);
        details.accept(thrown);
        assertThat(registry(w.tenant())).as("nothing was written").isEqualTo(before);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), target))).as("no row bears the rename's target").isNull();
    }

    @Test
    void anIdRetiredBeforeTheTransaction_isRefusedAsRetired_namingTheSuccessor_andNothingIsWritten() throws Exception {
        World w = world("d");
        String successor = coll(w.ownerA() + "succ", 1);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, w.tenant(), successor);
            return null;
        });
        long successorId = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), successor));
        engine.su(ctx -> {
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor).set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, successorId)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_AT, OffsetDateTime.now())
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(w.tenant())).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(w.idA())).execute();
            return null;
        });

        bodyRefused(w, w.idA(), w.a(), coll(w.ownerA() + "to", 2), CollectionRetiredIdException.class, e -> {
            var retired = (CollectionRetiredIdException) e;
            assertThat(retired.collectionId()).isEqualTo(w.idA());
            assertThat(retired.successor()).as("the live successor, found by id").hasValueSatisfying(s -> {
                assertThat(s.id()).isEqualTo(successorId);
                assertThat(s.label()).isEqualTo(successor);
            });
        });
    }

    @Test
    void aLabelFreedAndNotRegisteredAgain_isRefusedAsMismatchNamingTheCurrentLabel_notAsNotLive() throws Exception {
        World w = world("e");
        String a2 = coll(w.ownerA() + "away", 1);
        engine.su(ctx -> {
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, a2).set(CATALOG_COLLECTIONS.OWNER_ID, w.ownerA() + "away")
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(w.tenant())).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(w.idA())).execute();
            return null;
        });

        bodyRefused(w, w.idA(), w.a(), coll(w.ownerA() + "to", 2), CollectionIdMismatchException.class, e -> {
            var mismatch = (CollectionIdMismatchException) e;
            assertThat(mismatch.labelDisagreement()).isTrue();
            assertThat(mismatch.requestedLabel()).isEqualTo(w.a());
            assertThat(mismatch.currentLabel()).isEqualTo(a2);
        });
    }
}
