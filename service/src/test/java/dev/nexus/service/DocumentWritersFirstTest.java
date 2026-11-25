// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteCensus.Route;
import dev.nexus.service.CollectionRouteFixture.Resp;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DynamicNode;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.TestFactory;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Stream;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the WRITER-FIRST half of "a document write by {@code physical_collection_id} is live at commit", for every
 * document route of the census ({@code CollectionRouteCensus#DOCUMENT_ROUTES}); {@code DocumentWritesLiveAtCommitTest} covers the verb-first
 * order and the writer-first order for the register route. Each route's request names a live collection by id; the write is parked right after
 * its registry read (the seam {@code afterTransactionRead}, which the by-id read of a document write now fires), holding the row
 * {@code FOR KEY SHARE}. The probe is a {@code FOR UPDATE NOWAIT} on the registry row from another session, which is what a retiring verb
 * takes: it must be refused (55P03), because the writer in flight holds the share. Released, the write completes 200. The per-transaction
 * census sink must also have counted a write transaction for the route: a positive count, so a route whose write left the census cannot pass
 * by examining nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class DocumentWritersFirstTest {

    CollectionRouteFixture fx;
    ExecutorService pool;
    private final AtomicInteger seq = new AtomicInteger(7000);
    private final ConcurrentLinkedQueue<String> censusLines = new ConcurrentLinkedQueue<>();
    private final ConcurrentLinkedQueue<String> unconsumed = new ConcurrentLinkedQueue<>();

    @BeforeAll
    void start() throws Exception {
        fx = new CollectionRouteFixture();
        pool = Executors.newFixedThreadPool(2);
        fx.service.collectionSelectorFilter().testHooks(null, null, null, unconsumed::add);   // turns the per-transaction census on
        fx.service.collectionSelectorFilter().censusSink(censusLines::add);
    }

    @AfterAll
    void stop() {
        if (pool != null) pool.shutdownNow();
        if (fx != null) fx.close();
    }

    private String expand(String text, int n, String doc) {
        return text.replace(CollectionRouteCensus.TOKEN_SEQ, String.valueOf(n))
            .replace(CollectionRouteCensus.TOKEN_TITLE, "p35a-first-" + n)
            .replace(CollectionRouteCensus.TOKEN_DOC, doc == null ? "" : doc);
    }

    private String seedDocument(Route route, int n) {
        if (!(route.extras() + route.elementExtras()).contains(CollectionRouteCensus.TOKEN_DOC)) {
            return null;
        }
        String home = CollectionRouteFixture.label("first-seed" + n);
        fx.register(home);
        String tumbler = "p35a." + n + ".9";
        Resp r = fx.post("/v1/catalog/register", "{\"tumbler\":\"" + tumbler + "\",\"title\":" + json("p35a-first-seed-" + n)
            + ",\"content_type\":\"knowledge\",\"corpus\":\"knowledge\",\"physical_collection\":" + json(home) + "}");
        assertThat(r.status()).as("setup: the document the update moves: %s", r.body()).isEqualTo(200);
        return tumbler;
    }

    private Resp send(Route route, long id, int n, String doc) {
        String selector = "\"physical_collection_id\":" + id;
        String extras = expand(route.extras(), n, doc);
        List<String> parts = new ArrayList<>();
        if (route.slots().get(0).nested()) {
            if (!extras.isBlank()) {
                parts.add(extras);
            }
            String element = expand(route.elementExtras(), n, doc);
            parts.add("\"" + route.slots().get(0).nestedIn() + "\":[{" + selector + (element.isBlank() ? "" : "," + element) + "}]");
        } else {
            parts.add(selector);
            if (!extras.isBlank()) {
                parts.add(extras);
            }
        }
        return fx.post(route.path(), "{" + String.join(",", parts) + "}");
    }

    @TestFactory
    Stream<DynamicNode> everyDocumentRouteByIdHoldsTheRowKeyShared_whileTheWriteIsInFlight() {
        List<DynamicNode> nodes = new ArrayList<>();
        for (Route route : CollectionRouteCensus.DOCUMENT_ROUTES) {
            nodes.add(DynamicTest.dynamicTest(route.label(), () -> {
                int n = seq.incrementAndGet();
                String x = CollectionRouteFixture.label("first-x" + n);
                fx.register(x);
                long xid = fx.idOf(x);
                String doc = seedDocument(route, n);
                CountDownLatch parked = new CountDownLatch(1);
                CountDownLatch release = new CountDownLatch(1);
                fx.service.collectionSelectorFilter().testHooks(null, null, () -> {
                    parked.countDown();
                    try {
                        release.await(60, TimeUnit.SECONDS);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }
                }, unconsumed::add);
                censusLines.clear();
                Future<Resp> writer = pool.submit(() -> send(route, xid, n, doc));
                try {
                    assertThat(parked.await(60, TimeUnit.SECONDS)).as("%s: the write reached its registry read and parked", route.label()).isTrue();
                    try (Connection verb = fx.pg.createConnection("")) {
                        verb.setAutoCommit(false);
                        try {
                            DSL.using(verb, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(xid))
                                .forUpdate().noWait().fetch();
                            throw new AssertionError(route.label() + ": a verb's FOR UPDATE got the row while the write was in flight: "
                                + "the write does not hold the registry row FOR KEY SHARE");
                        } catch (DataAccessException e) {
                            assertThat(e.sqlState()).as("%s: the verb is refused because the writer holds the row", route.label()).isEqualTo("55P03");
                        } finally {
                            verb.rollback();
                        }
                    }
                } finally {
                    release.countDown();
                    fx.service.collectionSelectorFilter().testHooks(null, null, null, unconsumed::add);
                }
                Resp r = writer.get(60, TimeUnit.SECONDS);
                assertThat(r.status()).as("%s completes once released: %s", route.label(), r.body()).isEqualTo(200);
                // The census line is added by the route boundary's filter AFTER the handler has sent its answer, so the client can have the
                // answer a moment before the line exists (the lightest route, register, loses that race): wait for the line, then count.
                long lineDeadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(15);
                while (censusLines.stream().noneMatch(l -> l.startsWith(route.path() + " ")) && System.nanoTime() < lineDeadline) {
                    Thread.sleep(10);
                }
                assertThat(censusLines.stream().filter(l -> l.startsWith(route.path() + " ")).mapToInt(l -> {
                    String examined = l.replaceAll(".*examined=(\\d+).*", "$1");
                    return Integer.parseInt(examined);
                }).sum()).as("%s: the per-transaction census examined a write transaction (a positive count); lines seen: %s", route.label(),
                    censusLines).isPositive();
            }));
        }
        nodes.add(DynamicTest.dynamicTest("non-vacuity: the document routes", () ->
            assertThat(CollectionRouteCensus.DOCUMENT_ROUTES).hasSize(7)));
        return nodes.stream();
    }
}
