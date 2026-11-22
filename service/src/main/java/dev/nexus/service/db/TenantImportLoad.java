// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import dev.nexus.service.db.TenantExport.ExportClass;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExport.Reference;
import dev.nexus.service.db.TenantExport.SoftRule;
import dev.nexus.service.db.TenantImportPlan.TablePlan;
import dev.nexus.service.db.TenantImportReport.SoftResult;
import dev.nexus.service.db.TenantImportReport.TableResult;
import dev.nexus.service.db.TenantImportStaging.Stage;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record;
import org.jooq.SelectJoinStep;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.jooq.SQLDialect;
import org.jooq.impl.DefaultDataType;

import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Consumer;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_META;

/**
 * The load of a tenant import (RDR-248 "Import session", Commit; bead nexus-csayy.6): the work the session's held
 * connection does after the restore-load lock is taken, all in that one transaction, so a failure anywhere leaves
 * the tenant as it was.
 *
 * <ol>
 * <li>re-check emptiness (writes that landed during the upload refuse the load) and re-decide which {@code derived}
 *     tables to skip;</li>
 * <li>per table in registry order: build the old-to-new id map from the table's own sequence (the engine role
 *     cannot {@code setval}, so ids are always reassigned), then ONE {@code INSERT ... SELECT} from the stage naming
 *     the file's columns, with the caller's tenant, new ids, and every referring column rewritten through its
 *     parent's map (foreign keys by the id map; soft references by their {@link SoftRule});</li>
 * <li>verify (S3): staged rows equal inserted plus seeded rows kept plus rows a soft-reference rule dropped; no
 *     foreign key value without a parent; every positive soft-reference value is a new id from its map;</li>
 * <li>upsert {@code catalog_meta(tenant, 'rdr248_restore', json)} as the last write.</li>
 * </ol>
 * The caller commits. Every statement is a typed jOOQ statement over names from the registry and the engine's own
 * stage names: no request text reaches SQL.
 */
final class TenantImportLoad {

    /** The {@code catalog_meta} key of the restore marker the status-less path reads after an engine restart. */
    static final String MARKER_KEY = "rdr248_restore";

    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** A failure the session reports as {@code failed: <message>}. */
    static final class LoadFailedException extends RuntimeException {
        LoadFailedException(String message) {
            super(message);
        }
    }

    private TenantImportLoad() {
    }

    // ── the whole load ──────────────────────────────────────────────────────────────────────────

    /**
     * @param ctx       the held connection's context: autocommit off, the tenant stamped, the restore lock held
     * @param status    receives {@code loading <table>} and {@code verifying}
     * @param staged    the staged row count per table, as the upload closed them
     */
    static TenantImportReport run(DSLContext ctx, String tenant, Map<String, TablePlan> plan, Map<String, Stage> stages,
                                  Map<String, Long> staged, String headerSha256, String sessionId, Instant now,
                                  Consumer<String> status) {
        status.accept("loading (checking the tenant is empty)");
        var nonEmpty = TenantImportStaging.firstNonEmptyDataTable(ctx, tenant);
        if (nonEmpty.isPresent()) {
            throw new LoadFailedException("tenant not empty: " + nonEmpty.get());
        }
        Map<String, String> skipped = new LinkedHashMap<>();
        for (TablePlan tp : plan.values()) {
            if (!tp.loads()) skipped.put(tp.name(), tp.skipReason());
        }
        skipDerivedWithRows(ctx, plan, skipped);
        List<TableResult> results = new ArrayList<>();
        List<SoftResult> softs = new ArrayList<>();
        for (ExportTable e : TenantExport.TABLES) {
            TablePlan tp = plan.get(e.name());
            if (tp == null || skipped.containsKey(e.name())) continue;
            status.accept("loading " + e.name());
            Stage st = stages.get(e.name());
            if (st.mapName() != null) fillMap(ctx, e, st);
            results.add(loadTable(ctx, tenant, tp, stages, staged.getOrDefault(e.name(), 0L), softs));
        }
        status.accept("verifying");
        verify(ctx, tenant, plan, stages, skipped, softs);
        List<String> skippedColumns = new ArrayList<>();
        for (TablePlan tp : plan.values()) {
            if (!skipped.containsKey(tp.name())) {
                tp.retiredColumns().forEach(c -> skippedColumns.add(tp.name() + "." + c));
            }
        }
        TenantImportReport report = new TenantImportReport(results, softs, skipped, skippedColumns);
        writeMarker(ctx, tenant, headerSha256, sessionId, now, report);
        return report;
    }

