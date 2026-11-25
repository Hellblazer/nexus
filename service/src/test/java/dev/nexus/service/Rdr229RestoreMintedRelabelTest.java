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
 * name column that names the collection, in a tombstone's {@code superseded_by}, in a chunk's {@code origin_collection}
 * tag and in a note's {@code nx-note://} {@code source_uri}. A label that is not of the minted form is kept. A later mint at
 * the old number's position then finds its label free.
 *
 * <p>Every case restores the committed old-release fixture (three registry rows) through the import routes, on a real
 * Postgres, after a byte-level edit: {@link #prepared} keeps the label of the registry row with the fewest rows in the file and
 * renames the other two to labels of the minted form wherever the file names them; it makes the kept row a tombstone whose
 * {@code superseded_by} names the first renamed row's old label, tags the first chunk's {@code origin_collection} with that
 * label, and gives the renamed collections' documents, aspects and highlights note URIs (and one {@code chroma://} and one
 * {@code nxexp://} value that must stay as they are).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229RestoreMintedRelabelTest {

    private static final Path LIST = Path.of("src", "main", "resources", "db", "rdr229", "p31-checks.tsv");
    private static final long OLD_BASE = 4812;
    private static final String MODEL = "bge-base-en-v15-768";   // the restored tenant's embedding profile for knowledge
    private static final String NOTE = "nx-note://";

    /** The tables whose {@code source_uri} carries a note's collection, with the column that names the row's collection. */
    private static final Map<String, String> URI_TABLES = Map.of("catalog_documents", "physical_collection",
        "document_aspects", "collection", "document_highlights", "collection");

    /** A name column, the id column beside it. */
    record Site(String table, String nameColumn, String idColumn) {
        @Override
        public String toString() {
            return table + "." + nameColumn;
        }
    }

    /** The labels (the file's) of the renamed rows, given the file positions of those rows in the registry, in order. */
    @FunctionalInterface
    private interface Labels {
        List<String> of(List<Integer> filePositions);
    }

    /**
     * The fixture with its minted-form names written in, and what the file held for each: {@code expected} the rows per name
     * site, {@code notes} the note-URI rows per table, both by the file's label; {@code untouched} the URIs that must survive.
     */
    private record Prepared(ImportTestEnv.BackupFile file, List<String> oldLabels, String plain,
                            Map<String, Map<Site, Long>> expected, Map<String, Map<String, List<String>>> notes,
                            List<String> untouched, List<Tuple> tuples) {
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
     * The fixture, prepared as the class javadoc says. {@code labels} names the minted-form label of each renamed row, in file
     * order; the kept row is the one with the fewest rows in the file, so the renamed ones carry the children.
     */
    private Prepared prepared(Labels labels) throws IOException {
        ImportTestEnv.BackupFile original = fixture();
        int nameAt = original.columnIndex("catalog_collections", "name");
        List<BinaryCopyTestSupport.Row> registry = rows(original, "catalog_collections");
        assertThat(registry.size()).as("the fixture's registry rows").isGreaterThanOrEqualTo(3);
        List<String> originals = registry.stream().map(r -> text(r, nameAt)).toList();
        int plainAt = 0;
        long fewest = Long.MAX_VALUE;
        for (int i = 0; i < originals.size(); i++) {
            long total = 0;
            for (Site s : sites()) total += countIn(original, s, originals.get(i));
            if (total <= fewest) {
                fewest = total;
                plainAt = i;
            }
        }
        String plain = originals.get(plainAt);
        List<Integer> positions = new ArrayList<>();
        for (int i = 0; i < originals.size(); i++) if (i != plainAt) positions.add(i);
        List<String> newLabels = labels.of(positions);
        assertThat(newLabels).hasSameSizeAs(positions).allMatch(CollectionLabel::isMintedForm).doesNotHaveDuplicates();

        Map<String, String> names = new LinkedHashMap<>();
        List<Tuple> tuples = new ArrayList<>();
        Map<String, Map<Site, Long>> expected = new LinkedHashMap<>();
        for (int k = 0; k < positions.size(); k++) {
            String old = originals.get(positions.get(k));
            names.put(old, newLabels.get(k));
            BinaryCopyTestSupport.Row r = registry.get(positions.get(k));
            tuples.add(new Tuple(text(r, original.columnIndex("catalog_collections", "content_type")),
                text(r, original.columnIndex("catalog_collections", "owner_id")),
                text(r, original.columnIndex("catalog_collections", "embedding_model")),
                text(r, original.columnIndex("catalog_collections", "model_version"))));
            Map<Site, Long> perSite = new LinkedHashMap<>();
            for (Site s : sites()) perSite.put(s, countIn(original, s, old));
            expected.put(newLabels.get(k), perSite);
        }
        String firstOriginal = names.keySet().iterator().next();
        String firstLabel = newLabels.get(0);

        int supersededAt = original.columnIndex("catalog_collections", "superseded_by");
        ImportTestEnv.BackupFile file = edit(original, "catalog_collections", rows -> rows.stream()
            .map(r -> plain.equals(text(r, nameAt)) ? with(r, supersededAt, utf8(firstOriginal)) : r).toList());
        for (Site s : sites()) file = rename(file, s.table(), s.nameColumn(), names);
        file = rename(file, "catalog_collections", "superseded_by", names);

        // the first chunk's origin tag names the first renamed collection (by its file label)
        int metadataAt = file.columnIndex("chunks", "metadata");
        int[] seen = {0};
        file = edit(file, "chunks", rows -> rows.stream().map(r -> {
            if (seen[0]++ != 0) return r;
            byte[] json = utf8("{\"origin_collection\": \"" + firstLabel + "\", \"origin_collection_id\": 9999991, \"keep\": \"yes\"}");
            byte[] jsonb = new byte[json.length + 1];
            jsonb[0] = 1;   // the binary format of jsonb: a version byte, then the text
            System.arraycopy(json, 0, jsonb, 1, json.length);
            return with(r, metadataAt, jsonb);
        }).toList());

        // note URIs: the renamed collections' rows get nx-note:// URIs; two documents get a URI the relabel must leave alone
        Set<String> renamed = new LinkedHashSet<>(newLabels);
        Map<String, Map<String, List<String>>> notes = new LinkedHashMap<>();
        List<String> untouched = new ArrayList<>();
        for (Map.Entry<String, String> e : URI_TABLES.entrySet()) {
            String table = e.getKey();
            if (!has(file, table, "source_uri") || !has(file, table, e.getValue())) continue;
            int uriAt = file.columnIndex(table, "source_uri");
            int collectionAt = file.columnIndex(table, e.getValue());
            int[] n = {0};
            file = edit(file, table, rows -> rows.stream().map(r -> {
                String label = text(r, collectionAt);
                if (label == null || !renamed.contains(label)) return r;
                int k = n[0]++;
                String uri;
                if (table.equals("catalog_documents") && k == 0) {
                    uri = "chroma://" + label + "/legacy";
                    untouched.add(uri);
                } else if (table.equals("catalog_documents") && k == 1) {
                    uri = "nxexp://p311/chroma://" + label + "/exported";
                    untouched.add(uri);
                } else {
                    String title = "title-" + table + "-" + k;
                    uri = NOTE + label + "/" + title;
                    notes.computeIfAbsent(label, x -> new LinkedHashMap<>()).computeIfAbsent(table, x -> new ArrayList<>()).add(title);
                }
                return with(r, uriAt, utf8(uri));
            }).toList());
        }
        return new Prepared(file, List.copyOf(newLabels), plain, expected, notes, untouched, tuples);
    }

    private Prepared defaultPrepared() throws IOException {
        return prepared(positions -> {
            List<String> out = new ArrayList<>();
            for (int k = 0; k < positions.size(); k++) out.add("c" + (OLD_BASE + k));
            return out;
        });
    }

    /**
     * Labels that make the target's new ids the file's other labels: with the sequence at {@code s}, the row at file position
     * {@code p} is drawn {@code s + 1 + p}, so the first renamed row is labelled with the second's new id and the second with
     * the first's (a swap); any further row is labelled far away.
     */
    private static Labels swapped(long s) {
        return positions -> {
            List<String> out = new ArrayList<>();
            out.add("c" + (s + 1 + positions.get(1)));
            out.add("c" + (s + 1 + positions.get(0)));
            for (int k = 2; k < positions.size(); k++) out.add("c" + (s + 1000 + k));
            return out;
        };
    }

    /** As {@link #swapped}, but the first renamed row is labelled with its own new id: a relabel that changes nothing. */
    private static Labels firstIsItsOwnNewId(long s) {
        return positions -> {
            List<String> out = new ArrayList<>();
            out.add("c" + (s + 1 + positions.get(0)));
            for (int k = 1; k < positions.size(); k++) out.add("c" + (s + 1000 + k));
            return out;
        };
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

    /** The exact {@code source_uri} values of {@code table} that open with {@code prefix}. */
    private java.util.Set<String> noteUris(String tenant, String table, String prefix) {
        return env.su(ctx -> new java.util.TreeSet<>(ctx.select(txt("source_uri")).from(DSL.table(DSL.name("nexus", table)).as("t"))
            .where(txt("tenant_id").eq(tenant), txt("source_uri").startsWith(prefix)).fetchSet(txt("source_uri"))));
    }

    private long uriRows(String tenant, String table, String prefix) {
        return env.su(ctx -> (long) ctx.fetchCount(DSL.table(DSL.name("nexus", table)).as("t"),
            txt("tenant_id").eq(tenant), txt("source_uri").startsWith(prefix)));
    }

    private long uriRowsEqual(String tenant, String table, String uri) {
        return env.su(ctx -> (long) ctx.fetchCount(DSL.table(DSL.name("nexus", table)).as("t"),
            txt("tenant_id").eq(tenant), txt("source_uri").eq(uri)));
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

    private void setSequence(long value) {
        env.su(ctx -> ctx.select(DSL.function("setval", Long.class, sequence(), DSL.val(value))).fetchOne());
    }

    /** The restored row of each renamed collection, in {@code p.oldLabels()} order. */
    private List<Restored> renamedRows(String tenant, Prepared p) {
        Map<String, Restored> byTuple = restoredRows(tenant);
        assertThat(byTuple).as("every registry row of the file restored").hasSize(p.tuples().size() + 1);
        List<Restored> out = new ArrayList<>();
        for (Tuple t : p.tuples()) {
            assertThat(byTuple).containsKey(t.toString());
            out.add(byTuple.get(t.toString()));
        }
        return out;
    }

    /**
     * Everything the relabel owes a restored tenant, whatever the labels were: each renamed row is {@code "c" + its id}; each name
     * column names it on that id, with the rows the file held; the tombstone, the origin tag and the note URIs follow it; and the
     * report names the labels that changed and no other. {@code oldLabelsGone}: no row names a file label any more (not so when
     * the file's labels are other rows' new ones).
     */
    private void assertRelabelled(String tenant, String token, Prepared p, JsonNode end, boolean oldLabelsGone) throws Exception {
        List<Restored> rows = renamedRows(tenant, p);
        long minted = restoredRows(tenant).values().stream().filter(r -> CollectionLabel.isMintedForm(r.label())).count();
        assertThat(minted).as("non-vacuity: every renamed row is of the minted form after the restore").isEqualTo(rows.size());

        long examined = 0;
        long chunks = 0;
        long manifest = 0;
        long topics = 0;
        for (int i = 0; i < rows.size(); i++) {
            String old = p.oldLabels().get(i);
            Restored row = rows.get(i);
            assertThat(row.label()).as("the row restored from %s is c + its id", old).isEqualTo("c" + row.id());
            if (oldLabelsGone) assertThat(row.label()).as("the row restored from %s is relabelled", old).isNotEqualTo(old);
            for (Map.Entry<Site, Long> e : p.expected().get(old).entrySet()) {
                Site s = e.getKey();
                assertThat(rowsNamed(tenant, s, row.label())).as("rows of %s naming %s (from %s)", s, row.label(), old).isEqualTo(e.getValue());
                assertThat(rowsNamedOnAnotherId(tenant, s, row)).as("rows of %s naming %s on another id", s, row.label()).isZero();
                if (oldLabelsGone) assertThat(rowsNamed(tenant, s, old)).as("rows of %s still naming %s", s, old).isZero();
                examined += e.getValue();
                if (s.table().equals("chunks")) chunks += e.getValue();
                if (s.table().equals("catalog_document_chunks")) manifest += e.getValue();
                if (s.table().equals("topics")) topics += e.getValue();
            }
        }
        assertThat(chunks).as("non-vacuity: chunks of the renamed collections").isPositive();
        assertThat(manifest).as("non-vacuity: manifest rows of the renamed collections").isPositive();
        assertThat(topics).as("non-vacuity: topics of the renamed collections").isPositive();
        assertThat(examined).isGreaterThan(rows.size());

        // a label that is not of the minted form is kept; the tombstone follows its successor's new label and id
        Restored successor = rows.get(0);
        var plainRow = env.su(ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_BY_ID)
            .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                .and(CATALOG_COLLECTIONS.NAME.eq(p.plain()))).fetchOne());
        assertThat(plainRow).as("the non-minted label is untouched").isNotNull();
        assertThat(plainRow.value1()).as("a minted label in superseded_by follows the successor").isEqualTo(successor.label());
        assertThat(plainRow.value2()).as("and its superseded_by_id is the successor's new id").isEqualTo(successor.id());
        assertThat(num(ctx -> CollectionTombstones.invariantViolations(ctx, tenant))).as("R7").isZero();

        // the chunk's origin tag names the new label of the first renamed collection; its id key, one the file's registry does
        // not hold (this file has no ids), is stripped and never resolved by the name (P3.11's rule, Rdr229RestoreIdFillTest);
        // every other key is kept
        Field<JSONB> metadata = DSL.field(DSL.name("c", "metadata"), SQLDataType.JSONB);
        var chunkTable = DSL.table(DSL.name("nexus", "chunks")).as("c");
        Field<String> origin = DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, metadata, DSL.val("origin_collection"));
        Field<String> originId = DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, metadata, DSL.val("origin_collection_id"));
        Field<String> keep = DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, metadata, DSL.val("keep"));
        Field<String> chunkTenant = DSL.field(DSL.name("c", "tenant_id"), String.class);
        assertThat(num(ctx -> ctx.fetchCount(chunkTable, chunkTenant.eq(tenant), keep.eq("yes"))))
            .as("non-vacuity: the tagged chunk restored").isEqualTo(1);
        assertThat(num(ctx -> ctx.fetchCount(chunkTable, chunkTenant.eq(tenant), keep.eq("yes"), origin.eq(successor.label()),
            originId.isNull())))
            .as("the tag names the new label of the relabelled collection and no longer carries the source's id").isEqualTo(1);

        // note URIs follow their collection: the rows the file held under each old label are under the new one, in one pass
        long noteRows = 0;
        java.util.Set<String> tablesWithNotes = new java.util.TreeSet<>();
        for (int i = 0; i < rows.size(); i++) {
            for (Map.Entry<String, List<String>> e : p.notes().getOrDefault(p.oldLabels().get(i), Map.of()).entrySet()) {
                java.util.Set<String> want = new java.util.TreeSet<>();
                String label = rows.get(i).label();
                e.getValue().forEach(title -> want.add(NOTE + label + "/" + title));
                assertThat(noteUris(tenant, e.getKey(), NOTE + rows.get(i).label() + "/"))
                    .as("the nx-note URIs of %s in %s (from %s)", e.getKey(), rows.get(i).label(), p.oldLabels().get(i))
                    .isEqualTo(want);
                noteRows += want.size();
                tablesWithNotes.add(e.getKey());
            }
        }
        assertThat(tablesWithNotes).as("non-vacuity: the tables whose note URIs the restore carried")
            .containsExactlyInAnyOrder("catalog_documents", "document_aspects", "document_highlights");
        assertThat(p.untouched()).as("non-vacuity: a chroma:// and an nxexp:// URI").hasSize(2);
        for (String uri : p.untouched()) {
            assertThat(uriRowsEqual(tenant, "catalog_documents", uri)).as("%s is left as it was", uri).isEqualTo(1);
        }
        // a re-put of a note's title finds the row by its URI (documentsBySourceUri, the lookup resolve_or_register uses)
        Restored withNotes = null;
        for (int i = 0; i < rows.size(); i++) {
            if (p.notes().getOrDefault(p.oldLabels().get(i), Map.of()).containsKey("catalog_documents")) withNotes = rows.get(i);
        }
        assertThat(withNotes).as("non-vacuity: a renamed collection holds a note document").isNotNull();
        String noteLabel = withNotes.label();
        var note = env.su(ctx -> ctx.select(DSL.field(DSL.name("t", "tumbler"), String.class), txt("source_uri"))
            .from(DSL.table(DSL.name("nexus", "catalog_documents")).as("t"))
            .where(txt("tenant_id").eq(tenant), txt("source_uri").startsWith(NOTE + noteLabel + "/")).limit(1).fetchOne());
        assertThat(note).as("a note of a renamed collection restored").isNotNull();
        var found = env.call("GET", "/v1/catalog/list?source_uri=" + ImportTestEnv.enc(note.value2()), token);
        assertThat(found.statusCode()).as(found.body()).isEqualTo(200);
        assertThat(ImportTestEnv.json(found).get("documents").toString()).as("the note is found by its new-label URI")
            .contains(note.value1());
        assertThat(noteRows).isPositive();

        // the restore marker (what the engine keeps after it forgets the session) lists the same relabels, with their count
        JsonNode marker = ImportTestEnv.json(env.call("GET", "/v1/tenant/import-marker", token)).get("marker");
        JsonNode reported = end.get("report").get("collections_relabelled");
        assertThat(marker.get("collections_relabelled")).as("the marker lists the relabels the report does").isEqualTo(reported);
        assertThat(marker.get("collections_relabelled_count").asInt()).isEqualTo(reported.size()).isPositive();

        // the report names each label that changed, and no other
        List<Restored> changed = new ArrayList<>();
        for (int i = 0; i < rows.size(); i++) if (!rows.get(i).label().equals(p.oldLabels().get(i))) changed.add(rows.get(i));
        JsonNode relabelled = end.get("report").get("collections_relabelled");
        assertThat(relabelled).as("the report lists the relabelled collections: " + end.get("report")).isNotNull();
        assertThat(relabelled.size()).as("one entry per label that changed: " + relabelled).isEqualTo(changed.size());
        for (int i = 0; i < rows.size(); i++) {
            boolean listed = false;
            for (JsonNode n : relabelled) {
                listed |= n.path("label").asText().equals(p.oldLabels().get(i)) && n.path("target_label").asText().equals(rows.get(i).label());
            }
            assertThat(listed).as("the report names %s -> %s (only when it changed)", p.oldLabels().get(i), rows.get(i).label())
                .isEqualTo(!rows.get(i).label().equals(p.oldLabels().get(i)));
        }
    }

    // ── the cases ───────────────────────────────────────────────────────────────────────────────

    @Test
    void aRestoredMintedLabelTakesTheMintedLabelOfItsNewIdEverywhereAndALaterMintAtTheOldNumberSucceeds() throws Exception {
        Prepared p = defaultPrepared();
        String tenant = "t-p311-relabel-main";
        String token = env.newTenant(tenant);
        JsonNode end = env.restore(token, p.file());
        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");
        assertRelabelled(tenant, token, p, end, true);
        for (String old : p.oldLabels()) {
            assertThat(uriRows(tenant, "catalog_documents", NOTE + old + "/")).as("no note is left under the old label " + old).isZero();
        }

        // a later mint at the OLD number's position: the draw lands on it and its label is free
        setSequence(OLD_BASE - 1);
        String owner = "p311-relabel-mint-owner";
        var minting = env.callJson("POST", "/v1/catalog/collections/upsert", token,
            "{\"content_type\":\"knowledge\",\"owner_id\":\"" + owner + "\",\"embedding_model\":\"" + MODEL
                + "\",\"model_version\":\"v1\"}");
        assertThat(minting.statusCode()).as(minting.body()).isEqualTo(200);
        JsonNode mint = ImportTestEnv.json(minting);
        assertThat(mint.get("collection_id").asLong()).as("the mint drew the old number's id, no label in its way").isEqualTo(OLD_BASE);
        assertThat(mint.get("name").asText()).isEqualTo("c" + OLD_BASE);
    }

    /**
     * A backup from another installation: its minted labels are numbers the target's sequence is about to draw, so a label the
     * file holds is the new label of ANOTHER row (here a swap of the two renamed rows). The relabel is one pass per column and
     * per URI; a second pass would rename the rows it had just renamed.
     */
    @Test
    void aBackupWhoseMintedLabelsAreAnotherRowsNewIdsIsRelabelledInOnePass() throws Exception {
        long s = 7000;
        Prepared p = prepared(swapped(s));
        String tenant = "t-p311-relabel-overlap";
        String token = env.newTenant(tenant);
        setSequence(s);
        JsonNode end = env.restore(token, p.file());
        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");

        List<Restored> rows = renamedRows(tenant, p);
        // non-vacuity: a new label is a DIFFERENT row's old one, and that row's old label is not its new one (not the identity)
        long crossing = 0;
        for (int i = 0; i < rows.size(); i++) {
            int other = p.oldLabels().indexOf(rows.get(i).label());
            if (other >= 0 && other != i && !p.oldLabels().get(i).equals(rows.get(i).label())) crossing++;
        }
        assertThat(crossing).as("rows whose new label is another row's old label: %s -> %s", p.oldLabels(), rows).isEqualTo(2);
        assertRelabelled(tenant, token, p, end, false);
    }

    /** A label that already is {@code "c" + new id} is left as it is, and is not reported as a relabel. */
    @Test
    void aMintedLabelThatAlreadyEqualsItsNewIdIsKeptAndNotReported() throws Exception {
        long s = 9000;
        Prepared p = prepared(firstIsItsOwnNewId(s));
        String tenant = "t-p311-relabel-identity";
        String token = env.newTenant(tenant);
        setSequence(s);
        JsonNode end = env.restore(token, p.file());
        assertThat(end.get("status").asText()).as(end.toString()).isEqualTo("committed");

        List<Restored> rows = renamedRows(tenant, p);
        assertThat(rows.get(0).label()).as("non-vacuity: the first row's label is its new id's").isEqualTo(p.oldLabels().get(0));
        assertThat(rows.get(1).label()).isNotEqualTo(p.oldLabels().get(1));
        assertRelabelled(tenant, token, p, end, false);
    }
}
