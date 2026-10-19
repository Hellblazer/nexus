// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Every test class that boots a Postgres of its own must say why (nexus-0ty8z, step 1).
 *
 * <p>A migrated schema costs about 2,300 DDL statements to build and every runAlways grant replays on each walk;
 * a clone of the per-fork template costs a file copy. The suite's DDL is dominated by classes that walk the
 * changelog into an empty database or boot a container of their own, so each such class has to earn it. The
 * table {@code src/test/resources/db-start-classification.tsv} lists every class that calls
 * {@code PgContainerHelper.startEmptyDatabase}, {@code startDedicated} or {@code newContainer}, with the reason
 * and one kind from a closed vocabulary:
 * <ul>
 *   <li>{@code WALK}, {@code MIGRATOR}, {@code OWNERSHIP}: the class's subject is the migration (a part-way walk
 *       and the effect of a later changeset on seeded rows, the migrator's own contract, provisioning from
 *       scratch). It needs an unmigrated database.</li>
 *   <li>{@code SKELETON}: the class needs a database with no product schema at all, and walks nothing.</li>
 *   <li>{@code CLUSTER_STATE}, {@code SIZED}, {@code NETWORK}, {@code IMAGE}: the class needs a container of its
 *       own (role membership, the server log, a size, a network, an image).</li>
 *   <li>{@code INFRA}: the class tests the helpers that hand out databases.</li>
 *   <li>{@code CLONE}, {@code CLONE_OWNER}: not a caller. The class used to walk its own database and was moved
 *       onto a clone ({@code start()} or {@code startMigratedByOwner()}); the row keeps it there.</li>
 * </ul>
 * A class whose subject is not one of these has no row: it takes a clone. A new caller that is not listed fails
 * here, and so does a row whose class stopped calling. The scan reads the test tree, so it is not vacuous when
 * the table is empty: it fails below a floor on files read and on callers found. A call is a direct call
 * ({@code PgContainerHelper.startDedicated()}) or a method reference ({@code PgContainerHelper::startDedicated}).
 *
 * <p>What the gate does NOT check: a kind is a label checked only against the vocabulary allowed for its call,
 * and a reason is free text of at least 25 characters. Neither is checked against what the class does, so a row
 * can be mislabelled and the gate stays green; the table forces a decision and a place to review it, not
 * a proof that the decision is right.
 */
class DbStartClassificationGateTest {

    private static final Path TEST_ROOT = Path.of("src", "test", "java");
    private static final Path TABLE = Path.of("src", "test", "resources", "db-start-classification.tsv");

    /** The plumbing that defines the three entry points; it is not a caller. */
    private static final Set<String> INFRASTRUCTURE = Set.of(
        "PgContainerHelper.java", "SharedCluster.java", "SharedDatabaseHandle.java",
        "FailFastPostgreSQLContainer.java", "DbStartClassificationGateTest.java");

    private static final Map<String, Set<String>> KINDS_BY_CALL = Map.of(
        "startEmptyDatabase", Set.of("WALK", "MIGRATOR", "OWNERSHIP", "SKELETON", "INFRA"),
        "startDedicated", Set.of("CLUSTER_STATE", "SIZED", "INFRA"),
        "newContainer", Set.of("NETWORK", "IMAGE", "INFRA"),
        "clone", Set.of("CLONE", "CLONE_OWNER"));

    private static final Pattern CALL = Pattern.compile(
        "\\bPgContainerHelper\\s*\\.\\s*(startEmptyDatabase|startDedicated|newContainer)\\s*\\(");
    /** A method reference ({@code PgContainerHelper::startDedicated}) hands the entry point to a helper that boots it. */
    private static final Pattern METHOD_REF = Pattern.compile(
        "\\bPgContainerHelper\\s*::\\s*(startEmptyDatabase|startDedicated|newContainer)\\b");
    private static final Pattern STATIC_IMPORT = Pattern.compile(
        "import\\s+static\\s+[\\w.]*PgContainerHelper\\s*\\.\\s*(startEmptyDatabase|startDedicated|newContainer|\\*)");
    private static final Pattern TAKES_CLONE =
        Pattern.compile("\\bPgContainerHelper\\s*(?:\\.\\s*start\\s*\\(\\s*\\)|::\\s*start\\b)");
    private static final Pattern TAKES_OWNER_CLONE =
        Pattern.compile("\\bPgContainerHelper\\s*(?:\\.\\s*startMigratedByOwner\\s*\\(\\s*\\)|::\\s*startMigratedByOwner\\b)");