    /** A derived table the target already has rows for (a first search writes search_telemetry) is skipped with its dependents. */
    private static void skipDerivedWithRows(DSLContext ctx, Map<String, TablePlan> plan, Map<String, String> skipped) {
        for (ExportTable e : TenantExport.TABLES) {
            TablePlan tp = plan.get(e.name());
            if (tp == null || skipped.containsKey(e.name()) || e.exportClass() != ExportClass.DERIVED) continue;
            if (TenantImportStaging.derivedHasRows(ctx, e)) {
                skipped.put(e.name(), "the target already holds rows for it");
            }
        }
        boolean grew = true;
        while (grew) {
            grew = false;
            for (ExportTable e : TenantExport.TABLES) {
                TablePlan tp = plan.get(e.name());
                if (tp == null || skipped.containsKey(e.name())) continue;
                for (Reference r : e.references()) {
                    if (skipped.containsKey(r.parentName()) && plan.containsKey(r.parentName())) {
                        skipped.put(e.name(), "refers to skipped table " + r.parentName());
                        grew = true;
                        break;
                    }
                }
            }
        }
    }

    // ── fields ──────────────────────────────────────────────────────────────────────────────────

    private static Field<Object> sf(String column) {
        return DSL.field(DSL.name("s", column));
    }

    private static Field<Long> sl(String column) {
        return DSL.field(DSL.name("s", column), SQLDataType.BIGINT);
    }

    private static Field<Long> mapOld(String alias) {
        return DSL.field(DSL.name(alias, "old_id"), SQLDataType.BIGINT);
    }

    private static Field<Long> mapNew(String alias) {
        return DSL.field(DSL.name(alias, "new_id"), SQLDataType.BIGINT);
    }

    private static Table<?> mapTable(Stage st) {
        return DSL.table(DSL.name(st.mapName()));
    }

    /** Old id to new id from the table's own sequence, in old-id order (so the new ids keep the old order). */
    private static void fillMap(DSLContext ctx, ExportTable e, Stage st) {
        if (st.stageName() == null) return;   // a parent the file does not carry: an empty map
        Field<Long> oldId = DSL.field(DSL.name("s", e.reassignedId()), SQLDataType.BIGINT);
        Field<Object> sequence = DSL.function("pg_get_serial_sequence", SQLDataType.VARCHAR,
            DSL.val(e.schema() + "." + e.table()), DSL.val(e.reassignedId())).cast(new DefaultDataType<>(SQLDialect.POSTGRES, Object.class, "regclass"));
        Field<Long> next = DSL.function("nextval", SQLDataType.BIGINT, sequence);
        ctx.insertInto(mapTable(st), DSL.field(DSL.name("old_id"), SQLDataType.BIGINT),
                DSL.field(DSL.name("new_id"), SQLDataType.BIGINT))
            .select(ctx.select(oldId, next).from(TenantImportStaging.stageTable(st).as("s")).orderBy(oldId))
            .execute();
    }

    // ── one table ───────────────────────────────────────────────────────────────────────────────

