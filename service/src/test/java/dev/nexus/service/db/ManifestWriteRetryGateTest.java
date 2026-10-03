// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Structural gate (nexus-wbfpw.66): every transaction that writes {@code catalog_document_chunks} rows
 * runs under {@link DeadlockRetry}, exactly once, and nothing new can start writing them without
 * either joining that set or failing here.
 *
 * <p>A manifest write that drops an owner row fires vectors-021-3's chunk-locking trigger, so it can
 * deadlock with a concurrent chunk writer (40P01). {@code ManifestWriteDeadlockRetryTest} proves the
 * retry works through the seven writers it can drive; this gate is what keeps the others (the
 * collection-lifecycle transactions) and every FUTURE writer from being left out.
 *
 * <h2>What this gate guarantees</h2>
 * <ol>
 *   <li><b>Where the table is written.</b> Every {@code service/src/main/java} file whose raw text
 *       mentions {@code catalog_document_chunks} (comments and strings included, any case) is either
 *       one of the two scanned files ({@code CatalogRepository}, {@code ChashRepository}) or is on
 *       {@link #READ_ONLY_REFERENCES} with a reason, and a listed file contains no write verb on the
 *       table (a jOOQ {@code insertInto/deleteFrom/update/mergeInto(...CATALOG_DOCUMENT_CHUNKS)}, or a
 *       raw {@code INSERT INTO / DELETE FROM / UPDATE / MERGE INTO / TRUNCATE} on it, comments
 *       blanked, strings kept). The two scanned files contain no raw-SQL write, so the jOOQ scan below
 *       sees all of their writes. A new file that writes the table, or a listed file that starts to,
 *       fails by name.</li>
 *   <li><b>How each writer opens its transaction.</b> In the two scanned files the manifest writers are
 *       the methods containing a DML on the table ({@code CATALOG_DOCUMENT_CHUNKS} or {@code
 *       Tables.CATALOG_DOCUMENT_CHUNKS}), the {@code purge_trash} routine (its fk-001 cascade deletes
 *       manifest rows), or a list-driven DML over {@code COLLECTION_SCOPED_TABLES} (a {@code
 *       CollectionScopedTable} parameter or the list itself next to an {@code update/deleteFrom(x.table())}),
 *       closed under "calls a manifest writer". For each such method that does not take a {@code
 *       DSLContext} (those run inside their caller's transaction): EVERY {@code withTenant} it contains
 *       is inside a {@code manifestWriteTxn(...)} or {@code DeadlockRetry.run(...)} argument; it
 *       opens at most one retried transaction; and no retried method calls another retried method.</li>
 *   <li><b>Non-vacuity.</b> The set of retried methods equals {@link #EXPECTED_RETRIED} exactly, so a
 *       scan that parsed nothing, or a wrap that was dropped, fails by name; the {@code
 *       manifestWriteTxn} helper must itself contain both the retry and the transaction.</li>
 * </ol>
 *
 * <h2>What it does NOT guarantee</h2>
 * <ul>
 *   <li>That a retry is CORRECT. Per-attempt state, side effects after the commit and the like are
 *       held by the code's own discipline and {@code ManifestWriteDeadlockRetryTest}.</li>
 *   <li>That every writer survives a real 40P01. Only seven do under test; the others are held by
 *       structure (rule 2) alone.</li>
 *   <li>Writes it cannot see: a table handed through an untyped variable, an {@code UPDATE} of another
 *       table whose FK cascade rewrites manifest rows (the {@code chunks.collection} cascade is known
 *       and reaches the manifest only through the pinned methods), and SQL functions in the changelog
 *       (only {@code purge_trash} is a manifest writer there; {@code ManifestInsertGateTest} and the
 *       chunk-inserter lint hold the rest).</li>
 *   <li>Code outside {@code service/src/main/java}.</li>
 * </ul>
 *
 * <p>The synthetic sources below prove each rule can fail.
 */
class ManifestWriteRetryGateTest {

    private static final Path MAIN = Path.of("src", "main", "java");
    private static final Path DB = MAIN.resolve(Path.of("dev", "nexus", "service", "db"));
    private static final List<String> SCANNED = List.of("CatalogRepository", "ChashRepository");

    /** Retried manifest-writing methods, as {@code File#method}. */
    private static final TreeSet<String> EXPECTED_RETRIED = new TreeSet<>(List.of(
        "CatalogRepository#writeManifest",
        "CatalogRepository#writeManifestMany",
        "CatalogRepository#appendOneDocumentTx",
        "CatalogRepository#purgeManifest",
        "CatalogRepository#importChunk",
        "CatalogRepository#importChunksBatch",
        "CatalogRepository#deleteCollectionTxn",
        "CatalogRepository#renameCollectionTxn",
        "CatalogRepository#rehomeCollection",
        "CatalogRepository#purgeTrash",
        "ChashRepository#renameCollection"));

    /** Files that mention the table without writing it, each with the reason. A pinned allowlist. */
    private static final Map<String, String> READ_ONLY_REFERENCES = new TreeMap<>(Map.of(
        "PgVectorRepository", "ownership, manifest and source_uri lookups (SELECT ... FROM catalog_document_chunks) and prose;"
            + " it writes chunks, never manifest rows",
        "ReaperRepository", "holdsNothing: an existence read of the tenant's manifest rows (nexus-wbfpw.73); no DML",
        "SchemaMigrator", "names the table in the chash-length constraint map: DDL bookkeeping, no DML",
        "TaxonomyRepository", "a comment naming the idiom; no code reference",
        "TenantScope", "a table-name list for the VACUUM allowlist; VACUUM ANALYZE is not manifest DML",
        "DeadlockRetry", "class javadoc naming the manifest writers it guards; the belt itself touches no table",
        "CatalogHandler", "comments only",
        "VectorHandler", "javadoc only"));

    private static final Pattern TABLE_REF = Pattern.compile("catalog_document_chunks|CatalogDocumentChunks",
        Pattern.CASE_INSENSITIVE);

    private static final String JOOQ_VERBS =
        "insertInto|deleteFrom|update|mergeInto|truncate|newRecord|batchInsert|executeInsert|executeUpdate|executeDelete|loadInto";
    /** A jOOQ write naming the manifest table. */
    private static final Pattern WRITE_JOOQ = Pattern.compile(
        "\\b(" + JOOQ_VERBS + ")\\s*\\(\\s*(?:Tables\\s*\\.\\s*)?CATALOG_DOCUMENT_CHUNKS\\b");
    /** A raw-SQL write naming the manifest table (strings kept, comments blanked before matching). */
    private static final Pattern WRITE_SQL = Pattern.compile(
        "\\b(insert\\s+into|delete\\s+from|update|merge\\s+into|truncate(?:\\s+table)?)\\s+(?:only\\s+)?"
            + "(?:\"?nexus\"?\\s*\\.\\s*)?\"?catalog_document_chunks\\b",
        Pattern.CASE_INSENSITIVE);

    private static final Pattern DML = Pattern.compile(
        "\\b(" + JOOQ_VERBS + ")\\s*\\(\\s*(?:Tables\\s*\\.\\s*)?CATALOG_DOCUMENT_CHUNKS\\b"
            + "|\\bRoutines\\s*\\.\\s*purgeTrash\\s*\\(");
    /** A DML whose target is a table handle, not a named table: {@code ctx.update(t.table())}. */
    private static final Pattern HANDLE_DML = Pattern.compile(
        "\\b(update|deleteFrom|insertInto|mergeInto)\\s*\\(\\s*\\w+\\s*\\.\\s*table\\s*\\(\\s*\\)");
    private static final Pattern RETRY = Pattern.compile("\\bmanifestWriteTxn\\s*\\(|\\bDeadlockRetry\\s*\\.\\s*run\\s*\\(");
    private static final Pattern RAW_TXN = Pattern.compile("\\bwithTenant\\s*\\(");

    /** A method of the scanned source: its name, parameter list and body (comments and strings blanked). */
    private record Method(String name, String params, String body) {
        boolean takesDslContext() {
            return params.contains("DSLContext");
        }

        boolean calls(String other) {
            return Pattern.compile("\\b" + Pattern.quote(other) + "\\s*\\(").matcher(body).find();
        }

        int retryCount() {
            Matcher m = RETRY.matcher(body);
            int n = 0;
            while (m.find()) n++;
            return n;
        }

        /** True when the body drives a DML through a table handle over the collection-scoped table list. */
        boolean listDrivenDml() {
            return HANDLE_DML.matcher(body).find()
                && (params.contains("CollectionScopedTable") || body.contains("COLLECTION_SCOPED_TABLES"));
        }

        /** {@code withTenant} calls that are NOT inside the argument list of a retry call. */
        int unretriedTransactions() {
            List<int[]> regions = new ArrayList<>();
            Matcher r = RETRY.matcher(body);
            while (r.find()) {
                int open = r.end() - 1;
                regions.add(new int[] {open, matching(body, open, '(', ')')});
            }
            Matcher t = RAW_TXN.matcher(body);
            int outside = 0;
            while (t.find()) {
                int at = t.start();
                if (regions.stream().noneMatch(g -> at > g[0] && at < g[1])) outside++;
            }
            return outside;
        }
    }

    // ── the real tree ───────────────────────────────────────────────────────

    @Test
    void everyManifestWritingTransactionIsRetriedExactlyOnce() throws IOException {
        TreeSet<String> retried = new TreeSet<>();
        List<String> violations = new ArrayList<>();
        for (String file : SCANNED) {
            String src = Files.readString(DB.resolve(file + ".java"));
            analyse(file, src, retried, violations);
        }
        assertThat(violations).as("manifest-writing transactions outside DeadlockRetry, or a retry inside a retry")
            .isEmpty();
        assertThat(retried).as("the retried manifest writers (add a new one here deliberately)")
            .containsExactlyElementsOf(EXPECTED_RETRIED);

        String catalog = blank(Files.readString(DB.resolve("CatalogRepository.java")), true);
        Method helper = methodsOf(catalog).stream().filter(m -> m.name().equals("manifestWriteTxn")).findFirst()
            .orElseThrow(() -> new AssertionError("CatalogRepository.manifestWriteTxn is gone: the gate's helper"));
        assertThat(isRealRetryHelper(helper)).as("manifestWriteTxn must open the transaction under DeadlockRetry").isTrue();
    }

    @Test
    void noOtherMainFileWritesTheManifestTable() throws IOException {
        TreeSet<String> referencing = new TreeSet<>();
        List<String> problems = new ArrayList<>();
        int files = 0;
        try (Stream<Path> walk = Files.walk(MAIN)) {
            for (Path p : walk.filter(f -> f.toString().endsWith(".java")).sorted().toList()) {
                files++;
                String name = p.getFileName().toString().replaceFirst("\\.java$", "");
                String raw = Files.readString(p);
                if (!TABLE_REF.matcher(raw).find()) continue;
                referencing.add(name);
                if (SCANNED.contains(name)) {
                    if (WRITE_SQL.matcher(blank(raw, false)).find()) {
                        problems.add(name + " writes the table in raw SQL, which the method scan cannot see");
                    }
                } else if (!READ_ONLY_REFERENCES.containsKey(name)) {
                    problems.add(name + " mentions catalog_document_chunks and is neither scanned nor on"
                        + " READ_ONLY_REFERENCES: if it writes the table it needs the retry and a place in this gate");
                } else if (writesTable(raw)) {
                    problems.add(name + " is listed as read-only but writes the table");
                }
            }
        }
        assertThat(files).as("non-vacuity: the scan walked the real source tree").isGreaterThan(100);
        assertThat(referencing).as("non-vacuity: the scanned files reference the table").containsAll(SCANNED);
        assertThat(problems).as("catalog_document_chunks writers outside the retry gate").isEmpty();
        TreeSet<String> stale = new TreeSet<>(READ_ONLY_REFERENCES.keySet());
        stale.removeAll(referencing);
        assertThat(stale).as("READ_ONLY_REFERENCES names a file that no longer mentions the table; delete the entry")
            .isEmpty();
    }

    // ── the rules, on synthetic sources (each can fail) ─────────────────────

    @Test
    void anUnwrappedManifestWriterIsFlagged() {
        String src = """
            class X {
                public void write(String tenant) {
                    tenantScope.withTenant(tenant, ctx -> {
                        ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).execute();
                        return null;
                    });
                }
                public void viaHelper(String tenant) {
                    tenantScope.withTenant(tenant, ctx -> { write(tenant); return null; });
                }
            }
            """;
        List<String> violations = new ArrayList<>();
        analyse("X", src, new TreeSet<>(), violations);
        assertThat(violations).hasSize(2);
        assertThat(violations.get(0)).contains("X#write");
        assertThat(violations.get(1)).contains("X#viaHelper");
    }

    @Test
    void aQualifiedTableReferenceIsSeenAsAWrite() {
        String src = """
            class X {
                public void write(String tenant) {
                    tenantScope.withTenant(tenant, ctx -> {
                        ctx.update(Tables.CATALOG_DOCUMENT_CHUNKS).set(a, b).execute();
                        return null;
                    });
                }
            }
            """;
        List<String> violations = new ArrayList<>();
        analyse("X", src, new TreeSet<>(), violations);
        assertThat(violations).singleElement().asString().contains("X#write");
    }

    @Test
    void aListDrivenUpdateOverTheCollectionScopedTablesIsAManifestWriter() {
        String src = """
            class X {
                private int move(DSLContext ctx, CollectionScopedTable t, String a, String b) {
                    return ctx.update(t.table()).set(t.collection(), b).execute();
                }
                public void rehome(String tenant) {
                    tenantScope.withTenant(tenant, ctx -> { return move(ctx, null, "a", "b"); });
                }
                public void rehomeRetried(String tenant) {
                    manifestWriteTxn(tenant, "r", ctx -> { return move(ctx, null, "a", "b"); });
                }
                public void renameInline(String tenant) {
                    tenantScope.withTenant(tenant, ctx -> {
                        for (CollectionScopedTable t : COLLECTION_SCOPED_TABLES) {
                            ctx.update(t.table()).set(t.collection(), "b").execute();
                        }
                        return null;
                    });
                }
            }
            """;
        List<String> violations = new ArrayList<>();
        TreeSet<String> retried = new TreeSet<>();
        analyse("X", src, retried, violations);
        assertThat(violations).hasSize(2);
        assertThat(violations).anyMatch(v -> v.contains("X#rehome "));
        assertThat(violations).anyMatch(v -> v.contains("X#renameInline"));
        assertThat(retried).containsExactly("X#rehomeRetried");
    }

    @Test
    void aMethodWithOneWrappedAndOneRawTransactionIsFlagged() {
        String src = """
            class X {
                public void mixed(String tenant) {
                    manifestWriteTxn(tenant, "a", ctx -> { ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).execute(); return null; });
                    tenantScope.withTenant(tenant, ctx -> { ctx.update(CATALOG_DOCUMENT_CHUNKS).execute(); return null; });
                }
                public void directForm(String tenant) {
                    DeadlockRetry.run("c", () -> tenantScope.withTenant(tenant, ctx -> {
                        ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).execute();
                        return null;
                    }));
                }
            }
            """;
        List<String> violations = new ArrayList<>();
        TreeSet<String> retried = new TreeSet<>();
        analyse("X", src, retried, violations);
        assertThat(violations).singleElement().asString().contains("X#mixed").contains("outside DeadlockRetry");
        assertThat(retried).as("the direct DeadlockRetry.run form is one retried transaction, not a raw one")
            .containsExactly("X#directForm", "X#mixed");
    }

    @Test
    void aRetryNestedInsideAnotherRetryIsFlagged() {
        String src = """
            class X {
                public void inner(String tenant) {
                    manifestWriteTxn(tenant, "inner", ctx -> { ctx.update(CATALOG_DOCUMENT_CHUNKS).execute(); return null; });
                }
                public void outer(String tenant) {
                    manifestWriteTxn(tenant, "outer", ctx -> { inner(tenant); return null; });
                }
                public void twice(String tenant) {
                    manifestWriteTxn(tenant, "a", ctx -> { inner(tenant); return null; });
                    manifestWriteTxn(tenant, "b", ctx -> { return null; });
                }
            }
            """;
        List<String> violations = new ArrayList<>();
        TreeSet<String> retried = new TreeSet<>();
        analyse("X", src, retried, violations);
        assertThat(retried).containsExactly("X#inner", "X#outer", "X#twice");
        assertThat(violations).anyMatch(v -> v.contains("X#outer") && v.contains("calls retried"));
        assertThat(violations).anyMatch(v -> v.contains("X#twice") && v.contains("calls retried"));
        assertThat(violations).anyMatch(v -> v.contains("X#twice") && v.contains("2 retried"));
    }

    @Test
    void aHollowRetryHelperIsFlagged() {
        String hollow = "class X { private <T> T manifestWriteTxn(String t, String c, Function f) { return tenantScope.withTenant(t, f); } }";
        String real = "class X { private <T> T manifestWriteTxn(String t, String c, Function f) {"
            + " return DeadlockRetry.run(c, () -> tenantScope.withTenant(t, f)); } }";
        assertThat(isRealRetryHelper(methodsOf(blank(hollow, true)).get(0))).isFalse();
        assertThat(isRealRetryHelper(methodsOf(blank(real, true)).get(0))).isTrue();
    }

    @Test
    void theFileEnumerationSeesWritesInCodeAndInSqlStrings_butNotInComments() {
        assertThat(writesTable("class A { void f() { ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).execute(); } }")).isTrue();
        assertThat(writesTable("class A { void f() { ctx.update(Tables.CATALOG_DOCUMENT_CHUNKS).execute(); } }")).isTrue();
        assertThat(writesTable("class A { String q = \"DELETE FROM nexus.catalog_document_chunks WHERE x\"; }")).isTrue();
        assertThat(writesTable("class A { String q = \"insert into catalog_document_chunks (a) values (1)\"; }")).isTrue();
        assertThat(writesTable("class A { String q = \"UPDATE nexus.catalog_document_chunks SET collection = 'b'\"; }")).isTrue();
        assertThat(writesTable("class A { void f() { ctx.select(1).from(CATALOG_DOCUMENT_CHUNKS).fetch(); } }")).isFalse();
        assertThat(writesTable("class A { String q = \"SELECT 1 FROM nexus.catalog_document_chunks m\"; }")).isFalse();
        assertThat(writesTable("class A { /* DELETE FROM nexus.catalog_document_chunks */ // update catalog_document_chunks\n }"))
            .isFalse();
    }

    // ── analysis ────────────────────────────────────────────────────────────

    private static boolean writesTable(String raw) {
        String noComments = blank(raw, false);
        return WRITE_SQL.matcher(noComments).find() || WRITE_JOOQ.matcher(noComments).find();
    }

    private static boolean isRealRetryHelper(Method helper) {
        return RETRY.matcher(helper.body()).find() && RAW_TXN.matcher(helper.body()).find()
            && helper.unretriedTransactions() == 0;
    }

    private static void analyse(String file, String source, TreeSet<String> retriedOut, List<String> violations) {
        List<Method> methods = methodsOf(blank(source, true));
        // The manifest writers: DML seeds, closed under "calls a manifest writer".
        Map<String, Method> writers = new LinkedHashMap<>();
        boolean grew = true;
        while (grew) {
            grew = false;
            for (Method m : methods) {
                if (writers.containsKey(key(m))) continue;
                boolean seed = DML.matcher(m.body()).find() || m.listDrivenDml();
                boolean caller = writers.values().stream().anyMatch(w -> !w.name().equals(m.name()) && m.calls(w.name()));
                if (seed || caller) {
                    writers.put(key(m), m);
                    grew = true;
                }
            }
        }
        TreeSet<String> retried = new TreeSet<>();
        for (Method m : writers.values()) {
            if (m.retryCount() > 0) retried.add(file + "#" + m.name());
        }
        for (Method m : writers.values()) {
            if (m.takesDslContext()) continue;      // runs inside its caller's transaction
            String id = file + "#" + m.name();
            int raw = m.unretriedTransactions();
            if (raw > 0) {
                violations.add(id + " opens " + raw + " transaction(s) outside DeadlockRetry");
            }
            if (m.retryCount() > 1) {
                violations.add(id + " opens " + m.retryCount() + " retried transactions (one per method)");
            }
            if (m.retryCount() > 0) {
                for (String other : retried) {
                    String otherName = other.substring(other.indexOf('#') + 1);
                    if (!otherName.equals(m.name()) && m.calls(otherName)) {
                        violations.add(id + " calls retried " + other + ": a retry nested inside a retry");
                    }
                }
            }
        }
        retriedOut.addAll(retried);
    }

    private static String key(Method m) {
        return m.name() + "(" + m.params() + ")";
    }

    /**
     * Blanks block comments and line comments, and, when {@code strings}, string/text-block/char
     * literals too; keeps length and newlines.
     */
    static String blank(String s, boolean strings) {
        StringBuilder out = new StringBuilder(s.length());
        int i = 0, n = s.length();
        while (i < n) {
            char c = s.charAt(i);
            if (s.startsWith("/*", i)) {
                int end = s.indexOf("*/", i + 2);
                end = end < 0 ? n : end + 2;
                for (int k = i; k < end; k++) out.append(s.charAt(k) == '\n' ? '\n' : ' ');
                i = end;
            } else if (s.startsWith("//", i)) {
                int end = s.indexOf('\n', i);
                end = end < 0 ? n : end;
                for (int k = i; k < end; k++) out.append(' ');
                i = end;
            } else if (s.startsWith("\"\"\"", i)) {
                int end = s.indexOf("\"\"\"", i + 3);
                end = end < 0 ? n : end + 3;
                if (strings) {
                    out.append("\"\"\"");
                    for (int k = i + 3; k < end - 3; k++) out.append(s.charAt(k) == '\n' ? '\n' : ' ');
                    out.append("\"\"\"");
                } else {
                    out.append(s, i, end);
                }
                i = end;
            } else if (c == '"' || c == '\'') {
                int k = i + 1;
                while (k < n && s.charAt(k) != c) k += (s.charAt(k) == '\\') ? 2 : 1;
                int stop = Math.min(k, n - 1);
                if (strings) {
                    out.append(c);
                    for (int j = i + 1; j < Math.min(k, n); j++) out.append(' ');
                    out.append(c);
                } else {
                    out.append(s, i, stop + 1);
                }
                i = stop + 1;
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }

    /** The methods declared directly in the top-level class body (depth 1), by brace matching. */
    static List<Method> methodsOf(String code) {
        List<Method> out = new ArrayList<>();
        int depth = 0;
        int headerStart = 0;
        int n = code.length();
        for (int i = 0; i < n; i++) {
            char c = code.charAt(i);
            if (c == '{') {
                if (depth == 1) {
                    int close = matching(code, i, '{', '}');
                    String header = code.substring(headerStart, i).trim();
                    Method m = asMethod(header, code.substring(i + 1, close));
                    if (m != null) out.add(m);
                    i = close;
                    headerStart = close + 1;
                    continue;
                }
                depth++;
                if (depth == 1) headerStart = i + 1;
            } else if (c == '}') {
                depth--;
            } else if (c == ';' && depth == 1) {
                headerStart = i + 1;
            }
        }
        return out;
    }

    private static int matching(String code, int open, char up, char down) {
        int depth = 0;
        for (int i = open; i < code.length(); i++) {
            char c = code.charAt(i);
            if (c == up) depth++;
            else if (c == down && --depth == 0) return i;
        }
        return code.length() - 1;
    }

    /** {@code header} is the text since the previous member ended; a method header ends with a parameter list. */
    private static Method asMethod(String header, String body) {
        String h = header.replaceAll("\\bthrows\\s+[\\w.,\\s]+$", "").trim();
        if (!h.endsWith(")")) return null;
        int depth = 0, open = -1;
        for (int i = h.length() - 1; i >= 0; i--) {
            char c = h.charAt(i);
            if (c == ')') depth++;
            else if (c == '(' && --depth == 0) {
                open = i;
                break;
            }
        }
        if (open <= 0) return null;
        Matcher nm = Pattern.compile("(\\w+)\\s*$").matcher(h.substring(0, open));
        if (!nm.find()) return null;
        String name = nm.group(1);
        if (name.equals("if") || name.equals("for") || name.equals("while") || name.equals("switch")
                || name.equals("catch") || name.equals("synchronized")) {
            return null;
        }
        return new Method(name, h.substring(open + 1, h.length() - 1), body);
    }
}
