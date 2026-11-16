// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.db.RestoreLoadLock;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;

/**
 * The restore-load lock between a tenant restore and {@code nexus.create_tenant_partitions} (RDR-248 Step 5, bead
 * nexus-csayy.6, changelog vectors-059): the slow path, the one that runs DDL, refuses at once while a load holds the
 * key, the fast path takes no lock, {@code p_check_restore = false} skips the check, a creation in flight keeps a
 * load from starting, and the Java key and the SQL key are one key.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RestoreLoadLockTest {

    private PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    private Connection connect(boolean autocommit) throws SQLException {
        Connection c = pg.createConnection("");
        c.setAutoCommit(autocommit);
        return c;
    }

    private static DSLContext dsl(Connection c) {
        return DSL.using(c, SQLDialect.POSTGRES);
    }

    private static boolean refusedAsLockNotAvailable(Throwable t) {
        for (; t != null; t = t.getCause()) {
            if (t instanceof SQLException s && "55P03".equals(s.getSQLState())) return true;
        }
        return false;
    }

    @Test
    void theSlowPathRefusesAtOnceWhileALoadHoldsTheKeyAndSucceedsWhenItDoesNot() throws Exception {
        try (Connection load = connect(false); Connection create = connect(true)) {
            assertThat(RestoreLoadLock.tryExclusive(dsl(load))).as("the load takes the key").isTrue();
            assertThat(RestoreLoadLock.heldByLoad(dsl(create))).as("and a probe from another session sees it held").isTrue();

            long t0 = System.nanoTime();
            assertThatThrownBy(() -> PartitionScratch.createTenantPartitions(dsl(create), "chunks", "t-lock-new", true))
                .matches(RestoreLoadLockTest::refusedAsLockNotAvailable, "lock_not_available (55P03)");
            assertThat((System.nanoTime() - t0) / 1_000_000).as("at once, not a lock wait").isLessThan(1_000);

            load.rollback();
            assertThat(RestoreLoadLock.heldByLoad(dsl(create))).isFalse();
            assertThat(PartitionScratch.createTenantPartitions(dsl(create), "chunks", "t-lock-new", true))
                .as("it makes the leaves the refused call did not, once the load is over").isPositive();
        }
    }

    @Test
    void theFastPathTakesNoLockSoATenantWhoseLeavesExistIsNeverDelayedByALoad() throws Exception {
        try (Connection load = connect(false); Connection create = connect(true)) {
            assertThat(PartitionScratch.createTenantPartitions(dsl(create), "chunks", "t-lock-existing", true)).isPositive();
            assertThat(RestoreLoadLock.tryExclusive(dsl(load))).isTrue();

            assertThat(PartitionScratch.createTenantPartitions(dsl(create), "chunks", "t-lock-existing", true))
                .as("every leaf present: the fast path, no lock, no refusal").isZero();
            load.rollback();
        }
    }

    @Test
    void checkRestoreFalseSkipsTheCheckForTheDoctorAndForOperators() throws Exception {
        try (Connection load = connect(false); Connection create = connect(true)) {
            assertThat(RestoreLoadLock.tryExclusive(dsl(load))).isTrue();
            assertThat(PartitionScratch.createTenantPartitions(dsl(create), "chunks", "t-lock-walk", true, false))
                .as("p_check_restore = false creates the leaves while a load holds the key").isPositive();
            load.rollback();
        }
    }

    @Test
    void aCreationInFlightKeepsALoadFromStartingUntilItsTransactionEnds() throws Exception {
        try (Connection create = connect(false); Connection load = connect(false)) {
            assertThat(PartitionScratch.createTenantPartitions(dsl(create), "chunks", "t-lock-inflight", true)).isPositive();   // shared key, held to commit

            assertThat(RestoreLoadLock.tryExclusive(dsl(load))).as("a creation holds the key shared: the load's try-lock fails").isFalse();
            assertThat(RestoreLoadLock.heldByLoad(dsl(load))).as("and that is not a load").isFalse();

            create.commit();
            assertThat(RestoreLoadLock.tryExclusive(dsl(load))).as("the creation ended").isTrue();
            load.rollback();
        }
    }

    @Test
    void theKeyTheJavaSideTakesIsTheKeyTheFunctionChecks() throws Exception {
        assertThat(RestoreLoadLock.KEY_NAME).isEqualTo("nexus.restore_load");
        try (Connection c = connect(true)) {
            DSLContext ctx = dsl(c);
            Long key = ctx.select(DSL.function("hashtextextended", org.jooq.impl.SQLDataType.BIGINT, DSL.val(RestoreLoadLock.KEY_NAME), DSL.inline(0L)))
                .fetchOne(0, Long.class);
            assertThat(key).isNotNull();
            // the function body spells the text out: it must hash to the same key the Java lock does
            String body = ctx.select(DSL.field(DSL.name("prosrc"), String.class)).from(DSL.table(DSL.name("pg_catalog", "pg_proc")))
                .where(DSL.field(DSL.name("proname"), String.class).eq("create_tenant_partitions")).fetchOne(0, String.class);
            assertThat(body).contains("hashtextextended('" + RestoreLoadLock.KEY_NAME + "', 0)");
        }
    }
}
