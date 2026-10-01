// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.ChunkReaper.Refusal;
import dev.nexus.service.ChunkReaper.RunResult;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 9 (bead nexus-2x9xa): the reaper is WIRED into {@code NexusService}, not merely correct in isolation.
 * Every arm of the sweep loop had its own unit test while nothing proved the scheduler invoked it (nexus-lgiqw), so
 * this drives the instance's own scheduled entry point.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NexusServiceReaperWiringTest {

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource ds;
    private NexusService withVectors;
    private NexusService withoutVectors;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        ds = new com.zaxxer.hikari.HikariDataSource(cfg);

        var zero = new dev.nexus.service.vectors.Embedder() {
            @Override public List<float[]> embed(List<String> texts) {
                return texts.stream().map(t -> new float[384]).toList();
            }
            @Override public void close() { }
        };
        var vectors = new PgVectorRepository(new TenantScope(ds), zero, zero);
        withVectors = new NexusService(0, "reaper-wiring-token", ds, null, vectors, null, null, null);
        withoutVectors = new NexusService(0, "reaper-wiring-token", ds, null, null, null, null, null);
    }

    @AfterAll
    void stopAll() {
        for (NexusService s : new NexusService[] {withVectors, withoutVectors}) {
            if (s != null) {
                try {
                    s.stop();
                } catch (Exception ignored) {
                    // never started
                }
            }
        }
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    @Test
    void anInstanceWithAVectorBackendSchedulesTheReaper_withSamsDefaults() {
        ChunkReaper reaper = withVectors.chunkReaper();
        assertThat(reaper).isNotNull();
        assertThat(reaper.settings()).isEqualTo(ChunkReaper.Settings.defaults());
    }

    @Test
    void anInstanceWithoutAVectorBackendHasNothingToReap() {
        assertThat(withoutVectors.chunkReaper()).isNull();
    }

    @Test
    void theScheduledEntryPointVisitsTheDefaultTenant_andRefusesItWithoutABackfillRecord() {
        RunResult run = withVectors.chunkReaper().run();

        assertThat(run.tenant("default")).as("the default tenant is always visited").isNotNull();
        assertThat(run.tenant("default").tenantRefusal()).isEqualTo(Refusal.BACKFILL_INCOMPLETE);
        assertThat(withVectors.chunkReaper().refusedTotal()).isGreaterThanOrEqualTo(1);
    }
}
