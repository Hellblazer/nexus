// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.VectorSeeds.vec;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.1 stage 2a (nexus-p2m1x.17): the shared test seeds set {@code collection_id}, so a test that seeds through
 * them no longer fails with 23514 under vectors-068's CHECKs. Each case seeds through one shared helper on a fully
 * migrated schema and reads the id column back against the registry's own id.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionIdSeedsTest {

    private static final String TENANT = "seeds-ids";
    private static final String C1024 = "code__seeds-ids__voyage-code-3__v1";
    private static final String C384 = "docs__seeds-ids__minilm-l6-v2-384__v1";
    private static final String C768 = "knowledge__seeds-ids__bge-base-en-v15-768__v1";

    PostgreSQLContainer<?> pg;
    Connection su;
    DSLContext ctx;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection c = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(c);
        }
        su = pg.createConnection("");
        su.setAutoCommit(true);
        ctx = DSL.using(su, SQLDialect.POSTGRES);
        for (String c : List.of(C1024, C384, C768)) {
            PgContainerHelper.insertCollection(ctx, TENANT, c);
        }
    }

    @AfterAll
    void stop() throws Exception {
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    private Long registryId(String name) {
        return ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
    }

    private static byte[] chash(int n) {
        byte[] b = new byte[32];
        b[0] = (byte) n;
        b[31] = 1;
        return b;
    }

    private static String hex(int n) {
        return java.util.HexFormat.of().formatHex(chash(n));
    }

    @Test
    void theIdLayoutIsDetected() {
        assertThat(CollectionIdSeeds.hasIdColumns(ctx)).isTrue();
    }

    @Test
    void theThreeInsertChunkSeedsSetTheCollectionsId() {
        PgContainerHelper.insertChunk1024(ctx, TENANT, C1024, chash(1), vec(1024, 0.1f));
        PgContainerHelper.insertChunk384(ctx, TENANT, C384, chash(2), vec(384, 0.1f));
        PgContainerHelper.insertChunk768(ctx, TENANT, C768, chash(3), vec(768, 0.1f));
        Map<String, Long> ids = ctx.select(CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.CHASH.in(chash(1), chash(2), chash(3)))
            .fetchMap(CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID);
        assertThat(ids).containsEntry(C1024, registryId(C1024)).containsEntry(C384, registryId(C384))
            .containsEntry(C768, registryId(C768));
        assertThat(ids.values()).doesNotContainNull();
    }

    @Test
    void insertChunksAndTheReferenceOnlySeedSetTheId() {
        PgContainerHelper.insertChunks(ctx, TENANT, C768, List.of(hex(10), hex(11)), List.of("a", "b"),
            List.of(new float[768], new float[768]), List.of(Map.of(), Map.of()));
        PgContainerHelper.insertReferenceOnlyChunk(ctx, TENANT, C768, hex(12), new float[768], Map.of());
        // a repeat of the reference-only row refreshes it and keeps the id
        PgContainerHelper.insertReferenceOnlyChunk(ctx, TENANT, C768, hex(12), new float[768], Map.of("k", "v"));
        List<Long> ids = ctx.select(CHUNKS.COLLECTION_ID).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.CHASH.in(chash(10), chash(11), chash(12)))
            .fetch(CHUNKS.COLLECTION_ID);
        assertThat(ids).hasSize(3).containsOnly(registryId(C768));
    }

    @Test
    void ownChunksSetsTheIdOnTheOwnerDocumentAndEveryManifestRow() {
        PgContainerHelper.insertChunks(ctx, TENANT, C1024, List.of(hex(20), hex(21)), List.of("a", "b"),
            List.of(new float[1024], new float[1024]), List.of(Map.of(), Map.of()));
        PgContainerHelper.ownChunks(ctx, TENANT, C1024, hex(20), hex(21));
        // a second call appends, and its rows carry the id too
        PgContainerHelper.insertChunks(ctx, TENANT, C1024, List.of(hex(22)), List.of("c"),
            List.of(new float[1024]), List.of(Map.of()));
        PgContainerHelper.ownChunks(ctx, TENANT, C1024, hex(22));

        assertThat(ctx.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.TUMBLER.eq("own-" + C1024))
            .fetch(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)).containsExactly(registryId(C1024));
        List<Long> manifest = ctx.select(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID).from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(C1024))
            .fetch(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID);
        assertThat(manifest).hasSize(3).containsOnly(registryId(C1024));
    }

    @Test
    void supersedeWritesTheNameAndTheSuccessorsIdTogetherAndRegistersTheSuccessor() {
        String retired = "code__seeds-ids-retired__voyage-code-3__v1";
        String successor = "code__seeds-ids-successor__voyage-code-3__v1";
        PgContainerHelper.insertCollection(ctx, TENANT, retired);
        CollectionIdSeeds.supersede(ctx, TENANT, retired, successor);
        var row = ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_BY_ID)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(retired)).fetchOne();
        assertThat(row.value1()).isEqualTo(successor);
        assertThat(row.value2()).isNotNull().isEqualTo(registryId(successor));
    }
}
