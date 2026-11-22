// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import javax.sql.DataSource;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CANARY_QUERY_RING;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-4lnn1.1.3 (RDR-230 P1.2), the writer, reader and age-bound halves, against a real Postgres with row-level
 * security forced and the service role under {@link TenantScope} (so the policy applies). Admission through the
 * handler is {@code PciQueryRingAdmissionIntegrationTest}; the reconciler's call of the age bound is in
 * {@code PciReconcilerIntegrationTest}.
 *
 * <p>Each test makes its own tenant and collections, so the tests share the database and need no clean-up.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciQueryRingIntegrationTest {

    private static final String MODEL_384 = "minilm-l6-v2-384";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    ListAppender<ILoggingEvent> appender;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        svcDs = pool(4);
        scope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void captureLogs() {
        appender = new ListAppender<>();
        appender.start();
        for (Class<?> c : List.of(PciQueryRing.class, PciQueryRingReader.class, PciQueryRingExpiry.class)) {
            var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(c);
            logger.setLevel(Level.DEBUG);
            logger.addAppender(appender);
        }
    }

    @AfterEach
    void releaseLogs() {
        for (Class<?> c : List.of(PciQueryRing.class, PciQueryRingReader.class, PciQueryRingExpiry.class)) {
            ((ch.qos.logback.classic.Logger) LoggerFactory.getLogger(c)).detachAppender(appender);
        }
    }

    // -- helpers ---------------------------------------------------------------------------------------------

    private HikariDataSource pool(int size) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(size);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    private static String newTenant() {
        return "ring-" + UUID.randomUUID().toString().substring(0, 8);
    }

    private static String collectionOf(String tenant, String tag) {
        return "knowledge__" + tenant + "-" + tag + "__minilm-l6-v2-384__v1";
    }

    private void register(String tenant, String... collections) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : collections) {
                PgContainerHelper.insertCollection(dsl, tenant, c);
            }
        }
    }

    private String modelOf(String tenant, String collection) {
        return scope.withTenant(tenant, ctx -> ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(collection))
            .fetchOne(CATALOG_COLLECTIONS.EMBEDDING_MODEL));
    }

    /** A 384-dimensional vector that differs per seed. */
    private static float[] vec(int seed) {
        float[] v = new float[384];
        for (int i = 0; i < v.length; i++) {
            v[i] = (((seed + 1) * 31 + i * 7) % 997) / 1000f + 0.001f;
        }
        return v;
    }

    private PciQueryRing ring(TenantScope s) {
        return new PciQueryRing(s, (model, tenant, collection) -> true);
    }

    private void admit(PciQueryRing ring, String tenant, String collection, int seed) {
        ring.admit(tenant, QueryVector.alone(modelOf(tenant, collection), vec(seed), collection));
    }

    private int rows(String tenant, String collection) {
        return scope.withTenant(tenant, ctx -> ctx.fetchCount(CANARY_QUERY_RING,
            CANARY_QUERY_RING.COLLECTION.eq(collection)));
    }

    private void insertOldRow(String tenant, String collection, int seed, OffsetDateTime addedAt) {
        scope.withTenant(tenant, ctx -> ctx.insertInto(CANARY_QUERY_RING)
            .set(CANARY_QUERY_RING.TENANT_ID, tenant).set(CANARY_QUERY_RING.COLLECTION, collection)
            .set(CANARY_QUERY_RING.EMBEDDING_MODEL, modelOf(tenant, collection))
            .set(CANARY_QUERY_RING.EMBEDDING_384, Vector.of(vec(seed)))
            .set(CANARY_QUERY_RING.VECTOR_DIGEST, PciQueryRing.digest(vec(seed)))
            .set(CANARY_QUERY_RING.ADDED_AT, addedAt)
            .execute());
    }

    private List<String> logLines() {
        return appender.list.stream().map(ILoggingEvent::getFormattedMessage).toList();
    }

    // -- the writer and the reader ----------------------------------------------------------------------------

    @Test
    void anAdmittedVectorIsWritten_andTheReaderReturnsItWithItsModel() throws Exception {
        String t = newTenant();
        String c = collectionOf(t, "a");
        register(t, c);
        PciQueryRing ring = ring(scope);

        admit(ring, t, c, 1);
        assertThat(ring.flushNow()).isEqualTo(1);

        var read = new PciQueryRingReader(scope).load(t, c);
        assertThat(read).hasSize(1);
        assertThat(read.get(0).model()).isEqualTo(modelOf(t, c));
        assertThat(read.get(0).vector()).containsExactly(vec(1));
        assertThat(read.get(0).addedAt()).isNotNull();
    }

    @Test
    void theReaderReturnsAtMostTenRows_newestFirst() throws Exception {
        String t = newTenant();
        String c = collectionOf(t, "a");
        register(t, c);
        PciQueryRing ring = ring(scope);
        for (int i = 0; i < 12; i++) {
            admit(ring, t, c, i);
        }
        ring.flushNow();
        assertThat(rows(t, c)).as("12 distinct vectors, under the ring's 32").isEqualTo(12);

        var read = new PciQueryRingReader(scope).load(t, c);

        assertThat(read).hasSize(PciQueryRingReader.READ_LIMIT);
        assertThat(read.get(0).vector()).as("the last one written is first").containsExactly(vec(11));
        assertThat(read.get(9).vector()).containsExactly(vec(2));
        for (int i = 1; i < read.size(); i++) {
            assertThat(read.get(i).addedAt()).isBeforeOrEqualTo(read.get(i - 1).addedAt());
        }
    }

    /** Test 3. */
    @Test
    void aSecondTenantNeverSeesTheFirstTenantsRows() throws Exception {
        String a = newTenant();
        String b = newTenant();
        String ca = collectionOf(a, "a");
        String cb = collectionOf(b, "a");
        register(a, ca);
        register(b, cb);
        PciQueryRing ring = ring(scope);
        admit(ring, a, ca, 1);
        admit(ring, a, ca, 2);
        admit(ring, b, cb, 3);
        ring.flushNow();

        var reader = new PciQueryRingReader(scope);
        assertThat(reader.load(a, ca)).hasSize(2);
        assertThat(reader.load(b, cb)).hasSize(1);
        assertThat(reader.load(b, ca)).as("tenant b names tenant a's collection: nothing").isEmpty();
        assertThat(reader.load(a, cb)).isEmpty();
        int seenByB = scope.withTenant(b, ctx -> ctx.fetchCount(CANARY_QUERY_RING, CANARY_QUERY_RING.TENANT_ID.eq(a)));
        assertThat(seenByB).as("row-level security hides a's rows from b").isZero();
    }

    /** Test 4. */
    @Test
    void the33rdDistinctVectorEvictsTheOldest() throws Exception {
        String t = newTenant();
        String c = collectionOf(t, "a");
        register(t, c);
        PciQueryRing ring = ring(scope);
        for (int i = 0; i < 33; i++) {
            admit(ring, t, c, i);
        }
        ring.flushNow();

        assertThat(rows(t, c)).isEqualTo(32);
        List<String> kept = scope.withTenant(t, ctx -> ctx.select(CANARY_QUERY_RING.VECTOR_DIGEST)
            .from(CANARY_QUERY_RING).where(CANARY_QUERY_RING.COLLECTION.eq(c))
            .fetch(r -> HexFormat.of().formatHex(r.value1())));
        assertThat(kept).doesNotContain(HexFormat.of().formatHex(PciQueryRing.digest(vec(0))));
        assertThat(kept).contains(HexFormat.of().formatHex(PciQueryRing.digest(vec(32))));
    }

    /** Test 5, the write half: the trim of a write removes a row older than 30 days. */
    @Test
    void aRowOlderThan30DaysGoesOnAWritesTrim() throws Exception {
        String t = newTenant();
        String c = collectionOf(t, "a");
        register(t, c);
        insertOldRow(t, c, 100, OffsetDateTime.now().minusDays(31));
        PciQueryRing ring = ring(scope);

        admit(ring, t, c, 1);
        ring.flushNow();

        assertThat(rows(t, c)).as("the new row only").isEqualTo(1);
        assertThat(new PciQueryRingReader(scope).load(t, c).get(0).vector()).containsExactly(vec(1));
    }

    /** Test 5, the idle half: on a ring with no new write the age bound deletes the old row and keeps the young. */
    @Test
    void anIdleRingsOldRowsGoToTheAgeBound_theYoungStay() throws Exception {
        String t = newTenant();
        String idle = collectionOf(t, "idle");
        register(t, idle);
        insertOldRow(t, idle, 100, OffsetDateTime.now().minusDays(31));
        insertOldRow(t, idle, 101, OffsetDateTime.now().minusDays(29));
        String other = newTenant();
        String otherColl = collectionOf(other, "idle");
        register(other, otherColl);
        insertOldRow(other, otherColl, 102, OffsetDateTime.now().minusDays(45));

        int deleted = new PciQueryRingExpiry(scope, () -> List.of(t)).expireAll();

        assertThat(deleted).isEqualTo(1);
        var left = new PciQueryRingReader(scope).load(t, idle);
        assertThat(left).hasSize(1);
        assertThat(left.get(0).vector()).containsExactly(vec(101));
        assertThat(rows(other, otherColl)).as("a tenant outside the list is not visited").isEqualTo(1);
    }

    @Test
    void theAgeBoundVisitsEveryTenantInTheList_andOneTenantsFailureDoesNotStopTheRest() throws Exception {
        String a = newTenant();
        String b = newTenant();
        String ca = collectionOf(a, "idle");
        String cb = collectionOf(b, "idle");
        register(a, ca);
        register(b, cb);
        insertOldRow(a, ca, 1, OffsetDateTime.now().minusDays(40));
        insertOldRow(b, cb, 2, OffsetDateTime.now().minusDays(40));

        // A blank tenant id makes TenantScope.withTenant throw for that entry alone.
        int deleted = new PciQueryRingExpiry(scope, () -> List.of(a, "", b)).expireAll();

        assertThat(deleted).isEqualTo(2);
        assertThat(rows(a, ca)).isZero();
        assertThat(rows(b, cb)).isZero();
        assertThat(logLines()).anyMatch(l -> l.contains("event=pci_ring_expire_failed"));
    }

    /** Test 6. */
    @Test
    void aRepeatedVectorRefreshesAddedAt_andAddsNoRow() throws Exception {
        String t = newTenant();
        String c = collectionOf(t, "a");
        register(t, c);
        PciQueryRing ring = ring(scope);
        admit(ring, t, c, 1);
        ring.flushNow();
        OffsetDateTime first = new PciQueryRingReader(scope).load(t, c).get(0).addedAt();

        Thread.sleep(20);
        admit(ring, t, c, 1);
        ring.flushNow();

        var read = new PciQueryRingReader(scope).load(t, c);
        assertThat(read).hasSize(1);
        assertThat(read.get(0).addedAt()).isAfter(first);
    }

    /** Test 8: a fresh engine on a fresh pool reads back the same ring. */
    @Test
    void aRestartedEngineReadsBackTheSameRing() throws Exception {
        String t = newTenant();
        String c = collectionOf(t, "a");
        register(t, c);
        PciQueryRing before = ring(scope);
        for (int i = 0; i < 3; i++) {
            admit(before, t, c, i);
        }
        before.flushNow();
        before.stop();
        var firstRead = new PciQueryRingReader(scope).load(t, c);

        try (HikariDataSource second = pool(2)) {
            var afterRead = new PciQueryRingReader(new TenantScope(second)).load(t, c);
            assertThat(afterRead).hasSameSizeAs(firstRead).hasSize(3);
            for (int i = 0; i < 3; i++) {
                assertThat(afterRead.get(i).vector()).containsExactly(firstRead.get(i).vector());
                assertThat(afterRead.get(i).addedAt()).isEqualTo(firstRead.get(i).addedAt());
            }
        }
    }

    /** Test 9, gate S6. */
    @Test
    void aBatchWhoseCollectionWasDeletedAfterEnqueueWritesTheOtherEntries() throws Exception {
        String t = newTenant();
        String gone = collectionOf(t, "gone");
        String kept = collectionOf(t, "kept");
        register(t, gone, kept);
        PciQueryRing ring = ring(scope);
        admit(ring, t, gone, 1);
        admit(ring, t, kept, 2);
        admit(ring, t, kept, 3);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CATALOG_COLLECTIONS)
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(t)).and(CATALOG_COLLECTIONS.NAME.eq(gone)).execute();
        }

        int written = ring.flushNow();

        assertThat(written).as("the two entries of the surviving collection").isEqualTo(2);
        assertThat(rows(t, kept)).isEqualTo(2);
        assertThat(rows(t, gone)).isZero();
        assertThat(logLines()).anyMatch(l -> l.contains("event=pci_ring_flush") && l.contains("skipped=1")
            && l.contains("failed=0"));
    }

    // -- bounds and failure -------------------------------------------------------------------------------------

    @Test
    void aFullQueueDropsTheEntry_andTheNextFlushLineCountsIt() throws Exception {
        String t = newTenant();
        String c = collectionOf(t, "a");
        register(t, c);
        PciQueryRing ring = new PciQueryRing(scope, (m, tn, col) -> true, 2, 64, Duration.ofSeconds(30));

        admit(ring, t, c, 1);
        admit(ring, t, c, 2);
        admit(ring, t, c, 3);      // the third finds the queue full
        admit(ring, t, c, 4);
        assertThat(ring.queued()).isEqualTo(2);
        ring.flushNow();

        assertThat(rows(t, c)).isEqualTo(2);
        assertThat(logLines()).anyMatch(l -> l.contains("event=pci_ring_flush") && l.contains("dropped=2"));
    }

    @Test
    void aCollectionWithoutAValidIndexIsNotAdmitted() throws Exception {
        String t = newTenant();
        String c = collectionOf(t, "a");
        register(t, c);
        PciQueryRing ring = new PciQueryRing(scope, PciIndexSet.NONE);

        admit(ring, t, c, 1);

        assertThat(ring.queued()).isZero();
        assertThat(ring.flushNow()).isZero();
    }

    @Test
    void theBackgroundWriterFlushesWhenFlushAtEntriesAreWaiting_withoutWaitingForThePeriod() throws Exception {
        String t = newTenant();
        String c = collectionOf(t, "a");
        register(t, c);
        PciQueryRing ring = new PciQueryRing(scope, (m, tn, col) -> true, 1024, 8, Duration.ofMinutes(10));
        ring.start();
        try {
            for (int i = 0; i < 8; i++) {
                admit(ring, t, c, i);
            }
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
            while (rows(t, c) < 8 && System.nanoTime() < deadline) {
                Thread.sleep(50);
            }
            assertThat(rows(t, c)).as("flushed by the size trigger, not the 10 minute period").isEqualTo(8);
        } finally {
            ring.stop();
        }
    }

    /**
     * nexus-4lnn1.1.7: the batch call has a statement timeout. A lock on the ring row the batch must refresh stands in
     * for a slow database: without the bound the flush waits for the lock, with it the call is cancelled and the batch
     * dropped, the failure logged by the ring's own sanitised line.
     */
    @Test
    void aBatchBlockedOnALockEndsAtTheStatementTimeout_andTheWriterMovesOn() throws Exception {
        String t = newTenant();
        String c = collectionOf(t, "a");
        register(t, c);
        insertOldRow(t, c, 1, OffsetDateTime.now().minusDays(1));
        PciQueryRing ring = new PciQueryRing(scope, (m, tn, col) -> true, 1024, 64, Duration.ofMinutes(10),
            Duration.ofMillis(700));
        admit(ring, t, c, 1);   // the same vector: the batch updates the row the other session holds
        var flusher = java.util.concurrent.Executors.newSingleThreadExecutor();
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).selectFrom(CANARY_QUERY_RING)
                .where(CANARY_QUERY_RING.TENANT_ID.eq(t)).and(CANARY_QUERY_RING.COLLECTION.eq(c)).forUpdate().fetch();
            var flush = flusher.submit(() -> ring.flushNow());
            try {
                flush.get(15, TimeUnit.SECONDS);
            } catch (java.util.concurrent.TimeoutException blocked) {
                throw new AssertionError("the flush waited on the lock: no statement timeout on the batch call", blocked);
            } finally {
                holder.rollback();
            }
        } finally {
            flusher.shutdownNow();
        }

        assertThat(logLines()).anyMatch(l -> l.contains("event=pci_ring_write_failed") && l.contains(t));
        admit(ring, t, c, 2);
        assertThat(ring.flushNow()).as("the writer is free again").isEqualTo(1);
    }

    /** Test 10 (and the failure line of the design): a refusing database, an error that quotes the vector. */
    @Test
    void noRingLogLineCarriesAVectorOrQueryText_evenWhenTheDriverQuotesOne() throws Exception {
        String t = newTenant();
        String c = collectionOf(t, "a");
        register(t, c);
        float[] secret = vec(7);
        String literal = Vector.of(secret).toString();
        DataSource refusing = refusingDataSource(
            () -> new SQLException("invalid input syntax for type vector: \"" + literal + "\""), null);

        PciQueryRing failing = ring(new TenantScope(refusing));
        failing.admit(t, QueryVector.alone(MODEL_384, secret, c));
        failing.flushNow();                                         // the failed write
        PciQueryRing small = new PciQueryRing(scope, (m, tn, col) -> true, 1, 64, Duration.ofSeconds(30));
        admit(small, t, c, 1);
        admit(small, t, c, 2);                                      // a drop
        small.flushNow();                                           // a good write and a drop count
        new PciQueryRingExpiry(scope, () -> List.of(t)).expireAll();
        new PciQueryRingReader(scope).load(t, c);

        List<String> lines = logLines();
        assertThat(lines).anyMatch(l -> l.startsWith("event=pci_ring_write_failed tenant=" + t + " entries=1 error="));
        Pattern vectorShape = Pattern.compile("\\[?-?\\d+\\.\\d+(,\\s*-?\\d+(\\.\\d+)?){3,}");
        for (String line : lines) {
            assertThat(line).as("no literal").doesNotContain(literal).doesNotContain(Float.toString(secret[3]));
            assertThat(vectorShape.matcher(line).find()).as("no run of floats in: " + line).isFalse();
        }
        for (ILoggingEvent e : appender.list) {
            assertThat(e.getArgumentArray() == null ? "" : java.util.Arrays.deepToString(e.getArgumentArray()))
                .as("no vector among the raw log arguments").doesNotContain(Float.toString(secret[3]));
        }
    }

    /** Test 7 (the writer's half): while the database hangs, admission and the caller are not held up. */
    @Test
    void aHungDatabaseNeverHoldsUpAdmission() throws Exception {
        String t = newTenant();
        String c = collectionOf(t, "a");
        CountDownLatch released = new CountDownLatch(1);
        CountDownLatch writerArrived = new CountDownLatch(1);
        DataSource hung = refusingDataSource(() -> new SQLException("refused"), () -> {
            writerArrived.countDown();
            try {
                released.await(60, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        });
        PciQueryRing ring = new PciQueryRing(new TenantScope(hung), (m, tn, col) -> true, 1024, 1, Duration.ofSeconds(30));
        ring.start();
        try {
            ring.admit(t, QueryVector.alone(MODEL_384, vec(1), c));
            assertThat(writerArrived.await(20, TimeUnit.SECONDS)).as("the writer is stuck in the database").isTrue();

            long start = System.nanoTime();
            for (int i = 0; i < 500; i++) {
                ring.admit(t, QueryVector.alone(MODEL_384, vec(i), c));
            }
            long elapsedMs = (System.nanoTime() - start) / 1_000_000;

            assertThat(elapsedMs).as("500 admissions while the writer is stuck, ms").isLessThan(2_000);
            System.out.println("MEASURE admit_while_writer_hung total_ms=" + elapsedMs + " per_admit_us="
                + (elapsedMs * 1000 / 500));
        } finally {
            released.countDown();
            ring.stop();
        }
    }

    /** A DataSource whose getConnection runs {@code beforeRefusing} (if any) and then throws what {@code failure} makes. */
    private static DataSource refusingDataSource(java.util.function.Supplier<SQLException> failure, Runnable beforeRefusing) {
        return (DataSource) Proxy.newProxyInstance(DataSource.class.getClassLoader(), new Class<?>[] {DataSource.class},
            (proxy, method, args) -> {
                if (method.getName().equals("getConnection")) {
                    if (beforeRefusing != null) {
                        beforeRefusing.run();
                    }
                    throw failure.get();
                }
                if (method.getName().equals("hashCode")) return System.identityHashCode(proxy);
                if (method.getName().equals("equals")) return proxy == args[0];
                if (method.getName().equals("toString")) return "refusing-datasource";
                throw new UnsupportedOperationException(method.getName());
            });
    }
}
