// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.CollectionTombstones;
import dev.nexus.service.db.TenantExport;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.UnaryOperator;

/**
 * RDR-229 P3.1 B (nexus-p2m1x.17): a tenant restore under the dual write's NOT VALID CHECKs. A pre-P3.1 backup has names and
 * no ids, and the CHECK refuses every such row, so the load fills each id from the RESTORED registry row on
 * {@code (tenant_id, name)} before the insert ({@code RestoreIdFill}). A file's ids, where it has any, are never
 * trusted: the restore refills every id from the restored registry by name.
 *
 * <p>Every case restores the committed old-release fixture ({@code rdr248-tenant-backup.nxtenant}, written before the id
 * columns existed) or a copy of it edited at the byte level, through the import routes, on a real Postgres.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229RestoreIdFillTest {

    private static final Path LIST = Path.of("src", "main", "resources", "db", "rdr229", "p31-checks.tsv");

    /** A name column, its id column, and whether the id is a history label (no CHECK, no fill). */
    record Site(String table, String nameColumn, String idColumn) {
        @Override
        public String toString() {
            return table + "." + idColumn;
        }
    }

    private ImportTestEnv env;

    @BeforeAll
    void startAll() throws Exception {
        env = ImportTestEnv.start();
    }

    @AfterAll
    void stopAll() {
        if (env != null) env.close();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────

    private static ImportTestEnv.BackupFile fixture() throws IOException {
        try (var in = Rdr229RestoreIdFillTest.class.getResourceAsStream(TenantImportFixtureTest.RESOURCE)) {
            assertThat(in).as("the committed fixture").isNotNull();
            return TenantBackupFiles.read(in.readAllBytes());
        }
    }

    /** The NAME_ID rows of the expand's list whose table a backup file carries (data and derived), each once. */
    private static List<Site> exportedSites() throws IOException {
        Set<Site> sites = new LinkedHashSet<>();
        for (String line : Files.readAllLines(LIST)) {
            if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) continue;
            String[] f = line.split("\t");
            if (!f[4].equals("NAME_ID") || TenantExport.exportable(f[0]).isEmpty()) continue;
            sites.add(new Site(f[0], f[1], f[2]));
        }
        return List.copyOf(sites);
    }

    private static Field<String> text(String alias, String column) {
        return DSL.field(DSL.name(alias, column), SQLDataType.VARCHAR);
    }

    private static Field<Long> id(String alias, String column) {
        return DSL.field(DSL.name(alias, column), SQLDataType.BIGINT);
    }

    /** Rows of {@code tenant} in {@code site.table()} with a non-empty name. */
    private static long examined(DSLContext ctx, String tenant, Site site) {
        return ctx.fetchCount(DSL.table(DSL.name("nexus", site.table())).as("t"),
            text("t", "tenant_id").eq(tenant), text("t", site.nameColumn()).ne(""));
    }

    /** Of those, the rows whose id is not the restored registry row's id (a NULL counts: the CHECK forbids it). */
    private static long wrongIds(DSLContext ctx, String tenant, Site site) {
        return ctx.fetchCount(DSL.table(DSL.name("nexus", site.table())).as("t"),
            text("t", "tenant_id").eq(tenant), text("t", site.nameColumn()).ne(""),
            id("t", site.idColumn()).isDistinctFrom(
                CollectionIds.idOf(text("t", "tenant_id"), text("t", site.nameColumn()))));
    }

    private static byte[] utf8(String s) {
        return s.getBytes(StandardCharsets.UTF_8);
    }

    private static byte[] bigint(long v) {
        return ByteBuffer.allocate(8).putLong(v).array();
    }

    /** The file with {@code table}'s rows rewritten page by page by {@code edit}. */
    private static ImportTestEnv.BackupFile editRows(ImportTestEnv.BackupFile file, String table,
                                                     UnaryOperator<List<BinaryCopyTestSupport.Row>> edit) {
        ImportTestEnv.BackupFile c = file.copy();
        c.pages.computeIfPresent(table, (k, pages) -> pages.stream().map(p -> {
            BinaryCopyTestSupport.Stream s = BinaryCopyTestSupport.parse(p);
            return BinaryCopyTestSupport.encode(new BinaryCopyTestSupport.Stream(s.header(), edit.apply(s.rows())));
        }).toList());
        return c.seal();
    }

    private static List<BinaryCopyTestSupport.Row> allRows(ImportTestEnv.BackupFile file, String table) {
        List<BinaryCopyTestSupport.Row> rows = new ArrayList<>();
        file.pages.getOrDefault(table, List.of()).forEach(p -> rows.addAll(BinaryCopyTestSupport.parse(p).rows()));
        return rows;
    }

    private static String fieldText(BinaryCopyTestSupport.Row row, int index) {
        byte[] f = row.fields().get(index);
        return f == null ? null : new String(f, StandardCharsets.UTF_8);
    }

    private static BinaryCopyTestSupport.Row withField(BinaryCopyTestSupport.Row row, int index, byte[] value) {
        List<byte[]> f = new ArrayList<>(row.fields());
        f.set(index, value);
        return new BinaryCopyTestSupport.Row(f);
    }

    /** A number read as the superuser; a named type keeps AssertJ from reading the lambda as a Predicate. */
    private long num(java.util.function.Function<DSLContext, ? extends Number> read) {
        Number n = env.su(ctx -> read.apply(ctx));
        return n.longValue();
    }

    private static String status(JsonNode end) {
        return end.get("status").asText();
    }

    // ── the old-release fixture ─────────────────────────────────────────────────────────────────

    @Test
    void theOldReleaseFixtureRestoresAndEveryStampedRowTakesItsRegistryRowsId() throws Exception {
        String tenant = "t-p31-fill-fixture";
        JsonNode end = env.restore(env.newTenant(tenant), fixture());
        assertThat(status(end)).as(end.toString()).isEqualTo("committed");

        long totalExamined = 0;
        List<String> sitesWithNoRow = new ArrayList<>();
        for (Site site : exportedSites()) {
            long examined = env.su(ctx -> examined(ctx, tenant, site));
            if (examined == 0) sitesWithNoRow.add(site.toString());
            totalExamined += examined;
            assertThat(num(ctx -> wrongIds(ctx, tenant, site))).as("rows of %s whose id is not the registry row's", site).isZero();
        }
        assertThat(sitesWithNoRow).as("non-vacuity: the fixture holds rows in every stamped exported table").isEmpty();
        assertThat(totalExamined).isGreaterThan(exportedSites().size());
        long registry = env.count("catalog_collections", tenant);
        assertThat(registry).isEqualTo(fixture().rows("catalog_collections")).isPositive();
        assertThat(num(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", "catalog_collections")),
            DSL.field(DSL.name("tenant_id"), String.class).eq(tenant),
            DSL.field(DSL.name("collection_id"), Long.class).isNull())))
            .as("registry rows with no collection_id").isZero();
    }

    @Test
    void anEmptyNameKeepsANullId() throws Exception {
        ImportTestEnv.BackupFile file = fixture();
        int nameAt = file.columnIndex("catalog_documents", "physical_collection");
        int[] edited = {0};
        ImportTestEnv.BackupFile blanked = editRows(file, "catalog_documents", rows -> rows.stream().map(r -> {
            if (edited[0]++ == 0) return withField(r, nameAt, utf8(""));
            return r;
        }).toList());
        String tenant = "t-p31-fill-empty";
        JsonNode end = env.restore(env.newTenant(tenant), blanked);
        assertThat(status(end)).as(end.toString()).isEqualTo("committed");
        long blankRows = env.su(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", "catalog_documents")).as("t"),
            text("t", "tenant_id").eq(tenant), text("t", "physical_collection").eq("")));
        assertThat(blankRows).as("non-vacuity: the file has a row with an empty name").isEqualTo(1);
        assertThat(num(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", "catalog_documents")).as("t"),
            text("t", "tenant_id").eq(tenant), text("t", "physical_collection").eq(""), id("t", "physical_collection_id").isNull())))
            .as("the empty-name row keeps a NULL id").isEqualTo(1);
    }

    @Test
    void aFileWhoseIdColumnsExistAndAreNullIsFilledByNameToo() throws Exception {
        ImportTestEnv.BackupFile withNullColumn = fixture().withColumn("chunks", "collection_id", "bigint", null).seal();
        String tenant = "t-p31-fill-nullcol";
        JsonNode end = env.restore(env.newTenant(tenant), withNullColumn);
        assertThat(status(end)).as(end.toString()).isEqualTo("committed");
        Site chunks = new Site("chunks", "collection", "collection_id");
        assertThat(num(ctx -> examined(ctx, tenant, chunks))).isPositive();
        assertThat(num(ctx -> wrongIds(ctx, tenant, chunks))).isZero();
    }

    // ── refusals ────────────────────────────────────────────────────────────────────────────────

    @Test
    void aBackupWithAnOrphanNameIsRefusedBeforeAnyWriteNamingTheTableTheCountAndAName() throws Exception {
        ImportTestEnv.BackupFile file = fixture();
        int nameAt = file.columnIndex("catalog_documents", "physical_collection");
        String orphan = fieldText(allRows(file, "catalog_documents").get(0), nameAt);
        int registryNameAt = file.columnIndex("catalog_collections", "name");
        ImportTestEnv.BackupFile seeded = editRows(file, "catalog_collections",
            rows -> rows.stream().filter(r -> !orphan.equals(fieldText(r, registryNameAt))).toList());
        assertThat(seeded.rows("catalog_collections")).as("non-vacuity: the registry row was removed")
            .isEqualTo(file.rows("catalog_collections") - 1);

        String tenant = "t-p31-fill-orphan";
        JsonNode end = env.restore(env.newTenant(tenant), seeded);

        assertThat(status(end)).startsWith("failed: restore_orphan_collection")
            .contains("catalog_documents").contains(orphan).containsPattern("catalog_documents\\.physical_collection: \\d+ row");
        assertThat(env.count("catalog_collections", tenant)).as("the refusal writes nothing").isZero();
        assertThat(env.count("catalog_documents", tenant)).isZero();
        assertThat(env.count("chunks", tenant)).isZero();
    }

    // ── a file's ids are never trusted ──────────────────────────────────────────────────────────

    /**
     * A file's registry ids are the source's and are never written: the restored registry draws its own, whatever the file held
     * (here the same value in every row, and a successor id on live rows). Since P3.11 a child's id is mapped through the file's
     * registry ids rather than refilled by name, so a child twin the registry does not hold is a refusal
     * ({@code Rdr229RestoreByIdTest#t3_aTwinThatNamesNoRegistryRowOfTheFileIsRefusedBeforeAnyWrite}), and this file's children
     * have no twin and are filled by name.
     */
    @Test
    void aFileWhoseRegistryIdsAreAnotherInstallationsIsNeverTrusted() throws Exception {
        ImportTestEnv.BackupFile poisoned = fixture()
            .withColumn("catalog_collections", "collection_id", "bigint", bigint(4243))
            .withColumn("catalog_collections", "superseded_by_id", "bigint", bigint(4244)).seal();
        String tenant = "t-p31-fill-poisoned";
        JsonNode end = env.restore(env.newTenant(tenant), poisoned);
        assertThat(status(end)).as(end.toString()).isEqualTo("committed");

        Site chunks = new Site("chunks", "collection", "collection_id");
        assertThat(num(ctx -> examined(ctx, tenant, chunks))).isPositive();
        assertThat(num(ctx -> wrongIds(ctx, tenant, chunks))).as("chunks carry the registry rows' ids").isZero();
        assertThat(num(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", "catalog_collections")),
            DSL.field(DSL.name("tenant_id"), String.class).eq(tenant),
            DSL.field(DSL.name("collection_id"), Long.class).in(4243L, 4244L)))).isZero();
        assertThat(num(ctx -> ctx.fetchCount(DSL.table(DSL.name("nexus", "catalog_collections")),
            DSL.field(DSL.name("tenant_id"), String.class).eq(tenant),
            DSL.field(DSL.name("superseded_by_id"), Long.class).isNotNull(),
            DSL.field(DSL.name("superseded_by"), String.class).eq("")))).as("no live row takes a successor id").isZero();
    }

    /**
     * Code review F6, and P3.11's rule: a quarantined chunk's metadata carries the origin collection by name and, since P3.1, by id
     * ({@code QuarantineOrigin.ORIGIN_ID_KEY}, {@code origin_collection_id}). The id is the SOURCE installation's: a restore mints new
     * ids, so one the file's registry does not hold (this file has no ids at all) is stripped, whatever name sits beside it (a name
     * may be stale from P3.7). Every other key of the metadata is kept. A tag with the id key mapped is
     * {@code Rdr229RestoreByIdTest#t1_aRenamedCollectionsRowsStayOnItWhenAnotherCollectionTookTheOldLabel}.
     */
    @Test
    void aChunksOriginIdTagThatTheFilesRegistryDoesNotHoldIsStripped_neverLeftAsTheSourcesId() throws Exception {
        ImportTestEnv.BackupFile file = fixture();
        int metadataAt = file.columnIndex("chunks", "metadata");
        int collectionAt = file.columnIndex("chunks", "collection");
        List<BinaryCopyTestSupport.Row> chunkRows = allRows(file, "chunks");
        assertThat(chunkRows.size()).as("non-vacuity: the fixture holds chunks").isGreaterThanOrEqualTo(3);
        String origin = fieldText(chunkRows.get(0), collectionAt);
        String[] tags = {
            "{\"origin_collection\": \"" + origin + "\", \"origin_collection_id\": 9999991, \"keep\": \"yes\"}",
            "{\"origin_collection\": \"ghost-origin-p31\", \"origin_collection_id\": 9999992, \"keep\": \"yes\"}",
            "{\"origin_collection_id\": 9999993, \"keep\": \"yes\"}"};
        int[] seen = {0};
        ImportTestEnv.BackupFile tagged = editRows(file, "chunks", rows -> rows.stream().map(r -> {
            int n = seen[0]++;
            if (n >= tags.length) return r;
            byte[] json = utf8(tags[n]);
            byte[] jsonb = new byte[json.length + 1];
            jsonb[0] = 1;   // the binary format of jsonb: a version byte, then the text
            System.arraycopy(json, 0, jsonb, 1, json.length);
            return withField(r, metadataAt, jsonb);
        }).toList());

        String tenant = "t-p31-fill-origin-tag";
        JsonNode end = env.restore(env.newTenant(tenant), tagged);
        assertThat(status(end)).as(end.toString()).isEqualTo("committed");

        Field<org.jooq.JSONB> metadata = DSL.field(DSL.name("c", "metadata"), SQLDataType.JSONB);
        Field<String> originId = DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, metadata, DSL.val("origin_collection_id"));
        var chunks = DSL.table(DSL.name("nexus", "chunks")).as("c");
        assertThat(num(ctx -> ctx.fetchCount(chunks, text("c", "tenant_id").eq(tenant),
            DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, metadata, DSL.val("keep")).eq("yes"))))
            .as("non-vacuity: the three tagged rows restored, with every other key kept").isEqualTo(3);
        assertThat(num(ctx -> ctx.fetchCount(chunks, text("c", "tenant_id").eq(tenant),
            originId.in("9999991", "9999992", "9999993"))))
            .as("no row keeps the backup's foreign origin id").isZero();
        assertThat(num(ctx -> ctx.fetchCount(chunks, text("c", "tenant_id").eq(tenant),
            DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, metadata, DSL.val("keep")).eq("yes"),
            originId.isNull()))).as("every tagged row lost the id key, the one whose name resolves included").isEqualTo(3);
        assertThat(num(ctx -> ctx.fetchCount(chunks, text("c", "tenant_id").eq(tenant),
            DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, metadata, DSL.val("origin_collection")).eq(origin))))
            .as("the name tag is kept as it was").isEqualTo(1);
    }

    @Test
    void aBackupTakenOnThisEngineRestoresWithEveryIdTheRestoredRegistryRowsAndNeverTheFiles() throws Exception {
        String source = "t-p31-fill-src";
        JsonNode first = env.restore(env.newTenant(source), fixture());
        assertThat(status(first)).as(first.toString()).isEqualTo("committed");
        ImportTestEnv.BackupFile taken = env.export(env.anotherToken(source));
        int chunkId = taken.columnIndex("chunks", "collection_id");
        assertThat(allRows(taken, "chunks").stream().filter(r -> r.fields().get(chunkId) != null).count())
            .as("non-vacuity: the file carries non-null twins").isPositive();
        java.util.Set<Long> fileIds = new java.util.HashSet<>();
        int registryId = taken.columnIndex("catalog_collections", "collection_id");
        allRows(taken, "catalog_collections").forEach(r -> fileIds.add(ByteBuffer.wrap(r.fields().get(registryId)).getLong()));

        String tenant = "t-p31-fill-thisengine";
        JsonNode end = env.restore(env.newTenant(tenant), taken);
        assertThat(status(end)).as(end.toString()).isEqualTo("committed");

        for (Site site : exportedSites()) {
            assertThat(num(ctx -> examined(ctx, tenant, site))).as("non-vacuity %s", site).isPositive();
            assertThat(num(ctx -> wrongIds(ctx, tenant, site))).as("rows of %s whose id is not the registry row's", site).isZero();
        }
        java.util.List<Long> restored = env.su(ctx -> ctx.select(DSL.field(DSL.name("collection_id"), Long.class))
            .from(DSL.table(DSL.name("nexus", "catalog_collections")))
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).fetch(0, Long.class));
        assertThat(restored).as("the registry's ids come from the target's sequence, never the file's")
            .isNotEmpty().doesNotContainAnyElementsOf(fileIds);
        assertThat(num(ctx -> CollectionTombstones.invariantViolations(ctx, tenant))).isZero();
    }

    // ── tombstones and the sequence ─────────────────────────────────────────────────────────────

    @Test
    void aTombstoneWhoseSuccessorIsInTheBackupPointsAtTheSuccessorsNewIdAndADanglingOneKeepsItsOwn() throws Exception {
        ImportTestEnv.BackupFile file = fixture();
        int nameAt = file.columnIndex("catalog_collections", "name");
        int supersededAt = file.columnIndex("catalog_collections", "superseded_by");
        List<BinaryCopyTestSupport.Row> registry = allRows(file, "catalog_collections");
        assertThat(registry.size()).as("the fixture's registry rows").isGreaterThanOrEqualTo(3);
        String tombstoned = fieldText(registry.get(0), nameAt);
        String successor = fieldText(registry.get(1), nameAt);
        String dangling = fieldText(registry.get(2), nameAt);
        ImportTestEnv.BackupFile edited = editRows(file, "catalog_collections", rows -> rows.stream().map(r -> {
            String name = fieldText(r, nameAt);
            if (name.equals(tombstoned)) return withField(r, supersededAt, utf8(successor));
            if (name.equals(dangling)) return withField(r, supersededAt, utf8("code__no-such-successor__voyage-code-3__v1"));
            return r;
        }).toList());

        String tenant = "t-p31-fill-tombstone";
        JsonNode end = env.restore(env.newTenant(tenant), edited);
        assertThat(status(end)).as(end.toString()).isEqualTo("committed");

        Map<String, Record> byName = env.su(ctx -> {
            var t = DSL.table(DSL.name("nexus", "catalog_collections"));
            Map<String, Record> out = new java.util.LinkedHashMap<>();
            ctx.select(DSL.field(DSL.name("name"), String.class), DSL.field(DSL.name("collection_id"), Long.class),
                    DSL.field(DSL.name("superseded_by"), String.class), DSL.field(DSL.name("superseded_by_id"), Long.class))
                .from(t).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant))
                .forEach(r -> out.put(r.get(0, String.class), r));
            return out;
        });
        Long successorId = byName.get(successor).get(1, Long.class);
        assertThat(byName.get(tombstoned).get(3, Long.class)).as("the tombstone points at its successor's NEW id").isEqualTo(successorId);
        assertThat(byName.get(tombstoned).get(3, Long.class)).isNotEqualTo(byName.get(tombstoned).get(1, Long.class));
        assertThat(byName.get(dangling).get(3, Long.class)).as("a successor absent from the backup leaves the row's own id")
            .isEqualTo(byName.get(dangling).get(1, Long.class));
        assertThat(byName.get(successor).get(3, Long.class)).as("a live row has no successor id").isNull();
        assertThat(num(ctx -> CollectionTombstones.invariantViolations(ctx, tenant)))
            .as("R7: every tombstone's id is its named row's id, or its own when none exists").isZero();
    }

    @Test
    void aMintAfterTheRestoreNeverCollidesWithARestoredId() throws Exception {
        String tenant = "t-p31-fill-sequence";
        JsonNode end = env.restore(env.newTenant(tenant), fixture());
        assertThat(status(end)).as(end.toString()).isEqualTo("committed");
        Long maxRestored = env.su(ctx -> ctx.select(DSL.max(DSL.field(DSL.name("collection_id"), Long.class)))
            .from(DSL.table(DSL.name("nexus", "catalog_collections"))).fetchOne(0, Long.class));
        Long next = env.su(ctx -> ctx.select(DSL.function("nextval", SQLDataType.BIGINT,
                DSL.function("pg_get_serial_sequence", SQLDataType.VARCHAR, DSL.val("nexus.catalog_collections"),
                    DSL.val("collection_id")).cast(new org.jooq.impl.DefaultDataType<>(org.jooq.SQLDialect.POSTGRES, Object.class, "regclass"))))
            .fetchOne(0, Long.class));
        assertThat(next).as("the sequence is ahead of every id the restore wrote").isGreaterThan(maxRestored);
    }
}
