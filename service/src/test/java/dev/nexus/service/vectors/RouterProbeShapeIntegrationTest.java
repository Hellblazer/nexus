// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.nexus.Routines;
import org.jooq.DSLContext;
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
import java.util.Random;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-225 (nexus-3wh8d.15): the cardinality router's probe, {@code PgVectorRepository#probeSelectedRows}, counts
 * ONE model's partition of one tenant, and keeps an index scan on the leaf's primary-key prefix when the
 * selected collection is a large share of that leaf.
 *
 * <p>Why a pin and a latency check. Before the table was partitioned by model, a collection was a small share of
 * the tenant's one leaf. Now a leaf holds one model, so a scope is about twice the fraction of it (the P0.2
 * harness critique, 2026-10-06: at a 55k-row scope in a 300k-row leaf the prototype's probe took a Seq Scan where
 * the single-leaf layout used the primary-key prefix scan). The probe reads at most {@code limit + 1} rows, so an
 * index scan on (tenant_id, collection) costs a few hundred index pages however large the leaf is, and a
 * sequential scan costs the pages of every row it walks before it has found {@code limit + 1} qualifying ones,
 * which is {@code (limit + 1) / share} rows. The fixture makes each scope 26 to 33 percent of its leaf and sets
 * the router threshold ({@link #LIMIT}) at the three scopes' sizes: just under, at, and just over.
 *
 * <p>The plan is read from EXPLAIN of the engine's own statement ({@code probeSelectedRowsQuery}, which
 * {@code probeSelectedRows} runs), through the bound-parameter path with the transaction's
 * {@code force_custom_plan}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RouterProbeShapeIntegrationTest {

    static final String TENANT = "rps-tenant";
    static final String SVC_ROLE = "svc_rps";
    static final String SVC_PASS = "svc_rps_pass";
    static final String MODEL = "minilm-l6-v2-384";
    static final int DIM = 384;

    /** The router threshold under test: the probe counts at most LIMIT + 1 rows. */
    static final int LIMIT = 2000;
    static final int[] SCOPE_ROWS = {LIMIT * 9 / 10, LIMIT, LIMIT * 11 / 10};
    static final int OTHER_ROWS = 800;
    /** A second tenant whose scope is 88 percent of its leaf: the share at which a sequential scan wins most easily. */
    static final String HI_TENANT = "rps-tenant-hi";
    static final int HI_SCOPE_ROWS = LIMIT * 11 / 10;
    static final int HI_OTHER_ROWS = 300;
    String hiScope;
    String hiLeaf;

    final List<String> scopes = new ArrayList<>();
    String other;
    int leafRows;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    String leaf;

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
        seed();
        // A production leaf is vacuumed, which sets its visibility map: that is what lets the probe's index scan
        // answer from the index alone. A leaf that has only been inserted into and analyzed has no map, so every
        // index probe would also cost a heap fetch and the comparison would not be production's.
        try (Connection su = pg.createConnection("")) {
            // nexus_svc has MAINTAIN on the table in production (grants-005-chunks-unify-maintain); the test's
            // per-class role gets the same grant.
            PgContainerHelper.runSuperuserDdl(su, "GRANT MAINTAIN ON nexus.chunks TO " + SVC_ROLE);
            // ... and a leaf carries its own copy of the parent's privileges, which the engine's
            // partition-sync function copies.
            @SuppressWarnings("deprecation")
            int synced = Routines.partitionSyncAccess(DSL.using(su, SQLDialect.POSTGRES).configuration(),
                "nexus.chunks", true);
            assertThat(synced).as("the leaves took the parent's privileges").isPositive();
        }
        var vacuumed = tenantScope.vacuumAnalyze(List.of("nexus.chunks"));
        assertThat(vacuumed.get("nexus.chunks").vacuumed()).as("the leaf was vacuumed: %s", vacuumed).isTrue();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void seed() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            Random rnd = new Random(225);
            for (int i = 0; i < SCOPE_ROWS.length; i++) {
                String coll = "knowledge__rps-scope" + i + "__minilm-l6-v2-384__v1";
                scopes.add(coll);
                PgContainerHelper.insertCollection(dsl, TENANT, coll);
                insertRows(dsl, TENANT, coll, SCOPE_ROWS[i], rnd);
            }
            other = "knowledge__rps-other__minilm-l6-v2-384__v1";
            PgContainerHelper.insertCollection(dsl, TENANT, other);
            insertRows(dsl, TENANT, other, OTHER_ROWS, rnd);
            hiScope = "knowledge__rps-hi__minilm-l6-v2-384__v1";
            PgContainerHelper.insertCollection(dsl, HI_TENANT, hiScope);
            insertRows(dsl, HI_TENANT, hiScope, HI_SCOPE_ROWS, rnd);
            String hiOther = "knowledge__rps-hi-other__minilm-l6-v2-384__v1";
            PgContainerHelper.insertCollection(dsl, HI_TENANT, hiOther);
            insertRows(dsl, HI_TENANT, hiOther, HI_OTHER_ROWS, rnd);
            hiLeaf = Routines.partitionName(dsl.configuration(), "chunks", MODEL, HI_TENANT);
            leafRows = OTHER_ROWS;
            for (int n : SCOPE_ROWS) leafRows += n;
            PgContainerHelper.analyzeTable(su, CHUNKS);
            leaf = Routines.partitionName(dsl.configuration(), "chunks", MODEL, TENANT);
        }
    }

    private void insertRows(DSLContext dsl, String tenant, String coll, int n, Random rnd) {
        for (int from = 0; from < n; from += 300) {
            int to = Math.min(n, from + 300);
            List<String> hex = new ArrayList<>();
            List<String> texts = new ArrayList<>();
            List<float[]> vecs = new ArrayList<>();
            List<Map<String, Object>> metas = new ArrayList<>();
            for (int i = from; i < to; i++) {
                hex.add(dev.nexus.service.db.Chash.ofText(tenant + "/" + coll + "/" + i).toHex());
                texts.add("router probe fixture row " + i);
                float[] v = new float[DIM];
                double norm = 0;
                for (int k = 0; k < DIM; k++) { v[k] = (float) rnd.nextGaussian(); norm += v[k] * v[k]; }
                norm = Math.sqrt(norm);
                for (int k = 0; k < DIM; k++) v[k] /= (float) norm;
                vecs.add(v);
                metas.add(Map.of());
            }
            PgContainerHelper.insertChunks(dsl, tenant, coll, hex, texts, vecs, metas);
        }
    }

    private String planOf(String collection) {
        return planOf(TENANT, collection);
    }

    private String planOf(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            PgSession.setSearchPlanCacheMode(ctx);
            return ctx.explain(PgVectorRepository.probeSelectedRowsQuery(
                ctx, DIM, new String[] {collection}, MODEL, tenant, LIMIT)).plan();
        });
    }

    @Test
    void probe_readsOneLeaf_byIndex_whenTheScopeIsMostOfTheLeaf() {
        String plan = planOf(HI_TENANT, hiScope);
        assertThat(plan)
            .as("scope of %d rows in a leaf of %d: the probe reads that tenant's leaf by index. Plan:%n%s",
                HI_SCOPE_ROWS, HI_SCOPE_ROWS + HI_OTHER_ROWS, plan)
            .contains(hiLeaf)
            .doesNotContain("Seq Scan")
            .containsPattern("Index (Only )?Scan using \"?" + hiLeaf);
    }

    @Test
    void fixture_eachScopeIsMoreThanAQuarterOfItsLeaf() {
        int total = leafRows;
        for (int i = 0; i < SCOPE_ROWS.length; i++) {
            assertThat(SCOPE_ROWS[i] / (double) total)
                .as("scope %d is %d of %d leaf rows", i, SCOPE_ROWS[i], total).isGreaterThan(0.25);
        }
        int count = tenantScope.withTenant(TENANT, ctx -> ctx.fetchCount(CHUNKS));
        assertThat(count).as("the leaf holds the rows the share is computed from").isEqualTo(total);
    }

    @Test
    void probe_readsOneLeaf_byIndex_atScopesNearTheThreshold() {
        for (int i = 0; i < SCOPE_ROWS.length; i++) {
            String plan = planOf(scopes.get(i));
            assertThat(plan)
                .as("scope of %d rows: the probe reads the (model, tenant) leaf, planned to it. Plan:%n%s",
                    SCOPE_ROWS[i], plan)
                .contains(leaf)
                .doesNotContain("Subplans Removed")
                .doesNotContain("Append");
            assertThat(plan)
                .as("scope of %d rows (%d percent of the leaf): an index scan on the primary-key prefix, not a "
                    + "sequential scan of the leaf. Plan:%n%s", SCOPE_ROWS[i], 100 * SCOPE_ROWS[i] / leafRows, plan)
                .doesNotContain("Seq Scan")
                .containsPattern("Index (Only )?Scan using \"?" + leaf);
        }
    }

    /**
     * Latency of the probe itself, as the engine runs it, at scopes just under, at and just over the threshold.
     * The bound is a catastrophe guard (a sequential scan of the leaf's heap is far over it on a cold leaf);
     * the measured medians are recorded in the bead's record.
     *
     * <p>What it does NOT cover. At this scale (a leaf of about 100 pages) even a sequential scan finishes in a
     * few milliseconds, so this check cannot fail on the regression it names; the EXPLAIN pin above does that
     * work. Both cover a VACUUMED leaf only. The state the cloud walk leaves every leaf in (loaded, analyzed,
     * never vacuumed: the walk is one transaction and cannot VACUUM) is measured on the PITR fork before and after
     * a VACUUM (docs/runbooks/rdr-225-cloud-deploy.md, § 7.2 and the .27 placeholders), not here.
     */
    @Test
    void probe_latency_atScopesNearTheThreshold() {
        for (int i = 0; i < SCOPE_ROWS.length; i++) {
            final String coll = scopes.get(i);
            long[] ms = new long[15];
            for (int run = 0; run < ms.length; run++) {
                long t0 = System.nanoTime();
                int counted = tenantScope.withTenant(TENANT, ctx -> {
                    PgSession.setSearchPlanCacheMode(ctx);
                    return PgVectorRepository.probeSelectedRowsQuery(
                        ctx, DIM, new String[] {coll}, MODEL, TENANT, LIMIT).fetchOne(0, Integer.class);
                });
                ms[run] = (System.nanoTime() - t0) / 1_000L;
                assertThat(counted).as("the probe counts min(scope, limit + 1) rows")
                    .isEqualTo(Math.min(SCOPE_ROWS[i], LIMIT + 1));
            }
            java.util.Arrays.sort(ms);
            long p50us = ms[ms.length / 2];
            System.out.println("router-probe scope=" + SCOPE_ROWS[i] + " leaf=" + leafRows + " limit=" + LIMIT
                + " p50_us=" + p50us + " max_us=" + ms[ms.length - 1]);
            assertThat(p50us / 1000.0).as("probe p50 ms at scope %d", SCOPE_ROWS[i]).isLessThan(100.0);
        }
    }
}
