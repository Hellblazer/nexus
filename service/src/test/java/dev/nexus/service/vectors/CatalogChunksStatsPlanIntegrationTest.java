// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.datamove.IdUniqueIndexPass;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.JSONB;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.util.ArrayList;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.COLLECTION_VECTOR_STATS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.4: the plan of the per-collection stats read by id over {@code nexus.collection_vector_stats} (vectors-077-98), on a tenant shaped like
 * elsinore's 5x80k run (scaled down, one collection a fifth of the tenant's rows), with {@code IdUniqueIndexPass}'s {@code idx_catalog_chunks_collection_id}
 * VALID and the tables analyzed. The plan is read as the engine reads it: the tenant bound, the service role.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogChunksStatsPlanIntegrationTest extends PciIdIndexSupport {

    private static final Logger log = LoggerFactory.getLogger(CatalogChunksStatsPlanIntegrationTest.class);
    private static final int COLLECTIONS = 5;
    private static final int ROWS = 20_000;                       // per collection: one collection is a fifth of the tenant
    private static final String INDEX = "idx_catalog_chunks_collection_id";

    /** {@code rows} chunks with a live manifest owner, stamped with the collection's id; returns the id. */
    private long seed(String tenant, String coll, int rows) throws Exception {
        asSuperuser(su -> {
            PgContainerHelper.insertCollection(su, tenant, coll, M384);
            return null;
        });
        long id = idOf(tenant, coll);
        String[] hex = new String[rows];
        asSuperuser(su -> {
            for (int from = 0; from < rows; from += 500) {
                var step = su.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT,
                    CHUNKS.EMBEDDING_384, CHUNKS.METADATA, CHUNKS.COLLECTION_ID);
                for (int i = from; i < Math.min(rows, from + 500); i++) {
                    hex[i] = Chash.ofText(tenant + "/" + coll + "/" + i).toHex();
                    step = step.values(tenant, coll, Chash.fromHex(hex[i]).toBytes(), M384, "r", vector(i), JSONB.jsonb("{}"), id);
                }
                step.onConflictDoNothing().execute();
            }
            for (int from = 0; from < rows; from += 4000) {
                PgContainerHelper.ownChunks(su, tenant, coll, java.util.Arrays.copyOfRange(hex, from, Math.min(rows, from + 4000)));
            }
            return null;
        });
        return id;
    }

    private String plan(String tenant, java.util.function.Function<org.jooq.DSLContext, org.jooq.ResultQuery<?>> query) {
        return new TenantScope(svcDs).withTenant(tenant, ctx -> {
            dev.nexus.service.db.PgSession.disableJit(ctx);
            return ctx.explain(query.apply(ctx)).plan();
        });
    }

    /** The median wall time of running the statement, in milliseconds (three runs after one warm-up). */
    private long millis(String tenant, java.util.function.Function<org.jooq.DSLContext, org.jooq.ResultQuery<?>> query) {
        long[] t = new long[3];
        for (int i = -1; i < 3; i++) {
            long start = System.nanoTime();
            new TenantScope(svcDs).withTenant(tenant, ctx -> {
                dev.nexus.service.db.PgSession.disableJit(ctx);
                return query.apply(ctx).fetch().size();
            });
            if (i >= 0) {
                t[i] = (System.nanoTime() - start) / 1_000_000L;
            }
        }
        java.util.Arrays.sort(t);
        return t[1];
    }

    /** A tenant of {@link #COLLECTIONS} collections, the pass's index built, the tables analyzed; returns the collection ids in order. */
    private List<Long> shapedTenant(String tenant) throws Exception {
        List<Long> ids = new ArrayList<>();
        for (int c = 0; c < COLLECTIONS; c++) {
            ids.add(seed(tenant, name("statsplan-" + c), ROWS));
        }
        gate2 = true;
        var pass = new IdUniqueIndexPass(new AdminConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS), new BootWorkRegistry(Clock.systemUTC()), () -> true);
        pass.runOnce();
        try (java.sql.Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.analyzeTable(su, org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "catalog_document_chunks")));
            PgContainerHelper.analyzeTable(su, org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", "chunks")));
        }
        boolean built = asSuperuser(su -> PgCatalogProbesHolder.valid(su, INDEX));
        assertThat(built).as("non-vacuity: the pass built its index").isTrue();
        return ids;
    }

    @Test
    void theStatsByIdRead_usesTheCatalogChunksIdIndex_notASeqScanOfTheManifest() throws Exception {
        String tenant = newTenant("statsplan");
        List<Long> ids = shapedTenant(tenant);
        long target = ids.get(2);
        // elsinore's form: one collection, by id, over the view
        java.util.function.Function<org.jooq.DSLContext, org.jooq.ResultQuery<?>> oneCollection = ctx -> ctx
            .select(COLLECTION_VECTOR_STATS.COLLECTION, COLLECTION_VECTOR_STATS.CHUNK_COUNT, COLLECTION_VECTOR_STATS.STORED_COUNT)
            .from(COLLECTION_VECTOR_STATS)
            .where(COLLECTION_VECTOR_STATS.TENANT_ID.eq(tenant)).and(COLLECTION_VECTOR_STATS.COLLECTION_ID.eq(target));
        // collectionStats's own statement, as built (no lifecycle filter): the view joined to the registry by id
        java.util.function.Function<org.jooq.DSLContext, org.jooq.ResultQuery<?>> full = ctx -> ctx
            .select(COLLECTION_VECTOR_STATS.COLLECTION, COLLECTION_VECTOR_STATS.DIM, COLLECTION_VECTOR_STATS.CHUNK_COUNT,
                COLLECTION_VECTOR_STATS.LAST_WRITE, CATALOG_COLLECTIONS.CONTENT_TYPE, COLLECTION_VECTOR_STATS.STORED_COUNT)
            .from(COLLECTION_VECTOR_STATS)
            .leftJoin(CATALOG_COLLECTIONS)
            .on(CATALOG_COLLECTIONS.TENANT_ID.eq(COLLECTION_VECTOR_STATS.TENANT_ID)
                .and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(COLLECTION_VECTOR_STATS.COLLECTION_ID)))
            .orderBy(COLLECTION_VECTOR_STATS.COLLECTION.asc(), COLLECTION_VECTOR_STATS.DIM.asc());
        String viaView = plan(tenant, full);
        String one = plan(tenant, oneCollection);
        log.info("event=stats_plan_pin shape=collection_stats_full ms={}\n{}", millis(tenant, full), viaView);
        log.info("event=stats_plan_pin shape=one_collection_by_id ms={}\n{}", millis(tenant, oneCollection), one);

        assertThat(one).as("the one-collection stats-by-id plan:%n%s", one).doesNotContain("Seq Scan on catalog_document_chunks");
        assertThat(one).as("and it uses the pass's index:%n%s", one).contains(INDEX);
    }

    /**
     * The per-collection ghost sweep's stored-dimension read (CatalogRepository's sweep, "the collection inventory"): one label,
     * {@code SELECT dim FROM collection_vector_stats WHERE collection_id = <the registry row's id>} under the tenant bind, the id the
     * sweep reads with each registry row (linda's (iii), RDR-229 P3.5b). The hot production one-collection path. By name it reached
     * no index (the view's label is COALESCE(r.name, c.collection)), and the manifest was a parallel seq scan (marlowe, 143 ms).
     */
    @Test
    void theGhostSweepsOneCollectionInventoryRead_usesAnIndexOnTheManifest_notASeqScan() throws Exception {
        String tenant = newTenant("ghostplan");
        shapedTenant(tenant);
        String label = name("statsplan-2");
        java.util.function.Function<org.jooq.DSLContext, org.jooq.ResultQuery<?>> sweep = ctx -> {
            Long id = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
            assertThat(id).as("non-vacuity: the swept label has a registry row").isNotNull();
            return ctx.select(COLLECTION_VECTOR_STATS.DIM).from(COLLECTION_VECTOR_STATS)
                .where(COLLECTION_VECTOR_STATS.COLLECTION_ID.eq(id));
        };
        String plan = plan(tenant, sweep);
        log.info("event=stats_plan_pin shape=ghost_sweep_inventory ms={}\n{}", millis(tenant, sweep), plan);

        assertThat(plan).as("the ghost sweep's one-collection plan:%n%s", plan).doesNotContain("Seq Scan on catalog_document_chunks");
    }

    /** Whether the named index of schema nexus is valid; the catalog probe the other tests use, by jOOQ. */
    private static final class PgCatalogProbesHolder {
        static boolean valid(org.jooq.DSLContext su, String index) {
            return dev.nexus.service.PgCatalogProbes.indexExists(su, "nexus", index);
        }
    }
}
