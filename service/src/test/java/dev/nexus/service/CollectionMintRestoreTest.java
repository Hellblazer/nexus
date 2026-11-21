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
 * partial UNIQUE index exists and would refuse the load with a bare 23505. P3.9 decides how a bundle carries the id.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionMintRestoreTest {

    private static final String BGE = "bge-base-en-v15-768";
    private static final String MINILM = "minilm-l6-v2-384";
    private static final String VOYAGE = "voyage-context-3";
    private static final String INDEX = "catalog_collections_live_tuple_uq";

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
        env.su(ctx -> ctx.execute("DROP INDEX IF EXISTS nexus." + INDEX));
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
    void aFileWithTwoLiveRowsOnOneTupleIsRefusedNamingBothOnceTheIndexExists() throws Exception {
        String src = "t-mint-restore-twin-src";
        String dst = "t-mint-restore-twin-dst";
        String srcToken = env.newTenant(src);
        seedCollections(src);
        env.su(ctx -> {
            // the second live row of one tuple, as an install that predates the index can hold (D4's accepted duplicate)
            ctx.execute("INSERT INTO nexus.catalog_collections (tenant_id, name, content_type, owner_id, embedding_model, "
                + "model_version, lifecycle_state) SELECT tenant_id, 'twin-of-bge-p38', content_type, owner_id, embedding_model, "
                + "model_version, lifecycle_state FROM nexus.catalog_collections WHERE tenant_id = '" + src
                + "' AND embedding_model = '" + BGE + "' AND lifecycle_state = 'live' AND name <> 'twin-of-bge-p38' LIMIT 1");
            return null;
        });
        String original = env.su(ctx -> ctx.resultQuery("SELECT name FROM nexus.catalog_collections WHERE tenant_id = '" + src
            + "' AND embedding_model = '" + BGE + "' AND name <> 'twin-of-bge-p38' ORDER BY name LIMIT 1").fetchOne(0, String.class));
        var file = env.export(srcToken);
        // the duplicate leaves the source, then the index can be built (a duplicate anywhere refuses its build)
        env.su(ctx -> ctx.execute("DELETE FROM nexus.catalog_collections WHERE tenant_id = '" + src + "' AND name = 'twin-of-bge-p38'"));
        env.su(ctx -> ctx.execute("CREATE UNIQUE INDEX " + INDEX + " ON nexus.catalog_collections "
            + "(tenant_id, content_type, owner_id, embedding_model, model_version) "
            + "WHERE superseded_by = '' AND lifecycle_state <> 'quarantine'"));
        String dstToken = env.newTenant(dst);

        JsonNode end = env.restore(dstToken, file);

        assertThat(end.get("status").asText()).as(end.toString()).startsWith("failed: ")
            .contains("twin-of-bge-p38").contains(original);
        assertThat(env.count("catalog_collections", dst)).as("the load is one transaction: nothing landed").isZero();
    }
}
