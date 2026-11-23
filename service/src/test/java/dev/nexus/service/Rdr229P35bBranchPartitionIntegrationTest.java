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
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Objects;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24), the fix round of 2026-11-23 (linda): {@code nexus.collection_vector_stats} and
 * {@code nexus.live_chunks} were rewritten as UNION ALL branches by the shape of a chunk's collection, so a filter on the label or
 * the id reaches the keys. A branch split can count a chunk twice or not at all, so this class pins, on one fixture holding every
 * shape, that each chunk lands in exactly one branch and that the rewrite gives what r3's bodies (68bb22481a, copied into the
 * scratch schema {@code p35b_r3} by {@code db.changelog-test-p35b-r3-views.xml}) give, apart from the one deliberate difference.
 *
 * <p>The shapes, one collection each: stamped and registered ({@code REG}, one chunk stored and not live); half stamped
 * ({@code MIX}: chunks and manifest rows each part stamped); not stamped and registered ({@code UREG}); not stamped and not
 * registered ({@code UNREG}); stamped with an id no registry row holds ({@code DANG}, its row removed); and the unset name with
 * no id ({@code ''}). Rows are seeded before the expand (a NOT VALID CHECK still applies to rows written after it), and the shapes
 * a key forbids are written with the session's triggers off (session_replication_role = replica), as a store left by an earlier
 * writer holds them.
 *
 * <p>The deliberate difference: a collection none of whose chunks is stamped, but which the registry holds by name, shows the
 * registry's id in the stats view's trailing column (r3 showed NULL, the max over no id), so it is one group with its stamped rows
 * once the stamp begins. Everything else is equal.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35bBranchPartitionIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_p35b_part";
    private static final String ADMIN_PASS = "nexus_admin_p35b_part_pass";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String FIRST = "vectors-068-1";
    private static final String R3_VIEWS = "db/changelog-test/db.changelog-test-p35b-r3-views.xml";
    private static final String TENANT = "p35b-part";

    private static final String REG = "p35b-part-reg";
    private static final String MIX = "p35b-part-mix";
    private static final String UREG = "p35b-part-ureg";
    private static final String UNREG = "p35b-part-unreg";
    private static final String DANG = "p35b-part-dang";
    private static final String BLANK = "";

    private static final List<String> REG_LIVE = labels("reg-1", "reg-2", "reg-3");
    private static final List<String> REG_STORED_ONLY = labels("reg-4");
    private static final List<String> MIX_CHASHES = labels("mix-1", "mix-2", "mix-3", "mix-4");
    private static final List<String> UREG_CHASHES = labels("ureg-1", "ureg-2");
    private static final List<String> UNREG_CHASHES = labels("unreg-1");
    private static final List<String> DANG_CHASHES = labels("dang-1", "dang-2");
    private static final List<String> BLANK_CHASHES = labels("blank-1");

    private static final Table<?> NEW_STATS = DSL.table(DSL.name("nexus", "collection_vector_stats"));
    private static final Table<?> R3_STATS = DSL.table(DSL.name("p35b_r3", "collection_vector_stats"));
    private static final Table<?> NEW_LIVE = DSL.table(DSL.name("nexus", "live_chunks"));
    private static final Table<?> R3_LIVE = DSL.table(DSL.name("p35b_r3", "live_chunks"));
    private static final Field<String> V_TENANT = DSL.field(DSL.name("tenant_id"), String.class);
    private static final Field<String> V_COLLECTION = DSL.field(DSL.name("collection"), String.class);
    private static final Field<Integer> V_DIM = DSL.field(DSL.name("dim"), Integer.class);
    private static final Field<Long> V_CHUNK_COUNT = DSL.field(DSL.name("chunk_count"), Long.class);
    private static final Field<Long> V_STORED_COUNT = DSL.field(DSL.name("stored_count"), Long.class);
    private static final Field<java.time.OffsetDateTime> V_LAST_WRITE =
        DSL.field(DSL.name("last_write"), java.time.OffsetDateTime.class);
    private static final Field<Long> V_COLLECTION_ID = DSL.field(DSL.name("collection_id"), Long.class);
    private static final Field<byte[]> V_CHASH = DSL.field(DSL.name("chash"), byte[].class);

    private PostgreSQLContainer<?> pg;
    private HikariDataSource ds;
    private long uregId;

    @BeforeAll
    void seedEveryShape() throws Exception {
        pg = PgContainerHelper.startEmptyDatabase();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-admin-p35b-part");
        ds = new HikariDataSource(cfg);

        migrateUpTo(ds, FIRST);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(REG, MIX, UREG, UNREG, DANG, BLANK)) {
                PgContainerHelper.insertCollection(ctx, TENANT, c);
            }
            seed(ctx, REG, REG_LIVE, true);
            seed(ctx, REG, REG_STORED_ONLY, false);
            seed(ctx, MIX, MIX_CHASHES, true);
            // linda (the re-check of b6ff7a3cb7): a chunk held by two live manifest rows (REG's first), and one held by an
            // id-stamped and an unstamped manifest row (MIX's first, stamped below by position), pin count(DISTINCT chash)
            PgContainerHelper.ownChunks(ctx, TENANT, REG, REG_LIVE.get(0));
            PgContainerHelper.ownChunks(ctx, TENANT, MIX, MIX_CHASHES.get(0));
            seed(ctx, UREG, UREG_CHASHES, true);
            seed(ctx, DANG, DANG_CHASHES, true);
            seed(ctx, BLANK, BLANK_CHASHES, true);
            seed(ctx, UNREG, UNREG_CHASHES, true);
        }

        SchemaMigrator.migrate(ds);

        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION_ID.isNotNull())))
                .as("non-vacuity: every seeded row is unstamped after the expand").isZero();
            long regId = registryId(ctx, REG);
            stamp(ctx, REG, regId, concat(REG_LIVE, REG_STORED_ONLY), REG_LIVE);
            long mixId = registryId(ctx, MIX);
            stamp(ctx, MIX, mixId, MIX_CHASHES.subList(0, 3), MIX_CHASHES.subList(1, 2));
            stampFirstManifestRow(ctx, MIX, mixId, MIX_CHASHES.get(0));
            assertThat(ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)
                    .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(Chash.fromHex(REG_LIVE.get(0)).toBytes()))
                    .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.eq(regId))))
                .as("non-vacuity: two id-stamped live manifest rows hold REG's first chunk").isEqualTo(2);
            assertThat(ctx.fetch(DSL.select(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID).from(CATALOG_DOCUMENT_CHUNKS)
                    .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT))
                    .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(Chash.fromHex(MIX_CHASHES.get(0)).toBytes())))
                    .getValues(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID))
                .as("non-vacuity: MIX's first chunk is held by one stamped and one unstamped manifest row")
                .containsExactlyInAnyOrder(mixId, null);
            uregId = registryId(ctx, UREG);
            long dangId = registryId(ctx, DANG);
            stamp(ctx, DANG, dangId, DANG_CHASHES, DANG_CHASHES);
            // DANG's and UNREG's registry rows go, their rows stay (DANG's with its id, UNREG's with none): the shapes a deleted
            // collection's leftovers have, written with the triggers (and so the keys) off
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            assertThat(ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
                .and(CATALOG_COLLECTIONS.NAME.in(DANG, UNREG)).execute()).isEqualTo(2);
            ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT).and(CHUNKS.COLLECTION_ID.eq(dangId))))
                .as("non-vacuity: the dangling rows keep their id").isEqualTo(DANG_CHASHES.size());
            PgContainerHelper.runSuperuserTestChangelog(su, R3_VIEWS, "databasechangelog_test_p35b_r3_views", Map.of());
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

    @Test
    void stats_everyChunkIsStoredInExactlyOneBranch_andEveryShapeIsPresent() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            long chunks = ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(TENANT));
            assertThat(chunks).as("non-vacuity: the fixture's chunks").isEqualTo(14);
            Long stored = ctx.select(DSL.sum(V_STORED_COUNT).cast(Long.class)).from(NEW_STATS).where(V_TENANT.eq(TENANT))
                .fetchOne(0, Long.class);
            assertThat(stored).as("every chunk is stored in exactly one branch: none counted twice, none dropped").isEqualTo(chunks);
            assertThat(ctx.select(V_COLLECTION).from(NEW_STATS).where(V_TENANT.eq(TENANT)).fetch(V_COLLECTION))
                .as("one row per collection, every shape present").containsExactlyInAnyOrder(REG, MIX, UREG, UNREG, DANG, BLANK);
        }
    }

    @Test
    void stats_equalsR3_butForTheUnstampedRegisteredCollectionsId() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Map<String, List<Object>> now = statsRows(ctx, NEW_STATS);
            Map<String, List<Object>> r3 = statsRows(ctx, R3_STATS);
            assertThat(r3).as("non-vacuity: r3's view reads the fixture").hasSize(6);
            assertThat(r3.get(UREG).get(4)).as("r3: a collection with no stamped chunk shows no id").isNull();
            assertThat(now.get(UREG).get(4)).as("the rewrite: the registry's id, so it groups with its stamped rows later")
                .isEqualTo(uregId);
            now.get(UREG).set(4, null);
            assertThat(now).as("chunk_count, stored_count, last_write and collection_id per collection and dim, as r3")
                .isEqualTo(r3);
            assertThat(r3.get(REG).subList(1, 3)).as("non-vacuity: a stored chunk that is not live, and a chunk two live rows hold counted once")
                .containsExactly(3L, 4L);
            assertThat(now.get(MIX).subList(1, 3)).as("a chunk held by a stamped and an unstamped manifest row is counted once")
                .containsExactly(4L, 4L);
            assertThat(r3.get(BLANK).subList(1, 3)).as("non-vacuity: the '' row with no id is stored, never live")
                .containsExactly(0L, 1L);
        }
    }

    @Test
    void liveChunks_everyLiveChunkIsListedOnce_andTheRowsEqualR3() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<String> now = liveRows(ctx, NEW_LIVE);
            List<String> r3 = liveRows(ctx, R3_LIVE);
            int expected = REG_LIVE.size() + MIX_CHASHES.size() + UREG_CHASHES.size() + UNREG_CHASHES.size() + DANG_CHASHES.size();
            assertThat(r3).as("non-vacuity: r3 lists every live chunk of every shape, and none of ''").hasSize(expected);
            assertThat(now).as("each live chunk lands in exactly one branch: the same rows as r3, duplicates included")
                .containsExactlyInAnyOrderElementsOf(r3);
            assertThat(now).as("and no row is listed twice").doesNotHaveDuplicates();
        }
    }

    @Test
    void aFilterOnTheLabelOrTheId_selectsTheSameRowsAsTheWholeView() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Map<String, List<Object>> all = statsRows(ctx, NEW_STATS);
            for (String c : List.of(REG, MIX, UREG, UNREG, DANG, BLANK)) {
                Record r = ctx.select(V_CHUNK_COUNT, V_STORED_COUNT).from(NEW_STATS)
                    .where(V_TENANT.eq(TENANT)).and(V_COLLECTION.eq(c)).fetchOne();
                assertThat(r).as("WHERE collection = '%s' finds its row", c).isNotNull();
                assertThat(List.of(r.get(V_CHUNK_COUNT), r.get(V_STORED_COUNT))).isEqualTo(all.get(c).subList(1, 3));
            }
            long mixId = registryId(ctx, MIX);
            assertThat(ctx.select(V_STORED_COUNT).from(NEW_STATS).where(V_TENANT.eq(TENANT)).and(V_COLLECTION_ID.eq(mixId))
                    .fetch(V_STORED_COUNT))
                .as("WHERE collection_id = the half-stamped collection's id: one row, all four chunks")
                .containsExactly(4L);
            assertThat(ctx.fetchCount(NEW_LIVE, V_TENANT.eq(TENANT).and(V_COLLECTION.eq(MIX))))
                .as("live_chunks WHERE collection = the half-stamped one: all four").isEqualTo(4);
        }
    }

    // ---- helpers ---------------------------------------------------------------------------------------------------

    /** collection -> [dim, chunk_count, stored_count, last_write, collection_id]; one row per collection on this fixture. */
    private static Map<String, List<Object>> statsRows(DSLContext ctx, Table<?> view) {
        Map<String, List<Object>> out = new HashMap<>();
        for (Record r : ctx.select(V_COLLECTION, V_DIM, V_CHUNK_COUNT, V_STORED_COUNT, V_LAST_WRITE, V_COLLECTION_ID)
                .from(view).where(V_TENANT.eq(TENANT)).fetch()) {
            List<Object> row = new ArrayList<>(Arrays.asList(r.get(V_DIM), r.get(V_CHUNK_COUNT), r.get(V_STORED_COUNT),
                Objects.toString(r.get(V_LAST_WRITE)), r.get(V_COLLECTION_ID)));
            assertThat(out.put(r.get(V_COLLECTION), row)).as("one row of %s per collection on this fixture", view).isNull();
        }
        return out;
    }


    private static List<String> liveRows(DSLContext ctx, Table<?> view) {
        return ctx.select(V_COLLECTION, V_CHASH, V_COLLECTION_ID).from(view).where(V_TENANT.eq(TENANT))
            .fetch(r -> r.get(V_COLLECTION) + "|" + HexFormat.of().formatHex(r.get(V_CHASH)) + "|" + r.get(V_COLLECTION_ID));
    }

    private static void seed(DSLContext ctx, String collection, List<String> hexes, boolean owned) {
        Vector v = vector(768);
        for (String hex : hexes) {
            PgContainerHelper.insertChunk768(ctx, TENANT, collection, Chash.fromHex(hex).toBytes(), v);
        }
        if (owned) {
            PgContainerHelper.ownChunks(ctx, TENANT, collection, hexes.toArray(String[]::new));
        }
    }

    private static void stamp(DSLContext ctx, String collection, long id, List<String> chunkHexes, List<String> manifestHexes) {
        assertThat(ctx.update(CHUNKS).set(CHUNKS.COLLECTION_ID, id)
            .where(CHUNKS.TENANT_ID.eq(TENANT)).and(CHUNKS.COLLECTION.eq(collection)).and(CHUNKS.CHASH.in(bytes(chunkHexes)))
            .execute()).as("stamped chunks of '%s'", collection).isEqualTo(chunkHexes.size());
        assertThat(ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, id)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(collection))
            .and(CATALOG_DOCUMENT_CHUNKS.CHASH.in(bytes(manifestHexes)))
            .execute()).as("stamped manifest rows of '%s'", collection).isGreaterThanOrEqualTo(manifestHexes.size());
    }

    /** Stamps the manifest row of {@code hex} at the lowest position only, leaving any other row of it unstamped. */
    private static void stampFirstManifestRow(DSLContext ctx, String collection, long id, String hex) {
        byte[] chash = Chash.fromHex(hex).toBytes();
        var first = DSL.select(DSL.min(CATALOG_DOCUMENT_CHUNKS.POSITION)).from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(collection))
            .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(chash));
        assertThat(ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, id)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(collection))
            .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(chash)).and(CATALOG_DOCUMENT_CHUNKS.POSITION.eq(first))
            .execute()).as("stamped the first manifest row of one chunk of '%s'", collection).isEqualTo(1);
    }

    private static long registryId(DSLContext ctx, String name) {
        Long id = ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        assertThat(id).as("registry id of '%s'", name).isNotNull();
        return id;
    }

    private static List<String> concat(List<String> a, List<String> b) {
        List<String> out = new ArrayList<>(a);
        out.addAll(b);
        return out;
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
                out.add(HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(("p35b-part/" + label).getBytes(StandardCharsets.UTF_8))));
            }
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
        return out;
    }

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
