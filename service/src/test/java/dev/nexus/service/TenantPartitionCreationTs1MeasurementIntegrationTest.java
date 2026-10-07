// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static dev.nexus.service.PartitionScratch.CENTROIDS_NEW;
import static dev.nexus.service.PartitionScratch.CHUNKS_NEW;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-225 P1.2 (nexus-3wh8d.7), TS1: the time {@code create_tenant_partitions} takes for ONE new tenant across
 * BOTH parents ({@code chunks_new}, {@code taxonomy_centroids_new}), as the number of existing tenants grows to
 * 300. Acceptance: p95 at most 2 s at 300 existing tenants, lock_timeout 2 s (the function's own setting).
 *
 * <p>Each sample is what the {@code service_tokens} trigger costs the token insert: one transaction, the two
 * calls, the commit, on a {@code nexus_svc} connection, against the scratch parents with the live column set,
 * the live parent-level indexes (three HNSW, two GIN, one btree on chunks; three HNSW on centroids), the
 * production RLS and grants, and the three referencing tables with 4-column foreign keys. The tenant names are
 * fixed ({@code ts1-0001}...), so a rerun builds the same layout; only the clock varies.
 *
 * <p>Two layouts: the four real models (eight leaves per tenant), and the four real models plus the three
 * disputed-dimension placeholders the migration may register (fourteen partitions per tenant, the RDR's upper
 * bound). The windows are the creations made with about 2, 100 and 300 tenants already present; the slope is
 * the mean of the first 50 creations against the mean of the last 50, and a least-squares line over all of them.
 *
 * <p>Slow (minutes), so tagged {@code integration}: run with
 * {@code -Dtest.excluded.groups= -Dgroups=integration -Dtest=TenantPartitionCreationTs1MeasurementIntegrationTest}.
 * Figures go to {@code target/p225-evidence.txt}.
 */
@Tag("integration")
class TenantPartitionCreationTs1MeasurementIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_ts1";
    private static final String ADMIN_PASS = "nexus_admin_ts1_pass";
    private static final int SAMPLES = 310;
    private static final double P95_BOUND_MS = 2000.0;

    PostgreSQLContainer<?> pg;

    /**
     * A fresh dedicated container per measurement. The scratch fixture drops and rebuilds the parents, and
     * dropping the ~2,500 leaves a finished run leaves behind in ONE statement exhausts the default lock table
     * ("out of shared memory", max_locks_per_transaction 64 x 100 slots); a fresh database never has to.
     */
    private void startFresh() throws Exception {
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        try (var adminDs = new HikariDataSource(cfg)) {
            SchemaMigrator.migrate(adminDs);
            try (Connection a = adminDs.getConnection()) {
                // vectors-030 has walked the live tables; free the scratch parents' partition names (see the changelog header)
                PartitionScratch.captureTemplatesAndClearLiveParents(a);
            }
        }
    }

    @AfterEach
    void stop() {
        if (pg != null) pg.stop();
    }

    @Test
    void ts1_fourRealModels_eightLeavesPerTenant() throws Exception {
        startFresh();
        measure("4 models, 8 leaves per tenant", PartitionScratch.REAL_MODELS);
    }

    @Test
    void ts1_sevenModelsIncludingPlaceholders_fourteenPartitionsPerTenant() throws Exception {
        startFresh();
        try (Connection a = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)) {
            var ctx = DSL.using(a, SQLDialect.POSTGRES);
            for (int dim : new int[] {1024, 768, 384}) {
                ctx.insertInto(EMBEDDING_MODELS)
                    .set(EMBEDDING_MODELS.EMBEDDING_MODEL, "disputed-" + dim)
                    .set(EMBEDDING_MODELS.DIMENSION, dim)
                    .set(EMBEDDING_MODELS.PROVIDER, "disputed")
                    .onConflictDoNothing().execute();
            }
        }
        List<String> models = new ArrayList<>(PartitionScratch.REAL_MODELS);
        models.addAll(List.of("disputed-1024", "disputed-768", "disputed-384"));
        measure("7 models, 14 partitions per tenant", models);
    }

    private void measure(String label, List<String> models) throws Exception {
        try (Connection a = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)) {
            PartitionScratch.reset(a);
            var ctx = DSL.using(a, SQLDialect.POSTGRES);
            for (String parent : List.of(CHUNKS_NEW, CENTROIDS_NEW)) {
                for (String model : models) {
                    PartitionScratch.createModelPartition(ctx, parent, model, true);
                }
            }
        }
        double[] ms = new double[SAMPLES];
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(),
                PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD)) {
            c.setAutoCommit(false);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            for (int i = 0; i < SAMPLES; i++) {
                String tenant = String.format("ts1-%04d", i + 1);
                long t0 = System.nanoTime();
                int made = PartitionScratch.createTenantPartitions(ctx, CHUNKS_NEW, tenant, true)
                    + PartitionScratch.createTenantPartitions(ctx, CENTROIDS_NEW, tenant, true);
                c.commit();
                ms[i] = (System.nanoTime() - t0) / 1e6;
                assertThat(made).as("leaves made for %s", tenant).isEqualTo(2 * models.size());
            }
        }
        int relations;
        try (Connection a = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)) {
            relations = PartitionScratch.nexusRelationCount(DSL.using(a, SQLDialect.POSTGRES));
        }
        PartitionScratch.evidence("TS1 [" + label + "] " + SAMPLES + " creations, each across both parents, "
            + "lock_timeout 2s, nexus_svc connection, commit included; nexus relations at end: " + relations);
        // "existing" counts the default tenant, which the model partitions already carry.
        Window w2 = window(ms, 0, 2, 1);
        Window w100 = window(ms, 90, 110, 1);
        Window w300 = window(ms, 290, 310, 1);
        for (Window w : List.of(w2, w100, w300)) {
            PartitionScratch.evidence(String.format(
                "TS1 [%s] creations %d-%d (about %d tenants already present): n=%d p50=%.1f ms p95=%.1f ms max=%.1f ms",
                label, w.fromInclusive + 1, w.toExclusive, w.existingMid, w.n, w.p50, w.p95, w.max));
        }
        double first50 = mean(ms, 0, 50);
        double last50 = mean(ms, SAMPLES - 50, SAMPLES);
        PartitionScratch.evidence(String.format(
            "TS1 [%s] mean of first 50 = %.1f ms, mean of last 50 = %.1f ms, least-squares slope = %.3f ms per tenant, overall p95 = %.1f ms",
            label, first50, last50, slope(ms), percentile(ms, 0, SAMPLES, 0.95)));
        assertThat(w300.p95).as("p95 at about 300 tenants (%s)", label).isLessThanOrEqualTo(P95_BOUND_MS);
    }

    private record Window(int fromInclusive, int toExclusive, int existingMid, int n, double p50, double p95, double max) {}

    private static Window window(double[] ms, int from, int to, int existingBase) {
        int mid = (from + to) / 2;
        return new Window(from, to, mid + existingBase, to - from,
            percentile(ms, from, to, 0.50), percentile(ms, from, to, 0.95),
            Arrays.stream(ms, from, to).max().orElse(Double.NaN));
    }

    private static double percentile(double[] ms, int from, int to, double p) {
        double[] s = Arrays.copyOfRange(ms, from, to);
        Arrays.sort(s);
        int idx = (int) Math.ceil(p * s.length) - 1;
        return s[Math.max(0, Math.min(idx, s.length - 1))];
    }

    private static double mean(double[] ms, int from, int to) {
        return Arrays.stream(ms, from, to).average().orElse(Double.NaN);
    }

    /** Ordinary least-squares slope of creation time against creation index, in ms per tenant. */
    private static double slope(double[] ms) {
        int n = ms.length;
        double mx = (n - 1) / 2.0;
        double my = mean(ms, 0, n);
        double num = 0;
        double den = 0;
        for (int i = 0; i < n; i++) {
            num += (i - mx) * (ms[i] - my);
            den += (i - mx) * (i - mx);
        }
        return num / den;
    }
}
