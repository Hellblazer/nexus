// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_META;
import static org.assertj.core.api.Assertions.assertThat;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.Container.ExecResult;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.util.Optional;

/**
 * RDR-229 P3.11 (nexus-p2m1x.31): a PostgreSQL restart changes the catalog epoch, with nothing else changed. A snapshot
 * restored on the same timeline always needs a restart, and a table-held value would rewind with the snapshot, so the
 * postmaster start time is part of the epoch ({@link CatalogEpoch}).
 *
 * <p>The container's own postmaster is PID 1, so restarting it ends the container (and its tmpfs data with it). This
 * class starts a second postmaster in the same container, on a published port, and restarts that one in place. It is a
 * cluster of its own: the shared cluster is never restarted.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogEpochRestartTest {

    private static final int SECOND_PORT = 5433;
    private static final String DATA = "/tmp/pg-epoch-restart";
    private static final String SVC_ROLE = "svc_epoch_restart";
    private static final String SVC_PASS = "svc_epoch_restart_pass";
    private static final String TENANT = "epoch-restart-tenant";

    PostgreSQLContainer<?> pg;
    String jdbcUrl;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.newContainer();
        pg.addExposedPort(SECOND_PORT);
        pg.start();
        exec("gosu", "postgres", "initdb", "-D", DATA, "-U", "postgres", "--auth=trust", "-E", "UTF8");
        exec("sh", "-c", "echo 'host all all 0.0.0.0/0 trust' >> " + DATA + "/pg_hba.conf");
        exec("gosu", "postgres", "pg_ctl", "-D", DATA, "-w", "-l", "/tmp/pg-epoch-restart.log",
            "-o", "-p " + SECOND_PORT + " -c listen_addresses=* -c unix_socket_directories=/tmp -c fsync=off", "start");
        jdbcUrl = "jdbc:postgresql://" + pg.getHost() + ":" + pg.getMappedPort(SECOND_PORT) + "/postgres";
        try (Connection su = DriverManager.getConnection(jdbcUrl, "postgres", "")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = DriverManager.getConnection(jdbcUrl, "postgres", "")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    private void exec(String... command) throws Exception {
        ExecResult r = pg.execInContainer(command);
        assertThat(r.getExitCode()).as(String.join(" ", command) + "\n" + r.getStdout() + r.getStderr()).isZero();
    }

    private HikariDataSource pool() {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(jdbcUrl);
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    private record Reading(Optional<String> epoch, CatalogEpoch.ClusterIdentity cluster) {}

    /** The engine's epoch for the tenant, and the cluster facts it was made from, on a fresh pool. */
    private Reading read() {
        try (HikariDataSource ds = pool()) {
            return new TenantScope(ds).withTenant(TENANT,
                ctx -> new Reading(CatalogEpoch.of(ctx, TENANT), CatalogEpoch.readCluster(ctx).orElseThrow()));
        }
    }

    @Test
    void aPostgresRestartChangesTheEpochAndNothingElseDid() throws Exception {
        try (HikariDataSource ds = pool()) {
            new TenantScope(ds).withTenant(TENANT, (DSLContext ctx) -> ctx.insertInto(CATALOG_META)
                .set(CATALOG_META.TENANT_ID, TENANT)
                .set(CATALOG_META.KEY, TenantImportLoad.MARKER_KEY)
                .set(CATALOG_META.VALUE, "{\"session_id\":\"before-restart\"}")
                .onConflict(CATALOG_META.TENANT_ID, CATALOG_META.KEY).doNothing()
                .execute());
        }
        Reading before = read();
        assertThat(read().epoch()).as("non-vacuity: stable without a restart").isEqualTo(before.epoch());
        assertThat(before.epoch()).isPresent();

        exec("gosu", "postgres", "pg_ctl", "-D", DATA, "-w", "-m", "fast", "restart");

        Reading after = read();
        assertThat(after.cluster().systemIdentifier()).as("same cluster").isEqualTo(before.cluster().systemIdentifier());
        assertThat(after.cluster().timelineId()).as("same timeline").isEqualTo(before.cluster().timelineId());
        assertThat(after.cluster().postmasterStart()).as("the postmaster start time moved")
            .isNotEqualTo(before.cluster().postmasterStart());
        assertThat(after.epoch()).as("the epoch changed with the restart alone").isPresent().isNotEqualTo(before.epoch());
    }
}
