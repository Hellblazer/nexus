// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.TenantExport.Column;
import dev.nexus.service.db.TenantExport.ExportTable;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.postgresql.PGConnection;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.io.IOException;
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;

/**
 * The reads of a tenant export session (RDR-248, bead nexus-csayy.2): the header and each page, every one
 * its own short {@code REPEATABLE READ READ ONLY} transaction whose FIRST statement is
 * {@code SET TRANSACTION SNAPSHOT '<id>'} (Postgres refuses it later, and refuses it under READ COMMITTED),
 * followed by the tenant stamp. The tenant is the session owner's and is an argument here, so no request
 * text reaches the stamp. The transaction is rolled back and its connection returned before the caller
 * sends a byte to the client, so a slow client holds no lock and no pool connection.
 *
 * <p>A table read here is the registry's ({@link TenantExport#exportable}): the table and column names in
 * the statement come from the registry and from the live catalog, never from a request, and the key bounds
 * are typed values rendered by jOOQ, never text spliced in. Columns are the table's non-generated ones in
 * attribute order, which is the order of every row of the page.
 */
public final class TenantExportReads {

    private static final Logger log = LoggerFactory.getLogger(TenantExportReads.class);

    /** What Postgres calls a snapshot id: hex words joined by dashes ({@code 00000003-0000001B-1}). */
    private static final Pattern SNAPSHOT_ID = Pattern.compile("[0-9A-Fa-f]+(?:-[0-9A-Fa-f]+)*");

    /** SQLSTATE of {@code SET TRANSACTION SNAPSHOT} naming a snapshot that no longer exists (UndefinedObject). */
    private static final String UNDEFINED_OBJECT = "42704";

    /** SQLSTATE of a lock wait that ran out of {@code lock_timeout}. */
    private static final String LOCK_NOT_AVAILABLE = "55P03";

    /** SQLSTATE of a statement cancelled by {@code statement_timeout} (the page's time budget). */
    private static final String QUERY_CANCELED = "57014";

    /** The snapshot is gone: the session's holding transaction ended (closed, expired or its connection died). */
    public static final class SnapshotGoneException extends RuntimeException {
        public SnapshotGoneException(String snapshotId) {
            super("snapshot " + snapshotId + " no longer exists");
        }
    }

    /**
     * A read could not take a table lock inside its lock bound, or ran past its time budget (statement timeout);
     * retryable.
     */
    public static final class PageBusyException extends RuntimeException {
        public PageBusyException(Throwable cause) {
            super("export read waited too long for a table lock or ran past its time budget", cause);
        }

        PageBusyException(String message) {
            super(message);
        }
    }

    /** A table's name, class, key and the columns of its rows, as the header reports them. */
    public record TableHeader(String name, TenantExport.ExportClass exportClass, List<String> primaryKey,
                              List<Column> columns) {
    }

    /** The export header's data part: every exportable table and the embedding models the tenant uses. */
    public record Header(List<TableHeader> tables, List<Map<String, Object>> embeddingModels) {
    }

    /**
     * One page. {@code lastKey} is the cursor of the page's last row, null for an empty page; {@code done} is
     * true when this page ended the table; {@code tableRows} is set on a table's first page only.
     */
    public record Page(byte[] buffer, int length, int rows, String lastKey, boolean done, Long tableRows) {
        /** A copy of the page's bytes ({@code buffer[0, length)}); the route writes the buffer directly. */
        public byte[] body() {
            return java.util.Arrays.copyOf(buffer, length);
        }
    }

    private final DataSource dataSource;
    private final int lockTimeoutMs;
    private final int budgetMs;

    /** Test seam: runs inside the page's transaction just before its COPY statement is rendered; null in production. */
    java.util.function.Consumer<DSLContext> beforeCopyForTests;
    /** Test seam: wraps the page's COPY sink (a test's sink can fail where the real one cannot); null in production. */
    java.util.function.UnaryOperator<java.io.OutputStream> sinkWrapperForTests;

    /**
     * @param lockTimeoutMs the bound on a read's wait for any table lock
     * @param budgetMs      the bound on one read's database time, all its statements together
     */
    public TenantExportReads(DataSource dataSource, int lockTimeoutMs, int budgetMs) {
        this.dataSource = dataSource;
        this.lockTimeoutMs = lockTimeoutMs;
        this.budgetMs = budgetMs;
    }

    // ── the header ───────────────────────────────────────────────────────────────────────────────

