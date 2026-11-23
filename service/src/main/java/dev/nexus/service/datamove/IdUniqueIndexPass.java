// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.vectors.PciBuilderSession;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
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
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
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
 * RDR-229 P3.4 (nexus-p2m1x.22; linda's rulings 2026-11-22, from elsinore's P3.5c mapping and P3.4's arbiter census): the id-keyed indexes P3.4
 * builds at run time. Eight are UNIQUE arbiters ({@link ArbiterTwins}), which the id-keyed upserts of P3.5c take as their ON CONFLICT arbiters
 * and without which P3.7's one-row rename could make duplicate rows. They are
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
 * INVALID leftover, logs the duplicate keys (at most {@link #DUPLICATES_LOGGED}) at ERROR, keeps a {@code boot_work} entry that says so in words
 * ({@link #HELD_BOOT_WORK_ID}: visible and safe to stop, but not work for {@code nx upgrade}), and does not build that index again in this engine's
 * life: nothing loops. Gate 3 stays false, so the rename keeps its fourteen-table name
 * path. Any other failure (a lock wait, a cancelled build) is retried at the next tick. The status text names the table, never a tenant or
 * a key value (the status is unauthenticated); the values are in the log.
 *
 * <p>Visible to the migration walk as the PCI builder is: its {@code application_name} is under the builders' prefix, so
 * {@code PciBuilders.terminate} ends it, and it reads the migration advisory lock before each index it builds.
 */
public final class IdUniqueIndexPass implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(IdUniqueIndexPass.class);

    public static final String APPLICATION_NAME = PciBuilderSession.APPLICATION_NAME_PREFIX + "id-unique-index-pass";
    /** The {@code boot_work} entry of builds in progress: that IS work, and {@code nx upgrade} waits for it. */
    public static final String BOOT_WORK_ID = "id-unique-index-pass";
    /**
     * The {@code boot_work} entry of a standing condition: a table found corrupt (a duplicate on the id key), or an index of a pass name that stands with another
     * definition. It stays visible ({@code nx doctor}, {@code gate3_why}, the log at ERROR) and is safe to stop, but it is NOT work: nothing is running, an
     * upgrade or restart changes nothing about it, and a permanent entry would otherwise defer {@code nx upgrade}'s bundle swap and the boot-write harness's idle
     * rule for good. {@code nexus.daemon.pg_bundle_refresh.engine_busy_reason} and {@code scripts/upgrade_boot_writes.py} skip this id (linda, 2026-11-23). A
     * twin that only waits for a changeset has no entry at all: {@link #waitingChangesets} names it in {@code gate3_why}.
     */
    public static final String HELD_BOOT_WORK_ID = "id-unique-index-held";
    /** ASCII "iduqidxp". */
    public static final long LOCK_KEY = 0x6964757169647870L;

    static final String WHAT = "building the unique id indexes the rename's upserts use";
    static final String UNIT = "indexes";
    static final String HELD_ON_STOP = "nothing is lost and nothing is running: this is a standing condition, which a restart does not clear; the corrupt rows "
        + "or the other index are the operator's to repair";
    static final String ON_STOP = "a build in flight keeps running in the database and finishes there; one that is ended is dropped and built again "
        + "by the next engine";
    static final Duration INITIAL_DELAY = Duration.ofSeconds(30);
    static final Duration PERIOD = Duration.ofSeconds(60);
    static final Duration BUILD_STATEMENT_TIMEOUT = Duration.ofHours(6);
    static final int DUPLICATES_LOGGED = 5;
    /** A finished pass looks again this often, so an index dropped or invalidated later is rebuilt (r1 minor). */
    static final Duration RECHECK = Duration.ofMinutes(10);
    /** The duplicate report's statement bound: it groups a whole table. */
    static final Duration REPORT_STATEMENT_TIMEOUT = Duration.ofSeconds(60);
    /** A timed-out or cancelled build waits this long, doubling, at most {@link #BACKOFF_MAX}, before it is tried again. */
    static final Duration BACKOFF_BASE = Duration.ofHours(1);
    static final Duration BACKOFF_MAX = Duration.ofHours(24);

    private static final String SCHEMA = "nexus";
    private static final String BUILD_TEMPLATE = "CREATE %sINDEX CONCURRENTLY %I ON %I.%I (%s)%s";
    private static final String DROP_TEMPLATE = "DROP INDEX CONCURRENTLY IF EXISTS %I.%I";

    private static final Table<?> INDEX = DSL.table(DSL.name("pg_catalog", "pg_index"));
    private static final Table<?> CLASS = DSL.table(DSL.name("pg_catalog", "pg_class"));
    private static final Table<?> NAMESPACE = DSL.table(DSL.name("pg_catalog", "pg_namespace"));
    private static final Table<?> ATTRIBUTE = DSL.table(DSL.name("pg_catalog", "pg_attribute"));

    private static Field<Object> col(String alias, String column) {
        return DSL.field(DSL.name(alias, column));
    }

    private static Field<String> text(String alias, String column) {
        return DSL.field(DSL.name(alias, column), String.class);
    }

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

        /** The arguments of {@link #BUILD_TEMPLATE}: the UNIQUE keyword, index, schema, table, column list and WHERE clause (constants of this class and {@link ArbiterTwins}). */
        String[] buildArguments() {
            return new String[] {unique ? "UNIQUE " : "", index, SCHEMA, table, columns, predicate == null ? "" : " WHERE " + predicate};
        }
    }

    /**
     * The arbiters ({@link ArbiterTwins#builtByThePass}), then the five supporting indexes. The topics and reembed_jobs predicates are exact; the topics one is exactly the upsert's ({@code ON CONFLICT (tenant_id, collection_id, label) WHERE parent_id IS NULL}),
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
        Spec.support("aspect_extraction_queue", "idx_aspect_queue_collection_id", "tenant_id, collection_id", true),
        // the id twin of idx_catalog_chunks_collection (tenant_id, collection) WHERE collection IS NOT NULL, for the R2 anti-join on the
        // id path (linda 2026-11-22); a full index, and gate 3 requires it
        Spec.support("catalog_document_chunks", "idx_catalog_chunks_collection_id", "tenant_id, collection_id", false))).toList();

    /** Gate 2 of the completion check; production wires the completion reporter's latest read. */
    @FunctionalInterface
    public interface Gate2 {
        boolean holds();
    }

    /** What the pass holds, as words (the {@link #HELD_BOOT_WORK_ID} text): read by the completion check for {@code gate3_why}. One pass runs per engine. */
    private static volatile List<String> heldReasons = List.of();

    /** The corruption and definition-mismatch conditions the running pass holds, in words that name a table or an index, never a tenant or a key value. */
    public static List<String> heldReasons() {
        return heldReasons;
    }

    /** Test seam: forget what a pass held, so one test's corruption is not another's gate3_why. */
    static void forgetHeld() {
        heldReasons = List.of();
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
    /** Indexes of a pass name that exist VALID with another definition: reported, never dropped or rebuilt by the pass. Guarded by {@link #passLock}. */
    private final Map<String, String> mismatched = new LinkedHashMap<>();
    /** Twins whose wait has been logged at INFO once. Guarded by {@link #passLock}. */
    private final java.util.Set<String> waitLogged = new java.util.HashSet<>();
    /** A build that timed out or was cancelled waits out its backoff, by index name. Guarded by {@link #passLock}. */
    private final Map<String, Backoff> backoffs = new java.util.HashMap<>();
    /** The build statement's bound; a test shortens it. */
    private volatile Duration buildTimeout = BUILD_STATEMENT_TIMEOUT;
    /** How long a finished pass waits before it looks again; a test shortens it. */
    private volatile Duration recheck = RECHECK;
    /** When the last pass found every index in place; the pass looks again after {@link #RECHECK}. Null when not finished. */
    private Instant finishedAt;
    private ScheduledExecutorService scheduler;

    /** One index's failed builds: how many in a row and when the next may start. */
    private static final class Backoff {
        int failures;
        Instant nextAttempt = Instant.MIN;
    }

    public IdUniqueIndexPass(AdminConnection admin, BootWorkRegistry registry, Gate2 gate2) {
        this.admin = Objects.requireNonNull(admin, "admin");
        this.registry = Objects.requireNonNull(registry, "registry");
        this.gate2 = Objects.requireNonNull(gate2, "gate2");
    }

    /** Test seam: sees each statement just before it is sent. */
    void beforeStatement(Consumer<String> hook) {
        this.beforeStatement = hook;
    }

    /** Test seam: how long a finished pass waits before it looks again. */
    void recheckEvery(Duration period) {
        this.recheck = Objects.requireNonNull(period);
    }

    /** Test seam: the build statement's bound. */
    void buildTimeout(Duration timeout) {
        this.buildTimeout = Objects.requireNonNull(timeout);
    }

    /** How long an index whose build failed {@code failures} times in a row waits: one hour, doubling, at most a day. */
    static Duration backoffAfter(int failures) {
        Duration d = BACKOFF_BASE;
        for (int i = 1; i < failures && d.compareTo(BACKOFF_MAX) < 0; i++) {
            d = d.multipliedBy(2);
        }
        return d.compareTo(BACKOFF_MAX) > 0 ? BACKOFF_MAX : d;
    }

    /** The status text for an index of the pass's name that stands with another definition. */
    static String mismatchText(Spec s) {
        return "an index named " + s.index() + " on " + s.table() + " exists with a different definition than P3.4's (" + s.columns()
            + (s.predicate() == null ? "" : " WHERE " + s.predicate()) + "); the pass neither drops nor rebuilds it, so gate 3 waits until an "
            + "operator drops it, after which the next pass builds the right one";
    }

    /** True while a changeset {@code s}'s twin waits for ({@link ArbiterTwins#WAITS_FOR_CHANGESETS}) is not recorded in databasechangelog. */
    static boolean waiting(DSLContext ctx, Spec s) {
        List<String> ids = ArbiterTwins.WAITS_FOR_CHANGESETS.getOrDefault(s.index(), List.of());
        if (ids.isEmpty()) {
            return false;
        }
        var dcl = DSL.table(DSL.name("public", "databasechangelog"));
        var id = DSL.field(DSL.name("id"), String.class);
        return ctx.fetchCount(DSL.selectDistinct(id).from(dcl).where(id.in(ids))) < ids.size();
    }

    /**
     * The changesets the pass's twins still wait for ({@link ArbiterTwins#WAITS_FOR_CHANGESETS}), for gate3_why and boot_work: the ids not yet
     * recorded in databasechangelog of every twin that is not yet VALID. Empty when nothing waits.
     */
    public static List<String> waitingChangesets(DSLContext ctx) {
        var dcl = DSL.table(DSL.name("public", "databasechangelog"));
        var id = DSL.field(DSL.name("id"), String.class);
        List<String> out = new ArrayList<>();
        for (Spec s : SPECS) {
            List<String> ids = ArbiterTwins.WAITS_FOR_CHANGESETS.getOrDefault(s.index(), List.of());
            if (ids.isEmpty() || valid(ctx, s)) {
                continue;
            }
            var recorded = ctx.selectDistinct(id).from(dcl).where(id.in(ids)).fetch(id);
            ids.stream().filter(i -> !recorded.contains(i) && !out.contains(i)).forEach(out::add);
        }
        return out;
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
        // catalog fields, not pg_get_indexdef's text, which schema-qualifies by the reader's search_path (review r1, Minor 7). The key
        // columns come from indkey (an int2vector, read as its text) and the table's attributes, joined: one row per attribute.
        Field<String> indkey = DSL.cast(col("i", "indkey"), SQLDataType.VARCHAR);
        Field<Integer> nkeyatts = DSL.field(DSL.name("i", "indnkeyatts"), Integer.class);
        Field<Boolean> isValid = DSL.field(DSL.name("i", "indisvalid"), Boolean.class);
        Field<Boolean> isUnique = DSL.field(DSL.name("i", "indisunique"), Boolean.class);
        Field<String> pred = DSL.function(DSL.name("pg_catalog", "pg_get_expr"), String.class, col("i", "indpred"), col("i", "indrelid"));
        Field<Short> attnum = DSL.field(DSL.name("a", "attnum"), Short.class);
        var rows = ctx.select(isValid, isUnique, text("t", "relname"), indkey, nkeyatts, pred, attnum, text("a", "attname"))
            .from(INDEX.as("i"))
            .join(CLASS.as("c")).on(col("c", "oid").eq(col("i", "indexrelid")))
            .join(CLASS.as("t")).on(col("t", "oid").eq(col("i", "indrelid")))
            .join(NAMESPACE.as("n")).on(col("n", "oid").eq(col("c", "relnamespace")))
            .join(ATTRIBUTE.as("a")).on(col("a", "attrelid").eq(col("i", "indrelid")))
            .where(text("n", "nspname").eq(SCHEMA)).and(text("c", "relname").eq(s.index()))
            .and(attnum.gt((short) 0))
            .fetch();
        if (rows.isEmpty()) {
            return false;
        }
        var first = rows.get(0);
        if (!Boolean.TRUE.equals(first.get(isValid)) || !Objects.equals(first.get(isUnique), s.unique()) || !s.table().equals(first.get(text("t", "relname")))) {
            return false;
        }
        Map<Short, String> names = new LinkedHashMap<>();
        rows.forEach(r -> names.put(r.get(attnum), r.get(text("a", "attname"))));
        String[] key = first.get(indkey).trim().split("\\s+");
        List<String> cols = new ArrayList<>();
        for (int k = 0; k < first.get(nkeyatts); k++) {
            cols.add(names.get(Short.valueOf(key[k])));
        }
        String want = s.predicate() == null ? null : "(" + s.predicate() + ")";
        return s.columns().equals(String.join(", ", cols)) && Objects.equals(want, first.get(pred));
    }

    /** The table exists with its id column (the first id-named column of the key). */
    static boolean applicable(DSLContext ctx, Spec s) {
        String idColumn = java.util.Arrays.stream(s.columns().split(",\\s*")).filter(c -> c.endsWith("collection_id")).findFirst().orElseThrow();
        return ctx.fetchExists(DSL.selectOne().from(ATTRIBUTE.as("a"))
            .join(CLASS.as("c")).on(col("c", "oid").eq(col("a", "attrelid")))
            .join(NAMESPACE.as("n")).on(col("n", "oid").eq(col("c", "relnamespace")))
            .where(text("n", "nspname").eq(SCHEMA)).and(text("c", "relname").eq(s.table()))
            .and(text("a", "attname").eq(idColumn))
            .and(DSL.field(DSL.name("a", "attisdropped"), Boolean.class).isFalse()));
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
        heldReasons = List.of();
        if (scheduler != null) {
            scheduler.shutdownNow();
            scheduler = null;
        }
    }

    /** One pass, now. A gate that does not hold ends it before any connection opens. */
    public Result runOnce() {
        synchronized (passLock) {
            if (finishedAt != null && Duration.between(finishedAt, Instant.now()).compareTo(recheck) < 0) {
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
        java.util.Set<String> builtNames = new java.util.HashSet<>();
        Instant now = Instant.now();
        for (Spec s : SPECS) {
            if (mismatched.containsKey(s.index())) {
                if (validExists(ctx, s) && !valid(ctx, s)) {
                    continue;   // the wrong index still stands: reported, left alone
                }
                mismatched.remove(s.index());   // dropped or replaced by the operator: judged afresh below, built if it is missing
                log.info("event=id_unique_index_mismatch_cleared index={}", s.index());
            }
            if (corrupt.containsKey(s.index()) || valid(ctx, s)) {
                continue;
            }
            if (waiting(ctx, s)) {
                List<String> ids = ArbiterTwins.WAITS_FOR_CHANGESETS.get(s.index());
                if (waitLogged.add(s.index())) {
                    log.info("event=id_unique_index_waiting index={} for_changesets={} reason=writer_replaced_by_a_changeset_not_yet_recorded",
                        s.index(), ids);
                }
                continue;   // its table's writer is replaced by a changeset not yet recorded: building now would race the old body
            }
            if (validExists(ctx, s)) {   // a VALID index of our name with another definition: a named failure, never dropped by us
                mismatched.put(s.index(), mismatchText(s));
                log.error("event=id_unique_index_definition_mismatch table={} index={} want=\"{}{}\"", s.table(), s.index(), s.columns(),
                    s.predicate() == null ? "" : " WHERE " + s.predicate());
                continue;
            }
            Backoff wait = backoffs.get(s.index());
            if (wait != null && now.isBefore(wait.nextAttempt)) {
                continue;
            }
            todo.add(s);
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
                try {   // a leftover drop that cannot get its lock skips this index only, not the rest of the tick (r2 M4)
                    if (dropLeftover(conn, ctx, s)) {
                        dropped++;
                    }
                } catch (SQLException | DataAccessException e) {
                    log.warn("event=id_unique_index_drop_failed table={} index={} cause=\"{}\" retry=next_tick", s.table(), s.index(), oneLine(e));
                    if (connectionLost(e instanceof SQLException q ? q.getSQLState() : ((DataAccessException) e).sqlState())) {
                        break;
                    }
                    continue;
                }
                try {
                    setSession(ctx, "lock_timeout", "0");
                    setSession(ctx, "statement_timeout", buildTimeout.toMillis() + "ms");
                    ddl(conn, ctx, BUILD_TEMPLATE, s.buildArguments());
                    built++;
                    builtNames.add(s.index());
                    backoffs.remove(s.index());
                    registry.progress(BOOT_WORK_ID, (long) built, (long) todo.size());
                    log.info("event=id_unique_index_built table={} index={}", s.table(), s.index());
                } catch (SQLException | DataAccessException e) {
                    String state = sqlState(e);
                    if (s.unique() && "23505".equals(state)) {
                        duplicate(conn, ctx, s);
                    } else if ("57014".equals(state)) {   // the build's statement bound or a cancel: back off, do not rebuild every tick (r1 minor)
                        Backoff b = backoffs.computeIfAbsent(s.index(), k -> new Backoff());
                        b.failures++;
                        b.nextAttempt = Instant.now().plus(backoffAfter(b.failures));
                        log.warn("event=id_unique_index_build_failed table={} index={} cause=\"{}\" failures={} next_attempt={}", s.table(),
                            s.index(), oneLine(e), b.failures, b.nextAttempt);
                    } else {
                        log.warn("event=id_unique_index_build_failed table={} cause=\"{}\" retry=next_tick", s.table(), oneLine(e));
                        if (connectionLost(state)) {
                            break;
                        }
                    }
                }
            }
        } finally {
            try {
                setSession(ctx, "lock_timeout", null);   // set_config with NULL resets to the default
                setSession(ctx, "statement_timeout", null);
            } catch (DataAccessException e) {   // a dead connection (the walk ended the backend): the session goes with it
                log.debug("event=id_unique_index_reset_skipped cause=\"{}\"", oneLine(e));
            }
            List<String> held = new ArrayList<>(corrupt.values());
            held.addAll(mismatched.values());
            heldReasons = List.copyOf(held);
            if (!held.isEmpty()) {
                // a standing condition, not work: its own entry id, which the idle rules skip (HELD_BOOT_WORK_ID)
                registry.register(HELD_BOOT_WORK_ID, String.join("; ", held), 0L, (long) held.size(), UNIT, true, HELD_ON_STOP);
            } else {
                registry.finish(HELD_BOOT_WORK_ID);
            }
            // The builds in progress are work until every one that can be built has been; a corrupt table is not left to build.
            boolean workLeft = !backoffs.isEmpty()
                || todo.stream().anyMatch(sp -> !builtNames.contains(sp.index()) && !corrupt.containsKey(sp.index()));
            if (!workLeft) {
                registry.finish(BOOT_WORK_ID);
            }   // otherwise the entry stays, with its count: work is left for a later tick
        }
        finishedAt = corrupt.isEmpty() && mismatched.isEmpty() && allValid(ctx) ? Instant.now() : null;
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
            List<Field<?>> columns = Arrays.stream(s.columns().split(",\\s*")).<Field<?>>map(c -> DSL.field(DSL.name(c))).toList();
            Field<?>[] parts = new Field<?>[columns.size() * 2 - 1];
            for (int k = 0; k < columns.size(); k++) {
                parts[2 * k] = DSL.cast(columns.get(k), SQLDataType.VARCHAR);
                if (k > 0) {
                    parts[2 * k - 1] = DSL.inline("/");
                }
            }
            Field<String> key = DSL.concat(parts);
            Field<Integer> n = DSL.count();
            // one short transaction bounds the report (it groups a whole table): PgSession.setLocal sets the statement bound with its network timeout
            ctx.transaction(cfg -> {
                DSLContext tx = DSL.using(cfg);
                dev.nexus.service.db.PgSession.setLocal(tx, "statement_timeout", Long.toString(REPORT_STATEMENT_TIMEOUT.toMillis()));
                tx.select(key, n).from(DSL.table(DSL.name(SCHEMA, s.table()))).where(filter(s))
                    .groupBy(columns).having(n.gt(1)).orderBy(key).limit(DUPLICATES_LOGGED)
                    .forEach(r -> keys.add(r.get(key) + " x" + r.get(n)));
            });
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
        setSession(ctx, "lock_timeout", "5s");
        if (!invalidExists(ctx, s)) {
            return false;
        }
        ddl(conn, ctx, DROP_TEMPLATE, SCHEMA, s.index());
        log.info("event=id_unique_index_dropped reason=invalid_leftover table={}", s.table());
        return true;
    }

    /** A VALID index of {@code s}'s name exists (whatever its definition). */
    private static boolean validExists(DSLContext ctx, Spec s) {
        return ctx.fetchExists(DSL.selectOne().from(INDEX.as("i"))
            .join(CLASS.as("c")).on(col("c", "oid").eq(col("i", "indexrelid")))
            .join(NAMESPACE.as("n")).on(col("n", "oid").eq(col("c", "relnamespace")))
            .where(text("n", "nspname").eq(SCHEMA)).and(text("c", "relname").eq(s.index()))
            .and(DSL.field(DSL.name("i", "indisvalid"), Boolean.class).isTrue()));
    }

    private static boolean invalidExists(DSLContext ctx, Spec s) {
        return ctx.fetchExists(DSL.selectOne().from(INDEX.as("i"))
            .join(CLASS.as("c")).on(col("c", "oid").eq(col("i", "indexrelid")))
            .join(NAMESPACE.as("n")).on(col("n", "oid").eq(col("c", "relnamespace")))
            .where(text("n", "nspname").eq(SCHEMA)).and(text("c", "relname").eq(s.index()))
            .and(DSL.field(DSL.name("i", "indisvalid"), Boolean.class).isFalse()));
    }

    /**
     * The predicate of {@code s} as a typed condition, for the duplicate report: only the forms {@link #SPECS} carries have one (a new
     * predicate fails here, in the pass's own tests, rather than reading a different set of rows).
     */
    static Condition filter(Spec s) {
        if (s.predicate() == null) {
            return DSL.noCondition();
        }
        if (s.predicate().equals(ArbiterTwins.TOPICS_ROOT)) {
            return DSL.field(DSL.name("parent_id")).isNull();
        }
        if (s.predicate().equals(ArbiterTwins.REEMBED_ACTIVE)) {
            return DSL.field(DSL.name("state"), String.class).in(ArbiterTwins.REEMBED_ACTIVE_STATES);
        }
        throw new IllegalStateException("no typed form for the predicate of " + s.index() + ": " + s.predicate());
    }

    /** Session settings go through set_config (no SET text); a null value resets the setting to its default. */
    private static void setSession(DSLContext ctx, String name, String value) {
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val(name), DSL.val(value, SQLDataType.VARCHAR), DSL.inline(false))).fetch();
    }

    /** Renders {@code template} with PostgreSQL's {@code format()} ({@code %I} and {@code %s}), shows the text to the test seam and sends it. */
    private void ddl(Connection conn, DSLContext ctx, String template, String... args) throws SQLException {
        Field<?>[] params = new Field<?>[args.length + 1];
        params[0] = DSL.val(template);
        for (int i = 0; i < args.length; i++) {
            params[i + 1] = DSL.val(args[i]);
        }
        String sql = ctx.select(DSL.function("format", SQLDataType.CLOB, params)).fetchOne(0, String.class);
        Consumer<String> hook = beforeStatement;
        if (hook != null) {
            hook.accept(sql);
        }
        runDdl(conn, sql);
    }

    // SANCTIONED RAW (RDR-229 P3.4, nexus-p2m1x.22): CREATE [UNIQUE] INDEX CONCURRENTLY and DROP INDEX CONCURRENTLY have no typed jOOQ
    // form, and a concurrent build cannot run in a function or a transaction block, which rules out a SECURITY DEFINER wrapper. The
    // text is rendered by PostgreSQL's format() with %I and %s (ddl) from a literal template, so no identifier is concatenated in
    // Java; the column list and predicate are constants of SPECS and ArbiterTwins. Plain JDBC Statement.execute, as LeafIndexPass's
    // and PciBuilderSession's. Single-homed: runDdl.
    private static void runDdl(Connection conn, String sql) throws SQLException {
        try (Statement st = conn.createStatement()) {
            st.execute(sql);
        }
    }

    private static String sqlState(Exception e) {
        return e instanceof SQLException sql ? sql.getSQLState() : e instanceof DataAccessException d ? d.sqlState() : null;
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
