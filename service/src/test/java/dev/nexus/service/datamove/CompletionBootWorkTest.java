// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17): {@link CompletionBootWork}, the engine's report of the completion check in
 * {@code /v1/status} {@code boot_work}, which {@code nx doctor} reads. One entry for the three gates, one per missing CHECK
 * (the superseded_by one says how many registry rows hold its changeset back), one per table whose stamp is done and whose
 * CHECK is still NOT VALID (orphans). No entry names a collection or a tenant: the status is unauthenticated. Entries leave
 * when the state clears. A read that fails leaves the entries as they were.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CompletionBootWorkTest {

    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final String TENANT = "p31-cbw-tenant";
    private static final String GHOST = "ghost-collection-p31-cbw";

    private PostgreSQLContainer<?> pg;
    private Connection su;
    private DSLContext ctx;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        su = pg.createConnection("");
        su.setAutoCommit(true);
        ctx = DSL.using(su, SQLDialect.POSTGRES);
    }

    @AfterAll
    void stop() throws Exception {
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void emptyRegistry() {
        ctx.deleteFrom(MOVES).execute();
    }

    private static final class Fixed implements CompletionBootWork.Reader {
        private final Supplier<CompletionCheck.Result> source;
        boolean closed;
        boolean aborted;

        Fixed(Supplier<CompletionCheck.Result> source) {
            this.source = source;
        }

        @Override
        public CompletionCheck.Result read() {
            return source.get();
        }

        @Override
        public void abort() {
            aborted = true;
        }

        @Override
        public void close() {
            closed = true;
        }
    }

    private CompletionBootWork.Reader live(CompletionCheck.ReadSet readSet) {
        return new Fixed(() -> CompletionCheck.evaluate(ctx, readSet, CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT));
    }

    private static Map<String, BootWorkRegistry.Entry> byId(BootWorkRegistry registry) {
        Map<String, BootWorkRegistry.Entry> out = new java.util.LinkedHashMap<>();
        registry.snapshot().forEach(e -> out.put(e.id(), e));
        return out;
    }

    private static Field<String> text(String name) {
        return DSL.field(DSL.name(name), String.class);
    }

    // ---- the summary ---------------------------------------------------------------------------------------------------

    @Test
    void theSummaryEntrySaysWhereTheThreeGatesStand_withNoMoveRegistered() {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var work = new CompletionBootWork(registry, live(CompletionCheck.ReadSet.p31()));
        assertThat(work.latest()).isNull();
        assertThat(work.publish()).isEqualTo(1);
        var entries = byId(registry);
        assertThat(entries).containsOnlyKeys(CompletionBootWork.SUMMARY_ID);
        var e = entries.get(CompletionBootWork.SUMMARY_ID);
        assertThat(e.what()).startsWith("gate 1 waits (15 of 16 stamp moves incomplete)")
            .contains("gate 2 waits on gate 1").endsWith("gate 3 waits: gate 2 does not hold");
        assertThat(e.done()).isZero();
        assertThat(e.total()).isEqualTo(3L);
        assertThat(e.unit()).isEqualTo("gates");
        assertThat(work.latest()).isNotNull();
        assertThat(work.latest().gate1()).isFalse();
    }

    @Test
    void theSummaryCountsTheGatesThatHold() {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var work = new CompletionBootWork(registry, live(new CompletionCheck.ReadSet(List.of(), List.of())));
        work.publish();
        var e = byId(registry).get(CompletionBootWork.SUMMARY_ID);
        assertThat(e.what()).isEqualTo("gate 1 holds; gate 2 holds; gate 3 waits: this release builds no id-predicate index (P3.4), "
            + "so no collection has a valid one");
        assertThat(e.done()).isEqualTo(2L);
    }

    // ---- a missing CHECK and the VALIDATE that waits ---------------------------------------------------------------------

    @Test
    void aMissingSupersededByCheckIsAnEntryThatSaysHowManyRowsHoldItBack_andLeavesWhenItLands() throws Exception {
        PgContainerHelper.insertCollection(ctx, TENANT, "tomb-p31-cbw");
        Table<?> registryTable = DSL.table(DSL.name("nexus", "catalog_collections"));
        P31CheckReplay.lift(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
        try {
            ctx.update(registryTable).set(text("superseded_by"), "successor-gone").set(text("model_version"), "")
                .where(text("name").eq("tomb-p31-cbw")).execute();
            var registry = new BootWorkRegistry(Clock.systemUTC());
            var work = new CompletionBootWork(registry, live(new CompletionCheck.ReadSet(List.of(), List.of())));
            assertThat(work.publish()).isEqualTo(2);
            var entries = byId(registry);
            var id = CompletionBootWork.CHECK_PREFIX + CompletionCheck.SUPERSEDED_BY_CHECK;
            assertThat(entries).containsKeys(CompletionBootWork.SUMMARY_ID, id);
            assertThat(entries.get(id).what()).startsWith("waits: " + CompletionCheck.SUPERSEDED_BY_CHECK)
                .contains("1 registry row(s) with a blank model_version and no superseded_by_id")
                .contains("the switch is refused until then");
            assertThat(entries.get(CompletionBootWork.SUMMARY_ID).what()).contains("the superseded_by CHECK is missing");

            ctx.update(registryTable).set(DSL.field(DSL.name("superseded_by_id"), Long.class), DSL.field(DSL.name("collection_id"), Long.class))
                .where(text("name").eq("tomb-p31-cbw")).execute();
        } finally {
            P31CheckReplay.restore(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
            ctx.deleteFrom(registryTable).where(text("name").eq("tomb-p31-cbw")).execute();
        }
    }

    @Test
    void anOrphanThatKeepsACheckNotValidIsAnEntryWithTheCount_neverTheNameOrTheTenant_andLeavesOnceValidated() throws Exception {
        Table<?> docs = DSL.table(DSL.name("nexus", "catalog_documents"));
        P31CheckReplay.lift(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
        try {
            ctx.insertInto(docs).set(text("tenant_id"), TENANT).set(text("tumbler"), "1.8.1").set(text("title"), "t")
                .set(text("physical_collection"), GHOST).execute();
        } finally {
            P31CheckReplay.restore(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
        }
        try {
            String label = StampLabels.labelFor("catalog_documents");
            ctx.insertInto(MOVES).set(text("label"), label).set(text("template"), "t").set(text("driving_schema"), "nexus")
                .set(text("driving_table"), "catalog_documents")
                .set(DSL.field(DSL.name("chunk_size"), Integer.class), 100).set(DSL.field(DSL.name("sleep_ms"), Integer.class), 0)
                .set(DSL.field(DSL.name("statement_timeout_ms"), Integer.class), 60_000)
                .set(text("verification"), "v").set(text("changeset_id"), "test")
                .set(DSL.field(DSL.name("completed_at"), org.jooq.impl.SQLDataType.TIMESTAMPWITHTIMEZONE), java.time.OffsetDateTime.now())
                .execute();
            var registry = new BootWorkRegistry(Clock.systemUTC());
            var work = new CompletionBootWork(registry, live(new CompletionCheck.ReadSet(List.of(label), List.of())));
            assertThat(work.publish()).isEqualTo(2);
            var entries = byId(registry);
            var e = entries.get(CompletionBootWork.VALIDATE_PREFIX + "catalog_documents");
            assertThat(e).isNotNull();
            assertThat(e.what()).isEqualTo("waits: 1 orphan row(s) (a name with no registry row in its tenant) and 0 unstamped row(s) keep "
                + "catalog_documents_physical_collection_id_check on catalog_documents NOT VALID; its VALIDATE waits instead of failing a boot");
            assertThat(entries.values()).allSatisfy(x -> assertThat(x.what()).doesNotContain(GHOST).doesNotContain(TENANT));

            ctx.deleteFrom(docs).where(text("tenant_id").eq(TENANT)).execute();
            PgContainerHelper.validateConstraint(su, docs, "catalog_documents_physical_collection_id_check");
            work.publish();
            assertThat(byId(registry)).as("the entry leaves once the CHECK is validated").containsOnlyKeys(CompletionBootWork.SUMMARY_ID);
        } finally {
            ctx.deleteFrom(docs).where(text("tenant_id").eq(TENANT)).execute();
        }
    }

    // ---- reading ---------------------------------------------------------------------------------------------------------

    @Test
    void aReadThatFailsLeavesTheEntriesAsTheyWere() {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        AtomicInteger calls = new AtomicInteger();
        var reader = new Fixed(() -> {
            if (calls.incrementAndGet() == 2) {
                throw new IllegalStateException("the admin session is gone");
            }
            return CompletionCheck.evaluate(ctx, new CompletionCheck.ReadSet(List.of(), List.of()),
                CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT);
        });
        var work = new CompletionBootWork(registry, reader);
        assertThat(work.publish()).isEqualTo(1);
        var before = byId(registry).get(CompletionBootWork.SUMMARY_ID);
        assertThat(work.publish()).as("the read failed").isEqualTo(-1);
        assertThat(byId(registry).get(CompletionBootWork.SUMMARY_ID)).isEqualTo(before);
        assertThat(work.publish()).as("and the next sample recovers").isEqualTo(1);
    }

    @Test
    void closeTakesTheEntriesOffAndEndsTheRead() {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var reader = (Fixed) live(new CompletionCheck.ReadSet(List.of(), List.of()));
        var work = new CompletionBootWork(registry, reader);
        work.publish();
        assertThat(registry.snapshot()).hasSize(1);
        work.close();
        assertThat(registry.snapshot()).isEmpty();
        assertThat(reader.aborted).isTrue();
        assertThat(reader.closed).isTrue();
        assertThat(work.publish()).as("a sample after close does nothing").isEqualTo(-1);
    }

    @Test
    void refreshReadsAtOnce_andTheScheduleKeepsReading() throws Exception {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        AtomicInteger reads = new AtomicInteger();
        var work = new CompletionBootWork(registry, new Fixed(() -> {
            reads.incrementAndGet();
            return CompletionCheck.evaluate(ctx, new CompletionCheck.ReadSet(List.of(), List.of()),
                CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT);
        }));
        try {
            assertThat(work.refresh()).isNotNull();
            assertThat(reads.get()).isEqualTo(1);
            work.start(Duration.ofMillis(20));
            long deadline = System.nanoTime() + Duration.ofSeconds(20).toNanos();
            while (reads.get() < 4 && System.nanoTime() < deadline) {
                Thread.sleep(10);
            }
            assertThat(reads.get()).as("the schedule re-reads with no restart").isGreaterThanOrEqualTo(4);
        } finally {
            work.close();
        }
    }

    // ---- the count of rows that keep a CHECK NOT VALID is bounded work, kept for a while --------------------------------

    @Test
    void theViolatorCountIsKeptForTheTtl_thenReadAgain_andACountThatFailedIsNotKept() {
        AtomicInteger calls = new AtomicInteger();
        boolean[] fail = {false};
        CompletionCheck.ViolatorCounter delegate = (c, row) -> {
            calls.incrementAndGet();
            return new CompletionCheck.Violators(row.table(), row.constraint(), 1, 2, !fail[0]);
        };
        var now = new Instant[] {Instant.parse("2026-11-21T00:00:00Z")};
        Clock clock = new Clock() {
            @Override public java.time.ZoneId getZone() { return ZoneOffset.UTC; }
            @Override public Clock withZone(java.time.ZoneId zone) { return this; }
            @Override public Instant instant() { return now[0]; }
        };
        var counter = new CompletionBootWork.CachedCounter(delegate, clock, Duration.ofMinutes(10));
        var row = P31Checks.load().stream().filter(P31Checks.Row::hasCheck).findFirst().orElseThrow();
        counter.count(ctx, row);
        counter.count(ctx, row);
        assertThat(calls.get()).as("the second call inside the TTL reads nothing").isEqualTo(1);
        now[0] = now[0].plus(Duration.ofMinutes(11));
        counter.count(ctx, row);
        assertThat(calls.get()).as("after the TTL it counts again").isEqualTo(2);
        fail[0] = true;
        now[0] = now[0].plus(Duration.ofMinutes(11));
        assertThat(counter.count(ctx, row).counted()).isFalse();
        fail[0] = false;
        counter.count(ctx, row);
        assertThat(calls.get()).as("a failed count is not kept: the next call asks again").isEqualTo(4);
    }
}
