// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.TreeMap;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 7 structural guard (review T2 nexus/review-wbfpw15-17-critique, Issue 4): the two
 * obligations a caller of {@code nexus.chunk_is_reapable} carries are prose in the function's header,
 * so they are made mechanical here, the way {@link ChunksWriterLastWrittenAtScanTest} does it for the
 * writers of {@code last_written_at}.
 *
 * <p>The obligations, for any statement that DELETEs or copy-INSERTs chunks under the predicate:
 * <ol>
 *   <li>it sits in a function that takes the EXCLUSIVE per-collection sweep gate
 *       ({@code pg_advisory_xact_lock(hashtext('sweepgate:' ...))}), because the predicate itself takes no
 *       lock and a manifest row committed during the statement is only the gate's concern;</li>
 *   <li>it passes a literal {@code NULL} grace, so the 30 day default is the only window. A caller that
 *       wants a configurable window must clamp it to a floor first (a small window reopens the race the
 *       grace exists to close), and that is a decision to make deliberately, here, in review.</li>
 * </ol>
 * The named exemption is the read-only listing route, {@code PgVectorRepository#reapableChunks}, which
 * deletes and copies nothing and so needs neither. Any other Java use of the predicate fails this test: a
 * future reaper adds itself to {@link #JAVA_CONSUMERS} with its gate and its clamp, in the same change.
 *
 * <p>The scan reads the Liquibase changelogs for SQL functions and {@code service/src/main/java} for
 * the typed jOOQ call. It cannot see a statement assembled at run time, which the RawSqlGate already
 * forbids.
 */
class ReapableConsumersScanTest {

    /** The only Java file, and the only method in it, allowed to call the predicate. */
    private static final String LISTING_FILE = "PgVectorRepository.java";
    private static final String LISTING_METHOD_START = "/** One chunk {@code reapable(c)} selects";
    private static final String LISTING_METHOD_END = "private static org.jooq.types.YearToSecond exactSeconds";

    /** Java consumers other than the listing, each with the reason it is gated. Empty today. */
    private static final Map<String, String> JAVA_CONSUMERS = new TreeMap<>();

    private static final Pattern FUNCTION = Pattern.compile(
        "CREATE OR REPLACE FUNCTION\\s+(\\S+?)\\s*\\(.*?(?=CREATE OR REPLACE FUNCTION|\\z)", Pattern.DOTALL);
    private static final Pattern SQL_BLOCK = Pattern.compile("<sql[^>]*>(.*?)</sql>", Pattern.DOTALL);
    private static final Pattern ROLLBACK = Pattern.compile("<rollback>.*?</rollback>", Pattern.DOTALL);
    private static final Pattern CALL = Pattern.compile("chunk_is_reapable\\(([^()]*)\\)");

    /** Code (not a comment) that calls the predicate: the jOOQ table function, or SQL text naming it in a string literal. */
    private static boolean usesPredicate(String javaSource) {
        return javaSource.contains("CHUNK_IS_REAPABLE") || javaSource.contains("ChunkIsReapable")
            || Pattern.compile("\"[^\"\\n]*chunk_is_reapable\\(").matcher(javaSource).find();
    }

    /** Why {@code body} (one function's text) breaks the obligations, or an empty list. */
    static List<String> violations(String functionName, String body) {
        List<String> out = new ArrayList<>();
        Matcher calls = CALL.matcher(body);
        int n = 0;
        while (calls.find()) {
            n++;
            String[] args = calls.group(1).split(",");
            if (!args[args.length - 1].trim().equals("NULL")) {
                out.add(functionName + ": chunk_is_reapable is called with grace '" + args[args.length - 1].trim()
                    + "', not a literal NULL; clamp a configurable grace to a floor and allowlist it here");
            }
        }
        if (n == 0) return out;
        boolean destructive = body.contains("DELETE FROM nexus.chunks") || body.contains("INSERT INTO nexus.chunks");
        if (destructive && !body.contains("pg_advisory_xact_lock(hashtext('sweepgate:'")) {
            out.add(functionName + ": deletes or copies chunks under chunk_is_reapable without taking the exclusive sweep gate");
        }
        return out;
    }

    @Test
    void everySqlFunctionThatDeletesOrCopiesChunksUnderThePredicate_takesTheSweepGate_andPassesNullGrace()
            throws IOException {
        List<String> found = new ArrayList<>();
        List<String> problems = new ArrayList<>();
        try (Stream<Path> walk = Files.walk(Path.of("src", "main", "resources", "db", "changelog"))) {
            for (Path p : walk.filter(f -> f.toString().endsWith(".xml")).sorted().toList()) {
                String xml = Files.readString(p);
                String forward = ROLLBACK.matcher(xml).replaceAll("");
                Matcher blocks = SQL_BLOCK.matcher(forward);
                while (blocks.find()) {
                    Matcher fns = FUNCTION.matcher(blocks.group(1));
                    while (fns.find()) {
                        String name = fns.group(1);
                        if (name.endsWith("chunk_is_reapable")) continue;   // the definition itself
                        if (!fns.group().contains("chunk_is_reapable(")) continue;
                        found.add(p.getFileName() + "#" + name);
                        problems.addAll(violations(p.getFileName() + "#" + name, fns.group()));
                    }
                }
            }
        }

        assertThat(found).as("non-vacuity: the scan found the gc functions that use the predicate")
            .anyMatch(f -> f.endsWith("#nexus.gc_quarantine_orphans"))
            .anyMatch(f -> f.endsWith("#nexus.gc_quarantine_orphans_bounded"));
        assertThat(problems).isEmpty();
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
                // The import and the listing are the only mentions; anything else is another consumer.
                String stripped = outside.replace("import static dev.nexus.service.jooq.nexus.Tables.CHUNK_IS_REAPABLE;", "");
                if (usesPredicate(stripped)) {
                    offenders.add(name + " (outside reapableChunks)");
                }
                // And the listing itself must stay read-only.
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

    @Test
    void theScannerFlagsADeleteWithoutTheGate() {
        String body = "CREATE OR REPLACE FUNCTION nexus.bad() ... DELETE FROM nexus.chunks c WHERE EXISTS ("
            + "SELECT 1 FROM nexus.chunk_is_reapable(c.tenant_id, c.collection, c.chash, c.last_written_at, NULL))";
        assertThat(violations("bad", body)).anyMatch(v -> v.contains("without taking the exclusive sweep gate"));
    }

    @Test
    void theScannerFlagsANonNullGrace() {
        String body = "pg_advisory_xact_lock(hashtext('sweepgate:' || t)) DELETE FROM nexus.chunks c WHERE EXISTS ("
            + "SELECT 1 FROM nexus.chunk_is_reapable(c.tenant_id, c.collection, c.chash, c.last_written_at, p_grace))";
        assertThat(violations("bad", body)).anyMatch(v -> v.contains("not a literal NULL"));
    }

    @Test
    void theScannerAcceptsAGatedNullGraceConsumer() {
        String body = "pg_advisory_xact_lock(hashtext('sweepgate:' || t)) DELETE FROM nexus.chunks c WHERE EXISTS ("
            + "SELECT 1 FROM nexus.chunk_is_reapable(c.tenant_id, c.collection, c.chash, c.last_written_at, NULL))";
        assertThat(violations("ok", body)).isEmpty();
    }
}
