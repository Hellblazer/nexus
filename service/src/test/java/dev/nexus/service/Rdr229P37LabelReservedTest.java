// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.LabelReserved;
import dev.nexus.service.db.LabelReservedException;
import dev.nexus.service.vectors.PgVectorRepository;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27; linda 2026-11-23, addenda 8 and 8a): after a one-row rename A to B, B's rows in the name-keyed tables still carry the
 * name A. Until P4.1 the label A is RESERVED: registering it, making it the successor of a supersede, or renaming another collection onto it is refused with a
 * typed 409 {@code label_reserved}, so a new collection named A can never meet B's rows on a name key. The collection that was renamed away
 * from the label may take it back.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37LabelReservedTest {

    private static final String MODEL = "minilm-l6-v2-384";

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

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__" + MODEL + "__v" + version;
    }

    private static final java.util.concurrent.atomic.AtomicInteger SEQ = new java.util.concurrent.atomic.AtomicInteger();

    /** A fresh tenant holding a collection {@code a} with one chunk, renamed (one-row) to {@code b}. */
    private record World(String tenant, String token, String a, String b, long id, String chash) { }

    private World renamed(String tag) throws Exception {
        int n = SEQ.incrementAndGet();
        String tenant = "p37-lr-" + tag + n;
        String token = engine.newTenant(tenant);
        String a = coll("p37lr" + tag + n + "a", 1);
        String b = coll("p37lr" + tag + n + "b", 1);
        String chash = dev.nexus.service.db.Chash.ofText("p37 label reserved " + tag + n).toHex();
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, a);
            PgContainerHelper.insertChunks(ctx, tenant, a, List.of(chash), List.of("text of " + tag), List.of(new float[384]), List.of(Map.of()));
            return null;
        });
        long id = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, a));
        var r = engine.rename(token, a, b);
        assertThat(r.status()).as("the rename route: " + r).isEqualTo(200);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, b))).as("non-vacuity: one-row, the id is kept").isEqualTo(id);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.naming(ctx, tenant, "chunks", "collection", a)))
            .as("non-vacuity: the chunk of B still carries the name A").isEqualTo(1L);
        return new World(tenant, token, a, b, id, chash);
    }

    private java.net.http.HttpResponse<String> upsert(World w, String name, String owner) throws Exception {
        return engine.call("POST", "/v1/catalog/collections/upsert", w.token(),
            "{\"name\":\"" + name + "\",\"content_type\":\"knowledge\",\"owner_id\":\"" + owner + "\",\"embedding_model\":\"" + MODEL
                + "\",\"model_version\":\"v1\"}");
    }

    @Test
    void afterARenameAToB_registeringAIsRefusedLabelReserved_aFreshLabelIsNot() throws Exception {
        var w = renamed("reg");

        var refused = upsert(w, w.a(), "someone-else");

        assertThat(refused.statusCode()).as(refused.body()).isEqualTo(409);
        var body = Rdr229RenameEngine.MAPPER.readTree(refused.body());
        assertThat(body.path("reason").asText()).as("a typed refusal").isEqualTo("label_reserved");
        assertThat(body.path("label").asText()).isEqualTo(w.a());
        assertThat(body.path("error").asText()).as("naming P4.1").contains("P4.1");
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), w.a()))).as("nothing was registered under A").isNull();

        var fresh = upsert(w, coll("p37lrfresh" + SEQ.incrementAndGet(), 1), "someone-else");
        assertThat(fresh.statusCode()).as("control: an unreserved label registers: " + fresh.body()).isEqualTo(200);
    }

    /**
     * B's write of the chash that A's old name still labels. The id-keyed upsert is P3.5c's (elsinore): on a base without it the write meets the id key through
     * the name arbiter and this leg is red.
     */
    @Test
    void afterARenameAToB_bWritesTheSameChashAndItSucceeds() throws Exception {
        var w = renamed("bw");
        var embedder = new AtomicWriteTestBase.CountingFakeEmbedder();
        var repo = new PgVectorRepository(engine.scope(), embedder, embedder, () -> true);

        repo.upsertChunks(w.tenant(), w.b(), List.of(w.chash()), List.of("rewritten text"), List.of(Map.of()), true);

        long underB = engine.su(ctx -> (long) ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.CHUNKS,
            dev.nexus.service.jooq.nexus.Tables.CHUNKS.TENANT_ID.eq(w.tenant()).and(dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION_ID.eq(w.id()))));
        assertThat(underB).as("one row for the chash under B's id").isEqualTo(1L);
    }

    @Test
    void renamingAnotherCollectionOntoTheReservedLabelIsRefused_butTheCollectionThatLeftItMayTakeItBack() throws Exception {
        var w = renamed("rn");
        String c = coll("p37lrrnc" + SEQ.incrementAndGet(), 1);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, w.tenant(), c);
            return null;
        });

        var refused = engine.rename(w.token(), c, w.a());

        assertThat(refused.status()).as(refused.toString()).isEqualTo(409);
        assertThat(refused.reason()).isEqualTo("label_reserved");
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), c))).as("C is where it was").isNotNull();

        var back = engine.rename(w.token(), w.b(), w.a());

        assertThat(back.status()).as("B may take its old label back: " + back).isEqualTo(200);
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), w.a()))).as("the same collection").isEqualTo(w.id());
    }

    @Test
    void supersedingToTheReservedLabelIsRefused() throws Exception {
        var w = renamed("sup");
        String c = coll("p37lrsupc" + SEQ.incrementAndGet(), 1);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, w.tenant(), c);
            return null;
        });
        var repo = new CatalogRepository(engine.scope());

        assertThatThrownBy(() -> repo.supersedeCollection(w.tenant(), c, w.a(), ""))
            .as("the successor's label is reserved").isInstanceOf(LabelReservedException.class);
        assertThat(engine.<Boolean>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), c) != null)).as("C is untouched").isTrue();
    }

    @Test
    void theCheckAnswersFromTheRowsAndTheLiveHolder() throws Exception {
        var w = renamed("chk");
        engine.su(ctx -> {
            assertThat(LabelReserved.isReserved(ctx, w.tenant(), w.a(), null)).as("A: B's rows carry it and A has no live holder").isTrue();
            assertThat(LabelReserved.isReserved(ctx, w.tenant(), w.a(), w.id())).as("the collection that left may take it").isFalse();
            assertThat(LabelReserved.isReserved(ctx, w.tenant(), w.b(), null)).as("B: no row carries it").isFalse();
            assertThat(LabelReserved.isReserved(ctx, "another-tenant", w.a(), null)).as("a tenant's rows only").isFalse();
            return null;
        });
    }

    // ---- linda's ruling after folio's review of 19d19081dc: the definition and its controls ---------------------------------------

    private java.net.http.HttpResponse<String> supersede(String token, String name, String successor) throws Exception {
        return engine.call("POST", "/v1/catalog/collections/supersede", token, "{\"name\":\"" + name + "\",\"superseded_by\":\"" + successor + "\"}");
    }

    /** Reviving a populated tombstone is not refused: the tombstone's own rows resolve to its own registry row. */
    @Test
    void revivingAPopulatedTombstoneSucceeds() throws Exception {
        int n = SEQ.incrementAndGet();
        String tenant = "p37-lr-rv" + n;
        String token = engine.newTenant(tenant);
        String t = coll("p37lrrvt" + n, 1);
        String u = coll("p37lrrvu" + n, 1);
        String chash = dev.nexus.service.db.Chash.ofText("revive " + n).toHex();
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, t);
            PgContainerHelper.insertChunks(ctx, tenant, t, List.of(chash), List.of("populated"), List.of(new float[384]), List.of(Map.of()));
            PgContainerHelper.insertCollection(ctx, tenant, u);
            return null;
        });
        var sup = supersede(token, t, u);
        assertThat(sup.statusCode()).as("non-vacuity: T is a populated tombstone: " + sup.body()).isEqualTo(200);

        var revive = engine.call("POST", "/v1/catalog/collections/upsert", token,
            "{\"name\":\"" + t + "\",\"content_type\":\"knowledge\",\"owner_id\":\"p37lrrvt" + n + "\",\"embedding_model\":\"" + MODEL
                + "\",\"model_version\":\"v1\"}");

        assertThat(revive.statusCode()).as("the revive is not refused label_reserved: " + revive.body()).isEqualTo(200);
    }

    /** Delete a collection and recreate its label: history rows (chash_remap, a failed re-embed job) and a dangling id reserve nothing. */
    @Test
    void deletingACollectionAndRecreatingItsLabelSucceeds_withChashRemapAndReembedHistoryLeft() throws Exception {
        int n = SEQ.incrementAndGet();
        String tenant = "p37-lr-dl" + n;
        String token = engine.newTenant(tenant);
        String d = coll("p37lrdld" + n, 1);
        String e = coll("p37lrdle" + n, 1);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, d);
            PgContainerHelper.insertCollection(ctx, tenant, e);
            CollectionIdSites.seedUnexported(ctx, tenant, d, e);   // pdf_pipeline, chash_remap, reembed_jobs (planned), canary ring, logs: all naming D
            ctx.update(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "reembed_jobs")))
                .set(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("state"), String.class), "aborted")
                .where(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("tenant_id"), String.class).eq(tenant)).execute();   // history, not a lock state
            return null;
        });
        var del = engine.call("POST", "/v1/catalog/collections/delete", token, "{\"name\":\"" + d + "\"}");
        assertThat(del.statusCode()).as("non-vacuity: D is deleted: " + del.body()).isEqualTo(200);
        assertThat(engine.<Long>su(ctx -> (long) ctx.fetchCount(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "chash_remap")),
            org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("tenant_id"), String.class).eq(tenant)
                .and(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("source_collection"), String.class).eq(d)))))
            .as("non-vacuity: the chash_remap history of D is left").isPositive();

        var again = upsert(new World(tenant, token, d, e, 0L, ""), d, "someone-else");

        assertThat(again.statusCode()).as("the label is free again: " + again.body()).isEqualTo(200);
    }

    /** Supersede onto a LIVE successor is not refused. */
    @Test
    void supersedingOntoALiveSuccessorIsNotRefused() throws Exception {
        var w = renamed("ls");
        String c = coll("p37lrlsc" + SEQ.incrementAndGet(), 1);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, w.tenant(), c);
            return null;
        });
        var repo = new CatalogRepository(engine.scope());

        int n = repo.supersedeCollection(w.tenant(), c, w.b(), "");

        assertThat(n).as("B is live: the supersede goes through").isEqualTo(1);
    }

    /** A pdf_pipeline row that carries the old label under the renamed collection reserves it, as the chunks do. */
    @Test
    void aOneRowRenameWhereBHasAPdfPipelineRow_thenRegisteringAIsRefused() throws Exception {
        int n = SEQ.incrementAndGet();
        String tenant = "p37-lr-pdf" + n;
        String token = engine.newTenant(tenant);
        String a = coll("p37lrpdfa" + n, 1);
        String b = coll("p37lrpdfb" + n, 1);
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, a);   // no chunk, no canary row: only the pdf_pipeline row carries the label
            long id = Rdr229RenameChecks.registryId(ctx, tenant, a);
            var now = java.time.OffsetDateTime.now();
            ctx.insertInto(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "pdf_pipeline")),
                    org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("tenant_id"), String.class), org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("content_hash"), String.class),
                    org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("pdf_path"), String.class), org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("collection"), String.class),
                    org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("collection_id"), Long.class), org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("started_at"), java.time.OffsetDateTime.class),
                    org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("updated_at"), java.time.OffsetDateTime.class))
                .values(tenant, "h1", "a.pdf", a, id, now, now).execute();
            return null;
        });
        var r = engine.rename(token, a, b);
        assertThat(r.status()).as("the one-row rename: " + r).isEqualTo(200);

        var refused = upsert(new World(tenant, token, a, b, 0L, ""), a, "someone-else");

        assertThat(refused.statusCode()).as("pdf_pipeline is probed: " + refused.body()).isEqualTo(409);
        assertThat(refused.body()).contains("label_reserved");
    }

    /** The pre-latch control: no latch row, no reservation, whatever rows carry a stale name. With the row written, the same rows reserve the label. */
    @Test
    void beforeTheLatch_nothingIsReserved_afterTheRowTheSameRowsReserve() throws Exception {
        try (var pre = new Rdr229RenameEngine().gate3(false)) {
            pre.boot();
            Rdr229RenameRestartPrecondition.awaitStampMovesComplete(pre);
            String tenant = "p37-lr-pre";
            pre.newTenant(tenant);
            String a = coll("p37lrprea", 1);
            String b = coll("p37lrpreb", 1);
            String chash = dev.nexus.service.db.Chash.ofText("pre latch").toHex();
            pre.su(ctx -> {
                PgContainerHelper.insertCollection(ctx, tenant, a);
                PgContainerHelper.insertCollection(ctx, tenant, b);
                PgContainerHelper.insertChunks(ctx, tenant, a, List.of(chash), List.of("t"), List.of(new float[384]), List.of(Map.of()));
                // the state a one-row rename A to B leaves: the chunk still named A, under B's id; A has no registry row of its own
                long idB = Rdr229RenameChecks.registryId(ctx, tenant, b);
                ctx.update(dev.nexus.service.jooq.nexus.Tables.CHUNKS).set(dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION_ID, idB)
                    .where(dev.nexus.service.jooq.nexus.Tables.CHUNKS.TENANT_ID.eq(tenant)).and(dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION.eq(a)).execute();
                // a registry row with no row of its own can exist only once the name keys are gone (the drop move's effect, as ImportTestEnv#startOneRow does it)
                var keys = new java.util.ArrayList<>(dev.nexus.service.datamove.NameKeyDropMove.NAME_KEYS);
                keys.add(dev.nexus.service.datamove.NameKeyDropMove.QUEUE_KEY);
                for (var k : keys) {
                    ctx.alterTableIfExists(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", k.table()))).dropConstraintIfExists(org.jooq.impl.DSL.name(k.constraint())).execute();
                }
                ctx.deleteFrom(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS)
                    .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME.eq(a))).execute();
                return null;
            });
            assertThat(pre.<Boolean>su(ctx -> dev.nexus.service.datamove.CompletionCheck.readLatch(ctx, dev.nexus.service.datamove.CompletionCheck.GATE3).isEmpty()))
                .as("non-vacuity: no latch row").isTrue();
            assertThat(pre.<Boolean>su(ctx -> LabelReserved.isReserved(ctx, tenant, a, null))).as("pre-latch: not reserved").isFalse();

            pre.su(ctx -> {
                dev.nexus.service.datamove.CompletionCheck.insertLatch(ctx, dev.nexus.service.datamove.CompletionCheck.GATE3, "test", false);
                return null;
            });

            assertThat(pre.<Boolean>su(ctx -> LabelReserved.isReserved(ctx, tenant, a, null))).as("after the latch row: the same rows reserve A").isTrue();
        }
    }

    // ---- import (folio's review SB2) -----------------------------------------------------------------------------------------------

    @Test
    void afterARenameAToB_importingACollectionNamedAIsRefusedLabelReserved_aFreshLabelImports() throws Exception {
        var w = renamed("imp");
        String fresh = coll("p37lrimpf" + SEQ.incrementAndGet(), 1);
        String rows = "{\"rows\":[{\"name\":\"" + w.a() + "\",\"content_type\":\"knowledge\",\"owner_id\":\"x\",\"embedding_model\":\"" + MODEL
            + "\",\"model_version\":\"v1\"},{\"name\":\"" + fresh + "\",\"content_type\":\"knowledge\",\"owner_id\":\"y\",\"embedding_model\":\"" + MODEL
            + "\",\"model_version\":\"v1\"}]}";

        var r = engine.call("POST", "/v1/catalog/import/collection", w.token(), rows);

        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        var body = Rdr229RenameEngine.MAPPER.readTree(r.body());
        assertThat(body.path("imported").asInt()).as("the fresh row imported, the reserved one did not").isEqualTo(1);
        assertThat(body.path("refused").toString()).as("the reserved label is a refused row of the report").contains(w.a()).contains("label_reserved");
        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, w.tenant(), w.a()))).as("nothing registered under A").isNull();
    }

    @Test
    void theSingleFormImportOfAReservedLabelIsRefused() throws Exception {
        var w = renamed("imp1");
        var repo = new CatalogRepository(engine.scope());

        assertThatThrownBy(() -> repo.importCollection(w.tenant(), Map.of("name", w.a(), "content_type", "knowledge", "owner_id", "x",
            "embedding_model", MODEL, "model_version", "v1"))).isInstanceOf(LabelReservedException.class);
    }
}
