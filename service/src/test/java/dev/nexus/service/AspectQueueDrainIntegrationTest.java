// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AspectQueueDrain;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TupleRepository;
import dev.nexus.service.tuples.TemplateRegistry;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-hbnml: the engine-start drain of a retired {@code aspect_extraction_queue} table that
 * {@code aspects-005-2} kept because it held rows ({@link AspectQueueDrain}).
 *
 * <p>Each test walks the changelog as a NOBYPASSRLS, non-superuser owner (the production migration role) to just
 * before the drop, seeds rows as superuser (as old clients wrote them), lets the real migration KEEP the table, and
 * runs the drain with the real {@link AspectQueueDrain.AdminCensus} on the owner's connection and the real
 * {@code nexus_svc} pool for the moves. Assertions read the tuple table and the queue table back as superuser.
 *
 * <ul>
 *   <li>{@link #pendingInProgressAndFailedRowsOfTwoTenantsMoveToTheRightTuples_theNextBootDropsTheTable}: the happy
 *       path, tenant isolation, the delay of a retrying row, the dead-letter state of a failed row, the document's
 *       CURRENT collection (a rename or move since the row was written), a document that already has a live task
 *       (not overwritten), an idempotent second run, and the next boot's drop.</li>
 *   <li>{@link #aRowThatCannotBeMovedStaysAndIsReported}: an over-cap body and an unknown status stay, are counted by
 *       reason, the table is kept at the next boot, and the healthy row still moved.</li>
 * </ul>
 */
class AspectQueueDrainIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_aspq_drain";
    private static final String ADMIN_PASS = "nexus_admin_aspq_drain_pass";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String DROP_CHANGESET = "aspects-005-2";

    private static final String TENANT_A = "drain-a";
    private static final String TENANT_B = "drain-b";
    private static final String COLL_A = "knowledge__drain-a__minilm-l6-v2-384__v1";
    private static final String COLL_A2 = "knowledge__drain-a2__minilm-l6-v2-384__v1";
    private static final String COLL_B = "knowledge__drain-b__minilm-l6-v2-384__v1";

    private static final Table<?> QUEUE = DSL.table(DSL.name("nexus", "aspect_extraction_queue"));
    private static final ObjectMapper JSON = new ObjectMapper();

    /** A walked, seeded and migrated database with the real pools. */
    private static final class Fixture implements AutoCloseable {
        final PostgreSQLContainer<?> pg;
        final HikariDataSource adminDs;
        final HikariDataSource svcDs;
        final TenantScope scope;
        final TupleRepository tuples;

        Fixture(java.util.function.Consumer<DSLContext> seed) throws Exception {
            pg = PgContainerHelper.startEmptyDatabase();
            try {
                try (Connection su = pg.createConnection("")) {
                    PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
                }
                adminDs = pool(pg, ADMIN_ROLE, ADMIN_PASS, 2);
                migrateUpTo(adminDs, DROP_CHANGESET);
                try (Connection su = pg.createConnection("")) {
                    seed.accept(DSL.using(su, SQLDialect.POSTGRES));
                }
                SchemaMigrator.migrate(adminDs);
                svcDs = pool(pg, "nexus_svc", PgContainerHelper.SVC_PASSWORD, 6);
                scope = new TenantScope(svcDs);
                tuples = new TupleRepository(scope,
                    TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L));
            } catch (Exception | Error e) {
                pg.stop();
                throw e;
            }
        }

        AspectQueueDrain drain() {
            return new AspectQueueDrain(scope, tuples,
                new AspectQueueDrain.AdminCensus(new AdminConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)),
                () -> List.of(TENANT_A, TENANT_B));
        }

        <T> T su(java.util.function.Function<DSLContext, T> f) throws Exception {
            try (Connection c = pg.createConnection("")) {
                c.setAutoCommit(true);
                return f.apply(DSL.using(c, SQLDialect.POSTGRES));
            }
        }

        @Override
        public void close() {
            svcDs.close();
            adminDs.close();
            pg.stop();
        }
    }

    // ── the happy path ──────────────────────────────────────────────────────

    @Test
    void pendingInProgressAndFailedRowsOfTwoTenantsMoveToTheRightTuples_theNextBootDropsTheTable() throws Exception {
        try (Fixture fx = new Fixture(ctx -> {
            for (String[] c : new String[][] {{TENANT_A, COLL_A}, {TENANT_A, COLL_A2}, {TENANT_B, COLL_B}}) {
                PgContainerHelper.insertCollection(ctx, c[0], c[1]);
            }
            doc(ctx, TENANT_A, "a.1", COLL_A);
            doc(ctx, TENANT_A, "a.2", COLL_A);
            doc(ctx, TENANT_A, "a.3", COLL_A);
            doc(ctx, TENANT_A, "a.4", COLL_A2);   // the document moved to COLL_A2 after its row was written
            doc(ctx, TENANT_B, "b.1", COLL_B);
            doc(ctx, TENANT_B, "b.2", COLL_B);
            doc(ctx, TENANT_B, "b.3", COLL_B);
            row(ctx, TENANT_A, COLL_A, "a.1", "/a/1.md", "pending", OffsetDateTime.now().plusHours(1), "h1");
            row(ctx, TENANT_A, COLL_A, "a.2", "/a/2.md", "in_progress", OffsetDateTime.now().plusHours(1), "h2");
            row(ctx, TENANT_A, COLL_A, "a.3", "/a/3.md", "failed", null, "h3");
            row(ctx, TENANT_A, COLL_A, "a.4", "/a/4.md", "pending", null, "h4");
            row(ctx, TENANT_B, COLL_B, "b.1", "/b/1.md", "pending", null, "hb1");
            row(ctx, TENANT_B, COLL_B, "b.2", "/b/2.md", "failed", null, "hb2");
            row(ctx, TENANT_B, COLL_B, "b.3", "/b/3.md", "pending", null, "old-hash");
        })) {
            // The table was kept by the real migration: it holds rows.
            assertThat(fx.<Integer>su(ctx -> ctx.fetchCount(QUEUE))).isEqualTo(7);
            // b.3 already has a live task, written by a current client with a newer content hash.
            fx.tuples.out(TENANT_B, "aspects", Map.of("queue", "aspects"), Map.of(),
                body(COLL_B, "/b/3.md", "b.3", "newer-hash"), "b.3", null);

            AspectQueueDrain.Result r = fx.drain().run();

            assertThat(r.tablePresent()).isTrue();
            assertThat(r.rowsRetained()).as("every row moved or was already queued").isZero();
            assertThat(r.moved()).as("a.1 a.2 a.4 b.1: pending and in_progress").isEqualTo(4);
            assertThat(r.deadLettered()).as("a.3 b.2: failed").isEqualTo(2);
            assertThat(r.alreadyQueued()).as("b.3").isEqualTo(1);
            assertThat(fx.<Integer>su(ctx -> ctx.fetchCount(QUEUE))).as("the table is empty").isZero();

            Map<String, Record> byDoc = tasks(fx);
            assertThat(byDoc.keySet()).containsExactlyInAnyOrder("a.1", "a.2", "a.3", "a.4", "b.1", "b.2", "b.3");

            // tenant isolation: each task sits in its own row's tenant
            for (String d : List.of("a.1", "a.2", "a.3", "a.4")) {
                assertThat(byDoc.get(d).get(TUPLES.TENANT_ID)).as(d).isEqualTo(TENANT_A);
            }
            for (String d : List.of("b.1", "b.2", "b.3")) {
                assertThat(byDoc.get(d).get(TUPLES.TENANT_ID)).as(d).isEqualTo(TENANT_B);
            }
            // the identity body, with the document's current collection
            JsonNode a1 = JSON.readTree(byDoc.get("a.1").get(TUPLES.BODY));
            assertThat(a1.get("collection").asText()).isEqualTo(COLL_A);
            assertThat(a1.get("source_path").asText()).isEqualTo("/a/1.md");
            assertThat(a1.get("doc_id").asText()).isEqualTo("a.1");
            assertThat(a1.get("content_hash").asText()).isEqualTo("h1");
            assertThat(JSON.readTree(byDoc.get("a.4").get(TUPLES.BODY)).get("collection").asText())
                .as("the document's current home, not the row's stale collection").isEqualTo(COLL_A2);
            // pending with a retry time keeps its wait; in_progress moves with none
            assertThat(byDoc.get("a.1").get(TUPLES.AVAILABLE_AT)).as("a.1 waits out its retry")
                .isNotNull().isAfter(OffsetDateTime.now().plusMinutes(50));
            assertThat(byDoc.get("a.1").get(TUPLES.CLAIM_STATE)).isNull();
            assertThat(byDoc.get("a.2").get(TUPLES.AVAILABLE_AT)).as("in_progress: no delay").isNull();
            assertThat(byDoc.get("a.2").get(TUPLES.CLAIM_STATE)).isNull();
            assertThat(byDoc.get("a.4").get(TUPLES.AVAILABLE_AT)).isNull();
            // failed rows are dead-lettered, so requeue-failed finds them
            for (String d : List.of("a.3", "b.2")) {
                assertThat(byDoc.get(d).get(TUPLES.CLAIM_STATE)).as(d).isEqualTo("dead");
                assertThat(byDoc.get(d).get(TUPLES.AVAILABLE_AT)).as(d).isNull();
                assertThat(byDoc.get(d).get(TUPLES.ATTEMPTS)).as(d).isEqualTo(6);
                assertThat(byDoc.get(d).get(TUPLES.CONSUMED_AT)).as(d).isNull();
            }
            // the live task of b.3 was not overwritten
            assertThat(JSON.readTree(byDoc.get("b.3").get(TUPLES.BODY)).get("content_hash").asText())
                .isEqualTo("newer-hash");
            // a drained task is claimable by a worker; a dead one is not
            var claimed = fx.tuples.inp(TENANT_B, "aspects", Map.of("queue", "aspects"), "w-1", 600L);
            assertThat(claimed).isPresent();

            // idempotent: a second run finds the table present and empty and moves nothing
            AspectQueueDrain.Result again = fx.drain().run();
            assertThat(again.tablePresent()).isTrue();
            assertThat(again.rowsRetained()).isZero();
            assertThat(again.moved() + again.deadLettered() + again.alreadyQueued()).isZero();

            // the next boot's guard finds the table empty and drops it; the drain then finds no table
            SchemaMigrator.migrate(fx.adminDs);
            assertThat(fx.<Boolean>su(ctx -> PgCatalogProbes.tableExists(ctx, "nexus", "aspect_extraction_queue"))).isFalse();
            assertThat(fx.<Boolean>su(ctx -> ctx.fetchExists(DSL.table(DSL.name("public", "databasechangelog")),
                DSL.field(DSL.name("id"), String.class).eq(DROP_CHANGESET)))).as("aspects-005-2 recorded").isTrue();
            AspectQueueDrain.Result gone = fx.drain().run();
            assertThat(gone.tablePresent()).isFalse();
            assertThat(gone.rowsRetained()).isZero();
        }
    }

    /**
     * RDR-229 P3.5c: from gate 3 a document's home is the registry's current label of its {@code physical_collection_id}. The
     * registry row of COLL_A is renamed in place after the migration (FK triggers off: P3.7's one-row rename leaves the document
     * row saying COLL_A with the id), so the task a drain writes carries COLL_A before the latch and the new label after it.
     */
    @Test
    void aDocumentsHomeIsReadByItsIdFromGate3() throws Exception {
        String renamed = "knowledge__drain-a-renamed__minilm-l6-v2-384__v1";
        try (Fixture fx = new Fixture(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT_A, COLL_A);
            doc(ctx, TENANT_A, "a.1", COLL_A);
            row(ctx, TENANT_A, COLL_A, "a.1", "/a/1.md", "pending", null, "h1");
        })) {
            fx.su(ctx -> {
                ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
                try {
                    ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, renamed)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT_A)).and(CATALOG_COLLECTIONS.NAME.eq(COLL_A)).execute();
                } finally {
                    ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
                }
                return null;
            });
            AspectQueueDrain.Result byName = fx.drain().run();
            assertThat(byName.moved()).isEqualTo(1);
            assertThat(JSON.readTree(tasks(fx).get("a.1").get(TUPLES.BODY)).get("collection").asText())
                .as("before the latch: the document's own name column").isEqualTo(COLL_A);

            fx.su(ctx -> {
                // a document row the stamp reached: name COLL_A, the id of the renamed registry row
                ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                        CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
                    .values(DSL.val(TENANT_A), DSL.val("a.2"), DSL.val("Test Doc a.2"), DSL.val(COLL_A), CollectionIds.idOf(TENANT_A, renamed)).execute();
                ctx.insertInto(QUEUE, DSL.field(DSL.name("tenant_id")), DSL.field(DSL.name("collection")),
                        DSL.field(DSL.name("collection_id")), DSL.field(DSL.name("source_path")), DSL.field(DSL.name("doc_id")),
                        DSL.field(DSL.name("status")), DSL.field(DSL.name("enqueued_at")), DSL.field(DSL.name("content_hash")))
                    .values(DSL.val(TENANT_A), DSL.val(renamed), CollectionIds.idOf(TENANT_A, renamed), DSL.val("/a/2.md"),
                        DSL.val("a.2"), DSL.val("pending"), DSL.val(OffsetDateTime.now()), DSL.val("h2"))
                    .execute();
                return null;
            });
            AspectQueueDrain byId = new AspectQueueDrain(fx.scope, fx.tuples,
                new AspectQueueDrain.AdminCensus(new AdminConnection(fx.pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)),
                () -> List.of(TENANT_A, TENANT_B), () -> true);
            assertThat(byId.run().moved()).isEqualTo(1);
            assertThat(JSON.readTree(tasks(fx).get("a.2").get(TUPLES.BODY)).get("collection").asText())
                .as("from gate 3: the registry's current label of the document's id").isEqualTo(renamed);

            // a document with no home: the queue row's own collection_id (the table kept the column), not its stale name
            fx.su(ctx -> {
                ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE)
                    .values("drain-a", "a.3", "Test Doc a.3").execute();
                ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
                try {
                    ctx.insertInto(QUEUE, DSL.field(DSL.name("tenant_id")), DSL.field(DSL.name("collection")),
                            DSL.field(DSL.name("collection_id")), DSL.field(DSL.name("source_path")), DSL.field(DSL.name("doc_id")),
                            DSL.field(DSL.name("status")), DSL.field(DSL.name("enqueued_at")), DSL.field(DSL.name("content_hash")))
                        .values(DSL.val(TENANT_A), DSL.val(COLL_A), CollectionIds.idOf(TENANT_A, renamed), DSL.val("/a/3.md"),
                            DSL.val("a.3"), DSL.val("pending"), DSL.val(OffsetDateTime.now()), DSL.val("h3"))
                        .execute();
                } finally {
                    ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
                }
                return null;
            });
            assertThat(byId.run().moved()).isEqualTo(1);
            assertThat(JSON.readTree(tasks(fx).get("a.3").get(TUPLES.BODY)).get("collection").asText())
                .as("no home: the queue row's collection_id names the registry's current label, not the stale name")
                .isEqualTo(renamed);
        }
    }

    /**
     * RDR-229 P3.5c (linda 2026-11-23, census L2): from gate 3 a queued row with no home document and no collection id resolves its stored
     * name to a LIVE registry row. One that does is dispatched under that name; one that names nothing is skipped with a WARN, counted, and
     * left in the table: never dispatched under a label nothing holds.
     */
    @Test
    void aQueuedRowWithNoIdResolvesItsNameToALiveRow_orIsSkippedAndLeftInTheTable() throws Exception {
        String nobody = "knowledge__drain-nobody__minilm-l6-v2-384__v1";
        try (Fixture fx = new Fixture(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT_A, COLL_A);
            // two documents with no home, two queue rows with no collection_id: one names a registered live collection, one names nothing
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "a.live");
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "a.nobody");
            row(ctx, TENANT_A, COLL_A, "a.live", "/a/live.md", "pending", null, "hl");
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();   // the queue row names a collection nothing registers
            try {
                row(ctx, TENANT_A, nobody, "a.nobody", "/a/nobody.md", "pending", null, "hn");
            } finally {
                ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
            }
        })) {
            long before = AspectQueueDrain.skippedNoLiveCollection();
            AspectQueueDrain latched = new AspectQueueDrain(fx.scope, fx.tuples,
                new AspectQueueDrain.AdminCensus(new AdminConnection(fx.pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)),
                () -> List.of(TENANT_A, TENANT_B), () -> true);

            latched.run();

            var tasks = tasks(fx);
            assertThat(tasks).as("the row whose name a live registry row holds is dispatched, the other is not")
                .containsKey("a.live").doesNotContainKey("a.nobody");
            assertThat(JSON.readTree(tasks.get("a.live").get(TUPLES.BODY)).get("collection").asText()).isEqualTo(COLL_A);
            assertThat(AspectQueueDrain.skippedNoLiveCollection()).as("counted (the row is read again on a later batch)").isGreaterThanOrEqualTo(before + 1);
            int left = fx.su(ctx -> ctx.fetchCount(QUEUE, DSL.field(DSL.name("doc_id")).eq("a.nobody")));
            assertThat(left).as("and left in the table").isEqualTo(1);
        }
    }

    /**
     * RDR-229 P3.5c (linda 2026-11-23, ruling a): a queued row whose collection_id names no registry row (dangling) is skipped with a WARN and
     * a count, the same as the no-live-row case: it is not dispatched under its stored name, though a live collection bears that name.
     */
    @Test
    void aQueuedRowWhoseCollectionIdNamesNoRegistryRowIsSkippedWithAWarn_notDispatchedUnderItsStoredName() throws Exception {
        try (Fixture fx = new Fixture(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT_A, COLL_A);
            PgContainerHelper.insertCatalogDocument(ctx, TENANT_A, "a.dangling");
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            try {
                ctx.insertInto(QUEUE, DSL.field(DSL.name("tenant_id")), DSL.field(DSL.name("collection")),
                        DSL.field(DSL.name("collection_id")), DSL.field(DSL.name("source_path")), DSL.field(DSL.name("doc_id")),
                        DSL.field(DSL.name("status")), DSL.field(DSL.name("enqueued_at")), DSL.field(DSL.name("content_hash")))
                    .values(DSL.val(TENANT_A), DSL.val(COLL_A), DSL.val(987654321L), DSL.val("/a/dangling.md"),
                        DSL.val("a.dangling"), DSL.val("pending"), DSL.val(OffsetDateTime.now()), DSL.val("hd"))
                    .execute();
            } finally {
                ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
            }
        })) {
            long before = AspectQueueDrain.skippedNoLiveCollection();
            var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(AspectQueueDrain.class);
            var appender = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
            appender.start();
            logger.addAppender(appender);
            AspectQueueDrain latched = new AspectQueueDrain(fx.scope, fx.tuples,
                new AspectQueueDrain.AdminCensus(new AdminConnection(fx.pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS)),
                () -> List.of(TENANT_A, TENANT_B), () -> true);
            try {
                latched.run();
            } finally {
                logger.detachAppender(appender);
            }

            assertThat(tasks(fx)).as("a dangling collection id is no collection: nothing is dispatched").doesNotContainKey("a.dangling");
            assertThat(AspectQueueDrain.skippedNoLiveCollection()).as("counted").isGreaterThanOrEqualTo(before + 1);
            assertThat(appender.list).as("and a WARN names the reason").anyMatch(e ->
                e.getLevel() == ch.qos.logback.classic.Level.WARN && e.getFormattedMessage().contains("reason=dangling_collection_id"));
            assertThat((int) fx.su(ctx -> ctx.fetchCount(QUEUE, DSL.field(DSL.name("doc_id")).eq("a.dangling")))).as("and the row stays").isEqualTo(1);
        }
    }

    // ── rows that cannot move ───────────────────────────────────────────────

    @Test
    void aRowThatCannotBeMovedStaysAndIsReported() throws Exception {
        String hugePath = "/huge/" + "x".repeat(5000);
        try (Fixture fx = new Fixture(ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT_A, COLL_A);
            doc(ctx, TENANT_A, "a.1", COLL_A);
            doc(ctx, TENANT_A, "a.2", COLL_A);
            doc(ctx, TENANT_A, "a.3", COLL_A);
            row(ctx, TENANT_A, COLL_A, "a.1", "/a/1.md", "pending", null, "h1");
            row(ctx, TENANT_A, COLL_A, "a.2", hugePath, "pending", null, "h2");
            row(ctx, TENANT_A, COLL_A, "a.3", "/a/3.md", "weird", null, "h3");
        })) {
            AspectQueueDrain.Result r = fx.drain().run();

            assertThat(r.moved()).as("the healthy row moved").isEqualTo(1);
            assertThat(r.tablePresent()).isTrue();
            assertThat(r.rowsRetained()).as("the two unmovable rows are counted, never a clean zero").isEqualTo(2);
            assertThat(r.reasons()).containsEntry(AspectQueueDrain.REASON_BODY_OVER_CAP, 1L)
                .containsEntry(AspectQueueDrain.REASON_UNKNOWN_STATUS, 1L);
            assertThat(fx.<List<String>>su(ctx -> ctx.select(DSL.field(DSL.name("doc_id"), String.class)).from(QUEUE)
                .orderBy(DSL.field(DSL.name("doc_id"))).fetch(0, String.class)))
                .as("they stay in the table").containsExactly("a.2", "a.3");
            assertThat(tasks(fx).keySet()).containsExactly("a.1");

            // the next boot keeps the table (it is not empty) and does not halt
            SchemaMigrator.migrate(fx.adminDs);
            assertThat(fx.<Boolean>su(ctx -> PgCatalogProbes.tableExists(ctx, "nexus", "aspect_extraction_queue"))).isTrue();
            // and a second drain reports the same two rows, moving nothing more
            AspectQueueDrain.Result again = fx.drain().run();
            assertThat(again.rowsRetained()).isEqualTo(2);
            assertThat(again.moved()).isZero();
        }
    }

    /** The census cannot run: the drain still tries the fallback tenants and reports the count as unknown. */
    @Test
    void aCensusThatFailsMakesTheRetainedCountUnknown_neverZero() {
        AspectQueueDrain drain = new AspectQueueDrain(null, null, () -> {
            throw new java.sql.SQLException("permission denied");
        }, List::of);
        AspectQueueDrain.Result r = drain.run();
        assertThat(r.tablePresent()).isTrue();
        assertThat(r.rowsRetained()).isEqualTo(-1);
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    private static Map<String, Record> tasks(Fixture fx) throws Exception {
        Map<String, Record> out = new LinkedHashMap<>();
        for (Record r : fx.su(ctx -> ctx.select(TUPLES.TENANT_ID, TUPLES.BODY, TUPLES.CLAIM_STATE, TUPLES.AVAILABLE_AT,
                TUPLES.ATTEMPTS, TUPLES.CONSUMED_AT).from(TUPLES).where(TUPLES.SUBSPACE.eq("aspects"))
            .orderBy(TUPLES.TENANT_ID, TUPLES.CREATED_AT).fetch())) {
            String docId = JSON.readTree(r.get(TUPLES.BODY)).get("doc_id").asText();
            out.put(docId, r);
        }
        return out;
    }

    private static String body(String collection, String path, String docId, String hash) throws Exception {
        Map<String, String> m = new LinkedHashMap<>();
        m.put("collection", collection);
        m.put("source_path", path);
        m.put("doc_id", docId);
        m.put("content_hash", hash);
        return JSON.writeValueAsString(m);
    }

    private static void doc(DSLContext ctx, String tenant, String tumbler, String home) {
        PgContainerHelper.insertCatalogDocument(ctx, tenant, tumbler);
        ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, home)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)).execute();
    }

    private static void row(DSLContext ctx, String tenant, String collection, String docId, String path,
                            String status, OffsetDateTime nextRetryAt, String hash) {
        ctx.insertInto(QUEUE, DSL.field(DSL.name("tenant_id")), DSL.field(DSL.name("collection")),
                DSL.field(DSL.name("source_path")), DSL.field(DSL.name("doc_id")), DSL.field(DSL.name("status")),
                DSL.field(DSL.name("enqueued_at")), DSL.field(DSL.name("next_retry_at")),
                DSL.field(DSL.name("content_hash")))
            .values(tenant, collection, path, docId, status, OffsetDateTime.now(), nextRetryAt, hash)
            .execute();
    }

    private static HikariDataSource pool(PostgreSQLContainer<?> pg, String user, String pass, int size) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(user);
        cfg.setPassword(pass);
        cfg.setMaximumPoolSize(size);
        cfg.setAutoCommit(true);
        cfg.setPoolName("aspq-drain-" + user);
        return new HikariDataSource(cfg);
    }

    /** Applies the HEAD changelog up to, but not including, {@code changesetId}. */
    private static void migrateUpTo(HikariDataSource ds, String changesetId) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<liquibase.changelog.ChangeSet> unrun =
                    liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (changesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as("%s must be pending in the HEAD changelog", changesetId)
                    .isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }
}
