// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.TwinKeyUpsert;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): the two-writer race on one switched upsert, {@code AspectRepository#upsertAspect} on
 * {@code document_aspects}, with the writers on opposite twins.
 *
 * <p>One writer has gate 3 latched and arbitrates on the id key {@code (tenant_id, collection_id, source_path)}; the other still
 * arbitrates on the name key {@code (tenant_id, collection, source_path)} (an instance that has not seen the latch, or the same
 * install the moment before it). They write the same new row at once. Each one's speculative insert passes its own arbiter's check
 * (the other key is not its arbiter, and the row is not committed anywhere), so the second to reach the shared unique index trips the
 * writer's NON-arbiter key as a plain 23505. {@link TwinKeyUpsert} names that constraint as the other member of the pair, rolls the
 * statement's savepoint back and runs it once more: the winner has committed by then, so the arbiter takes the conflict and the
 * retry updates the winner's row. Neither writer may see the 23505, the row is stored once, and the helper logs
 * {@code event=twin_key_upsert_retry table=document_aspects} for the retry.
 *
 * <p>Non-vacuity: the loop runs until a retry was logged (at most {@link #CAP} rounds) and fails when none was, so a pass is never a
 * pass over a race that did not happen. The id unique index is the one P3.4's IdUniqueIndexPass builds at run time after gate 2; this
 * schema has not run that pass, so the test creates it (and drops it) the way the chunks upsert test does.
 */
class Rdr229P35cAspectUpsertRaceTest extends AtomicWriteTestBase {

    private static final int CAP = 300;
    private static final String ID_KEY = "p35c_aspects_id_key";

    @Test
    @Timeout(value = 280, unit = TimeUnit.SECONDS, threadMode = Timeout.ThreadMode.SEPARATE_THREAD)
    void aWriterOnTheIdTwinRacingAWriterOnTheNameTwin_retriesOnceAndBothSucceed() throws Exception {
        String collection = "code__p35crace" + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
        registerCollection(collection);
        String doc = "p35c.race." + seq.incrementAndGet();
        registerDoc(doc, collection);
        var onId = new AspectRepository(tenantScope, () -> true);
        var onName = new AspectRepository(tenantScope, Gate3.never());

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.createUniqueIndexIfNotExists(ID_KEY).on(DOCUMENT_ASPECTS,
                DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION_ID, DOCUMENT_ASPECTS.SOURCE_PATH).execute();
            ExecutorService pool = Executors.newFixedThreadPool(2);
            try (RetryLog log = new RetryLog()) {
                int rounds = 0;
                while (rounds < CAP && log.retries().isEmpty()) {
                    String path = "race-" + rounds + ".pdf";
                    var barrier = new CyclicBarrier(2);
                    List<Future<Throwable>> writers = new ArrayList<>();
                    for (AspectRepository repo : List.of(onId, onName)) {
                        writers.add(pool.submit(writer(repo, collection, path, doc, barrier)));
                    }
                    for (Future<Throwable> w : writers) {
                        assertThat(w.get(60, TimeUnit.SECONDS)).as("round %d: neither writer sees a 23505", rounds).isNull();
                    }
                    assertThat(ctx.fetchCount(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT)
                        .and(DOCUMENT_ASPECTS.COLLECTION.eq(collection)).and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(path))))
                        .as("round %d: the raced row is stored once", rounds).isEqualTo(1);
                    rounds++;
                }
                assertThat(dev.nexus.service.db.DeadlockRetry.lastRetriedContext())
                    .as("no deadlock: neither writer's wrapped transaction was retried by DeadlockRetry (a retry would end in two clean answers too)")
                    .isNotEqualTo("aspect.upsert " + collection);
                assertThat(log.retries()).as("non-vacuity: the race tripped a non-arbiter key within %d rounds, and was retried", CAP)
                    .isNotEmpty();
                assertThat(log.retries().get(0)).contains("table=document_aspects");
            } finally {
                pool.shutdownNow();
                ctx.dropIndexIfExists(DSL.name("nexus", ID_KEY)).execute();
            }
        }
    }

    private Callable<Throwable> writer(AspectRepository repo, String collection, String path, String doc, CyclicBarrier barrier) {
        var first = new AtomicBoolean(true);
        return () -> {
            try {
                if (first.getAndSet(false)) {
                    barrier.await(20, TimeUnit.SECONDS);
                }
                repo.upsertAspect(TENANT, Rdr229P35cReadersByIdTest.aspect(collection, path, doc));
                return null;
            } catch (Throwable t) {
                return t;
            }
        };
    }

    /** Attaches a list appender to {@link TwinKeyUpsert}'s logger at INFO for the length of a try block. */
    private static final class RetryLog implements AutoCloseable {
        private final Logger logger = (Logger) LoggerFactory.getLogger(TwinKeyUpsert.class);
        private final ListAppender<ILoggingEvent> appender = new ListAppender<>();
        private final Level prior = logger.getLevel();

        RetryLog() {
            logger.setLevel(Level.INFO);
            appender.start();
            logger.addAppender(appender);
        }

        List<String> retries() {
            synchronized (appender) {
                return appender.list.stream().map(ILoggingEvent::getFormattedMessage)
                    .filter(m -> m.contains("event=twin_key_upsert_retry")).toList();
            }
        }

        @Override
        public void close() {
            logger.detachAppender(appender);
            appender.stop();
            logger.setLevel(prior);
        }
    }
}
