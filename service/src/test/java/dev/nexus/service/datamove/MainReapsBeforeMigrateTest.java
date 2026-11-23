// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Main ends the leftover {@code nexus-data-move-} backends BEFORE the schema migration (nexus-p2m1x.21): a validation with no statement bound
 * outlives its engine and holds locks a migration would wait on. A source-order check over the CODE of Main (comments and string and character
 * literals blanked first, so a comment that names either call cannot hide the order); it needs no database.
 */
class MainReapsBeforeMigrateTest {

    /** The source with every comment, string literal, text block and character literal replaced by spaces (newlines kept). */
    static String code(String src) {
        StringBuilder out = new StringBuilder(src.length());
        int i = 0;
        int n = src.length();
        while (i < n) {
            char c = src.charAt(i);
            if (src.startsWith("//", i)) {
                while (i < n && src.charAt(i) != '\n') {
                    out.append(' ');
                    i++;
                }
            } else if (src.startsWith("/*", i)) {
                int end = src.indexOf("*/", i + 2);
                end = end < 0 ? n : end + 2;
                blank(src, i, end, out);
                i = end;
            } else if (src.startsWith("\"\"\"", i)) {
                int end = src.indexOf("\"\"\"", i + 3);
                end = end < 0 ? n : end + 3;
                blank(src, i, end, out);
                i = end;
            } else if (c == '"' || c == '\'') {
                int j = i + 1;
                while (j < n && src.charAt(j) != c) {
                    j += src.charAt(j) == '\\' ? 2 : 1;
                }
                int end = Math.min(n, j + 1);
                blank(src, i, end, out);
                i = end;
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }

    private static void blank(String src, int from, int to, StringBuilder out) {
        for (int k = from; k < to; k++) {
            out.append(src.charAt(k) == '\n' ? '\n' : ' ');
        }
    }

    @Test
    void theScannerBlanksWhatIsNotCode() {
        String masked = code("// DataMoveOrphans.terminateLeftover(\nint x = 1; /* SchemaMigrator.migrate( */ String s = \"terminateLeftover(\"; char q = '\"'; run();");
        assertThat(masked).doesNotContain("terminateLeftover").doesNotContain("migrate(").contains("run();");
    }

    @Test
    void mainReapsLeftoverBackendsBeforeItMigrates() throws Exception {
        String main = code(Files.readString(Path.of("src/main/java/dev/nexus/service/Main.java")));
        int reap = main.indexOf("DataMoveOrphans.terminateLeftover(");
        int migrate = main.indexOf("SchemaMigrator.migrate(migrationDs");
        assertThat(reap).as("Main calls the reaper in code (not in a comment)").isPositive();
        assertThat(migrate).as("and the migration").isPositive();
        assertThat(reap).as("the reaper runs first").isLessThan(migrate);
    }
}
