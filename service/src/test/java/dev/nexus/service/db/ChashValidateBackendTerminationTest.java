// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.ChashConstraintValidator.Outcome;
import dev.nexus.service.db.ChashConstraintValidator.Step;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/**
 * nexus-f3yqd, engine 0.1.190, fix round r1 (critic S1): an orphaned VALIDATE does not stall a schema walk. A VALIDATE
 * takes SHARE UPDATE EXCLUSIVE for as long as it scans (up to 30 minutes) and the backend outlives an engine that died; a walk
 * with schema work queues its ACCESS EXCLUSIVE behind it, and every statement waits behind that. {@code SchemaMigrator} ends
 * {@code nexus-chash-validate*} backends where it ends the index builders, and the next job resumes (a VALIDATE is atomic).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashValidateBackendTerminationTest {

    private static final String CHUNKS_CHECK = "chunks_chash_octet_check";

    private PostgreSQLContainer<?> pg;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    private AdminConnection admin() {
        return new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
    }

    /** The number of backends of this database whose application_name starts with the validator's prefix. */
    private int validatorBackends() throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).selectCount()
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(DSL.field(DSL.name("application_name"), String.class).startsWith(ChashConstraintValidator.APPLICATION_NAME_PREFIX))
                .and(DSL.field(DSL.name("datname"), String.class).eq(DSL.function("current_database", SQLDataType.VARCHAR)))
                .fetchOne(0, Integer.class);
        }
    }

    /** True when a validator step backend waits on a lock: its VALIDATE is queued behind the lock the test holds. */
    private boolean stepWaitsOnALock() throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(DSL.selectOne()
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(DSL.field(DSL.name("application_name"), String.class).startsWith(ChashConstraintValidator.APPLICATION_NAME_PREFIX + "-step"))
                .and(DSL.field(DSL.name("wait_event_type"), String.class).eq("Lock")));
        }
    }

    private void awaitTrue(java.util.concurrent.Callable<Boolean> cond, String what) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (System.nanoTime() < deadline) {
            if (cond.call()) return;
            Thread.sleep(50);
        }
        throw new AssertionError("timed out waiting for: " + what);
    }

    /** A leaf of the chunks tree (a constraint row whose table has no copy below it). */
    private String aLeaf() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var rows = ChashConformanceConstraints.query(DSL.using(su, SQLDialect.POSTGRES)).fetch();
            var parents = new java.util.HashSet<Long>();
            rows.forEach(r -> parents.add(r.value2()));
            return rows.stream().filter(r -> !"chunks_retired_225".equals(r.value3()) && r.value2() != 0 && !parents.contains(r.value1()))
                .map(r -> r.value3()).sorted().findFirst().orElseThrow();
        }
    }

    /** convalidated of {@code relname}'s copy of the chunks width CHECK. */
    private boolean validity(String relname) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return ChashConformanceConstraints.query(DSL.using(su, SQLDialect.POSTGRES)).fetch().stream()
                .filter(r -> r.value3().equals(relname)).findFirst().orElseThrow().value5();
        }
    }

    private Connection holdShare(String relname) throws Exception {
        Connection c = pg.createConnection("");
        c.setAutoCommit(false);
        DSL.using(c, SQLDialect.POSTGRES).createIndex(DSL.name("held_" + relname + "_idx"))
            .on(DSL.table(DSL.name("nexus", relname)), DSL.field(DSL.name("chash"))).execute();
        return c;
    }

    private static final class Quiet implements ChashConstraintValidator.Listener {
        final List<Step> steps = new ArrayList<>();

        @Override public void planned(int total) { }
        @Override public void current(String table, Long bytes) { }
        @Override public void stepped(Step step) { steps.add(step); }
        @Override public String stopReason() { return null; }
    }

    private HikariDataSource migrationPool() {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(1);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    /**
     * An orphaned VALIDATE (its engine gone; here: a run held open by another session's SHARE lock with a long lock wait) is
     * ended by the next boot's walk when the walk has schema work, and the next job resumes at the step that was cut.
     * Kills: the validator prefix missing from SchemaMigrator's terminate set (the backend would still be there).
     */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void aWalkWithSchemaWork_endsAnOrphanedValidate_andTheNextJobResumes() throws Exception {
        try {
            orphanedValidateScenario();
        } finally {
            System.clearProperty(ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY);
            System.clearProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY);
            System.clearProperty(ChashConstraintValidator.PAUSE_PROPERTY);
        }
    }

    private void orphanedValidateScenario() throws Exception {
        System.setProperty(ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY, "600000");
        System.setProperty(ChashConstraintValidator.LOCK_ATTEMPTS_PROPERTY, "1");
        System.setProperty(ChashConstraintValidator.PAUSE_PROPERTY, "0");
        String leaf = aLeaf();
        Connection held = holdShare(leaf);
        try (HikariDataSource migrationDs = migrationPool()) {
            Quiet first = new Quiet();
            CompletableFuture<Object> run = CompletableFuture.supplyAsync(() -> {
                try {
                    return new ChashConstraintValidator(admin()).run(false, first);
                } catch (RuntimeException e) {
                    return e;
                }
            });
            awaitTrue(this::stepWaitsOnALock, "the VALIDATE queued behind the SHARE lock");
            assertThat(validatorBackends()).isPositive();

            // a real schema changeset pending: the walk ends the validator's backends before it starts
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                int deleted = DSL.using(su, SQLDialect.POSTGRES).deleteFrom(DSL.table(DSL.name("public", "databasechangelog")))
                    .where(DSL.field(DSL.name("id"), String.class).eq("vectors-058-1")).execute();
                assertThat(deleted).as("vectors-058-1 was applied").isEqualTo(1);
            }
            var walk = CompletableFuture.runAsync(() -> SchemaMigrator.migrate(migrationDs));
            walk.get(120, TimeUnit.SECONDS);

            Object ended = run.get(60, TimeUnit.SECONDS);
            System.out.println("orphaned run ended with: " + ended);
            assertThat(ended).as("the orphaned run's backend was ended under it").isInstanceOf(RuntimeException.class);
            awaitTrue(() -> validatorBackends() == 0, "no validator backend left");
            assertThat(first.steps.stream().filter(s -> s.table().equals("nexus." + leaf)))
                .as("the cut step never reported a result: it was ended mid-VALIDATE, not finished or deferred").isEmpty();
            assertThat(validity(leaf)).as("and its constraint is exactly as it was: NOT VALID (a VALIDATE is atomic)").isFalse();
        } finally {
            held.rollback();
            held.close();
        }
        // resume: the next job validates what is left; a validated constraint stayed validated through the termination
        System.setProperty(ChashConstraintValidator.LOCK_TIMEOUT_PROPERTY, "5000");
        Quiet second = new Quiet();
        var result = new ChashConstraintValidator(admin()).run(false, second);
        assertThat(result.stopped()).isNull();
        assertThat(result.allValidated()).isTrue();
        assertThat(second.steps.stream().filter(s -> s.outcome() == Outcome.REFUSED || s.outcome() == Outcome.DEFERRED)).isEmpty();
        assertThat(second.steps.stream().anyMatch(s -> s.table().equals("nexus." + leaf) && s.outcome() == Outcome.VALIDATED))
            .as("the leaf that was cut is validated by the rerun").isTrue();
    }
}
