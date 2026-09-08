// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;

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

/**
 * House-rule gate (bead nexus-ft04v.15, RDR-204 Phase 2 item 3; ACCEPTANCE
 * SIGNAL 1 of 2): pins the count of collection-name PARSE sites in {@code
 * service/src/main/java} at the measured value, reduce-only, so the sibling
 * bead nexus-ft04v.16 (which replaces those sites with a registry lookup)
 * has a visible ratchet to drive to zero. Modeled on {@link
 * dev.nexus.service.db.RawSqlGateTest}'s per-file declared-count ceiling
 * (the pattern §Existing Infrastructure Audit names): comment-blanked source,
 * a per-file {@code Map<String, Integer>} pinned at the measured count, an
 * over-count fails as a REGRESSION, a file falling BELOW its declared count
 * fails as a STALE FINGERPRINT (the direction that keeps a pin from silently
 * rotting once the sites it counts are deleted), and a non-vacuity assert
 * that the walk actually saw files and matched at least the declared total.
 *
 * <p><b>Why this does not count {@code split("__")} alone.</b> The RDR text
 * that seeded this bead describes the target as "pins the count of {@code
 * split("__")} on collection names", but the measured engine census (T2
 * {@code nexus/rdr-204-planning-source-corrections-2026-09-07} [24871],
 * finding 1) found that {@code EmbedderRouter}'s content-type routing
 * ({@code CCE_PREFIXES}/{@code CODE_PREFIX} and their {@code startsWith}
 * uses, and {@code stripTrailingSeparator}'s {@code endsWith("__")}) derives
 * content type from the collection name WITHOUT ever calling {@code split}.
 * A gate that grepped {@code split("__")} alone would sit at zero the moment
 * those two files' {@code split} calls were converted while the prefix-based
 * routing survived untouched — exactly the kind of gate that reports a false
 * "done" (the class javadoc's own worked example: {@code
 * StagingPromoteOps}'s {@code contains}/{@code indexOf}/{@code substring}
 * trio was a second call shape entirely absent from a split-only scan, and
 * was in fact already deleted by the time this bead landed).
 *
 * <p><b>What is actually counted.</b> Every constant that feeds a parse
 * call ({@code CCE_PREFIXES}, {@code CODE_PREFIX}) and every inline literal
 * argument to a parse call ({@code split("__")}, {@code endsWith("__")}) is,
 * at the Java source level, a STRING LITERAL containing {@code "__"} — the
 * constant's own declaration site is one such literal, and an inline call is
 * another. So {@link #countDunderLiterals} counts every non-comment string
 * literal containing {@code "__"} per file; that census covers constants and
 * inline calls alike without having to track which named constant feeds
 * which call site (a dataflow problem this gate deliberately does not take
 * on). {@link #countSplitDunderCalls} additionally pins {@code split("__")}
 * specifically, as its own narrower per-file count, purely so nexus-ft04v.16's
 * acceptance criterion — "the split pin falls to zero" — has a number to
 * point at; it is a subset of the literal census, not an independent one.
 *
 * <p><b>Reduce-only.</b> Exactly like {@link
 * dev.nexus.service.db.RawSqlGateTest#TEST_TREE_RAW_SQL_CEILING}: a file
 * whose count goes to zero must have its entry REMOVED, not left at {@code
 * 0} — the "no entry means zero expected" default already covers that, and a
 * stray {@code file -> 0} entry is dead weight the stale-fingerprint check
 * cannot distinguish from a real ceiling. Both {@link
 * #COLLECTION_LITERAL_CENSUS} and {@link #COLLECTION_SPLIT_CENSUS} — plus
 * their total ceilings — are expected to fall to zero at nexus-ft04v.16 once
 * the parse sites read the registry row instead of the collection-name
 * string itself.
 *
 * <p>This gate is a pure source scanner (no jOOQ, no DB, no {@code
 * scripts/mvnw-leased.sh} substrate beyond plain {@code javac}/JUnit) — it
 * walks {@code service/src/main/java} once per {@code @Test} run and reads
 * plain text, exactly like {@code RawSqlGateTest} does for its own tree
 * walks.
 */
