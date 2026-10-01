// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 7 structural guard (review T2 nexus/review-wbfpw15-17-critique, Issue 4, and the verification
 * round's I4): the two obligations a caller of {@code nexus.chunk_is_reapable} carries are prose in the
 * function's header, so they are made mechanical here, the way {@link ChunksWriterLastWrittenAtScanTest}
 * does it for the writers of {@code last_written_at}.
 *
 * <p>The obligations, for any statement that DELETEs or copy-INSERTs chunks under the predicate:
 * <ol>
 *   <li>it sits in a function that takes the EXCLUSIVE per-collection sweep gate
 *       ({@code pg_advisory_xact_lock(hashtext('sweepgate:' ...))}), because the predicate itself takes no
 *       lock and a manifest row committed during the statement is only the gate's concern;</li>
 *   <li>it passes a literal {@code NULL} grace, so the 30 day default is the only window. A caller that
 *       wants a configurable window must clamp it to a floor first (a small window reopens the race the
 *       grace exists to close), and that is a decision to make deliberately, here, in review. A clamp
 *       written as {@code GREATEST(p_grace, ...)} or {@code make_interval(...)} is therefore NOT accepted
 *       by this scan: the consumer is added to {@link #SQL_CONSUMERS} with its reason.</li>
 * </ol>
 * The named exemption is the read-only listing route, {@code PgVectorRepository#reapableChunks}, which
 * deletes and copies nothing and so needs neither. Any other Java use of the predicate fails this test.
 *
 * <p>The SQL scan reads the Liquibase changelogs: every {@code <sql>} block outside a {@code <rollback>},
 * split into {@code CREATE [OR REPLACE] FUNCTION|PROCEDURE} units plus the text before the first one (a
 * {@code DO} block). A unit that mentions the predicate in ANY spelling must parse at least one call: zero
 * parsed calls in a unit that mentions it and deletes or copies chunks is a FAILURE, never a pass, because
 * a scanner that cannot read a call has not checked it (the first version of this scan returned no
 * violations for exactly those bodies). Calls are parsed with balanced parentheses.
 */
class ReapableConsumersScanTest {

    /** The only Java file, and the only region in it, allowed to call the predicate. */
    private static final String LISTING_FILE = "PgVectorRepository.java";
    private static final String LISTING_METHOD_START = "/** One chunk {@code reapable(c)} selects";
    private static final String LISTING_METHOD_END = "private static org.jooq.types.YearToSecond exactSeconds";

    /** Java consumers other than the listing, each with the reason it is gated. Empty today. */
    private static final Map<String, String> JAVA_CONSUMERS = new TreeMap<>();

    /** SQL consumers whose grace is not a literal NULL, each with where its floor clamp lives. Empty today. */
    private static final Map<String, String> SQL_CONSUMERS = new TreeMap<>();

    private static final Pattern UNIT_START = Pattern.compile(
        "(?i)CREATE\\s+(?:OR\\s+REPLACE\\s+)?(FUNCTION|PROCEDURE)\\s+([^\\s(]+)");
    private static final Pattern SQL_BLOCK = Pattern.compile("<sql[^>]*>(.*?)</sql>", Pattern.DOTALL);
    private static final Pattern ROLLBACK = Pattern.compile("<rollback>.*?</rollback>", Pattern.DOTALL);
    private static final Pattern MENTION = Pattern.compile("(?i)chunk_is_reapable");
    private static final Pattern CALL_START = Pattern.compile("(?i)chunk_is_reapable\\s*\\(");
    private static final Pattern DESTRUCTIVE = Pattern.compile(
        "(?i)(DELETE\\s+FROM\\s+nexus\\.chunks\\b|INSERT\\s+INTO\\s+nexus\\.chunks\\b)");
    private static final Pattern GATE = Pattern.compile("pg_advisory_xact_lock\\s*\\(\\s*hashtext\\s*\\(\\s*'sweepgate:'");

    /** The text between the parenthesis at {@code open} and its matching close, or null when unbalanced. */
    private static String balanced(String s, int open) {
        int depth = 0;
        for (int i = open; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '(') depth++;
            else if (c == ')' && --depth == 0) return s.substring(open + 1, i);
        }
        return null;
    }

    /** Top-level comma split: commas inside nested parentheses or quotes do not split. */
    private static List<String> topLevelArgs(String args) {
        List<String> out = new ArrayList<>();
        int depth = 0;
        boolean quoted = false;
        StringBuilder cur = new StringBuilder();
        for (char c : args.toCharArray()) {
            if (c == '\'') quoted = !quoted;
            if (!quoted && c == '(') depth++;
            if (!quoted && c == ')') depth--;
            if (!quoted && depth == 0 && c == ',') {
                out.add(cur.toString().trim());
                cur.setLength(0);
            } else {
                cur.append(c);
            }
        }
        out.add(cur.toString().trim());
        return out;
    }

    /** Why {@code body} (one function, procedure or DO block) breaks the obligations, or an empty list. */
    static List<String> violations(String unitName, String body) {
        List<String> out = new ArrayList<>();
        if (!MENTION.matcher(body).find()) return out;
        boolean destructive = DESTRUCTIVE.matcher(body).find();
        Matcher starts = CALL_START.matcher(body);
        int parsed = 0;
        while (starts.find()) {
            String args = balanced(body, starts.end() - 1);
            if (args == null) {
                out.add(unitName + ": a chunk_is_reapable call has unbalanced parentheses and cannot be checked");
                continue;
            }
            parsed++;
            List<String> list = topLevelArgs(args);
            String grace = list.get(list.size() - 1);
            if (!grace.equalsIgnoreCase("NULL")) {
                out.add(unitName + ": chunk_is_reapable is called with grace '" + grace + "', not a literal NULL;"
                    + " clamp a configurable grace to a floor and allowlist the consumer in SQL_CONSUMERS");
            }
        }
        if (parsed == 0 && destructive) {
            out.add(unitName + ": deletes or copies chunks and mentions chunk_is_reapable, but no call could be parsed:"
                + " an unreadable call is an unchecked one");
        }
        if (destructive && !GATE.matcher(body).find()) {
            out.add(unitName + ": deletes or copies chunks under chunk_is_reapable without taking the exclusive sweep gate");
        }
        return out;
    }

    /** The units of one {@code <sql>} block: the text before the first CREATE (a DO block), then each CREATE. */
    static Map<String, String> units(String sql) {
        Map<String, String> out = new TreeMap<>();
        Matcher m = UNIT_START.matcher(sql);
        List<int[]> spans = new ArrayList<>();
        List<String> names = new ArrayList<>();
        while (m.find()) {
            spans.add(new int[] {m.start(), 0});
            names.add(m.group(2));
        }
        int firstStart = spans.isEmpty() ? sql.length() : spans.get(0)[0];
        if (!sql.substring(0, firstStart).isBlank()) out.put("<anonymous or DO block>", sql.substring(0, firstStart));
        for (int i = 0; i < spans.size(); i++) {
            int end = i + 1 < spans.size() ? spans.get(i + 1)[0] : sql.length();
            out.merge(names.get(i), sql.substring(spans.get(i)[0], end), (a, b) -> a + b);
        }
        return out;
    }

    @Test
    void everySqlUnitThatDeletesOrCopiesChunksUnderThePredicate_takesTheSweepGate_andPassesNullGrace()
            throws IOException {
        List<String> found = new ArrayList<>();
        List<String> problems = new ArrayList<>();
        try (Stream<Path> walk = Files.walk(Path.of("src", "main", "resources", "db", "changelog"))) {
            for (Path p : walk.filter(f -> f.toString().endsWith(".xml")).sorted().toList()) {
                String forward = ROLLBACK.matcher(Files.readString(p)).replaceAll("");
                Matcher blocks = SQL_BLOCK.matcher(forward);
                while (blocks.find()) {
                    for (var unit : units(blocks.group(1)).entrySet()) {
                        String name = unit.getKey();
                        if (name.endsWith("chunk_is_reapable")) continue;   // the definition itself
                        if (!MENTION.matcher(unit.getValue()).find()) continue;
                        String id = p.getFileName() + "#" + name;
                        found.add(id);
                        if (SQL_CONSUMERS.containsKey(id)) continue;
                        problems.addAll(violations(id, unit.getValue()));
                    }
                }
            }
        }

        assertThat(found).as("non-vacuity: the scan found the gc functions that use the predicate")
            .anyMatch(f -> f.endsWith("#nexus.gc_quarantine_orphans"))
            .anyMatch(f -> f.endsWith("#nexus.gc_quarantine_orphans_bounded"));
        assertThat(problems).isEmpty();
    }

    /** Code (not a comment) that calls the predicate: the jOOQ table function, or SQL text naming it in a string literal. */
    private static boolean usesPredicate(String javaSource) {
        return javaSource.contains("CHUNK_IS_REAPABLE") || javaSource.contains("ChunkIsReapable")
            || Pattern.compile("\"[^\"\\n]*chunk_is_reapable\\s*\\(").matcher(javaSource).find();
    }

    @Test
    void theOnlyJavaCallerOfThePredicateIsTheReadOnlyListing() throws IOException {
        List<String> offenders = new ArrayList<>();
        int seen = 0;
        try (Stream<Path> walk = Files.walk(Path.of("src", "main", "java"))) {
            for (Path p : walk.filter(f -> f.toString().endsWith(".java")).sorted().toList()) {
                String src = Files.readString(p);
                if (!usesPredicate(src)) continue;
                seen++;
                String name = p.getFileName().toString();
                if (!name.equals(LISTING_FILE)) {
                    if (!JAVA_CONSUMERS.containsKey(name)) offenders.add(name);
                    continue;
                }
                int start = src.indexOf(LISTING_METHOD_START);
                int end = src.indexOf(LISTING_METHOD_END);
                assertThat(start).as("the listing method moved or was renamed; update this scan").isGreaterThan(0);
                assertThat(end).isGreaterThan(start);
                String outside = src.substring(0, start) + src.substring(end);
                String stripped = outside.replace("import static dev.nexus.service.jooq.nexus.Tables.CHUNK_IS_REAPABLE;", "");
                if (usesPredicate(stripped)) offenders.add(name + " (outside reapableChunks)");
                String listing = src.substring(start, end);
                assertThat(listing).as("the named exemption is read-only").doesNotContain("deleteFrom")
                    .doesNotContain("insertInto").doesNotContain(".update(");
            }
        }
        assertThat(seen).as("non-vacuity: the listing was found").isGreaterThan(0);
        assertThat(offenders)
            .as("A Java caller of chunk_is_reapable that is not the read-only listing must take the exclusive"
                + " sweep gate and pass a NULL or clamped grace; add it to JAVA_CONSUMERS with the reason")
            .isEmpty();
    }

    // ── the scanner can fail ──────────────────────────────────────────────────

    private static final String DELETE = "DELETE FROM nexus.chunks c WHERE EXISTS (SELECT 1 FROM nexus.chunk_is_reapable";
    private static final String GATED = "pg_advisory_xact_lock(hashtext('sweepgate:' || t)) ";

    @Test
    void theScannerFlagsADeleteWithoutTheGate() {
        assertThat(violations("bad", DELETE + "(c.tenant_id, c.collection, c.chash, c.last_written_at, NULL))"))
            .anyMatch(v -> v.contains("without taking the exclusive sweep gate"));
    }

    @Test
    void theScannerFlagsABareGraceParameter() {
        assertThat(violations("bad", GATED + DELETE + "(c.tenant_id, c.collection, c.chash, c.last_written_at, p_grace))"))
            .anyMatch(v -> v.contains("not a literal NULL"));
    }

    /** The reviewer's example: a clamped grace with nested parentheses, and no gate. The first scan returned []. */
    @Test
    void theScannerFlagsAGreatestClampedGrace_andTheMissingGate() {
        List<String> v = violations("bad", DELETE
            + "(c.tenant_id, c.collection, c.chash, c.last_written_at, GREATEST(p_grace, interval '1 day')))");
        assertThat(v).anyMatch(x -> x.contains("not a literal NULL") && x.contains("GREATEST(p_grace, interval '1 day')"));
        assertThat(v).anyMatch(x -> x.contains("without taking the exclusive sweep gate"));
    }

    @Test
    void theScannerFlagsAMakeIntervalGrace() {
        assertThat(violations("bad", GATED + DELETE
            + "(c.tenant_id, c.collection, c.chash, c.last_written_at, make_interval(days => 0)))"))
            .anyMatch(v -> v.contains("not a literal NULL") && v.contains("make_interval(days => 0)"));
    }

    @Test
    void theScannerReadsACallWithASpaceBeforeTheParenthesis() {
        assertThat(violations("bad", DELETE + " (c.tenant_id, c.collection, c.chash, c.last_written_at, NULL))"))
            .as("parsed, so the missing gate is reported").anyMatch(v -> v.contains("without taking the exclusive sweep gate"));
        assertThat(violations("ok", GATED + DELETE + " (c.tenant_id, c.collection, c.chash, c.last_written_at, NULL))"))
            .isEmpty();
    }

    @Test
    void zeroParsedCallsInAnUnitThatDeletesChunksIsAFailure_notAPass() {
        // A call the scanner cannot read (here: unbalanced) in a body that deletes chunks.
        assertThat(violations("bad", GATED + "DELETE FROM nexus.chunks c WHERE EXISTS (SELECT 1 FROM nexus.chunk_is_reapable ("))
            .isNotEmpty();
        // Mentioned only in a comment or a name, with a delete beside it: nothing parses, so it fails too.
        assertThat(violations("bad", GATED + "DELETE FROM nexus.chunks c WHERE true -- see chunk_is_reapable"))
            .anyMatch(v -> v.contains("no call could be parsed"));
    }

    @Test
    void theScannerAcceptsAGatedNullGraceConsumer() {
        assertThat(violations("ok", GATED + DELETE + "(c.tenant_id, c.collection, c.chash, c.last_written_at, NULL))"))
            .isEmpty();
    }

    @Test
    void theScannerSeesDoBlocksAndCreateFunctionAndCreateProcedure() {
        String doBlock = "DO $$ BEGIN " + DELETE + "(c.tenant_id, c.collection, c.chash, c.last_written_at, NULL)); END $$;"
            + " CREATE PROCEDURE nexus.p() AS $$ BEGIN " + DELETE
            + "(c.tenant_id, c.collection, c.chash, c.last_written_at, NULL)); END $$;"
            + " CREATE FUNCTION nexus.f() RETURNS void AS $$ BEGIN " + DELETE
            + "(c.tenant_id, c.collection, c.chash, c.last_written_at, NULL)); END $$;";
        Map<String, String> units = units(doBlock);
        assertThat(units.keySet()).containsExactlyInAnyOrder("<anonymous or DO block>", "nexus.p", "nexus.f");
        units.forEach((name, body) -> assertThat(violations(name, body))
            .as("%s has no gate", name).anyMatch(v -> v.contains("without taking the exclusive sweep gate")));
    }
}
