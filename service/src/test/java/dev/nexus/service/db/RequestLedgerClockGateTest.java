/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-yye8l.22 gate: {@code RequestLedger} and {@code KeyedSweep} read no JVM clock. Every instant they
 * compare or hand to a client ({@code started_at}, {@code lease_until}, the {@code InFlight} reading behind
 * {@code Retry-After}) comes from the database clock ({@code clock_timestamp()}), so the engine's lease
 * arithmetic cannot drift from the database's by JVM-to-container skew (nexus-yye8l.20). A behavioural test
 * cannot pin that: it runs the JVM and the container on one host clock, where the two agree. A seam on classes
 * that read no clock would be dead code, so the claim is enforced here, by scanning the two sources with
 * comments and string literals removed.
 *
 * <p>Non-vacuity: both files must be found and non-trivial, the patterns are shown to match every clock
 * shape they exist to forbid, the comment/literal stripper is shown to hide a banned token that appears only
 * in prose, and a banned token in code is shown to survive it.
 */
class RequestLedgerClockGateTest {

    /** Main-tree paths, relative to the service module, of the classes that must stay clock-free. */
    static final List<Path> GUARDED = List.of(
        Path.of("src", "main", "java", "dev", "nexus", "service", "db", "RequestLedger.java"),
        Path.of("src", "main", "java", "dev", "nexus", "service", "http", "KeyedSweep.java"));

    /** Each shape a JVM clock read takes. No pattern holds a repeated group. */
    static final List<Pattern> JVM_CLOCK_READS = List.of(
        Pattern.compile("\\bSystem\\s*\\.\\s*(?:currentTimeMillis|nanoTime)\\s*\\("),
        Pattern.compile("(?<![.\\w])(?:currentTimeMillis|nanoTime)\\s*\\("),
        Pattern.compile("\\.\\s*now\\s*\\("),
        Pattern.compile("(?<![.\\w])now\\s*\\("),
        Pattern.compile("\\bClock\\s*\\.\\s*(?:system|systemUTC|systemDefaultZone|tick)\\w*\\s*\\("),
        Pattern.compile("\\bnew\\s+Date\\s*\\(\\s*\\)"));

    /**
     * {@code src} with block comments, line comments, string literals, text blocks and char literals blanked
     * (newlines kept), so prose such as "never {@code now()}" cannot trip the scan and code cannot hide in a
     * string. A single left-to-right pass; no regex.
     */
    static String stripCommentsAndLiterals(String src) {
        StringBuilder out = new StringBuilder(src.length());
        int i = 0;
        int n = src.length();
        while (i < n) {
            char c = src.charAt(i);
            char next = i + 1 < n ? src.charAt(i + 1) : '\0';
            if (c == '/' && next == '*') {
                int end = src.indexOf("*/", i + 2);
                int stop = end < 0 ? n : end + 2;
                blank(out, src, i, stop);
                i = stop;
            } else if (c == '/' && next == '/') {
                int end = src.indexOf('\n', i);
                int stop = end < 0 ? n : end;
                blank(out, src, i, stop);
                i = stop;
            } else if (c == '"' && src.startsWith("\"\"\"", i)) {
                int end = src.indexOf("\"\"\"", i + 3);
                int stop = end < 0 ? n : end + 3;
                blank(out, src, i, stop);
                i = stop;
            } else if (c == '"' || c == '\'') {
                int j = i + 1;
                while (j < n && src.charAt(j) != c && src.charAt(j) != '\n') {
                    j += src.charAt(j) == '\\' ? 2 : 1;
                }
                int stop = Math.min(j + 1, n);
                blank(out, src, i, stop);
                i = stop;
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }

    private static void blank(StringBuilder out, String src, int from, int to) {
        for (int k = from; k < to; k++) {
            out.append(src.charAt(k) == '\n' ? '\n' : ' ');
        }
    }

    static List<String> clockReads(String src) {
        String code = stripCommentsAndLiterals(src);
        List<String> hits = new ArrayList<>();
        for (Pattern p : JVM_CLOCK_READS) {
            var m = p.matcher(code);
            while (m.find()) {
                hits.add(m.group().strip());
            }
        }
        return hits;
    }

    @Test
    void thePatternsMatchEveryClockShapeTheyForbid() {
        for (String shape : List.of(
                "long t = System.currentTimeMillis();",
                "long t = System . nanoTime ();",
                "long t = currentTimeMillis();",
                "var t = Instant.now();",
                "var t = OffsetDateTime.now(ZoneOffset.UTC);",
                "var t = LocalDateTime.now();",
                "var t = java.time.OffsetDateTime.now();",
                "var t = now();",
                "var c = Clock.systemUTC();",
                "var c = Clock.system(zone);",
                "var c = Clock.systemDefaultZone();",
                "var d = new Date();")) {
            assertThat(clockReads(shape)).as(shape).isNotEmpty();
        }
    }

    @Test
    void theStripperHidesAClockNameOnlyInProseOrLiteralsAndKeepsOneInCode() {
        String prose = "/** never {@code now()}, not System.nanoTime() */\n"
            + "// Instant.now() is banned\n"
            + "String s = \"OffsetDateTime.now()\";\n"
            + "String t = \"\"\"\n  System.currentTimeMillis()\n  \"\"\";\n"
            + "char q = '\"'; int x = 1;\n";
        assertThat(clockReads(prose)).as("prose and literals are not code").isEmpty();
        assertThat(clockReads(prose + "long t = System.nanoTime();\n"))
            .as("a real read after them is still found").hasSize(1);
        assertThat(stripCommentsAndLiterals("a\n/* b\n c */\nd")).as("line numbers are kept")
            .hasSize("a\n/* b\n c */\nd".length()).contains("a\n").endsWith("\nd");
    }

    @Test
    void requestLedgerAndKeyedSweepReadNoJvmClock() throws IOException {
        List<String> offenders = new ArrayList<>();
        int scanned = 0;
        long scannedChars = 0;
        for (Path file : GUARDED) {
            assertThat(file).as("run from the service module; the guarded source must exist").isRegularFile();
            String src = Files.readString(file);
            scanned++;
            scannedChars += src.length();
            for (String hit : clockReads(src)) {
                offenders.add(file.getFileName() + ": JVM clock read '" + hit + "'; use the database clock"
                    + " (clock_timestamp()) so lease and Retry-After arithmetic has no JVM instant in it");
            }
        }
        assertThat(scanned).as("both guarded files were scanned").isEqualTo(GUARDED.size());
        assertThat(scannedChars).as("and they were not empty").isGreaterThan(10_000L);
        assertThat(offenders).isEmpty();
    }

    @Test
    void theGuardedSourcesStillUseTheDatabaseClock() throws IOException {
        String ledger = Files.readString(GUARDED.get(0));
        assertThat(ledger)
            .as("RequestLedger stamps and compares with clock_timestamp(), the reading this gate protects")
            .contains("DSL.function(\"clock_timestamp\"");
    }
}
