// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.ChashConformanceConstraints;
import dev.nexus.service.db.ChashConstraintValidator;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenStore;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.concurrent.TimeUnit;

/**
 * nexus-f3yqd, engine 0.1.190, fix round r1 (critic S1): the engine's shutdown cancels the VALIDATE a running job has in
 * flight, so the backend does not keep scanning (or queueing) after the engine that asked for it is gone. The
 * {@code SchemaMigrator} side (an orphan that did survive) is {@code ChashValidateBackendTerminationTest}'s.
 */
class ChashValidateShutdownTest {

    private static final String ROOT = "tok-root-chash-shut-0123456789abcdef";

    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void stoppingTheEngine_cancelsTheVALIDATEInFlight_andNoValidatorBackendRemains() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        NexusService service = null;
        HikariDataSource ds = null;
        Connection held = null;
        System.setProperty(ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY, "600000");
        System.setProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY, "1");
        System.setProperty(ChashConstraintValidator.PAUSE_PROPERTY, "0");
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.applyProductSchema(su);
            }
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), ROOT, TenantConstants.DEFAULT_TENANT,
                    TokenStore.ROOT_TOKEN_LABEL, TokenStore.SCOPE_ROOT, null, null);
            }
            var cfg = new HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(PgContainerHelper.SVC_USERNAME);
            cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
            cfg.setMaximumPoolSize(4);
            cfg.setAutoCommit(true);
            ds = new HikariDataSource(cfg);
            service = new NexusService(0, ROOT, ds);
            service.chashConstraintValidator(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()));
            service.start();

            // a SHARE lock on a leaf: the job's VALIDATE of it queues behind the lock (an index build in an open transaction)
            String leaf;
            try (Connection su = pg.createConnection("")) {
                var rows = ChashConformanceConstraints.query(DSL.using(su, SQLDialect.POSTGRES)).fetch();
                var parents = new java.util.HashSet<Long>();
                rows.forEach(r -> parents.add(r.value2()));
                leaf = rows.stream().filter(r -> !"chunks_retired_225".equals(r.value3()) && r.value2() != 0 && !parents.contains(r.value1()))
                    .map(r -> r.value3()).sorted().findFirst().orElseThrow();
            }
            held = pg.createConnection("");
            held.setAutoCommit(false);
            DSL.using(held, SQLDialect.POSTGRES).createIndex(DSL.name("held_shut_idx"))
                .on(DSL.table(DSL.name("nexus", leaf)), DSL.field(DSL.name("chash"))).execute();

            var client = TestHttp.client();
            var resp = client.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/admin/validate-constraints")
                .header("Authorization", "Bearer " + ROOT).POST(HttpRequest.BodyPublishers.ofString("{}")).build(),
                HttpResponse.BodyHandlers.ofString());
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(202);
            new ObjectMapper().readTree(resp.body()).get("job_id").asText();

            awaitTrue(() -> stepWaitsOnALock(pg), "the step's VALIDATE is in flight, queued behind the SHARE lock (a Lock wait event)");
            service.stop();
            service = null;
            awaitTrue(() -> backends(pg, "") == 0, "the engine's shutdown cancelled the VALIDATE: no validator backend remains");
        } finally {
            if (held != null) {
                held.rollback();
                held.close();
            }
            if (service != null) service.stop();
            if (ds != null) ds.close();
            System.clearProperty(ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY);
            System.clearProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY);
            System.clearProperty(ChashConstraintValidator.PAUSE_PROPERTY);
            pg.stop();
        }
    }

    /** True when a validator step backend waits on a lock: its VALIDATE has been issued and is queued, not merely connected. */
    private static boolean stepWaitsOnALock(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(DSL.selectOne()
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(DSL.field(DSL.name("application_name"), String.class)
                    .startsWith(ChashConstraintValidator.APPLICATION_NAME_PREFIX + "-step"))
                .and(DSL.field(DSL.name("wait_event_type"), String.class).eq("Lock")));
        }
    }

    private static int backends(PostgreSQLContainer<?> pg, String suffix) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).selectCount()
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(DSL.field(DSL.name("application_name"), String.class)
                    .startsWith(ChashConstraintValidator.APPLICATION_NAME_PREFIX + suffix))
                .and(DSL.field(DSL.name("datname"), String.class).eq(DSL.function("current_database", SQLDataType.VARCHAR)))
                .fetchOne(0, Integer.class);
        }
    }

    private static void awaitTrue(java.util.concurrent.Callable<Boolean> cond, String what) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (System.nanoTime() < deadline) {
            if (cond.call()) return;
            Thread.sleep(50);
        }
        throw new AssertionError("timed out waiting for: " + what);
    }
}
