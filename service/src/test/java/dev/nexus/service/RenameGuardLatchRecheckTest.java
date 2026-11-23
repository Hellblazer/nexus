// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.datamove.RenameReadiness.Missing;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.RenameGuard;
import dev.nexus.service.db.OneRowRename;
import dev.nexus.service.db.RenameLatchLock;
import dev.nexus.service.db.RenameRefusedException;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TelemetryRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.locks.LockSupport;
import java.util.function.BiConsumer;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.5a, linda's ruling D2 (bead nexus-p2m1x.23): the rename guard's latch re-check INSIDE the rename transaction, under the
 * lock the latch write takes ({@link RenameLatchLock}). Before it, {@code RenameGuard.requireRenameAllowed()} ran at method entry,
 * outside the transaction, so a rename that passed it just before the gate 3 latch row was written could commit after it.
 *
 * <p>Every rename primitive the guard covers is driven through its own public method, with a real guard built as Main builds it
 * (a {@code RenameReadiness} and the fail-closed {@code OneRowRename}), over a real database: the catalog rename, and the chash, taxonomy, telemetry,
 * aspects and highlights renames. Every refusal below is the row, read inside the transaction by
 * {@code RenameGuard.runRename}; there is no check outside it.
 *
 * <p>No timing is assumed. The order of events is made by the database's own lock state: a session of the test holds a lock, and the
 * test goes on only once {@code pg_locks} shows the other side waiting on it (a condition polled against a deadline, which only
 * fails the test when it is not reached).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RenameGuardLatchRecheckTest {

    private static final String TENANT = "latch-recheck-tenant";
    private static final String SVC_ROLE = "svc_latch_recheck";
    private static final String SVC_PASS = "svc_latch_recheck_pass";
    private static final String MODEL = "bge-base-en-v15-768";
    private static final org.jooq.Table<?> LATCH = DSL.table(DSL.name("nexus", "rdr229_gate_latch"));

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    ExecutorService pool;
    CatalogRepository catalog;
    ChashRepository chash;
    TaxonomyRepository taxonomy;
    TelemetryRepository telemetry;
    AspectRepository aspects;
    final AtomicInteger n = new AtomicInteger();

    /**
     * One rename primitive, by its public method. {@code registeredTarget}: the primitive refuses a target the registry has no row
     * for (chash, taxonomy, aspects, highlights re-point rows at the registry's row, as their callers register it first); the catalog
     * rename makes the target itself, and telemetry needs no row.
     */
    private record Prim(String name, boolean registeredTarget, BiConsumer<String, String> rename) { }

    private List<Prim> prims;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var config = new HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(6);
        config.setAutoCommit(true);
        svcDs = new HikariDataSource(config);
        pool = Executors.newFixedThreadPool(3);

        // Main's shape: readiness says the drop move is missing; the one-row catalog rename is the fail-closed default.
        var guard = new RenameGuard(() -> Missing.NAME_KEY_DROP_MOVE, OneRowRename.failClosed());
        var scope = new TenantScope(svcDs);
        catalog = new CatalogRepository(scope, Gate3.never(), guard);
        chash = new ChashRepository(scope, guard);
        taxonomy = new TaxonomyRepository(scope, guard);
        telemetry = new TelemetryRepository(scope, guard);
        aspects = new AspectRepository(scope, guard);
        prims = List.of(
            new Prim("catalog.renameCollection", false, (o, nw) -> catalog.renameCollection(TENANT, o, nw)),
            new Prim("chash.renameCollection", true, (o, nw) -> chash.renameCollection(TENANT, o, nw)),
            new Prim("taxonomy.renameCollection", true, (o, nw) -> taxonomy.renameCollection(TENANT, o, nw)),
            new Prim("telemetry.renameCollection", false, (o, nw) -> telemetry.renameCollection(TENANT, o, nw)),
            new Prim("aspects.renameAspectCollection", true, (o, nw) -> aspects.renameAspectCollection(TENANT, o, nw)),
            new Prim("aspects.renameHighlightsCollection", true, (o, nw) -> aspects.renameHighlightsCollection(TENANT, o, nw)));
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    @AfterEach
    void noLatch() {
        su(ctx -> ctx.deleteFrom(LATCH).execute());
    }

    // ---- helpers ------------------------------------------------------------------------------------------------------

    private <T> T su(Function<DSLContext, T> work) {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private String register(String stem) {
        return registerNamed("lr-" + stem + "-" + n.incrementAndGet());
    }

    private String registerNamed(String name) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("content_type", "knowledge");
        m.put("owner_id", "own-" + name);
        m.put("embedding_model", MODEL);
        m.put("model_version", "v1");
        catalog.upsertCollection(TENANT, m, true);
        return name;
    }

    private String fresh(String stem) {
        return "lr-" + stem + "-to-" + n.incrementAndGet();
    }

    /** The row's {@code superseded_by}, or null when the tenant has no row of that name. */
    private String supersededBy(String name) {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY));
    }

    private boolean latchRow() {
        return su(ctx -> ctx.fetchExists(LATCH));
    }

    /** The production latch write, on a connection of its own. */
    private void writeLatch() {
        su(ctx -> {
            CompletionCheck.insertLatch(ctx, CompletionCheck.GATE3, "test-engine", false);
            return null;
        });
    }

    /** Whether some session holds ({@code granted}) or waits for ({@code !granted}) the rename/latch lock. */
    private boolean renameLockState(boolean granted) {
        Field<Long> classid = DSL.field(DSL.name("classid")).cast(SQLDataType.BIGINT);
        Field<Long> objid = DSL.field(DSL.name("objid")).cast(SQLDataType.BIGINT);
        return su(ctx -> ctx.fetchExists(ctx.selectOne().from(DSL.table(DSL.name("pg_catalog", "pg_locks")))
            .where(DSL.field(DSL.name("locktype"), String.class).eq("advisory"))
            .and(DSL.field(DSL.name("granted"), Boolean.class).eq(granted))
            .and(classid.eq((long) RenameLatchLock.NAMESPACE)).and(objid.eq((long) RenameLatchLock.GATE3_KEY))));
    }

    /** Waits (against a deadline, no assumption about speed) until the lock state holds, or fails when {@code racer} ended first. */
    private void await(boolean granted, Future<?> racer, String what) {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (!renameLockState(granted)) {
            if (racer != null && racer.isDone() || System.nanoTime() > deadline) {
                throw new AssertionError(what + ": the lock was never " + (granted ? "held" : "waited for") + " (racer done: "
                    + (racer != null && racer.isDone()) + ")");
            }
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        }
    }

    private static RenameRefusedException refusalIn(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof RenameRefusedException r) {
                return r;
            }
        }
        return null;
    }

    private void assertRefused(String prim, Runnable rename) {
        assertThatThrownBy(rename::run).as(prim).satisfies(t -> {
            assertThat(refusalIn(t)).as("%s: a typed rename refusal, got %s", prim, t).isNotNull();
            assertThat(refusalIn(t).missing()).isEqualTo(Missing.NAME_KEY_DROP_MOVE);
        });
    }

    // ---- the latch row decides, inside the transaction -------------------------------------------------------------------

    /** With the latch row there and readiness missing, every primitive is refused with the typed 409 and nothing is renamed. */
    @Test
    void withTheLatchRow_everyPrimitiveIsRefusedAndNothingIsRenamed() {
        for (Prim p : prims) {
            String from = register("row");
            String to = fresh("row");
            writeLatch();

            assertRefused(p.name(), () -> p.rename().accept(from, to));

            assertThat(supersededBy(from)).as("%s: nothing renamed: the source is live", p.name()).isEqualTo("");
            assertThat(supersededBy(to)).as("%s: nothing renamed: the target has no row", p.name()).isNull();
            su(ctx -> ctx.deleteFrom(LATCH).execute());
        }
    }

    /**
     * The other order, with two connections and an explicit lock order: the latch writer holds the lock with its row not yet
     * committed; a rename that has passed the entry check reaches its transaction, waits on the lock, and once the writer commits
     * it reads the row and is refused. (Without the in-transaction read it would have committed on the name path.)
     */
    @Test
    void aRenameThatWaitsOnALatchWriteInFlight_readsTheRowItWaitedForAndIsRefused() throws Exception {
        for (Prim p : prims) {
            String from = register("wait");
            String to = fresh("wait");
            try (Connection w = pg.createConnection("")) {
                w.setAutoCommit(false);
                DSLContext wctx = DSL.using(w, SQLDialect.POSTGRES);
                RenameLatchLock.exclusive(wctx);
                insertLatchRow(wctx);
                assertThat(latchRow()).as("%s: guard: the row is not committed yet", p.name()).isFalse();

                Future<?> rename = pool.submit(() -> p.rename().accept(from, to));
                try {
                    await(false, rename, p.name() + ": the rename");
                    assertThat(rename.isDone()).as("%s: the rename waits for the latch write", p.name()).isFalse();
                } finally {
                    w.commit();
                }

                try {
                    rename.get(60, TimeUnit.SECONDS);
                    throw new AssertionError(p.name() + ": the rename committed after the latch row did");
                } catch (java.util.concurrent.ExecutionException e) {
                    assertThat(refusalIn(e)).as("%s: refused, typed: %s", p.name(), e.getCause()).isNotNull();
                }
            }
            assertThat(supersededBy(from)).as("%s: nothing renamed", p.name()).isEqualTo("");
            assertThat(supersededBy(to)).isNull();
            su(ctx -> ctx.deleteFrom(LATCH).execute());
        }
    }

    // ---- the latch is written after the rename commits ------------------------------------------------------------------

    /** A rename with no latch commits and stands; the latch written after it is no concern of it; the next rename is refused. */
    @Test
    void aRenameThatCommitsBeforeTheLatchStands_andTheNextOneIsRefusedByTheRow() {
        for (Prim p : prims) {
            String from = register("after");
            String to = p.registeredTarget() ? register("after-to") : fresh("after");
            p.rename().accept(from, to);   // no latch: the name path
            if (p.name().startsWith("catalog")) {
                assertThat(supersededBy(from)).as("the catalog rename stands: the source names its successor").isEqualTo(to);
                assertThat(supersededBy(to)).as("and the target is a live row").isEqualTo("");
            }

            writeLatch();
            assertThat(latchRow()).as("%s: the latch write follows the committed rename", p.name()).isTrue();

            String from2 = register("after2");
            String to2 = p.registeredTarget() ? register("after2-to") : fresh("after2");
            assertRefused(p.name(), () -> p.rename().accept(from2, to2));
            su(ctx -> ctx.deleteFrom(LATCH).execute());
        }
    }

    /**
     * A real rename in flight (stopped at its source row, held by a session of the test) holds the lock; the latch write waits for
     * it, does not write the row, and goes on only once the rename has committed. The rename stands and the row is written after it.
     */
    @Test
    void aLatchWriteWaitsForARenameInFlight_andFollowsItsCommit() throws Exception {
        String from = register("inflight");
        String to = fresh("inflight");
        Future<?> latch = null;
        Future<?> rename;
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(from)).forUpdate().fetch();

            rename = pool.submit(() -> catalog.renameCollection(TENANT, from, to));
            try {
                await(true, rename, "the rename holds the lock");   // it took the shared lock; it stands still at the held source row
                latch = pool.submit(this::writeLatch);
                await(false, latch, "the latch write");
                assertThat(latch.isDone()).as("the latch write waits for the rename in flight").isFalse();
                assertThat(rename.isDone()).as("guard: the rename is held at the source row").isFalse();
                assertThat(latchRow()).as("no latch row while the rename is in flight").isFalse();
            } finally {
                holder.rollback();
            }
        }
        rename.get(60, TimeUnit.SECONDS);
        latch.get(60, TimeUnit.SECONDS);

        assertThat(supersededBy(from)).as("the rename stands: the source names its successor").isEqualTo(to);
        assertThat(supersededBy(to)).as("and the target is live").isEqualTo("");
        assertThat(latchRow()).as("the latch row was written once the rename was done").isTrue();
        assertThatThrownBy(() -> {
            try {
                pool.submit(() -> catalog.renameCollection(TENANT, register("inflight2"), fresh("inflight2"))).get(60, TimeUnit.SECONDS);
            } catch (java.util.concurrent.ExecutionException e) {
                throw e.getCause();
            }
        }).as("after the latch, the next rename is refused").satisfies(t -> assertThat(refusalIn(t)).isNotNull());
    }

    private RenameGuard.RenamePath runRenameDecision(RenameGuard g) {
        return g.runRename(new TenantScope(svcDs), TENANT, ctx -> RenameGuard.RenamePath.NAME_PATH, ctx -> RenameGuard.RenamePath.ONE_ROW);
    }

    /**
     * runRename decides from the latch row: none is the name path, the row with readiness complete is the one-row body, the row with
     * readiness missing is the refusal. The open guard is always the name path.
     */
    @Test
    void runRenameDecidesFromTheLatchRowOnly() {
        var ready = new RenameGuard(() -> Missing.NONE, OneRowRename.failClosed());
        var notReady = new RenameGuard(() -> Missing.NAME_KEY_DROP_MOVE, OneRowRename.failClosed());
        assertThat(runRenameDecision(ready)).as("no row: the name path").isEqualTo(RenameGuard.RenamePath.NAME_PATH);

        writeLatch();
        assertThat(runRenameDecision(ready)).as("the row and readiness complete").isEqualTo(RenameGuard.RenamePath.ONE_ROW);
        assertThat(runRenameDecision(RenameGuard.open())).as("the open guard never leaves the name path").isEqualTo(RenameGuard.RenamePath.NAME_PATH);
        assertRefused("runRename", () -> runRenameDecision(notReady));
    }

    /**
     * The lock order, with the writer holding the lock and its row not yet committed: the decision waits (pg_locks), and once the writer
     * commits it is not the name path. A decision made without the lock would have answered the name path at once.
     */
    @Test
    void theDecisionWaitsForALatchWriteInFlight_andIsNotTheNamePathAfterIt() throws Exception {
        var ready = new RenameGuard(() -> Missing.NONE, OneRowRename.failClosed());
        try (Connection w = pg.createConnection("")) {
            w.setAutoCommit(false);
            DSLContext wctx = DSL.using(w, SQLDialect.POSTGRES);
            RenameLatchLock.exclusive(wctx);
            insertLatchRow(wctx);
            Future<RenameGuard.RenamePath> decision = pool.submit(() -> runRenameDecision(ready));
            try {
                await(false, decision, "the decision");
                assertThat(decision.isDone()).as("the decision waits for the latch write").isFalse();
            } finally {
                w.commit();
            }
            assertThat(decision.get(60, TimeUnit.SECONDS)).as("it read the row it waited for").isEqualTo(RenameGuard.RenamePath.ONE_ROW);
        }
    }

    /**
     * The rename lock is the FIRST thing every primitive's transaction takes: while a primitive waits on it, that backend holds no
     * advisory lock and no relation lock for a write. Moving the call below the sweep gate, the re-embed guard or a row statement
     * would show a held lock here.
     */
    @Test
    void everyPrimitiveTakesTheRenameLockBeforeAnyOtherLock() throws Exception {
        for (Prim p : prims) {
            String from = register("order");
            String to = p.registeredTarget() ? register("order-to") : fresh("order");
            Future<?> rename;
            try (Connection w = pg.createConnection("")) {
                w.setAutoCommit(false);
                RenameLatchLock.exclusive(DSL.using(w, SQLDialect.POSTGRES));
                rename = pool.submit(() -> p.rename().accept(from, to));
                try {
                    await(false, rename, p.name());
                    int pid = su(ctx -> ctx.select(DSL.field(DSL.name("pid"), Integer.class)).from(DSL.table(DSL.name("pg_catalog", "pg_locks")))
                        .where(DSL.field(DSL.name("locktype"), String.class).eq("advisory"))
                        .and(DSL.field(DSL.name("granted"), Boolean.class).eq(false)).fetchAny(0, Integer.class));
                    int held = su(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
                        DSL.field(DSL.name("pid"), Integer.class).eq(pid).and(DSL.field(DSL.name("granted"), Boolean.class).eq(true))
                            .and(DSL.field(DSL.name("locktype"), String.class).eq("advisory")
                                .or(DSL.field(DSL.name("locktype"), String.class).eq("relation")
                                    .and(DSL.field(DSL.name("mode"), String.class).in("RowExclusiveLock", "RowShareLock", "ShareLock"))))));
                    assertThat(held).as("%s: while it waits for the rename lock it holds no other lock", p.name()).isZero();
                } finally {
                    w.rollback();
                }
            }
            rename.get(60, TimeUnit.SECONDS);   // no latch row: the name path completes once the writer is gone
        }
    }

    /** Under REPEATABLE READ the snapshot predates the lock wait, so the read of the row could be stale: runRename fails closed. */
    @Test
    void aTransactionThatIsNotReadCommittedFailsClosed() throws Exception {
        var guard = new RenameGuard(() -> Missing.NONE, OneRowRename.failClosed());
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            c.setTransactionIsolation(Connection.TRANSACTION_REPEATABLE_READ);
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            assertThatThrownBy(() -> guard.runRename(work -> work.apply(ctx), x -> "name", x -> "one"))
                .isInstanceOf(IllegalStateException.class).hasMessageContaining("READ COMMITTED").hasMessageContaining("repeatable read");
            c.rollback();
        }
    }

    /**
     * The latch write's lock wait is bounded: with a rename's shared lock held it throws at the bound (here 500 ms), writes no row,
     * and a second call after the release succeeds.
     */
    @Test
    void theLatchWriteGivesUpAtItsLockTimeout_writesNoRow_andSucceedsOnceTheRenameIsDone() throws Exception {
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            RenameLatchLock.shared(DSL.using(holder, SQLDialect.POSTGRES));   // a rename in flight
            assertThatThrownBy(() -> su(ctx -> {
                CompletionCheck.insertLatch(ctx, CompletionCheck.GATE3, "test-engine", false, "500");
                return null;
            })).as("lock_not_available").hasMessageContaining("lock timeout");
            assertThat(latchRow()).as("no row was written").isFalse();
            holder.rollback();
        }
        su(ctx -> {
            CompletionCheck.insertLatch(ctx, CompletionCheck.GATE3, "test-engine", false, "500");
            return null;
        });
        assertThat(latchRow()).as("after the release the write goes through").isTrue();
    }

    /** The open guard takes no lock and reads no row: what a repository built without a guard gets. */
    @Test
    void theOpenGuardTakesNoLockAndReadsNoRow() throws Exception {
        try (Connection w = pg.createConnection("")) {
            w.setAutoCommit(false);
            RenameLatchLock.exclusive(DSL.using(w, SQLDialect.POSTGRES));   // a latch write in flight
            Future<?> f = pool.submit(() -> runRenameDecision(RenameGuard.open()));
            try {
                f.get(30, TimeUnit.SECONDS);   // returns at once: it does not wait on the lock the writer holds
            } catch (TimeoutException e) {
                throw new AssertionError("the open guard waited on the latch lock", e);
            } finally {
                w.rollback();
            }
        }
    }

    private void insertLatchRow(DSLContext wctx) {
        wctx.insertInto(LATCH).set(DSL.field(DSL.name("gate"), String.class), CompletionCheck.GATE3)
            .set(DSL.field(DSL.name("latched_at"), java.time.OffsetDateTime.class), java.time.OffsetDateTime.now())
            .set(DSL.field(DSL.name("engine_version"), String.class), "test-writer").execute();
    }
}
