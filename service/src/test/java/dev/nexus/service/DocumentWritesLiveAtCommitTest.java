// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.DeadlockRetry;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;

import java.sql.Connection;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.LockSupport;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), review of 449d5d8b03: a document write that names its collection by {@code physical_collection_id}
 * is live at commit. {@code DocumentCollectionRef.resolve} used to answer from the registry cache or an unlocked read, so a cache that
 * called X live and a supersede that committed before the write did let the document INSERT land in the retired row (the foreign key
 * passes on a tombstone). The write's own transaction now reads the row of the id {@code FOR KEY SHARE}
 * ({@code CollectionRegistry#requireIdForWrite}).
 *
 * <p>Both orders, against the real {@code POST /v1/catalog/collections/supersede} parked by a row the test holds, each asserting no
 * {@code DeadlockRetry} retry: (i) the verb first: the writer waits for the key share, then reads the retired row and is refused 409
 * {@code collection_superseded}, nothing stored; (ii) the writer first (parked after its key share on a document row another session
 * holds uncommitted): the verb waits for it, and the document lands in the row that is then retired. The batch register
 * ({@code doc/register_many}) is shown in order (i). The census sees these routes: a 2xx that no transaction read the id for would be
 * reported to the sink, and the test asserts the sink stayed empty.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class DocumentWritesLiveAtCommitTest {

    private static final String SUPERSEDE = "/v1/catalog/collections/supersede";

    CollectionRouteFixture fx;
    ExecutorService pool;
    int n = 5000;
    private final ConcurrentLinkedQueue<String> retries = new ConcurrentLinkedQueue<>();
    private final ConcurrentLinkedQueue<String> unconsumed = new ConcurrentLinkedQueue<>();

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        pool = Executors.newFixedThreadPool(3);
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (fx != null) fx.close();
    }

    @BeforeEach
    void listen() {
        retries.clear();
        unconsumed.clear();
        DeadlockRetry.setRetryListenerForTests(retries::add);
        fx.service.collectionSelectorFilter().testHooks(null, null, null, unconsumed::add);
    }

    @AfterEach
    void noDeadlockAndNoUnreadId() {
        DeadlockRetry.setRetryListenerForTests(null);
        fx.service.collectionSelectorFilter().testHooks(null, null, null, null);
        assertThat(retries).as("the lock order prevents a deadlock: DeadlockRetry absorbed none").isEmpty();
        assertThat(unconsumed).as("every document write by id read the id in its own transaction (the per-request census)").isEmpty();
    }

    private String registered(String stem) {
        String l = label("w1-doc-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    private int waitingRows() {
        return fx.asSuperuser(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("granted"), Boolean.class).isFalse()
                .and(DSL.field(DSL.name("locktype"), String.class).in("tuple", "transactionid"))));
    }

    private void awaitWaiting(int count, Future<?> racer, String what) {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (waitingRows() < count) {
            if (racer.isDone() || System.nanoTime() > deadline) {
                throw new AssertionError(what + ": " + count + " waiting lock request(s) never showed in pg_locks (racer done: "
                    + racer.isDone() + ")");
            }
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        }
    }

    private Future<Resp> parkedSupersede(Connection park, String x, String y) throws Exception {
        park.setAutoCommit(false);
        DSL.using(park, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(y)).forUpdate().fetch();
        Future<Resp> supersede = pool.submit(() -> fx.post(SUPERSEDE, "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}"));
        awaitWaiting(1, supersede, "the supersede parks on the successor's row, holding X");
        return supersede;
    }

    private int documentsTitled(String title) {
        return fx.asSuperuser(ctx -> ctx.fetchCount(CATALOG_DOCUMENTS,
            CATALOG_DOCUMENTS.TENANT_ID.eq(fx.tenant).and(CATALOG_DOCUMENTS.TITLE.eq(title))));
    }

    private String registerBody(String tumbler, String title, long collectionId) {
        return "{\"tumbler\":\"" + tumbler + "\",\"title\":" + json(title)
            + ",\"content_type\":\"knowledge\",\"corpus\":\"knowledge\",\"physical_collection_id\":" + collectionId + "}";
    }

    // ---- (i) the verb first --------------------------------------------------------------------------------------------------

    @Timeout(180)
    @Test
    void aDocumentRegisterByIdArrivingWhileASupersedeHoldsTheRow_waits_thenIsRefused409_andStoresNothing() throws Exception {
        String x = registered("reg-x");
        String y = registered("reg-y");
        long xid = fx.idOf(x);
        String title = "w1-doc-register-" + n;
        Future<Resp> supersede;
        Future<Resp> writer;
        try (Connection park = fx.pg.createConnection("")) {
            supersede = parkedSupersede(park, x, y);
            writer = pool.submit(() -> fx.post("/v1/catalog/register", registerBody("p35a." + n + ".1", title, xid)));
            awaitWaiting(2, writer, "the document writer waits for the registry row of " + x);
            assertThat(writer.isDone()).as("the writer waits for the verb").isFalse();
            park.rollback();
        }
        assertThat(supersede.get(60, TimeUnit.SECONDS).status()).isEqualTo(200);
        Resp w = writer.get(60, TimeUnit.SECONDS);
        assertThat(w.status()).as(w.body()).isEqualTo(409);
        assertThat(w.reason()).isEqualTo("collection_superseded");
        assertThat(w.json().path("superseded_by").asText()).isEqualTo(y);
        assertThat(documentsTitled(title)).as("no document was stored in the retired row").isZero();
    }

    @Timeout(180)
    @Test
    void aBatchRegisterByIdArrivingWhileASupersedeHoldsTheRow_waits_thenIsRefused409_andStoresNothing() throws Exception {
        String x = registered("many-x");
        String y = registered("many-y");
        long xid = fx.idOf(x);
        String title = "w1-doc-many-" + n;
        Future<Resp> supersede;
        Future<Resp> writer;
        try (Connection park = fx.pg.createConnection("")) {
            supersede = parkedSupersede(park, x, y);
            writer = pool.submit(() -> fx.post("/v1/catalog/doc/register_many", "{\"owner_prefix\":\"p35a." + n + "\",\"docs\":[{\"title\":"
                + json(title) + ",\"content_type\":\"knowledge\",\"corpus\":\"knowledge\",\"file_path\":" + json(title + ".md")
                + ",\"physical_collection_id\":" + xid + "}]}"));
            awaitWaiting(2, writer, "the batch writer waits for the registry row of " + x);
            assertThat(writer.isDone()).as("the writer waits for the verb").isFalse();
            park.rollback();
        }
        assertThat(supersede.get(60, TimeUnit.SECONDS).status()).isEqualTo(200);
        Resp w = writer.get(60, TimeUnit.SECONDS);
        assertThat(w.status()).as(w.body()).isEqualTo(409);
        assertThat(w.reason()).isEqualTo("collection_superseded");
        assertThat(documentsTitled(title)).as("no document was stored in the retired row").isZero();
    }

    // ---- (ii) the writer first -----------------------------------------------------------------------------------------------

    /**
     * The writer is parked AFTER its registry key share, on a document row another session holds uncommitted (the same tumbler: the
     * insert waits for it). The verb then waits for the writer's key share, shown in pg_locks; when the other session lets go the writer
     * stores the document, and the verb completes after it: the document is inside the row that is then retired.
     */
    @Timeout(180)
    @Test
    void aSupersedeWaitsForADocumentRegisterByIdInFlight_andTheDocumentLandsInTheThenRetiredRow() throws Exception {
        String x = registered("first-x");
        String y = registered("first-y");
        String host = registered("first-host");
        long xid = fx.idOf(x);
        String tumbler = "p35a." + n + ".7";
        String title = "w1-doc-first-" + n;
        Future<Resp> writer;
        Future<Resp> supersede;
        try (Connection blocker = fx.pg.createConnection("")) {
            blocker.setAutoCommit(false);
            // an uncommitted row of the same tumbler, so the writer's insert waits for it after it holds the registry key share
            CollectionIdSeeds.ownerDocument(DSL.using(blocker, SQLDialect.POSTGRES), fx.tenant, tumbler, host);
            writer = pool.submit(() -> fx.post("/v1/catalog/register", registerBody(tumbler, title, xid)));
            awaitWaiting(1, writer, "the document writer parks on the blocker's document row, holding the key share of " + x);
            supersede = pool.submit(() -> fx.post(SUPERSEDE, "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}"));
            awaitWaiting(2, supersede, "the supersede waits for the writer's key share on " + x);
            assertThat(supersede.isDone()).as("the verb waits for the writer in flight").isFalse();
            blocker.rollback();
        }
        Resp w = writer.get(60, TimeUnit.SECONDS);
        assertThat(w.status()).as("the writer finished (the verb waited for it): %s", w.body()).isEqualTo(200);
        assertThat(supersede.get(60, TimeUnit.SECONDS).status()).as("and the verb completed after it").isEqualTo(200);
        String stored = fx.asSuperuser(ctx -> ctx.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(fx.tenant)).and(CATALOG_DOCUMENTS.TITLE.eq(title))
            .fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION));
        assertThat(stored).as("the document landed in the row that is now retired").isEqualTo(x);
        assertThat(fx.supersededBy(x)).isEqualTo(y);
    }
}
