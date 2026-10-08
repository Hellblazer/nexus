// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Record1;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-41sfa: {@code listCollections} reads the registry, not the chunk rows.
 *
 * <p>{@code listCollections} was {@code SELECT DISTINCT collection FROM nexus.chunks} (a scan of every row of
 * every leaf the tenant owns: 20 to 27 ms at 21.8k rows against 0.18 ms for the registry shape, T2
 * {@code nexus/rdr225-partition-pruning-census-2026-10-08}). It now reads {@code catalog_collections} with an
 * {@code EXISTS} on {@code chunks}.
 *
 * <p>Fixture: two tenants x two models (384, 1024). Per tenant: a live collection per model, a collection whose
 * chunks are all unowned, a quarantine sibling, a rename tombstone (superseded, still 'live'), and a registered
 * collection with no chunk. Rows are bulk-inserted ({@code -Dcli.rows=N} per collection, default 4000) so that a
 * scan of the chunk rows costs something to compare against.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ListCollectionsRegistryReadIntegrationTest {

    static final String SVC_ROLE = "svc_clr";
    static final String SVC_PASS = "svc_clr_pass";
    static final String T1 = "clr-tenant-1";
    static final String T2 = "clr-tenant-2";
    static final int ROWS = Integer.getInteger("cli.rows", 4000);

    static final String LIVE_384 = "knowledge__clr-live__minilm-l6-v2-384__v1";
    static final String LIVE_1024 = "code__clr-live__voyage-code-3__v1";
    static final String UNOWNED = "knowledge__clr-unowned__voyage-context-3__v1";
    static final String QUARANTINE = "quarantine-knowledge__clr-q__minilm-l6-v2-384__v1";
    static final String TOMBSTONE = "knowledge__clr-old__minilm-l6-v2-384__v1";
    static final String EMPTY = "knowledge__clr-empty__minilm-l6-v2-384__v1";
    static final String OTHER_TENANT_ONLY = "knowledge__clr-t2only__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        Embedder none = new Embedder() {
            @Override public List<float[]> embed(List<String> texts) {
                throw new UnsupportedOperationException("the listings never embed");
            }
            @Override public void close() { }
        };
        repo = new PgVectorRepository(tenantScope, none, none);
        seed();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * {@code rows} chunks of {@code coll} cloned from one seed row: chash = sha256(coll || n) for n in 1..rows, the
     * seed row's text and vector.
     */
    private static void bulk(DSLContext dsl, String tenant, String coll, String model, int dim, int rows) {
        String seedHex = Chash.ofText("seed " + coll).toHex();
        PgContainerHelper.insertChunks(dsl, tenant, coll, List.of(seedHex), List.of("seed"),
            List.of(new float[dim]), List.of(Map.of()));
        org.jooq.Table<Record1<Integer>> series = DSL.generateSeries(1, rows).as("s", "g");
        org.jooq.Field<Integer> g = series.field(0, Integer.class);
        org.jooq.Field<Vector> emb = dim == 384 ? CHUNKS.EMBEDDING_384 : CHUNKS.EMBEDDING_1024;
        dsl.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                CHUNKS.CHUNK_TEXT, emb)
            .select(dsl.select(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, chashOf(coll, g), CHUNKS.EMBEDDING_MODEL,
                    CHUNKS.CHUNK_TEXT, emb)
                .from(CHUNKS, series)
                .where(CHUNKS.TENANT_ID.eq(tenant)).and(CHUNKS.COLLECTION.eq(coll))
                .and(CHUNKS.CHASH.eq(Chash.fromHex(seedHex).toBytes())))
            .execute();
    }

    private static org.jooq.Field<byte[]> chashOf(String coll, org.jooq.Field<Integer> g) {
        return DSL.function("sha256", byte[].class,
            DSL.function("convert_to", byte[].class, DSL.concat(DSL.inline(coll), g.cast(String.class)),
                DSL.inline("UTF8")));
    }

    private void seed() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String t : List.of(T1, T2)) {
                for (String c : List.of(LIVE_384, LIVE_1024, UNOWNED, QUARANTINE, TOMBSTONE, EMPTY)) {
                    PgContainerHelper.insertCollection(dsl, t, c);
                }
                bulk(dsl, t, LIVE_384, "minilm-l6-v2-384", 384, ROWS);
                bulk(dsl, t, LIVE_1024, "voyage-code-3", 1024, ROWS);
                bulk(dsl, t, UNOWNED, "voyage-context-3", 1024, 50);
                bulk(dsl, t, QUARANTINE, "minilm-l6-v2-384", 384, 50);
                bulk(dsl, t, TOMBSTONE, "minilm-l6-v2-384", 384, 50);
            }
            PgContainerHelper.insertCollection(dsl, T2, OTHER_TENANT_ONLY);
            bulk(dsl, T2, OTHER_TENANT_ONLY, "minilm-l6-v2-384", 384, 10);
            // A rename tombstone: still 'live', superseded by the new name.
            dsl.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, LIVE_384)
                .where(CATALOG_COLLECTIONS.NAME.eq(TOMBSTONE)).execute();
            // Own the first 20 chunks of each live collection so the stats view has live counts.
            for (String t : List.of(T1, T2)) {
                for (String[] c : new String[][] {{LIVE_384, "minilm-l6-v2-384"}, {LIVE_1024, "voyage-code-3"}}) {
                    String doc = "own-" + c[0];
                    dsl.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                            CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                        .values(t, doc, "o", c[0]).execute();
                    org.jooq.Table<Record1<Integer>> series = DSL.generateSeries(1, 20).as("s", "g");
                    org.jooq.Field<Integer> g = series.field(0, Integer.class);
                    dsl.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                            CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                            CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                            CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
                        .select(dsl.select(DSL.inline(t), DSL.inline(doc), g, chashOf(c[0], g), DSL.inline(c[0]),
                                DSL.inline(c[1]))
                            .from(series))
                        .execute();
                }
            }
            // As in production: the service role has MAINTAIN and every leaf carries the parent's privileges.
            PgContainerHelper.runSuperuserDdl(su, "GRANT MAINTAIN ON nexus.chunks TO " + SVC_ROLE);
            @SuppressWarnings("deprecation")
            int synced = dev.nexus.service.jooq.nexus.Routines.partitionSyncAccess(dsl.configuration(),
                "nexus.chunks", true);
            assertThat(synced).as("the leaves took the parent's privileges").isPositive();
        }
        var vacuumed = tenantScope.vacuumAnalyze(List.of("nexus.chunks"));
        assertThat(vacuumed.get("nexus.chunks").vacuumed()).as("vacuumed: %s", vacuumed).isTrue();
    }

    private List<String> oldListing(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectDistinct(CHUNKS.COLLECTION).from(CHUNKS).orderBy(1).fetch(CHUNKS.COLLECTION));
    }

    private List<String> newListing(String tenant) {
        List<String> out = new ArrayList<>();
        for (var m : repo.listCollections(tenant)) out.add((String) m.get("name"));
        return out;
    }

    // ── listCollections (nexus-41sfa) ─────────────────────────────────────────

    @Test
    void listCollections_returnsExactlyTheCollectionsTheChunkScanReturned() {
        for (String t : List.of(T1, T2)) {
            assertThat(newListing(t)).as("tenant %s", t).containsExactlyElementsOf(oldListing(t));
        }
        assertThat(newListing(T1)).containsExactly(LIVE_1024, LIVE_384, TOMBSTONE, UNOWNED, QUARANTINE);
        assertThat(newListing(T1)).as("a registered collection with no chunk is absent").doesNotContain(EMPTY);
        assertThat(newListing(T1)).as("another tenant's collection is invisible").doesNotContain(OTHER_TENANT_ONLY);
        assertThat(newListing(T2)).contains(OTHER_TENANT_ONLY);
    }

    /**
     * Every chunk row has a registry row under the validated composite foreign key, so a collection that holds
     * chunks always appears; the scan could only ever differ from the registry read for a chunk the foreign key
     * forbids. Stated as a test: the key is enforced, and a chunk of an unregistered collection cannot be written.
     */
    @Test
    void aChunkWithoutARegistryRow_cannotExist() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            org.assertj.core.api.Assertions.assertThatThrownBy(() ->
                dsl.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL,
                        CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384)
                    .values(T1, "knowledge__clr-nope__minilm-l6-v2-384__v1", new byte[32], "minilm-l6-v2-384", "x",
                        Vector.of(new float[384]))
                    .execute())
                .hasMessageContaining("chunks_collection_fk");
        }
    }

    /**
     * The shape change, as a plan: the chunk scan aggregates EVERY row of the tenant's leaves into groups
     * (HashAggregate over an Append of leaf scans, linear in the tenant's chunks), the registry read is a
     * semi-join that stops at the first chunk per registered collection. The measured executions (the
     * planner's cost estimates do not separate them on a small fixture) are in the bead.
     */
    @Test
    void listCollections_isASemiJoinOnTheRegistry_notAnAggregateOverEveryChunk() {
        String[] plans = tenantScope.withTenant(T1, ctx -> new String[] {
            ctx.explain(ctx.selectDistinct(CHUNKS.COLLECTION).from(CHUNKS).orderBy(1)).plan(),
            ctx.explain(PgVectorRepository.listCollectionsQuery(ctx, T1)).plan()});
        System.out.println("listCollections OLD plan:\n" + plans[0] + "\nlistCollections NEW plan:\n" + plans[1]);
        assertThat(plans[0]).as("control: the old statement groups every chunk row").contains("HashAggregate");
        assertThat(plans[1]).as("the new statement is a semi join from the registry:%n%s", plans[1])
            .contains("Semi Join").contains("catalog_collections").doesNotContain("HashAggregate");
    }

    /** Execution time of the old statement against the new one, printed (bounds nothing; -Dcli.rows=N sizes it). */
    @Test
    void latency_chunkScanVersusRegistryRead_printed() {
        int runs = 15;
        long[] oldUs = new long[runs];
        long[] newUs = new long[runs];
        for (int i = 0; i < runs; i++) {
            long t0 = System.nanoTime();
            oldListing(T1);
            oldUs[i] = (System.nanoTime() - t0) / 1_000L;
            t0 = System.nanoTime();
            newListing(T1);
            newUs[i] = (System.nanoTime() - t0) / 1_000L;
        }
        java.util.Arrays.sort(oldUs);
        java.util.Arrays.sort(newUs);
        System.out.println("listCollections latency rows_per_collection=" + ROWS + " (tenant chunks ~" + (2 * ROWS + 150)
            + ") median old=" + oldUs[runs / 2] / 1000.0 + "ms new=" + newUs[runs / 2] / 1000.0 + "ms");
    }
}
