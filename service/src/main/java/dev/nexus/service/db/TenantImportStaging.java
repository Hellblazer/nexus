// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.TenantExport.Column;
import dev.nexus.service.db.TenantExport.ExportClass;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantImportPlan.TablePlan;
import dev.nexus.service.db.TenantImportPlan.TargetColumn;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.DefaultDataType;
import org.jooq.impl.SQLDataType;
import org.postgresql.PGConnection;

import javax.sql.DataSource;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.function.Function;

/**
 * The database reads and the staging half of a tenant import (RDR-248 "Import session", Open and Upload; bead
 * nexus-csayy.6): the target's live columns, the refusal checks that run in their own short transactions, the stage
 * temp tables shaped from the FILE's columns, and the copy of one part into a stage. The load itself is
 * {@link TenantImportLoad}.
 *
 * <p>A stage is never {@code CREATE TABLE ... AS} or {@code LIKE} the target: that would lock every {@code chunks}
 * partition until commit. It is a temp table whose columns are the file's, named by position
 * ({@code nx_imp_stage_N}), so no request text reaches an identifier. A column's type is the target's own
 * {@code format_type} text for a known column (the plan proved the file's equal to it) and a validated plain type
 * name for a retired column.
 */
final class TenantImportStaging {

    static final String STAGE_PREFIX = "nx_imp_stage_";
    static final String MAP_PREFIX = "nx_imp_map_";

    private final DataSource dataSource;

    TenantImportStaging(DataSource dataSource) {
        this.dataSource = dataSource;
    }

    /** A table's stage and, for a table whose id is reassigned, its old-to-new id map (both session temp tables). */
    record Stage(String table, String stageName, String mapName) {
    }

    // ── the target's live schema ────────────────────────────────────────────────────────────────

    private static <T> Field<T> f(String alias, String column, Class<T> type) {
        return DSL.field(DSL.name(alias, column), type);
    }

    /** The live non-generated columns of every exported registry table, by name; a table the target lacks is absent. */
    Map<String, List<TargetColumn>> targetColumns() {
        try (Connection conn = dataSource.getConnection()) {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            Map<String, List<TargetColumn>> out = new LinkedHashMap<>();
            for (ExportTable t : TenantExport.TABLES) {
                if (!t.exported()) continue;
                List<TargetColumn> cols = liveColumns(ctx, t);
                if (!cols.isEmpty()) out.put(t.name(), cols);
            }
            return out;
        } catch (SQLException e) {
            throw new IllegalStateException("could not read the target's columns", e);
        }
    }

    private static List<TargetColumn> liveColumns(DSLContext ctx, ExportTable table) {
        Field<String> name = f("a", "attname", String.class);
        Field<String> type = DSL.function("format_type", String.class,
            f("a", "atttypid", Long.class), f("a", "atttypmod", Integer.class));
        Field<String> generated = f("a", "attgenerated", String.class);
        Field<String> identity = f("a", "attidentity", String.class);
        Field<Boolean> notNull = f("a", "attnotnull", Boolean.class);
        Field<Boolean> hasDef = f("a", "atthasdef", Boolean.class);
        var rows = ctx.select(name, type, generated, identity, notNull, hasDef)
            .from(DSL.table(DSL.name("pg_attribute")).as("a"))
            .join(DSL.table(DSL.name("pg_class")).as("c"))
            .on(f("a", "attrelid", Long.class).eq(f("c", "oid", Long.class)))
            .join(DSL.table(DSL.name("pg_namespace")).as("n"))
            .on(f("c", "relnamespace", Long.class).eq(f("n", "oid", Long.class)))
            .where(f("n", "nspname", String.class).eq(table.schema()))
            .and(f("c", "relname", String.class).eq(table.table()))
            .and(f("a", "attnum", Short.class).gt((short) 0))
            .and(f("a", "attisdropped", Boolean.class).isFalse())
            .orderBy(f("a", "attnum", Short.class))
            .fetch();
        List<TargetColumn> cols = new ArrayList<>();
        for (Record r : rows) {
            String gen = r.get(generated);
            if (gen != null && !gen.isEmpty() && gen.charAt(0) != 0) continue;
            String ident = r.get(identity);
            boolean defaulted = Boolean.TRUE.equals(r.get(hasDef)) || (ident != null && !ident.isEmpty() && ident.charAt(0) != 0);
            cols.add(new TargetColumn(r.get(name), r.get(type), Boolean.TRUE.equals(r.get(notNull)), defaulted));
        }
        return cols;
    }

