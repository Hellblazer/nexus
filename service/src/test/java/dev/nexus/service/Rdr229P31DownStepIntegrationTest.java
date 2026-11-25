// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), residual R5 (nexus-p2m1x.44): the operator down-step, {@code db/rdr229/p31-downstep.sql},
 * generated from {@code db/rdr229/p31-checks.tsv} by {@code scripts/rdr229_p31_downstep.py} and run by conexus before a
 * pre-P3.1 engine starts (the cloud revert is relay-mediated and conexus-owned; a local install has no rollback).
 *
 * <p>Run against a real PostgreSQL walked by the product changelog, through {@code db.changelog-test-p31-downstep.xml} (the
 * operator's own file, no SQL text here):
 * <ul>
 *   <li>the three write shapes a pre-P3.1 engine makes (a chunk INSERT with the collection set and collection_id NULL, a
 *       supersede UPDATE with superseded_by_id NULL, a carrier INSERT) fail with 23514 before, succeed after;</li>
 *   <li>no listed constraint remains on ANY relation afterwards: the partitioned {@code chunks} parent and its two-level
 *       leaves included, NOT VALID and validated alike;</li>
 *   <li>the id columns and the identity column stay (an old engine ignores them) and {@code nexus.data_moves} is not
 *       touched; a second run changes nothing; a store that kept the queue table has its CHECK dropped, and one without it
 *       is not an error.</li>
 * </ul>
 */
class Rdr229P31DownStepIntegrationTest {

    private static final Path LIST = Path.of("src", "main", "resources", "db", "rdr229", "p31-checks.tsv");
    private static final String DOWN_STEP = "db/changelog-test/db.changelog-test-p31-downstep.xml";
    private static final String TENANT = "p31-down";
    private static final String COLLECTION = "code__p31-down__voyage-code-3__v1";
    private static final String OTHER = "code__p31-down-b__voyage-code-3__v1";
    private static final String QUEUE = "aspect_extraction_queue";

    private PostgreSQLContainer<?> pg;
    private Connection su;
    private DSLContext ctx;

    record Row(String table, String idColumn, String constraint) { }

    private static List<Row> checks() throws IOException {
        List<Row> rows = new ArrayList<>();
        for (String line : Files.readAllLines(LIST)) {
            if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) continue;
            String[] f = line.split("\t");
            if (!f[3].equals("-")) rows.add(new Row(f[0], f[2], f[3]));
        }
        return rows;
    }

    /** One database per case (a clone of the migrated template, so cheap): each case needs the P3.1 constraints in place. */
    @BeforeEach
    void start() throws Exception {
        pg = PgContainerHelper.start();
        su = pg.createConnection("");
        su.setAutoCommit(true);
        ctx = DSL.using(su, SQLDialect.POSTGRES);
    }

    @AfterEach
    void stop() throws Exception {
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    private void runDownStep() throws Exception {
        PgContainerHelper.runSuperuserTestChangelog(su, DOWN_STEP, "databasechangelog_test_p31_downstep", Map.of());
    }

    /** Every pg_constraint row of this name on any relation: the parent, and a partition leaf's copy. */
    private int constraintRows(String name) {
        return ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_constraint")),
            DSL.field(DSL.name("conname"), String.class).eq(name));
    }

    private static String sqlState(Action a) {
        try {
            a.run();
        } catch (Exception e) {
            for (Throwable t = e; t != null; t = t.getCause()) {
                if (t instanceof SQLException se && se.getSQLState() != null) return se.getSQLState() + ": " + se.getMessage();
            }
            throw new AssertionError("failed without a SQLSTATE: " + e, e);
        }
        return "ok";
    }

    private interface Action {
        void run() throws Exception;
    }

    private static byte[] hex(String h) {
        return dev.nexus.service.db.Chash.fromHex(h).toBytes();
    }

    @Test
    void thePreP31WriteShapesFailWith23514BeforeTheDownStep_andSucceedAfterIt() throws Exception {
        PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
        PgContainerHelper.insertCollection(ctx, TENANT, OTHER);
        var chunks = dev.nexus.service.jooq.nexus.Tables.CHUNKS;
        var registry = DSL.table(DSL.name("nexus", "catalog_collections"));
        var tenant = DSL.field(DSL.name("tenant_id"), String.class);
        var name = DSL.field(DSL.name("name"), String.class);
        var supersededBy = DSL.field(DSL.name("superseded_by"), String.class);
        var topics = DSL.table(DSL.name("nexus", "topics"));

        Action chunkInsert = () -> ctx.insertInto(chunks)
            .set(chunks.TENANT_ID, TENANT).set(chunks.COLLECTION, COLLECTION).set(chunks.CHASH, hex("44".repeat(32)))
            .set(chunks.EMBEDDING_MODEL, PgContainerHelper.collectionModel(ctx, TENANT, COLLECTION))
            .set(chunks.CHUNK_TEXT, "text").set(chunks.EMBEDDING_1024, Vector.of(new float[1024])).execute();
        Action supersede = () -> ctx.update(registry).set(supersededBy, OTHER)
            .where(tenant.eq(TENANT).and(name.eq(COLLECTION))).execute();
        Action carrier = () -> ctx.insertInto(topics).set(tenant, TENANT)
            .set(DSL.field(DSL.name("label"), String.class), "l").set(DSL.field(DSL.name("collection"), String.class), OTHER)
            .set(DSL.field(DSL.name("created_at"), OffsetDateTime.class), OffsetDateTime.now()).execute();

        assertThat(sqlState(chunkInsert)).as("chunk INSERT, collection set, collection_id NULL").startsWith("23514")
            .contains("chunks_collection_id_check");
        assertThat(sqlState(supersede)).as("supersede UPDATE, superseded_by_id NULL").startsWith("23514")
            .contains("catalog_collections_superseded_by_id_check");
        assertThat(sqlState(carrier)).as("carrier INSERT, id NULL").startsWith("23514").contains("topics_collection_id_check");

        runDownStep();

        assertThat(sqlState(chunkInsert)).as("after the down-step the old engine's chunk INSERT passes").isEqualTo("ok");
        assertThat(sqlState(supersede)).as("and its supersede UPDATE").isEqualTo("ok");
        assertThat(sqlState(carrier)).as("and its carrier INSERT").isEqualTo("ok");
    }

    @Test
    void noListedConstraintRemainsOnAnyRelation_chunksLeavesAndNotValidAndValidatedAlike() throws Exception {
        PgContainerHelper.insertCollection(ctx, TENANT + "-leaf", "code__p31-down-leaf__voyage-code-3__v1");   // creates leaves
        List<Row> rows = checks();
        assertThat(rows).as("non-vacuity: 20 listed CHECKs").hasSize(20);

        // Validated and NOT VALID both: validate a few before the run (an empty table validates at once).
        PgContainerHelper.validateConstraint(su, DSL.table(DSL.name("nexus", "document_highlights")), "document_highlights_collection_id_check");
        PgContainerHelper.validateConstraint(su, DSL.table(DSL.name("nexus", "chunks")), "chunks_collection_id_not_null");

        List<String> present = new ArrayList<>();
        int onLeaves = 0;
        for (Row r : rows) {
            int n = constraintRows(r.constraint());
            if (n > 0) present.add(r.constraint());
            if (r.table().equals("chunks") && n > 1) onLeaves++;
        }
        assertThat(present).as("non-vacuity: every listed constraint of a table that exists is there before: 19 (the queue is absent)")
            .hasSize(19);
        assertThat(onLeaves).as("non-vacuity: the chunks CHECKs are also on the leaves (the parent's drop recurses)").isEqualTo(2);

        runDownStep();

        for (Row r : rows) {
            assertThat(constraintRows(r.constraint())).as("%s on any relation after the down-step", r.constraint()).isZero();
        }
    }

    @Test
    void theIdColumnsAndTheIdentityColumnStay_theRegistryOfMovesIsNotTouched_andASecondRunChangesNothing() throws Exception {
        Table<?> moves = DSL.table(DSL.name("nexus", "data_moves"));
        // the walk itself registered the stamp moves (vectors-070): the registry the down-step must leave alone
        List<String> before = ctx.select(DSL.field(DSL.name("xmin")).cast(org.jooq.impl.SQLDataType.VARCHAR),
                DSL.field(DSL.name("label"), String.class)).from(moves).fetch(r -> r.value1() + ":" + r.value2());
        // the registry also holds develop's moves (the walk registers them too); the stamp moves are the ones this cut owns:
        // one per table that carries a CHECK, the queue's only while its table exists
        int stampMoves = PgCatalogProbes.tableExists(ctx, "nexus", QUEUE) ? 16 : 15;
        assertThat(before.stream().filter(row -> row.contains(":rdr229-stamp-")).count())
            .as("non-vacuity: the walk registered exactly the %d stamp moves this schema carries", stampMoves)
            .isEqualTo(stampMoves);

        runDownStep();
        assertThatCode(this::runDownStep).as("a second run: IF EXISTS everywhere").doesNotThrowAnyException();

        assertThat(ctx.select(DSL.field(DSL.name("xmin")).cast(org.jooq.impl.SQLDataType.VARCHAR),
                DSL.field(DSL.name("label"), String.class)).from(moves).fetch(r -> r.value1() + ":" + r.value2()))
            .as("the registry of moves is untouched: within the window no stamp has completed, nothing has to re-attach")
            .containsExactlyInAnyOrderElementsOf(before);
        for (Row r : checks()) {
            if (r.table().equals(QUEUE)) continue;
            assertThat(PgCatalogProbes.columnExists(ctx, "nexus", r.table(), r.idColumn()))
                .as("%s.%s stays: an old engine ignores a column it does not know", r.table(), r.idColumn()).isTrue();
        }
        assertThat(PgCatalogProbes.columnExists(ctx, "nexus", "catalog_collections", "collection_id")).isTrue();
        ctx.deleteFrom(moves).execute();
    }

    @Test
    void aStoreThatKeptTheQueueTableHasItsCheckDropped_andOneWithoutItIsNotAnError() throws Exception {
        assertThat(PgCatalogProbes.tableExists(ctx, "nexus", QUEUE)).isFalse();
        assertThatCode(this::runDownStep).as("the queue table is absent: IF EXISTS on the table").doesNotThrowAnyException();

        RetiredAspectQueueTable.restore(ctx);
        try {
            PgContainerHelper.replayChangeset(su, P31CheckReplay.CHANGELOG, "vectors-068-18");
            assertThat(constraintRows("aspect_extraction_queue_collection_id_check")).as("non-vacuity").isEqualTo(1);
            runDownStep();
            assertThat(constraintRows("aspect_extraction_queue_collection_id_check")).isZero();
        } finally {
            RetiredAspectQueueTable.drop(ctx);
        }
    }
}
