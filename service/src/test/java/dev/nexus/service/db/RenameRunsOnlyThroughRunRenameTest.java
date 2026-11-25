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

    /**
     * The writes the scan sees: {@code .set(<T>.COLLECTION | PHYSICAL_COLLECTION | SOURCE_COLLECTION, ...)}, a write of the registry's
     * own {@code CATALOG_COLLECTIONS.NAME}, and the accessor form {@code .set(<t>.collection(), ...)} of {@code DimTables} and
     * {@code CollectionScopedTable}. Out of scope: raw SQL in string literals (the raw-SQL gate counts those) and writes that build the
     * column by name at run time.
     */
    private static final Pattern NAME_WRITE = Pattern.compile("\\.set\\(\\s*(?:[A-Za-z_]\\w*\\.(?:COLLECTION|PHYSICAL_COLLECTION|SOURCE_COLLECTION)"
        + "|CATALOG_COLLECTIONS\\.NAME|[A-Za-z_]\\w*\\.collection\\(\\))\\s*,");

    /**
     * The collection-name column writes of {@code src} (already {@link #strip stripped}): the site texts (the {@code .set(...)} call,
     * whitespace collapsed) of those outside any runRename call, with how many times each occurs, and the number inside one.
     */
    static NameWrites nameWrites(String src) {
        return writes(NAME_WRITE, src);
    }

    /** Writes of the retirement columns: {@code .set(<T>.SUPERSEDED_BY | SUPERSEDED_BY_ID, ...)}. */
    private static final Pattern RETIRE_WRITE = Pattern.compile("\\.set\\(\\s*[A-Za-z_]\\w*\\.SUPERSEDED_BY(?:_ID)?\\s*,");

    static NameWrites writes(Pattern pattern, String src) {
        List<int[]> spans = new ArrayList<>();
        var calls = Pattern.compile("\\.runRename\\(").matcher(src);
        while (calls.find()) {
            int open = src.indexOf('(', calls.start());
            spans.add(new int[] {open, open + balanced(src, open, '(', ')').length()});
        }
        java.util.Map<String, Integer> outside = new java.util.TreeMap<>();
        int inside = 0;
        var writes = pattern.matcher(src);
        while (writes.find()) {
            int at = writes.start();
            if (spans.stream().anyMatch(sp -> sp[0] <= at && at < sp[1])) {
                inside++;
            } else {
                String site = balanced(src, src.indexOf('(', at), '(', ')');
                outside.merge((".set" + site).replaceAll("\\s+", " "), 1, Integer::sum);
            }
        }
        return new NameWrites(outside, inside);
    }

    /** The sites outside runRename (text to count) and the number of writes inside runRename calls. */
    record NameWrites(java.util.Map<String, Integer> outside, int inside) { }

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

    /**
     * The widened scan: no write of a collection-name column outside runRename that the allowlist does not name, keyed by file and site
     * text, with exact counts. The numbers pinned here (all sites, and those inside runRename) change only when a write is added or
     * removed, which is the moment to look.
     */
    @Test
    void everyNameColumnWriteOutsideRunRenameIsOnTheAllowlist() throws Exception {
        java.util.Map<String, Integer> outside = new java.util.TreeMap<>();
        int inside = 0;
        try (Stream<Path> s = Files.walk(MAIN)) {
            for (Path f : s.filter(x -> x.toString().endsWith(".java")).toList()) {
                NameWrites w = nameWrites(code(f));
                inside += w.inside();
                w.outside().forEach((site, count) -> outside.put(f.getFileName() + " | " + site, count));
            }
        }
        java.util.Map<String, Integer> allowed = allowlist("src/test/resources/rename-name-write-allowlist.txt");
        // The telemetry rename's two name writes (search_telemetry, hook_failures) went into the generic helper TelemetryRepository.retag
        // (RDR-229 P3.5c: retagged by id), whose .set(<field parameter>, ...) the column scan cannot see: 10 became 8. They stay covered by
        // the pin that retag is declared once and called only inside runRename.
        assertThat(inside).as("the primitives' own name writes, inside runRename: exact").isEqualTo(8);
        NameWrites retag = writes(Pattern.compile("\\bretag\\("), code(MAIN.resolve("dev/nexus/service/db/TelemetryRepository.java")));
        assertThat(retag.inside()).as("TelemetryRepository.retag is called by the rename's two tables, inside its runRename call").isEqualTo(2);
        assertThat(retag.outside().keySet()).as("and nowhere else: the one text outside runRename is its own declaration")
            .hasSize(1).allMatch(site -> site.contains("DSLContext ctx"));
        assertThat(outside.values().stream().mapToInt(Integer::intValue).sum()).as("sites outside runRename: exact").isEqualTo(39);
        assertThat(unlisted(outside, allowed)).as("a name-column write outside runRename that is not on the allowlist").isEmpty();
    }

    static java.util.Map<String, Integer> allowlist(String file) throws IOException {
        java.util.Map<String, Integer> allowed = new java.util.TreeMap<>();
        for (String line : Files.readAllLines(Path.of(file))) {
            if (line.isBlank() || line.startsWith("#")) continue;
            String[] cols = line.split("\\|", 4);
            assertThat(cols).as("file | count | site | reason: %s", line).hasSize(4);
            assertThat(cols[3].trim()).as("a reason for %s", cols[0]).isNotBlank();
            allowed.put(cols[0].trim() + " | " + cols[2].trim(), Integer.parseInt(cols[1].trim()));
        }
        return allowed;
    }

    /**
     * Every write of {@code superseded_by} or {@code superseded_by_id} is on the retirement allowlist, keyed by file and site text: a
     * verb that retires or revives a registry row takes its rows FOR UPDATE first ({@code CollectionTombstones#lockRows}), so a new
     * writer of these columns is the moment to ask whether it is such a verb. Exact counts.
     */
    @Test
    void everyWriteOfTheRetirementColumnsIsOnTheAllowlist() throws Exception {
        java.util.Map<String, Integer> found = new java.util.TreeMap<>();
        int total = 0;
        try (Stream<Path> s = Files.walk(MAIN)) {
            for (Path f : s.filter(x -> x.toString().endsWith(".java")).toList()) {
                NameWrites w = writes(RETIRE_WRITE, code(f));
                total += w.inside() + w.outside().values().stream().mapToInt(Integer::intValue).sum();
                w.outside().forEach((site, count) -> found.put(f.getFileName() + " | " + site, count));
            }
        }
        // 20 on the name path's side, plus one in P3.7's one-row body: CollectionOneRowRename.apply's tombstone repoint, .set(CATALOG_COLLECTIONS.SUPERSEDED_BY,
        // newName) (listed in retire-write-allowlist.txt with its reason). The body runs only inside runRename's one-row branch (see
        // theOneRowBodyIsReachedOnlyThroughRenameGuardOneRow), so the write is inside runRename's transaction though not inside its lexical call.
        assertThat(total).as("retirement-column writes: exact").isEqualTo(21);
        assertThat(found).as("the one-row body's one retirement write, by file and site: it is the only one in CollectionOneRowRename.java")
            .containsEntry("CollectionOneRowRename.java | .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, newName)", 1);
        assertThat(found.keySet().stream().filter(k -> k.startsWith("CollectionOneRowRename.java | ")).count()).isEqualTo(1);
        assertThat(unlisted(found, allowlist("src/test/resources/retire-write-allowlist.txt")))
            .as("a write of superseded_by or superseded_by_id that is not on the allowlist").isEmpty();
    }

    /**
     * The aspect task bodies name their collection in a JSON field, which the column scan cannot see: {@code AspectTaskRefs.rewriteCollection}
     * and its per-document form are called, outside runRename, only by the document move and rehome (a move changes no label), and
     * inside it by the catalog rename and the aspects rename.
     */
    @Test
    void theAspectTaskRewriteIsCalledInsideRunRenameOrByTheMovesOnly() throws Exception {
        java.util.Map<String, Integer> outside = new java.util.TreeMap<>();
        int inside = 0;
        try (Stream<Path> s = Files.walk(MAIN)) {
            for (Path f : s.filter(x -> x.toString().endsWith(".java")).toList()) {
                String src = code(f);
                if (f.getFileName().toString().equals("AspectTaskRefs.java")) continue;
                List<int[]> spans = new ArrayList<>();
                var calls = Pattern.compile("\\.runRename\\(").matcher(src);
                while (calls.find()) {
                    int open = src.indexOf('(', calls.start());
                    spans.add(new int[] {open, open + balanced(src, open, '(', ')').length()});
                }
                var rewrites = Pattern.compile("AspectTaskRefs\\.rewriteCollection(?:ForDoc)?\\(").matcher(src);
                while (rewrites.find()) {
                    int at = rewrites.start();
                    if (spans.stream().anyMatch(sp -> sp[0] <= at && at < sp[1])) inside++;
                    else outside.merge(f.getFileName().toString(), 1, Integer::sum);
                }
            }
        }
        assertThat(inside).as("the catalog rename's and the aspects rename's own calls: exact").isEqualTo(2);
        // CatalogRepository.java=2: the document move (rewriteCollectionForDoc) and the rehome (rewriteCollection), neither changes a label.
        // CollectionOneRowRename.java=1: apply's rewriteCollection(ctx, tenant, oldName, newName), the one-row rename's own rewrite of the queued tasks; the body is
        // reached only through RenameGuard.oneRow inside runRename's one-row branch (theOneRowBodyIsReachedOnlyThroughRenameGuardOneRow), so it is inside runRename's
        // transaction, though not inside its lexical call. A new caller anywhere else still fails this exact map.
        assertThat(outside).as("the document move and rehome in CatalogRepository, and the one-row body's own rewrite: exact")
            .isEqualTo(java.util.Map.of("CatalogRepository.java", 2, "CollectionOneRowRename.java", 1));
    }

    /**
     * P3.7's one-row body, {@code CollectionOneRowRename.rename}, runs only as {@code RenameGuard.oneRow}, the one-row branch of a {@code runRename} call:
     * the pins above list its write sites (the tombstone repoint in the retirement allowlist, the queued-task rewrite in the aspect-task map) as outside
     * runRename's lexical call, and this is what makes them inside its transaction. Source scan of {@code src/main}: the class is named in no main file but its own
     * and {@code NexusService}, which constructs it once, as the argument of the one {@code RenameGuard}; the seam's {@code rename} is invoked once, in
     * {@code RenameGuard.oneRow}; {@code oneRow} is called once, in {@code CatalogRepository}, inside a {@code runRename} call; the body's static
     * {@code apply} is called by nothing else; and its name write is the one the {@code .set(} scan cannot see (a map entry), pinned here.
     */
    @Test
    void theOneRowBodyIsReachedOnlyThroughRenameGuardOneRow() throws Exception {
        List<String> naming = new ArrayList<>();
        List<String> seamCalls = new ArrayList<>();
        List<String> oneRowCalls = new ArrayList<>();
        List<String> applyCalls = new ArrayList<>();
        int files = 0;
        try (Stream<Path> s = Files.walk(MAIN)) {
            for (Path f : s.filter(x -> x.toString().endsWith(".java")).toList()) {
                String src = code(f);
                String file = f.getFileName().toString();
                files++;
                if (src.contains("CollectionOneRowRename")) naming.add(file);
                if (Pattern.compile("\\boneRowRename\\.rename\\(").matcher(src).find()) seamCalls.add(file);
                if (src.contains("CollectionOneRowRename.apply(")) applyCalls.add(file);
                var calls = Pattern.compile("renameGuard\\.oneRow\\(").matcher(src);
                while (calls.find()) {
                    int at = calls.start();
                    List<int[]> spans = new ArrayList<>();
                    var runs = Pattern.compile("\\.runRename\\(").matcher(src);
                    while (runs.find()) {
                        int open = src.indexOf('(', runs.start());
                        spans.add(new int[] {open, open + balanced(src, open, '(', ')').length()});
                    }
                    oneRowCalls.add(file + (spans.stream().anyMatch(sp -> sp[0] <= at && at < sp[1]) ? " (inside runRename)" : " (OUTSIDE runRename)"));
                }
            }
        }
        assertThat(files).as("non-vacuity: the scan read the sources").isGreaterThan(100);
        assertThat(naming).as("production code naming the one-row body: itself, and the service that constructs it").containsExactlyInAnyOrder(
            "CollectionOneRowRename.java", "NexusService.java");
        String service = code(MAIN.resolve("dev/nexus/service/NexusService.java"));
        assertThat(service).as("constructed once, as the argument of the one RenameGuard").containsOnlyOnce("new dev.nexus.service.db.CollectionOneRowRename(")
            .contains("new dev.nexus.service.db.RenameGuard(renameReadiness, new dev.nexus.service.db.CollectionOneRowRename(");
        assertThat(seamCalls).as("the seam's rename is invoked in RenameGuard only").containsExactly("RenameGuard.java");
        String guard = code(MAIN.resolve("dev/nexus/service/db/RenameGuard.java"));
        assertThat(Pattern.compile("oneRowRename\\.rename\\(").matcher(guard).results().count()).as("once").isEqualTo(1);
        int oneRowDecl = guard.indexOf("Map<String, Integer> oneRow(");   // package-private, so methodBody's modifier pattern does not match it
        assertThat(oneRowDecl).as("RenameGuard.oneRow is declared").isGreaterThan(0);
        int oneRowParen = guard.indexOf('(', oneRowDecl);
        int oneRowBrace = guard.indexOf('{', oneRowParen + balanced(guard, oneRowParen, '(', ')').length() - 1);
        assertThat(balanced(guard, oneRowBrace, '{', '}')).as("and that one call is in RenameGuard.oneRow").contains("oneRowRename.rename(");
        assertThat(oneRowCalls).as("RenameGuard.oneRow is called once, by the catalog rename, inside its runRename call")
            .containsExactly("CatalogRepository.java (inside runRename)");
        assertThat(applyCalls).as("no production file calls the body's static apply (tests do, to drive it directly)").isEmpty();
        // the one-row body's own sites, as the scans above count them: the registry name is written by a map entry, which the .set( scan cannot see
        String body = code(MAIN.resolve("dev/nexus/service/db/CollectionOneRowRename.java"));
        assertThat(Pattern.compile("set\\.put\\(CATALOG_COLLECTIONS\\.NAME,").matcher(body).results().count())
            .as("the one-row body's registry name write: one map entry, run inside the guard's one-row branch").isEqualTo(1);
        assertThat(Pattern.compile("(?<![.\\w])apply\\(").matcher(body).results().count())
            .as("inside the class, apply is declared once and called once, by the seam's rename").isEqualTo(2);
        assertThat(nameWrites(body).outside()).as("and no .set(<name column>) write besides, which the name-write scan would count").isEmpty();
        assertThat(Pattern.compile("AspectTaskRefs\\.rewriteCollection\\(").matcher(body).results().count())
            .as("the one-row body's queued-task rewrite: the one counted in the aspect-task map").isEqualTo(1);
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
        NameWrites w = nameWrites(src);
        assertThat(w.inside()).as("one inside").isEqualTo(1);
        assertThat(w.outside()).as("one outside, keyed by its site text").containsExactly(java.util.Map.entry(".set(T.COLLECTION, x)", 1));
        assertThat(unlisted(java.util.Map.of("R.java | .set(T.COLLECTION, x)", 1), java.util.Map.of())).as("an unlisted site").hasSize(1);
        assertThat(unlisted(java.util.Map.of("R.java | .set(T.COLLECTION, x)", 2), java.util.Map.of("R.java | .set(T.COLLECTION, x)", 1)))
            .as("a larger count").hasSize(1);
        assertThat(unlisted(java.util.Map.of("R.java | .set(T.COLLECTION, newName)", 1), java.util.Map.of("R.java | .set(T.COLLECTION, x)", 1)))
            .as("an allowed site swapped for a label-changing one in the same file").hasSize(2);
        assertThat(unlisted(java.util.Map.of("R.java | .set(T.COLLECTION, x)", 1), java.util.Map.of("R.java | .set(T.COLLECTION, x)", 1)))
            .as("listed and equal").isEmpty();
        String names = strip("""
            class R {
                void a() { ctx.update(C).set(CATALOG_COLLECTIONS.NAME, n).execute(); ctx.update(t.table()).set(t.collection(), n).execute(); }
            }
            """);
        assertThat(nameWrites(names).outside()).as("the registry's own name and the accessor form are seen").hasSize(2);

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
        // renameCollectionOutcome has two overloads since row 16: the 7-argument one by label delegates to the one that takes the requested source id
        int overload = src.indexOf("renameCollectionOutcome(String tenant, String oldName, String newName, Long requestedSourceId");
        assertThat(overload).as("the overload that takes the requested source id").isGreaterThan(0);
        int paren = src.indexOf('(', overload);
        int brace = src.indexOf('{', paren + balanced(src, paren, '(', ')').length() - 1);
        assertThat(balanced(src, brace, '{', '}')).contains("renameCollectionTxn(");
    }
}
