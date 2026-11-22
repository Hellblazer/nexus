// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.vectors.PciBuilderSession;
import org.jooq.DSLContext;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Properties;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22, linda's ruling 2026-11-22, from elsinore's P3.5c mapping and P3.4's arbiter census; linda's rulings 2026-11-22): the id-keyed indexes P3.4 builds at run time. Eight UNIQUE arbiters, which that the
 * id-keyed upserts of P3.5c take as their ON CONFLICT arbiters, and without which P3.7's one-row rename could make duplicate rows. They are
 * built at run time, after the engine serves, in the reconciler's pattern ({@code LeafIndexPass}'s, minus the partition route: none of the
 * tables is partitioned): one {@code CREATE [UNIQUE] INDEX CONCURRENTLY} at a time, on its own admin connection, under an advisory lock
 * of its own ({@link #LOCK_KEY}), with an INVALID leftover of the pass's name dropped before the build. No changeset carries them, so the
 * migration walk's DDL count does not.
 *
 * <p><b>Gate 2.</b> The pass starts only when gate 2 of the completion check holds (every stamp move, every P3.3a move, both P3.3c swaps and
 * their three validation moves complete): before it a row may still lack its id, and a chunk-family {@code on_complete} ends builds in
 * flight ({@code VerifyingFinisher}). Gate 3 requires {@link #allValid} (P3.4 is not complete while one of them is missing).
 *
 * <p><b>The supporting indexes.</b> Beside the arbiters, the plain (tenant_id, id) indexes that support P3.3a's id-twin foreign keys where no
 * arbiter's leading columns already do: out of the boot walk by linda's ruling (2026-11-22), because an upgrade boot may write at most
 * 64 MiB and the RDR builds no index on a user-data-growing table inside it. The retired queue's applies only where its table exists with
 * its id column. A unique violation can only come from an arbiter.
 *
 * <p><b>A duplicate is corruption, not data.</b> Before P3.7 each collection name maps to exactly one id and each table's name-keyed unique
 * already forbids the duplicate, so a unique violation (23505) on one of these builds means the table is corrupt. The pass then drops the
 * INVALID leftover, logs the duplicate keys (at most {@link #DUPLICATES_LOGGED}), keeps a {@code boot_work} entry that says so in words,
 * and does not build that index again in this engine's life: nothing loops. Gate 3 stays false, so the rename keeps its fourteen-table name
 * path. Any other failure (a lock wait, a cancelled build) is retried at the next tick. The status text names the table, never a tenant or
 * a key value (the status is unauthenticated); the values are in the log.
 *
 * <p>Visible to the migration walk as the PCI builder is: its {@code application_name} is under the builders' prefix, so
 * {@code PciBuilders.terminate} ends it, and it reads the migration advisory lock before each index it builds.
 */
public final class IdUniqueIndexPass implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(IdUniqueIndexPass.class);

    public static final String APPLICATION_NAME = PciBuilderSession.APPLICATION_NAME_PREFIX + "id-unique-index-pass";
    public static final String BOOT_WORK_ID = "id-unique-index-pass";
    /** ASCII "iduqidxp". */
    public static final long LOCK_KEY = 0x6964757169647870L;

    static final String WHAT = "building the unique id indexes the rename's upserts use";
    static final String UNIT = "indexes";
    static final String ON_STOP = "a build in flight keeps running in the database and finishes there; one that is ended is dropped and built again "
        + "by the next engine";
    static final Duration INITIAL_DELAY = Duration.ofSeconds(30);
    static final Duration PERIOD = Duration.ofSeconds(60);
    static final Duration BUILD_STATEMENT_TIMEOUT = Duration.ofHours(6);
    static final int DUPLICATES_LOGGED = 5;

    /**
     * One index: its table, name, key columns and partial predicate ({@code null} for none) in the form {@code pg_get_expr} deparses it,
     * whether it is UNIQUE (an arbiter) or plain (a foreign key's supporting index), and whether its table may be absent (the retired queue:
     * then the index is not applicable and counts as in place).
     */
    public record Spec(String table, String index, String columns, String predicate, boolean unique, boolean optional) {
        static Spec arbiter(String table, String index, String columns, String predicate) {
            return new Spec(table, index, columns, predicate, true, false);
        }

        static Spec support(String table, String index, String columns, boolean optional) {
            return new Spec(table, index, columns, null, false, optional);
        }

        String definition() {
            return "CREATE " + (unique ? "UNIQUE " : "") + "INDEX CONCURRENTLY " + index + " ON nexus." + table + " (" + columns + ")"
                + (predicate == null ? "" : " WHERE " + predicate);
        }
    }

    /**
     * The arbiters ({@link ArbiterTwins#builtByThePass}), then the four supporting indexes. The topics and reembed_jobs predicates are exact; the topics one is exactly the upsert's ({@code ON CONFLICT (tenant_id, collection_id, label) WHERE parent_id IS NULL}),
     * or PostgreSQL cannot infer the index as its arbiter.
     */
    public static final List<Spec> SPECS = java.util.stream.Stream.concat(
        ArbiterTwins.builtByThePass().stream().map(a -> Spec.arbiter(a.table(), a.twinIndex(), a.idColumns(), a.predicate())),
        java.util.stream.Stream.of(
        // The supporting indexes of P3.3a's id twins (linda 2026-11-22: out of the boot walk, which an upgrade boot's 64 MiB write bound and
        // the RDR's no-index-builds-at-boot rule forbid) that no arbiter already covers with its leading (tenant_id, id) columns:
        // document_aspects, taxonomy_meta and canary_query_ring are covered; topics' arbiter is partial, which IdKeyIndexCensus does not count.
        Spec.support("topic_assignments", "idx_ta_tenant_source_collection_id", "tenant_id, source_collection_id", false),
        Spec.support("topics", "idx_topics_tenant_collection_id", "tenant_id, collection_id", false),
        Spec.support("document_highlights", "idx_doc_highlights_collection_id", "tenant_id, collection_id", false),
        Spec.support("aspect_extraction_queue", "idx_aspect_queue_collection_id", "tenant_id, collection_id", true))).toList();

    /** Gate 2 of the completion check; production wires the completion reporter's latest read. */
    @FunctionalInterface
    public interface Gate2 {
        boolean holds();
    }

    /** What one pass did: whether it ran, how many it built, how many leftovers it dropped, and the tables found corrupt so far. */
    public record Result(boolean ran, int built, int dropped, List<String> corrupt) { }

    private final AdminConnection admin;
    private final BootWorkRegistry registry;
    private final Gate2 gate2;
    /** Indexes whose build hit a duplicate, by index name, with the status text: not built again in this engine's life. Guarded by {@link #passLock}. */
    private final Map<String, String> corrupt = new LinkedHashMap<>();
    private final Object passLock = new Object();
    private volatile Consumer<String> beforeStatement;
    private boolean finished;
    private ScheduledExecutorService scheduler;

    public IdUniqueIndexPass(AdminConnection admin, BootWorkRegistry registry, Gate2 gate2) {
        this.admin = Objects.requireNonNull(admin, "admin");
        this.registry = Objects.requireNonNull(registry, "registry");
        this.gate2 = Objects.requireNonNull(gate2, "gate2");
    }

    /** Test seam: sees each statement just before it is sent. */
    void beforeStatement(Consumer<String> hook) {
        this.beforeStatement = hook;
    }

    /** True when every applicable index exists and is VALID with its definition: what gate 3 requires. A failed read is false. */
    public static boolean allValid(DSLContext ctx) {
        try {
            for (Spec s : SPECS) {
                if (!valid(ctx, s)) {
                    return false;
                }
            }
            return true;
        } catch (DataAccessException e) {
            log.warn("event=id_unique_index_valid_unread cause=\"{}\"", oneLine(e));
            return false;
        }
    }

    /** The index of {@code s}'s name is VALID and is {@code s}'s definition (unique, its columns, its predicate). */
    static boolean valid(DSLContext ctx, Spec s) {
        if (s.optional() && !applicable(ctx, s)) {
            return true;   // the retired queue table is gone, or has no id column: nothing to support
        }
        // catalog fields, not pg_get_indexdef's text, which schema-qualifies by the reader's search_path (review r1, Minor 7)
        var r = ctx.fetchOne("""
            SELECT i.indisvalid AND i.indisunique = ? AND t.relname = ? AS shape,
                   (SELECT string_agg(a.attname, ', ' ORDER BY k.ord) FROM generate_series(0, i.indnkeyatts - 1) k(ord)
                      JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[k.ord]) AS cols,
                   pg_catalog.pg_get_expr(i.indpred, i.indrelid) AS pred
              FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
              JOIN pg_catalog.pg_class t ON t.oid = i.indrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'nexus' AND c.relname = ?""", s.unique(), s.table(), s.index());
        if (r == null || !Boolean.TRUE.equals(r.get("shape", Boolean.class))) {
            return false;
        }
        String pred = r.get("pred", String.class);
        String want = s.predicate() == null ? null : "(" + s.predicate() + ")";
        return s.columns().equals(r.get("cols", String.class)) && Objects.equals(want, pred);
    }

    /** The table exists with its id column (the first id-named column of the key). */
    static boolean applicable(DSLContext ctx, Spec s) {
        String idColumn = java.util.Arrays.stream(s.columns().split(",\\s*")).filter(c -> c.endsWith("collection_id")).findFirst().orElseThrow();
        return ctx.fetchExists(DSL.selectOne().from("pg_catalog.pg_attribute a")
            .where("a.attrelid = to_regclass(?) AND a.attname = ? AND NOT a.attisdropped", "nexus." + s.table(), idColumn));
    }

    /** The status text for a table whose build hit a duplicate on the id key. */
    static String corruptionText(String table) {
        return "the unique id index on " + table + " was not built: two rows share an id key, which means the table is corrupt (each "
            + "collection name maps to one id, and the name-keyed unique already forbids the duplicate). P3.4 is not complete, so the rename "
            + "stays on its fourteen-table name path until the rows are repaired; the duplicate keys are in the engine log "
            + "(event=id_unique_index_duplicate), and the next engine start tries the build again";
    }

    public synchronized void start() {
        if (scheduler != null) {
            return;
        }
        scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "id-unique-index-pass");
            t.setDaemon(true);
            return t;
        });
        scheduler.scheduleWithFixedDelay(() -> {
            try {
                runOnce();
            } catch (VirtualMachineError e) {
                throw e;
            } catch (Throwable e) {
                log.warn("event=id_unique_index_pass_failed cause=\"{}\"", e.toString());
            }
        }, INITIAL_DELAY.toSeconds(), PERIOD.toSeconds(), TimeUnit.SECONDS);
    }

    @Override
    public synchronized void close() {
        if (scheduler != null) {
            scheduler.shutdownNow();
            scheduler = null;
        }
    }

    /** One pass, now. A gate that does not hold ends it before any connection opens. */
    public Result runOnce() {
        synchronized (passLock) {
            if (finished) {
                return new Result(true, 0, 0, corruptTables());
            }
            boolean held;
            try {
                held = gate2.holds();
            } catch (RuntimeException e) {
                log.warn("event=id_unique_index_pass_gate_unread cause=\"{}\"", oneLine(e));
                held = false;
            }
            if (!held) {
                return new Result(false, 0, 0, corruptTables());
            }
            try (Connection conn = open()) {
                if (conn == null) {
                    return new Result(false, 0, 0, corruptTables());
                }
                return run(conn);
            } catch (SQLException | DataAccessException e) {
                log.warn("event=id_unique_index_pass_aborted cause=\"{}\"", oneLine(e));
                return new Result(false, 0, 0, corruptTables());
            }
        }
    }

    private Connection open() throws SQLException {
        Properties props = new Properties();
        props.setProperty("user", admin.user());
        props.setProperty("password", admin.password());
        props.setProperty("ApplicationName", APPLICATION_NAME);
        props.setProperty("connectTimeout", "10");
        props.setProperty("loginTimeout", "30");
        props.setProperty("socketTimeout", Long.toString(BUILD_STATEMENT_TIMEOUT.toSeconds() + 5 * 60));
        props.setProperty("tcpKeepAlive", "true");
        Connection conn = DriverManager.getConnection(admin.url(), props);
        try {
            conn.setAutoCommit(true);
            var ctx = DSL.using(conn, org.jooq.SQLDialect.POSTGRES);
            if (!Boolean.TRUE.equals(ctx.select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN, DSL.val(LOCK_KEY))).fetchOne(0, Boolean.class))) {
                conn.close();
                return null;
            }
            return conn;
        } catch (SQLException | RuntimeException e) {
            try {
                conn.close();
            } catch (SQLException suppressed) {
                e.addSuppressed(suppressed);
            }
            throw e;
        }
    }

    private Result run(Connection conn) throws SQLException {
        var ctx = DSL.using(conn, org.jooq.SQLDialect.POSTGRES);
        List<Spec> todo = new ArrayList<>();
        for (Spec s : SPECS) {
            if (!corrupt.containsKey(s.index()) && !valid(ctx, s)) {
                todo.add(s);
            }
        }
        int built = 0;
        int dropped = 0;
        if (!todo.isEmpty()) {
            registry.register(BOOT_WORK_ID, WHAT, 0L, (long) todo.size(), UNIT, true, ON_STOP);
        }
        try {
            for (Spec s : todo) {
                if (SchemaMigrator.migrationLockHolderPid(ctx) >= 0) {
                    log.info("event=id_unique_index_pass_skipped reason=migration_in_progress");
                    break;
                }
                if (dropLeftover(conn, ctx, s)) {
                    dropped++;
                }
                try {
                    exec(conn, "SET lock_timeout = 0");
                    exec(conn, "SET statement_timeout = '" + BUILD_STATEMENT_TIMEOUT.toMillis() + "ms'");
                    exec(conn, s.definition());
                    built++;
                    registry.progress(BOOT_WORK_ID, (long) built, (long) todo.size());
                    log.info("event=id_unique_index_built table={} index={}", s.table(), s.index());
                } catch (SQLException e) {
                    if (s.unique() && "23505".equals(e.getSQLState())) {
                        duplicate(conn, ctx, s);
                    } else {
                        log.warn("event=id_unique_index_build_failed table={} cause=\"{}\" retry=next_tick", s.table(), oneLine(e));
                        if (connectionLost(e.getSQLState())) {
                            break;
                        }
                    }
                }
            }
        } finally {
            try {
                exec(conn, "RESET lock_timeout");
                exec(conn, "RESET statement_timeout");
            } catch (SQLException e) {   // a dead connection (the walk ended the backend): the session goes with it
                log.debug("event=id_unique_index_reset_skipped cause=\"{}\"", oneLine(e));
            }
            if (!corrupt.isEmpty()) {
                String text = String.join("; ", corrupt.values());
                registry.register(BOOT_WORK_ID, text, 0L, (long) corrupt.size(), UNIT, true, ON_STOP);
            } else if (built == todo.size()) {
                registry.finish(BOOT_WORK_ID);
            }   // otherwise the entry stays, with its count: work is left for the next tick
        }
        finished = corrupt.isEmpty() && allValid(ctx);
        return new Result(true, built, dropped, corruptTables());
    }

    /** A 23505: drop the INVALID leftover, log the duplicate keys, record the table as corrupt. Never retried in this engine's life. */
    private void duplicate(Connection conn, DSLContext ctx, Spec s) {
        corrupt.put(s.index(), corruptionText(s.table()));   // first: a failed drop below must not leave it to be rebuilt every tick
        try {
            dropLeftover(conn, ctx, s);
        } catch (SQLException | DataAccessException e) {
            log.warn("event=id_unique_index_drop_failed table={} cause=\"{}\" retry=next_engine_start", s.table(), oneLine(e));
        }
        List<String> keys = new ArrayList<>();
        try {
            String sql = "SELECT " + s.columns().replace(", ", "::text || '/' || ") + "::text AS k, count(*) AS n FROM nexus." + s.table()
                + (s.predicate() == null ? "" : " WHERE " + s.predicate()) + " GROUP BY " + s.columns()
                + " HAVING count(*) > 1 ORDER BY 1 LIMIT " + DUPLICATES_LOGGED;
            ctx.fetch(sql).forEach(r -> keys.add(r.get("k", String.class) + " x" + r.get("n", Long.class)));
        } catch (DataAccessException e) {
            keys.add("(could not read: " + oneLine(e) + ")");
        }
        log.error("event=id_unique_index_duplicate table={} index={} columns=\"{}\" duplicates=\"{}\" meaning=corruption retry=next_engine_start",
            s.table(), s.index(), s.columns(), String.join(", ", keys));
    }

    /** Drops an INVALID index of {@code s}'s name, reading {@code indisvalid} again just before; true when it dropped one. */
    private boolean dropLeftover(Connection conn, DSLContext ctx, Spec s) throws SQLException {
        if (!invalidExists(ctx, s)) {
            return false;
        }
        exec(conn, "SET lock_timeout = '5s'");
        if (!invalidExists(ctx, s)) {
            return false;
        }
        exec(conn, "DROP INDEX CONCURRENTLY IF EXISTS nexus." + s.index());
        log.info("event=id_unique_index_dropped reason=invalid_leftover table={}", s.table());
        return true;
    }

    private static boolean invalidExists(DSLContext ctx, Spec s) {
        return ctx.fetchExists(DSL.selectOne().from("pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid "
            + "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace")
            .where("n.nspname = 'nexus' AND c.relname = ? AND NOT i.indisvalid", s.index()));
    }

    private void exec(Connection conn, String sql) throws SQLException {
        Consumer<String> hook = beforeStatement;
        if (hook != null) {
            hook.accept(sql);
        }
        try (Statement st = conn.createStatement()) {
            st.execute(sql);
        }
    }

    /** The tables of the indexes found corrupt, in the order found. */
    private List<String> corruptTables() {
        return SPECS.stream().filter(sp -> corrupt.containsKey(sp.index())).map(Spec::table).distinct().toList();
    }

    /** The connection is gone ({@code 08xxx}, or {@code 57P01}: the walk's termination ended the backend), as the PCI builder reads it. */
    static boolean connectionLost(String state) {
        return state != null && (state.startsWith("08") || "57P01".equals(state));
    }

    private static String oneLine(Exception e) {
        String m = e.getMessage();
        return m == null ? e.getClass().getSimpleName() : m.replaceAll("\\s+", " ");
    }
}