    /** Columns and types of every exportable table, and the tenant's embedding models, in one snapshot. */
    public Header header(String snapshotId, String tenant) {
        return inSnapshot(snapshotId, tenant, txn -> {
            DSLContext ctx = txn.ctx;
            List<TableHeader> tables = new ArrayList<>();
            for (ExportTable t : TenantExport.TABLES) {
                if (!t.exported()) continue;
                List<Column> columns = liveColumns(ctx, t).stream().map(LiveColumn::column).toList();
                tables.add(new TableHeader(t.name(), t.exportClass(),
                    t.primaryKey().stream().map(Column::name).toList(), columns));
            }
            var models = ctx.select(EMBEDDING_MODELS.fields())
                .from(EMBEDDING_MODELS)
                .where(EMBEDDING_MODELS.EMBEDDING_MODEL.in(
                    ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL).from(CATALOG_COLLECTIONS)
                        .union(ctx.select(EMBEDDING_PROFILE.EMBEDDING_MODEL).from(EMBEDDING_PROFILE))))
                .orderBy(EMBEDDING_MODELS.EMBEDDING_MODEL)
                .fetch();
            List<Map<String, Object>> rows = new ArrayList<>();
            for (Record r : models) {
                Map<String, Object> row = new LinkedHashMap<>();
                for (Field<?> f : r.fields()) {
                    row.put(f.getName(), r.get(f));
                }
                rows.add(row);
            }
            return new Header(List.copyOf(tables), List.copyOf(rows));
        });
    }

    // ── a page ───────────────────────────────────────────────────────────────────────────────────

    /**
     * The page of {@code table} after the key {@code after} (null for the first page): up to {@code maxRows}
     * rows in primary-key order, cut at {@code targetBytes}. The first page also counts the table's rows in
     * the snapshot.
     */
    public Page page(String snapshotId, String tenant, ExportTable table, List<Object> after, int targetBytes,
                     int maxRows) {
        return inSnapshot(snapshotId, tenant, txn -> {
            DSLContext ctx = txn.ctx;
            List<LiveColumn> columns = liveColumns(ctx, table);
            Long tableRows = null;
            if (after == null) {
                rebound(txn);
                tableRows = ctx.select(DSL.count()).from(sourceOf(table)).where(scope(columns, tenant))
                    .fetchOne(0, Long.class);
            }
            var select = pageSelect(ctx, table, columns, tenant, after, maxRows);
            rebound(txn);
            if (beforeCopyForTests != null) beforeCopyForTests.accept(ctx);
            String copy = copyStatement(ctx, select);
            BinaryCopyPage sink = new BinaryCopyPage(targetBytes, maxRows);
            java.io.OutputStream target = sinkWrapperForTests == null ? sink : sinkWrapperForTests.apply(sink);
            boolean finished = false;
            try {
                copyOut(txn.conn, copy, target);
                finished = true;
            } catch (BinaryCopyPage.PageFull cut) {
                // the byte target: the copy was cancelled, the page ends at its last whole row
                finished = true;
                txn.discard = true;   // the cancel leaves the driver behind the server: see release()
            } catch (IOException | SQLException e) {
                throw retryable(e) ? new PageBusyException(e) : new IllegalStateException(
                    "export page copy failed for " + table.name(), e);
            } finally {
                // any other way out (a failed copy, a sink that threw, a cancelled statement) leaves the connection
                // in a protocol state nobody checked, and the driver's cancel may still be in flight: not reusable
                if (!finished) txn.discard = true;
            }
            int[] keyIndexes = table.primaryKey().stream()
                .mapToInt(k -> indexOf(columns, k.name(), table)).toArray();
            BinaryCopyPage.Result result = sink.finish(keyIndexes);
            String lastKey = null;
            if (result.rows() > 0) {
                List<String> parts = new ArrayList<>();
                for (int i = 0; i < keyIndexes.length; i++) {
                    parts.add(TenantExportKeys.render(table.primaryKey().get(i), result.lastKeyFields().get(i)));
                }
                lastKey = TenantExportKeys.encode(parts);
            }
            return new Page(result.buffer(), result.length(), result.rows(), lastKey, result.exhausted(), tableRows);
        });
    }

    private static Table<?> sourceOf(ExportTable table) {
        return DSL.table(DSL.name(table.schema(), table.table()));
    }

    /**
     * RLS already restricts every row to the stamped tenant; saying it in the statement as well lets the planner
     * prune a partitioned table to the tenant's leaf when it plans, not only when it runs.
     */
    private static Condition scope(List<LiveColumn> columns, String tenant) {
        if (columns.stream().anyMatch(c -> "tenant_id".equals(c.column().name()))) {
            return DSL.field(DSL.name("tenant_id"), String.class).eq(text(tenant));
        }
        return DSL.trueCondition();
    }

    /**
     * The page's query: the table's non-generated columns, the tenant's rows, strictly after {@code after} (null
     * for a first page) by one row-value comparison over the whole primary key, in primary-key order, at most
     * {@code maxRows}. Package-private so the plan test explains exactly this statement.
     */
    static org.jooq.Select<?> pageSelect(DSLContext ctx, ExportTable table, List<LiveColumn> columns, String tenant,
                                         List<Object> after, int maxRows) {
        List<Field<?>> keyFields = new ArrayList<>();
        for (Column k : table.primaryKey()) {
            keyFields.add(DSL.field(DSL.name(k.name()), TenantExportKeys.dataType(k.type())));
        }
        Condition condition = scope(columns, tenant);
        if (after != null) {
            condition = condition.and(keyAfter(keyFields, after));
        }
        return ctx.select(columns.stream().map(c -> DSL.field(DSL.name(c.column().name()))).toList())
            .from(sourceOf(table))
            .where(condition)
            .orderBy(keyFields)
            .limit(maxRows);
    }

    /**
     * A text value as an expression that renders the same under every {@code standard_conforming_strings}: its UTF-8
     * bytes as hex, decoded by the server. A COPY takes no bind parameters, so a key reaches the statement as
     * inlined text, and a quoted literal means one thing with the setting on (a backslash is a backslash) and
     * another with it off (a backslash escapes): hex digits mean the same in both, so no key text can end the
     * expression, whatever the setting is.
     */
    static Field<String> text(String value) {
        String hex = java.util.HexFormat.of().formatHex(value.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        // convert_from's encoding argument is a name, which carries the C collation, and a comparison would then
        // sort by it instead of by the column's: the explicit collation puts the value back on the database default,
        // the column's (a key compared under another collation than its index's is a wrong page, not an error)
        return DSL.function("convert_from", SQLDataType.VARCHAR,
            DSL.function("decode", SQLDataType.BLOB, DSL.val(hex), DSL.inline("hex")), DSL.inline("UTF8"))
            .collate("default");
    }

    /** The key is greater than {@code after}: one row-value comparison over the whole primary key (gate N1). */
    private static Condition keyAfter(List<Field<?>> keyFields, List<Object> after) {
        return rowGreater(keyFields, after);
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private static Condition rowGreater(List<Field<?>> keyFields, List<Object> after) {
        Field[] fields = keyFields.toArray(new Field[0]);
        Field[] values = new Field[fields.length];
        for (int i = 0; i < fields.length; i++) {
            values[i] = after.get(i) instanceof String text ? text(text) : DSL.val(after.get(i), fields[i].getDataType());
        }
        return DSL.row(fields).gt(DSL.row(values));
    }

    /**
     * SANCTIONED RAW (nexus-csayy.2): {@code COPY (query) TO STDOUT (FORMAT binary)} has no jOOQ form. The
     * query inside is a typed jOOQ select rendered with its key bounds inlined (a COPY takes no bind
     * parameters); only the fixed COPY wrapper is text.
     */
    private static String copyStatement(DSLContext ctx, org.jooq.Select<?> query) {
        return "COPY (" + ctx.renderInlined(query) + ") TO STDOUT (FORMAT binary)";
    }

    private static void copyOut(Connection conn, String copy, java.io.OutputStream sink) throws IOException, SQLException {
        conn.unwrap(PGConnection.class).getCopyAPI().copyOut(copy, sink);
    }

    /** A lock wait that ran out of its bound, or a statement cancelled at the time budget: both are retried. */
    private static boolean retryable(Throwable e) {
        for (Throwable c = e; c != null; c = c.getCause()) {
            if (c instanceof SQLException s
                    && (LOCK_NOT_AVAILABLE.equals(s.getSQLState()) || QUERY_CANCELED.equals(s.getSQLState()))) {
                return true;
            }
        }
        return false;
    }

    /**
     * Restarts the statement bound at what is left of the read's budget, so the statements of one read share one
     * budget instead of each having its own. Spent: the read is refused as busy, before it starts another statement.
     */
    private static void rebound(Txn txn) {
        long left = (txn.deadlineNanos - System.nanoTime()) / 1_000_000L;
        if (left < 1) {
            throw new PageBusyException("the export read used its whole time budget before its next statement");
        }
        PgSession.setLocal(txn.ctx, "statement_timeout", Long.toString(left));
    }

    private static int indexOf(List<LiveColumn> columns, String name, ExportTable table) {
        for (int i = 0; i < columns.size(); i++) {
            if (columns.get(i).column().name().equals(name)) return i;
        }
        throw new IllegalStateException(table.name() + " primary-key column " + name + " is not an exported column");
    }

    // ── the live catalog ─────────────────────────────────────────────────────────────────────────

    record LiveColumn(Column column) {
    }

    private static <T> Field<T> f(String alias, String column, Class<T> type) {
        return DSL.field(DSL.name(alias, column), type);
    }

    /** The table's non-generated columns, in attribute order, with their {@code format_type} text. */
    static List<LiveColumn> liveColumns(DSLContext ctx, ExportTable table) {
        Field<String> name = f("a", "attname", String.class);
        Field<String> type = DSL.function("format_type", String.class,
            f("a", "atttypid", Long.class), f("a", "atttypmod", Integer.class));
        Field<String> generated = f("a", "attgenerated", String.class);
        var rows = ctx.select(name, type, generated)
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
        List<LiveColumn> columns = new ArrayList<>();
        for (Record r : rows) {
            String gen = r.get(generated);
            if (gen != null && !gen.isEmpty() && gen.charAt(0) != 0) continue;   // a generated column is recomputed
            columns.add(new LiveColumn(new Column(r.get(name), r.get(type))));
        }
        if (columns.isEmpty()) {
            throw new IllegalStateException("the registry names " + table.name() + " but the database has no such table");
        }
        return columns;
    }

    // ── the snapshot-importing transaction ───────────────────────────────────────────────────────

    /** One snapshot-importing transaction as the read sees it; {@code discard} marks the connection unfit for reuse. */
    private static final class Txn {
        private final Connection conn;
        private final DSLContext ctx;
        private final long deadlineNanos;
        private boolean discard;

        private Txn(Connection conn, DSLContext ctx, long deadlineNanos) {
            this.conn = conn;
            this.ctx = ctx;
            this.deadlineNanos = deadlineNanos;
        }
    }

    private <T> T inSnapshot(String snapshotId, String tenant, java.util.function.Function<Txn, T> work) {
        if (snapshotId == null || !SNAPSHOT_ID.matcher(snapshotId).matches()) {
            throw new IllegalArgumentException("not a snapshot id: " + snapshotId);
        }
        if (tenant == null || tenant.isBlank()) {
            throw new IllegalArgumentException("tenant must not be null or blank");
        }
        Connection conn = null;
        Txn txn = null;
        try {
            conn = dataSource.getConnection();
            conn.setAutoCommit(false);
            conn.setTransactionIsolation(Connection.TRANSACTION_REPEATABLE_READ);
            conn.setReadOnly(true);
            importSnapshot(conn, snapshotId);
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            PgSession.GucBatch stamp = PgSession.gucBatch(ctx).stamp(TenantScope.DEFAULT_TENANT_GUC, tenant);
            PgSession.setLockTimeout(stamp, lockTimeoutMs);
            stamp.set("statement_timeout", Integer.toString(budgetMs));   // the read's time budget, restarted per statement
            long deadline = System.nanoTime() + budgetMs * 1_000_000L;
            stamp.apply(conn);
            txn = new Txn(conn, ctx, deadline);
            return work.apply(txn);
        } catch (org.jooq.exception.DataAccessException e) {
            // a statement inside the read (jOOQ wraps the driver's exception): a lock wait that ran out is retryable
            if (retryable(e)) throw new PageBusyException(e);
            throw e;
        } catch (SQLException e) {
            throw retryable(e) ? new PageBusyException(e) : new IllegalStateException("export read failed", e);
        } finally {
            release(conn, txn != null && txn.discard);
        }
    }

    /**
     * SANCTIONED RAW (nexus-csayy.2): {@code SET TRANSACTION SNAPSHOT} has no jOOQ form and no function form.
     * The id is Postgres's own text, checked against {@link #SNAPSHOT_ID} before it reaches the statement.
     */
    private static void importSnapshot(Connection conn, String snapshotId) throws SQLException {
        try (Statement st = conn.createStatement()) {
            st.execute("SET TRANSACTION SNAPSHOT '" + snapshotId + "'");
        } catch (SQLException e) {
            if (UNDEFINED_OBJECT.equals(e.getSQLState())) {
                throw new SnapshotGoneException(snapshotId);
            }
            throw e;
        }
    }

    private void release(Connection conn, boolean discard) {
        if (conn == null) return;
        if (discard) {
            if (dataSource instanceof com.zaxxer.hikari.HikariDataSource pool) {
                pool.evictConnection(conn);   // the pool closes it on its next pass instead of handing it out again
            }
            try {
                conn.close();
            } catch (SQLException | RuntimeException e) {
                log.debug("event=export_read_close_after_discard error={}", e.getMessage());
            }
            return;
        }
        try {
            conn.rollback();
        } catch (SQLException e) {
            log.warn("event=export_read_rollback_failed error={}", e.getMessage());
        }
        try {
            conn.setReadOnly(false);
            conn.setAutoCommit(true);
        } catch (SQLException e) {
            log.warn("event=export_read_restore_failed error={}", e.getMessage());
        }
        try {
            conn.close();
        } catch (SQLException e) {
            log.warn("event=export_read_close_failed error={}", e.getMessage());
        }
    }
}