    /** The names among {@code models} that the target's {@code embedding_models} does not hold. */
    List<String> missingModels(Collection<String> models) {
        if (models.isEmpty()) return List.of();
        try (Connection conn = dataSource.getConnection()) {
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            Field<String> model = DSL.field(DSL.name("embedding_models", "embedding_model"), String.class);
            Set<String> have = new LinkedHashSet<>(ctx.select(model)
                .from(DSL.table(DSL.name("nexus", "embedding_models"))).where(model.in(models)).fetch(model));
            List<String> missing = new ArrayList<>();
            for (String m : models) if (!have.contains(m)) missing.add(m);
            return missing;
        } catch (SQLException e) {
            throw new IllegalStateException("could not read embedding_models", e);
        }
    }

    // ── emptiness ───────────────────────────────────────────────────────────────────────────────

    /**
     * The first {@code data} table, in registry order, that holds a row of the stamped tenant other than its seeded
     * rows. {@code ctx} runs in a transaction that has the tenant stamped.
     */
    static Optional<String> firstNonEmptyDataTable(DSLContext ctx, String tenant) {
        for (ExportTable t : TenantExport.TABLES) {
            if (t.exportClass() != ExportClass.DATA) continue;
            Condition rows = DSL.trueCondition();
            if (t.primaryKey().stream().anyMatch(c -> "tenant_id".equals(c.name()))) {
                rows = DSL.field(DSL.name("tenant_id"), String.class).eq(tenant);
            }
            Condition seeded = seededCondition(t, tenant, name -> DSL.field(DSL.name(name), String.class));
            if (seeded != null) rows = rows.and(DSL.not(seeded));
            if (ctx.fetchExists(ctx.selectOne().from(DSL.table(DSL.name(t.schema(), t.table()))).where(rows))) {
                return Optional.of(t.name());
            }
        }
        return Optional.empty();
    }

    /** True when the stamped tenant holds any row of {@code derived} table {@code t}. */
    static boolean derivedHasRows(DSLContext ctx, ExportTable t) {
        return ctx.fetchExists(ctx.selectOne().from(DSL.table(DSL.name(t.schema(), t.table()))));
    }

    /**
     * A condition that is true for a row whose key is one of the table's seeded keys that applies to {@code tenant}
     * (a default-tenant-only key applies to {@code default} alone); null when none applies. {@code column} names the
     * row's key columns, so the same rule reads the target and a stage.
     */
    static Condition seededCondition(ExportTable t, String tenant, Function<String, Field<String>> column) {
        List<String> keyColumns = t.primaryKey().stream().map(Column::name).filter(n -> !"tenant_id".equals(n)).toList();
        Condition any = null;
        for (TenantExport.SeededKey k : t.seededKeys()) {
            if (k.defaultTenantOnly() && !TenantConstants.DEFAULT_TENANT.equals(tenant)) continue;
            Condition one = DSL.trueCondition();
            for (int i = 0; i < keyColumns.size(); i++) {
                one = one.and(column.apply(keyColumns.get(i)).eq(k.values().get(i)));
            }
            any = any == null ? one : any.or(one);
        }
        return any;
    }

