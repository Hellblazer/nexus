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
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17): {@link CompletionBootWork}, the engine's report of the completion check in its own
 * {@code /v1/status} field, {@code rdr229_completion}, which {@code nx doctor} reads. (Fix round A, critique B1: never in
 * {@code boot_work}, which is work in progress; the three-gate state waits by design until P3.4.) The field carries the three
 * gates, a row per missing CHECK (the superseded_by one says how many registry rows hold its changeset back) and a row per
 * table whose stamp is done and whose CHECK is still NOT VALID (orphans). Nothing in it names a collection or a tenant: the
 * status is unauthenticated. The field follows the state as it clears. A read that fails leaves the field as it was.
 * {@link CompletionStatusFieldIntegrationTest} covers the rendered body and the dangling-tombstone count.
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

    private static final com.fasterxml.jackson.databind.ObjectMapper MAPPER = new com.fasterxml.jackson.databind.ObjectMapper();

    /** The {@code rdr229_completion} field as the status route would send it. */
    private static com.fasterxml.jackson.databind.JsonNode field(CompletionBootWork work) throws Exception {
        String json = work.statusJson();
        assertThat(json).as("the field is there once a sample was read").isNotNull();
        return MAPPER.readTree(json);
    }

    private static Field<String> text(String name) {
        return DSL.field(DSL.name(name), String.class);
    }

    // ---- the summary ---------------------------------------------------------------------------------------------------

    @Test
    void theSummarySaysWhereTheThreeGatesStand_withNoMoveRegistered() throws Exception {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var work = new CompletionBootWork(registry, live(CompletionCheck.ReadSet.p31()));
        assertThat(work.latest()).isNull();
        assertThat(work.publish()).isEqualTo(1);
        assertThat(registry.snapshot()).as("never boot_work: it is work in progress, and this waits by design").isEmpty();
        var f = field(work);
        assertThat(f.get("summary").asText()).startsWith("gate 1 waits (15 of 16 stamp moves incomplete)")
            .contains("gate 2 waits on gate 1").endsWith("gate 3 waits: gate 2 does not hold");
        assertThat(f.get("gates_holding").asInt()).isZero();
        assertThat(f.get("gates_total").asInt()).isEqualTo(3);
        assertThat(work.latest()).isNotNull();
        assertThat(work.latest().gate1()).isFalse();
    }

    @Test
    void theSummaryCountsTheGatesThatHold() throws Exception {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var work = new CompletionBootWork(registry, live(new CompletionCheck.ReadSet(List.of(), List.of())));
        work.publish();
        var f = field(work);
        assertThat(f.get("summary").asText()).isEqualTo("gate 1 holds; gate 2 holds; gate 3 waits: the PCI coverage (P3.4) is not installed or could not be read "
            + "(no catalog, or no registry rows visible), so nothing says the id indexes exist");
        assertThat(f.get("gates_holding").asInt()).isEqualTo(2);
    }

    @Test
    void whenEveryGateHoldsAndNothingWaitsNothingIsListed_theFieldIsStillThere() throws Exception {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var reader = new Fixed(() -> CompletionCheck.evaluate(ctx, new CompletionCheck.ReadSet(List.of(), List.of()),
            c -> Optional.of(new CompletionCheck.IdIndexCoverage(Set.of("a"), Set.of("a"), Set.of())), CompletionCheck.DIRECT));
        var work = new CompletionBootWork(registry, reader);
        assertThat(work.publish()).as("every gate holds: nothing waits").isZero();
        assertThat(registry.snapshot()).isEmpty();
        assertThat(work.latest().gate3()).as("non-vacuity: gate 3 held").isTrue();
        var f = field(work);
        assertThat(f.get("gates_holding").asInt()).isEqualTo(3);
        assertThat(f.get("checks_missing")).isEmpty();
        assertThat(f.get("validate_waits")).isEmpty();
    }

    // ---- a missing CHECK and the VALIDATE that waits ---------------------------------------------------------------------

    @Test
    void aMissingSupersededByCheckIsARowThatSaysWhatHoldsItBack_withNoCount() throws Exception {
        PgContainerHelper.insertCollection(ctx, TENANT, "tomb-p31-cbw");
        Table<?> registryTable = DSL.table(DSL.name("nexus", "catalog_collections"));
        P31CheckReplay.lift(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
        try {
            ctx.update(registryTable).set(text("superseded_by"), "successor-gone").set(text("model_version"), "")
                .where(text("name").eq("tomb-p31-cbw")).execute();
            var registry = new BootWorkRegistry(Clock.systemUTC());
            var work = new CompletionBootWork(registry, live(new CompletionCheck.ReadSet(List.of(), List.of())));
            assertThat(work.publish()).isEqualTo(2);
            assertThat(registry.snapshot()).isEmpty();
            var f = field(work);
            assertThat(f.get("checks_missing")).singleElement().satisfies(c -> {
                assertThat(c.get("constraint").asText()).isEqualTo(CompletionCheck.SUPERSEDED_BY_CHECK);
                assertThat(c.get("what").asText()).startsWith("waits: " + CompletionCheck.SUPERSEDED_BY_CHECK)
                    .contains("registry rows with a blank model_version and no superseded_by_id hold its changeset back")
                    .contains("the switch is refused until then").doesNotContain("1 registry");
            });
            assertThat(f.has("superseded_by_waiting")).as("a registry row count is tenant data: /v1/status is unauthenticated").isFalse();
            assertThat(f.get("summary").asText()).contains("the superseded_by CHECK is missing");

            ctx.update(registryTable).set(DSL.field(DSL.name("superseded_by_id"), Long.class), DSL.field(DSL.name("collection_id"), Long.class))
                .where(text("name").eq("tomb-p31-cbw")).execute();
        } finally {
            P31CheckReplay.restore(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
            ctx.deleteFrom(registryTable).where(text("name").eq("tomb-p31-cbw")).execute();
        }
    }

    @Test
    void anOrphanThatKeepsACheckNotValidIsARowWithNoCount_neverTheNameOrTheTenant_andLeavesOnceValidated() throws Exception {
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
            assertThat(registry.snapshot()).isEmpty();
            var f = field(work);
            assertThat(f.get("validate_waits")).singleElement().satisfies(w -> {
                assertThat(w.get("table").asText()).isEqualTo("catalog_documents");
                assertThat(w.has("orphans") || w.has("unstamped") || w.has("counted")).as("counts are tenant data").isFalse();
                String what = w.get("what").asText();
                // item 5 (confirmation review S1 residual): an orphan row is write-frozen, and a table with no orphan row is finished by the restamp alone
                assertThat(what).startsWith("waits: catalog_documents_physical_collection_id_check on catalog_documents is NOT VALID")
                    .contains("An orphan row is write-frozen: any UPDATE of it fails with SQLSTATE 23514 until its collection is registered or the row is deleted")
                    .contains("register each missing collection or delete the orphan rows, if there are any")
                    .contains("nexus.rdr229_restamp('catalog_documents')").contains("as the migrating role")
                    .contains("With no orphan row, that restamp step alone finishes it")
                    .doesNotMatch("(?s).*\\d+\\s+(orphan|unstamped).*");
            });
            assertThat(f.toString()).doesNotContain(GHOST).doesNotContain(TENANT);

            ctx.deleteFrom(docs).where(text("tenant_id").eq(TENANT)).execute();
            PgContainerHelper.validateConstraint(su, docs, "catalog_documents_physical_collection_id_check");
            work.publish();
            assertThat(field(work).get("validate_waits")).as("the row leaves once the CHECK is validated").isEmpty();
        } finally {
            ctx.deleteFrom(docs).where(text("tenant_id").eq(TENANT)).execute();
        }
    }

    // ---- reading ---------------------------------------------------------------------------------------------------------

    @Test
    void aReadThatFailsLeavesTheFieldAsItWas() throws Exception {
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
        var before = work.statusJson();
        assertThat(work.publish()).as("the read failed").isEqualTo(-1);
        assertThat(work.statusJson()).isEqualTo(before);
        assertThat(work.publish()).as("and the next sample recovers").isEqualTo(1);
    }

    @Test
    void closeTakesTheFieldOffAndEndsTheRead() {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var reader = (Fixed) live(new CompletionCheck.ReadSet(List.of(), List.of()));
        var work = new CompletionBootWork(registry, reader);
        work.publish();
        assertThat(registry.rdr229Completion()).isNotNull();
        work.close();
        assertThat(registry.rdr229Completion()).as("a closed check leaves no field behind").isNull();
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
