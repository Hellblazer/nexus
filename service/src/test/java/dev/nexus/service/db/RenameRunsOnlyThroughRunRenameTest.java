// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (linda, R-A; bead nexus-p2m1x.23): no code outside {@code RenameGuard.runRename} takes the rename path. A source scan
 * of {@code src/main}: each of the six label-changing primitives (the catalog's {@code renameCollectionTxn}, and the chash, taxonomy,
 * telemetry, aspects and highlights renames) opens its transaction only through one {@code runRename} call, so its name body (the
 * lambda that rewrites the name columns) is reachable only from there; no method outside the guard decides a rename; and the old
 * entry checks are gone. The scan fails when it finds fewer than the six primitives.
 */
class RenameRunsOnlyThroughRunRenameTest {

    private static final Path MAIN = Path.of("src/main/java");

    /** {@code file, method name}: the primitives, each the one place its store's name columns are rewritten by a rename. */
    private static final String[][] PRIMITIVES = {
        {"db/CatalogRepository.java", "renameCollectionTxn"},
        {"db/ChashRepository.java", "renameCollection"},
        {"db/TaxonomyRepository.java", "renameCollection"},
        {"db/TelemetryRepository.java", "renameCollection"},
        {"db/AspectRepository.java", "renameAspectCollection"},
        {"db/AspectRepository.java", "renameHighlightsCollection"},
    };

    private static String code(Path file) throws IOException {
        return Files.readString(file).replaceAll("(?s)/\\*.*?\\*/", "").replaceAll("//[^\\n]*", "");
    }

    /** The text from {@code open} (an opening bracket's index) to its match, brackets included. */
    private static String balanced(String src, int open, char o, char c) {
        int depth = 0;
        for (int i = open; i < src.length(); i++) {
            char ch = src.charAt(i);
            if (ch == o) depth++;
            if (ch == c && --depth == 0) return src.substring(open, i + 1);
        }
        throw new AssertionError("unbalanced " + o);
    }

    private static String methodBody(String src, String name) {
        var m = Pattern.compile("(?m)^\\s+(?:public|private|protected)\\s+[\\w<>,\\[\\] .?]+\\s" + name + "\\s*\\(").matcher(src);
        assertThat(m.find()).as("method %s found", name).isTrue();
        int paren = src.indexOf('(', m.end() - 1);
        int brace = src.indexOf('{', src.indexOf(')', paren + balanced(src, paren, '(', ')').length() - 1));
        return balanced(src, brace, '{', '}');
    }

    @Test
    void everyPrimitiveOpensItsTransactionOnlyThroughRunRename() throws Exception {
        int examined = 0;
        for (String[] p : PRIMITIVES) {
            String body = methodBody(code(MAIN.resolve("dev/nexus/service").resolve(p[0])), p[1]);
            int at = body.indexOf("renameGuard.runRename(");
            assertThat(at).as("%s.%s calls runRename", p[0], p[1]).isGreaterThanOrEqualTo(0);
            assertThat(body.indexOf("renameGuard.runRename(", at + 1)).as("%s.%s calls it once", p[0], p[1]).isEqualTo(-1);
            String call = balanced(body, body.indexOf('(', at), '(', ')');
            String outside = body.replace(call, "");
            assertThat(outside).as("%s.%s opens no transaction outside runRename", p[0], p[1])
                .doesNotContain("withTenant(").doesNotContain("manifestWriteTxn(");
            examined++;
        }
        assertThat(examined).as("non-vacuity: the six primitives").isEqualTo(6);
    }

    @Test
    void noMethodOutsideTheGuardDecidesARename_andTheEntryChecksAreGone() throws Exception {
        List<String> callers = new ArrayList<>();
        List<String> stale = new ArrayList<>();
        int files = 0;
        try (Stream<Path> s = Files.walk(MAIN)) {
            for (Path f : s.filter(x -> x.toString().endsWith(".java")).toList()) {
                String src = code(f);
                files++;
                if (src.contains("requireRenameAllowed") || src.contains("decideRenamePath")) stale.add(f.getFileName().toString());
                if (!f.getFileName().toString().equals("RenameGuard.java") && src.contains(".runRename(")) {
                    callers.add(f.getFileName().toString());
                }
            }
        }
        assertThat(files).as("non-vacuity: the scan read the sources").isGreaterThan(100);
        assertThat(stale).as("the method-entry check and the out-of-transaction decision are gone").isEmpty();
        assertThat(callers).as("runRename is called from the five repositories only")
            .containsExactlyInAnyOrder("CatalogRepository.java", "ChashRepository.java", "TaxonomyRepository.java",
                "TelemetryRepository.java", "AspectRepository.java");
    }

    /** The catalog's transaction body is reached from the one public primitive only. */
    @Test
    void theCatalogRenameTransactionIsCalledFromTheOutcomeMethodOnly() throws Exception {
        String src = code(MAIN.resolve("dev/nexus/service/db/CatalogRepository.java"));
        var calls = Pattern.compile("renameCollectionTxn\\(").matcher(src);
        int n = 0;
        while (calls.find()) n++;
        assertThat(n).as("the definition and its one call").isEqualTo(2);
        assertThat(methodBody(src, "renameCollectionOutcome")).contains("renameCollectionTxn(");
    }
}
