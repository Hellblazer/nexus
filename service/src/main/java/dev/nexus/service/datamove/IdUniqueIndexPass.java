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
 * RDR-229 P3.4 (nexus-p2m1x.22, linda's ruling 2026-11-22, from elsinore's P3.5c mapping and P3.4's arbiter census; linda's rulings 2026-11-22): the eight id-keyed UNIQUE indexes that the
 * id-keyed upserts of P3.5c take as their ON CONFLICT arbiters, and without which P3.7's one-row rename could make duplicate rows. They are
 * built at run time, after the engine serves, in the reconciler's pattern ({@code LeafIndexPass}'s, minus the partition route: none of the
 * eight tables is partitioned): one {@code CREATE UNIQUE INDEX CONCURRENTLY} at a time, on its own admin connection, under an advisory lock
 * of its own ({@link #LOCK_KEY}), with an INVALID leftover of the pass's name dropped before the build. No changeset carries them, so the
 * migration walk's DDL count does not.
 *
 * <p><b>Gate 2.</b> The pass starts only when gate 2 of the completion check holds (every stamp move, every P3.3a move, both P3.3c swaps and
 * their three validation moves complete): before it a row may still lack its id, and a chunk-family {@code on_complete} ends builds in
 * flight ({@code VerifyingFinisher}). Gate 3 requires {@link #allValid} (P3.4 is not complete while one of the eight is missing).
 *
 * <p><b>A duplicate is corruption, not data.</b> Before P3.7 each collection name maps to exactly one id and each table's name-keyed unique
 * already forbids the duplicate, so a unique violation (23505) on one of these builds means the table is corrupt. The pass then drops the
 * INVALID leftover, logs the duplicate keys (at most {@link #DUPLICATES_LOGGED}), keeps a {@code boot_work} entry that says so in words,
 * and does not build that index again in this engine's life: nothing loops. Gate 3 stays false, so the rename keeps its fourteen-table name
 * path. Any other failure (a lock wait, a cancelled build) is retried at the next tick. The status text names the table, never a tenant or
 * a key value (the status is unauthenticated); the values are in the log.
 *
 * <p>Visible to the migration walk as the PCI builder is: its {@code application_name} is under the builders' prefix, so
 * {@code PciBuilders.terminate} ends it, and it reads the migration advisory lock before every statement.
 */
public final class IdUniqueIndexPass implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(IdUniqueIndexPass.class);

    public static final String APPLICATION_NAME = PciBuilderSession.APPLICATION_NAME_PREFIX + "id-unique-index-pass";
    public static final String BOOT_WORK_ID = "id-unique-index-pass";
    /** ASCII "iduqidxp". */
    public static final long LOCK_KEY = 0x696475716964787L;

    static final String WHAT = "building the unique id indexes the rename's upserts use";
    static final String UNIT = "indexes";
    static final String ON_STOP = "a build in flight keeps running in the database and finishes there; one that is ended is dropped and built again "
        + "by the next engine";
    static final Duration INITIAL_DELAY = Duration.ofSeconds(30);
    static final Duration PERIOD = Duration.ofSeconds(60);
    static final Duration BUILD_STATEMENT_TIMEOUT = Duration.ofHours(6);
    static final int DUPLICATES_LOGGED = 5;

    /** One index: its table, name, key columns and partial predicate ({@code null} for none), in the deparsed form pg_get_indexdef shows. */
    public record Spec(String table, String index, String columns, String predicate) {
        String definition() {
            return "CREATE UNIQUE INDEX CONCURRENTLY " + index + " ON nexus." + table + " (" + columns + ")"
                + (predicate == null ? "" : " WHERE " + predicate);
        }
    }

    /**
     * The eight. The topics and reembed_jobs predicates are exact; the topics one is exactly the upsert's ({@code ON CONFLICT (tenant_id, collection_id, label) WHERE parent_id IS NULL}),
     * or PostgreSQL cannot infer the index as its arbiter.
     */
    public static final List<Spec> SPECS = List.of(
        new Spec("document_aspects", "idx_doc_aspects_id_source_path_uq", "tenant_id, collection_id, source_path", null),
        new Spec("chash_remap", "idx_chash_remap_id_old_id_uq", "tenant_id, source_collection_id, old_id", null),
        new Spec("taxonomy_meta", "idx_taxonomy_meta_id_uq", "tenant_id, collection_id", null),
        new Spec("topics", "idx_topics_root_label_id_uq", "tenant_id, collection_id, label", "parent_id IS NULL"),
        // the two SQL arbiters P3.4's census found beside the mapping: stamp_chunks_on_manifest_delete/_update (vectors-069-12/-13) and
        // canary_query_ring_upsert (vectors-069) upsert ON CONFLICT on these tables' name keys
        new Spec("chunk_orphaned_at", "idx_chunk_orphaned_at_id_chash_uq", "tenant_id, collection_id, chash", null),
        new Spec("canary_query_ring", "idx_canary_query_ring_id_digest_uq", "tenant_id, collection_id, vector_digest", null),
        // target-less ON CONFLICT DO NOTHING (PipelineRepository, ReembedJobRepository) arbitrates on every unique index, so the twin protects
        // them with no code change once the row carries its id (P3.1's dual write); reembed_jobs keeps reembed_jobs_source_lock's predicate exactly
        new Spec("pdf_pipeline", "idx_pdf_pipeline_id_uq", "tenant_id, content_hash, collection_id, pdf_path", null),
        new Spec("reembed_jobs", "idx_reembed_jobs_source_id_lock", "tenant_id, source_collection_id",
            "state = ANY (ARRAY['planned'::text, 'running'::text, 'paused'::text, 'cutover'::text, 'failed'::text])"));

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
    /** Tables whose build hit a duplicate, with the status text: not built again in this engine's life. Guarded by {@link #passLock}. */
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

    /** True when all eight exist and are VALID with their definitions: what gate 3 requires. A failed read is false. */
    public static boolean allValid(DSLContext ctx) {
        for (Spec s : SPECS) {
            if (!valid(ctx, s)) {
                return false;
            }
        }
        return true;
    }

    /** The index of {@code s}'s name is VALID and is {@code s}'s definition (unique, its columns, its predicate). */
    static boolean valid(DSLContext ctx, Spec s) {
        var r = ctx.fetchOne("""
            SELECT i.indisvalid AS valid, pg_catalog.pg_get_indexdef(i.indexrelid) AS def
              FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'nexus' AND c.relname = ?""", s.index());
        if (r == null || !Boolean.TRUE.equals(r.get("valid", Boolean.class))) {
            return false;
        }
        String def = r.get("def", String.class);
        String want = "CREATE UNIQUE INDEX " + s.index() + " ON nexus." + s.table() + " USING btree (" + s.columns() + ")"
            + (s.predicate() == null ? "" : " WHERE (" + s.predicate() + ")");
        return want.equals(def);
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
                return new Result(true, 0, 0, List.copyOf(corrupt.keySet()));
            }
            boolean held;
            try {
                held = gate2.holds();
            } catch (RuntimeException e) {
                log.warn("event=id_unique_index_pass_gate_unread cause=\"{}\"", oneLine(e));
                held = false;
            }
            if (!held) {
                return new Result(false, 0, 0, List.copyOf(corrupt.keySet()));
            }
            try (Connection conn = open()) {
                if (conn == null) {
                    return new Result(false, 0, 0, List.copyOf(corrupt.keySet()));
                }
                return run(conn);
            } catch (SQLException | DataAccessException e) {
                log.warn("event=id_unique_index_pass_aborted cause=\"{}\"", oneLine(e));
                return new Result(false, 0, 0, List.copyOf(corrupt.keySet()));
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
        conn.setAutoCommit(true);
        var ctx = DSL.using(conn, org.jooq.SQLDialect.POSTGRES);
        if (!Boolean.TRUE.equals(ctx.select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN, DSL.val(LOCK_KEY))).fetchOne(0, Boolean.class))) {
            conn.close();
            return null;
        }
        return conn;
    }

    private Result run(Connection conn) throws SQLException {
        var ctx = DSL.using(conn, org.jooq.SQLDialect.POSTGRES);
        List<Spec> todo = new ArrayList<>();
        for (Spec s : SPECS) {
            if (!corrupt.containsKey(s.table()) && !valid(ctx, s)) {
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
                    if ("23505".equals(e.getSQLState())) {
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
            exec(conn, "RESET lock_timeout");
            exec(conn, "RESET statement_timeout");
            if (corrupt.isEmpty()) {
                registry.finish(BOOT_WORK_ID);
            } else {
                String text = String.join("; ", corrupt.values());
                registry.register(BOOT_WORK_ID, text, 0L, (long) corrupt.size(), UNIT, true, ON_STOP);
            }
        }
        finished = corrupt.isEmpty() && allValid(ctx);
        return new Result(true, built, dropped, List.copyOf(corrupt.keySet()));
    }

    /** A 23505: drop the INVALID leftover, log the duplicate keys, record the table as corrupt. Never retried in this engine's life. */
    private void duplicate(Connection conn, DSLContext ctx, Spec s) throws SQLException {
        dropLeftover(conn, ctx, s);
        List<String> keys = new ArrayList<>();
        try {
            String sql = "SELECT " + s.columns().replace(", ", "::text || '/' || ") + "::text AS k, count(*) AS n FROM nexus." + s.table()
                + (s.predicate() == null ? "" : " WHERE " + s.predicate()) + " GROUP BY " + s.columns()
                + " HAVING count(*) > 1 ORDER BY 1 LIMIT " + DUPLICATES_LOGGED;
            ctx.fetch(sql).forEach(r -> keys.add(r.get("k", String.class) + " x" + r.get("n", Long.class)));
        } catch (DataAccessException e) {
            keys.add("(could not read: " + oneLine(e) + ")");
        }
        corrupt.put(s.table(), corruptionText(s.table()));
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

    /** The connection is gone ({@code 08xxx}, or {@code 57P01}: the walk's termination ended the backend), as the PCI builder reads it. */
    static boolean connectionLost(String state) {
        return state != null && (state.startsWith("08") || "57P01".equals(state));
    }

    private static String oneLine(Exception e) {
        String m = e.getMessage();
        return m == null ? e.getClass().getSimpleName() : m.replaceAll("\\s+", " ");
    }
}
