// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;

/**
 * DDL statements per test run for the engine suite (nexus-sz651): this fork's containers'
 * logs, counted and written to {@code target/ddl-counts/*.json} when a container stops and
 * when the JVM exits. {@code scripts/ddl_ratchet.py} sums the files of a run.
 *
 * <p>Buckets: {@code shared_migration} (the per-fork template: container boot, role and
 * template creation, the Liquibase migration), {@code shared_tests} (everything after the
 * template's {@code ready} mark: the per-class {@code CREATE DATABASE ... TEMPLATE} clones
 * and the DDL tests run, except the databases below), {@code shared_empty} (everything logged
 * against a database from {@link SharedCluster#acquireEmptyDatabase}: the migration-process
 * classes' own walks of the changelog, which a virgin dedicated container used to carry; the
 * probe is installed into each such database because {@code template0} has none, see
 * {@link #probeInstallBlock()}), {@code dedicated} (every container from
 * {@link PgContainerHelper#startDedicated()} or a bare {@code newContainer()}: those tests
 * migrate their own cluster). The counting rule is {@link DdlLogParser}'s.
 *
 * <p>{@code per_class}: DDL attributed to the test class that took the database or
 * container (shared databases by name, dedicated containers by owner); a shared clone's own
 * {@code CREATE DATABASE} runs in the control database and stays unattributed.
 *
 * <p>Non-vacuity: {@code containers}, {@code log_errors} and {@code ready_mark_seen} are in
 * the file, so a run whose logs could not be read shows up as errors, not as zero DDL.
 */
final class DdlCounts {

    static final String DIR_PROPERTY = "nexus.ddl.dir";
    static final String DEFAULT_DIR = "target/ddl-counts";
    private static final int TOP_CLASSES = 40;

    private static final Set<FailFastPostgreSQLContainer> LIVE = ConcurrentHashMap.newKeySet();
    private static final Totals GLOBAL = new Totals();
    private static final String FILE_STEM = "ddl-" + ProcessHandle.current().pid() + "-" + System.nanoTime();

    static {
        Runtime.getRuntime().addShutdownHook(new Thread(DdlCounts::onJvmExit, "ddl-counts-exit"));
    }

