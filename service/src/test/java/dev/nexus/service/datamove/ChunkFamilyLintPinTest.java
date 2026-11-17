// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * nexus-f3yqd.2.14: {@link ChunkFamily} is the chunk family of {@code tests/test_changelog_continue_precondition_chunks_lint.py}
 * (the tables with a key into {@code nexus.chunks}, its leaves, the partition helpers and the index builder), and the two
 * must not drift. This test reads the lint's source: its {@code _FK_TABLES} tuple and the regular expressions of its
 * {@code _FORBIDDEN} table (the lint's own test derives the foreign-key set from the changelog, so equality with
 * {@code _FK_TABLES} is equality with the changelog). A table added to the lint and not to {@code ChunkFamily} fails here.
 */
class ChunkFamilyLintPinTest {

    private static final Path LINT = Path.of("..", "tests", "test_changelog_continue_precondition_chunks_lint.py");

    private static String lint() throws IOException {
        assertThat(LINT).as("the lint source, read from the service module").isRegularFile();
        return Files.readString(LINT);
    }

    private static List<String> all(Pattern pattern, String text, int group) {
        List<String> out = new ArrayList<>();
        Matcher m = pattern.matcher(text);
        while (m.find()) {
            out.add(m.group(group));
        }
        return out;
    }

    @Test
    void theForeignKeyTables_areTheLintsFkTables() throws IOException {
        Matcher tuple = Pattern.compile("(?s)_FK_TABLES\\s*=\\s*\\((.*?)\\)").matcher(lint());
        assertThat(tuple.find()).as("_FK_TABLES is found in the lint").isTrue();
        List<String> lintTables = all(Pattern.compile("\"([^\"]+)\""), tuple.group(1), 1);

        assertThat(lintTables).as("non-vacuity: the lint lists tables").hasSizeGreaterThanOrEqualTo(3);
        assertThat(ChunkFamily.FOREIGN_KEY_TABLES).containsExactlyElementsOf(lintTables);
    }

    @Test
    void theNamePatterns_areTheLintsForbiddenPatterns() throws IOException {
        Matcher table = Pattern.compile("(?s)_FORBIDDEN\\s*=\\s*\\{(.*?)\\n\\}").matcher(lint());
        assertThat(table.find()).as("_FORBIDDEN is found in the lint").isTrue();
        // the literal re.compile(r"...") entries; the foreign-key entry is built from _FK_TABLES and is covered above
        List<String> lintPatterns = all(Pattern.compile("re\\.compile\\(r\"([^\"]*)\"\\)"), table.group(1), 1);

        assertThat(lintPatterns).as("non-vacuity: the lint has literal patterns").hasSizeGreaterThanOrEqualTo(4);
        assertThat(ChunkFamily.NAME_PATTERNS).containsExactlyElementsOf(lintPatterns);
    }
}
