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

        assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.naming(ctx, w.tenant(), "chunks", "collection_id", String.valueOf(w.id()))))
            .as("one row for the chash under B's id").isEqualTo(1L);
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
}