    private static TableResult loadTable(DSLContext ctx, String tenant, TablePlan tp, Map<String, Stage> stages,
                                         long stagedClaimed, List<SoftResult> softs) {
        ExportTable e = tp.entry();
        Stage st = stages.get(e.name());
        long staged = TenantImportStaging.stagedRows(ctx, st);
        if (staged != stagedClaimed) {
            throw new LoadFailedException("staged rows of " + e.name() + " are " + staged + ", the upload closed it at "
                + stagedClaimed);
        }
        Map<String, Reference> fkMapped = new LinkedHashMap<>();
        Map<String, Reference> softByColumn = new LinkedHashMap<>();
        for (Reference r : e.references()) {
            if (r.foreignKey()) {
                ExportTable parent = TenantExport.entry(r.parentTable()).orElse(null);
                if (parent == null || parent.reassignedId() == null || !r.parentSchema().equals(parent.schema())) continue;
                int i = r.parentColumns().indexOf(parent.reassignedId());
                Stage ps = stages.get(r.parentName());
                if (i >= 0 && ps != null && ps.mapName() != null) fkMapped.put(r.columns().get(i), r);
            } else if (r.softRule() != null && tp.loadColumns().contains(r.columns().get(0))) {
                softByColumn.put(r.columns().get(0), r);
            }
        }
        for (String column : fkMapped.keySet()) {
            Reference r = fkMapped.get(column);
            Stage ps = stages.get(r.parentName());
            Table<?> m = mapTable(ps).as("chk");
            if (ctx.fetchExists(ctx.selectOne().from(TenantImportStaging.stageTable(st).as("s")).leftJoin(m)
                    .on(mapOld("chk").eq(sl(column)))
                    .where(sf(column).isNotNull(), mapOld("chk").isNull()))) {
                throw new LoadFailedException(e.name() + "." + column + " refers to a " + r.parentName()
                    + " row the file does not hold");
            }
        }
        checkSeededRowsMatch(ctx, tenant, tp, st);

        List<String> columns = new ArrayList<>(tp.loadColumns());
        if (tp.tenantScoped() && !columns.contains("tenant_id")) columns.add(0, "tenant_id");
        List<Field<?>> targets = new ArrayList<>();
        List<Field<?>> values = new ArrayList<>();
        List<Object[]> joins = new ArrayList<>();   // {leftJoin?, Table, Condition}
        String dropColumn = null;
        Stage dropParent = null;
        for (String column : columns) {
            targets.add(DSL.field(DSL.name(column)));
            if ("tenant_id".equals(column)) {
                values.add(DSL.val(tenant));
            } else if (column.equals(e.reassignedId())) {
                joins.add(new Object[]{true, mapTable(st).as("own"), mapOld("own").eq(sl(column))});
                values.add(mapNew("own"));
            } else if (fkMapped.containsKey(column)) {
                String alias = "fk_" + column;
                Stage ps = stages.get(fkMapped.get(column).parentName());
                joins.add(new Object[]{true, mapTable(ps).as(alias), mapOld(alias).eq(sl(column))});
                values.add(mapNew(alias));
            } else if (softByColumn.containsKey(column)) {
                Reference r = softByColumn.get(column);
                Stage ps = stages.get(r.parentName());
                SoftExpr x = softExpression(r, ps, column);
                joins.addAll(x.joins());
                values.add(x.value());
                if (r.softRule() == SoftRule.DROP_ROW) {
                    dropColumn = column;
                    dropParent = ps;
                }
                softs.add(countSoft(ctx, e, r, ps, column, st));
            } else {
                values.add(sf(column));
            }
        }
        SelectJoinStep<Record> select = ctx.select(values).from(TenantImportStaging.stageTable(st).as("s"));
        for (Object[] j : joins) {
            Table<?> t = (Table<?>) j[1];
            Condition on = (Condition) j[2];
            select = (Boolean) j[0] ? select.leftJoin(t).on(on) : select.join(t).on(on);
        }
        var insert = ctx.insertInto(DSL.table(DSL.name(e.schema(), e.table())), targets).select(select);
        long inserted = e.seededKeys().isEmpty() ? insert.execute() : insert.onConflictDoNothing().execute();
        long seededMatching = e.seededKeys().isEmpty() ? 0 : seededStageRows(ctx, tenant, e, st);
        // rows the soft-reference rule dropped are counted on their own (the staged rows with no entry in the parent's map), so
        // the arithmetic below checks them instead of being filled in by whatever is left over
        long dropped = dropColumn == null ? 0 : unmappedStageRows(ctx, st, dropColumn, dropParent);
        long seededSkipped = staged - inserted - dropped;
        if (seededSkipped < 0 || (e.seededKeys().isEmpty() ? seededSkipped != 0 : seededSkipped > seededMatching)) {
            throw new LoadFailedException("verification: " + e.name() + " staged " + staged + " rows, the load wrote " + inserted
                + ", dropped " + dropped + " and kept " + seededSkipped + " of the engine's own");
        }
        return new TableResult(e.name(), staged, inserted, seededSkipped, dropped);
    }

