// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), linda's ruling: no writer claims "nothing to write" in place of reading its collection's row. Every
 * writer reads the row {@code FOR KEY SHARE} first, empty or no-op input included, so a by-id call answers the same whatever its batch
 * size: a 200 holds only for a live row and a freed id is 404. {@code ExpectedCollectionIds.nothingToWrite} was the way a write
 * marked a named id consumed without a transaction reading it; two taxonomy writers, then seven more methods, used it, and each answered 200
 * for a freed id on an empty batch. The method is gone, and this reads the source of {@code src/main} and fails on any call to a method
 * of that name, or a declaration of one, so the claim cannot come back by another door. The sanctioned set is empty: an empty write calls
 * {@code CollectionRegistry.readRowForEmptyWrite} (which opens a transaction only for a request that named an id) or reads the row in a
 * transaction of its own. It fails when it scans too few files, and its pattern is shown to catch the shapes it exists for.
 */
class NothingToWriteSourceScanTest {

    private static final Path MAIN = Path.of("src/main/java");
    private static final Pattern CLAIM = Pattern.compile("\\bnothingToWrite\\s*\\(");

    /** Comments blanked (lines kept), so a name in a javadoc is not a call. */
    static String code(String s) {
        Matcher block = Pattern.compile("(?s)/\\*.*?\\*/").matcher(s);
        StringBuilder out = new StringBuilder();
        while (block.find()) {
            block.appendReplacement(out, Matcher.quoteReplacement(block.group().replaceAll("[^\\n]", " ")));
        }
        block.appendTail(out);
        return out.toString().replaceAll("//[^\\n]*", "");
    }

    @Test
    void noMainSourceClaimsNothingToWrite() throws IOException {
        List<String> found = new ArrayList<>();
        int files = 0;
        try (Stream<Path> walk = Files.walk(MAIN)) {
            for (Path f : walk.filter(p -> p.toString().endsWith(".java")).toList()) {
                files++;
                String src = code(Files.readString(f));
                Matcher m = CLAIM.matcher(src);
                while (m.find()) {
                    found.add(f.getFileName() + ":" + (src.substring(0, m.start()).chars().filter(c -> c == '\n').count() + 1));
                }
            }
        }
        assertThat(files).as("non-vacuity: the main sources scanned").isGreaterThan(300);
        assertThat(found).as("a writer that returns early on an empty batch must read its collection's row first "
            + "(CollectionRegistry.readRowForEmptyWrite): no nothingToWrite claim is sanctioned").isEmpty();
    }

    /** The pattern finds a call and a declaration, in the shapes the old claim took, and not a comment. */
    @Test
    void thePatternFindsACallAndADeclarationButNotAComment() {
        assertThat(CLAIM.matcher(code("            ExpectedCollectionIds.nothingToWrite(collection);\n            return 0;")).find()).isTrue();
        assertThat(CLAIM.matcher(code("    public static void nothingToWrite(String label) {")).find()).isTrue();
        assertThat(CLAIM.matcher(code("    // nothingToWrite( in a comment\n    /* and nothingToWrite( here */")).find()).isFalse();
    }
}
