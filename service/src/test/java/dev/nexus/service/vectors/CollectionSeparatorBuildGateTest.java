// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * RDR-229 P1.R (nexus-p2m1x.5): the companion to {@link CollectionParseGateTest}'s literal census for a collection-name
 * separator that is BUILT, not written.
 *
 * <p>{@link CollectionParseGateTest#countDunderLiterals} counts string literals that hold two underscores.
 * {@code ChunkKeyPlan} declares {@code COLLECTION_SEPARATOR = "_".repeat(2)} so that its source holds no such literal, and
 * then builds {@code TAXONOMY_PREFIX} and {@code PREFIX_SHAPE} from the constant. The literal census saw none of the three,
 * so a prefix rule could live there with the gate at its pinned count. This census counts the build ({@code "_".repeat(2)})
 * and each use of the constant (its declaration is the build, not a use), comment-blanked, per file, reduce-only in both
 * directions like the literal census (a file at zero carries no entry).
 *
 * <p>Known limit: another way to build the separator ({@code String.valueOf}, a char loop, a {@code "_" + "_"}) is not
 * counted. The literal census still sees {@code "_" + "_"} only if a single literal holds both.
 */
class CollectionSeparatorBuildGateTest {

    private static final Pattern BUILT_SEPARATOR =
        Pattern.compile("\"_\"\\s*\\.repeat\\(\\s*2\\s*\\)|(?<!String\\s{1,8})\\bCOLLECTION_SEPARATOR\\b");

    /**
     * Per-file declared count, measured at develop {@code 118701321}: {@code ChunkKeyPlan.java} 3, the {@code "_".repeat(2)}
     * build at its declaration, the {@code TAXONOMY_PREFIX} use and the {@code PREFIX_SHAPE} use.
     */
    private static final Map<String, Integer> BUILT_SEPARATOR_CENSUS =
        Map.of("dev/nexus/service/vectors/ChunkKeyPlan.java", 3);

    private static final int BUILT_SEPARATOR_TOTAL_CEILING = 3;

    /** Count of {@link #BUILT_SEPARATOR} matches in comment-blanked {@code rawSource}. */
    static int countBuiltSeparator(String rawSource) {
        Matcher m = BUILT_SEPARATOR.matcher(CollectionParseGateTest.blankComments(rawSource));
        int count = 0;
        while (m.find()) count++;
        return count;
    }

    /** The census over the real tree in both directions, non-vacuous: it must find what it declares. */
    @Test
    void builtSeparatorCensus_pinnedAtMeasuredCount() throws IOException {
        Path root = Path.of("src", "main", "java");
        assertThat(root).exists();
        Map<String, Integer> actual = new TreeMap<>();
        int filesWalked = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path p : files.filter(f -> f.toString().endsWith(".java")).toList()) {
                filesWalked++;
                int n = countBuiltSeparator(Files.readString(p));
                if (n > 0) actual.put(root.relativize(p).toString().replace(java.io.File.separatorChar, '/'), n);
            }
        }
        int total = actual.values().stream().mapToInt(Integer::intValue).sum();
        System.out.println("built-separator census: files=" + filesWalked + " sites=" + total + actual);
        assertThat(filesWalked).as("the walk over " + root + " must see .java files").isGreaterThan(0);
        assertThat(CollectionParseGateTest.checkCensus("built-separator", actual, BUILT_SEPARATOR_CENSUS)).isEmpty();
        assertThat(total).as("non-vacuity: the scanner must find the declared sites, not fewer")
            .isEqualTo(BUILT_SEPARATOR_TOTAL_CEILING);
    }

    /** A source that builds the separator with {@code repeat(2)} and uses the constant is counted; a comment and the declaration word are not. */
    @Test
    void aSeparatorBuiltFromParts_isCountedWhereTheLiteralCensusIsBlind_andACommentIsNot() {
        String built = String.join("\n",
            "final class K {",
            "    // \"_\".repeat(2) and COLLECTION_SEPARATOR in a comment",
            "    static final String COLLECTION_SEPARATOR = \"_\".repeat(2);",
            "    static final String P = \"taxonomy\" + COLLECTION_SEPARATOR;",
            "    static final String Q = \"x\" + COLLECTION_SEPARATOR + \"y\";",
            "}");
        assertThat(countBuiltSeparator(built)).isEqualTo(3);
        assertThat(CollectionParseGateTest.countDunderLiterals(built)).as("the literal census is blind to it").isZero();
        assertThat(CollectionParseGateTest.checkCensus("built-separator", Map.of("K.java", 3), Map.of("K.java", 2)))
            .anyMatch(v -> v.contains("REGRESSION"));
        assertThat(CollectionParseGateTest.checkCensus("built-separator", Map.of("K.java", 2), Map.of("K.java", 3)))
            .anyMatch(v -> v.contains("STALE FINGERPRINT"));
    }

    /** The total ceiling equals the sum of the declared census, so lowering one and forgetting the other fails. */
    @Test
    void theCeilingEqualsTheDeclaredCensusSum() {
        assertThat(BUILT_SEPARATOR_CENSUS.values().stream().mapToInt(Integer::intValue).sum())
            .isEqualTo(BUILT_SEPARATOR_TOTAL_CEILING);
    }
}
