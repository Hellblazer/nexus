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
        return strip(Files.readString(file));
    }

    /**
     * The source without comments, and with every string, text block and char literal blanked to an empty one: a brace, a quote or
     * a {@code //} inside a literal (a URL, a JSON fragment) cannot unbalance the scan, and a comment cannot hide or fake a call.
     */
    static String strip(String src) {
        StringBuilder out = new StringBuilder(src.length());
        int i = 0, n = src.length();
        while (i < n) {
            char c = src.charAt(i);
            if (c == '/' && i + 1 < n && src.charAt(i + 1) == '/') {
                while (i < n && src.charAt(i) != '\n') i++;
            } else if (c == '/' && i + 1 < n && src.charAt(i + 1) == '*') {
                int end = src.indexOf("*/", i + 2);
                i = end < 0 ? n : end + 2;
            } else if (src.startsWith("\"\"\"", i)) {
                int end = src.indexOf("\"\"\"", i + 3);
                i = end < 0 ? n : end + 3;
                out.append("\"\"");
            } else if (c == '"' || c == '\'') {
                int j = i + 1;
                while (j < n && src.charAt(j) != c) j += src.charAt(j) == '\\' ? 2 : 1;
                i = j + 1;
                out.append(c).append(c);
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
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
        int brace = src.indexOf('{', paren + balanced(src, paren, '(', ')').length() - 1);
        return balanced(src, brace, '{', '}');
    }

    /**
     * What {@code method} of {@code src} (already {@link #strip stripped}) opens outside its one {@code renameGuard.runRename(} call:
     * a transaction opener, or the call missing or repeated. Null when the method is clean.
     */
    static String opensOutsideRunRename(String src, String method) {
        String body = methodBody(src, method);
        int at = body.indexOf("renameGuard.runRename(");
        if (at < 0) return "no runRename call";
        if (body.indexOf("renameGuard.runRename(", at + 1) >= 0) return "more than one runRename call";
        String outside = body.replace(balanced(body, body.indexOf('(', at), '(', ')'), "");
        for (String opener : List.of("withTenant(", "manifestWriteTxn(")) {
            if (outside.contains(opener)) return opener;
        }
        return null;
    }

    @Test
    void everyPrimitiveOpensItsTransactionOnlyThroughRunRename() throws Exception {
        int examined = 0;
        for (String[] p : PRIMITIVES) {
            String src = code(MAIN.resolve("dev/nexus/service").resolve(p[0]));
            assertThat(opensOutsideRunRename(src, p[1])).as("%s.%s opens its transaction only through one runRename call", p[0], p[1]).isNull();
            examined++;
        }
        assertThat(examined).as("non-vacuity: the six primitives").isEqualTo(6);
    }

    /** The check has teeth: it flags a primitive that opens a transaction outside runRename, and is not fooled by literals or comments. */
    @Test
    void theScanFlagsATransactionOutsideRunRename_andIgnoresLiteralsAndComments() {
        String clean = strip("""
            class R {
                public int renameCollection(String t, String o, String n) {
                    String url = "http://x/{";   // a brace and a slash pair inside a literal
                    char open = '{';
                    /* tenantScope.withTenant( in a comment */
                    return renameGuard.runRename(tenantScope, t, ctx -> { return 1; }, ctx -> 0);
                }
            }
            """);
        assertThat(opensOutsideRunRename(clean, "renameCollection")).as("clean: literals and comments are not code").isNull();

        String outsideTxn = strip("""
            class R {
                public int renameCollection(String t, String o, String n) {
                    tenantScope.withTenant(t, ctx -> ctx.update(X).execute());
                    return renameGuard.runRename(tenantScope, t, ctx -> 1, ctx -> 0);
                }
            }
            """);
        assertThat(opensOutsideRunRename(outsideTxn, "renameCollection")).as("a transaction opened beside runRename").isEqualTo("withTenant(");

        String noCall = strip("""
            class R {
                public int renameCollection(String t, String o, String n) {
                    return tenantScope.withTenant(t, ctx -> 1);
                }
            }
            """);
        assertThat(opensOutsideRunRename(noCall, "renameCollection")).as("a primitive that skips runRename").isEqualTo("no runRename call");

        String twice = strip("""
            class R {
                public int renameCollection(String t, String o, String n) {
                    renameGuard.runRename(tenantScope, t, ctx -> 1, ctx -> 0);
                    return renameGuard.runRename(tenantScope, t, ctx -> 1, ctx -> 0);
                }
            }
            """);
        assertThat(opensOutsideRunRename(twice, "renameCollection")).isEqualTo("more than one runRename call");
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
