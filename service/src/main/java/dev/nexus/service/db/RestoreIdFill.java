// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantImportLoad.LoadFailedException;
import dev.nexus.service.db.TenantImportPlan.TablePlan;
import dev.nexus.service.db.TenantImportReport.Relabel;
import dev.nexus.service.db.TenantImportStaging.Stage;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record2;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.DefaultDataType;
import org.jooq.impl.SQLDataType;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17) and P3.11 (nexus-p2m1x.31): the ids a restore writes beside the collection names. P3.1's NOT VALID
 * CHECKs refuse every row with a non-empty name and a NULL id, and a backup written before the id columns existed holds exactly
 * such rows, so the load fills each id on the stage, before the table's insert. {@code ''} means unset and gives NULL. History
 * tables ({@code relevance_log}, {@code search_telemetry}) are filled the same way but an id that resolves to nothing is not
 * refused: they have no CHECK and keep a label.
 *
 * <p><b>The rule, per staged row and per (name column, id column) pair.</b> A file's id is the SOURCE installation's, so it is
 * never written as it is; it is mapped.
 * <ul>
 *   <li>The twin is non-null: the id is {@link CollectionIdMap} of it, by id. From P3.7 a rename is one registry row's update
 *       and the children's name columns are stale until P4.1, so the name of such a row may name no collection, or another one,
 *       and is not read. A twin the map does not hold is refused ({@link #ORPHAN_ID_CODE}), except in a history table, where it
 *       gives NULL.</li>
 *   <li>The twin is NULL, or the file has no such column (a file from before P3.1, or a row stamped late): the id is the
 *       restored registry row's on {@code (tenant_id, name)}, by name. The name is valid there because the CHECKs of P3.1
 *       refuse a writer a row with a name and no id, so a NULL twin beside a name only exists before P3.1's stamp completes,
 *       which is before P3.7's rename can stale a name. A file that contradicts this (a NULL-twin row whose name another row of
 *       the table carries with the id of a different collection) is refused as {@link #AMBIGUOUS_CODE}. An empty name gives NULL.
 *       A history table's NULL twin is filled by name only when the file's registry carries no id at all (a file from before
 *       P3.1); otherwise it stays NULL, because a history label may be an old one.</li>
 * </ul>
 * A row whose twin was mapped also takes the mapped registry row's CURRENT name in its name column, so a stale label never
 * reaches a name foreign key that the target still has (the name-key drop move may not have completed there), and the chunk
 * family's name keys agree with the rewritten chunk names.
 *
 * <p><b>The registry.</b> {@code catalog_collections.collection_id} is never taken from a file: the engine draws every
 * restored row's id from the column's own sequence ({@code nextval(pg_get_serial_sequence(...))}), so a later mint cannot
 * collide. A row cannot name its own identity in the INSERT that assigns it, so the id is drawn on the stage first and
 * inserted explicitly, a tombstone's as both {@code collection_id} and {@code superseded_by_id} (the CHECK refuses a
 * tombstone with a NULL successor id). Before the draw overwrites them, the file's {@code collection_id} and
 * {@code superseded_by_id} are saved to stage columns ({@link #SRC_ID}, {@link #SRC_SUCCESSOR}); the pair
 * (saved id, drawn id) is the {@link CollectionIdMap}. {@link #repointTombstones} then, after every registry row is in and
 * before any child table loads, points each tombstone whose file successor id is mapped at that row's new id, one whose file
 * successor id is NULL at the restored row of its {@code superseded_by} name (the R7 invariant), and leaves any other on its own
 * id (a successor absent from the backup).
 *
 * <p><b>Relabel (RDR-229 item 10).</b> A label of the minted form ({@link CollectionLabel#isMintedForm}, {@code c<digits>}) is the
 * SOURCE install's own: it was {@code "c" + id} there, and the restore gives the row a new id. So {@link #relabelRegistry} rewrites
 * each such registry row's {@code name} (and any {@code superseded_by} that names it) to {@link CollectionLabel#mintedLabel} of the
 * new id, keeping {@code label == "c" + collection_id} (the minter's invariant), and {@link #relabelNames} carries the same
 * old-to-new map into every name column the fill joins on and into a chunk's {@code origin_collection} tag, in one pass per
 * column, before {@link #fillIds} (the maps overlap when the backup comes from another installation, so a second pass would rename twice). A label that is
 * not of the minted form is kept. A note's {@code source_uri} ({@code nx-note://<label>/...}) follows its collection, as
 * {@link NoteUri#moveTo} does for a rename. Each relabel that changes a label is reported, as the recovery bundle's {@code collections_relabelled} does.
 *
 * <p><b>Refusal.</b> {@link #refuseOrphans} runs before the first real-table insert, on the stages: a non-null twin that the
 * file's registry does not hold once is {@link #ORPHAN_ID_CODE}, a NULL-twin row with a non-empty name that no registry row
 * of the backup holds is {@link #ORPHAN_CODE}, and one whose name another row of the table carries with a different collection's
 * id is {@link #AMBIGUOUS_CODE}; each names the table and column, the count and an example, and nothing is written.
 *
 * <p>The quarantine tag {@link QuarantineOrigin#ORIGIN_ID_KEY} of a chunk's metadata: an id the map holds is mapped, any other id
 * is stripped (never resolved by the tag's name, which may be stale), and a tag with no id key is left as it is.
 *
 * <p>Entry for a table: {@link #fill}, which returns the map and the registry's relabels ({@link Filled}). The name and id columns come from {@code db/rdr229/p31-checks.tsv}, the one list the
 * expand's changesets are pinned to.
 */
final class RestoreIdFill {

    /** The stable code at the head of the orphan refusal. */
    static final String ORPHAN_CODE = "restore_orphan_collection";

    /** The stable code at the head of the by-id refusal: a row's collection id that the backup's registry does not hold. */
    static final String ORPHAN_ID_CODE = "restore_orphan_collection_id";

    /** The stable code at the head of the cross-check refusal: a NULL-twin name that another row of the table carries with another collection's id. */
    static final String AMBIGUOUS_CODE = "restore_ambiguous_collection_name";

    /** The registry stage's columns that keep the file's own {@code collection_id} and {@code superseded_by_id}. */
    static final String SRC_ID = "src_collection_id";
    static final String SRC_SUCCESSOR = "src_superseded_by_id";

    private static final String REGISTRY = "catalog_collections";
    private static final String CHUNKS = "chunks";
    private static final String LIST = "/db/rdr229/p31-checks.tsv";

    /** The session's temp table of the relabel: the backup's minted-form label and the label of the row's new id. */
    private static final String RELABEL = "nx_imp_relabel";
    private static final int RELABEL_ROWS_PER_INSERT = 2000;


    /** A name column and the id column beside it; {@code history}: a log table's label, with no CHECK. */
    private record Pair(String nameColumn, String idColumn, boolean history) {
    }

    /** The exported tables' NAME_ID rows of the expand's list, by table, in list order. */
    private static final Map<String, List<Pair>> PAIRS = Map.copyOf(readPairs());

    private RestoreIdFill() {
    }

    private static Map<String, List<Pair>> readPairs() {
        Map<String, Set<Pair>> byTable = new LinkedHashMap<>();
        try (InputStream in = RestoreIdFill.class.getResourceAsStream(LIST)) {
            if (in == null) throw new IllegalStateException("the expand's list " + LIST + " is not on the classpath");
            BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            for (String line = r.readLine(); line != null; line = r.readLine()) {
                if (line.isBlank() || line.startsWith("#") || line.startsWith("table\t")) continue;
                String[] f = line.split("\t");
                if (f.length != 5) throw new IllegalStateException("a row of " + LIST + " has " + f.length + " columns: " + line);
                boolean history = "HISTORY".equals(f[4]);
                if (!("NAME_ID".equals(f[4]) || history) || TenantExport.exportable(f[0]).isEmpty()) continue;
                byTable.computeIfAbsent(f[0], k -> new LinkedHashSet<>()).add(new Pair(f[1], f[2], history));
            }
        } catch (IOException e) {
            throw new IllegalStateException("could not read " + LIST, e);
        }
        Map<String, List<Pair>> out = new LinkedHashMap<>();
        byTable.forEach((k, v) -> out.put(k, List.copyOf(v)));
        return out;
    }

    /**
     * The id columns the insert of {@code table} must name beyond the file's own: those {@link #fill} wrote on its stage, so
     * a file that carries them keeps them in its own list and one that does not gets them added.
     */
    static List<String> filledColumns(DSLContext ctx, ExportTable table, Stage rows) {
        return computedColumns(table.name()).stream().filter(c -> stageHasColumn(ctx, rows, c)).toList();
    }

    /**
     * The id columns of {@code table} this class computes on every restore, whatever the file holds: the registry's own
     * ids, and every other table's ids from the expand's list. The import's plan does not require them from the file
     * (TenantImportPlan#identityColumns), because {@link #fill} writes them before the insert.
     */
    static List<String> computedColumns(String table) {
        return REGISTRY.equals(table) ? List.of("collection_id", "superseded_by_id")
            : PAIRS.getOrDefault(table, List.of()).stream().map(Pair::idColumn).toList();
    }

    // ── the refusal ─────────────────────────────────────────────────────────────────────────────

    /**
     * Refuses a backup with a collection that its registry does not hold, by the rule of the fill: a non-null twin is checked
     * by id against the registry stage's own {@code collection_id} (the file's, still unmapped here; an id held by more than
     * one registry row is ambiguous and counts as not held), a NULL-twin row by its name. Writes nothing.
     */
    static void refuseOrphans(DSLContext ctx, Map<String, TablePlan> plan, Map<String, Stage> stages) {
        Stage registry = stages.get(REGISTRY);
        boolean registryHasIds = registry != null && stageHasColumn(ctx, registry, "collection_id");
        List<String> byName = new ArrayList<>();
        List<String> byId = new ArrayList<>();
        List<String> ambiguous = new ArrayList<>();
        for (ExportTable e : TenantExport.TABLES) {
            TablePlan tp = plan.get(e.name());
            Stage st = stages.get(e.name());
            if (tp == null || st == null || !tp.loads()) continue;
            for (Pair p : PAIRS.getOrDefault(e.name(), List.of())) {
                if (p.history()) continue;
                boolean hasName = stageHasColumn(ctx, st, p.nameColumn());
                boolean hasId = stageHasColumn(ctx, st, p.idColumn());
                if (!hasName && !hasId) continue;
                Table<?> rows = TenantImportStaging.stageTable(st).as("s");
                Field<Long> twin = DSL.field(DSL.name("s", p.idColumn()), SQLDataType.BIGINT);
                if (hasId) {
                    Condition orphan = twin.isNotNull();
                    if (registryHasIds) {
                        Table<?> r = TenantImportStaging.stageTable(registry).as("r");
                        Field<Long> held = DSL.field(DSL.name("r", "collection_id"), SQLDataType.BIGINT);
                        orphan = orphan.and(DSL.not(twin.in(DSL.select(held).from(r).where(held.isNotNull())
                            .groupBy(held).having(DSL.count().eq(1)))));
                    }
                    Record2<Integer, Long> row = ctx.select(DSL.count(), DSL.min(twin)).from(rows).where(orphan).fetchOne();
                    if (row != null && row.value1() > 0) {
                        byId.add(e.name() + "." + p.idColumn() + ": " + row.value1() + " row" + (row.value1() == 1 ? "" : "s")
                            + " carry a collection id that the backup's registry does not hold exactly once (for example "
                            + row.value2() + ")");
                    }
                }
                if (hasName) {
                    Field<String> name = DSL.field(DSL.name("s", p.nameColumn()), SQLDataType.VARCHAR);
                    Condition orphan = name.isNotNull().and(name.ne(""));
                    if (hasId) orphan = orphan.and(twin.isNull());
                    if (registry != null) {
                        Table<?> r = TenantImportStaging.stageTable(registry).as("r");
                        orphan = orphan.and(DSL.notExists(DSL.selectOne().from(r)
                            .where(DSL.field(DSL.name("r", "name"), SQLDataType.VARCHAR).eq(name))));
                    }
                    if (hasId && registryHasIds) {
                        // a NULL-twin row whose name another row of the table carries with the id of a different collection
                        Table<?> o = TenantImportStaging.stageTable(st).as("o");
                        Table<?> r2 = TenantImportStaging.stageTable(registry).as("r2");
                        Field<String> otherName = DSL.field(DSL.name("o", p.nameColumn()), SQLDataType.VARCHAR);
                        Field<Long> otherTwin = DSL.field(DSL.name("o", p.idColumn()), SQLDataType.BIGINT);
                        Field<Long> own = DSL.field(DSL.select(DSL.field(DSL.name("r2", "collection_id"), SQLDataType.BIGINT)).from(r2)
                            .where(DSL.field(DSL.name("r2", "name"), SQLDataType.VARCHAR).eq(name)));
                        Condition clash = name.isNotNull().and(name.ne("")).and(twin.isNull())
                            .and(DSL.exists(DSL.selectOne().from(o).where(otherName.eq(name)).and(otherTwin.isNotNull())
                                .and(otherTwin.isDistinctFrom(own))));
                        Record2<Integer, String> amb = ctx.select(DSL.count(), DSL.min(name)).from(rows).where(clash).fetchOne();
                        if (amb != null && amb.value1() > 0) {
                            ambiguous.add(e.name() + "." + p.nameColumn() + ": " + amb.value1() + " row" + (amb.value1() == 1 ? "" : "s")
                                + " name '" + amb.value2() + "' with no collection id, while other rows of the table carry that name "
                                + "with the id of another collection");
                        }
                    }
                    Record2<Integer, String> row = ctx.select(DSL.count(), DSL.min(name)).from(rows).where(orphan).fetchOne();
                    if (row != null && row.value1() > 0) {
                        byName.add(e.name() + "." + p.nameColumn() + ": " + row.value1() + " row" + (row.value1() == 1 ? "" : "s")
                            + " name a collection with no registry row in the backup (for example '" + row.value2() + "')");
                    }
                }
            }
        }
        List<String> parts = new ArrayList<>();
        if (!byName.isEmpty()) {
            parts.add(ORPHAN_CODE + ": " + String.join("; ", byName)
                + "; a restored row must name a collection the backup's registry holds");
        }
        if (!byId.isEmpty()) {
            parts.add(ORPHAN_ID_CODE + ": " + String.join("; ", byId)
                + "; a restored row's collection id must be one the backup's registry holds");
        }
        if (!ambiguous.isEmpty()) {
            parts.add(AMBIGUOUS_CODE + ": " + String.join("; ", ambiguous)
                + "; the backup contradicts itself about which collection the name means");
        }
        if (!parts.isEmpty()) throw new LoadFailedException(String.join("; ", parts));
    }

    // ── the fill ────────────────────────────────────────────────────────────────────────────────

    /** What {@link #fill} gives back: the map after this table, and the registry's relabels (empty for every other table). */
    record Filled(CollectionIdMap map, List<Relabel> relabelled) {
    }

    /**
     * Fills {@code table}'s ids on its stage, before the table's insert: the registry draws its own ids, builds the map, and
     * relabels its minted-form labels to the new ids' labels; every other table first takes the relabel in its name columns,
     * then its ids by the rule in the class comment.
     *
     * @param tenant         the restoring tenant
     * @param rows           the table's stage
     * @param sourceToTarget the map so far: empty until the registry's fill builds it
     * @return the map after this table (the registry's fill returns the one it built, any other table the one it was given),
     *         and the registry's relabels (a minted-form label and the label of the row's new id), empty for every other table
     */
    static Filled fill(DSLContext ctx, String tenant, ExportTable table, Stage rows, CollectionIdMap sourceToTarget) {
        CollectionIdMap map = sourceToTarget;
        List<Relabel> relabelled = List.of();
        if (REGISTRY.equals(table.name())) {
            drawRegistryIds(ctx, rows);
            map = buildMap(ctx, rows);
            relabelled = relabelRegistry(ctx, rows);
        } else {
            relabelNames(ctx, table, rows);
        }
        fillIds(ctx, tenant, table, rows, map);
        if (CHUNKS.equals(table.name())) refillOriginTag(ctx, rows, map);
        return new Filled(map, relabelled);
    }

    /**
     * Rewrites each staged registry row whose {@code name} is of the minted form to the minted label of its new id (drawn by
     * {@link #drawRegistryIds}), and every staged {@code superseded_by} that names such a row to that row's new label. The
     * old-to-new pairs go to {@value #RELABEL}, a temp table of the session's transaction, for {@link #relabelNames}; a backup
     * with no minted-form label creates none. The rule is {@link CollectionLabel}'s ({@code isMintedForm}, {@code mintedLabel}).
     */
    private static List<Relabel> relabelRegistry(DSLContext ctx, Stage rows) {
        Table<?> stage = TenantImportStaging.stageTable(rows);
        Field<String> name = DSL.field(DSL.name("name"), SQLDataType.VARCHAR);
        Field<Long> id = DSL.field(DSL.name("collection_id"), SQLDataType.BIGINT);
        List<Relabel> pairs = new ArrayList<>();
        for (var r : ctx.select(name, id).from(stage).fetch()) {
            String label = r.value1();
            if (CollectionLabel.isMintedForm(label)) pairs.add(new Relabel(label, CollectionLabel.mintedLabel(r.value2())));
        }
        if (pairs.isEmpty()) return List.of();
        ctx.createTemporaryTable(DSL.name(RELABEL))
            .column("old_name", SQLDataType.VARCHAR.nullable(false))
            .column("new_name", SQLDataType.VARCHAR.nullable(false))
            .constraints(DSL.primaryKey("old_name"))
            .onCommitDrop().execute();
        for (int from = 0; from < pairs.size(); from += RELABEL_ROWS_PER_INSERT) {
            var insert = ctx.insertInto(DSL.table(DSL.name(RELABEL)), oldName(), newName());
            for (Relabel p : pairs.subList(from, Math.min(pairs.size(), from + RELABEL_ROWS_PER_INSERT))) {
                insert = insert.values(p.label(), p.targetLabel());
            }
            insert.execute();
        }
        relabelColumn(ctx, rows, "name");
        relabelColumn(ctx, rows, "superseded_by");
        // the map keeps every minted-form pair (an identity pair is harmless there), the report only real changes, as the bundle's
        return pairs.stream().filter(p -> !p.label().equals(p.targetLabel())).toList();
    }

    /**
     * Carries {@link #relabelRegistry}'s map into {@code table}'s stage: every name column of the expand's list that the stage
     * holds takes the new label of a collection the registry relabelled, and a chunk's {@code origin_collection} tag follows
     * it. Run before {@link #fillIds}, whose by-name rule joins the restored registry on the name. A no-op when nothing was relabelled.
     */
    private static void relabelNames(DSLContext ctx, ExportTable table, Stage rows) {
        if (!tempTableExists(ctx, RELABEL)) return;
        for (Pair p : PAIRS.getOrDefault(table.name(), List.of())) {
            relabelColumn(ctx, rows, p.nameColumn());
        }
        if (CHUNKS.equals(table.name())) relabelOriginTag(ctx, rows);
        if (NOTE_URI_TABLES.contains(table.name())) relabelNoteUris(ctx, rows);
    }

    /**
     * The tables that carry a document's {@code source_uri}: {@code catalog_documents} and the two copies the aspects and the
     * highlights keep (all three are exported, with every live column; a file older than the column is skipped by the stage
     * check). {@code NoteUri.moveTo} is what a collection rename does to this column, and the restore's relabel is a rename.
     */
    private static final Set<String> NOTE_URI_TABLES = Set.of("catalog_documents", "document_aspects", "document_highlights");

    /**
     * {@code nx-note://<old>/<rest>} becomes {@code nx-note://<new>/<rest>} for each relabelled collection, one pass over the
     * stage's {@code source_uri}: the form {@link NoteUri#moveTo} writes, so a later re-put of the same title into the relabelled
     * collection finds the row by its URI. {@code chroma://} and {@code nxexp://} values are left as they are, as {@code moveTo}
     * leaves them. A minted-form label never opens with a file-routed prefix ({@link NoteUri#FILE_ROUTED_PREFIXES}), so every
     * such note URI is a note. A label holds no {@code '/'}, so at most one map row names a URI.
     */
    private static void relabelNoteUris(DSLContext ctx, Stage rows) {
        if (!stageHasColumn(ctx, rows, "source_uri")) return;
        Field<String> uri = DSL.field(DSL.name("s", "source_uri"), SQLDataType.VARCHAR);
        var m = DSL.table(DSL.name(RELABEL)).as("m");
        Field<String> oldLabel = DSL.field(DSL.name("m", "old_name"), SQLDataType.VARCHAR);
        Field<String> newLabel = DSL.field(DSL.name("m", "new_name"), SQLDataType.VARCHAR);
        Condition names = NoteUri.startsWith(uri, DSL.val(NoteUri.NOTE).concat(oldLabel).concat(DSL.val("/")));
        Field<String> moved = DSL.field(DSL.select(DSL.val(NoteUri.NOTE).concat(newLabel).concat(DSL.val("/"))
                .concat(DSL.substring(uri, DSL.charLength(oldLabel).plus(NoteUri.NOTE.length() + 2))))
            .from(m).where(names));
        ctx.update(TenantImportStaging.stageTable(rows).as("s"))
            .set(DSL.field(DSL.name("source_uri"), SQLDataType.VARCHAR), moved)
            .where(DSL.exists(DSL.selectOne().from(m).where(names)))
            .execute();
    }

    private static Field<String> oldName() {
        return DSL.field(DSL.name("old_name"), SQLDataType.VARCHAR);
    }

    private static Field<String> newName() {
        return DSL.field(DSL.name("new_name"), SQLDataType.VARCHAR);
    }

    /** One pass over {@code column} of the stage: a value that is a key of the relabel map becomes its label; others are kept. */
    private static void relabelColumn(DSLContext ctx, Stage rows, String column) {
        if (!stageHasColumn(ctx, rows, column)) return;
        Field<String> value = DSL.field(DSL.name("s", column), SQLDataType.VARCHAR);
        ctx.update(TenantImportStaging.stageTable(rows).as("s"))
            .set(DSL.field(DSL.name(column), SQLDataType.VARCHAR),
                DSL.field(DSL.select(newName()).from(DSL.table(DSL.name(RELABEL))).where(oldName().eq(value))))
            .where(value.in(DSL.select(oldName()).from(DSL.table(DSL.name(RELABEL)))))
            .execute();
    }

    /** A chunk's {@code origin_collection} tag that names a relabelled collection names its new label; every other key is kept. */
    private static void relabelOriginTag(DSLContext ctx, Stage rows) {
        if (!stageHasColumn(ctx, rows, "metadata")) return;
        Field<JSONB> metadata = DSL.field(DSL.name("s", "metadata"), SQLDataType.JSONB);
        Field<String> origin = DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, metadata, DSL.val("origin_collection"));
        Field<String> target = DSL.field(DSL.select(newName()).from(DSL.table(DSL.name(RELABEL))).where(oldName().eq(origin)));
        ctx.update(TenantImportStaging.stageTable(rows).as("s"))
            .set(DSL.field(DSL.name("metadata"), SQLDataType.JSONB), DSL.function("jsonb_set", SQLDataType.JSONB, metadata,
                DSL.val(new String[] {"origin_collection"}), DSL.function("to_jsonb", SQLDataType.JSONB, target)))
            .where(origin.in(DSL.select(oldName()).from(DSL.table(DSL.name(RELABEL)))))
            .execute();
    }

    /**
     * A quarantined chunk's metadata tags its origin by name ({@code origin_collection}) and by id
     * ({@link QuarantineOrigin#ORIGIN_ID_KEY}). The id is the backup's installation's; a restore mints new ids, so a tag that
     * has the key gets the id the map holds for it, or loses the key when the map holds none (the name beside it is not
     * consulted: from P3.7 it may be a stale label, or another collection's). Every other key of the metadata is kept. A row
     * without the key is not written.
     */
    private static void refillOriginTag(DSLContext ctx, Stage rows, CollectionIdMap map) {
        if (!stageHasColumn(ctx, rows, "metadata")) return;
        Field<JSONB> metadata = DSL.field(DSL.name("s", "metadata"), SQLDataType.JSONB);
        Field<String> originIdText = DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, metadata,
            DSL.val(QuarantineOrigin.ORIGIN_ID_KEY));
        Field<Long> sourceId = DSL.when(originIdText.likeRegex("^[0-9]{1,18}$"), originIdText.cast(SQLDataType.BIGINT))
            .otherwise(DSL.castNull(SQLDataType.BIGINT));
        Field<Long> restored = map.target(sourceId);
        Field<JSONB> refilled = DSL.function("jsonb_set", SQLDataType.JSONB, metadata,
            DSL.val(new String[] {QuarantineOrigin.ORIGIN_ID_KEY}), DSL.function("to_jsonb", SQLDataType.JSONB, restored));
        Field<JSONB> stripped = DSL.function("jsonb_delete", SQLDataType.JSONB, metadata, DSL.val(QuarantineOrigin.ORIGIN_ID_KEY));
        ctx.update(TenantImportStaging.stageTable(rows).as("s"))
            .set(DSL.field(DSL.name("metadata"), SQLDataType.JSONB), DSL.when(restored.isNotNull(), refilled).otherwise(stripped))
            .where(DSL.function("jsonb_exists", SQLDataType.BOOLEAN, metadata, DSL.val(QuarantineOrigin.ORIGIN_ID_KEY)).isTrue())
            .execute();
    }

    /** The step that follows {@code table}'s insert: after the registry, the tombstones' second pass. */
    static void afterLoad(DSLContext ctx, String tenant, ExportTable table, Stage rows, CollectionIdMap map) {
        if (REGISTRY.equals(table.name())) repointTombstones(ctx, tenant, rows, map);
    }

    /**
     * Every staged row's id, by the rule of the class comment: a non-null twin through the map (NULL when the map does not hold
     * it, which only a history table can reach, as {@link #refuseOrphans} refused the rest), a NULL or absent twin by the
     * restored registry row on (tenant, name); an empty or unresolved name gives NULL. A history row with a NULL twin is
     * filled by name only for a file that predates the ids. A non-history row with an id then takes its collection's current name.
     */
    private static void fillIds(DSLContext ctx, String tenant, ExportTable table, Stage rows, CollectionIdMap map) {
        for (Pair p : PAIRS.getOrDefault(table.name(), List.of())) {
            boolean hasName = stageHasColumn(ctx, rows, p.nameColumn());
            boolean hasId = stageHasColumn(ctx, rows, p.idColumn());
            if (!hasName && !hasId) continue;
            Field<Long> id = DSL.field(DSL.name(p.idColumn()), SQLDataType.BIGINT);
            if (!hasId) ctx.alterTable(TenantImportStaging.stageTable(rows)).addColumn(id).execute();
            Field<Long> twin = DSL.field(DSL.name("s", p.idColumn()), SQLDataType.BIGINT);
            // a history row keeps a NULL twin unless the file predates the id columns: its label may be an old one
            boolean nameFillsNullTwin = hasName && (!p.history() || !map.fileCarriesIds());
            Field<Long> byName = nameFillsNullTwin
                ? CollectionIds.idOf(DSL.val(tenant), DSL.field(DSL.name("s", p.nameColumn()), SQLDataType.VARCHAR))
                : DSL.castNull(SQLDataType.BIGINT);
            ctx.update(TenantImportStaging.stageTable(rows).as("s"))
                .set(id, DSL.when(twin.isNotNull(), map.target(twin)).otherwise(byName))
                .execute();
            if (hasName && !p.history()) {
                // the row's name becomes its collection's current name, so a stale label never reaches a name key the target keeps
                var current = CATALOG_COLLECTIONS.as("cur");
                Field<String> currentName = DSL.field(DSL.select(current.NAME).from(current)
                    .where(current.TENANT_ID.eq(tenant)).and(current.COLLECTION_ID.eq(twin)));
                ctx.update(TenantImportStaging.stageTable(rows).as("s"))
                    .set(DSL.field(DSL.name(p.nameColumn()), SQLDataType.VARCHAR),
                        DSL.coalesce(currentName, DSL.field(DSL.name("s", p.nameColumn()), SQLDataType.VARCHAR)))
                    .where(twin.isNotNull())
                    .execute();
            }
        }
    }

    /**
     * Gives every staged registry row its id from the column's own sequence and each tombstone the same value as its
     * {@code superseded_by_id}. A file's own {@code collection_id} and {@code superseded_by_id} are another installation's:
     * they are saved to {@link #SRC_ID} and {@link #SRC_SUCCESSOR} first (NULL where the file has no such column), then
     * overwritten.
     */
    private static void drawRegistryIds(DSLContext ctx, Stage rows) {
        Table<?> stage = TenantImportStaging.stageTable(rows);
        Field<Long> id = DSL.field(DSL.name("collection_id"), SQLDataType.BIGINT);
        Field<Long> successor = DSL.field(DSL.name("superseded_by_id"), SQLDataType.BIGINT);
        Field<String> supersededBy = DSL.field(DSL.name("superseded_by"), SQLDataType.VARCHAR);
        Field<Long> srcId = DSL.field(DSL.name(SRC_ID), SQLDataType.BIGINT);
        Field<Long> srcSuccessor = DSL.field(DSL.name(SRC_SUCCESSOR), SQLDataType.BIGINT);
        boolean hadId = stageHasColumn(ctx, rows, "collection_id");
        boolean hadSuccessor = stageHasColumn(ctx, rows, "superseded_by_id");
        ctx.alterTable(stage).addColumnIfNotExists(srcId).execute();
        ctx.alterTable(stage).addColumnIfNotExists(srcSuccessor).execute();
        if (hadId) ctx.update(stage).set(srcId, id).execute();
        if (hadSuccessor) ctx.update(stage).set(srcSuccessor, successor).execute();
        ctx.alterTable(stage).addColumnIfNotExists(id).execute();
        ctx.alterTable(stage).addColumnIfNotExists(successor).execute();
        ctx.alterTable(stage).addColumnIfNotExists(supersededBy).execute();
        Field<Object> sequence = DSL.function("pg_get_serial_sequence", SQLDataType.VARCHAR,
            DSL.val("nexus." + REGISTRY), DSL.val("collection_id"))
            .cast(new DefaultDataType<>(SQLDialect.POSTGRES, Object.class, "regclass"));
        ctx.update(stage).set(id, DSL.function("nextval", SQLDataType.BIGINT, sequence)).execute();
        ctx.update(stage).set(successor, DSL.when(DSL.coalesce(supersededBy, "").ne(""), id).otherwise(DSL.castNull(Long.class)))
            .execute();
    }

    /**
     * The map of this load: the registry stage's saved file id to its drawn id, for each file id the file's registry holds once.
     * A file with no ids (before P3.1) gives an empty table, so every row of it is filled by name.
     */
    private static CollectionIdMap buildMap(DSLContext ctx, Stage registry) {
        ctx.createTemporaryTable(DSL.name(CollectionIdMap.TABLE))
            .column(CollectionIdMap.SOURCE, SQLDataType.BIGINT.nullable(false))
            .column(CollectionIdMap.TARGET, SQLDataType.BIGINT.nullable(false))
            .constraints(DSL.primaryKey(CollectionIdMap.SOURCE))
            .onCommitDrop().execute();
        Table<?> s = TenantImportStaging.stageTable(registry).as("s");
        Field<Long> source = DSL.field(DSL.name("s", SRC_ID), SQLDataType.BIGINT);
        Field<Long> drawn = DSL.field(DSL.name("s", "collection_id"), SQLDataType.BIGINT);
        ctx.insertInto(DSL.table(DSL.name(CollectionIdMap.TABLE)),
                DSL.field(DSL.name(CollectionIdMap.SOURCE), SQLDataType.BIGINT), DSL.field(DSL.name(CollectionIdMap.TARGET), SQLDataType.BIGINT))
            .select(ctx.select(source, DSL.min(drawn)).from(s).where(source.isNotNull()).groupBy(source).having(DSL.count().eq(1)))
            .execute();
        boolean carriesIds = ctx.fetchExists(ctx.selectOne().from(s).where(source.isNotNull()));
        return new CollectionIdMap(CollectionIdMap.TABLE, carriesIds);
    }

    /**
     * After every registry row is in: each tombstone takes its successor's id (R7), replacing the self id the insert gave it.
     * <ul>
     *   <li>A file successor id that the map holds: the mapped id. The tombstone's {@code superseded_by} name is not read (from
     *       P3.7 it may be a stale label, or another collection's).</li>
     *   <li>A NULL file successor id (a file from before P3.1, or a row stamped late): the id of the restored row that
     *       {@code superseded_by} names, when there is one.</li>
     *   <li>Any other keeps the self id, as a successor absent from the backup does.</li>
     * </ul>
     */
    static void repointTombstones(DSLContext ctx, String tenant, Stage rows, CollectionIdMap map) {
        var t = CATALOG_COLLECTIONS;
        Table<?> s = TenantImportStaging.stageTable(rows).as("s");
        Field<Long> drawn = DSL.field(DSL.name("s", "collection_id"), SQLDataType.BIGINT);
        Field<Long> fileSuccessor = DSL.field(DSL.name("s", SRC_SUCCESSOR), SQLDataType.BIGINT);
        if (!map.isEmpty()) {
            Field<Long> mapped = DSL.field(DSL.select(map.target(fileSuccessor)).from(s).where(drawn.eq(t.COLLECTION_ID)));
            ctx.update(t).set(t.SUPERSEDED_BY_ID, mapped)
                .where(t.TENANT_ID.eq(tenant)).and(t.SUPERSEDED_BY.ne(""))
                .and(mapped.isNotNull()).and(t.SUPERSEDED_BY_ID.isDistinctFrom(mapped))
                .execute();
        }
        var reg = CATALOG_COLLECTIONS.as("reg");
        Field<Long> registered = DSL.field(DSL.select(reg.COLLECTION_ID).from(reg)
            .where(reg.TENANT_ID.eq(t.TENANT_ID)).and(reg.NAME.eq(t.SUPERSEDED_BY)));
        ctx.update(t).set(t.SUPERSEDED_BY_ID, registered)
            .where(t.TENANT_ID.eq(tenant)).and(t.SUPERSEDED_BY.ne(""))
            .and(t.COLLECTION_ID.in(DSL.select(drawn).from(s).where(fileSuccessor.isNull())))
            .and(registered.isNotNull()).and(t.SUPERSEDED_BY_ID.isDistinctFrom(registered))
            .execute();
    }

    // ── the stage's columns ─────────────────────────────────────────────────────────────────────

    /** True when this session has a temp table {@code name}. */
    private static boolean tempTableExists(DSLContext ctx, String name) {
        Table<?> c = DSL.table(DSL.name("pg_class")).as("c");
        return ctx.fetchExists(ctx.selectOne().from(c)
            .where(DSL.field(DSL.name("c", "relname"), String.class).eq(name))
            .and(DSL.field(DSL.name("c", "relnamespace"), Long.class).eq(DSL.function("pg_my_temp_schema", Long.class))));
    }


    /** True when the session's temp table of {@code st} has {@code column} (a stage has the FILE's columns, not the target's). */
    private static boolean stageHasColumn(DSLContext ctx, Stage st, String column) {
        Table<?> a = DSL.table(DSL.name("pg_attribute")).as("a");
        Table<?> c = DSL.table(DSL.name("pg_class")).as("c");
        return ctx.fetchExists(ctx.selectOne().from(a).join(c)
            .on(DSL.field(DSL.name("a", "attrelid"), Long.class).eq(DSL.field(DSL.name("c", "oid"), Long.class)))
            .where(DSL.field(DSL.name("c", "relname"), String.class).eq(st.stageName()))
            .and(DSL.field(DSL.name("c", "relnamespace"), Long.class).eq(DSL.function("pg_my_temp_schema", Long.class)))
            .and(DSL.field(DSL.name("a", "attname"), String.class).eq(column))
            .and(DSL.field(DSL.name("a", "attisdropped"), Boolean.class).isFalse()));
    }
}
