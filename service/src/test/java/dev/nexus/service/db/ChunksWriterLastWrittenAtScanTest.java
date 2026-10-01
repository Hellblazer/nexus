// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * nexus-wbfpw.43 structural guard: every place in {@code service/src/main/java} that WRITES
 * {@code nexus.chunks} either refreshes {@code last_written_at} or is on a named allowlist
 * with a one-line reason. A new writer that omits the refresh turns this test red.
 *
 * <p>Why a scan and not only the behaviour tests in {@code ChunkLastWrittenAtIntegrationTest}:
 * those pin the writers somebody thought of. The dangerous error is the one nobody thought of,
 * and it runs in one direction. An UNDER-refresh (a client re-write that leaves the column
 * old) lets the reaper delete a chunk between the client's chunk write and its manifest
 * write. An OVER-refresh only delays a reap. So a writer that was never classified has to
 * fail loud, and that is what {@link #EXPECTED} does: a write site the scan finds that is not
 * in the map is red, whichever way it should have been classified.
 *
 * <p>Three checks per site, all keyed on {@code File#method}:
 * <ol>
 *   <li>the set of sites found equals the set in {@link #EXPECTED} (new site: red; stale
 *       entry, meaning a writer was deleted or renamed: red);</li>
 *   <li>a {@link Kind#REFRESH} method mentions {@code lastWrittenAt()} at least once per site;</li>
 *   <li>a {@link Kind#EXEMPT} method never mentions it. An allowlisted writer that starts
 *       refreshing must move to REFRESH so the allowlist keeps describing the code.</li>
 * </ol>
 *
 * <p>SCOPE, stated so the guarantee is not read wider than it is. The scan reads jOOQ
 * {@code insertInto}/{@code mergeInto}/{@code update} calls whose target is the chunks table
 * ({@code Tables.CHUNKS}, a {@code ChunkTable}/{@code ChunkDim}/{@code CollectionScopedTable}
 * accessor, or the {@code nexus.chunks} name), plus any raw-SQL string literal that begins an
 * INSERT/UPDATE/MERGE on it. It does not read Liquibase changelogs (the quarantine and
 * return-from-quarantine functions live there and take the column DEFAULT; see
 * {@code ChunkLastWrittenAtIntegrationTest}), and it cannot follow a table handed through an
 * untyped variable. A {@code .table()} receiver whose declared type it cannot resolve is
 * counted as a chunks site, so that gap fails loud rather than passing.
 */
class ChunksWriterLastWrittenAtScanTest {

    enum Kind { REFRESH, EXEMPT }

    /** {@code sites}: how many chunks write calls the scan must find in that method. */
    record Decision(Kind kind, int sites, String reason) {}

    /** One line each: why the writer refreshes, or why leaving the column alone is right. */
    static final Map<String, Decision> EXPECTED = new TreeMap<>(Map.ofEntries(
        Map.entry("PgVectorRepository#upsertChunksInternal", new Decision(Kind.REFRESH, 1,
            "content upsert ON CONFLICT DO UPDATE: a client re-write of an existing chunk")),
        Map.entry("PgVectorRepository#referenceOnlyInsertQuery", new Decision(Kind.REFRESH, 1,
            "reference-only upsert ON CONFLICT DO UPDATE: a client re-write of an existing chunk")),
        Map.entry("PgVectorRepository#batchUpdateMetadata", new Decision(Kind.REFRESH, 1,
            "have-vector and identical-text branches: the client re-indexed the chunk, metadata only")),
        Map.entry("CatalogRepository#upsertManifestChunkVectors", new Decision(Kind.REFRESH, 1,
            "combined write's chunk upsert ON CONFLICT DO UPDATE: a client re-write")),
        Map.entry("PgVectorRepository#updateMetadataOneRow", new Decision(Kind.EXEMPT, 1,
            "update-metadata route (frecency, enrichment stamps): annotates, re-writes no content;"
                + " refreshing would let a periodic stamp keep an unowned chunk alive forever")),
        Map.entry("ChashRepository#renameCollection", new Decision(Kind.EXEMPT, 1,
            "collection re-home is maintenance: refreshing would keep an unowned chunk alive by moving it")),
        Map.entry("CatalogRepository#renameCollectionTxn", new Decision(Kind.EXEMPT, 1,
            "collection rename over COLLECTION_SCOPED_TABLES (chunks is one): maintenance, not a client re-write")),
        Map.entry("CatalogRepository#moveScopedTable", new Decision(Kind.EXEMPT, 1,
            "collection move over COLLECTION_SCOPED_TABLES (chunks is one): maintenance, not a client re-write"))
    ));

    // ── the real tree ────────────────────────────────────────────────────────

    @Test
    void everyChunksWriter_refreshesLastWrittenAt_orIsAllowlistedWithAReason() throws IOException {
        Map<String, Integer> sites = new TreeMap<>();
        Map<String, Integer> refreshRefs = new TreeMap<>();
        int files = 0;
        Path root = Path.of("src", "main", "java");
        try (Stream<Path> walk = Files.walk(root)) {
            for (Path p : walk.filter(f -> f.toString().endsWith(".java")).sorted().toList()) {
                files++;
                Scan s = scan(p.getFileName().toString().replaceFirst("\\.java$", ""), Files.readString(p));
                s.sites.forEach((k, v) -> sites.merge(k, v, Integer::sum));
                s.refreshRefs.forEach((k, v) -> refreshRefs.merge(k, v, Integer::sum));
            }
        }

        assertThat(files).as("non-vacuity: the scan walked the real source tree").isGreaterThan(100);
        assertThat(sites).as("non-vacuity: the scan found chunks writers at all").isNotEmpty();

        Set<String> unclassified = new TreeSet<>(sites.keySet());
        unclassified.removeAll(EXPECTED.keySet());
        assertThat(unclassified)
            .as("A write to nexus.chunks that is not classified. Either set the column in it"
                + " (.set(ch.lastWrittenAt(), DimTables.lastWrittenNow()) on a client re-write)"
                + " and add it as REFRESH, or add it as EXEMPT with the reason a refresh would be wrong."
                + " Found (File#method): " + sites)
            .isEmpty();

        Set<String> stale = new TreeSet<>(EXPECTED.keySet());
        stale.removeAll(sites.keySet());
        assertThat(stale)
            .as("EXPECTED names a writer the scan no longer finds (deleted, renamed, or the"
                + " scan lost sight of it). Update the map; do not leave a dead entry.")
            .isEmpty();

        List<String> problems = new ArrayList<>();
        for (var e : EXPECTED.entrySet()) {
            String key = e.getKey();
            Decision d = e.getValue();
            int found = sites.get(key);
            int refs = refreshRefs.getOrDefault(key, 0);
            if (found != d.sites()) {
                problems.add(key + ": expected " + d.sites() + " chunks write site(s), found " + found
                    + " - a new write in an already-classified method needs its own decision");
            }
            if (d.kind() == Kind.REFRESH && refs < found) {
                problems.add(key + ": classified REFRESH but " + found + " chunks write site(s) and only "
                    + refs + " lastWrittenAt() reference(s) - a client re-write is not refreshing the column");
            }
            if (d.kind() == Kind.EXEMPT && refs != 0) {
                problems.add(key + ": classified EXEMPT (" + d.reason() + ") but it now references"
                    + " lastWrittenAt() " + refs + " time(s) - reclassify it as REFRESH");
            }
        }
        assertThat(problems).as("last_written_at classification drift").isEmpty();

        assertThat(EXPECTED.values().stream().filter(d -> d.kind() == Kind.REFRESH).count())
            .as("non-vacuity: the REFRESH side is populated").isGreaterThanOrEqualTo(4);
        assertThat(EXPECTED.values().stream().filter(d -> d.kind() == Kind.EXEMPT).count())
            .as("non-vacuity: the EXEMPT side is populated").isGreaterThanOrEqualTo(4);
    }

    // ── the scanner itself, on synthetic sources (proves each verdict can fail) ──

    @Test
    void scanner_findsAnUnlistedWriter_soANewOneTurnsTheGuardRed() {
        String src = """
            class Fresh {
                void addNewWriter(DSLContext ctx, DimTables.ChunkTable ch) {
                    ctx.update(ch.table()).set(ch.metadata(), x).where(y).execute();
                }
            }
            """;
        Scan s = scan("Fresh", src);
        assertThat(s.sites).containsEntry("Fresh#addNewWriter", 1);
        assertThat(EXPECTED).as("a brand-new writer is not on the allowlist").doesNotContainKey("Fresh#addNewWriter");
    }

    @Test
    void scanner_countsLastWrittenAtOnlyInCode_notInCommentsOrStrings() {
        String src = """
            class C {
                void w(DSLContext ctx, DimTables.ChunkTable ch) {
                    // .set(ch.lastWrittenAt(), now) is only a comment
                    /* lastWrittenAt() in a block comment */
                    String s = "lastWrittenAt()";
                    ctx.insertInto(ch.table()).columns(ch.chash()).values(h).execute();
                }
            }
            """;
        Scan s = scan("C", src);
        assertThat(s.sites).containsEntry("C#w", 1);
        assertThat(s.refreshRefs.getOrDefault("C#w", 0)).isZero();
    }

    @Test
    void scanner_seesTheRefreshWhenItIsInTheMethod() {
        String src = """
            class C {
                void w(DSLContext ctx, DimTables.ChunkTable ch) {
                    ctx.insertInto(ch.table()).columns(ch.chash()).values(h)
                       .onConflict(ch.chash()).doUpdate()
                       .set(ch.lastWrittenAt(), DimTables.lastWrittenNow()).execute();
                }
            }
            """;
        Scan s = scan("C", src);
        assertThat(s.sites).containsEntry("C#w", 1);
        assertThat(s.refreshRefs).containsEntry("C#w", 1);
    }

    @Test
    void scanner_ignoresNonChunkTables_butTreatsAnUnresolvedTableReceiverAsChunks() {
        String src = """
            class C {
                void centroid(DSLContext ctx, DimTables.CentroidTable ct) {
                    ctx.insertInto(ct.table()).columns(ct.label()).values(l).execute();
                }
                void other(DSLContext ctx) {
                    ctx.update(CATALOG_DOCUMENTS).set(a, b).execute();
                    ctx.insertInto(PDF_CHUNKS).columns(x).values(y).execute();
                }
                void mystery(DSLContext ctx) {
                    ctx.update(thing.table()).set(a, b).execute();
                }
            }
            """;
        Scan s = scan("C", src);
        assertThat(s.sites).doesNotContainKey("C#centroid").doesNotContainKey("C#other");
        assertThat(s.sites).as("unresolved receiver fails loud").containsEntry("C#mystery", 1);
    }

    @Test
    void scanner_findsStaticChunksAndRawSqlWrites_insideLambdas() {
        String src = """
            class C {
                int viaLambda(TenantScope ts) {
                    return ts.withTenant(t, ctx -> {
                        ctx.update(CHUNKS).set(CHUNKS.COLLECTION, n).execute();
                        ctx.execute("UPDATE nexus.chunks SET collection = ?", n);
                        return 1;
                    });
                }
            }
            """;
        Scan s = scan("C", src);
        assertThat(s.sites).containsEntry("C#viaLambda", 2);
    }

    // ── scanner ──────────────────────────────────────────────────────────────

    /** Per-file result: chunks write sites and lastWrittenAt() references, both keyed File#method. */
    record Scan(Map<String, Integer> sites, Map<String, Integer> refreshRefs) {}

    private static final Pattern WRITE_CALL = Pattern.compile("\\b(insertInto|mergeInto|update)\\s*\\(");
    private static final Pattern RAW_CHUNKS_WRITE = Pattern.compile(
        "(?i)\\b(insert\\s+into|update|merge\\s+into)\\s+(nexus\\.)?chunks\\b");
    private static final Pattern REFRESH_REF = Pattern.compile("\\blastWrittenAt\\s*\\(\\s*\\)");
    private static final Pattern RECEIVER_TABLE = Pattern.compile("^(\\w+)\\.table\\(\\)$");
    private static final Set<String> CHUNK_TYPES = Set.of("ChunkTable", "ChunkDim", "CollectionScopedTable");
    private static final Set<String> CONTROL_WORDS = Set.of(
        "if", "for", "while", "switch", "catch", "synchronized", "try", "return", "else");

    /** A method declaration span in the sanitized text. */
    private record Method(String name, int headStart, int bodyStart, int bodyEnd) {}

    static Scan scan(String fileName, String source) {
        String noComments = blank(source, false);
        String code = blank(source, true);
        List<Method> methods = methods(code);

        Map<String, Integer> sites = new TreeMap<>();
        Map<String, Set<Integer>> declsWithSites = new LinkedHashMap<>();
        Map<String, Integer> refs = new TreeMap<>();

        Matcher m = WRITE_CALL.matcher(code);
        while (m.find()) {
            int callAt = m.start();
            if (!precededByDot(code, callAt)) continue;
            int open = m.end() - 1;
            String arg = firstArgument(noComments, open + 1).strip();
            if (!targetsChunks(arg, code, noComments, methods, callAt)) continue;
            noteSite(fileName, methods, callAt, sites, declsWithSites, refs, code);
        }

        // Raw SQL strings that begin a write on the chunks table (literals kept, comments gone).
        Matcher raw = RAW_CHUNKS_WRITE.matcher(noComments);
        while (raw.find()) {
            if (!insideStringLiteral(source, code, raw.start())) continue;
            noteSite(fileName, methods, raw.start(), sites, declsWithSites, refs, code);
        }
        return new Scan(sites, refs);
    }

    private static void noteSite(String fileName, List<Method> methods, int at, Map<String, Integer> sites,
                               Map<String, Set<Integer>> declsWithSites, Map<String, Integer> refs, String code) {
        Method enclosing = innermost(methods, at);
        String key = fileName + "#" + (enclosing == null ? "<class-level>" : enclosing.name());
        sites.merge(key, 1, Integer::sum);
        if (enclosing != null && declsWithSites.computeIfAbsent(key, k -> new TreeSet<>()).add(enclosing.headStart())) {
            Matcher r = REFRESH_REF.matcher(code.substring(enclosing.bodyStart(), enclosing.bodyEnd()));
            int n = 0;
            while (r.find()) n++;
            refs.merge(key, n, Integer::sum);
        }
    }

    private static boolean precededByDot(String code, int at) {
        int i = at - 1;
        while (i >= 0 && Character.isWhitespace(code.charAt(i))) i--;
        return i >= 0 && code.charAt(i) == '.';
    }

    /** The text of a call's first argument, from just after '(' to the first top-level ',' or ')'. */
    private static String firstArgument(String text, int from) {
        int depth = 0;
        for (int i = from; i < text.length(); i++) {
            char c = text.charAt(i);
            if (c == '(' || c == '[' || c == '{') depth++;
            else if (c == ')' || c == ']' || c == '}') {
                if (depth == 0) return text.substring(from, i);
                depth--;
            } else if (c == ',' && depth == 0) return text.substring(from, i);
        }
        return text.substring(from);
    }

    private static boolean targetsChunks(String arg, String code, String noComments,
                                         List<Method> methods, int callAt) {
        if (arg.matches("(?s).*\\bCHUNKS\\b.*") || arg.contains("CHUNKS_TABLE_NAME")
                || arg.contains("\"nexus.chunks\"")
                || arg.matches("(?s).*DSL\\.name\\(\\s*\"nexus\"\\s*,\\s*\"chunks\"\\s*\\).*")) {
            return true;
        }
        Matcher r = RECEIVER_TABLE.matcher(arg);
        if (!r.matches()) return false;
        String ident = r.group(1);
        Method enclosing = innermost(methods, callAt);
        int from = enclosing == null ? 0 : enclosing.headStart();
        Pattern decl = Pattern.compile("([\\w.]+(?:<[^;=()]*>)?)\\s+" + Pattern.quote(ident) + "\\s*(?=[=:;,)])");
        String type = lastDeclaredType(decl, code, noComments, from, callAt);
        if (type == null) type = lastDeclaredType(decl, code, noComments, 0, callAt);
        if (type == null) return true;                       // unresolved: fail loud
        if (type.equals("var")) return true;                 // initializer not chased: fail loud
        String simple = type.replaceAll("<.*", "");
        simple = simple.substring(simple.lastIndexOf('.') + 1);
        return CHUNK_TYPES.contains(simple);
    }

    private static String lastDeclaredType(Pattern decl, String code, String noComments, int from, int to) {
        Matcher m = decl.matcher(code);
        String type = null;
        int lo = Math.max(0, from);
        m.region(lo, Math.min(to, code.length()));
        while (m.find()) {
            String t = m.group(1);
            if (CONTROL_WORDS.contains(t) || t.equals("new")) continue;
            type = t;
        }
        return type;
    }

    private static Method innermost(List<Method> methods, int at) {
        Method best = null;
        for (Method mth : methods) {
            if (at > mth.bodyStart() && at < mth.bodyEnd()
                    && (best == null || mth.bodyStart() > best.bodyStart())) {
                best = mth;
            }
        }
        return best;
    }

    /** True when {@code pos} sits inside a string literal (the sanitized {@code code} blanks them). */
    private static boolean insideStringLiteral(String source, String code, int pos) {
        return pos < code.length() && code.charAt(pos) == ' ' && source.charAt(pos) != ' ';
    }

    /**
     * Method and constructor declarations by brace matching over the sanitized text. The head of a
     * '{' is the text since the previous ';', '{' or '}'; it is a method head when it ends in ')'
     * (optionally followed by a throws clause), holds a name directly before '(' that is not a
     * control keyword, and is not an anonymous-class or lambda head.
     */
    private static List<Method> methods(String code) {
        List<Method> out = new ArrayList<>();
        Deque<int[]> stack = new ArrayDeque<>();         // {isMethod, headStart, bodyStart, nameIdx}
        List<String> names = new ArrayList<>();
        int boundary = 0;
        for (int i = 0; i < code.length(); i++) {
            char c = code.charAt(i);
            if (c == '{') {
                String head = code.substring(boundary, i);
                String name = methodName(head);
                int headStart = boundary;
                if (name != null) {
                    names.add(name);
                    stack.push(new int[] {1, headStart, i, names.size() - 1});
                } else {
                    stack.push(new int[] {0, headStart, i, -1});
                }
                boundary = i + 1;
            } else if (c == '}') {
                if (!stack.isEmpty()) {
                    int[] top = stack.pop();
                    if (top[0] == 1) out.add(new Method(names.get(top[3]), top[1], top[2], i));
                }
                boundary = i + 1;
            } else if (c == ';') {
                boundary = i + 1;
            }
        }
        return out;
    }

    private static final Pattern METHOD_HEAD = Pattern.compile(
        "(?s)^.*\\b(\\w+)\\s*\\(([^;{}]*)\\)\\s*(?:throws\\s+[\\w.,\\s]+)?\\s*$");

    private static String methodName(String head) {
        String h = head.strip();
        if (h.isEmpty() || h.endsWith("->") || h.endsWith("=") || h.endsWith(",")) return null;
        Matcher m = METHOD_HEAD.matcher(h);
        if (!m.matches()) return null;
        String name = m.group(1);
        if (CONTROL_WORDS.contains(name)) return null;
        // Anonymous class or object creation: "... new Foo(args)" / "return new Foo(args)".
        String before = h.substring(0, m.start(1));
        if (before.matches("(?s).*\\bnew\\s*$")) return null;
        if (before.contains("->")) return null;
        // A call head such as "x.foo(...)" before '{' is not a declaration.
        if (before.stripTrailing().endsWith(".")) return null;
        return name;
    }

    /**
     * Returns {@code src} with comments replaced by spaces (newlines kept, so offsets and lines are
     * stable) and, when {@code blankLiterals}, string, char and text-block contents blanked too.
     */
    static String blank(String src, boolean blankLiterals) {
        StringBuilder out = new StringBuilder(src.length());
        int n = src.length();
        int i = 0;
        while (i < n) {
            char c = src.charAt(i);
            char d = i + 1 < n ? src.charAt(i + 1) : '\0';
            if (c == '/' && d == '/') {
                while (i < n && src.charAt(i) != '\n') { out.append(' '); i++; }
            } else if (c == '/' && d == '*') {
                out.append("  ");
                i += 2;
                while (i < n && !(src.charAt(i) == '*' && i + 1 < n && src.charAt(i + 1) == '/')) {
                    out.append(src.charAt(i) == '\n' ? '\n' : ' ');
                    i++;
                }
                if (i < n) { out.append("  "); i += 2; }
            } else if (c == '"' && src.startsWith("\"\"\"", i)) {
                out.append(blankLiterals ? "   " : "\"\"\"");
                i += 3;
                while (i < n && !src.startsWith("\"\"\"", i)) {
                    char e = src.charAt(i);
                    if (e == '\\' && i + 1 < n) {
                        out.append(blankLiterals ? "  " : src.substring(i, i + 2));
                        i += 2;
                        continue;
                    }
                    out.append(blankLiterals && e != '\n' ? ' ' : e);
                    i++;
                }
                if (i < n) { out.append(blankLiterals ? "   " : "\"\"\""); i += 3; }
            } else if (c == '"' || c == '\'') {
                out.append(blankLiterals ? ' ' : c);
                i++;
                while (i < n && src.charAt(i) != c) {
                    char e = src.charAt(i);
                    if (e == '\\' && i + 1 < n) {
                        out.append(blankLiterals ? "  " : src.substring(i, i + 2));
                        i += 2;
                        continue;
                    }
                    if (e == '\n') break;               // unterminated: stop at the line end
                    out.append(blankLiterals ? ' ' : e);
                    i++;
                }
                if (i < n && src.charAt(i) == c) { out.append(blankLiterals ? ' ' : c); i++; }
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }
}
