// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.db.CollectionLabel;
import dev.nexus.service.db.CollectionTombstones;
import dev.nexus.service.db.TenantExport;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.DefaultDataType;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.UnaryOperator;

/**
 * RDR-229 P3.11 (nexus-p2m1x.31): a tenant restore gives every restored registry row a NEW {@code collection_id} from the
 * sequence and keeps the file's label, so a label of the minted form ({@code c<digits>}) stopped equalling {@code "c" + id}.
 * A minted-form label is the SOURCE install's own (RDR-229 item 10, the recovery bundle's {@code collections_relabelled}),
 * so the restore relabels it to {@link CollectionLabel#mintedLabel} of the row's new id, in the registry and in every
 * name column that names the collection, in a tombstone's {@code superseded_by}, and in a chunk's {@code origin_collection}
 * tag. A label that is not of the minted form is kept. A later mint at the old number's position then finds its label free.
 *
 * <p>Every case restores the committed old-release fixture with the names of all but its last registry row rewritten at the
 * byte level to labels of the minted form, through the import routes, on a real Postgres.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229RestoreMintedRelabelTest {

    private static final Path LIST = Path.of("src", "main", "resources", "db", "rdr229", "p31-checks.tsv");
    private static final long OLD_BASE = 4812;
    private static final String MODEL = "voyage-context-3";

    /** A name column, the id column beside it. */
    record Site(String table, String nameColumn, String idColumn) {
        @Override
        public String toString() {
            return table + "." + nameColumn;
        }
    }

    /** The fixture with its minted-form names written in, and what the file held for each. */
    private record Prepared(ImportTestEnv.BackupFile file, List<String> oldLabels, String plain,
                            Map<String, Map<Site, Long>> expected, List<Tuple> tuples) {
    }

    /** The attributes that identify a registry row across a restore (the name and the id both change). */
    private record Tuple(String contentType, String ownerId, String embeddingModel, String modelVersion) {
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

    // ── the file ────────────────────────────────────────────────────────────────────────────────

    private static ImportTestEnv.BackupFile fixture() throws IOException {
        try (var in = Rdr229RestoreMintedRelabelTest.class.getResourceAsStream(TenantImportFixtureTest.RESOURCE)) {
            assertThat(in).as("the committed fixture").isNotNull();
            return TenantBackupFiles.read(in.readAllBytes());
        }
    }

    /** The NAME_ID and HISTORY rows of the expand's list whose table a backup file carries, and the registry's own name. */
    private static List<Site> sites() throws IOException {
        Set<Site> sites = new LinkedHashSet<>();
        sites.add(new Site("catalog_collections", "name", "collection_id"));
        for (String line : Files.readAllLines(LIST)) {
            if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) continue;
            String[] f = line.split("\t");
            if (!(f[4].equals("NAME_ID") || f[4].equals("HISTORY")) || TenantExport.exportable(f[0]).isEmpty()) continue;
            sites.add(new Site(f[0], f[1], f[2]));
        }
        return List.copyOf(sites);
    }

    private static boolean has(ImportTestEnv.BackupFile file, String table, String column) {
        if (!file.tableNames().contains(table)) return false;
        for (JsonNode c : file.table(table).withArray("columns")) {
            if (column.equals(c.path("name").asText())) return true;
        }
        return false;
    }

    private static byte[] utf8(String s) {
        return s.getBytes(StandardCharsets.UTF_8);
    }

    private static String text(BinaryCopyTestSupport.Row row, int index) {
        byte[] f = row.fields().get(index);
        return f == null ? null : new String(f, StandardCharsets.UTF_8);
    }

    private static BinaryCopyTestSupport.Row with(BinaryCopyTestSupport.Row row, int index, byte[] value) {
        List<byte[]> f = new ArrayList<>(row.fields());
        f.set(index, value);
        return new BinaryCopyTestSupport.Row(f);
    }

    private static List<BinaryCopyTestSupport.Row> rows(ImportTestEnv.BackupFile file, String table) {
        List<BinaryCopyTestSupport.Row> rows = new ArrayList<>();
        file.pages.getOrDefault(table, List.of()).forEach(p -> rows.addAll(BinaryCopyTestSupport.parse(p).rows()));
        return rows;
    }

    private static ImportTestEnv.BackupFile edit(ImportTestEnv.BackupFile file, String table,
                                                 UnaryOperator<List<BinaryCopyTestSupport.Row>> edit) {
        ImportTestEnv.BackupFile c = file.copy();
        c.pages.computeIfPresent(table, (k, pages) -> pages.stream().map(p -> {
            BinaryCopyTestSupport.Stream s = BinaryCopyTestSupport.parse(p);
            return BinaryCopyTestSupport.encode(new BinaryCopyTestSupport.Stream(s.header(), edit.apply(s.rows())));
        }).toList());
        return c.seal();
    }

    /** The file with every value of {@code column} of {@code table} that is a key of {@code names} replaced by its value. */
    private static ImportTestEnv.BackupFile rename(ImportTestEnv.BackupFile file, String table, String column,
                                                   Map<String, String> names) {
        if (!has(file, table, column)) return file;
        int at = file.columnIndex(table, column);
        return edit(file, table, rows -> rows.stream().map(r -> {
            String v = text(r, at);
            return v != null && names.containsKey(v) ? with(r, at, utf8(names.get(v))) : r;
        }).toList());
    }

    private static long countIn(ImportTestEnv.BackupFile file, Site site, String name) {
        if (!has(file, site.table(), site.nameColumn())) return 0;
        int at = file.columnIndex(site.table(), site.nameColumn());
        return rows(file, site.table()).stream().filter(r -> name.equals(text(r, at))).count();
    }

    /**
     * The fixture with every registry row but the last renamed to {@code c<4812 + i>} wherever the file names it, and the last
     * row (its label stays as it was) made a tombstone whose {@code superseded_by} names the first row's ORIGINAL label.
     */
    private Prepared prepared() throws IOException {
        ImportTestEnv.BackupFile original = fixture();
        int nameAt = original.columnIndex("catalog_collections", "name");
        List<BinaryCopyTestSupport.Row> registry = rows(original, "catalog_collections");
        assertThat(registry.size()).as("the fixture's registry rows").isGreaterThanOrEqualTo(3);
        List<String> originals = registry.stream().map(r -> text(r, nameAt)).toList();
        String plain = originals.get(originals.size() - 1);
        Map<String, String> names = new LinkedHashMap<>();
        for (int i = 0; i < originals.size() - 1; i++) names.put(originals.get(i), "c" + (OLD_BASE + i));
        assertThat(names.values()).allMatch(CollectionLabel::isMintedForm);

        Map<String, Map<Site, Long>> expected = new LinkedHashMap<>();
        for (String old : names.keySet()) {
            Map<Site, Long> perSite = new LinkedHashMap<>();
            for (Site s : sites()) perSite.put(s, countIn(original, s, old));
            expected.put(names.get(old), perSite);
        }
        List<Tuple> tuples = new ArrayList<>();
        for (BinaryCopyTestSupport.Row r : registry.subList(0, registry.size() - 1)) {
            tuples.add(new Tuple(text(r, original.columnIndex("catalog_collections", "content_type")),
                text(r, original.columnIndex("catalog_collections", "owner_id")),
                text(r, original.columnIndex("catalog_collections", "embedding_model")),
                text(r, original.columnIndex("catalog_collections", "model_version"))));
        }

        int supersededAt = original.columnIndex("catalog_collections", "superseded_by");
        ImportTestEnv.BackupFile file = edit(original, "catalog_collections", rows -> rows.stream()
            .map(r -> plain.equals(text(r, nameAt)) ? with(r, supersededAt, utf8(originals.get(0))) : r).toList());
        for (Site s : sites()) file = rename(file, s.table(), s.nameColumn(), names);
        file = rename(file, "catalog_collections", "superseded_by", names);
        return new Prepared(file, List.copyOf(names.values()), plain, expected, tuples);
    }

    // ── the target ──────────────────────────────────────────────────────────────────────────────

    private record Restored(String label, long id) {
    }

    private Map<String, Restored> restoredRows(String tenant) {
        return env.su(ctx -> {
            Map<String, Restored> out = new LinkedHashMap<>();
            ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID, CATALOG_COLLECTIONS.CONTENT_TYPE,
                    CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION)
                .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).forEach(r ->
                    out.put(new Tuple(r.value3(), r.value4(), r.value5(), r.value6()).toString(),
                        new Restored(r.value1(), r.value2())));
            return out;
        });
    }

    private static Field<String> txt(String column) {
        return DSL.field(DSL.name("t", column), SQLDataType.VARCHAR);
    }

    private long rowsNamed(String tenant, Site site, String name) {
        return env.su(ctx -> (long) ctx.fetchCount(DSL.table(DSL.name("nexus", site.table())).as("t"),
            txt("tenant_id").eq(tenant), txt(site.nameColumn()).eq(name)));
    }

    private long rowsNamedOnAnotherId(String tenant, Site site, Restored row) {
        return env.su(ctx -> (long) ctx.fetchCount(DSL.table(DSL.name("nexus", site.table())).as("t"),
            txt("tenant_id").eq(tenant), txt(site.nameColumn()).eq(row.label()),
            DSL.field(DSL.name("t", site.idColumn()), SQLDataType.BIGINT).isDistinctFrom(row.id())));
    }

    /** A number read as the superuser; a named type keeps AssertJ from reading the lambda as a Predicate. */
    private long num(java.util.function.Function<DSLContext, ? extends Number> read) {
        Number n = env.su(ctx -> read.apply(ctx));
        return n.longValue();
    }

    private static Field<Object> sequence() {
        return DSL.function("pg_get_serial_sequence", String.class, DSL.val("nexus.catalog_collections"), DSL.val("collection_id"))
            .cast(new DefaultDataType<>(SQLDialect.POSTGRES, Object.class, "regclass"));
    }

    // ── the cases ───────────────────────────────────────────────────────────────────────────────

    @Test
    void aRestoredMintedLabelTakesTheMintedLabelOfItsNewIdEverywhereAndALaterMintAtTheOldNumberSucceeds() throws Exception {
        Prepared p = prepared();
        String tenant = "t-p311-relabel-main";
        String token = env.newTenant(tenant);
        JsonNode end = env.restore(token, p.file());
        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");

        Map<String, Restored> byTuple = restoredRows(tenant);
        assertThat(byTuple).as("every registry row of the file restored").hasSize(p.tuples().size() + 1);

        // the registry: label == "c" + its NEW id, for every minted-form row, and for each renamed row
        long minted = byTuple.values().stream().filter(r -> CollectionLabel.isMintedForm(r.label())).count();
        assertThat(minted).as("non-vacuity: every renamed row is of the minted form after the restore").isEqualTo(p.tuples().size());
        for (Restored r : byTuple.values()) {
            if (CollectionLabel.isMintedForm(r.label())) {
                assertThat(r.label()).as("a minted label is c + its collection_id").isEqualTo("c" + r.id());
            }
        }

        // every name column: the rows of each collection name the NEW label and sit on the new id; none names the old one
        long examined = 0;
        long chunks = 0;
        long manifest = 0;
        long topics = 0;
        List<Map.Entry<String, Map<Site, Long>>> byOld = new ArrayList<>(p.expected().entrySet());
        for (int i = 0; i < byOld.size(); i++) {
            String old = byOld.get(i).getKey();
            Restored row = byTuple.get(p.tuples().get(i).toString());
            assertThat(row).as("the restored row of %s", old).isNotNull();
            assertThat(row.label()).as("the restored row of %s is relabelled", old).isEqualTo("c" + row.id()).isNotEqualTo(old);
            for (Map.Entry<Site, Long> e : byOld.get(i).getValue().entrySet()) {
                Site s = e.getKey();
                assertThat(rowsNamed(tenant, s, row.label())).as("rows of %s naming the new label of %s", s, old).isEqualTo(e.getValue());
                assertThat(rowsNamedOnAnotherId(tenant, s, row)).as("rows of %s naming %s on another id", s, row.label()).isZero();
                assertThat(rowsNamed(tenant, s, old)).as("rows of %s still naming the old label %s", s, old).isZero();
                examined += e.getValue();
                if (s.table().equals("chunks")) chunks += e.getValue();
                if (s.table().equals("catalog_document_chunks")) manifest += e.getValue();
                if (s.table().equals("topics")) topics += e.getValue();
            }
        }
        assertThat(chunks).as("non-vacuity: chunks of the renamed collections").isPositive();
        assertThat(manifest).as("non-vacuity: manifest rows of the renamed collections").isPositive();
        assertThat(topics).as("non-vacuity: topics of the renamed collections").isPositive();
        assertThat(examined).isGreaterThan(p.tuples().size());

        // a label that is not of the minted form is kept, and the tombstone follows its successor's new label and id
        Restored successor = byTuple.get(p.tuples().get(0).toString());
        var plainRow = env.su(ctx -> ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.SUPERSEDED_BY,
                CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CATALOG_COLLECTIONS.COLLECTION_ID)
            .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                .and(CATALOG_COLLECTIONS.NAME.eq(p.plain()))).fetchOne());
        assertThat(plainRow).as("the non-minted label is untouched").isNotNull();
        assertThat(plainRow.value2()).as("a minted label in superseded_by follows the successor").isEqualTo(successor.label());
        assertThat(plainRow.value3()).as("and its superseded_by_id is the successor's new id").isEqualTo(successor.id());
        assertThat(num(ctx -> CollectionTombstones.invariantViolations(ctx, tenant))).as("R7").isZero();

        // the report names each relabel
        JsonNode relabelled = end.get("report").get("collections_relabelled");
        assertThat(relabelled).as("the report lists the relabelled collections: " + end.get("report")).isNotNull();
        assertThat(relabelled.size()).isEqualTo(p.tuples().size());
        for (int i = 0; i < byOld.size(); i++) {
            Restored row = byTuple.get(p.tuples().get(i).toString());
            boolean listed = false;
            for (JsonNode n : relabelled) {
                listed |= n.path("label").asText().equals(byOld.get(i).getKey())
                    && n.path("target_label").asText().equals(row.label());
            }
            assertThat(listed).as("the report names %s -> %s", byOld.get(i).getKey(), row.label()).isTrue();
        }

        // a later mint at the OLD number's position: the draw lands on it and its label is free
        env.su(ctx -> ctx.select(DSL.function("setval", Long.class, sequence(), DSL.val(OLD_BASE - 1))).fetchOne());
        String owner = "p311-relabel-mint-owner";
        var minting = env.callJson("POST", "/v1/catalog/collections/upsert", token,
            "{\"content_type\":\"knowledge\",\"owner_id\":\"" + owner + "\",\"embedding_model\":\"" + MODEL
                + "\",\"model_version\":\"v1\"}");
        assertThat(minting.statusCode()).as(minting.body()).isEqualTo(200);
        JsonNode mint = ImportTestEnv.json(minting);
        assertThat(mint.get("collection_id").asLong()).as("the mint drew the old number's id, no label in its way").isEqualTo(OLD_BASE);
        assertThat(mint.get("name").asText()).isEqualTo("c" + OLD_BASE);
    }

    @Test
    void aChunksOriginCollectionTagNamingARelabelledCollectionFollowsItToItsNewLabelAndId() throws Exception {
        Prepared p = prepared();
        ImportTestEnv.BackupFile file = p.file();
        String oldLabel = p.oldLabels().get(0);
        int metadataAt = file.columnIndex("chunks", "metadata");
        int[] seen = {0};
        ImportTestEnv.BackupFile tagged = edit(file, "chunks", rows -> rows.stream().map(r -> {
            if (seen[0]++ != 0) return r;
            byte[] json = utf8("{\"origin_collection\": \"" + oldLabel + "\", \"origin_collection_id\": 9999991, \"keep\": \"yes\"}");
            byte[] jsonb = new byte[json.length + 1];
            jsonb[0] = 1;   // the binary format of jsonb: a version byte, then the text
            System.arraycopy(json, 0, jsonb, 1, json.length);
            return with(r, metadataAt, jsonb);
        }).toList());

        String tenant = "t-p311-relabel-origin";
        JsonNode end = env.restore(env.newTenant(tenant), tagged);
        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");

        Restored row = restoredRows(tenant).get(p.tuples().get(0).toString());
        assertThat(row.label()).as("the collection the tag names is relabelled").isEqualTo("c" + row.id()).isNotEqualTo(oldLabel);
        Field<JSONB> metadata = DSL.field(DSL.name("c", "metadata"), SQLDataType.JSONB);
        var chunks = DSL.table(DSL.name("nexus", "chunks")).as("c");
        Field<String> origin = DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, metadata, DSL.val("origin_collection"));
        Field<String> originId = DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, metadata, DSL.val("origin_collection_id"));
        Field<String> keep = DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, metadata, DSL.val("keep"));
        assertThat(num(ctx -> ctx.fetchCount(chunks, DSL.field(DSL.name("c", "tenant_id"), String.class).eq(tenant),
            keep.eq("yes")))).as("non-vacuity: the tagged chunk restored").isEqualTo(1);
        assertThat(num(ctx -> ctx.fetchCount(chunks, DSL.field(DSL.name("c", "tenant_id"), String.class).eq(tenant),
            keep.eq("yes"), origin.eq(row.label()), originId.eq(Long.toString(row.id())))))
            .as("the tag names the new label and the new id of the relabelled collection").isEqualTo(1);
    }
}