    /** Staged rows whose {@code column} has no old id in {@code parent}'s map: the rows an inner join to the map leaves out. */
    private static long unmappedStageRows(DSLContext ctx, Stage st, String column, Stage parent) {
        Table<?> m = mapTable(parent).as("um");
        return ctx.fetchCount(ctx.selectOne().from(TenantImportStaging.stageTable(st).as("s")).leftJoin(m)
            .on(mapOld("um").eq(sl(column))).where(mapOld("um").isNull()));
    }

    private static long seededStageRows(DSLContext ctx, String tenant, ExportTable e, Stage st) {
        Condition seeded = TenantImportStaging.seededCondition(e, tenant, name -> DSL.field(DSL.name("s", name), String.class));
        if (seeded == null) return 0;
        return ctx.fetchCount(ctx.selectOne().from(TenantImportStaging.stageTable(st).as("s")).where(seeded));
    }

    /**
     * A seeded row that the engine wrote and the file also carries must be EQUAL on every non-tenant column when its
     * entry says so ({@code embedding_profile}): a different profile means the restored vectors do not match the
     * engine's embedder. Names the key and the column.
     */
    private static void checkSeededRowsMatch(DSLContext ctx, String tenant, TablePlan tp, Stage st) {
        ExportTable e = tp.entry();
        if (e.seededKeys().stream().noneMatch(TenantExport.SeededKey::mustMatch)) return;
        List<String> keyColumns = e.primaryKey().stream().map(TenantExport.Column::name)
            .filter(n -> !"tenant_id".equals(n)).toList();
        Condition seeded = TenantImportStaging.seededCondition(e, tenant, name -> DSL.field(DSL.name("s", name), String.class));
        if (seeded == null) return;
        Table<?> target = DSL.table(DSL.name(e.schema(), e.table())).as("t");
        Condition join = DSL.trueCondition();
        for (String k : keyColumns) {
            join = join.and(DSL.field(DSL.name("t", k)).eq(sf(k)));
        }
        if (tp.tenantScoped()) join = join.and(DSL.field(DSL.name("t", "tenant_id"), String.class).eq(tenant));
        for (String column : tp.loadColumns()) {
            if ("tenant_id".equals(column) || keyColumns.contains(column)) continue;
            var diff = ctx.select(keyColumns.stream().map(k -> (Field<?>) sf(k)).toList())
                .from(TenantImportStaging.stageTable(st).as("s")).join(target).on(join)
                .where(seeded, sf(column).isDistinctFrom(DSL.field(DSL.name("t", column))))
                .limit(1).fetchOne();
            if (diff != null) {
                throw new LoadFailedException("the file's " + e.name() + " row " + diff.intoList()
                    + " differs from the engine's own on column " + column
                    + " (a different profile means the restored vectors do not match this engine's embedder)");
            }
        }
    }

    // ── soft references ─────────────────────────────────────────────────────────────────────────

    private record SoftExpr(Field<?> value, List<Object[]> joins) {
    }

    private static Field<Long> scalarRule(Field<Long> v, Field<Long> mapped) {
        // NULL, 0 and negatives are sentinels and pass through; a positive value with no map entry is negated
        return DSL.when(v.isNull().or(v.le(0L)), v).when(mapped.isNull(), v.neg()).otherwise(mapped);
    }