    private DdlCounts() {}

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
     * Log the {@code ready} mark through the probe's {@code mark()} function (a typed jOOQ call, not
     * raw SQL: RawSqlGateTest counts raw call sites per file), so DDL logged before it is the
     * template's boot and migration.
     */
    static void markReady(java.sql.Connection template) {
        DSL.using(template, SQLDialect.POSTGRES).select(
            DSL.function(DSL.name("nx_ddl_probe", "mark"), Object.class, DSL.val(DdlLogParser.READY_LABEL))).fetch();
    }

    /**
     * The probe's install SQL for a database made from {@code template0} (which has no event
     * trigger), as one {@code DO} block: the part of {@code ddl/nx-ddl-probe.sql} after its
     * {@code \connect :nx_db} line, run through {@code EXECUTE}. A {@code DO} is not logged by
     * {@code log_statement=ddl} and what it runs through SPI is nested, so the install adds nothing
     * to the count; the event trigger is created last and does not fire for its own creation.
     * One source for the probe SQL: the container entrypoint runs the same file.
     */
    static String probeInstallBlock() {
        try (java.io.InputStream in = DdlCounts.class.getResourceAsStream("/ddl/nx-ddl-probe.sql")) {
            if (in == null) {
                throw new IllegalStateException("nexus-sz651: resource ddl/nx-ddl-probe.sql is missing");
            }
            String all = new String(in.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
            String marker = "\\connect :nx_db\n";
            int at = all.indexOf(marker);
            if (at < 0) {
                throw new IllegalStateException("nexus-sz651: ddl/nx-ddl-probe.sql has no connect-nx_db line");
            }
            String body = all.substring(at + marker.length());
            if (body.contains("$nxq$") || body.contains("$nxdo$")) {
                throw new IllegalStateException("nexus-sz651: probe SQL collides with the install block's quote tags");
            }
            return "DO $nxdo$ BEGIN EXECUTE $nxq$" + body + "$nxq$; END $nxdo$";
        } catch (java.io.IOException e) {
            throw new java.io.UncheckedIOException(e);
        }
    }

    static void register(FailFastPostgreSQLContainer c) {
        LIVE.add(c);
    }

    /** A shared-cluster database handed to {@code owner}'s class. */
    static void noteDatabase(String dbName, String owner, boolean empty) {
        GLOBAL.noteDatabase(dbName, owner, empty);
    }

    /** Read this container's log once and fold it in; a second call for the same container is a no-op. */
    static void record(FailFastPostgreSQLContainer c) {
        if (!LIVE.remove(c)) {
            return;
        }
        String log;
        try {
            log = c.getLogs();
        } catch (RuntimeException e) {
            GLOBAL.logError();
            flush();
            return;
        }
        GLOBAL.fold(log, c.ddlShared(), c.ddlOwner());
        flush();
    }

    /** One fork's running totals. An instance, so a unit test folds into its own, not the fork's. */
    static final class Totals {
        private final Map<String, List<DdlLogParser.Entry>> byBucket = new LinkedHashMap<>();
        private final Map<String, String> dbOwner = new HashMap<>();
        private final Set<String> emptyDbs = new java.util.HashSet<>();
        private final Map<String, long[]> perClass = new TreeMap<>(); // {databases, containers, ddl}
        private final Map<String, Map<String, Long>> tags = new LinkedHashMap<>();
        private int sharedContainers;
        private int dedicatedContainers;
        private int logErrors;
        private boolean readyMarkSeen;

        synchronized void noteDatabase(String dbName, String owner, boolean empty) {
            dbOwner.put(dbName, owner);
            if (empty) {
                emptyDbs.add(dbName);
            }
            perClass.computeIfAbsent(owner, k -> new long[3])[0]++;
        }

        synchronized void logError() {
            logErrors++;
        }

        /** Fold one container's log into the totals. */
        synchronized void fold(String log, boolean shared, String owner) {
            DdlLogParser.Parsed parsed = DdlLogParser.parse(log);
            if (shared) {
                sharedContainers++;
                readyMarkSeen = readyMarkSeen || parsed.marks().contains(DdlLogParser.READY_LABEL);
            } else {
                dedicatedContainers++;
                perClass.computeIfAbsent(owner, k -> new long[3])[1]++;
            }
            for (DdlLogParser.Entry e : parsed.entries()) {
                String bucket = !shared ? "dedicated"
                    : e.beforeMark() ? "shared_migration"
                    : emptyDbs.contains(e.db()) ? "shared_empty" : "shared_tests";
                byBucket.computeIfAbsent(bucket, k -> new ArrayList<>()).add(e);
                tags.computeIfAbsent(bucket, k -> new HashMap<>()).merge(e.tag(), 1L, Long::sum);
                String cls = !shared ? owner : dbOwner.get(e.db());
                if (cls != null) {
                    perClass.computeIfAbsent(cls, k -> new long[3])[2]++;
                }
            }
        }

        synchronized Map<String, Object> snapshot() {
            Map<String, Object> buckets = new LinkedHashMap<>();
            for (String b : List.of("shared_migration", "shared_tests", "shared_empty", "dedicated")) {
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
                .sorted((a, b) -> Long.compare(b.getValue()[2], a.getValue()[2]))
                .limit(TOP_CLASSES)
                .forEach(en -> perClassOut.put(en.getKey(), Map.of(
                    "databases", en.getValue()[0], "containers", en.getValue()[1], "ddl", en.getValue()[2])));
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("schema", 1);
            out.put("source", "java-fork");
            out.put("pid", ProcessHandle.current().pid());
            out.put("suite", System.getProperty("nexus.ddl.suite", ""));
            out.put("containers", Map.of("shared", sharedContainers, "dedicated", dedicatedContainers));
            out.put("log_errors", logErrors);
            out.put("ready_mark_seen", readyMarkSeen);
            out.put("buckets", buckets);
            out.put("tags", tagOut);
            out.put("per_class", perClassOut);
            out.put("classes_seen", perClass.size());
            return out;
        }
    }

    /** Rewrite this fork's file; cheap, so it runs after every container stop. */
    static void flush() {
        Path dir = Path.of(System.getProperty(DIR_PROPERTY, DEFAULT_DIR));
        try {
            Files.createDirectories(dir);
            new ObjectMapper().enable(SerializationFeature.INDENT_OUTPUT)
                .writeValue(dir.resolve(FILE_STEM + ".json").toFile(), GLOBAL.snapshot());
        } catch (IOException e) {
            System.err.println("nexus-sz651: could not write the DDL count file: " + e);
        }
    }

    private static void onJvmExit() {
        for (FailFastPostgreSQLContainer c : new ArrayList<>(LIVE)) {
            record(c);
        }
        flush();
    }
}
