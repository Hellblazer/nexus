// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgActivityProbe;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.5a row 14 (nexus-p2m1x.23): the collection delete reads the registry rows of the quarantine siblings whose rows it removes,
 * {@link CollectionDeleteFence}. Verb races: a retiring verb (a restore or a rename of a sibling) that holds the sibling's row
 * {@code FOR UPDATE} is mid-flight when the delete starts; the delete must wait on that row BEFORE it removes any of the sibling's rows
 * (its first wait used to be the registry row delete at its very end, after the rows were gone), and then finish with no deadlock and no
 * rows left behind.
 *
 * <p>The re-embed jobs (nexus-p2m1x.23, linda's ruling): a delete that arrives through the route refuses while a job in a non-terminal state names the
 * collection as its source or target, checked under the per-tenant start guard and the origin's registry row, so a job that starts while the
 * delete runs cannot slip in.
 *
 * <p>The verb is a hand session on its own connection, as {@code GcRegistryRowHeldByVerbTest} does it: {@code FOR UPDATE} on the row is all a
 * verb's first step is. The wait is observed through {@code pg_stat_activity} ({@link PgActivityProbe}), never a sleep.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionDeleteFenceTest {

    private static final String SVC_ROLE = "svc_rdr229_delfence";
    private static final String SVC_PASS = "svc_rdr229_delfence_pass";
    private static final String T = "row14-delete-fence";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    CatalogRepository catalog;
    ExecutorService pool;
    private final AtomicInteger n = new AtomicInteger();

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        // the in-place rename test renames one registry row, which the name-keyed foreign keys (chunks, documents, ...) refuse while a row
        // still names the old label; after P4.1 they are gone. This class's container is its own, so the keys are dropped here, as
        // HeavyRoutesRenameGapTest.prepared does on its throwaway database. The primary key stays (nothing here needs it gone).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (var key : CATALOG_COLLECTIONS.getKeys()) {
                if (!key.getFields().contains(CATALOG_COLLECTIONS.NAME)) continue;
                for (var fk : key.getReferences()) {
                    ctx.alterTable(fk.getTable()).dropConstraintIfExists(fk.getName()).execute();
                }
            }
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        catalog = new CatalogRepository(scope);
        pool = Executors.newCachedThreadPool();
    }

    @AfterAll
    void stop() {
        if (pool != null) pool.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private <R> R su(Function<DSLContext, R> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    /** An origin with a quarantine sibling that holds one of its rows; returns {origin, sibling}. */
    private String[] originWithSibling() throws Exception {
        String origin = "docs__row14del" + n.incrementAndGet() + "__minilm-l6-v2-384__v1";
        String sibling = "quarantine-" + origin;
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, T, origin);
            PgContainerHelper.insertCollection(ctx, T, sibling);
            PgContainerHelper.insertChunks(ctx, T, sibling, List.of(Chash.ofText("row14 sibling chunk " + origin).toHex()),
                List.of("row14 sibling chunk"), List.of(new float[384]), List.of(Map.<String, Object>of()));
            return null;
        });
        return new String[] {origin, sibling};
    }

    private long idOf(String label) throws Exception {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(label))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
    }

    private boolean registered(String label) throws Exception {
        return su(ctx -> ctx.fetchExists(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(T).and(CATALOG_COLLECTIONS.NAME.eq(label))));
    }

    private int chunksIn(String collection) throws Exception {
        return su(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(T).and(CHUNKS.COLLECTION.eq(collection))));
    }

    /** The registry row of {@code label} held {@code FOR UPDATE} by a hand session, as a retiring verb's first step holds it. */
    private Connection verbHolding(String label) throws Exception {
        Connection verb = pg.createConnection("");
        verb.setAutoCommit(false);
        DSL.using(verb, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(label)).forUpdate().fetch();
        return verb;
    }

    @Timeout(120)
    @Test
    void deleteOfAnOrigin_whileAVerbHoldsASiblingsRow_waitsOnThatRowBeforeRemovingAnyOfTheSiblingsRows_thenFinishesWithNothingLeftBehind() throws Exception {
        String[] c = originWithSibling();
        String origin = c[0];
        String sibling = c[1];
        assertThat(chunksIn(sibling)).as("setup: the sibling holds a row of the origin").isEqualTo(1);
        Future<Map<String, Integer>> delete;
        try (Connection verb = verbHolding(sibling)) {
            delete = pool.submit(() -> catalog.deleteCollection(T, origin, false));
            assertThat(PgActivityProbe.waitsOnALock(pg, "%key share%catalog_collections%"))
                .as("the delete waits on the sibling's registry row read FOR KEY SHARE (before, its first wait was the registry delete at its end)").isTrue();
            assertThat(delete.isDone()).as("and has not finished").isFalse();
            assertThat(chunksIn(sibling)).as("the sibling's row is still there: the delete had removed nothing when it met the verb").isEqualTo(1);
            verb.rollback();
        }

        Map<String, Integer> counts = delete.get(60, TimeUnit.SECONDS);

        assertThat(counts).containsEntry("quarantine_chunks", 1);
        assertThat(chunksIn(sibling)).as("no row left in the sibling").isZero();
        assertThat(registered(sibling)).as("the emptied sibling is unregistered").isFalse();
        assertThat(registered(origin)).as("and the origin is gone").isFalse();
    }

    @Timeout(120)
    @Test
    void deleteOfAnOrigin_whileARestoreEmptiesTheSibling_waitsThenFindsNothingToRemove_withoutDeadlock() throws Exception {
        String[] c = originWithSibling();
        String origin = c[0];
        String sibling = c[1];
        Future<Map<String, Integer>> delete;
        try (Connection verb = verbHolding(sibling)) {
            // a restore: the verb holds the sibling's row and moves its rows away (here, out of the table), then commits
            DSL.using(verb, SQLDialect.POSTGRES).deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(T).and(CHUNKS.COLLECTION.eq(sibling))).execute();
            delete = pool.submit(() -> catalog.deleteCollection(T, origin, false));
            assertThat(PgActivityProbe.waitsOnALock(pg, "%key share%catalog_collections%")).as("the delete waits on the verb's row").isTrue();
            verb.commit();
        }

        Map<String, Integer> counts = delete.get(60, TimeUnit.SECONDS);

        assertThat(counts).as("the restore took the rows: the delete had none left to remove").containsEntry("quarantine_chunks", 0);
        assertThat(chunksIn(sibling)).isZero();
        assertThat(registered(origin)).isFalse();
    }

    @Timeout(120)
    @Test
    void deleteOfAnOrigin_whileASiblingIsRenamedInPlace_locksTheRowById_andFinishes() throws Exception {
        String[] c = originWithSibling();
        String origin = c[0];
        String sibling = c[1];
        long siblingId = idOf(sibling);
        String renamed = "quarantine-row14-renamed-" + n.incrementAndGet();
        Future<Map<String, Integer>> delete;
        try (Connection verb = verbHolding(sibling)) {
            DSL.using(verb, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, renamed)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(siblingId)).execute();
            delete = pool.submit(() -> catalog.deleteCollection(T, origin, false));
            assertThat(PgActivityProbe.waitsOnALock(pg, "%key share%catalog_collections%")).as("the delete waits on the verb's row").isTrue();
            verb.commit();
        }

        try {
            delete.get(60, TimeUnit.SECONDS);
        } catch (TimeoutException e) {
            throw new AssertionError("the delete did not finish: a deadlock or a lost wake-up", e);
        }

        boolean siblingKept = su(ctx -> ctx.fetchExists(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(T)
            .and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(siblingId))));
        assertThat(siblingKept).as("the renamed sibling's row was not touched").isTrue();
        assertThat(registered(origin)).isFalse();
    }

    // ── (b) a delete is refused while a re-embed job holds the collection ───

    private String registeredLabel(String stem) throws Exception {
        String l = "docs__row14job" + stem + n.incrementAndGet() + "__minilm-l6-v2-384__v1";
        su(ctx -> { PgContainerHelper.insertCollection(ctx, T, l); return null; });
        return l;
    }

    @Test
    void aDeleteOfTheSourceOrTheTargetOfANonTerminalJob_isRefusedNamingTheJob_andRemovesNothing() throws Exception {
        var jobs = new ReembedJobRepository(scope);
        String src = registeredLabel("src");
        String tgt = registeredLabel("tgt");
        su(ctx -> { PgContainerHelper.insertChunks(ctx, T, src, List.of(Chash.ofText("row14 job src " + src).toHex()),
            List.of("row14 job chunk"), List.of(new float[384]), List.of(Map.<String, Object>of())); return null; });
        var job = jobs.start(T, src, tgt, null, false, false);

        assertThatThrownBy(() -> catalog.deleteCollection(T, src, false))
            .as("the source of a planned job").isInstanceOf(CatalogRepository.CollectionHeldByReembedJob.class)
            .hasMessageContaining(job.jobId().toString());
        assertThatThrownBy(() -> catalog.deleteCollectionById(T, tgt, idOf(tgt), false))
            .as("the target of it, by id").isInstanceOf(CatalogRepository.CollectionHeldByReembedJob.class);
        assertThat(registered(src)).as("nothing was removed").isTrue();
        assertThat(registered(tgt)).isTrue();
        assertThat(chunksIn(src)).isEqualTo(1);

        // the same on every other non-terminal state: a running job holds its collections too
        jobs.applyTransition(T, job.jobId(), ReembedJobRepository.PLANNED, null, ReembedJobRepository.RUNNING, ReembedJobRepository.Transition.NONE);
        assertThatThrownBy(() -> catalog.deleteCollection(T, src, false)).isInstanceOf(CatalogRepository.CollectionHeldByReembedJob.class);

        // a terminal job holds nothing
        jobs.applyTransition(T, job.jobId(), ReembedJobRepository.RUNNING, null, ReembedJobRepository.ABORTED, ReembedJobRepository.Transition.NONE);
        assertThat(catalog.deleteCollection(T, src, false)).as("once the job is aborted the delete goes through").containsEntry("chunks", 1);
        assertThat(registered(src)).isFalse();
    }

    @Test
    void theRunnersOwnDeleteOfItsSource_isNotRefusedForTheJobItBelongsTo() throws Exception {
        var jobs = new ReembedJobRepository(scope);
        String src = registeredLabel("rsrc");
        String tgt = registeredLabel("rtgt");
        jobs.start(T, src, tgt, null, false, false);

        // the two-argument delete is the re-embed runner's cutover step: the job is in a lock state and this is its own work
        catalog.deleteCollection(T, src);

        assertThat(registered(src)).isFalse();
    }

    @Timeout(120)
    @Test
    void aJobStartingWhileADeleteRuns_cannotProceed_andFindsTheCollectionGoneOnceTheDeleteCommits() throws Exception {
        var jobs = new ReembedJobRepository(scope);
        String[] c = originWithSibling();
        String origin = c[0];
        String sibling = c[1];
        String other = registeredLabel("other");
        Future<Map<String, Integer>> delete;
        try (Connection verb = verbHolding(sibling)) {
            delete = pool.submit(() -> catalog.deleteCollection(T, origin, false));
            // the delete holds the per-tenant guard SHARED and is parked on the sibling's row
            assertThat(PgActivityProbe.waitsOnALock(pg, "%key share%catalog_collections%")).as("the delete is mid-flight").isTrue();

            assertThatThrownBy(() -> jobs.start(T, origin, other, null, false, false))
                .as("a start takes the guard exclusive and cannot while the delete holds it: refused, retryable, nothing written")
                .isInstanceOf(ReembedJobRepository.LockContention.class);
            assertThat(delete.isDone()).isFalse();
            verb.rollback();
        }
        delete.get(60, TimeUnit.SECONDS);

        assertThatThrownBy(() -> jobs.start(T, origin, other, null, false, false))
            .as("after the delete committed the start's own registry read finds no collection").isInstanceOf(UnregisteredCollectionException.class);
        assertThat(jobs.listAll(T).stream().filter(j -> j.sourceCollection().equals(origin)).count()).as("no job was filed").isZero();
    }

    // ── (c) the lock order of a delete by id (B1 of the row 14 and row 16 merge) ───

    /** A hand session holding the per-tenant re-embed start guard EXCLUSIVE, as a start does before it reads any registry row. */
    private Connection startHoldingTheGuard() throws Exception {
        Connection start = pg.createConnection("");
        start.setAutoCommit(false);
        DSL.using(start, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock", Object.class,
            DSL.function("hashtext", Integer.class, DSL.val(CatalogRepository.reembedGuardKey(T))))).fetch();
        return start;
    }

    /** Polls until {@code n} active backends wait on a lock inside a statement matching {@code queryPattern}; false when they never do. */
    private boolean waiters(int n, String queryPattern) throws Exception {
        var activity = DSL.table(DSL.name("pg_catalog", "pg_stat_activity"));
        var waitType = DSL.field(DSL.name("wait_event_type"), String.class);
        var state = DSL.field(DSL.name("state"), String.class);
        var query = DSL.field(DSL.name("query"), String.class);
        for (int i = 0; i < 150; i++) {
            int waiting = su(ctx -> ctx.fetchCount(DSL.selectOne().from(activity)
                .where(waitType.eq("Lock")).and(state.eq("active")).and(query.likeIgnoreCase(queryPattern))));
            if (waiting >= n) {
                return true;
            }
            Thread.sleep(100);
        }
        return false;
    }

    /**
     * The cycle B1 closes: a start holds the guard exclusive and then wants the origin's row {@code FOR KEY SHARE}; a delete by id that held
     * the row {@code FOR UPDATE} first and only then waited for the guard (shared) would wait on the start while the start waits on it,
     * 40P01. The delete takes the guard BEFORE any registry row, so it is parked on the guard holding nothing, the start's key share is
     * granted at once (bounded here: a delete that held the row would make it time out), and the delete finishes when the start commits.
     */
    @Timeout(120)
    @Test
    void aDeleteById_againstAReEmbedStartHoldingTheGuardExclusive_waitsOnTheGuardHoldingNoRegistryRow_andFinishesWithNoDeadlock() throws Exception {
        String origin = registeredLabel("byidguard");
        long originId = idOf(origin);
        java.util.List<String> retries = new java.util.concurrent.CopyOnWriteArrayList<>();
        DeadlockRetry.setRetryListenerForTests(retries::add);
        try {
            Future<Map<String, Integer>> delete;
            try (Connection start = startHoldingTheGuard()) {
                delete = pool.submit(() -> catalog.deleteCollectionById(T, origin, originId, false));
                assertThat(PgActivityProbe.waitsOnALock(pg, "%pg_advisory_xact_lock_shared%"))
                    .as("the delete is parked on the start guard, which it takes first").isTrue();
                DSLContext hand = DSL.using(start, SQLDialect.POSTGRES);
                hand.select(DSL.function("set_config", String.class, DSL.val("lock_timeout"), DSL.val("1s"), DSL.val(true))).fetch();
                assertThatCode(() -> hand.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(originId)).forKeyShare().fetch())
                    .as("the start's read of the origin's row is not blocked: the parked delete holds no registry row").doesNotThrowAnyException();
                assertThat(delete.isDone()).as("the delete still waits for the guard").isFalse();
                start.commit();
            }

            Map<String, Integer> counts = delete.get(60, TimeUnit.SECONDS);

            assertThat(counts).as("the delete went through once the guard was free").isNotNull();
            assertThat(registered(origin)).isFalse();
            assertThat(retries).as("no 40P01: the deadlock retry never ran for this delete").noneMatch(c -> c.contains(origin));
        } finally {
            DeadlockRetry.setRetryListenerForTests(null);
        }
    }

    /**
     * Two deletes of one id at once: each takes the guard shared (they do not conflict), then the origin's row {@code FOR UPDATE}, so they
     * serialise on it. The one that runs second finds the row gone and answers 404; neither is a deadlock victim. The origin's row is never
     * key-shared first and upgraded afterwards: two such deletes would each hold a key share and wait on the other's upgrade. Both are held on
     * the guard by a hand session and released together, so they meet at the row; the deadlock retry belt would turn a 40P01 into a clean answer,
     * so the retry listener is what shows none happened.
     */
    @Timeout(300)
    @Test
    void twoConcurrentDeletesByIdOfOneId_serialiseOnTheOriginsRow_oneWinsTheOtherAnswers404_andNeitherDeadlocks() throws Exception {
        java.util.List<String> retries = new java.util.concurrent.CopyOnWriteArrayList<>();
        DeadlockRetry.setRetryListenerForTests(retries::add);
        try {
            for (int round = 0; round < 8; round++) {
                String[] c = originWithSibling();   // a sibling too: the siblings' key shares are in the same ordered lock
                String origin = c[0];
                long originId = idOf(origin);
                Future<Map<String, Integer>> first;
                Future<Map<String, Integer>> second;
                try (Connection start = startHoldingTheGuard()) {
                    first = pool.submit(() -> catalog.deleteCollectionById(T, origin, originId, false));
                    second = pool.submit(() -> catalog.deleteCollectionById(T, origin, originId, false));
                    assertThat(waiters(2, "%pg_advisory_xact_lock_shared%")).as("both deletes are parked on the guard").isTrue();
                    start.commit();   // both are released at once and meet at the origin's row
                }
                int removed = 0;
                int refused = 0;
                for (Future<Map<String, Integer>> f : List.of(first, second)) {
                    try {
                        f.get(60, TimeUnit.SECONDS);
                        removed++;
                    } catch (ExecutionException e) {
                        assertThat(e.getCause()).as("the second delete finds the row gone: 404, never a deadlock").isInstanceOf(UnknownCollectionIdException.class);
                        refused++;
                    }
                }
                assertThat(removed).as("exactly one delete removed the collection (round %d)", round).isEqualTo(1);
                assertThat(refused).isEqualTo(1);
                assertThat(registered(origin)).isFalse();
            }
            assertThat(retries).as("no delete was a deadlock victim: the deadlock retry belt never ran for a delete")
                .noneMatch(c -> c.startsWith("catalog.deleteCollection "));
        } finally {
            DeadlockRetry.setRetryListenerForTests(null);
        }
    }
}
