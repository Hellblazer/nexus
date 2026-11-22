// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantImportLoad.LoadFailedException;
import dev.nexus.service.db.TenantImportPlan.TablePlan;
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
 * RDR-229 P3.1 (nexus-p2m1x.17): the ids a restore writes beside the collection names. P3.1's NOT VALID CHECKs refuse every
 * row with a non-empty name and a NULL id, and a backup written before the id columns existed holds exactly such rows, so
 * the load fills each id from the RESTORED registry row on {@code (tenant_id, name)}, on the stage, before the table's
 * insert. A file's own ids, where it has any, are another installation's and are never trusted: every twin is overwritten, so
 * the result is the same for a file whose twins are NULL and one whose twins are not. {@code ''} means unset and gives NULL.
 * History tables ({@code relevance_log}, {@code search_telemetry}) are refilled the same way but an unresolved name is not
 * refused: they have no CHECK and keep a label.
 *
 * <p><b>The registry.</b> {@code catalog_collections.collection_id} is never taken from a file: the engine draws every
 * restored row's id from the column's own sequence ({@code nextval(pg_get_serial_sequence(...))}), so a later mint cannot
 * collide. A row cannot name its own identity in the INSERT that assigns it, so the id is drawn on the stage first and
 * inserted explicitly, a tombstone's as both {@code collection_id} and {@code superseded_by_id} (the CHECK refuses a
 * tombstone with a NULL successor id). {@link #repointTombstones} then, after every registry row is in and before any child
 * table loads, points each tombstone whose successor is in the backup at that row's new id (the R7 invariant); a successor
 * absent from the backup leaves the self id.
 *
 * <p><b>Refusal.</b> {@link #refuseOrphans} runs before the first real-table insert: a non-empty name with no registry row
 * in the backup is {@code restore_orphan_collection}, naming each table, the count and an example name, and nothing is
 * written.
 *
 * <p>Entry for a table: {@link #fill(DSLContext, String, ExportTable, Stage, CollectionIdMap)}. P3.1 passes
 * {@link CollectionIdMap#empty()}; P3.11 adds a by-id rule beside {@link #fillByName} and the map. The name and id
 * columns come from {@code db/rdr229/p31-checks.tsv}, the one list the expand's changesets are pinned to.
 */
final class RestoreIdFill {

    /** The stable code at the head of the orphan refusal. */
    static final String ORPHAN_CODE = "restore_orphan_collection";

    private static final String REGISTRY = "catalog_collections";
    private static final String CHUNKS = "chunks";
    private static final String LIST = "/db/rdr229/p31-checks.tsv";

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
        List<String> candidates = REGISTRY.equals(table.name()) ? List.of("collection_id", "superseded_by_id")
            : PAIRS.getOrDefault(table.name(), List.of()).stream().map(Pair::idColumn).toList();
        return candidates.stream().filter(c -> stageHasColumn(ctx, rows, c)).toList();
    }

    // ── the refusal ─────────────────────────────────────────────────────────────────────────────

    /** Refuses a backup with a non-empty collection name that no registry row of the backup holds. Writes nothing. */
    static void refuseOrphans(DSLContext ctx, Map<String, TablePlan> plan, Map<String, Stage> stages) {
        Stage registry = stages.get(REGISTRY);
        List<String> found = new ArrayList<>();
        for (ExportTable e : TenantExport.TABLES) {
            TablePlan tp = plan.get(e.name());
            Stage st = stages.get(e.name());
            if (tp == null || st == null || !tp.loads()) continue;
            for (Pair p : PAIRS.getOrDefault(e.name(), List.of())) {
                if (p.history() || !stageHasColumn(ctx, st, p.nameColumn())) continue;
                Table<?> rows = TenantImportStaging.stageTable(st).as("s");
                Field<String> name = DSL.field(DSL.name("s", p.nameColumn()), SQLDataType.VARCHAR);
                Condition orphan = name.isNotNull().and(name.ne(""));
                if (registry != null) {
                    Table<?> r = TenantImportStaging.stageTable(registry).as("r");
                    orphan = orphan.and(DSL.notExists(DSL.selectOne().from(r)
                        .where(DSL.field(DSL.name("r", "name"), SQLDataType.VARCHAR).eq(name))));
                }
                Record2<Integer, String> row = ctx.select(DSL.count(), DSL.min(name)).from(rows).where(orphan).fetchOne();
                if (row != null && row.value1() > 0) {
                    found.add(e.name() + "." + p.nameColumn() + ": " + row.value1() + " row" + (row.value1() == 1 ? "" : "s")
                        + " name a collection with no registry row in the backup (for example '" + row.value2() + "')");
                }
            }
        }
        if (!found.isEmpty()) {
            throw new LoadFailedException(ORPHAN_CODE + ": " + String.join("; ", found)
                + "; a restored row must name a collection the backup's registry holds");
        }
    }

    // ── the fill ────────────────────────────────────────────────────────────────────────────────

    /**
     * Fills {@code table}'s ids on its stage, before the table's insert: the registry draws its own ids, every other table
     * takes them by name from the restored registry rows.
     *
     * @param tenant         the restoring tenant
     * @param rows           the table's stage
     * @param sourceToTarget the map from the backup's ids to the restored ones: always empty in P3.1 (P3.11 adds the by-id rule)
     */
    static void fill(DSLContext ctx, String tenant, ExportTable table, Stage rows, CollectionIdMap sourceToTarget) {
        if (!sourceToTarget.isEmpty()) {
            throw new IllegalStateException("filling collection ids by id arrives with RDR-229 P3.11 (nexus-p2m1x.31)");
        }
        if (REGISTRY.equals(table.name())) drawRegistryIds(ctx, rows);
        fillByName(ctx, tenant, table, rows);
        if (CHUNKS.equals(table.name())) refillOriginTag(ctx, tenant, rows);
    }

    /**
     * A quarantined chunk's metadata tags its origin by name ({@code origin_collection}) and by id
     * ({@link QuarantineOrigin#ORIGIN_ID_KEY}). The id is the backup's installation's; a restore mints new ids, so it is refilled
     * the way the twins are: from the restored registry row of the tag's name, or stripped when that name has no registry row
     * (or the tag has no name). Every other key of the metadata is kept. A row without the key is not written.
     */
    private static void refillOriginTag(DSLContext ctx, String tenant, Stage rows) {
        if (!stageHasColumn(ctx, rows, "metadata")) return;
        Field<JSONB> metadata = DSL.field(DSL.name("s", "metadata"), SQLDataType.JSONB);
        Field<String> originName = DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, metadata, DSL.val("origin_collection"));
        Field<Long> restored = CollectionIds.idOf(DSL.val(tenant), originName);
        Field<JSONB> refilled = DSL.function("jsonb_set", SQLDataType.JSONB, metadata,
            DSL.val(new String[] {QuarantineOrigin.ORIGIN_ID_KEY}), DSL.function("to_jsonb", SQLDataType.JSONB, restored));
        Field<JSONB> stripped = DSL.function("jsonb_delete", SQLDataType.JSONB, metadata, DSL.val(QuarantineOrigin.ORIGIN_ID_KEY));
        ctx.update(TenantImportStaging.stageTable(rows).as("s"))
            .set(DSL.field(DSL.name("metadata"), SQLDataType.JSONB), DSL.when(restored.isNotNull(), refilled).otherwise(stripped))
            .where(DSL.function("jsonb_exists", SQLDataType.BOOLEAN, metadata, DSL.val(QuarantineOrigin.ORIGIN_ID_KEY)).isTrue())
            .execute();
    }

    /** The step that follows {@code table}'s insert: after the registry, the tombstones' second pass. */
    static void afterLoad(DSLContext ctx, String tenant, ExportTable table) {
        if (REGISTRY.equals(table.name())) repointTombstones(ctx, tenant);
    }

    /**
     * Every staged row takes the id of the restored registry row on (tenant, name), whatever the file's own value was (a file's
     * ids are another installation's and are never trusted); an empty or unresolved name gives NULL.
     */
    private static void fillByName(DSLContext ctx, String tenant, ExportTable table, Stage rows) {
        for (Pair p : PAIRS.getOrDefault(table.name(), List.of())) {
            if (!stageHasColumn(ctx, rows, p.nameColumn())) continue;
            Field<Long> id = DSL.field(DSL.name(p.idColumn()), SQLDataType.BIGINT);
            ctx.alterTable(TenantImportStaging.stageTable(rows)).addColumnIfNotExists(id).execute();
            Field<String> name = DSL.field(DSL.name("s", p.nameColumn()), SQLDataType.VARCHAR);
            ctx.update(TenantImportStaging.stageTable(rows).as("s"))
                .set(id, CollectionIds.idOf(DSL.val(tenant), name))
                .execute();
        }
    }

    /**
     * Gives every staged registry row its id from the column's own sequence and each tombstone the same value as its
     * {@code superseded_by_id}. A file's own {@code collection_id} is overwritten: it is another installation's.
     */
    private static void drawRegistryIds(DSLContext ctx, Stage rows) {
        Table<?> stage = TenantImportStaging.stageTable(rows);
        Field<Long> id = DSL.field(DSL.name("collection_id"), SQLDataType.BIGINT);
        Field<Long> successor = DSL.field(DSL.name("superseded_by_id"), SQLDataType.BIGINT);
        Field<String> supersededBy = DSL.field(DSL.name("superseded_by"), SQLDataType.VARCHAR);
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
     * After every registry row is in: each tombstone whose {@code superseded_by} names a restored row takes that row's id
     * (R7), replacing the self id the insert gave it. One whose successor is not in the backup keeps the self id.
     * P3.11 extends this to map a non-null source successor id through its map, by name as the fallback.
     */
    static void repointTombstones(DSLContext ctx, String tenant) {
        var t = CATALOG_COLLECTIONS.as("t");
        var s = CATALOG_COLLECTIONS.as("succ");
        List<String> names = ctx.selectDistinct(t.SUPERSEDED_BY).from(t).join(s)
            .on(s.TENANT_ID.eq(t.TENANT_ID)).and(s.NAME.eq(t.SUPERSEDED_BY))
            .where(t.TENANT_ID.eq(tenant)).and(t.SUPERSEDED_BY.ne(""))
            .fetch(t.SUPERSEDED_BY);
        CollectionTombstones.repointTombstonesNaming(ctx, tenant, names);
    }

    // ── the stage's columns ─────────────────────────────────────────────────────────────────────

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
