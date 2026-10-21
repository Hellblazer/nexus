// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;

import java.io.IOException;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Consumer;

/**
 * Bytes written and DDL statements per test run for the engine suite (nexus-sz651): this fork's
 * containers' server logs, read from the file in each container ({@link PgContainerHelper#serverLog}),
 * never from Docker's log (its json-file store is on the host's disk, which the counter must not write to),
 * counted and written to {@code target/ddl-counts/*.json} when a container starts, when it stops and when the JVM exits. {@code scripts/ddl_ratchet.py} reads the files of
 * ONE run of ONE suite.
 *
 * <p>The target is bytes written (disk thrashing); the DDL count is the diagnostic that explains
 * them. Bytes are the WAL a cluster generated: the probe's {@code mark()} logs the WAL position
 * and the summed database size at the {@code start} (container init script), {@code ready}
 * (template migrated) and {@code end} (this class, before the log is read) marks, so a shared
 * cluster's migration is {@code ready - start} and everything the tests caused is {@code end -
 * ready}; a dedicated container's whole life is {@code end - start}. WAL is a RELATIVE, LOWER-BOUND
 * churn proxy, not what a disk-backed cluster would write: the containers run {@code fsync=off} and
 * {@code full_page_writes=off} and sit on tmpfs, and Python's substrate runs {@code wal_level=minimal},
 * so Python and Java WAL are not comparable. NOT MEASURED: the job workspace and checkout,
 * {@code target/}, {@code .venv}, jar builds, journald, anything before the tests start, and the
 * docker-run and e2e shell clusters. The labrun NVMe sampler is the source of truth for disk writes.
 *
 * <p>Buckets: {@code boot} (everything before the {@code start} mark: the container bootstrap and
 * the probe's own install, never counted), {@code shared_migration} (the per-fork template: the
 * Liquibase migration), {@code shared_tests} (everything after the template's {@code ready} mark:
 * the per-class {@code CREATE DATABASE ... TEMPLATE} clones and the DDL tests run, except the
 * databases below), {@code shared_empty} (everything logged against a database from
 * {@link SharedCluster#acquireEmptyDatabase}: the migration-process classes' own walks of the
 * changelog, which a virgin dedicated container used to carry; the probe is installed into each
 * such database because {@code template0} has none, see {@link #probeInstallBlock()}),
 * {@code dedicated} (every container from {@link PgContainerHelper#startDedicated()} or a bare
 * {@code newContainer()}: those tests migrate their own cluster). The counting rule is
 * {@link DdlLogParser}'s.
 *
 * <p>{@code per_class}: bytes and DDL attributed to the test class that took the database or
 * container. A dedicated container's bytes are its own end minus start. A shared database's bytes
 * are the cluster's WAL between its {@code CREATE DATABASE} and the end of its drop; classes run
 * one at a time inside a fork, so that interval is that class's (the clone's own copy included).
 * A shared clone's {@code CREATE DATABASE} runs in the control database and stays unattributed
 * for DDL.
 *
 * <p>Non-vacuity: {@code containers}, {@code containers_unread}, {@code log_errors},
 * {@code ready_mark_seen} and {@code wal.incomplete_containers} are in the file, so a run whose logs
 * or marks could not be read shows up as a problem, not as zero.
 */
final class DdlCounts {

    /**
     * {@code -Dnexus.ddl.count=false} turns the counter fully off (nexus-3cwsw), so a run can be compared with
     * one at the same sha. Off: {@link PgContainerHelper#newContainer} adds none of the counter's settings and
     * this class touches no container, connection or file. Default (unset or {@code true}): on. Anything else
     * fails loud, because a typo that left the counter on would make the comparison a comparison of nothing.
     */
    static final String COUNT_PROPERTY = "nexus.ddl.count";
    static final String DIR_PROPERTY = "nexus.ddl.dir";
    static final String SUITE_PROPERTY = "nexus.ddl.suite";
    static final String RUN_PROPERTY = "nexus.ddl.run";
    static final String DEFAULT_DIR = "target/ddl-counts";
    private static final int TOP_CLASSES = 40;

    // Not final only so DdlCountSwitchTest can give itself private state (isolateForTest).
    private static volatile Set<FailFastPostgreSQLContainer> LIVE = ConcurrentHashMap.newKeySet();
    private static volatile Totals GLOBAL = new Totals();
    private static final String SUITE = System.getProperty(SUITE_PROPERTY, "unset");
    private static final String RUN = sanitize(System.getProperty(RUN_PROPERTY, "none"));
    private static final String FILE_STEM = "ddl-" + sanitize(SUITE) + "-" + RUN + "-"
        + ProcessHandle.current().pid() + "-" + System.nanoTime();
    private static final CgroupIo CGROUP_START = enabled() ? CgroupIo.read() : null;

