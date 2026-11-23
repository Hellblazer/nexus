// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), the after-serve registration pass of the name-key drop move ({@link NameKeyDropPass}; linda 2026-11-22, addendum 2):
 * none before gate 3 is latched, exactly one after, idempotent across boots (a second pass object, a second engine's pass), and nothing while a schema
 * migration holds its lock. The pass writes on the admin connection; the completion check stays read only.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NameKeyDropPassIntegrationTest {

    private static final String VERSION = "9.9.9-pass-test";

    private PostgreSQLContainer<?> pg;
    private AdminConnection admin;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        admin = new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
    }

    @AfterAll
    void stop() {
        if (pg != null) {
            pg.stop();
        }
    }

    @BeforeEach
    void clean() {
        su(ctx -> ctx.deleteFrom(DSL.table(DSL.name("nexus", "data_moves")))
            .where(DSL.field(DSL.name("label"), String.class).eq(NameKeyDropMove.LABEL)).execute());
    }

    private <T> T su(java.util.function.Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private int rows() {
        return su(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", "data_moves")),
            DSL.field(DSL.name("label"), String.class).eq(NameKeyDropMove.LABEL)));
    }

    private static CompletionCheck.Result snapshot(boolean gate3, boolean latched) {
        return new CompletionCheck.Result(true, true, gate3, List.of(), List.of(), true, 0L, List.of(), List.of(), "",
            CompletionCheck.Dangling.UNREAD, false, latched, false, false, CompletionCheck.RenameState.NOT_READ);
    }

    @Test
    void nothingIsRegisteredBeforeGate3IsLatched_notWithoutASnapshot_notWithAHoldingButUnlatchedOne() {
        var none = new NameKeyDropPass(admin, () -> null, VERSION);
        assertThat(none.runOnce()).isEqualTo(NameKeyDropPass.Outcome.WAITING);
        var unlatched = new NameKeyDropPass(admin, () -> snapshot(true, false), VERSION);
        assertThat(unlatched.runOnce()).as("gate 3 holds live but the latch is not written: the latch is the gate").isEqualTo(NameKeyDropPass.Outcome.WAITING);
        assertThat(rows()).as("no row before the latch").isZero();
    }

    @Test
    void exactlyOneRowAfterTheLatch_andTheRowCarriesTheEngineVersion() {
        var pass = new NameKeyDropPass(admin, () -> snapshot(false, true), VERSION);   // a live dip after the latch still registers

        assertThat(pass.runOnce()).isEqualTo(NameKeyDropPass.Outcome.REGISTERED);

        assertThat(rows()).isEqualTo(1);
        String changesetId = su(ctx -> ctx.select(DSL.field(DSL.name("changeset_id"), String.class)).from(DSL.table(DSL.name("nexus", "data_moves")))
            .where(DSL.field(DSL.name("label"), String.class).eq(NameKeyDropMove.LABEL)).fetchOne(0, String.class));
        assertThat(changesetId).isEqualTo("engine:rdr229-name-key-drop:" + VERSION);
        assertThat(pass.runOnce()).as("a finished pass opens no connection and does nothing").isEqualTo(NameKeyDropPass.Outcome.FINISHED);
    }

    @Test
    void idempotentAcrossBoots_aSecondPassObjectAndASecondEnginesPassAddNothing() {
        assertThat(new NameKeyDropPass(admin, () -> snapshot(true, true), VERSION).runOnce()).isEqualTo(NameKeyDropPass.Outcome.REGISTERED);
        assertThat(rows()).isEqualTo(1);

        // the next boot: a new pass object over the same database, and a peer engine's pass at the same time
        assertThat(new NameKeyDropPass(admin, () -> snapshot(true, true), VERSION).runOnce()).isEqualTo(NameKeyDropPass.Outcome.PRESENT);
        assertThat(new NameKeyDropPass(admin, () -> snapshot(true, true), "9.9.9-peer").runOnce()).isEqualTo(NameKeyDropPass.Outcome.PRESENT);

        assertThat(rows()).as("still exactly one row").isEqualTo(1);
    }

    @Test
    void nothingIsRegisteredWhileASchemaMigrationHoldsItsLock_andTheNextTickRegistersOnce() throws Exception {
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(true);
            var ctx = DSL.using(holder, SQLDialect.POSTGRES);
            ctx.select(DSL.function("pg_advisory_lock", org.jooq.impl.SQLDataType.OTHER, DSL.val(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY))).fetch();   // the walk is running
            var pass = new NameKeyDropPass(admin, () -> snapshot(true, true), VERSION);

            assertThat(pass.runOnce()).as("the pass yields to the migration").isEqualTo(NameKeyDropPass.Outcome.MIGRATING);
            assertThat(rows()).as("nothing registered during migrate").isZero();

            ctx.select(DSL.function("pg_advisory_unlock", org.jooq.impl.SQLDataType.BOOLEAN, DSL.val(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY))).fetch();
            assertThat(pass.runOnce()).as("the next tick, the walk over").isEqualTo(NameKeyDropPass.Outcome.REGISTERED);
            assertThat(rows()).isEqualTo(1);
        }
    }

    @Test
    void anUnreachableDatabaseIsFailed_nothingIsRegisteredAndTheNextTickTriesAgain() {
        var pass = new NameKeyDropPass(new AdminConnection("jdbc:postgresql://127.0.0.1:1/none", "u", "p"), () -> snapshot(true, true), VERSION);

        assertThat(pass.runOnce()).as("the connection is refused: FAILED, logged, not thrown").isEqualTo(NameKeyDropPass.Outcome.FAILED);

        assertThat(rows()).isZero();
        assertThat(pass.runOnce()).as("a failed tick does not finish the pass: the next tick tries again").isEqualTo(NameKeyDropPass.Outcome.FAILED);
    }

    @Test
    void aBlankVersionIsAWiringDefect_refusedAtConstruction() {
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> new NameKeyDropPass(admin, () -> snapshot(true, true), " "))
            .isInstanceOf(IllegalArgumentException.class);
    }
}
