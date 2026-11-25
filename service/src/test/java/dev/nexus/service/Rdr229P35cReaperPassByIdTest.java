// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.ChunkReaper.CollectionResult;
import dev.nexus.service.ChunkReaper.Refusal;
import dev.nexus.service.ChunkReaper.RunResult;
import dev.nexus.service.ChunkReaper.Settings;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.ReaperRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), item 6, linda's ruling on tamsin's audit: with gate 3 latched, the reaper's pass lists
 * every distinct {@code collection_id} the tenant's chunks hold, and names each by the registry row's current name (by id, no
 * live filter), or, for an id no registry row holds, by the stored {@code chunks.collection} of that id's rows. The id path
 * used to drop a dangling id and, through an inner join, could drop any collection whose label the registry no longer had.
 *
 * <p>The one-row rename is simulated the way P3.7 does it: the registry name keys are dropped first, the registry row's
 * name is updated in place with its id kept, and the chunk rows keep the old name beside the id.
 */
class Rdr229P35cReaperPassByIdTest extends AtomicWriteTestBase {

    private static final Gate3 LATCHED = () -> true;
    private static final int DIM = 768;
    private static final String MODEL = "bge-base-en-v15-768";
    private static final Clock CLOCK = Clock.fixed(Instant.parse("2026-10-01T12:00:00Z"), ZoneOffset.UTC);

    @BeforeAll
    void dropTheNameKeys() throws Exception {
        su(ctx -> assertThat(ByIdRestoreSupport.dropRegistryNameKeys(ctx))
            .as("non-vacuity: the registry's name keys exist and are dropped").isNotEmpty());
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            PgContainerHelper.dropCatalogChunksFk(c);
        }
    }

    private void su(Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private static String docs(String tag) {
        return "docs__p35crp-" + tag + "__" + MODEL + "__v1";
    }

    /** {@code n} orphan chunks (no manifest row: reapable at a zero grace) in {@code collection}, which is registered by this call. */
    private void seed(DSLContext ctx, String tenant, String collection, int n) {
        PgContainerHelper.insertCollection(ctx, tenant, collection);
        List<String> hex = new ArrayList<>();
        List<float[]> vectors = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 1; i <= n; i++) {
            String h = Chash.ofText(tenant + "/" + collection + "/" + i).toHex();
            hex.add(h);
            float[] v = new float[DIM];
            java.util.Arrays.fill(v, 0.1f);
            vectors.add(v);
            texts.add("text " + h.substring(0, 8));
            metas.add(Map.of());
        }
        PgContainerHelper.insertChunks(ctx, tenant, collection, hex, texts, vectors, metas);
    }

    private static void renameRegistryRow(DSLContext ctx, String tenant, String from, String to) {
        int n = ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(from)).execute();
        assertThat(n).as("the registry row is renamed in place").isEqualTo(1);
    }

    private ChunkReaper reaper(ReaperRepository store, String tenant) {
        var ladder = new LadderRepository(tenantScope);
        ladder.record(tenant, Rdr192BackfillGate.RUNG_NAME, "7.99.0", "");
        var vectors = new PgVectorRepository(tenantScope, embedder, embedder, LATCHED);
        var gate = new Rdr192BackfillGate(ladder, ChunkReaper.emptyTenantProbe(store));
        return new ChunkReaper(store, vectors, repo, gate, () -> List.of(tenant), Settings.defaults(), CLOCK);
    }

    @Test
    void collectionsWithChunks_listsADanglingIdUnderItsStoredName_andEveryIdOnce() throws Exception {
        String tenant = "p35crp-lst-" + seq.incrementAndGet();
        String old = docs("lst-old"), renamed = docs("lst-renamed"), dangling = docs("lst-dangling");
        su(ctx -> {
            seed(ctx, tenant, old, 2);
            renameRegistryRow(ctx, tenant, old, renamed);
            seed(ctx, tenant, dangling, 2);
            assertThat(ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
                .and(CATALOG_COLLECTIONS.NAME.eq(dangling)).execute()).as("the registry no longer holds the id").isEqualTo(1);
        });

        assertThat(new ReaperRepository(tenantScope, LATCHED).collectionsWithChunks(tenant, Duration.ofSeconds(20)))
            .as("the renamed id under the registry's current name, the dangling id under its stored name, each once, sorted")
            .containsExactly(java.util.stream.Stream.of(renamed, dangling).sorted().toArray(String[]::new));
        assertThat(new ReaperRepository(tenantScope, Gate3.never()).collectionsWithChunks(tenant, Duration.ofSeconds(20)))
            .as("before gate 3 the chunk rows' own names, as ever").containsExactlyInAnyOrder(old, dangling);
    }

    @Test
    void passTenant_reapsARenamedCollectionUnderItsNewLabel_andListsADanglingIdUnderItsStoredName() throws Exception {
        String tenant = "p35crp-pass-" + seq.incrementAndGet();
        String old = docs("pass-old"), renamed = docs("pass-renamed"), dangling = docs("pass-dangling");
        su(ctx -> {
            seed(ctx, tenant, old, 2);
            renameRegistryRow(ctx, tenant, old, renamed);
            seed(ctx, tenant, dangling, 2);
            ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
                .and(CATALOG_COLLECTIONS.NAME.eq(dangling)).execute();
        });
        long[] before = new long[1];
        su(ctx -> before[0] = ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(old))));
        assertThat(before[0]).as("precondition: the renamed collection's chunks still carry the old name").isEqualTo(2);

        RunResult run = reaper(new ReaperRepository(tenantScope, LATCHED), tenant).runOnce(Duration.ZERO);

        CollectionResult renamedResult = run.tenant(tenant).collection(renamed);
        assertThat(renamedResult).as("listed under its new label").isNotNull();
        assertThat(renamedResult.refusal()).as("not refused UNREGISTERED_COLLECTION").isNull();
        assertThat(renamedResult.error()).isNull();
        assertThat(renamedResult.moved()).as("its reapable chunks are reaped").isEqualTo(2);
        assertThat(run.tenant(tenant).collection(old)).as("the old name is no collection from gate 3").isNull();

        CollectionResult danglingResult = run.tenant(tenant).collection(dangling);
        assertThat(danglingResult).as("a dangling id is listed under its stored name").isNotNull();
        assertThat(danglingResult.refusal()).as("and refused, since no registry row holds it")
            .isEqualTo(Refusal.UNREGISTERED_COLLECTION);
        assertThat(danglingResult.moved()).isZero();
        su(ctx -> assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(dangling))))
            .as("the dangling id's rows are untouched").isEqualTo(2));
    }
}