    static {
        if (enabled()) {
            Runtime.getRuntime().addShutdownHook(new Thread(DdlCounts::onJvmExit, "ddl-counts-exit"));
        }
    }

    private DdlCounts() {}

    /** Whether the counter is on for this JVM ({@link #COUNT_PROPERTY}); read on every call, so a test can flip it. */
    static boolean enabled() {
        return enabled(System.getProperty(COUNT_PROPERTY));
    }

    static boolean enabled(String value) {
        if (value == null || value.equals("true")) {
            return true;
        }
        if (value.equals("false")) {
            return false;
        }
        throw new IllegalArgumentException("-D" + COUNT_PROPERTY + " must be true or false, got '" + value + "'");
    }

    private static String sanitize(String s) {
        return s.replaceAll("[^A-Za-z0-9_.-]", "-");
    }

    /** The first test-side class on the stack, for attribution. */
    static String callerClass() {
        return StackWalker.getInstance().walk(frames -> frames
            .map(StackWalker.StackFrame::getClassName)
            .filter(c -> c.startsWith("dev.nexus.") && !isInfrastructure(c))
            .findFirst()
            .map(c -> c.substring(c.lastIndexOf('.') + 1))
            .orElse("unknown"));
    }

    private static boolean isInfrastructure(String className) {
        for (Class<?> k : List.of(DdlCounts.class, PgContainerHelper.class, SharedCluster.class,
            SharedDatabaseHandle.class, FailFastPostgreSQLContainer.class)) {
            // Exact or nested: a test class named SharedClusterFooTest is not SharedCluster.
            if (className.equals(k.getName()) || className.startsWith(k.getName() + "$")) {
                return true;
            }
        }
        return false;
    }

    /**
     * Log a mark through the probe's {@code mark()} function (a typed jOOQ call, not raw SQL:
     * RawSqlGateTest counts raw call sites per file). Best effort: a probe that is missing or a
     * connection that fails leaves the run without that mark, which the ratchet refuses as an
     * unmeasured run; it must never fail the bootstrap or a test's teardown.
     */
    static boolean mark(Connection connection, String label) {
        if (!enabled()) {
            return false;
        }
        try {
            DSL.using(connection, SQLDialect.POSTGRES).select(
                DSL.function(DSL.name("nx_ddl_probe", "mark"), Object.class, DSL.val(label))).fetch();
            return true;
        } catch (RuntimeException e) {
            System.err.println("nexus-sz651: the DDL '" + label + "' mark failed: " + e);
            return false;
        }
    }

    /** The {@code ready} mark: DDL logged before it is the template's boot and migration. */
    static boolean markReady(Connection template) {
        return mark(template, DdlLogParser.READY_LABEL);
    }