class CollectionParseGateTest {

    /**
     * Comment-blanking identical in shape to {@link
     * dev.nexus.service.db.RawSqlGateTest#blankComments} — duplicated here
     * rather than shared because that method is package-private to {@code
     * dev.nexus.service.db} and this gate lives in {@code
     * dev.nexus.service.vectors} (the package the parse sites themselves
     * live in, per this bead's own instructions). Blanks {@code //} and
     * {@code /* *}{@code /} comments to spaces (preserving line numbers);
     * string and char literals are skipped over WITHOUT being blanked, so a
     * literal's own content — including one that happens to contain {@code
     * /}{@code *} or {@code //} — survives intact for {@link
     * #countDunderLiterals} to inspect.
     */
    static String blankComments(String src) {
        char[] out = src.toCharArray();
        int i = 0;
        while (i < out.length) {
            char c = out[i];
            if (c == '/' && i + 1 < out.length && out[i + 1] == '*') {
                int end = src.indexOf("*/", i + 2);
                end = (end < 0) ? out.length : end + 2;
                for (int j = i; j < end; j++) if (out[j] != '\n') out[j] = ' ';
                i = end;
            } else if (c == '/' && i + 1 < out.length && out[i + 1] == '/') {
                while (i < out.length && out[i] != '\n') out[i++] = ' ';
            } else if (c == '"' || c == '\'') {
                char q = c;
                i++;
                while (i < out.length && out[i] != q) {
                    if (src.charAt(i) == '\\' && i + 1 < out.length) i++;
                    i++;
                }
                i++;  // closing quote
            } else {
                i++;
            }
        }
        return new String(out);
    }

    /** Every double-quoted string literal in (comment-blanked) source. */
    private static final Pattern STRING_LITERAL = Pattern.compile("\"(?:[^\"\\\\]|\\\\.)*\"");

    /** {@code .split(\"__\")} specifically — a strict subset of {@link
     * #STRING_LITERAL} matches, pinned separately so nexus-ft04v.16's
     * acceptance ("the split pin falls to zero") is a visible number. */
    private static final Pattern SPLIT_DUNDER = Pattern.compile("\\.split\\s*\\(\\s*\"__\"\\s*\\)");

    /**
     * Count of non-comment string literals containing {@code "__"} in
     * {@code rawSource}. This is the primary census: it catches both a
     * constant DECLARATION ({@code CCE_PREFIXES}, {@code CODE_PREFIX}) and
     * an INLINE call-site literal ({@code split("__")}, {@code
     * endsWith("__")}) by construction, since both are string literals at
     * the source level — see this class's own javadoc for why counting
     * {@code split} alone would miss the prefix-routing half of the census.
     */
    static int countDunderLiterals(String rawSource) {
        String blanked = blankComments(rawSource);
        int count = 0;
        Matcher m = STRING_LITERAL.matcher(blanked);
        while (m.find()) {
            if (m.group().contains("__")) count++;
        }
        return count;
    }

    /** Count of {@code .split("__")} call sites in {@code rawSource}, comment-blanked. */
    static int countSplitDunderCalls(String rawSource) {
        String blanked = blankComments(rawSource);
        Matcher m = SPLIT_DUNDER.matcher(blanked);
        int count = 0;
        while (m.find()) count++;
        return count;
    }