    /** Text blocks, string and char literals, line and block comments, dropped in one pass. The literal
     *  branches are unrolled loops with possessive runs, so the matcher recurses once per escape, not once
     *  per character: the old {@code (?:\\.|[^..])*} form overflowed the stack on a long literal (Service CI
     *  39328496341). Same output as the old pattern on every test source. */
    private static final Pattern NOT_CODE = Pattern.compile(
        "\"\"\"[^\"\\\\]*+(?:(?:\\\\.|\"(?!\"\"))[^\"\\\\]*+)*+\"\"\"|\"[^\"\\\\\\n]*+(?:\\\\.[^\"\\\\\\n]*+)*+\""
            + "|'(?:\\\\.|[^'\\\\])'|//[^\\n]*|/\\*.*?\\*/",
        Pattern.DOTALL);

    record Row(String clazz, String call, String kind, String reason) {
        String key() {
            return clazz + " " + call;
        }
    }

    static String codeOnly(String src) {
        return NOT_CODE.matcher(src).replaceAll("");
    }

    /** The {@code "<class> <entry point>"} keys of the direct calls and method references in {@code code}. */
    static Set<String> callsIn(String clazz, String code) {
        Set<String> found = new TreeSet<>();
        for (Pattern p : List.of(CALL, METHOD_REF)) {
            Matcher m = p.matcher(code);
            while (m.find()) {
                found.add(clazz + " " + m.group(1));
            }
        }
        return found;
    }

    static List<Row> parse(List<String> lines) {
        List<Row> rows = new ArrayList<>();
        for (String line : lines) {
            if (line.isBlank() || line.startsWith("#")) {
                continue;
            }
            String[] f = line.split("\t", -1);
            assertThat(f).as("four tab-separated columns: %s", line).hasSize(4);
            rows.add(new Row(f[0].strip(), f[1].strip(), f[2].strip(), f[3].strip()));
        }
        return rows;
    }

    /** Problems between the table and the callers found; empty when they agree. */
    static List<String> diff(List<Row> rows, Set<String> callerKeys) {
        List<String> problems = new ArrayList<>();
        Set<String> listed = new LinkedHashSet<>();
        for (Row r : rows) {
            Set<String> kinds = KINDS_BY_CALL.get(r.call());
            if (kinds == null) {
                problems.add(r.key() + ": call must be one of " + new TreeSet<>(KINDS_BY_CALL.keySet()));
                continue;
            }
            if (!kinds.contains(r.kind())) {
                problems.add(r.key() + ": kind " + r.kind() + " is not one of " + new TreeSet<>(kinds) + " for call " + r.call());
            }
            if (r.reason().length() < 25) {
                problems.add(r.key() + ": the reason is shorter than 25 characters, say what the class needs");
            }
            if (!listed.add(r.key())) {
                problems.add(r.key() + ": listed twice");
            }
        }
        for (String c : callerKeys) {
            if (!listed.contains(c)) {
                problems.add(c + ": calls it but is not in the table. If its subject is not the migration or a "
                    + "cluster-level fact, take a clone (PgContainerHelper.start() or startMigratedByOwner()); "
                    + "otherwise add a row with the reason");
            }
        }
        for (Row r : rows) {
            if (!r.call().equals("clone") && KINDS_BY_CALL.containsKey(r.call()) && !callerKeys.contains(r.key())) {
                problems.add(r.key() + ": listed but the class no longer calls it; remove the row (or, if it moved "
                    + "to a clone, make it a clone row)");
            }
        }
        return problems;
    }

    private static List<Path> testSources() throws IOException {
        try (Stream<Path> s = Files.walk(TEST_ROOT)) {
            return s.filter(p -> p.toString().endsWith(".java")).sorted().toList();
        }
    }

    private static String simpleName(Path p) {
        String n = p.getFileName().toString();
        return n.substring(0, n.length() - ".java".length());
    }