    /** Runs {@code work} in its own short READ COMMITTED transaction on a pooled connection with {@code tenant} stamped. */
    <T> T inTenantTransaction(String tenant, Function<DSLContext, T> work) {
        Connection conn = null;
        try {
            conn = dataSource.getConnection();
            conn.setAutoCommit(false);
            conn.setTransactionIsolation(Connection.TRANSACTION_READ_COMMITTED);
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            PgSession.GucBatch stamp = PgSession.gucBatch(ctx).stamp(TenantScope.DEFAULT_TENANT_GUC, tenant);
            stamp.set("statement_timeout", "30000");
            stamp.apply(conn);
            return work.apply(ctx);
        } catch (SQLException e) {
            throw new IllegalStateException("import check failed", e);
        } finally {
            if (conn != null) {
                try {
                    conn.rollback();
                } catch (SQLException ignored) {
                    // the connection goes back either way
                }
                try {
                    conn.setAutoCommit(true);
                    conn.close();
                } catch (SQLException ignored) {
                    // nothing more to do
                }
            }
        }
    }

    // ── the stage ───────────────────────────────────────────────────────────────────────────────

    /**
     * Creates, on the session's held connection, one stage per file table that loads (columns exactly the file's, in
     * the file's order) and one id map per table that reassigns ids or is the parent of a soft reference. Returns the
     * stages by table name.
     */
    Map<String, Stage> createStages(DSLContext ctx, Map<String, TablePlan> plan) {
        Map<String, Stage> stages = new LinkedHashMap<>();
        int n = 0;
        for (TablePlan tp : plan.values()) {
            if (!tp.loads()) continue;
            String stage = STAGE_PREFIX + n;
            List<Field<?>> columns = new ArrayList<>();
            for (Column c : tp.fileColumns()) {
                columns.add(DSL.field(DSL.name(c.name()), new DefaultDataType<>(SQLDialect.POSTGRES, Object.class, c.type())));
            }
            ctx.createTemporaryTable(DSL.name(stage)).columns(columns).onCommitDrop().execute();
            stages.put(tp.name(), new Stage(tp.name(), stage, null));
            n++;
        }
        // an id map for every registry table whose id is reassigned and that is loaded, or that a soft reference reads
        Set<String> mapped = new LinkedHashSet<>();
        for (ExportTable t : TenantExport.TABLES) {
            if (!t.exported() || t.reassignedId() == null) continue;
            boolean loaded = stages.containsKey(t.name());
            boolean softParent = TenantExport.TABLES.stream().flatMap(x -> x.references().stream())
                .anyMatch(r -> r.softRule() != null && r.parentName().equals(t.name()));
            if (loaded || softParent) mapped.add(t.name());
        }
        int m = 0;
        for (String table : mapped) {
            String map = MAP_PREFIX + m++;
            ctx.createTemporaryTable(DSL.name(map))
                .column("old_id", SQLDataType.BIGINT.nullable(false))
                .column("new_id", SQLDataType.BIGINT.nullable(false))
                .constraints(DSL.primaryKey("old_id"))
                .onCommitDrop().execute();
            Stage s = stages.get(table);
            stages.put(table, new Stage(table, s == null ? null : s.stageName(), map));
        }
        return stages;
    }

    /**
     * Copies one part, a complete binary COPY stream, into {@code stage}; returns the rows it held. The statement is
     * the one COPY form (no jOOQ form exists), its identifiers rendered by jOOQ from names this class made.
     *
     * @throws SQLException a malformed stream, a full disk or a timeout: the session's transaction is then aborted
     */
    long copyPart(Connection held, TablePlan tp, Stage stage, byte[] part) throws SQLException, IOException {
        DSLContext ctx = DSL.using(held, SQLDialect.POSTGRES);
        StringBuilder columns = new StringBuilder();
        for (Column c : tp.fileColumns()) {
            if (columns.length() > 0) columns.append(", ");
            columns.append(ctx.render(DSL.name(c.name())));
        }
        String copy = "COPY " + ctx.render(DSL.name(stage.stageName())) + " (" + columns + ") FROM STDIN (FORMAT binary)";
        return held.unwrap(PGConnection.class).getCopyAPI().copyIn(copy, new ByteArrayInputStream(part));
    }

    /** Rows currently in {@code stage}. */
    static long stagedRows(DSLContext ctx, Stage stage) {
        return ctx.fetchCount(DSL.table(DSL.name(stage.stageName())));
    }

    static Table<?> stageTable(Stage stage) {
        return DSL.table(DSL.name(stage.stageName()));
    }
}
