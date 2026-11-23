// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.LockSupport;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), review of de481230b6: the document move reads the registry row of its document's collection BEFORE it
 * locks the document rows ({@code CatalogMoves#lockCollectionsOfDocuments}); {@code rekeyHighlights} reads that row FOR KEY SHARE, and a
 * child row locked first and the registry row after it can close a cycle with a retiring verb (the verb holds the registry row FOR UPDATE
 * and wants child rows). The verb here is a hand session: it takes a {@code FOR UPDATE} lock, which is all a verb's first step is. Both orders:
 * <ul>
 * <li>verb first: the verb holds the registry row; the move waits for it, and while it waits the document row is NOT locked (a
 *     {@code FOR UPDATE NOWAIT} probe on it succeeds); released, the move completes;</li>
 * <li>writer first: another session holds the document row; the move holds the registry row {@code FOR KEY SHARE} while it waits for it (a
 *     verb's {@code FOR UPDATE NOWAIT} on the registry row is refused 55P03); released, the move completes.</li>
 * </ul>
 * Neither order ends in a 40P01, and the move is not retried (it runs under no {@code DeadlockRetry}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogMovesLockOrderTest {

    private static final String SVC_ROLE = "svc_moves_lock_order";
    private static final String SVC_PASS = "svc_moves_lock_order_pass";
    private static final String COLL = "knowledge__movelock__bge-base-en-v15-768__v1";
    private static final String OLD_URI = "file:///old/lock.pdf";
    private static final String NEW_URI = "file:///new/lock.pdf";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;
    CatalogMoves moves;
    ExecutorService pool;
    int tenantSeq;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
        moves = new CatalogMoves(repo);
        pool = Executors.newFixedThreadPool(2);
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /** A tenant with a curator owner, a registered collection and one document in it carrying {@link #OLD_URI}; the document's tumbler. */
    private String[] seed() throws Exception {
        String t = "moves-lock-" + (++tenantSeq);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), t, COLL);
        }
        Map<String, Object> o = new LinkedHashMap<>();
        o.put("tumbler_prefix", "1.1");
        o.put("name", "pdfs");
        o.put("owner_type", "curator");
        repo.upsertOwner(t, o);
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("title", "lock order");
        f.put("file_path", "/old/lock.pdf");
        f.put("content_type", "paper");
        f.put("corpus", "papers");
        f.put("physical_collection", COLL);
        f.put("source_uri", OLD_URI);
        return new String[] {t, repo.registerDocument(t, "1.1", f)};
    }

    private int waitingRows() {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
                DSL.field(DSL.name("granted"), Boolean.class).isFalse()
                    .and(DSL.field(DSL.name("locktype"), String.class).in("tuple", "transactionid")));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private void awaitWaiting(Future<?> racer, String what) {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (waitingRows() < 1) {
            if (racer.isDone() || System.nanoTime() > deadline) {
                throw new AssertionError(what + ": no lock request ever showed waiting in pg_locks (done: " + racer.isDone() + ")");
            }
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        }
    }

    private void lockRegistryRow(Connection c, String tenant, boolean noWait) {
        var q = DSL.using(c, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(COLL)).forUpdate();
        if (noWait) {
            q.noWait().fetch();
        } else {
            q.fetch();
        }
    }

    private void lockDocumentRow(Connection c, String tenant, String tumbler, boolean noWait) {
        var q = DSL.using(c, SQLDialect.POSTGRES).select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)).forUpdate();
        if (noWait) {
            q.noWait().fetch();
        } else {
            q.fetch();
        }
    }

    private String sourceUriOf(String tenant, String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
                .fetchOne(CATALOG_DOCUMENTS.SOURCE_URI);
        }
    }

    @Timeout(180)
    @Test
    void aMoveArrivingWhileAVerbHoldsTheRegistryRow_waitsForItBeforeItLocksTheDocumentRow() throws Exception {
        String[] seed = seed();
        String t = seed[0];
        String s = seed[1];
        Future<?> move;
        try (Connection verb = pg.createConnection("")) {
            verb.setAutoCommit(false);
            lockRegistryRow(verb, t, false);   // the verb's first lock
            move = pool.submit(() -> moves.moveDocument(t, s, NEW_URI, "/new/lock.pdf", false));
            awaitWaiting(move, "the move waits for the registry row the verb holds");
            try (Connection probe = pg.createConnection("")) {
                probe.setAutoCommit(false);
                try {
                    lockDocumentRow(probe, t, s, true);   // goes through at once: the waiting move holds no document row
                } catch (DataAccessException e) {
                    throw new AssertionError("the move holds the document row while it waits for the registry row: " + e.sqlState(), e);
                } finally {
                    probe.rollback();
                }
            }
            verb.rollback();
        }
        move.get(60, TimeUnit.SECONDS);
        assertThat(sourceUriOf(t, s)).as("the move completed once the verb let go").isEqualTo(NEW_URI);
    }

    @Timeout(180)
    @Test
    void aMoveInFlightHoldsTheRegistryRowWhileItWaitsForADocumentRow_soAVerbCannotStartBetween() throws Exception {
        String[] seed = seed();
        String t = seed[0];
        String s = seed[1];
        Future<?> move;
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            lockDocumentRow(holder, t, s, false);   // another writer holds the document row
            move = pool.submit(() -> moves.moveDocument(t, s, NEW_URI, "/new/lock.pdf", false));
            awaitWaiting(move, "the move waits for the document row the other session holds");
            try (Connection verb = pg.createConnection("")) {
                verb.setAutoCommit(false);
                try {
                    lockRegistryRow(verb, t, true);
                    throw new AssertionError("a verb's FOR UPDATE got the registry row while the move was in flight");
                } catch (DataAccessException e) {
                    assertThat(e.sqlState()).as("the verb is refused: the move holds the row FOR KEY SHARE").isEqualTo("55P03");
                } finally {
                    verb.rollback();
                }
            }
            holder.rollback();
        }
        move.get(60, TimeUnit.SECONDS);
        assertThat(sourceUriOf(t, s)).as("the move completed once the document row was free").isEqualTo(NEW_URI);
    }
}
