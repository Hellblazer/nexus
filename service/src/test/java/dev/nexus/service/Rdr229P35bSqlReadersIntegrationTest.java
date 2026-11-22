// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.jooq.binding.Vector;
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

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.OffsetDateTime;
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
 * <p>Diverging name columns. Between a chunk and its manifest row the name columns can differ only once the name key between
 * them ({@code fk_catalog_chunks_chunk}, a foreign key on the name columns) is gone, which is P3.7's name-key drop. The fixture
 * does that drop and then rewrites the manifest row's name, as an id-only writer or the one-row rename leaves it; the id on
 * both sides stays the registry's.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class Rdr229P35bSqlReadersIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_p35b_sql";
    private static final String ADMIN_PASS = "nexus_admin_p35b_sql_pass";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String FIRST = "vectors-068-1";
    private static final String TENANT = "p35b-sql";
    private static final String CHECKS_LIST = "db/rdr229/p31-checks.tsv";

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
            ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, DIVERGED_MANIFEST_NAME)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(DIVERGED))
                .execute();
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

    /** RED: once every {@code rdr229-stamp-*} move is complete and the next boot has run, the trigger fires on the id column. */
    @Test
    @Order(5)
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
    @Order(6)
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
     * RED: P4.1 drops every name column with CASCADE. A form A view that still reads a name column is dropped with it, so after the
     * form A changesets the three views must survive the drop. live_chunks and collection_vector_stats are form B and keep a name arm
     * until P4.1 redefines them in its own changeset, so they are not asserted here.
     */
    @Test
    @Order(7)
    void p41NameColumnDrop_leavesTheFormAViews() throws Exception {
        completeStampsAndMigrate();
        List<String> formAViews = List.of("collection_health_meta", "collection_doc_counts", "topics_with_counts");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String view : formAViews) {
                assertThat(PgCatalogProbes.viewExists(ctx, "nexus", view)).as("non-vacuity: %s exists before the drop", view).isTrue();
            }
            int dropped = 0;
            for (String[] tc : nameColumns()) {
                if (!PgCatalogProbes.columnExists(ctx, "nexus", tc[0], tc[1])) {
                    continue; // aspect_extraction_queue is absent on most installs
                }
                ctx.alterTable(DSL.table(DSL.name("nexus", tc[0]))).dropColumn(DSL.field(DSL.name(tc[1]))).cascade().execute();
                dropped++;
            }
            assertThat(dropped).as("non-vacuity: the drop reached the name columns of the list").isGreaterThan(10);
            List<String> missing = new ArrayList<>();
            for (String view : formAViews) {
                if (!PgCatalogProbes.viewExists(ctx, "nexus", view)) {
                    missing.add(view);
                }
            }
            su.rollback();
            assertThat(missing)
                .as("these form A views were dropped with the name columns: they still read a name column and must read the id")
                .isEmpty();
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String view : formAViews) {
                assertThat(PgCatalogProbes.viewExists(ctx, "nexus", view)).as("the rolled-back drop left %s", view).isTrue();
            }
            assertThat(PgCatalogProbes.columnExists(ctx, "nexus", "chunks", "collection")).as("and the name column").isTrue();
        }
    }

    // ---- fixtures ------------------------------------------------------------------------------------------------------

    private void completeStampsAndMigrate() throws Exception {
        if (stampsCompleted) {
            return;
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var moves = DSL.table(DSL.name("nexus", "data_moves"));
            var label = DSL.field(DSL.name("label"), String.class);
            int completed = ctx.update(moves).set(DSL.field(DSL.name("completed_at"), OffsetDateTime.class), OffsetDateTime.now())
                .where(label.like("rdr229-stamp-%")).execute();
            assertThat(completed).as("non-vacuity: the walk registered the stamp moves").isGreaterThan(0);
            assertThat(incompleteStampMoves(ctx)).as("every stamp move is complete").isZero();
        }
        SchemaMigrator.migrate(ds); // the next boot
        stampsCompleted = true;
    }

    private static int incompleteStampMoves(DSLContext ctx) {
        var moves = DSL.table(DSL.name("nexus", "data_moves"));
        return ctx.fetchCount(moves, DSL.field(DSL.name("label"), String.class).like("rdr229-stamp-%")
            .and(DSL.field(DSL.name("completed_at"), OffsetDateTime.class).isNull()));
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
        try (InputStream in = Rdr229P35bSqlReadersIntegrationTest.class.getClassLoader().getResourceAsStream(CHECKS_LIST)) {
            assertThat(in).as("%s is on the classpath", CHECKS_LIST).isNotNull();
            BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            for (String line = reader.readLine(); line != null; line = reader.readLine()) {
                if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) {
                    continue;
                }
                String[] f = line.split("\t", -1);
                if (f[4].equals("NAME_ID") || f[4].equals("SUPERSEDED")) {
                    out.add(new String[] {f[0], f[1]});
                }
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