    /**
     * Per-file declared count of {@link #countDunderLiterals}, relative to
     * {@code src/main/java} (same path convention as {@link
     * dev.nexus.service.db.RawSqlGateTest#TEST_TREE_RAW_SQL_CEILING}).
     * Measured against develop @ 3e7b7b78e (2026-09-08, before nexus-ft04v.16
     * touches either site):
     *
     * <ul>
     *   <li>{@code EmbedderRouter.java}: {@code CCE_PREFIXES}' three entries
     *       ({@code "knowledge__"}, {@code "docs__"}, {@code "rdr__"}),
     *       {@code CODE_PREFIX} ({@code "code__"}), {@code
     *       stripTrailingSeparator}'s {@code endsWith("__")}, and {@code
     *       resolveEmbedderStrict}'s {@code split("__")} — 6.</li>
     *   <li>{@code PgVectorRepository.java}: {@code dimForCollection}'s
     *       {@code split("__")} plus its four-segment error-message literal
     *       ({@code "(<content_type>__<owner>__<model>__v<n>)"}), and {@code
     *       modelSegment}'s identical pair — 4.</li>
     * </ul>
     */
    private static final Map<String, Integer> COLLECTION_LITERAL_CENSUS = Map.ofEntries(
        Map.entry("dev/nexus/service/vectors/EmbedderRouter.java", 6),
        Map.entry("dev/nexus/service/vectors/PgVectorRepository.java", 4));

    private static final int COLLECTION_LITERAL_TOTAL_CEILING = 10;

    /**
     * Per-file declared count of {@link #countSplitDunderCalls} — the
     * narrower subset pin nexus-ft04v.16 is expected to drive to zero.
     * Measured at the same commit: {@code EmbedderRouter.java}'s
     * {@code resolveEmbedderStrict} (1); {@code PgVectorRepository.java}'s
     * {@code dimForCollection} and {@code modelSegment} (2).
     */
    private static final Map<String, Integer> COLLECTION_SPLIT_CENSUS = Map.ofEntries(
        Map.entry("dev/nexus/service/vectors/EmbedderRouter.java", 1),
        Map.entry("dev/nexus/service/vectors/PgVectorRepository.java", 2));

    private static final int COLLECTION_SPLIT_TOTAL_CEILING = 3;

    /**
     * Compares an {@code actual} per-file census against a {@code declared}
     * one in BOTH directions, exactly like {@link
     * dev.nexus.service.db.RawSqlGateTest#noRawExecuteSqlRegressionInTestSources}
     * does for its own ceiling map: a file counting MORE than its declared
     * pin is a REGRESSION (a new parse site, or a file the census has never
     * seen); a declared file counting FEWER than its pin is a STALE
     * FINGERPRINT (the site was removed without lowering the pin — the
     * direction that keeps this ratchet reduce-only rather than a one-way
     * ceiling that could silently stop meaning anything).
     */
    static List<String> checkCensus(String label, Map<String, Integer> actual, Map<String, Integer> declared) {
        List<String> violations = new ArrayList<>();
        for (var entry : actual.entrySet()) {
            int decl = declared.getOrDefault(entry.getKey(), 0);
            if (entry.getValue() > decl) {
                violations.add(entry.getKey() + ": REGRESSION -- " + entry.getValue() + " " + label
                    + " site(s) found, census declares " + decl
                    + " (new parse site added, or a file the census has never seen)");
            }
        }
        for (var entry : declared.entrySet()) {
            int found = actual.getOrDefault(entry.getKey(), 0);
            if (found < entry.getValue()) {
                violations.add(entry.getKey() + ": STALE FINGERPRINT -- declares " + entry.getValue()
                    + ", only " + found + " found -- lower this census entry (and its total ceiling) "
                    + "to match, or remove it outright if the file reached zero");
            }
        }
        return violations;
    }

    // ── the real-tree gate ──────────────────────────────────────────────

