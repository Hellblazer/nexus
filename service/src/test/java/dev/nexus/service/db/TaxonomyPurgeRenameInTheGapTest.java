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
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a row 14 (nexus-p2m1x.23): the second topic-set read of {@code purgeAssignmentsForDoc} and {@code purgeCollection}
 * ({@link TaxonomyRegistryReads#registryFirstAround}). The first read judges the collection's topics from an unlocked guess, for lock order
 * only; the write acts on the row as locked. A one-row rename of the collection lands between them (the seam runs after the first read, before
 * the collection's own row is read): the label now names ANOTHER row, whose topics' source collections the guess never saw. The second
 * read must lock those: a hand session holds the registry row of that new topic's source {@code FOR UPDATE}, and the purge must wait on it
 * (a purge that kept the first read's set would not wait), then act on the topics of the row that holds the label now, never the renamed
 * collection's.
 *
 * <p>The class has its own container, so it drops the name-keyed foreign keys the one-row rename needs, as {@code HeavyRoutesRenameGapTest}
 * does on its throwaway database.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyPurgeRenameInTheGapTest {

    private static final String SVC_ROLE = "svc_rdr229_purgegap";
    private static final String SVC_PASS = "svc_rdr229_purgegap_pass";
    private static final String T = "row14-purge-gap";
    private static final String TS = "2026-01-01T00:00:00Z";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TaxonomyRepository repo;
    ExecutorService pool;
    private final AtomicInteger n = new AtomicInteger();
    private volatile Connection verb;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
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
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        repo = new TaxonomyRepository(new TenantScope(svcDs));
        pool = Executors.newCachedThreadPool();
    }

    @AfterEach
    void reset() throws Exception {
        TaxonomyRegistryReads.setAfterFirstReadHookForTests(null);
        Connection held = verb;
        if (held != null) {
            verb = null;
            held.rollback();
            held.close();
        }
    }

    @AfterAll
    void stop() {
        if (pool != null) pool.shutdownNow();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
        CollectionRegistry.shared().evictTenant(T);
    }

    private <R> R su(Function<DSLContext, R> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private static String label(String stem) {
        return "docs__" + stem + "__minilm-l6-v2-384__v1";
    }

    private long idOf(String label) throws Exception {
        Long id = su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(label))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
        return id;
    }

    private void chunk(String collection, String hex) throws Exception {
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, T, collection, List.of(hex), List.of("gap chunk"), List.of(new float[384]),
                List.of(Map.<String, Object>of()));
            return null;
        });
    }

    /** What a scenario holds: the collection {@code b} (id {@code x}), its topic {@code tx}, the document, and the source collection of the new holder's topic. */
    private record Scenario(String b, long x, long tx, String doc, String z2, AtomicLong ty) { }

    /**
     * A collection {@code b} with a topic holding the document projected from {@code z1}, and the one-shot seam armed: when it fires, {@code b}'s
     * row is renamed in place, a NEW row takes {@code b}, that row gets a topic holding the same document projected from {@code z2}, and a hand
     * session takes {@code z2}'s registry row {@code FOR UPDATE}.
     */
    private Scenario armed() throws Exception {
        String stem = "gap" + n.incrementAndGet();
        String b = label(stem + "-b");
        String z1 = label(stem + "-z1");
        String z2 = label(stem + "-z2");
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, T, b);
            PgContainerHelper.insertCollection(ctx, T, z1);
            PgContainerHelper.insertCollection(ctx, T, z2);
            return null;
        });
        CollectionRegistry.shared().evictTenant(T);
        String doc = Chash.ofText("gap-doc-" + stem).toHex();
        chunk(z1, doc);
        chunk(z2, doc);
        long x = idOf(b);
        long tx = repo.insertTopic(T, "tx-topic", null, b, 0, TS, null);
        repo.assignTopic(T, doc, tx, "projection", 0.5, z1, TS);
        String renamed = label(stem + "-renamed");
        AtomicLong ty = new AtomicLong();
        TaxonomyRegistryReads.setAfterFirstReadHookForTests(() -> {
            TaxonomyRegistryReads.setAfterFirstReadHookForTests(null);
            try {
                su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, renamed)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(x)).execute());
                su(ctx -> {
                    PgContainerHelper.insertCollection(ctx, T, b);
                    return null;
                });
                CollectionRegistry.shared().evictTenant(T);
                ty.set(repo.insertTopic(T, "ty-topic", null, b, 0, TS, null));
                repo.assignTopic(T, doc, ty.get(), "projection", 0.5, z2, TS);
                Connection hand = pg.createConnection("");
                hand.setAutoCommit(false);
                DSL.using(hand, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(z2)).forUpdate().fetch();
                verb = hand;
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        return new Scenario(b, x, tx, doc, z2, ty);
    }

    private void awaitWaitOnTheVerb(Future<?> purge) throws Exception {
        assertThat(PgActivityProbe.waitsOnALock(pg, "%key share%catalog_collections%"))
            .as("the purge waits on the registry row of the new topic's source collection (a purge that kept the first read's set would not)").isTrue();
        assertThat(purge.isDone()).as("and has not finished").isFalse();
        Connection held = verb;
        verb = null;
        held.rollback();
        held.close();
    }

    private int assignments(String doc, long topic) throws Exception {
        return su(ctx -> ctx.fetchCount(TOPIC_ASSIGNMENTS,
            TOPIC_ASSIGNMENTS.DOC_ID.eq(Chash.fromHex(doc).toBytes()).and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topic))));
    }

    private int topics(long id) throws Exception {
        return su(ctx -> ctx.fetchCount(TOPICS, TOPICS.ID.eq(id)));
    }

    @Timeout(180)
    @Test
    void purgeAssignmentsForDoc_whenARenameLandsAfterTheFirstRead_locksTheNewHoldersSources_andPurgesOnlyTheNewHoldersTopic() throws Exception {
        Scenario s = armed();

        Future<Integer> purge = pool.submit(() -> repo.purgeAssignmentsForDoc(T, s.b(), s.doc()));
        awaitWaitOnTheVerb(purge);
        int removed = purge.get(60, TimeUnit.SECONDS);

        assertThat(removed).as("one row: the topic of the row that holds the label now").isEqualTo(1);
        assertThat(assignments(s.doc(), s.ty().get())).as("the new holder's assignment is gone").isZero();
        assertThat(assignments(s.doc(), s.tx())).as("the renamed collection's assignment of the same document is not touched").isEqualTo(1);
    }

    @Timeout(180)
    @Test
    void purgeCollection_whenARenameLandsAfterTheFirstRead_locksTheNewHoldersSources_andPurgesOnlyTheNewHoldersTopics() throws Exception {
        Scenario s = armed();

        Future<Map<String, Integer>> purge = pool.submit(() -> repo.purgeCollection(T, s.b(), TaxonomyRepository.PURGE_SCOPE_FULL));
        awaitWaitOnTheVerb(purge);
        Map<String, Integer> counts = purge.get(60, TimeUnit.SECONDS);

        assertThat(counts).as("the row that holds the label now: its one topic and that topic's one assignment")
            .containsEntry("topics", 1).containsEntry("assignments", 1);
        assertThat(topics(s.ty().get())).as("the new holder's topic is gone").isZero();
        assertThat(topics(s.tx())).as("the renamed collection's topic is not purged").isEqualTo(1);
        assertThat(assignments(s.doc(), s.tx())).as("nor its assignment").isEqualTo(1);
    }
}
