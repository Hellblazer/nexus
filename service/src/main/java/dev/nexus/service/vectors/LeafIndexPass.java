// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.vectors.LeafIndexCatalog.Idx;
import dev.nexus.service.vectors.LeafIndexCatalog.Node;
import dev.nexus.service.vectors.LeafIndexCatalog.Rel;
import dev.nexus.service.vectors.LeafIndexCatalog.Tree;
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
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.Properties;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * RDR-229 P3.3b (nexus-p2m1x.20): the per-leaf unique-index pass that registers the swap moves (RDR item 6, "Index builds follow
 * the PCI reconciler's pattern"). In the reconciler's pattern (a background pass that builds with {@code CREATE ... INDEX
 * CONCURRENTLY}, one at a time, on its own admin connection, and drops an INVALID leftover before it rebuilds), but not on the PCI
 * reconciler: it takes no {@code PciSettings}, runs whether {@code NX_SEARCH_PCI} is on or off, sits outside
 * {@code NX_SEARCH_PCI_MAX_PER_LEAF} and holds a session advisory lock of its own ({@link #LOCK_KEY}), so a peer engine's pass
 * stands down (residual S-3, nexus-p2m1x.41).
 *
 * <p><b>Gate 1.</b> The pass starts only when {@link Gate1} holds: the boot-time completion check reports every stamp move and
 * every P3.3a move complete ({@code CompletionCheck.ReadSet.boot()} is the read set; P3.3a's seven {@code rdr229-fkvalidate-*}
 * labels are in it). Those moves' chunk-family on_complete ends any build in flight ({@code VerifyingFinisher}), which is why the pass
 * waits. On an install whose moves are blocked the stamp never completes, so the pass builds nothing and registers nothing (residual
 * S-5, nexus-p2m1x.43).
 *
 * <p><b>The route (S-3, T2 nexus/plan-rdr-229-execution; linda's seam ruling 2026-11-22).</b> {@code nexus.chunks} and
 * {@code nexus.taxonomy_centroids} are list-partitioned by model and each model partition by tenant, so a unique index is built in
 * levels. Per table: (1) every leaf gets a valid unique index on the id key, {@code CREATE UNIQUE INDEX CONCURRENTLY}, one at a
 * time (an INVALID leftover under the pass's name is dropped first, its {@code indisvalid} read again immediately before the drop);
 * (2) only once every leaf of the table has a valid index: {@code ALTER TABLE ONLY} the parent {@code ADD CONSTRAINT UNIQUE}, the
 * same on each model partition, then per leaf {@code ADD CONSTRAINT ... UNIQUE USING INDEX} and {@code ALTER INDEX <model key>
 * ATTACH PARTITION <leaf key>}, then per model partition {@code ALTER INDEX <parent key> ATTACH PARTITION <model key>}; (3) last,
 * {@code registerSwap}. Every step reads the catalog first and does only what is missing, so a pass that ended anywhere resumes,
 * and a pass over a finished table sends nothing. Swap registration waits for the whole chain to be in place and attached.
 *
 * <p><b>Registering.</b> One {@code INSERT ... WHERE NOT EXISTS} on the label, on the admin connection, with the text of
 * {@link Rdr229SwapMove#spec} (P3.3c's) and the run-time changeset_id {@link #CHANGESET_PREFIX}{@code <engine version>} (S-3 point
 * 3, rule 2: an engine pass whose changeset has no on_complete text). A row that exists, however it got there, is left alone.
 *
 * <p><b>Locks.</b> The ONLY constraints and the leaf constraints take short exclusive locks on a table that serves; each runs under a
 * {@link #DDL_LOCK_TIMEOUT} lock timeout and a failure leaves the rest for the next tick. The builds take {@code lock_timeout 0} (as
 * the PCI builder's do) and a six hour statement timeout: a build the timeout cancels is dropped and rebuilt by the next pass.
 *
 * <p><b>Reporting.</b> {@code boot_work} entry {@link #BOOT_WORK_ID}, present only while a pass has indexes to build, with
 * {@code done} of {@code total}; the unauthenticated status names no leaf, tenant or index.
 */
public final class LeafIndexPass implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(LeafIndexPass.class);

    /** The pass's own {@code boot_work} entry; not the reconciler's {@code pci-reconcile}. */
    public static final String BOOT_WORK_ID = "leaf-index-pass";

    /** The registering changeset_id of a swap move is this prefix and the engine version (S-3 point 3, rule 2). */
    public static final String CHANGESET_PREFIX = "engine:p33b-unique-index:";

    /** The session advisory lock ({@code "leafidxp"}): one pass at a time in a database; distinct from the PCI builder's and the migration's. */
    public static final long LOCK_KEY = 0x6c65616669647870L;

    static final String WHAT = "building unique id indexes";
    static final String UNIT = "indexes";
    static final String ON_STOP = "a build in flight keeps running in the database; if the engine's stop or a schema migration ends it, "
        + "the INVALID index is dropped and rebuilt from the start by the next pass";

    /** How long a constraint or attach statement waits for its lock; the next tick retries. */
    static final String DDL_LOCK_TIMEOUT = "700ms";
    static final String DDL_STATEMENT_TIMEOUT = "30s";
    static final Duration BUILD_STATEMENT_TIMEOUT = Duration.ofHours(6);
    static final Duration INITIAL_DELAY = Duration.ofSeconds(30);
    static final Duration PERIOD = Duration.ofSeconds(60);

    /** One id key: a table of {@code nexus} and the columns of its unique index, as {@code pg_get_indexdef} writes them. */
    record Key(String table, String columns) {
        Rel parent() {
            return new Rel("nexus", table);
        }
    }

    static final List<Key> KEYS = List.of(
        new Key("chunks", "tenant_id, collection_id, chash, embedding_model"),
        new Key("taxonomy_centroids", "tenant_id, collection_id, topic_id, embedding_model"));

    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final Field<String> LABEL = DSL.field(DSL.name("label"), String.class);

    private static final String BUILD_TEMPLATE = "CREATE UNIQUE INDEX CONCURRENTLY %I ON %I.%I (%s)";
    private static final String DROP_TEMPLATE = "DROP INDEX CONCURRENTLY IF EXISTS %I.%I";
    private static final String PARENT_CONSTRAINT_TEMPLATE = "ALTER TABLE ONLY %I.%I ADD CONSTRAINT %I UNIQUE (%s)";
    private static final String LEAF_CONSTRAINT_TEMPLATE = "ALTER TABLE %I.%I ADD CONSTRAINT %I UNIQUE USING INDEX %I";
    private static final String ATTACH_TEMPLATE = "ALTER INDEX %I.%I ATTACH PARTITION %I.%I";

    /** Gate 1 of the completion check, read on the admin connection; production wires the completion reporter's latest read. */
    @FunctionalInterface
    public interface Gate1 {
        boolean holds(DSLContext admin);
    }

    /**
     * What one pass did. {@code gateHeld} is false when gate 1 did not hold or a peer engine's pass holds the lock (and then the rest is
     * zero or empty); {@code built} counts indexes built, {@code dropped} INVALID leftovers dropped, {@code registered} the swap labels
     * this pass inserted.
     */
    public record Result(boolean gateHeld, int built, int dropped, List<String> registered) { }

    private final AdminConnection admin;
    private final BootWorkRegistry registry;
    private final Gate1 gate1;
    private final String engineVersion;
    /** Passes of one object never overlap; not {@code this}, so {@link #close} never waits for a build that runs for hours. */
    private final Object passLock = new Object();
    private ScheduledExecutorService scheduler;   // guarded by this

    public LeafIndexPass(AdminConnection admin, BootWorkRegistry registry, Gate1 gate1, String engineVersion) {
        this.admin = Objects.requireNonNull(admin, "admin");
        this.registry = Objects.requireNonNull(registry, "registry");
        this.gate1 = Objects.requireNonNull(gate1, "gate1");
        this.engineVersion = Objects.requireNonNull(engineVersion, "engineVersion");
    }

    /** The label of {@code table}'s swap move ({@code chunks} or {@code taxonomy_centroids}): P3.3c's move, registered by this pass. */
    public static String swapLabel(String table) {
        return "rdr229-swap-" + table;
    }

    /** The name of a leaf's id unique index (and of its constraint), so a leftover INVALID one is found by name and dropped. A leaf name is at most 35 characters. */
    public static String leafIndexName(String leaf) {
        return leaf + "_cid_uq";
    }

    /** The name of a table's, or a model partition's, id constraint. */
    static String constraintName(String relation) {
        return relation + "_cid_uq";
    }

    /** Starts the schedule: a tick every {@link #PERIOD}, the first after {@link #INITIAL_DELAY}. Never throws into the executor. */
    public synchronized void start() {
        if (scheduler != null) {
            return;
        }
        scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "leaf-index-pass");
            t.setDaemon(true);
            return t;
        });
        scheduler.scheduleWithFixedDelay(() -> {
            try {
                runOnce();
            } catch (VirtualMachineError e) {
                throw e;
            } catch (Throwable e) {
                log.warn("event=leaf_index_pass_failed cause=\"{}\"", e.toString());
            }
        }, INITIAL_DELAY.toSeconds(), PERIOD.toSeconds(), TimeUnit.SECONDS);
    }

    /** Stops the schedule. A build in flight is not ended (it keeps running in the database, as the PCI builder's does). */
    @Override
    public synchronized void close() {
        if (scheduler != null) {
            scheduler.shutdownNow();
            scheduler = null;
        }
    }

    /**
     * One pass, now: the scheduler's tick. Opens its own admin connection and takes {@link #LOCK_KEY}; a peer holding it, or a gate
     * that does not hold, ends the pass with nothing done.
     */
    public Result runOnce() {
        synchronized (passLock) {
            try (Session s = open()) {
                if (s == null) {
                    return new Result(false, 0, 0, List.of());
                }
                boolean held;
                try {
                    held = gate1.holds(s.ctx);
                } catch (DataAccessException e) {
                    log.warn("event=leaf_index_pass_gate_unread cause=\"{}\"", oneLine(e));
                    held = false;
                }
                if (!held) {
                    log.debug("event=leaf_index_pass_waiting reason=gate1_not_held");
                    return new Result(false, 0, 0, List.of());
                }
                return s.run();
            } catch (SQLException | DataAccessException e) {
                log.warn("event=leaf_index_pass_aborted cause=\"{}\"", oneLine(e));
                return new Result(false, 0, 0, List.of());
            }
        }
    }

    private Session open() throws SQLException {
        Properties props = new Properties();
        props.setProperty("user", admin.user());
        props.setProperty("password", admin.password());
        props.setProperty("ApplicationName", "nexus-leaf-index-pass");
        props.setProperty("connectTimeout", "10");
        props.setProperty("loginTimeout", "30");
        props.setProperty("socketTimeout", Long.toString(BUILD_STATEMENT_TIMEOUT.toSeconds() + 5 * 60));
        props.setProperty("tcpKeepAlive", "true");
        Connection conn = DriverManager.getConnection(admin.url(), props);
        try {
            conn.setAutoCommit(true);
            Session s = new Session(conn);
            if (!Boolean.TRUE.equals(s.ctx.select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN, DSL.val(LOCK_KEY)))
                    .fetchOne(0, Boolean.class))) {
                log.debug("event=leaf_index_pass_standby reason=peer_holds_lock key={}", LOCK_KEY);
                conn.close();
                return null;
            }
            return s;
        } catch (SQLException | RuntimeException e) {
            try {
                conn.close();
            } catch (SQLException suppressed) {
                e.addSuppressed(suppressed);
            }
            throw e;
        }
    }

    /** A leaf that needs its index built. */
    private record Build(Key key, Node leaf) { }

    /** One connection, and the work of one pass on it. */
    private final class Session implements AutoCloseable {
        final Connection conn;
        final DSLContext ctx;
        int built;
        int dropped;
        int done;
        final List<String> registered = new ArrayList<>();

        Session(Connection conn) {
            this.conn = conn;
            this.ctx = DSL.using(conn, org.jooq.SQLDialect.POSTGRES);
        }

        @Override
        public void close() throws SQLException {
            registry.finish(BOOT_WORK_ID);
            conn.close();   // ends the session, so the advisory lock goes with it
        }

        Result run() {
            // phase 1: drop leftovers and plan the builds of every table, so the entry's total is known before the first build
            List<Build> builds = new ArrayList<>();
            for (Key key : KEYS) {
                planTable(key, builds);
            }
            if (!builds.isEmpty()) {
                registry.register(BOOT_WORK_ID, WHAT, 0L, (long) builds.size(), UNIT, true, ON_STOP);
            }
            for (Build b : builds) {
                build(b);
                registry.progress(BOOT_WORK_ID, (long) ++done, (long) builds.size());
            }
            // phase 2: per table, the route and, last, the swap
            for (Key key : KEYS) {
                routeTable(key);
            }
            if (built + dropped + registered.size() > 0) {
                log.info("event=leaf_index_pass_ended built={} dropped={} registered={}", built, dropped, registered.size());
            }
            return new Result(true, built, dropped, List.copyOf(registered));
        }

        private void planTable(Key key, List<Build> builds) {
            Tree tree = LeafIndexCatalog.tree(ctx, key.parent());
            for (Node leaf : tree.leaves()) {
                dropLeftovers(leaf, key);
                if (LeafIndexCatalog.valid(LeafIndexCatalog.idIndexes(ctx, leaf.rel(), key.columns())) == null) {
                    builds.add(new Build(key, leaf));
                }
            }
        }

        /**
         * Drops the INVALID index this pass names for {@code leaf}: the leftover of a build that ended mid-way. The leftover is judged
         * on a read that may be a statement old, so its {@code indisvalid} is read again immediately before the DROP; a build still
         * running holds the pass's advisory lock on another session, so this one is never the pass that stands over it.
         */
        private void dropLeftovers(Node leaf, Key key) {
            String ours = leafIndexName(leaf.rel().name());
            boolean leftover = LeafIndexCatalog.idIndexes(ctx, leaf.rel(), key.columns()).stream()
                .anyMatch(i -> ours.equals(i.name()) && !i.valid());
            if (!leftover) {
                return;
            }
            try {
                setBounds("5s", "5min");
                ddl(DROP_TEMPLATE, leaf.rel().schema(), ours);
                dropped++;
                log.info("event=leaf_index_dropped reason=invalid_leftover table={}", key.table());
            } catch (SQLException | DataAccessException e) {
                log.warn("event=leaf_index_drop_failed table={} cause=\"{}\"", key.table(), oneLine(e));
            }
        }

        private void build(Build b) {
            Rel leaf = b.leaf().rel();
            try {
                setBounds("0", BUILD_STATEMENT_TIMEOUT.toMillis() + "ms");
                ddl(BUILD_TEMPLATE, leafIndexName(leaf.name()), leaf.schema(), leaf.name(), b.key().columns());
                built++;
            } catch (SQLException | DataAccessException e) {
                // a duplicate under the id key (23505) or a timeout leaves the index INVALID; the next pass drops and rebuilds it
                log.warn("event=leaf_index_build_failed table={} cause=\"{}\"", b.key().table(), oneLine(e));
            }
        }

        /** The route and the registration for one table, each step only if its state is missing; a failed step ends the table's turn. */
        private void routeTable(Key key) {
            try {
                Tree tree = LeafIndexCatalog.tree(ctx, key.parent());
                if (tree.leaves().isEmpty() || !everyLeafValid(key, tree)) {
                    return;   // the parent constraint waits for every leaf of the table to have a valid index
                }
                setBounds(DDL_LOCK_TIMEOUT, DDL_STATEMENT_TIMEOUT);
                Rel parent = key.parent();
                if (idConstraint(parent, key) == null) {
                    ddl(PARENT_CONSTRAINT_TEMPLATE, parent.schema(), parent.name(), constraintName(parent.name()), key.columns());
                }
                for (Node model : tree.models()) {
                    if (idConstraint(model.rel(), key) == null) {
                        ddl(PARENT_CONSTRAINT_TEMPLATE, model.rel().schema(), model.rel().name(),
                            constraintName(model.rel().name()), key.columns());
                    }
                }
                for (Node leaf : LeafIndexCatalog.tree(ctx, parent).leaves()) {   // read again: a leaf made meanwhile is in the walk
                    adoptLeaf(key, leaf);
                }
                for (Node model : tree.models()) {
                    attachUnder(key, model.rel(), parent);
                }
                if (chainComplete(key)) {
                    registerSwap(key.table());
                }
            } catch (SQLException | DataAccessException e) {
                log.warn("event=leaf_index_route_failed table={} cause=\"{}\"", key.table(), oneLine(e));
            }
        }

        private boolean everyLeafValid(Key key, Tree tree) {
            return tree.leaves().stream()
                .allMatch(l -> LeafIndexCatalog.valid(LeafIndexCatalog.idIndexes(ctx, l.rel(), key.columns())) != null);
        }

        /** The id index of {@code rel} that backs a constraint, or null. */
        private Idx idConstraint(Rel rel, Key key) {
            return LeafIndexCatalog.idIndexes(ctx, rel, key.columns()).stream().filter(Idx::constraint).findFirst().orElse(null);
        }

        /** A leaf's valid index becomes a constraint (ADD ... USING INDEX) and is attached to its model partition's index. */
        private void adoptLeaf(Key key, Node leaf) throws SQLException {
            Idx idx = LeafIndexCatalog.valid(LeafIndexCatalog.idIndexes(ctx, leaf.rel(), key.columns()));
            if (idx == null) {
                return;
            }
            if (!idx.constraint()) {
                ddl(LEAF_CONSTRAINT_TEMPLATE, leaf.rel().schema(), leaf.rel().name(), idx.name(), idx.name());
            }
            if (idx.attachedTo() == null) {
                attachUnder(key, leaf.rel(), leaf.parent());
            }
        }

        /** {@code ALTER INDEX <parent's id index> ATTACH PARTITION <child's id index>}, when the child's is not attached yet. */
        private void attachUnder(Key key, Rel child, Rel parent) throws SQLException {
            Idx childIdx = idConstraint(child, key);   // a model's ONLY index is INVALID until its leaves are attached
            Idx parentIdx = idConstraint(parent, key);
            if (childIdx == null || parentIdx == null || childIdx.attachedTo() != null) {
                return;
            }
            ddl(ATTACH_TEMPLATE, parent.schema(), parentIdx.name(), child.schema(), childIdx.name());
        }

        /** The parent has the constraint; every model partition and every leaf has a valid constraint-backed index attached to its parent's. */
        private boolean chainComplete(Key key) {
            Tree tree = LeafIndexCatalog.tree(ctx, key.parent());
            if (tree.leaves().isEmpty() || idConstraint(key.parent(), key) == null) {
                return false;
            }
            for (Node model : tree.models()) {
                Idx idx = idConstraint(model.rel(), key);
                if (idx == null || idx.attachedTo() == null) {
                    return false;
                }
            }
            for (Node leaf : tree.leaves()) {
                Idx idx = LeafIndexCatalog.valid(LeafIndexCatalog.idIndexes(ctx, leaf.rel(), key.columns()));
                if (idx == null || !idx.constraint() || idx.attachedTo() == null) {
                    return false;
                }
            }
            return true;
        }

        /**
         * Registers {@code table}'s swap move: one INSERT ... WHERE NOT EXISTS on the label, the last step of the table's route. A row
         * that exists is left alone, and a peer that wins the race (23505) leaves it registered once all the same.
         */
        private void registerSwap(String table) {
            String label = swapLabel(table);
            Rdr229SwapMove.Spec spec = Rdr229SwapMove.spec(table);
            try {
                int n = ctx.insertInto(MOVES, LABEL, DSL.field(DSL.name("template"), String.class),
                        DSL.field(DSL.name("driving_schema"), String.class), DSL.field(DSL.name("driving_table"), String.class),
                        DSL.field(DSL.name("chunk_size"), Integer.class), DSL.field(DSL.name("sleep_ms"), Integer.class),
                        DSL.field(DSL.name("statement_timeout_ms"), Integer.class),
                        DSL.field(DSL.name("verify_statement_timeout_ms"), Integer.class),
                        DSL.field(DSL.name("verification"), String.class), DSL.field(DSL.name("on_complete"), String.class),
                        DSL.field(DSL.name("changeset_id"), String.class))
                    .select(DSL.select(DSL.val(label), DSL.val(spec.template()), DSL.val("nexus"), DSL.val(table),
                            DSL.val(spec.chunkSize()), DSL.val(spec.sleepMs()), DSL.val(spec.statementTimeoutMs()),
                            DSL.val(spec.verifyStatementTimeoutMs()), DSL.val(spec.verification()),
                            DSL.val(spec.onComplete(), SQLDataType.VARCHAR), DSL.val(CHANGESET_PREFIX + engineVersion))
                        .whereNotExists(DSL.selectOne().from(MOVES).where(LABEL.eq(label))))
                    .execute();
                if (n > 0) {
                    registered.add(label);
                    log.info("event=leaf_index_swap_registered table={}", table);
                }
            } catch (DataAccessException e) {
                if (!"23505".equals(e.sqlState())) {
                    throw e;
                }
            }
        }

        private void setBounds(String lockTimeout, String statementTimeout) {
            setSession("lock_timeout", lockTimeout);
            setSession("statement_timeout", statementTimeout);
        }

        private void setSession(String name, String value) {
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val(name), DSL.val(value), DSL.inline(false))).fetch();
        }

        /** Renders {@code template} with PostgreSQL's {@code format()} (%I and %s) and sends the text on this connection. */
        private void ddl(String template, String... args) throws SQLException {
            Field<?>[] params = new Field<?>[args.length + 1];
            params[0] = DSL.val(template);
            for (int i = 0; i < args.length; i++) {
                params[i + 1] = DSL.val(args[i]);
            }
            String sql = ctx.select(DSL.function("format", SQLDataType.CLOB, params)).fetchOne(0, String.class);
            runDdl(sql);
        }

        // SANCTIONED RAW (RDR-229 P3.3b, nexus-p2m1x.20): CREATE UNIQUE INDEX CONCURRENTLY, DROP INDEX CONCURRENTLY, ALTER TABLE ...
        // ADD CONSTRAINT UNIQUE and ALTER INDEX ... ATTACH PARTITION have no typed jOOQ form (a concurrent build cannot run in a
        // function or a transaction block, which rules out a SECURITY DEFINER wrapper). The text is rendered by PostgreSQL's format()
        // with %I (ddl), from a literal template and catalog names, so no identifier is concatenated in Java; the column list is a
        // constant of KEYS. Plain JDBC Statement.execute, as PciBuilderSession's does. Single-homed: ddl.
        private void runDdl(String sql) throws SQLException {
            try (Statement st = conn.createStatement()) {
                st.execute(sql);
            }
        }
    }

    private static String oneLine(Exception e) {
        String m = e.getMessage();
        return (m == null ? e.toString() : m).replace('\n', ' ').replace('"', '\'');
    }
}