    @Test
    void every_class_that_boots_its_own_postgres_is_classified() throws IOException {
        List<Path> files = testSources();
        assertThat(files.size()).as("the scan reads the whole test tree").isGreaterThan(300);

        Set<String> callers = new TreeSet<>();
        Map<String, String> codeByClass = new java.util.HashMap<>();
        List<String> staticImports = new ArrayList<>();
        for (Path p : files) {
            if (INFRASTRUCTURE.contains(p.getFileName().toString())) {
                continue;
            }
            String code = codeOnly(Files.readString(p));
            String clazz = simpleName(p);
            codeByClass.put(clazz, code);
            if (STATIC_IMPORT.matcher(code).find()) {
                staticImports.add(clazz);
            }
            callers.addAll(callsIn(clazz, code));
        }
        assertThat(staticImports)
            .as("a static import of an entry point hides the call from this scan; qualify it with PgContainerHelper")
            .isEmpty();

        // Non-vacuity: the scan finds callers of every kind of entry point. Floors are today's live counts
        // (2026-10-19: 34 startEmptyDatabase, 10 startDedicated, 5 newContainer, 55 rows of which 6 clone rows)
        // minus a small margin, so losing a handful of callers from the scan fails here.
        assertThat(callers.stream().filter(c -> c.endsWith(" startEmptyDatabase")).count())
            .as("startEmptyDatabase callers found").isGreaterThanOrEqualTo(32);
        assertThat(callers.stream().filter(c -> c.endsWith(" startDedicated")).count())
            .as("startDedicated callers found").isGreaterThanOrEqualTo(8);
        assertThat(callers.stream().filter(c -> c.endsWith(" newContainer")).count())
            .as("newContainer callers found").isGreaterThanOrEqualTo(4);

        List<Row> rows = parse(Files.readAllLines(TABLE));
        assertThat(rows.size()).as("rows in the table").isGreaterThanOrEqualTo(52);
        assertThat(rows.stream().filter(r -> r.call().equals("clone")).count())
            .as("clone rows in the table").isGreaterThanOrEqualTo(6);

        List<String> problems = new ArrayList<>(diff(rows, callers));
        for (Row r : rows) {
            if (!r.call().equals("clone")) {
                continue;
            }
            String code = codeByClass.get(r.clazz());
            if (code == null) {
                problems.add(r.key() + ": no such test class");
                continue;
            }
            Pattern needed = r.kind().equals("CLONE_OWNER") ? TAKES_OWNER_CLONE : TAKES_CLONE;
            if (!needed.matcher(code).find()) {
                problems.add(r.key() + ": a " + r.kind() + " row, but the class does not call "
                    + (r.kind().equals("CLONE_OWNER") ? "PgContainerHelper.startMigratedByOwner()" : "PgContainerHelper.start()"));
            }
        }
        assertThat(problems).as("db-start-classification.tsv against the test tree").isEmpty();
    }

    // ── the check itself, falsified on synthetic input ───────────────────────────────────────────────────────

    private static final String REASON = "a reason long enough to say what the class needs";

    @Test
    void an_unlisted_caller_fails() {
        List<Row> rows = List.of(new Row("A", "startEmptyDatabase", "WALK", REASON));
        assertThat(diff(rows, Set.of("A startEmptyDatabase", "B startEmptyDatabase")))
            .singleElement().asString().startsWith("B startEmptyDatabase: calls it but is not in the table");
    }

    @Test
    void a_stale_row_fails() {
        List<Row> rows = List.of(new Row("A", "startDedicated", "SIZED", REASON));
        assertThat(diff(rows, Set.of())).singleElement().asString().contains("no longer calls it");
    }

    @Test
    void a_kind_outside_the_calls_vocabulary_fails() {
        List<Row> rows = List.of(new Row("A", "startDedicated", "WALK", REASON));
        assertThat(diff(rows, Set.of("A startDedicated"))).singleElement().asString().contains("kind WALK is not one of");
    }

    @Test
    void a_thin_reason_and_a_duplicate_row_fail() {
        List<Row> rows = List.of(
            new Row("A", "newContainer", "IMAGE", "image"), new Row("A", "newContainer", "IMAGE", REASON));
        assertThat(diff(rows, Set.of("A newContainer"))).hasSize(2);
    }

    @Test
    void a_method_reference_counts_as_a_call() {
        String src = """
            withCount("false", PgContainerHelper::newContainer);
            withCount(null, PgContainerHelper :: startDedicated);
            Callable<?> c = PgContainerHelper::startEmptyDatabase;
            Supplier<?> s = PgContainerHelper::start;
            """;
        assertThat(callsIn("X", codeOnly(src))).containsExactly("X newContainer", "X startDedicated", "X startEmptyDatabase");
        assertThat(TAKES_CLONE.matcher(codeOnly(src)).find()).as("PgContainerHelper::start is a clone").isTrue();
        assertThat(callsIn("X", codeOnly("var f = PgContainerHelper::startMigratedByOwner; // not an entry point"))).isEmpty();
    }

    @Test
    void comments_and_strings_do_not_count_as_calls() {
        String src = """
            /** takes PgContainerHelper.startDedicated() in prose */
            // PgContainerHelper.newContainer()
            String s = "PgContainerHelper.startEmptyDatabase()";
            String t = \"\"\"
                PgContainerHelper.startDedicated()
                \"\"\";
            var c = PgContainerHelper.startEmptyDatabase();
            """;
        Matcher m = CALL.matcher(codeOnly(src));
        List<String> found = new ArrayList<>();
        while (m.find()) {
            found.add(m.group(1));
        }
        assertThat(found).containsExactly("startEmptyDatabase");
    }

    /** A 200k-character string and text block strip without a StackOverflowError: the per-character (?:a|b)* form overflowed the stack on long input (Service CI 39328496341) */
    @Test
    void codeOnly_stripsAHugeLiteralWithoutOverflowingTheStack() {
        String big = "x".repeat(200_000);
        String src = "class X { String a = \"" + big + "\"; String b = \"\"\"\n" + big + "\"\"\"; }";
        assertThat(codeOnly(src)).isEqualTo("class X { String a = ; String b = ; }");
    }
}
