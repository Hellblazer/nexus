// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.Routines;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CHUNK_LIVE_OWNERS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-225 (nexus-68bsx): {@code PgVectorRepository#getEmbeddings} plans to ONE (model, tenant) leaf of
 * {@code nexus.chunks}.
 *
 * <p>{@code nexus.chunks} is LIST-partitioned by {@code embedding_model}, then by {@code tenant_id}. The
 * row-level-security policy is {@code current_setting}-based, so it cannot prune a leaf at plan time: a
 * statement that names only {@code collection} is planned against every model x tenant leaf (measured on the
 * managed cloud, engine v0.1.151: about 1.2 s per call, and a search makes 14 to 22 of them). The fix is the
 * explicit {@code embedding_model} and {@code tenant_id} predicates {@code probeSelectedRowsQuery} already has.
 *
 * <p>The plan is read from EXPLAIN of the engine's own statement ({@code getEmbeddingsQuery}, which
 * {@code getEmbeddings} runs). The fixture is two models of the SAME dimension (voyage-code-3 and
 * voyage-context-3, both 1024) by two tenants, with the SAME chashes in all four collections but a different
 * vector in each, so a statement that read the wrong leaf would return a wrong vector and not just a slow plan.
 *
 * <p>Fixture size: {@code -Dgep.rows=N} rows per leaf (default 400). The default is a correctness fixture;
 * the latency test at the bottom prints its numbers (use a larger N to see the difference) and bounds nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GetEmbeddingsPruningIntegrationTest {

    static final String SVC_ROLE = "svc_gep";
    static final String SVC_PASS = "svc_gep_pass";
    static final int DIM = 1024;
    static final int ROWS = Integer.getInteger("gep.rows", 400);
    static final String[] TENANTS = {"gep-tenant-a", "gep-tenant-b"};
    /** The first TARGETS models are the ones read; the others are sibling models of the same tenants, populated too. */
    static final String[] MODELS = {"voyage-code-3", "voyage-context-3", "bge-base-en-v15-768", "minilm-l6-v2-384"};
    static final int[] DIMS = {1024, 1024, 768, 384};
    static final int TARGETS = 2;

    final Map<String, String> collections = new LinkedHashMap<>();
    final Map<String, String> leaves = new LinkedHashMap<>();
    final List<String> chashes = new ArrayList<>();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository repo;

    static String key(int t, int m) {
        return t + "/" + m;
    }

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
        cfg.setMaximumPoolSize(3);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        Embedder none = new Embedder() {
            @Override public List<float[]> embed(List<String> texts) {
                throw new UnsupportedOperationException("the read routes never embed");
            }
            @Override public void close() { }
        };
        repo = new PgVectorRepository(tenantScope, none, none);
        seed();
        try (Connection su = pg.createConnection("")) {
            // As in production: the service role has MAINTAIN, every leaf carries the parent's privileges, and
            // a leaf is vacuumed (a loaded, unvacuumed leaf has no visibility map and plans differently).
            PgContainerHelper.runSuperuserDdl(su, "GRANT MAINTAIN ON nexus.chunks TO " + SVC_ROLE);
            @SuppressWarnings("deprecation")
            int synced = Routines.partitionSyncAccess(DSL.using(su, SQLDialect.POSTGRES).configuration(),
                "nexus.chunks", true);
            assertThat(synced).as("the leaves took the parent's privileges").isPositive();
        }
        var vacuumed = tenantScope.vacuumAnalyze(List.of("nexus.chunks"));
        assertThat(vacuumed.get("nexus.chunks").vacuumed()).as("vacuumed: %s", vacuumed).isTrue();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void seed() throws Exception {
        for (int i = 0; i < ROWS; i++) {
            chashes.add(Chash.ofText("gep-row-" + i).toHex());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            int marker = 0;
            for (int t = 0; t < TENANTS.length; t++) {
                for (int m = 0; m < MODELS.length; m++) {
                    marker++;
                    String coll = "knowledge__gep-" + t + "__" + MODELS[m] + "__v1";
                    collections.put(key(t, m), coll);
                    PgContainerHelper.insertCollection(dsl, TENANTS[t], coll, MODELS[m]);
                    for (int from = 0; from < ROWS; from += 300) {
                        int to = Math.min(ROWS, from + 300);
                        List<String> hex = chashes.subList(from, to);
                        List<String> texts = new ArrayList<>();
                        List<float[]> vecs = new ArrayList<>();
                        List<Map<String, Object>> metas = new ArrayList<>();
                        for (int i = from; i < to; i++) {
                            texts.add("gep fixture row " + i);
                            vecs.add(vector(marker, i, DIMS[m]));
                            metas.add(Map.of());
                        }
                        PgContainerHelper.insertChunks(dsl, TENANTS[t], coll, hex, texts, vecs, metas);
                    }
                    PgContainerHelper.ownChunks(dsl, TENANTS[t], coll, chashes.toArray(new String[0]));
                }
            }
            for (int t = 0; t < TENANTS.length; t++) {
                for (int m = 0; m < MODELS.length; m++) {
                    leaves.put(key(t, m),
                        Routines.partitionName(dsl.configuration(), "chunks", MODELS[m], TENANTS[t]));
                }
            }
            assertThat(leaves.values()).as("eight distinct leaves").doesNotHaveDuplicates().hasSize(8);
            PgContainerHelper.analyzeTable(su, dev.nexus.service.jooq.nexus.Tables.CHUNKS);
        }
    }

    /** The row's vector: element 0 names the (tenant, model) collection, element 1 the row. */
    static float[] vector(int marker, int row, int dim) {
        float[] v = new float[dim];
        v[0] = marker;
        v[1] = row;
        v[2] = 0.5f;
        return v;
    }

    private static boolean names(String plan, String leaf) {
        return Pattern.compile("\\b" + Pattern.quote(leaf) + "\\b").matcher(plan).find();
    }

    private String planOf(int t, int m, List<String> ids) {
        return tenantScope.withTenant(TENANTS[t], ctx -> {
            PgSession.setSearchPlanCacheMode(ctx);
            return ctx.explain(PgVectorRepository.getEmbeddingsQuery(
                ctx, DIM, TENANTS[t], MODELS[m], collections.get(key(t, m)), ids)).plan();
        });
    }

    @Test
    void getEmbeddingsQuery_plansToTheOneLeafOfItsModelAndTenant() {
        List<String> ids = chashes.subList(0, 5);
        for (int t = 0; t < TENANTS.length; t++) {
            for (int m = 0; m < TARGETS; m++) {
                String plan = planOf(t, m, ids);
                String own = leaves.get(key(t, m));
                assertThat(names(plan, own))
                    .as("(%s, %s) reads its own leaf %s. Plan:%n%s", TENANTS[t], MODELS[m], own, plan).isTrue();
                for (var e : leaves.entrySet()) {
                    if (e.getKey().equals(key(t, m))) continue;
                    assertThat(names(plan, e.getValue()))
                        .as("(%s, %s) must not plan against leaf %s of %s. Plan:%n%s",
                            TENANTS[t], MODELS[m], e.getValue(), e.getKey(), plan).isFalse();
                }
                assertThat(plan).as("pruned at plan time, not by an Append over leaves removed later")
                    .doesNotContain("Subplans Removed");
            }
        }
    }

    /**
     * Control: the statement WITHOUT the model and tenant predicates, as getEmbeddings ran before this change,
     * is planned against the leaf of every same-width model for the session's tenant. (Executor-startup pruning
     * on the {@code current_setting} tenant predicate drops the other tenants' leaves, which is why the plan
     * shows {@code Subplans Removed}; it cannot drop the other models', since nothing in the statement names one.)
     * Without this the pruning test above could pass on a fixture whose plan never lists the sibling leaves.
     */
    @Test
    void control_theOldShape_plansAgainstEveryModelsLeafOfTheTenant() {
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(DIM);
        String plan = tenantScope.withTenant(TENANTS[0], ctx -> {
            PgSession.setSearchPlanCacheMode(ctx);
            return ctx.explain(oldShape(ctx, ch, collections.get(key(0, 0)), chashes.subList(0, 5))).plan();
        });
        for (int m = 0; m < MODELS.length; m++) {
            // A leaf of another WIDTH is removed at plan time anyway: its CHECK says embedding_1024 IS NULL and
            // the statement requires embedding_1024 IS NOT NULL. Only the same-width sibling model survives.
            assertThat(names(plan, leaves.get(key(0, m))))
                .as("the tenant's %s leaf (width %d) in the unpruned plan:%n%s", MODELS[m], DIMS[m], plan)
                .isEqualTo(DIMS[m] == DIM);
        }
    }

    @Test
    @SuppressWarnings("unchecked")
    void getEmbeddings_returnsEachCollectionsOwnVectors_inRequestOrder_omittingMissing() {
        // Request order is not chash order, and one id exists nowhere.
        List<String> ids = new ArrayList<>(List.of(chashes.get(7), chashes.get(2), Chash.ofText("absent").toHex(),
            chashes.get(5)));
        for (int t = 0; t < TENANTS.length; t++) {
            for (int m = 0; m < TARGETS; m++) {
                int marker = t * MODELS.length + m + 1;
                Map<String, Object> out = repo.getEmbeddings(TENANTS[t], collections.get(key(t, m)), ids);
                List<String> outIds = (List<String>) out.get("ids");
                List<List<Float>> embeddings = (List<List<Float>>) out.get("embeddings");
                assertThat(outIds).as("request order, missing id omitted")
                    .containsExactly(chashes.get(7), chashes.get(2), chashes.get(5));
                int[] rows = {7, 2, 5};
                for (int i = 0; i < rows.length; i++) {
                    assertThat(embeddings.get(i)).hasSize(DIM);
                    assertThat(embeddings.get(i).get(0))
                        .as("the (%s, %s) collection's own vector", TENANTS[t], MODELS[m])
                        .isEqualTo((float) marker);
                    assertThat(embeddings.get(i).get(1)).isEqualTo((float) rows[i]);
                }
            }
        }
    }

    /**
     * The sibling reads scoped the same way: each answers for the collection's own rows although the same
     * chashes exist in the other three (tenant, model) collections.
     */
    @Test
    void siblingReads_areScopedToTheCollectionsOwnLeaf() {
        String coll = collections.get(key(1, 1));
        String tenant = TENANTS[1];
        List<String> ids = chashes.subList(0, 3);

        Map<String, Object> got = repo.get(tenant, coll, ids, 10, 0, false);
        assertThat((List<?>) got.get("ids")).hasSize(3);
        assertThat(got.get("count")).isEqualTo(3L);
        assertThat((List<?>) repo.presentRows(tenant, coll, ids).get("ids")).hasSize(3);
        assertThat(repo.count(tenant, coll)).isEqualTo(ROWS);
        assertThat((List<?>) repo.list(tenant, coll, ROWS, 0).get("ids")).hasSize(ROWS);
        assertThat((List<?>) repo.getWhere(tenant, coll, Map.of(), ROWS, 0, false, false).get("ids"))
            .hasSize(ROWS);
        assertThat((List<?>) repo.getWhere(tenant, coll, Map.of(), ROWS, 0, false, true).get("ids"))
            .hasSize(ROWS);
        assertThat((List<?>) repo.getAllMetadata(tenant, coll, Map.of()).get("ids")).hasSize(ROWS);
        assertThat(repo.selectExistingChashes(tenant, coll, ids)).containsExactlyInAnyOrderElementsOf(ids);
        assertThat(repo.fetchChunkText(tenant, coll, ids.get(0))).isEqualTo("gep fixture row 0");
    }

    /**
     * Latency of the engine statement against the old shape, printed for the
     * bead's record. Bounds nothing: at the default fixture size the difference is small; run with a larger
     * {@code -Dgep.rows} to see it.
     */
    @Test
    void latency_prunedVersusOldShape_printed() {
        int t = 1, m = 1;
        String coll = collections.get(key(t, m));
        List<String> ids = chashes.subList(0, Math.min(20, ROWS));
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(DIM);
        for (String shape : new String[] {"pruned", "old"}) {
            long[] us = new long[15];
            for (int run = 0; run < us.length; run++) {
                long t0 = System.nanoTime();
                int n = tenantScope.withTenant(TENANTS[t], ctx -> shape.equals("pruned")
                    ? PgVectorRepository.getEmbeddingsQuery(ctx, DIM, TENANTS[t], MODELS[m], coll, ids).fetch().size()
                    : oldShape(ctx, ch, coll, ids).fetch().size());
                us[run] = (System.nanoTime() - t0) / 1_000L;
                assertThat(n).isEqualTo(ids.size());
            }
            java.util.Arrays.sort(us);
            System.out.println("get-embeddings shape=" + shape + " rows_per_leaf=" + ROWS + " ids=" + ids.size()
                + " p50_us=" + us[us.length / 2] + " max_us=" + us[us.length - 1]);
        }
    }

    /** getEmbeddings as it ran before nexus-68bsx: collection and chash only, plus the live-owner EXISTS. */
    private static org.jooq.Select<? extends org.jooq.Record2<String, Vector>> oldShape(
            DSLContext ctx, DimTables.ChunkTable ch, String coll, List<String> ids) {
        Field<byte[]> rawChash = ch.table().field("chash", byte[].class);
        return ctx.select(ch.chash(), ch.embedding()).from(ch.table())
            .where(ch.collection().eq(coll).and(ch.chash().in(ids))
                .and(ch.embedding().isNotNull())
                .and(DSL.exists(ctx.selectOne().from(
                    CHUNK_LIVE_OWNERS.call(ch.tenantId(), ch.collection(), rawChash)))));
    }
}
