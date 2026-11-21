// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28), the tenant restore (RDR-248) as a route that writes collection rows: the file's
 * {@code collection_id} is a client-supplied id like any other, so the load drops it and the sequence assigns every
 * restored row its own (the same doctrine as the other serial keys: "the engine role cannot setval, so ids are
 * always reassigned"); and a file holding two live rows on one tuple is refused, naming both collections, once the
 * partial UNIQUE index is present (it would refuse the load with a bare 23505) and when it is absent alike. P3.9 decides how a bundle carries the id.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionMintRestoreTest {

    private static final String BGE = "bge-base-en-v15-768";
    private static final String MINILM = "minilm-l6-v2-384";
    private static final String VOYAGE = "voyage-context-3";

    private ImportTestEnv env;

    @BeforeAll
    void startAll() throws Exception {
        env = ImportTestEnv.start();
    }

    @AfterAll
    void stopAll() {
        if (env != null) env.close();
    }

    @BeforeEach
    void freshSessionsAndNoIndex() {
        env.freshSessions();
        env.su(ctx -> {
            CollectionMintSupport.dropIndex(ctx);
            return null;
        });
    }

    /**
     * Three collections of {@code tenant} (one per vector width), and nothing else: the writers of the other tables do not
     * set the id columns on this tree (the dual write is a later stage of P3.1), so a full seed cannot be written.
     */
    private void seedCollections(String tenant) {
        env.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, "restore-bge-" + tenant, BGE);
            PgContainerHelper.insertCollection(ctx, tenant, "restore-minilm-" + tenant, MINILM);
            PgContainerHelper.insertCollection(ctx, tenant, "restore-voyage-" + tenant, VOYAGE);
            return null;
        });
    }

    private Set<Long> collectionIds(String tenant) {
        return Set.copyOf(env.su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).fetch(CATALOG_COLLECTIONS.COLLECTION_ID)));
    }

    @Test
    void aRestoreAssignsEveryCollectionAnIdFromTheSequenceAndIgnoresTheFilesIds() throws Exception {
        String src = "t-mint-restore-src";
        String dst = "t-mint-restore-dst";
        String srcToken = env.newTenant(src);
        seedCollections(src);
        var file = env.export(srcToken);
        Set<Long> fileIds = collectionIds(src);
        assertThat(fileIds).as("guard: the file carries ids").hasSize(3);
        assertThat(file.table("catalog_collections").toString()).as("guard: the file names the column").contains("collection_id");
        String dstToken = env.newTenant(dst);

        JsonNode end = env.restore(dstToken, file);

        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");
        Set<Long> restored = collectionIds(dst);
        assertThat(restored).as("the three restored collections hold ids").hasSize(fileIds.size());
        assertThat(restored).as("none of them is the id the file carried").doesNotContainAnyElementsOf(fileIds);
        assertThat(end.get("report").toString()).as("the report names the column it skipped").contains("collection_id");
    }

    @Test
    void aFileWithTwoLiveRowsOnOneTupleIsRefusedNamingBoth_withTheIndexPresent() throws Exception {
        assertTwinFileIsRefused(true, "t-mint-restore-twin-on-src", "t-mint-restore-twin-on-dst");
    }

    @Test
    void aFileWithTwoLiveRowsOnOneTupleIsRefusedNamingBoth_withTheIndexAbsent() throws Exception {
        assertTwinFileIsRefused(false, "t-mint-restore-twin-off-src", "t-mint-restore-twin-off-dst");
    }

    private void assertTwinFileIsRefused(boolean indexed, String src, String dst) throws Exception {
        String srcToken = env.newTenant(src);
        seedCollections(src);
        // the second live row of one tuple, as an install that predates the index can hold (D4's accepted duplicate)
        env.su(ctx -> ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .select(ctx.select(CATALOG_COLLECTIONS.TENANT_ID, DSL.inline("twin-of-bge-p38"), CATALOG_COLLECTIONS.CONTENT_TYPE,
                    CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                    CATALOG_COLLECTIONS.LIFECYCLE_STATE)
                .from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(src)).and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq(BGE)).limit(1))
            .execute());
        String original = env.su(ctx -> ctx.select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(src)).and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq(BGE))
            .and(CATALOG_COLLECTIONS.NAME.ne("twin-of-bge-p38")).orderBy(CATALOG_COLLECTIONS.NAME).limit(1)
            .fetchOne(CATALOG_COLLECTIONS.NAME));
        var file = env.export(srcToken);
        // the duplicate leaves the source, then the index can be built (a duplicate anywhere refuses its build)
        env.su(ctx -> {
            CollectionMintSupport.deleteRow(ctx, src, "twin-of-bge-p38");
            if (indexed) {
                CollectionMintSupport.createIndex(ctx);
            }
            return null;
        });
        boolean present = env.su(CollectionMintSupport::indexPresent);
        assertThat(present).as("guard: the index state under test").isEqualTo(indexed);
        String dstToken = env.newTenant(dst);

        JsonNode end = env.restore(dstToken, file);

        assertThat(end.get("status").asText()).as(end.toString()).startsWith("failed: ")
            .contains("twin-of-bge-p38").contains(original);
        assertThat(env.count("catalog_collections", dst)).as("the load is one transaction: nothing landed").isZero();
    }
}
