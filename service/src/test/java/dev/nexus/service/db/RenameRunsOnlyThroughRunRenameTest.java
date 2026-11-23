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
        for (String opener : List.of("withTenant(", "manifestWriteTxn(", "transactionResult(", ".transaction(")) {
            if (outside.contains(opener)) return opener;
        }
        return sameLambdaForBothBodies(balanced(body, body.indexOf('(', at), '(', ')')) ? "the same lambda for both bodies" : null;
    }

    /** Whether the last two top-level arguments of a {@code runRename(...)} call (the two bodies) are the same text. */
    static boolean sameLambdaForBothBodies(String call) {
        List<String> args = new ArrayList<>();
        int depth = 0, from = 1;
        for (int i = 1; i < call.length() - 1; i++) {
            char ch = call.charAt(i);
            if (ch == '(' || ch == '{' || ch == '[') depth++;
            if (ch == ')' || ch == '}' || ch == ']') depth--;
            if (ch == ',' && depth == 0) {
                args.add(call.substring(from, i).trim());
                from = i + 1;
            }
        }
        args.add(call.substring(from, call.length() - 1).trim());
        return args.size() >= 3 && args.get(args.size() - 1).equals(args.get(args.size() - 2));
    }

    private static final Pattern NAME_WRITE =
        Pattern.compile("\\.set\\(\\s*[A-Za-z_]\\w*\\.(?:COLLECTION|PHYSICAL_COLLECTION|SOURCE_COLLECTION)\\s*,");

    /** The collection-name column writes of {@code src} (already {@link #strip stripped}): {@code [outside runRename, inside]}. */
    static int[] nameWrites(String src) {
        List<int[]> spans = new ArrayList<>();
        var calls = Pattern.compile("\\.runRename\\(").matcher(src);
        while (calls.find()) {
            int open = src.indexOf('(', calls.start());
            spans.add(new int[] {open, open + balanced(src, open, '(', ')').length()});
        }
        int outside = 0, inside = 0;
        var writes = NAME_WRITE.matcher(src);
        while (writes.find()) {
            int at = writes.start();
            if (spans.stream().anyMatch(sp -> sp[0] <= at && at < sp[1])) inside++; else outside++;
        }
        return new int[] {outside, inside};
    }

    /** The files of {@code counts} whose outside count differs from {@code allowed} (a missing entry is zero), as {@code file: found vs listed}. */
    static List<String> unlisted(java.util.Map<String, Integer> counts, java.util.Map<String, Integer> allowed) {
        List<String> bad = new ArrayList<>();
        var names = new java.util.TreeSet<>(counts.keySet());
        names.addAll(allowed.keySet());
        for (String f : names) {
            int found = counts.getOrDefault(f, 0), listed = allowed.getOrDefault(f, 0);
            if (found != listed) bad.add(f + ": " + found + " sites outside runRename, " + listed + " listed");
        }
        return bad;
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

    /** The widened scan: no write of a collection-name column outside runRename that the allowlist does not name. */
    @Test
    void everyNameColumnWriteOutsideRunRenameIsOnTheAllowlist() throws Exception {
        java.util.Map<String, Integer> outside = new java.util.TreeMap<>();
        int total = 0, inside = 0;
        try (Stream<Path> s = Files.walk(MAIN)) {
            for (Path f : s.filter(x -> x.toString().endsWith(".java")).toList()) {
                int[] w = nameWrites(code(f));
                total += w[0] + w[1];
                inside += w[1];
                if (w[0] > 0) outside.put(f.getFileName().toString(), w[0]);
            }
        }
        java.util.Map<String, Integer> allowed = new java.util.TreeMap<>();
        for (String line : Files.readAllLines(Path.of("src/test/resources/rename-name-write-allowlist.txt"))) {
            if (line.isBlank() || line.startsWith("#")) continue;
            String[] cols = line.split("\\|");
            assertThat(cols).as("file | count | reason: %s", line).hasSize(3);
            assertThat(cols[2].trim()).as("a reason for %s", cols[0]).isNotBlank();
            allowed.put(cols[0].trim(), Integer.parseInt(cols[1].trim()));
        }
        assertThat(total).as("non-vacuity: name-column writes scanned").isGreaterThanOrEqualTo(30);
        assertThat(inside).as("non-vacuity: the primitives' own name bodies, inside runRename").isGreaterThanOrEqualTo(8);
        assertThat(unlisted(outside, allowed)).as("a name-column write outside runRename that is not on the allowlist").isEmpty();
    }

    /** The widened scan has teeth: a write outside runRename in an unlisted file or a larger count is flagged, a write inside is not. */
    @Test
    void theNameWriteScanFlagsAnUnlistedSite_andTheOpenerScanFlagsTransactionResult() {
        String src = strip("""
            class R {
                void a() { ctx.update(T).set(T.COLLECTION, x).execute(); }
                int b() { return renameGuard.runRename(scope, t, c -> c.update(T).set(T.COLLECTION, x).execute(), c -> 0); }
            }
            """);
        assertThat(nameWrites(src)).as("one outside, one inside").containsExactly(1, 1);
        assertThat(unlisted(java.util.Map.of("R.java", 1), java.util.Map.of())).as("an unlisted file").hasSize(1);
        assertThat(unlisted(java.util.Map.of("R.java", 2), java.util.Map.of("R.java", 1))).as("a larger count").hasSize(1);
        assertThat(unlisted(java.util.Map.of("R.java", 1), java.util.Map.of("R.java", 1))).as("listed and equal").isEmpty();

        String txResult = strip("""
            class R {
                public int renameCollection(String t, String o, String n) {
                    ctx.transactionResult(cfg -> 1);
                    return renameGuard.runRename(tenantScope, t, c -> 1, c -> 0);
                }
            }
            """);
        assertThat(opensOutsideRunRename(txResult, "renameCollection")).isEqualTo("transactionResult(");
        String dsl = strip("""
            class R {
                public int renameCollection(String t, String o, String n) {
                    ctx.transaction(cfg -> { });
                    return renameGuard.runRename(tenantScope, t, c -> 1, c -> 0);
                }
            }
            """);
        assertThat(opensOutsideRunRename(dsl, "renameCollection")).isEqualTo(".transaction(");
        String same = strip("""
            class R {
                public int renameCollection(String t, String o, String n) {
                    return renameGuard.runRename(tenantScope, t, body, body);
                }
            }
            """);
        assertThat(opensOutsideRunRename(same, "renameCollection")).isEqualTo("the same lambda for both bodies");
        String distinct = strip("""
            class R {
                public int renameCollection(String t, String o, String n) {
                    return renameGuard.runRename(tenantScope, t, c -> 1, c -> 1);
                }
            }
            """);
        assertThat(opensOutsideRunRename(distinct, "renameCollection")).as("two lambdas with the same text are two arguments written out").isEqualTo("the same lambda for both bodies");
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
