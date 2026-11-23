// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.NexusService;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.LockSupport;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a, linda's writer lock order (bead nexus-p2m1x.23): the claim-fenced aspect upsert used to take the claim's tuples row
 * FIRST ({@code AspectRepository#upsertAspect}, "by documented design"), the reverse of the registry-first order every other writer
 * keeps. With a retiring verb holding the collection's registry row {@code FOR UPDATE} and wanting the tuples rows (a rename rewrites
 * the task bodies), a writer that held the tuples row and then asked for the registry key share is a deadlock. The order is now: the
 * registry row {@code FOR KEY SHARE}, then the tuples row, then the catalog row and {@code document_aspects}.
 *
 * <p>Both orders, shown by what each side holds while the other waits (no sleeps; the test goes on when {@code pg_locks} shows the
 * wait), and neither ends in a deadlock (an exhausted 40P01 would be the writer's exception):
 * <ol>
 *   <li>The verb first: a session holds the registry row {@code FOR UPDATE}. The writer waits for its key share and holds NO tuples
 *       row lock yet ({@code FOR UPDATE NOWAIT} on the claim's row succeeds).</li>
 *   <li>The writer first: a session holds the document's catalog row, so the writer parks after its first two locks. It holds the
 *       registry key share (a verb's {@code FOR UPDATE NOWAIT} on the row fails with 55P03) and the claim's tuples row; when the
 *       session lets go the writer finishes and the verb's lock goes through.</li>
 * </ol>
 */
class AspectUpsertFencedLockOrderTest extends AtomicWriteTestBase {

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final String SUBSPACE = "aspects";
    private static final Map<String, String> PATTERN = Map.of("queue", "aspects");

    private AspectRepository aspects;
    private TupleRepository tuples;

    /** Every retry {@code DeadlockRetry} made while a test ran: the belt turns a 40P01 into a clean answer, so each test asserts none. */
    private final java.util.concurrent.ConcurrentLinkedQueue<String> retries = new java.util.concurrent.ConcurrentLinkedQueue<>();

    @org.junit.jupiter.api.BeforeEach
    void listenForRetries() {
        retries.clear();
        dev.nexus.service.db.DeadlockRetry.setRetryListenerForTests(retries::add);
    }

    @org.junit.jupiter.api.AfterEach
    void noDeadlockWasAbsorbed() {
        dev.nexus.service.db.DeadlockRetry.setRetryListenerForTests(null);
        assertThat(retries).as("the lock order prevents a deadlock: DeadlockRetry absorbed none (a retried 40P01 ends in a clean answer)")
            .isEmpty();
    }

    @BeforeAll
    void wire() {
        aspects = new AspectRepository(tenantScope);
        tuples = new TupleRepository(tenantScope,
                TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L));
    }

    @BeforeEach
    void emptyQueue() throws Exception {
        admin(ctx -> ctx.deleteFrom(TUPLES)
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(SUBSPACE))).execute());
    }

    private <T> T admin(Function<DSLContext, T> f) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return f.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    private TupleRepository.ClaimedTuple seedAndClaim(Fx f) throws Exception {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("source_path", "src/" + f.docId() + ".pdf");
        body.put("doc_id", f.docId());
        body.put("content_hash", "h1");
        tuples.out(TENANT, SUBSPACE, PATTERN, Map.of(), JSON.writeValueAsString(body), f.docId(), null);
        var claimed = tuples.inp(TENANT, SUBSPACE, PATTERN, "wA:0", (Long) 600L);
        assertThat(claimed).as("setup: the worker finds the task it seeded").isPresent();
        return claimed.get();
    }

    private Map<String, Object> aspectBody(Fx f, String claimId) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("collection", f.collection());
        b.put("source_path", "src/" + f.docId() + ".pdf");
        b.put("problem_formulation", "p");
        b.put("proposed_method", "m");
        b.put("experimental_results", "r");
        b.put("confidence", 0.9);
        b.put("extracted_at", "2026-10-29T00:00:00.000000Z");
        b.put("model_version", "v1");
        b.put("extractor_name", "scholarly-paper");
        b.put("doc_id", f.docId());
        b.put("claim_id", claimId);
        b.put("claimant", "wA:0");
        return b;
    }

    private int waiting() throws Exception {
        return admin(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("granted"), Boolean.class).isFalse()
                .and(DSL.field(DSL.name("locktype"), String.class).in("tuple", "transactionid"))));
    }

    private void awaitWaiting(CompletableFuture<?> racer, String what) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (waiting() < 1) {
            if (racer.isDone() || System.nanoTime() > deadline) {
                throw new AssertionError(what + ": no lock request ever showed waiting in pg_locks (racer done: " + racer.isDone() + ")");
            }
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        }
    }

    /** True when the claim's tuples row can be locked at once by another session (NOWAIT), and is not held by the writer. */
    private boolean tuplesRowFree(String claimId) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            try {
                DSL.using(c, SQLDialect.POSTGRES).select(TUPLES.CLAIM_ID).from(TUPLES)
                    .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.CLAIM_ID.eq(claimId))).forUpdate().noWait().fetch();
                return true;
            } catch (DataAccessException e) {
                return false;
            } finally {
                c.rollback();
            }
        }
    }

    /** True when the collection's registry row can be locked FOR UPDATE at once by another session (a verb's lock), NOWAIT. */
    private boolean registryRowFree(String collection) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            try {
                DSL.using(c, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT).and(CATALOG_COLLECTIONS.NAME.eq(collection))).forUpdate().noWait().fetch();
                return true;
            } catch (DataAccessException e) {
                return false;
            } finally {
                c.rollback();
            }
        }
    }

    @Timeout(120)
    @Test
    void theVerbFirst_theFencedWriterWaitsForTheRegistryRowHoldingNoTuplesRowLock() throws Exception {
        Fx f = fixture("fo1");
        var claim = seedAndClaim(f);
        CompletableFuture<Long> writer;
        try (Connection verb = pg.createConnection("")) {
            verb.setAutoCommit(false);
            DSL.using(verb, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT).and(CATALOG_COLLECTIONS.NAME.eq(f.collection()))).forUpdate().fetch();
            writer = CompletableFuture.supplyAsync(() -> aspects.upsertAspect(TENANT, aspectBody(f, claim.claimId())));
            try {
                awaitWaiting(writer, "the fenced writer waits for the registry row's key share");
                assertThat(writer.isDone()).as("the writer waits for the verb").isFalse();
                assertThat(tuplesRowFree(claim.claimId()))
                    .as("the writer holds no tuples row lock while it waits for the registry row (registry first)").isTrue();
            } finally {
                verb.rollback();
            }
        }
        long written = writer.get(60, TimeUnit.SECONDS);
        assertThat(written).as("the writer finished once the verb let go, no deadlock").isPositive();
    }

    @Timeout(120)
    @Test
    void theWriterFirst_itHoldsTheRegistryKeyShareAndTheTuplesRow_andTheVerbsLockGoesThroughAfterIt() throws Exception {
        Fx f = fixture("fo2");
        var claim = seedAndClaim(f);
        CompletableFuture<Long> writer;
        try (Connection park = pg.createConnection("")) {
            park.setAutoCommit(false);
            // the document's catalog row held: the writer's third lock (the catalog row, FOR SHARE) parks it after the first two
            DSL.using(park, SQLDialect.POSTGRES).select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(f.docId()))).forUpdate().fetch();
            writer = CompletableFuture.supplyAsync(() -> aspects.upsertAspect(TENANT, aspectBody(f, claim.claimId())));
            try {
                awaitWaiting(writer, "the fenced writer parks on the document's catalog row");
                assertThat(writer.isDone()).as("the writer is parked").isFalse();
                assertThat(registryRowFree(f.collection()))
                    .as("the writer holds the registry key share: a verb's FOR UPDATE NOWAIT is refused").isFalse();
                assertThat(tuplesRowFree(claim.claimId())).as("and the claim's tuples row").isFalse();
            } finally {
                park.rollback();
            }
        }
        assertThat(writer.get(60, TimeUnit.SECONDS)).as("the writer finished, no deadlock").isPositive();
        assertThat(registryRowFree(f.collection())).as("the verb's lock goes through once the writer committed").isTrue();
    }
}