    /**
     * Log the {@code end} mark on a live container, on its own short-lived connection. Reached through
     * {@link #record}, which is off with the counter; {@link #mark} is off as well.
     */
    static boolean markEnd(FailFastPostgreSQLContainer c) {
        Properties props = new Properties();
        props.setProperty("user", c.getUsername());
        props.setProperty("password", c.getPassword());
        props.setProperty("connectTimeout", "5");
        props.setProperty("loginTimeout", "5");
        props.setProperty("socketTimeout", "20");
        try (Connection conn = DriverManager.getConnection(c.getJdbcUrl(), props)) {
            return mark(conn, DdlLogParser.END_LABEL);
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * The cluster's WAL INSERT position as bytes since LSN 0/0 ({@code pg_wal_lsn_diff}, a typed jOOQ
     * call), or -1 when it cannot be read. Two reads bracket a class's use of a shared cluster.
     * Insert, not {@code pg_current_wal_lsn()} (the write pointer): under {@code synchronous_commit=off}
     * the write pointer lags the insert pointer by megabytes, so a read at it would leave recent WAL out
     * of the interval. The probe's marks read the same pointer.
     */
    static long walBytes(Connection connection) {
        if (!enabled()) {
            return -1;
        }
        try {
            BigDecimal v = DSL.using(connection, SQLDialect.POSTGRES).select(
                DSL.function("pg_wal_lsn_diff", BigDecimal.class,
                    DSL.function("pg_current_wal_insert_lsn", Object.class), DSL.inline("0/0"))).fetchOne(0, BigDecimal.class);
            return v == null ? -1 : v.longValue();
        } catch (RuntimeException e) {
            return -1;
        }
    }

    /**
     * The probe's install SQL for a database made from {@code template0} (which has no event
     * trigger), as one {@code DO} block: the part of {@code ddl/nx-ddl-probe.sql} after its
     * {@code \connect :nx_db} line and before its {@code -- nx-ddl-probe-end} line, run through
     * {@code EXECUTE}. A {@code DO} is not logged by {@code log_statement=ddl} and what it runs
     * through SPI is nested, so the install adds nothing to the count; the event trigger is
     * created last and does not fire for its own creation. One source for the probe SQL: the
     * container entrypoint runs the same file.
     */
    static String probeInstallBlock() {
        try (java.io.InputStream in = DdlCounts.class.getResourceAsStream("/ddl/nx-ddl-probe.sql")) {
            if (in == null) {
                throw new IllegalStateException("nexus-sz651: resource ddl/nx-ddl-probe.sql is missing");
            }
            String all = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            String marker = "\\connect :nx_db\n";
            int at = all.indexOf(marker);
            if (at < 0) {
                throw new IllegalStateException("nexus-sz651: ddl/nx-ddl-probe.sql has no connect-nx_db line");
            }
            String body = all.substring(at + marker.length());
            int stop = body.indexOf("-- nx-ddl-probe-end");
            if (stop < 0) {
                throw new IllegalStateException("nexus-sz651: ddl/nx-ddl-probe.sql has no probe-end line");
            }
            body = body.substring(0, stop);
            if (body.contains("$nxq$") || body.contains("$nxdo$")) {
                throw new IllegalStateException("nexus-sz651: probe SQL collides with the install block's quote tags");
            }
            return "DO $nxdo$ BEGIN EXECUTE $nxq$" + body + "$nxq$; END $nxdo$";
        } catch (java.io.IOException e) {
            throw new java.io.UncheckedIOException(e);
        }
    }

    static void register(FailFastPostgreSQLContainer c) {
        if (!enabled()) {
            return;
        }
        LIVE.add(c);
        // A fork killed before it stops anything still leaves a file saying a container was unread.
        flush();
    }

    /** A shared-cluster database handed to {@code owner}'s class; {@code startLsn} is -1 when unread. */
    static void noteDatabase(String dbName, String owner, boolean empty, long startLsn) {
        if (!enabled()) {
            return;
        }
        GLOBAL.noteDatabase(dbName, owner, empty, startLsn);
    }

    /** The shared database {@code dbName} is dropped; {@code endLsn} is -1 when unread. */
    static void noteRelease(String dbName, long endLsn) {
        if (!enabled()) {
            return;
        }
        GLOBAL.noteRelease(dbName, endLsn);
    }

    /** The shared database {@code dbName} could not be dropped and leaks for the rest of the fork: it stays
     *  counted as unreleased, and its class's open span closes uncredited so the class's later databases
     *  are still credited (nexus-7he0t). */
    static void noteLeak(String dbName) {
        if (!enabled()) {
            return;
        }
        GLOBAL.noteLeak(dbName);
    }

    /** Wait briefly for the {@code end} mark to reach the container log (the log stream lags a moment). */
    private static String logsWithEndMark(FailFastPostgreSQLContainer c, boolean marked) {
        String log = PgContainerHelper.serverLog(c);
        long deadline = System.nanoTime() + 3_000_000_000L;
        while (marked && !log.contains(DdlLogParser.MARK_PREFIX + DdlLogParser.END_LABEL) && System.nanoTime() < deadline) {
            try {
                Thread.sleep(100);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
            log = PgContainerHelper.serverLog(c);
        }
        return log;
    }

    /**
     * Mark the end, read this container's log once and fold it in; a second call for the same
     * container is a no-op. Never throws: a container that is already gone is a counted
     * {@code log_errors}, so {@code stop()} can always go on to stop it.
     */
    static void record(FailFastPostgreSQLContainer c) {
        if (!enabled() || !claimLive(c)) {
            return;
        }
        try {
            boolean marked = markEnd(c);
            GLOBAL.fold(logsWithEndMark(c, marked), c.ddlShared(), c.ddlOwner());
        } catch (RuntimeException e) {
            GLOBAL.logError();
        }
        flush();
    }

    /**
     * Take {@code c} out of the live set; false when it was not there (already recorded). {@link #record} claims
     * a container BEFORE it reads the log, so a read that never returns has already left the set: the exit hook
     * counts it as a log error and only the containers it never reached stay unread. A test's recorder calls
     * this to model that order.
     */
    static boolean claimLive(FailFastPostgreSQLContainer c) {
        return LIVE.remove(c);
    }

    /**
     * Test hook (DdlCountSwitchTest): swap in empty live/global state, so a test can drive the notes and
     * records on and off without touching the fork's real counts; closing puts the fork's back.
     */
    static AutoCloseable isolateForTest() {
        Set<FailFastPostgreSQLContainer> savedLive = LIVE;
        Totals savedGlobal = GLOBAL;
        LIVE = ConcurrentHashMap.newKeySet();
        GLOBAL = new Totals();
        return () -> {
            LIVE = savedLive;
            GLOBAL = savedGlobal;
        };
    }

    /** Test hook: the current totals plus the live-container count, as the count file would carry them. */
    static Map<String, Object> stateForTest() {
        return GLOBAL.snapshot(LIVE.size());
    }

    /** One fork's running totals. An instance, so a unit test folds into its own, not the fork's. */
    static final class Totals {
        private final Map<String, List<DdlLogParser.Entry>> byBucket = new LinkedHashMap<>();
        private final Map<String, String> dbOwner = new HashMap<>();
        private final Map<String, Long> dbStartLsn = new HashMap<>();
        /** Per class, its databases outstanding now and the LSN its current span opened at: WAL is
         *  cluster-wide, so a class holding two databases at once is credited one merged span, not
         *  one per database (nexus-7he0t). One SharedCluster per fork, so one LSN space. */
        private final Map<String, long[]> openSpan = new HashMap<>();
        private final Set<String> leaked = new java.util.HashSet<>();
        private final Set<String> emptyDbs = new java.util.HashSet<>();
        // {databases, containers, ddl, wal_bytes}
        private final Map<String, long[]> perClass = new TreeMap<>();
        private final Map<String, Map<String, Long>> tags = new LinkedHashMap<>();
        private int sharedContainers;
        private int dedicatedContainers;
        private int logErrors;
        private int incompleteWal;
        private boolean readyMarkSeen;
        private long sharedMigrationBytes;
        private long sharedTestsBytes;
        private long dedicatedBytes;
        private long dbsizeGrowthBytes;
        /** A container whose database size was unreadable voids the total, as the Python counter's _sum_known does (nexus-mvr3t). */
        private boolean dbsizeGrowthUnknown;

        synchronized void noteDatabase(String dbName, String owner, boolean empty, long startLsn) {
            dbOwner.put(dbName, owner);
            if (startLsn >= 0) {
                dbStartLsn.put(dbName, startLsn);
                long[] span = openSpan.computeIfAbsent(owner, k -> new long[2]);
                span[1] = span[0] == 0 ? startLsn : Math.min(span[1], startLsn);
                span[0]++;
            }
            if (empty) {
                emptyDbs.add(dbName);
            }
            perClass.computeIfAbsent(owner, k -> new long[4])[0]++;
        }

        synchronized void noteRelease(String dbName, long endLsn) {
            Long start = dbStartLsn.remove(dbName);
            String owner = dbOwner.get(dbName);
            long[] span = owner == null ? null : openSpan.get(owner);
            if (start == null || span == null || span[0] == 0) {
                return;
            }
            if (--span[0] == 0 && endLsn >= span[1]) {
                perClass.computeIfAbsent(owner, k -> new long[4])[3] += endLsn - span[1];
            }
        }

        synchronized void noteLeak(String dbName) {
            String owner = dbOwner.get(dbName);
            long[] span = owner == null ? null : openSpan.get(owner);
            if (dbStartLsn.containsKey(dbName) && leaked.add(dbName) && span != null && span[0] > 0) {
                span[0]--;
            }
        }

        synchronized void logError() {
            logErrors++;
        }

        /** Fold one container's log into the totals. */
        synchronized void fold(String log, boolean shared, String owner) {
            DdlLogParser.Parsed parsed = DdlLogParser.parse(log);
            Map<String, Long> wal = DdlLogParser.walSplit(parsed.stats());
            if (shared) {
                sharedContainers++;
                readyMarkSeen = readyMarkSeen || parsed.marks().contains(DdlLogParser.READY_LABEL);
                Long migration = wal.get("migration_bytes");
                Long tests = wal.get("tests_bytes");
                if (migration == null || tests == null) {
                    incompleteWal++;
                } else {
                    sharedMigrationBytes += migration;
                    sharedTestsBytes += tests;
                }
                Long growth = wal.get("dbsize_growth_bytes");
                if (growth == null) {
                    dbsizeGrowthUnknown = true;
                } else {
                    dbsizeGrowthBytes += growth;
                }
            } else {
                dedicatedContainers++;
                long[] row = perClass.computeIfAbsent(owner, k -> new long[4]);
                row[1]++;
                Long total = wal.get("total_bytes");
                if (total == null) {
                    incompleteWal++;
                } else {
                    dedicatedBytes += total;
                    row[3] += total;
                }
            }
            for (DdlLogParser.Entry e : parsed.entries()) {
                String bucket = e.boot() ? "boot"
                    : !shared ? "dedicated"
                    : e.beforeMark() ? "shared_migration"
                    : emptyDbs.contains(e.db()) ? "shared_empty" : "shared_tests";
                byBucket.computeIfAbsent(bucket, k -> new ArrayList<>()).add(e);
                tags.computeIfAbsent(bucket, k -> new HashMap<>()).merge(e.tag(), 1L, Long::sum);
                if (e.boot()) {
                    continue; // the probe's own install is nobody's DDL
                }
                String cls = !shared ? owner : dbOwner.get(e.db());
                if (cls != null) {
                    perClass.computeIfAbsent(cls, k -> new long[4])[2]++;
                }
            }
        }

        synchronized Map<String, Object> snapshot() {
            return snapshot(0);
        }

        /** {@code unread}: containers started and not yet read (a fork killed now leaves this many uncounted). */
        synchronized Map<String, Object> snapshot(int unread) {
            Map<String, Object> buckets = new LinkedHashMap<>();
            for (String b : List.of("boot", "shared_migration", "shared_tests", "shared_empty", "dedicated")) {
                buckets.put(b, DdlLogParser.tally(byBucket.getOrDefault(b, List.of())));
            }
            Map<String, Object> tagOut = new LinkedHashMap<>();
            tags.forEach((bucket, counts) -> {
                Map<String, Long> top = new LinkedHashMap<>();
                counts.entrySet().stream()
                    .sorted(Map.Entry.<String, Long>comparingByValue(Comparator.reverseOrder()))
                    .limit(20).forEach(en -> top.put(en.getKey(), en.getValue()));
                tagOut.put(bucket, top);
            });
            Map<String, Object> perClassOut = new LinkedHashMap<>();
            perClass.entrySet().stream()
                .sorted((a, b) -> a.getValue()[3] != b.getValue()[3]
                    ? Long.compare(b.getValue()[3], a.getValue()[3])
                    : Long.compare(b.getValue()[2], a.getValue()[2]))
                .limit(TOP_CLASSES)
                .forEach(en -> perClassOut.put(en.getKey(), Map.of(
                    "databases", en.getValue()[0], "containers", en.getValue()[1],
                    "ddl", en.getValue()[2], "wal_bytes", en.getValue()[3])));
            Map<String, Object> wal = new LinkedHashMap<>();
            wal.put("shared_migration_bytes", sharedMigrationBytes);
            wal.put("shared_tests_bytes", sharedTestsBytes);
            wal.put("dedicated_bytes", dedicatedBytes);
            // An unread container's growth is unknown too, and so is one whose log read failed (counted in
            // log_errors, never folded), so either voids the total as an unreadable size does (nexus-uy5du).
            wal.put("dbsize_growth_bytes", dbsizeGrowthUnknown || unread > 0 || logErrors > 0 ? null : dbsizeGrowthBytes);
            wal.put("incomplete_containers", incompleteWal);
            wal.put("unreleased_databases", dbStartLsn.size());
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("schema", 2);
            out.put("source", "java-fork");
            out.put("pid", ProcessHandle.current().pid());
            out.put("suite", SUITE);
            out.put("run", RUN);
            out.put("containers", Map.of("shared", sharedContainers, "dedicated", dedicatedContainers));
            out.put("containers_unread", unread);
            out.put("log_errors", logErrors);
            out.put("ready_mark_seen", readyMarkSeen);
            out.put("buckets", buckets);
            out.put("wal", wal);
            out.put("tags", tagOut);
            out.put("per_class", perClassOut);
            out.put("classes_seen", perClass.size());
            return out;
        }
    }

    /** The calling process's cgroup (v2) {@code io.stat}, {@code wbytes} summed over its devices. */
    record CgroupIo(String cgroup, long wbytes) {
        static CgroupIo read() {
            try {
                String path = null;
                for (String line : Files.readAllLines(Path.of("/proc/self/cgroup"))) {
                    if (line.startsWith("0::")) {
                        path = line.substring(3).replaceFirst("^/", "");
                        break;
                    }
                }
                if (path == null) {
                    return null;
                }
                long total = 0;
                for (String row : Files.readAllLines(Path.of("/sys/fs/cgroup", path, "io.stat"))) {
                    for (String field : row.split(" ")) {
                        if (field.startsWith("wbytes=")) {
                            total += Long.parseLong(field.substring("wbytes=".length()));
                        }
                    }
                }
                return new CgroupIo(path, total);
            } catch (IOException | RuntimeException e) {
                return null;
            }
        }
    }

    private static Map<String, Object> cgroupDelta() {
        CgroupIo now = CgroupIo.read();
        if (CGROUP_START == null || now == null || !CGROUP_START.cgroup().equals(now.cgroup())) {
            return null;
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("cgroup", now.cgroup());
        out.put("wbytes_since_fork_start", now.wbytes() - CGROUP_START.wbytes());
        out.put("note", "shared by every fork of the run: overlapping, never summed");
        return out;
    }

    /** Rewrite this fork's file; cheap, so it runs after every container start and stop. */
    static synchronized void flush() {
        if (!enabled()) {
            return;
        }
        Path dir = Path.of(System.getProperty(DIR_PROPERTY, DEFAULT_DIR));
        try {
            Files.createDirectories(dir);
            Map<String, Object> snapshot = GLOBAL.snapshot(LIVE.size());
            snapshot.put("cgroup_io", cgroupDelta());
            new ObjectMapper().enable(SerializationFeature.INDENT_OUTPUT)
                .writeValue(dir.resolve(FILE_STEM + ".json").toFile(), snapshot);
        } catch (IOException e) {
            System.err.println("nexus-sz651: could not write the DDL count file: " + e);
        }
    }

    /**
     * All the JVM-exit hook may spend reading the logs of containers still live; see {@link #onJvmExit(Duration, Consumer)}.
     * Surefire 3.5.2 halts a fork {@code forkedProcessExitTimeoutInSeconds} (default 30, not set in
     * service/pom.xml) after exit starts, and this hook runs inside that window. The budget therefore has to
     * finish well inside 30 s, or the {@code log_errors} mark of an abandoned read is never flushed and the
     * ratchet trusts numbers that miss a container. A read that is merely slow, 15 s or more, is cut the same
     * way and counted as a log error, so the ratchet refuses that run: conservative, by design.
     */
    static final Duration EXIT_BUDGET = Duration.ofSeconds(15);

    /** What the exit hook runs its budget and recorder through; a test swaps in one that records them (nexus-ccg9y). */
    @FunctionalInterface
    interface ExitRunner {
        void run(Duration budget, Consumer<FailFastPostgreSQLContainer> recorder);
    }

    /** The runner {@link #onJvmExit()} uses: production runs the budgeted record; only a test replaces it. */
    static volatile ExitRunner exitRunner = DdlCounts::onJvmExit;

    /** The shutdown hook's body: {@link #EXIT_BUDGET} and {@link #record}, handed to {@link #exitRunner}. */
    static void onJvmExit() {
        exitRunner.run(EXIT_BUDGET, DdlCounts::record);
    }

    /**
     * Record every container still live, within ONE overall {@code budget}. Each record can take up to three
     * 60 s tries when an exec wedges ({@link PgContainerHelper#serverLog}); done one after another on a fork
     * with several live containers that outlasts any shutdown. The records run on a daemon thread and this
     * waits for it only as long as the budget. On a timeout the containers it did not reach stay live (the
     * count file reports them as unread) and the one it was inside is counted as a {@code log_errors}, so the
     * ratchet refuses the run instead of trusting numbers that are missing a container.
     */
    static void onJvmExit(Duration budget, Consumer<FailFastPostgreSQLContainer> recorder) {
        Thread worker = new Thread(() -> {
            for (FailFastPostgreSQLContainer c : new ArrayList<>(LIVE)) {
                recorder.accept(c);
            }
        }, "ddl-counts-exit-record");
        worker.setDaemon(true);
        worker.start();
        try {
            worker.join(budget.toMillis());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        if (worker.isAlive()) {
            GLOBAL.logError();
            System.err.println("nexus-t4y7b: DDL counts: still reading container logs after " + budget
                + "; " + LIVE.size() + " container(s) not read, one read abandoned");
        }
        flush();
    }
}