    private static Field<String> jsonText(Field<JSONB> doc, List<String> path) {
        List<Field<?>> args = new ArrayList<>();
        args.add(doc);
        path.forEach(p -> args.add(DSL.val(p)));
        return DSL.function("jsonb_extract_path_text", SQLDataType.VARCHAR, args.toArray(new Field<?>[0]));
    }

    private static Field<JSONB> jsonNode(Field<JSONB> doc, List<String> path) {
        List<Field<?>> args = new ArrayList<>();
        args.add(doc);
        path.forEach(p -> args.add(DSL.val(p)));
        return DSL.function("jsonb_extract_path", SQLDataType.JSONB, args.toArray(new Field<?>[0]));
    }

    /** The plan id inside a jsonb document: its value when a whole number sits at the path, else null. */
    private static Field<Long> jsonId(Field<JSONB> doc, List<String> path) {
        Field<String> text = jsonText(doc, path);
        Field<String> type = DSL.function("jsonb_typeof", SQLDataType.VARCHAR, jsonNode(doc, path));
        return DSL.when(type.eq("number").and(text.likeRegex("^-?[0-9]{1,18}$")), text.cast(SQLDataType.BIGINT)).otherwise(DSL.castNull(SQLDataType.BIGINT));
    }

    @SuppressWarnings("unchecked")
    private static SoftExpr softExpression(Reference r, Stage parent, String column) {
        List<Object[]> joins = new ArrayList<>();
        String alias = "sf_" + column;
        Table<?> m = mapTable(parent).as(alias);
        if (r.array()) {
            Field<Long[]> arr = DSL.field(DSL.name("s", column), SQLDataType.BIGINT.getArrayDataType());
            Table<?> u = DSL.unnest(arr).withOrdinality().as("u", "e", "ord");
            Field<Long> e = DSL.field(DSL.name("u", "e"), SQLDataType.BIGINT);
            Field<Integer> ord = DSL.field(DSL.name("u", "ord"), SQLDataType.INTEGER);
            Table<?> am = mapTable(parent).as("am");
            Field<Long[]> remapped = DSL.array(DSL.select(scalarRule(e, mapNew("am"))).from(u).leftJoin(am)
                .on(mapOld("am").eq(e)).orderBy(ord));
            return new SoftExpr(remapped, joins);
        }
        if (r.jsonPath() != null) {
            List<String> path = List.of(r.jsonPath().split("\\."));
            Field<JSONB> doc = DSL.field(DSL.name("s", column), SQLDataType.JSONB);
            Field<Long> id = jsonId(doc, path);
            joins.add(new Object[]{true, m, mapOld(alias).eq(id)});
            Field<JSONB> rewritten = DSL.function("jsonb_set", SQLDataType.JSONB, doc,
                DSL.val(path.toArray(new String[0]), SQLDataType.VARCHAR.getArrayDataType()),
                DSL.function("to_jsonb", SQLDataType.JSONB, scalarRule(id, mapNew(alias))), DSL.inline(false));
            return new SoftExpr(DSL.when(id.isNotNull(), rewritten).otherwise(doc), joins);
        }
        Field<Long> v = sl(column);
        if (r.softRule() == SoftRule.DROP_ROW) {
            joins.add(new Object[]{false, m, mapOld(alias).eq(v)});
            return new SoftExpr(mapNew(alias), joins);
        }
        joins.add(new Object[]{true, m, mapOld(alias).eq(v)});
        return new SoftExpr(scalarRule(v, mapNew(alias)), joins);
    }

    /** How many of a soft reference's staged values are remapped, kept as sentinels, or negated. */
    @SuppressWarnings("unchecked")
    private static SoftResult countSoft(DSLContext ctx, ExportTable e, Reference r, Stage parent, String column, Stage st) {
        String label = e.name() + "." + column + (r.jsonPath() != null ? ":" + r.jsonPath() : "");
        Table<?> stage = TenantImportStaging.stageTable(st).as("s");
        SoftResult counted = countValues(ctx, label, stage, r, column, parent, true);
        // a dropped row is not a negated value: it is counted in the table's own result ("dropped")
        return r.softRule() == SoftRule.DROP_ROW ? new SoftResult(label, counted.remapped(), counted.passedThrough(), 0) : counted;
    }

