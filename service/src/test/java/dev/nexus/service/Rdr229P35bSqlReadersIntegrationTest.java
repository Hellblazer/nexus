// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.Routines;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24), "Readers during the stamp", item 6, the SQL readers. Test-first: the assertions marked
 * RED fail on the tree before P3.5b and name the behaviour that must change; the half-stamped and incomplete-stamp
 * guards pass today and must keep passing.
 *
 * <p>The rule under test. A form B reader (its body names a chunk-family table: {@code nexus.live_chunks},
 * {@code nexus.collection_vector_stats}) is redefined in a plain changeset and matches a chunk to its manifest row by
 * {@code collection_id} where BOTH sides carry one, and by name where either lacks one; P4.1 removes the name arm. A
 * NULL id is no collection: it never falls back to a '' name (linda 2026-11-20). A form A reader (no chunk-family table:
 * the catalog-044 trigger and its owner functions, {@code collection_doc_counts}, {@code collection_health_meta},
 * {@code topics_with_counts}) is redefined by id behind a CONTINUE gate on every {@code rdr229-stamp-*} move's
 * {@code completed_at}, so it waits while any stamp is incomplete.
 *
 * <p>Fixture. One database, walked to just before vectors-068-1, where the rows are seeded unstamped with the shared seeds
 * (a NOT VALID CHECK still applies to every row written after it, so an unstamped chunk or a NULL id beside a '' name can only be
 * seeded before the expand). Then the full walk, and the stamp done by hand the way the move does it: the id of the registry row of
 * the same tenant and name. The form A changesets are not known by id here, so the tests assert behaviour: the trigger's definition
 * and the owner it derives.
 *
 * <p>The complete state. The form A gate is the completion check's own rule for a stamp label ({@link CompletionCheck}: the move is
 * complete, and its table's CHECKs are present and validated), so a store is complete only once {@link #completeTheStamps} has
 * stamped every row the stamp moves would reach, validated every CHECK as their {@code on_complete} does, and set
 * {@code completed_at}. Setting {@code completed_at} alone leaves the store half stamped, which the completion check does not count
 * complete, and the gate must not either; the test that pins that leaves an orphan row behind.
 *
 * <p>Diverging name columns. Between a chunk and its manifest row the name columns can differ only once the name key between
 * them ({@code fk_catalog_chunks_chunk}, a foreign key on the name columns) is gone. That key stays until P4.1 (P3.7's name-key
 * drop takes only the keys to the registry, not the child keys between chunk and manifest), so the fixture drops it itself and
 * rewrites the manifest row's name: the shape an id-only writer leaves once P4.1 has run, with the registry's id on both sides.
 * The form B views must already read that shape correctly, since P4.1 removes their name arm and nothing else.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class Rdr229P35bSqlReadersIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_p35b_sql";
    private static final String ADMIN_PASS = "nexus_admin_p35b_sql_pass";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String FIRST = "vectors-068-1";
    private static final String TENANT = "p35b-sql";

    /** Half stamped: four chunks, each with its manifest row. */
    private static final String HALF = "p35b-half";
    /** Both sides stamped with the registry id, the manifest row's name rewritten. */
    private static final String DIVERGED = "p35b-diverged";
    /** The name the manifest row of {@link #DIVERGED} takes. */
    private static final String DIVERGED_MANIFEST_NAME = "p35b-diverged-renamed";
    /** A '' name and a NULL id on both sides: the registry row named '' exists only to satisfy the chunk's name key. */
    private static final String BLANK = "";
    /** A code collection whose owner a document derives (catalog-044); conformant, so the seeds register it as 'code'. */
    private static final String OWNED = "code__p35bowner__bge-base-en-v15-768__v1";
    private static final String STALE_NAME = "p35b-stale-name-before-rename";
    private static final String TRIGGER = "catalog_documents_owner_to_collection";
    /** A document naming a collection that has no registry row: an orphan the catalog_documents stamp cannot give an id. */
    private static final String GHOST = "p35b-ghost-collection";
    private static final Table<?> JOURNAL = DSL.table(DSL.name("public", "databasechangelog"));
    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final Field<String> MOVE_LABEL = DSL.field(DSL.name("label"), String.class);

    private static final List<String> HALF_CHASHES = labels("half-1", "half-2", "half-3", "half-4");
    private static final List<String> DIVERGED_CHASHES = labels("div-1");
    private static final List<String> BLANK_CHASHES = labels("blank-1");

    private PostgreSQLContainer<?> pg;
    private HikariDataSource ds;
    private boolean stampsCompleted;

    @BeforeAll
    void seedWalkAndStampHalf() throws Exception {
        pg = PgContainerHelper.startEmptyDatabase();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-admin-p35b-sql");
        ds = new HikariDataSource(cfg);

        migrateUpTo(ds, FIRST);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String collection : List.of(HALF, DIVERGED, BLANK, OWNED)) {
                PgContainerHelper.insertCollection(ctx, TENANT, collection);
            }
            seedOwnedChunks(ctx, HALF, HALF_CHASHES);
            seedOwnedChunks(ctx, DIVERGED, DIVERGED_CHASHES);
            seedOwnedChunks(ctx, BLANK, BLANK_CHASHES);
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT)))
                .as("non-vacuity: the pre-expand seed wrote every chunk").isEqualTo(6);
        }

        SchemaMigrator.migrate(ds);

        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(PgCatalogProbes.columnExists(ctx, "nexus", "chunks", "collection_id"))
                .as("non-vacuity: the walk reached the expand").isTrue();
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION_ID.isNotNull())))
                .as("non-vacuity: the seeded rows are unstamped after the expand (the expand rewrites none)").isZero();

            // Half stamped: h1 and h2 on both sides, h3 on the chunk only, h4 on neither.
            long halfId = registryId(ctx, HALF);
            stampChunks(ctx, HALF, halfId, HALF_CHASHES.subList(0, 3));
            stampManifest(ctx, HALF, halfId, HALF_CHASHES.subList(0, 2));

            // Both sides stamped, then the name key between them dropped (P3.7) and the manifest row's name rewritten.
            long divergedId = registryId(ctx, DIVERGED);
            stampChunks(ctx, DIVERGED, divergedId, DIVERGED_CHASHES);
            stampManifest(ctx, DIVERGED, divergedId, DIVERGED_CHASHES);
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists(DSL.name("fk_catalog_chunks_chunk")).execute();
            // P3.7's vectors-079 trigger stamps a manifest row's name from its chunk until P4.1 drops the name columns, so the diverged
            // name is the state after P4.1, where neither the key nor the trigger exists: the rewrite runs with triggers off
            // (session_replication_role = replica, this session only; the key is already dropped).
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica"));
            ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, DIVERGED_MANIFEST_NAME)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(DIVERGED))
                .execute();
            ctx.set(DSL.name("session_replication_role"), DSL.inline("origin"));
            assertThat(ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)
                    .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(DIVERGED_MANIFEST_NAME))
                    .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.eq(divergedId))))
                .as("non-vacuity: the manifest row carries the registry id under a different name than its chunk").isEqualTo(1);
        }
    }

    @AfterAll
    void stop() {
        if (ds != null) {
            ds.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    // ---- T1: the form B views on a half-stamped store ----------------------------------------------------------------

    @Test
    @Order(1)
    void halfStamped_everyChunkIsListedAndCounted() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(liveChashes(ctx, HALF)).as("live_chunks lists all four chunks while two are stamped on both sides")
                .containsExactlyInAnyOrderElementsOf(HALF_CHASHES);
            assertThat(viewCount(ctx, "chunk_count", HALF)).as("collection_vector_stats counts all four live").isEqualTo(4L);
            assertThat(viewCount(ctx, "stored_count", HALF)).as("and stores four").isEqualTo(4L);
            // RDR-229 P3.5b (linda 2026-11-23): the view's label is the registry's where the row carries an id and the row's own
            // name where it does not, and it groups by that label, so two stamped chunks and an unstamped one are still one row
            org.jooq.Condition ofHalf = DSL.field(DSL.name("tenant_id"), String.class).eq(TENANT)
                .and(DSL.field(DSL.name("collection"), String.class).eq(HALF));
            assertThat(ctx.fetchCount(VECTOR_STATS, ofHalf)).as("a half-stamped collection is one row of the stats view").isEqualTo(1);
            assertThat(ctx.select(DSL.field(DSL.name("collection_id"), Long.class)).from(VECTOR_STATS).where(ofHalf)
                    .fetchOne(0, Long.class))
                .as("and its trailing collection_id is the registry's").isEqualTo(registryId(ctx, HALF));
        }
    }

    /** RED: the views join a chunk to its manifest row by name, so two rows of one collection id that differ in name are two collections. */
    @Test
    @Order(2)
    void sameIdOnBothSides_divergingNames_isLiveAndCounted() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(liveChashes(ctx, DIVERGED))
                .as("live_chunks must match a chunk to its manifest row by collection id when both carry one: chunk named '%s', manifest row named '%s', one id",
                    DIVERGED, DIVERGED_MANIFEST_NAME)
                .containsExactlyInAnyOrderElementsOf(DIVERGED_CHASHES);
            assertThat(viewCount(ctx, "chunk_count", DIVERGED))
                .as("collection_vector_stats must count the chunk live by collection id, whatever the manifest row's name column says")
                .isEqualTo(1L);
        }
    }

    /** RED: '' is unset and a NULL id is no collection, but the name join matches '' to ''. */
    @Test
    @Order(3)
    void emptyNameAndNullId_onBothSides_isNotLive_andNotCounted() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // non-vacuity: the chunk and its manifest row are there, both with the '' name and a NULL id
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION.eq(BLANK))
                .and(CHUNKS.COLLECTION_ID.isNull()))).isEqualTo(1);
            assertThat(ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)
                .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(BLANK)).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.isNull()))).isEqualTo(1);
            assertThat(liveChashes(ctx, BLANK))
                .as("a NULL collection id is no collection: live_chunks must not fall back to a '' name match")
                .isEmpty();
            assertThat(viewCount(ctx, "chunk_count", BLANK))
                .as("collection_vector_stats must not count a chunk live through a '' name match on a NULL id")
                .isZero();
            assertThat(viewCount(ctx, "stored_count", BLANK)).as("the chunk is still stored: inventory is not liveness").isEqualTo(1L);
            assertThat(ctx.select(DSL.field(DSL.name("collection_id"), Long.class)).from(VECTOR_STATS)
                    .where(DSL.field(DSL.name("tenant_id"), String.class).eq(TENANT))
                    .and(DSL.field(DSL.name("collection"), String.class).eq(BLANK)).fetchOne(0, Long.class))
                .as("a row with no id keeps its own name and shows no id").isNull();
        }
    }

    /**
     * linda 2026-11-23 (the confirmation round): a sensitivity miss is the one wrong answer of a form B reader that leaks, so
     * chunk_sensitive_many is pinned on the half-stamped collection: h1 and h2 match by the id arm, h3 and h4 by the name arm
     * (their manifest rows carry no id). Each one's owner is made sensitive here and put back after (its id stays: the CHECK
     * holds every row written after the expand to one).
     */
    @Test
    @Order(3)
    void halfStamped_chunkSensitiveMany_seesEverySensitiveOwner_byTheIdArmAndTheNameArm() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var ownersOfHalf = DSL.select(CATALOG_DOCUMENT_CHUNKS.DOC_ID).from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(HALF));
            // a row written after the expand's NOT VALID CHECK must carry its id, so the owners are stamped as they are marked
            int marked = ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SENSITIVE, true)
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, registryId(ctx, HALF))
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.TUMBLER.in(ownersOfHalf)).execute();
            try {
                assertThat(marked).as("non-vacuity: the half-stamped collection's owners were marked sensitive").isPositive();
                assertThat(ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)
                        .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(HALF)).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.isNull())))
                    .as("non-vacuity: two of the four manifest rows carry no id, so the name arm is exercised").isEqualTo(2);
                assertThat(dev.nexus.service.vectors.ChunkSensitivity.sensitiveChashes(ctx, TENANT, HALF, HALF_CHASHES))
                    .as("every chunk with a live sensitive owner reads sensitive, whichever arm matches its manifest row")
                    .containsExactlyInAnyOrderElementsOf(HALF_CHASHES);
            } finally {
                ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SENSITIVE, false)
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.TUMBLER.in(ownersOfHalf)).execute();
            }
        }
    }

    // ---- T2: form A is CONTINUE-gated on the stamp moves ---------------------------------------------------------------

    /** Guard (passes today): with a stamp move incomplete the form A objects stay on the name. */
    @Test
    @Order(4)
    void stampsIncomplete_formAObjectsStayOnTheName() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(incompleteStampMoves(ctx)).as("non-vacuity: the walk registered stamp moves and none is complete").isGreaterThan(0);
        }
        SchemaMigrator.migrate(ds); // another boot with the stamps still incomplete: the gated changesets wait again
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String def = triggerDefinition(ctx);
            assertThat(def).as("the owner trigger still fires on the name column while a stamp is incomplete")
                .containsPattern(Pattern.compile("UPDATE OF [^O]*\\bphysical_collection\\b"));
            assertThat(def).as("and not on the id").doesNotContain("physical_collection_id");
            assertThat(PgCatalogProbes.viewDefinition(ctx, "nexus", "collection_doc_counts")).doesNotContain("physical_collection_id");
            assertThat(PgCatalogProbes.viewDefinition(ctx, "nexus", "collection_health_meta")).doesNotContain("physical_collection_id");
            assertThat(PgCatalogProbes.viewDefinition(ctx, "nexus", "topics_with_counts")).doesNotContain("collection_id");
        }
    }

    /**
     * Every {@code completed_at} is set, but one stamp CHECK is still NOT VALID because an orphan row keeps it from validating: the
     * completion check counts that table's label incomplete, so the form A gate must hold and all twelve changesets stay unrecorded. A
     * gate that read {@code completed_at} alone applied them here (the review's ship-blocker). A stamp label with no row is the same:
     * the gate waits for the row. Once the orphan is gone and the CHECK validated, and the row is back, the twelve are still unapplied
     * only until the next migrate, which is the test after this one.
     */
    @Test
    @Order(5)
    void everyCompletedAtSet_butACheckStaysNotValid_orAStampLabelHasNoRow_allTwelveStayUnapplied() throws Exception {
        List<String> twelve = Rdr229CompleteStamps.continueList();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // an orphan: a document that names a collection with no registry row, written while its CHECK is lifted
            P31CheckReplay.lift(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
            try {
                ctx.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, "1.9.1")
                    .set(CATALOG_DOCUMENTS.TITLE, "p35b orphan").set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, GHOST)
                    .execute();
            } finally {
                P31CheckReplay.restore(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
            }
            removeBlankRows(ctx);
            Rdr229CompleteStamps.stampEveryRow(ctx);
            Rdr229CompleteStamps.setEveryStampCompletedAt(ctx);
            Rdr229CompleteStamps.validateEveryCheck(su, ctx, "catalog_documents_physical_collection_id_check");
            assertThat(incompleteStampMoves(ctx)).as("non-vacuity: every stamp move has completed_at").isZero();
            var states = stampLabelStates(ctx);
            assertThat(states).as("non-vacuity: the completion check reads the stamp labels").hasSizeGreaterThan(10);
            assertThat(states.stream().filter(s -> !s.satisfied()).map(CompletionCheck.LabelState::label).toList())
                .as("the completion check counts exactly the catalog_documents stamp incomplete (its CHECK cannot validate over the orphan)")
                .containsExactly("rdr229-stamp-catalog_documents");
        }
        SchemaMigrator.migrate(ds);
        assertThat(recordedOf(twelve)).as("a CHECK stays NOT VALID: the gate holds and none of the twelve is recorded").isEmpty();
        try (Connection su = pg.createConnection("")) {
            assertThat(triggerDefinition(DSL.using(su, SQLDialect.POSTGRES))).as("and the trigger is still on the name")
                .doesNotContain("physical_collection_id");
        }

        // the orphan goes and its CHECK validates: now one label has no row
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.deleteFrom(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT))
                .and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(GHOST)).execute();
            Rdr229CompleteStamps.validateEveryCheck(su, ctx);
            assertThat(stampLabelStates(ctx).stream().filter(s -> !s.satisfied()).toList())
                .as("non-vacuity: with the orphan gone every stamp label is complete").isEmpty();
            // the move row of one label goes (renamed, so it comes back as it was): the label has no row
            assertThat(ctx.update(MOVES).set(MOVE_LABEL, "held-p35b-taxonomy_meta").where(MOVE_LABEL.eq("rdr229-stamp-taxonomy_meta")).execute())
                .as("non-vacuity: the move row is there to take away").isEqualTo(1);
            assertThat(stampLabelStates(ctx).stream().filter(s -> !s.satisfied()).map(CompletionCheck.LabelState::label).toList())
                .as("the completion check counts a label with no row incomplete").containsExactly("rdr229-stamp-taxonomy_meta");
            SchemaMigrator.migrate(ds);
            assertThat(recordedOf(twelve)).as("a stamp label has no row: the gate holds and none of the twelve is recorded").isEmpty();
            ctx.update(MOVES).set(MOVE_LABEL, "rdr229-stamp-taxonomy_meta").where(MOVE_LABEL.eq("held-p35b-taxonomy_meta")).execute();
            assertThat(stampLabelStates(ctx).stream().filter(s -> !s.satisfied()).toList())
                .as("the row is back: every stamp label is complete again").isEmpty();
        }
    }

    /** RED: once every {@code rdr229-stamp-*} move is complete and the next boot has run, the trigger fires on the id column. */
    @Test
    @Order(6)
    void stampsComplete_nextBoot_ownerTriggerFiresOnTheIdColumn() throws Exception {
        completeStampsAndMigrate();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String def = triggerDefinition(ctx);
            assertThat(def).as("after the stamps complete and a re-migrate the trigger fires on UPDATE OF physical_collection_id")
                .containsPattern(Pattern.compile("UPDATE OF [^O]*\\bphysical_collection_id\\b"));
            assertThat(def).as("and no longer on the name column")
                .doesNotContainPattern(Pattern.compile("UPDATE OF [^O]*\\bphysical_collection\\b"));
        }
    }

    /**
     * RED: a document whose {@code physical_collection} is a stale name (the registry row has been renamed) but whose
     * {@code physical_collection_id} is the registry's. The owner derivation reads the registry row and the documents by id.
     */
    @Test
    @Order(7)
    void stampsComplete_ownerIsDerivedById_forADocumentWithAStaleName() throws Exception {
        completeStampsAndMigrate();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            long ownedId = registryId(ctx, OWNED);
            assertThat(ownerOf(ctx, OWNED)).as("non-vacuity: the slug owner is replaceable and no document derived one yet")
                .isEqualTo("p35bowner");
            ctx.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, "1.15.42")
                .set(CATALOG_DOCUMENTS.TITLE, "p35b stale-name document")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, STALE_NAME)
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, ownedId)
                .execute();
            assertThat(ownerOf(ctx, OWNED))
                .as("the document names collection id %d under a stale name; the owner derives from the registry row of that id, so the slug becomes 1-15", ownedId)
                .isEqualTo("1-15");
        }
    }

    // ---- T3: P4.1's drop of the name columns -----------------------------------------------------------------------------

    /**
     * RED: P4.1 drops the name columns with RESTRICT (linda 2026-11-22), so any object still depending on one makes that drop fail.
     * A form A object must therefore hold no dependency on a name column once its changeset has applied. Read from pg_depend, the
     * record RESTRICT itself consults: a view's rewrite rule and a trigger's UPDATE OF list each depend on the columns they name.
     * A literal RESTRICT drop cannot stand in for this here, because before P4.1 the name columns still carry their keys, indexes
     * and the form B views, which P4.1 removes or redefines first; live_chunks keeps its name arm until then, and the test uses it
     * to show the probe sees a dependency.
     */
    @Test
    @Order(8)
    void p41NameColumnDrop_noFormAObjectDependsOnANameColumn() throws Exception {
        completeStampsAndMigrate();
        List<String> formAViews = List.of("collection_health_meta", "collection_doc_counts", "topics_with_counts");
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<String[]> columns = new ArrayList<>();
            for (String[] tc : nameColumns()) {
                if (PgCatalogProbes.columnExists(ctx, "nexus", tc[0], tc[1])) { // aspect_extraction_queue is absent on most installs
                    columns.add(tc);
                }
            }
            assertThat(columns).as("non-vacuity: the name columns of the list exist").hasSizeGreaterThan(10);
            for (String view : formAViews) {
                assertThat(PgCatalogProbes.viewExists(ctx, "nexus", view)).as("non-vacuity: %s exists", view).isTrue();
            }
            assertThat(nameColumnDependents(ctx, columns))
                .as("non-vacuity: the probe sees the form B view's name arm, which P4.1 redefines before its drop")
                .contains("view nexus.live_chunks");
            List<String> formA = new ArrayList<>();
            for (String dependent : nameColumnDependents(ctx, columns)) {
                if (formAViews.stream().anyMatch(v -> dependent.equals("view nexus." + v))
                    || dependent.equals("trigger nexus.catalog_documents." + TRIGGER)) {
                    formA.add(dependent);
                }
            }
            assertThat(formA)
                .as("these form A objects still depend on a name column, so P4.1's RESTRICT drop would fail on them: they must read the id")
                .isEmpty();
        }
    }

    // ---- T4: canary_query_ring_upsert matches by id and stays one row under a race ---------------------------------------

    /**
     * Form A's canary_query_ring_upsert (vectors-078-8) no longer arbitrates on the name key: it UPDATEs the ring row by
     * collection_id and INSERTs only when none exists, refreshing on a unique_violation. Two writers race on one digest: A
     * inserts and holds its transaction open, B's INSERT waits on A's row, A commits, B takes the unique_violation and refreshes.
     * Both report one entry written and the ring holds one row for the digest, carrying the registry's id (linda 2026-11-22).
     */
    @Test
    @Order(9)
    void stampsComplete_canaryUpsert_twoWritersOneDigest_endInOneRow() throws Exception {
        completeStampsAndMigrate();
        String model;
        long halfId;
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            model = ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(HALF))
                .fetchOne(CATALOG_COLLECTIONS.EMBEDDING_MODEL);
            halfId = registryId(ctx, HALF);
        }
        String vector = "[" + String.join(",", java.util.Collections.nCopies(384, "0.5")) + "]";
        byte[] digest = MessageDigest.getInstance("SHA-256").digest("p35b-canary-race".getBytes(StandardCharsets.UTF_8));
        try (Connection a = pg.createConnection(""); Connection b = pg.createConnection("")) {
            a.setAutoCommit(false);
            b.setAutoCommit(false);
            int wroteA = upsertCanary(a, model, vector, digest);
            var bResult = java.util.concurrent.CompletableFuture.supplyAsync(() -> {
                try {
                    int n = upsertCanary(b, model, vector, digest);
                    b.commit();
                    return n;
                } catch (Exception e) {
                    throw new java.util.concurrent.CompletionException(e);
                }
            });
            // non-vacuity: B is waiting on A's uncommitted ring row, so the race reaches the INSERT
            Thread.sleep(500);
            assertThat(bResult).as("B's INSERT waits on A's uncommitted row with the same digest").isNotDone();
            a.commit();
            int wroteB = bResult.get(30, java.util.concurrent.TimeUnit.SECONDS);
            assertThat(wroteA).as("A wrote its entry").isEqualTo(1);
            assertThat(wroteB).as("B's unique_violation is caught and refreshes the row, as ON CONFLICT DO UPDATE did").isEqualTo(1);
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var ring = DSL.table(DSL.name("nexus", "canary_query_ring"));
            var cond = DSL.field(DSL.name("tenant_id"), String.class).eq(TENANT)
                .and(DSL.field(DSL.name("vector_digest"), byte[].class).eq(digest));
            assertThat(ctx.fetchCount(ring, cond)).as("two writers, one digest: one ring row").isEqualTo(1);
            assertThat(ctx.fetchCount(ring, cond.and(DSL.field(DSL.name("collection_id"), Long.class).eq(halfId))))
                .as("and it carries the registry's id").isEqualTo(1);
        }
    }

    /**
     * The row of the name key already holds the entry under another collection id (a stale stamp): the UPDATE by id finds nothing and
     * the INSERT meets the name primary key. The entry must not count as written, and the row is left as it is: restamping it would read
     * the name column (which a form A reader no longer does), and once P3.7 renames a collection the name key's row may belong to the
     * collection that label was renamed away from. The function says so with a WARNING. A handler that refreshed by id again counted the
     * entry written (vectors-078-8 before this round) with nothing written.
     */
    @Test
    @Order(10)
    void stampsComplete_canaryUpsert_aNameKeyRowOnAnotherId_isNotCounted_andNotTouched() throws Exception {
        completeStampsAndMigrate();
        String vector = "[" + String.join(",", java.util.Collections.nCopies(384, "0.25")) + "]";
        byte[] digest = MessageDigest.getInstance("SHA-256").digest("p35b-canary-other-id".getBytes(StandardCharsets.UTF_8));
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String model = ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(HALF))
                .fetchOne(CATALOG_COLLECTIONS.EMBEDDING_MODEL);
            long otherId = registryId(ctx, DIVERGED);
            assertThat(ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
                    .and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(otherId)).and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq(model))))
                .as("non-vacuity: the other registry row has the ring row's model, so the id twin foreign key accepts it").isEqualTo(1);
            assertThat(upsertCanary(su, model, vector, digest)).as("the entry is written").isEqualTo(1);
            var ring = DSL.table(DSL.name("nexus", "canary_query_ring"));
            var ringId = DSL.field(DSL.name("collection_id"), Long.class);
            var mine = DSL.field(DSL.name("tenant_id"), String.class).eq(TENANT)
                .and(DSL.field(DSL.name("vector_digest"), byte[].class).eq(digest));
            ctx.update(ring).set(ringId, otherId).where(mine).execute();
            assertThat(ctx.fetchCount(ring, mine.and(ringId.eq(otherId)))).as("non-vacuity: the name key row carries another id").isEqualTo(1);

            List<String> warnings = new ArrayList<>();
            assertThat(upsertCanary(su, model, vector, digest, warnings)).as("the entry is not written, so not counted").isEqualTo(0);
            assertThat(warnings).as("and it says so").anyMatch(w -> w.contains("was not written") && w.contains("another or no collection id"));
            assertThat(ctx.fetchCount(ring, mine)).as("one ring row for the digest").isEqualTo(1);
            assertThat(ctx.fetchCount(ring, mine.and(ringId.eq(otherId)))).as("its id is untouched").isEqualTo(1);
        }
    }

    /** As above where the row of the name key has no collection id at all (its CHECK lifted to write it, then restored NOT VALID). */
    @Test
    @Order(11)
    void stampsComplete_canaryUpsert_aNameKeyRowWithNoId_isNotCounted_andNotTouched() throws Exception {
        completeStampsAndMigrate();
        String vector = "[" + String.join(",", java.util.Collections.nCopies(384, "0.75")) + "]";
        byte[] digest = MessageDigest.getInstance("SHA-256").digest("p35b-canary-null-id".getBytes(StandardCharsets.UTF_8));
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String model = ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(HALF))
                .fetchOne(CATALOG_COLLECTIONS.EMBEDDING_MODEL);
            assertThat(upsertCanary(su, model, vector, digest)).as("the entry is written").isEqualTo(1);
            var ring = DSL.table(DSL.name("nexus", "canary_query_ring"));
            var ringId = DSL.field(DSL.name("collection_id"), Long.class);
            var mine = DSL.field(DSL.name("tenant_id"), String.class).eq(TENANT)
                .and(DSL.field(DSL.name("vector_digest"), byte[].class).eq(digest));
            P31CheckReplay.lift(ctx, "vectors-068-12"); // canary_query_ring_collection_id_check
            try {
                ctx.update(ring).set(ringId, (Long) null).where(mine).execute();
            } finally {
                P31CheckReplay.restore(ctx, "vectors-068-12");
            }
            assertThat(ctx.fetchCount(ring, mine.and(ringId.isNull()))).as("non-vacuity: the name key row has no id").isEqualTo(1);

            List<String> warnings = new ArrayList<>();
            assertThat(upsertCanary(su, model, vector, digest, warnings)).as("the entry is not written, so not counted").isEqualTo(0);
            assertThat(warnings).as("and it says so").anyMatch(w -> w.contains("was not written"));
            assertThat(ctx.fetchCount(ring, mine)).as("one ring row for the digest").isEqualTo(1);
            assertThat(ctx.fetchCount(ring, mine.and(ringId.isNull()))).as("its id is untouched").isEqualTo(1);
            ctx.deleteFrom(ring).where(mine).execute();
            PgContainerHelper.validateConstraintStandalone(su, ring, "canary_query_ring_collection_id_check");
        }
    }

    // ---- T5: the registry INSERT branch of the owner trigger (vectors-078-1, -2) ---------------------------------------------

    /**
     * {@code catalog_collection_doc_owner} finds documents through the registry row of the label it is given. On a registry INSERT the
     * BEFORE trigger hands it the new row's name before the row exists, and a document can carry only an id some registry row already
     * has, so a label that no registry row holds derives no owner from documents: the INSERT branch is dead for a fresh label, by design
     * (the by-name reader used to find documents that still carried the label under another id). The next test covers the one other way in.
     */
    @Test
    @Order(12)
    void stampsComplete_registryInsert_ofAFreshLabel_derivesNoOwnerFromDocuments() throws Exception {
        completeStampsAndMigrate();
        String holder = "code__p35bholder__bge-base-en-v15-768__v1";
        String stale = "code__p35bstale__bge-base-en-v15-768__v1";
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, holder);
            ctx.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, "1.16.5")
                .set(CATALOG_DOCUMENTS.TITLE, "p35b document of the holder under a stale label")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, stale)
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, registryId(ctx, holder))
                .execute();
            assertThat(ownerOf(ctx, holder)).as("non-vacuity: the document owns the holder's tumbler segment").isEqualTo("1-16");
            PgContainerHelper.insertCollection(ctx, TENANT, stale);
            assertThat(ownerOf(ctx, stale))
                .as("a document that names this label under another id derives nothing for the new row: no registry row held the label")
                .isEqualTo("p35bstale");
        }
    }

    /**
     * The one way a registry INSERT could still find documents is a label a tombstone (a superseded row) holds: the tombstone's registry
     * row carries the documents and the new row of the same name would read them. {@code catalog_collections_pk (tenant_id, name)} stands
     * until P4.1, so a tombstone and a live row cannot share a name and the INSERT is refused: the branch is dead today for every label.
     * When P4.1 replaces the key this test is the one to revisit.
     */
    @Test
    @Order(13)
    void stampsComplete_registryInsert_ofALabelATombstoneHolds_isRefusedByTheNamePrimaryKey() throws Exception {
        completeStampsAndMigrate();
        String holder = "code__p35bholder__bge-base-en-v15-768__v1";
        String tomb = "code__p35btomb__bge-base-en-v15-768__v1";
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, holder);
            PgContainerHelper.insertCollection(ctx, TENANT, tomb);
            long tombId = registryId(ctx, tomb);
            ctx.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, "1.17.3")
                .set(CATALOG_DOCUMENTS.TITLE, "p35b document of the tombstone")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, tomb).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, tombId)
                .execute();
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, holder)
                .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, registryId(ctx, holder))
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(tombId)).execute();
            assertThat(ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)
                    .and(CATALOG_COLLECTIONS.NAME.eq(tomb)).and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isNotNull())))
                .as("non-vacuity: the label is held by a tombstone").isEqualTo(1);
            PgContainerHelper.insertCollection(ctx, TENANT, tomb); // ON CONFLICT DO NOTHING: the key refuses the second row
            assertThat(ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT).and(CATALOG_COLLECTIONS.NAME.eq(tomb))))
                .as("the name primary key keeps the label to its one row, so no live row can read the tombstone's documents").isEqualTo(1);
        }
    }

    /** One entry for {@code HALF} through the generated routine, on the caller's connection (and so inside its open transaction). */
    private static int upsertCanary(Connection c, String model, String vector, byte[] digest) {
        return upsertCanary(c, model, vector, digest, new ArrayList<>());
    }

    /** As above, with the server's warnings of the call added to {@code warnings}. */
    private static int upsertCanary(Connection c, String model, String vector, byte[] digest, List<String> warnings) {
        var listener = new org.jooq.impl.DefaultExecuteListener() {
            @Override
            public void warning(org.jooq.ExecuteContext ctx) {
                for (java.sql.SQLWarning w = ctx.sqlWarning(); w != null; w = w.getNextWarning()) {
                    warnings.add(w.getMessage());
                }
            }
        };
        var configuration = DSL.using(c, SQLDialect.POSTGRES).configuration()
            .derive(new org.jooq.impl.DefaultExecuteListenerProvider(listener));
        Integer written = Routines.canaryQueryRingUpsert(configuration, TENANT,
            new String[] {HALF}, new String[] {model}, new String[] {vector}, new byte[][] {digest});
        return written == null ? 0 : written;
    }

    /** "view nexus.v" or "trigger nexus.t.name" for every view rule and trigger that pg_depend records as depending on one of the columns. */
    private static List<String> nameColumnDependents(DSLContext ctx, List<String[]> columns) {
        List<String> out = new ArrayList<>();
        for (String[] tc : columns) {
            // a view: its rewrite rule depends on the column (a view's rule also depends on the view itself, which is not a reader)
            Field<String> view = catStr("vc", "relname");
            for (String v : ctx.selectDistinct(view)
                    .from(dependsOnColumn()
                        .join(cat("pg_rewrite", "rw")).on(DSL.field(DSL.name("rw", "oid")).eq(DSL.field(DSL.name("d", "objid"))))
                        .join(cat("pg_class", "vc")).on(DSL.field(DSL.name("vc", "oid")).eq(DSL.field(DSL.name("rw", "ev_class")))))
                    .where(columnIs(tc[0], tc[1], "pg_rewrite"))
                    .and(view.ne(catStr("rc", "relname")))
                    .fetch(view)) {
                out.add("view nexus." + v);
            }
            // a trigger: its UPDATE OF list (tgattr) depends on each column it names
            Field<String> table = catStr("tc", "relname");
            Field<String> trigger = catStr("tg", "tgname");
            for (var r : ctx.selectDistinct(table, trigger)
                    .from(dependsOnColumn()
                        .join(cat("pg_trigger", "tg")).on(DSL.field(DSL.name("tg", "oid")).eq(DSL.field(DSL.name("d", "objid"))))
                        .join(cat("pg_class", "tc")).on(DSL.field(DSL.name("tc", "oid")).eq(DSL.field(DSL.name("tg", "tgrelid")))))
                    .where(columnIs(tc[0], tc[1], "pg_trigger"))
                    .fetch()) {
                out.add("trigger nexus." + r.value1() + "." + r.value2());
            }
        }
        return out.stream().distinct().sorted().toList();
    }

    /** {@code pg_catalog.<name>} under an alias: the catalog has no jOOQ codegen, so the typed schema-qualified form. */
    private static Table<?> cat(String name, String alias) {
        return DSL.table(DSL.name("pg_catalog", name)).as(alias);
    }

    private static Field<String> catStr(String alias, String column) {
        return DSL.field(DSL.name(alias, column), String.class);
    }

    /**
     * pg_depend (d) joined to the class of its dependent (dc) and of its referenced object (dr), the referenced relation (rc), its
     * schema (rn) and the referenced column (a); {@link #columnIs} filters it to one column of a nexus relation.
     */
    private static Table<?> dependsOnColumn() {
        return cat("pg_depend", "d")
            .join(cat("pg_class", "dc")).on(DSL.field(DSL.name("dc", "oid")).eq(DSL.field(DSL.name("d", "classid"))))
            .join(cat("pg_class", "dr")).on(DSL.field(DSL.name("dr", "oid")).eq(DSL.field(DSL.name("d", "refclassid"))))
            .join(cat("pg_class", "rc")).on(DSL.field(DSL.name("rc", "oid")).eq(DSL.field(DSL.name("d", "refobjid"))))
            .join(cat("pg_namespace", "rn")).on(DSL.field(DSL.name("rn", "oid")).eq(DSL.field(DSL.name("rc", "relnamespace"))))
            .join(cat("pg_attribute", "a")).on(DSL.field(DSL.name("a", "attrelid")).eq(DSL.field(DSL.name("d", "refobjid")))
                .and(DSL.field(DSL.name("a", "attnum")).eq(DSL.field(DSL.name("d", "refobjsubid")))));
    }

    /** The dependent is a {@code classOf} object (pg_rewrite or pg_trigger) and the referenced object is nexus.{@code table}.{@code column}. */
    private static org.jooq.Condition columnIs(String table, String column, String classOf) {
        return catStr("dc", "relname").eq(classOf).and(catStr("dr", "relname").eq("pg_class"))
            .and(catStr("rn", "nspname").eq("nexus")).and(catStr("rc", "relname").eq(table)).and(catStr("a", "attname").eq(column));
    }

    // ---- fixtures ------------------------------------------------------------------------------------------------------

    /** Brings the store to the completion check's complete state (see the class comment), then migrates: the next boot. */
    private void completeStampsAndMigrate() throws Exception {
        if (stampsCompleted) {
            return;
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.deleteFrom(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT))
                .and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(GHOST)).execute();
            removeBlankRows(ctx);
            Rdr229CompleteStamps.complete(su, ctx);
            assertThat(ctx.fetchCount(MOVES, MOVE_LABEL.like("rdr229-stamp-%"))).as("non-vacuity: the walk registered the stamp moves")
                .isGreaterThan(10);
            assertThat(incompleteStampMoves(ctx)).as("every stamp move is complete").isZero();
        }
        SchemaMigrator.migrate(ds); // the next boot
        stampsCompleted = true;
    }

    /**
     * The '' fixture rows of {@link #BLANK} go before the stamp: a chunk with no collection has no id to stamp, and a real store's
     * {@code chunks_collection_id_not_null} could not validate over it.
     */
    private static void removeBlankRows(DSLContext ctx) {
        ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT))
            .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(BLANK)).execute();
        ctx.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(BLANK)).execute();
    }

    private static int incompleteStampMoves(DSLContext ctx) {
        return Rdr229CompleteStamps.incompleteStampMoves(ctx);
    }

    private static List<CompletionCheck.LabelState> stampLabelStates(DSLContext ctx) {
        return Rdr229CompleteStamps.stampLabelStates(ctx);
    }

    /** The ids of {@code ids} that databasechangelog records. */
    private List<String> recordedOf(List<String> ids) throws Exception {
        try (Connection c = ds.getConnection()) {
            Field<String> id = DSL.field(DSL.name("id"), String.class);
            return DSL.using(c, SQLDialect.POSTGRES).select(id).from(JOURNAL).where(id.in(ids)).fetch(id);
        }
    }

    private static String triggerDefinition(DSLContext ctx) {
        Field<String> def = DSL.function("pg_get_triggerdef", String.class, DSL.field(DSL.name("t", "oid")));
        String found = ctx.select(def)
            .from(DSL.table(DSL.name("pg_catalog", "pg_trigger")).as("t"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c")).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("t", "tgrelid"))))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq("catalog_documents"))
            .and(DSL.field(DSL.name("t", "tgname"), String.class).eq(TRIGGER))
            .fetchOne(def);
        assertThat(found).as("the catalog-044 trigger exists").isNotNull();
        return found;
    }

    private static String ownerOf(DSLContext ctx, String collection) {
        return ctx.select(CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(collection))
            .fetchOne(CATALOG_COLLECTIONS.OWNER_ID);
    }

    private static long registryId(DSLContext ctx, String name) {
        Long id = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        assertThat(id).as("registry id of '%s'", name).isNotNull();
        return id;
    }

    private static void stampChunks(DSLContext ctx, String collection, long id, List<String> hexes) {
        int n = ctx.update(CHUNKS).set(CHUNKS.COLLECTION_ID, id)
            .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(collection)).and(CHUNKS.CHASH.in(bytes(hexes)))
            .execute();
        assertThat(n).as("stamped chunks of '%s'", collection).isEqualTo(hexes.size());
    }

    private static void stampManifest(DSLContext ctx, String collection, long id, List<String> hexes) {
        int n = ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, id)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(collection))
            .and(CATALOG_DOCUMENT_CHUNKS.CHASH.in(bytes(hexes)))
            .execute();
        assertThat(n).as("stamped manifest rows of '%s'", collection).isEqualTo(hexes.size());
    }

    private static void seedOwnedChunks(DSLContext ctx, String collection, List<String> hexes) {
        Vector v = vector(768);
        for (String hex : hexes) {
            PgContainerHelper.insertChunk768(ctx, TENANT, collection, Chash.fromHex(hex).toBytes(), v);
        }
        PgContainerHelper.ownChunks(ctx, TENANT, collection, hexes.toArray(String[]::new));
    }

    private static Vector vector(int dim) {
        float[] f = new float[dim];
        Arrays.fill(f, 0.1f);
        return Vector.of(f);
    }

    private static List<byte[]> bytes(List<String> hexes) {
        return hexes.stream().map(h -> Chash.fromHex(h).toBytes()).toList();
    }

    /** Deterministic 64-hex chashes: the SHA-256 of the label. */
    private static List<String> labels(String... labels) {
        List<String> out = new ArrayList<>();
        try {
            for (String label : labels) {
                out.add(HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(label.getBytes(StandardCharsets.UTF_8))));
            }
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
        return out;
    }

    private static final Table<?> LIVE_CHUNKS = DSL.table(DSL.name("nexus", "live_chunks"));
    private static final Table<?> VECTOR_STATS = DSL.table(DSL.name("nexus", "collection_vector_stats"));

    /** The chashes {@code nexus.live_chunks} lists for {@code collection} (the chunk's own name column), as hex. */
    private static List<String> liveChashes(DSLContext ctx, String collection) {
        return ctx.select(DSL.field(DSL.name("chash"), byte[].class)).from(LIVE_CHUNKS)
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(TENANT))
            .and(DSL.field(DSL.name("collection"), String.class).eq(collection))
            .fetch(r -> HexFormat.of().formatHex(r.value1()));
    }

    /** Sum of one count column of {@code nexus.collection_vector_stats} over the rows of {@code collection}; 0 when the view has no row. */
    private static long viewCount(DSLContext ctx, String column, String collection) {
        Long sum = ctx.select(DSL.sum(DSL.field(DSL.name(column), Long.class)).cast(Long.class)).from(VECTOR_STATS)
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(TENANT))
            .and(DSL.field(DSL.name("collection"), String.class).eq(collection))
            .fetchOne(0, Long.class);
        return sum == null ? 0L : sum;
    }

    /** (table, name column) of every NAME_ID and SUPERSEDED row of the one list of the expand's id columns. */
    private static List<String[]> nameColumns() throws IOException {
        List<String[]> out = new ArrayList<>();
        for (String[] f : Rdr229CompleteStamps.checkRows()) {
            if (f[4].equals("NAME_ID") || f[4].equals("SUPERSEDED")) {
                out.add(new String[] {f[0], f[1]});
            }
        }
        return out;
    }

    /** Applies the HEAD changelog up to, but not including, {@code changesetId}. */
    private static void migrateUpTo(HikariDataSource ds, String changesetId) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<liquibase.changelog.ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (changesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as("%s must be pending in the HEAD changelog", changesetId).isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }
}