    /**
     * The gate itself: walks {@code src/main/java}, scans every file with
     * {@link #countDunderLiterals}/{@link #countSplitDunderCalls}, and
     * checks both censuses against {@link #COLLECTION_LITERAL_CENSUS}/
     * {@link #COLLECTION_SPLIT_CENSUS} in both directions, plus their
     * aggregate ceilings. Passes on the tree as it stands at the measured
     * pins; nexus-ft04v.16 is expected to lower every number here to zero
     * in the same edit that replaces the parse sites.
     */
    @Test
    void collectionNameParseCensus_pinnedAtMeasuredCount() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).exists();

        Map<String, Integer> actualLiterals = new TreeMap<>();
        Map<String, Integer> actualSplits = new TreeMap<>();
        int filesWalked = 0;
        try (Stream<Path> files = Files.walk(root)) {
            List<Path> javaFiles = files.filter(p -> p.toString().endsWith(".java")).toList();
            filesWalked = javaFiles.size();
            for (Path p : javaFiles) {
                String rel = root.relativize(p).toString().replace(java.io.File.separatorChar, '/');
                String src = Files.readString(p);
                int lit = countDunderLiterals(src);
                int split = countSplitDunderCalls(src);
                if (lit > 0) actualLiterals.put(rel, lit);
                if (split > 0) actualSplits.put(rel, split);
            }
        }

        // Non-vacuity: a broken root path or an empty tree would let every
        // check below pass vacuously (an empty actual map never regresses
        // against anything). See the nexus-moht0 vacuous-gate doctrine.
        assertThat(filesWalked)
            .as("the walk over " + root + " must see a non-zero number of .java files -- "
                + "zero means the root path is wrong, not that nothing needed scanning")
            .isGreaterThan(0);

        List<String> violations = new ArrayList<>();
        violations.addAll(checkCensus("collection-name parse literal", actualLiterals, COLLECTION_LITERAL_CENSUS));
        violations.addAll(checkCensus("split(\"__\")", actualSplits, COLLECTION_SPLIT_CENSUS));

        assertThat(violations)
            .as("collection-name parse-site census (nexus-ft04v.15) -- a REGRESSION means a new "
                + "string-literal parse site was added; a STALE FINGERPRINT means a declared site "
                + "was removed without lowering its census entry (nexus-ft04v.16 should do exactly "
                + "that, in the same edit that replaces the parse site with a registry lookup)")
            .isEmpty();

        int totalLiterals = actualLiterals.values().stream().mapToInt(Integer::intValue).sum();
        assertThat(totalLiterals)
            .as("COLLECTION_LITERAL_TOTAL_CEILING must be lowered in the same edit as any "
                + "COLLECTION_LITERAL_CENSUS reduction")
            .isLessThanOrEqualTo(COLLECTION_LITERAL_TOTAL_CEILING);

        int totalSplits = actualSplits.values().stream().mapToInt(Integer::intValue).sum();
        assertThat(totalSplits)
            .as("COLLECTION_SPLIT_TOTAL_CEILING must be lowered in the same edit as any "
                + "COLLECTION_SPLIT_CENSUS reduction")
            .isLessThanOrEqualTo(COLLECTION_SPLIT_TOTAL_CEILING);
    }

    /**
     * {@code test_scanner_is_not_vacuous} equivalent: the walk saw a
     * non-zero number of files AND matched at least the declared literal
     * total. A regex that broke (matched nothing) would report {@code 0}
     * here even though {@link #COLLECTION_LITERAL_CENSUS} declares 10 --
     * that is a scanner failure, not a clean tree, and must fail loud.
     */
    @Test
    void scanner_isNotVacuous_walkedFilesAndMatchedDeclaredSites() throws IOException {
        Path root = Path.of("src", "main", "java");

        long javaFileCount;
        try (Stream<Path> files = Files.walk(root)) {
            javaFileCount = files.filter(p -> p.toString().endsWith(".java")).count();
        }
        assertThat(javaFileCount)
            .as("the walk must see a non-zero number of source files")
            .isGreaterThan(0);

        int totalLiteralHits = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path p : files.filter(p -> p.toString().endsWith(".java")).toList()) {
                totalLiteralHits += countDunderLiterals(Files.readString(p));
            }
        }
        int declaredTotal = COLLECTION_LITERAL_CENSUS.values().stream().mapToInt(Integer::intValue).sum();
        assertThat(totalLiteralHits)
            .as("non-vacuity: the scanner found " + totalLiteralHits + " dunder-literal sites but the "
                + "census declares " + declaredTotal + " -- a broken regex or a broken root path would "
                + "both silently report a lower number here, not fail")
            .isGreaterThanOrEqualTo(declaredTotal);
    }

    // ── falsification: the detector must actually detect ────────────────

    /**
     * A synthetic source string with one MORE dunder literal than the file's
     * declared census fails as a REGRESSION. Driven through {@link
     * #countDunderLiterals} over a {@code String} (never a filesystem
     * mutation), exactly the shape {@code RawSqlGateTest}'s own synthetic
     * fixtures use for {@link #checkCensus}'s counterpart in that class.
     */
    @Test
    void syntheticExtraParseSite_failsAsRegression() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    private static final String A = \"code__\";",
            "    private static final String B = \"docs__\";",
            "    void extra() {",
            "        // a brand-new parse site nobody has declared yet",
            "        boolean matches = \"knowledge__\".equals(prefix);",
            "    }",
            "}");
        int found = countDunderLiterals(synthetic);
        assertThat(found).isEqualTo(3);

        Map<String, Integer> actual = Map.of("Whatever.java", found);
        Map<String, Integer> declared = Map.of("Whatever.java", 2);  // the pin predates the new site

        List<String> violations = checkCensus("collection-name parse literal", actual, declared);
        assertThat(violations)
            .as("a new parse-site literal beyond the declared pin must fail loud, never pass silently")
            .anyMatch(v -> v.contains("REGRESSION"));
    }

    /**
     * A declared file whose parse-site count falls BELOW its pin (the site
     * was deleted without lowering the census entry) fails as a STALE
     * FINGERPRINT — this is what makes the ratchet reduce-only: the count
     * can go down only via a deliberate edit to {@link
     * #COLLECTION_LITERAL_CENSUS}/{@link #COLLECTION_SPLIT_CENSUS}
     * themselves, never as a silent side effect of deleting code.
     */
    @Test
    void declaredSiteRemovedWithoutLoweringPin_failsAsStaleFingerprint() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    private static final String A = \"code__\";",
            "    // the second literal that used to live here was deleted without lowering the pin",
            "}");
        int found = countDunderLiterals(synthetic);
        assertThat(found).isEqualTo(1);

        Map<String, Integer> actual = Map.of("Whatever.java", found);
        Map<String, Integer> declared = Map.of("Whatever.java", 2);  // stale: the tree no longer has 2

        List<String> violations = checkCensus("collection-name parse literal", actual, declared);
        assertThat(violations)
            .as("a declared pin the tree no longer supports must fail as a stale fingerprint, "
                + "never silently pass")
            .anyMatch(v -> v.contains("STALE FINGERPRINT"));
    }

    /** Commented-out literals must not count -- {@link #blankComments} strips
     * both comment shapes before {@link #countDunderLiterals} scans. */
    @Test
    void commentedOutLiterals_areNotCounted() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    // private static final String OLD = \"code__\";",
            "    /* private static final String OLDER = \"docs__\"; */",
            "    private static final String REAL = \"knowledge__\";",
            "}");
        assertThat(countDunderLiterals(synthetic)).isEqualTo(1);
    }

    /** {@link #countSplitDunderCalls} counts only {@code split("__")}, a
     * strict subset of {@link #countDunderLiterals}'s broader literal
     * census -- a bare {@code "__"}-containing literal used elsewhere
     * (e.g. {@code endsWith("__")}) must not inflate the split-specific pin. */
    @Test
    void splitScan_isStrictSubsetOfLiteralScan() {
        String synthetic = String.join("\n",
            "public final class Whatever {",
            "    private static final String A = \"code__\";",
            "    void m(String collection, String prefix) {",
            "        boolean trailing = prefix.endsWith(\"__\");",
            "        String[] segments = collection.split(\"__\");",
            "    }",
            "}");
        assertThat(countDunderLiterals(synthetic)).isEqualTo(3);
        assertThat(countSplitDunderCalls(synthetic)).isEqualTo(1);
    }
}