    /**
     * Counts a soft reference's values. {@code onStage} reads the staged (old) values against the map's old ids;
     * otherwise it reads the loaded (new) values against the map's new ids, where a positive value with no entry is
     * a violation.
     */
    private static SoftResult countValues(DSLContext ctx, String label, Table<?> source, Reference r, String column,
                                          Stage parent, boolean onStage) {
        String alias = onStage ? "s" : "t";
        Field<Long> v;
        Table<?> from = source;
        if (r.array()) {
            Field<Long[]> arr = DSL.field(DSL.name(alias, column), SQLDataType.BIGINT.getArrayDataType());
            from = source.crossJoin(DSL.unnest(arr).as("cu", "e"));
            v = DSL.field(DSL.name("cu", "e"), SQLDataType.BIGINT);
        } else if (r.jsonPath() != null) {
            v = jsonId(DSL.field(DSL.name(alias, column), SQLDataType.JSONB), List.of(r.jsonPath().split("\\.")));
        } else {
            v = DSL.field(DSL.name(alias, column), SQLDataType.BIGINT);
        }
        Table<?> m = mapTable(parent).as("cm");
        Field<Long> key = onStage ? mapOld("cm") : mapNew("cm");
        var row = ctx.select(
                DSL.count().filterWhere(v.gt(0L).and(key.isNotNull())),
                DSL.count().filterWhere(v.gt(0L).and(key.isNull())),
                DSL.count().filterWhere(v.le(0L)))
            .from(from).leftJoin(m).on(key.eq(v)).fetchOne();
        long mapped = row.get(0, Long.class);
        long unmapped = row.get(1, Long.class);
        long sentinel = row.get(2, Long.class);
        return onStage ? new SoftResult(label, mapped, sentinel, unmapped) : new SoftResult(label, mapped, sentinel, unmapped);
    }

    // ── verification and the marker ─────────────────────────────────────────────────────────────

    /** Every positive soft-reference value now in the tenant's tables is a new id from its map. */
    private static void verify(DSLContext ctx, String tenant, Map<String, TablePlan> plan, Map<String, Stage> stages,
                               Map<String, String> skipped, List<SoftResult> softs) {
        for (ExportTable e : TenantExport.TABLES) {
            TablePlan tp = plan.get(e.name());
            if (tp == null || skipped.containsKey(e.name())) continue;
            for (Reference r : e.references()) {
                if (r.softRule() == null || !tp.loadColumns().contains(r.columns().get(0))) continue;
                Stage parent = stages.get(r.parentName());
                if (parent == null || parent.mapName() == null) continue;
                Table<?> target = DSL.table(DSL.name(e.schema(), e.table())).as("t");
                SoftResult after = countValues(ctx, e.name() + "." + r.columns().get(0), target, r, r.columns().get(0),
                    parent, false);
                if (after.negated() > 0) {
                    throw new LoadFailedException("verification: " + e.name() + "." + r.columns().get(0) + " holds "
                        + after.negated() + " positive values that are not new ids of " + r.parentName());
                }
            }
        }
    }

    private static void writeMarker(DSLContext ctx, String tenant, String headerSha256, String sessionId, Instant now,
                                    TenantImportReport report) {
        ObjectNode marker = MAPPER.createObjectNode();
        marker.put("header_sha256", headerSha256);
        marker.put("session_id", sessionId);
        marker.put("committed_at", now.toString());
        ObjectNode counts = marker.putObject("row_counts");
        for (TableResult t : report.tables()) counts.put(t.table(), t.inserted());
        String json = marker.toString();
        ctx.insertInto(CATALOG_META)
            .set(CATALOG_META.TENANT_ID, tenant)
            .set(CATALOG_META.KEY, MARKER_KEY)
            .set(CATALOG_META.VALUE, json)
            .onConflict(CATALOG_META.TENANT_ID, CATALOG_META.KEY)
            .doUpdate().set(CATALOG_META.VALUE, json)
            .execute();
    }
}
