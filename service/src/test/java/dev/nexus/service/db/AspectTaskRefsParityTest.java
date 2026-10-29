// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Structural gate (nexus-5r2sc.2.6, RDR-235 Technical Design "Collection rename, move and purge"): every
 * engine site that acts on a whole collection's aspect-extraction work has a call to {@link AspectTaskRefs}
 * in the same method, so a collection rename, move, re-home, purge or re-embed count cannot leave an
 * unconsumed {@code aspects} task naming a collection that is gone.
 *
 * <p>The tasks cannot be a {@code COLLECTION_SCOPED_TABLES} entry (the collection is a field of the task
 * body, and {@code nexus.tuples} has no collection column), so the registry's own parity test
 * ({@code CrossModelDispositionParityTest}) cannot hold them. This is their parity test: a Java source scan,
 * in the style of {@link ManifestWriteRetryGateTest}.
 *
 * <h2>The predicate</h2>
 * A <b>site</b> in {@code CatalogRepository}, {@code AspectRepository} or {@code ReembedSourceReader} is
 * <ol>
 *   <li>a statement ({@code deleteFrom}, {@code update}, {@code fetchCount}, {@code fetchExists}) on
 *       {@code ASPECT_EXTRACTION_QUEUE} that names {@code ASPECT_EXTRACTION_QUEUE.COLLECTION} in a WHERE or
 *       SET and carries no per-row key ({@code ASPECT_EXTRACTION_QUEUE.SOURCE_PATH.eq(...)}: the
 *       {@code (collection, source_path)} primary key addresses one row, as the worker's own
 *       {@code markDone}, {@code markFailed} and {@code markRetry} do), or</li>
 *   <li>a loop over {@code COLLECTION_SCOPED_TABLES}.</li>
 * </ol>
 * A site is <b>covered</b> when its method also contains a call {@code AspectTaskRefs.<verb>(}. The queue's
 * own claim, ack, retry, list and import methods are not sites: none names the collection column without a
 * row key, so the scan neither lists nor exempts them.
 *
 * <p>A second rule holds the lock order the bead fixes (the tuple row lock FIRST, then
 * {@code document_aspects}, so a rename never takes the two in the opposite order to a claim-fenced aspect
 * write): in a method that rewrites or deletes tasks, the call precedes the method's first mention of
 * {@code DOCUMENT_ASPECTS} or of {@code COLLECTION_SCOPED_TABLES}.
 *
 * <h2>Non-vacuity</h2>
 * The real source must examine at least {@link #MIN_SITES} sites, in every one of the
 * {@link #EXPECTED_METHODS}; a planted registry loop and a planted collection-keyed queue statement with no
 * call are each reported, and a covered or row-keyed one is not.
 *
 * <h2>What it does not guarantee</h2>
 * That the call is the RIGHT verb for the site (a count where a rewrite is needed), that a site reached
 * through another file's call is covered, or that a new file holding such a statement is scanned: add it to
 * {@link #SCANNED}. The behaviour is held by {@code AspectTaskRefsRenameTest}.
 */
class AspectTaskRefsParityTest {

    private static final Path DB = Path.of("src", "main", "java", "dev", "nexus", "service", "db");
    private static final List<String> SCANNED = List.of("CatalogRepository", "AspectRepository", "ReembedSourceReader");

    /** The ten census sites of the RDR, as {@code File#method}. */
    private static final TreeSet<String> EXPECTED_METHODS = new TreeSet<>(List.of(
        "AspectRepository#renameQueueCollection",
        "CatalogRepository#completeMove",
        "CatalogRepository#deleteCollectionTxn",
        "ReembedSourceReader#residue",
        "ReembedSourceReader#deleteCounts",
        "CatalogRepository#blockingTable",
        "CatalogRepository#collectionHoldsContent",
        "CatalogRepository#renameCollectionTxn",
        "CatalogRepository#rehomeCollection",
        "CatalogRepository#rehomeStatus"));

    /** The non-vacuity floor: the census names ten sites. */
    private static final int MIN_SITES = 10;

    private static final Pattern QUEUE_VERB = Pattern.compile(
        "\\b(deleteFrom|update|fetchCount|fetchExists)\\s*\\(\\s*ASPECT_EXTRACTION_QUEUE\\b");
    private static final Pattern QUEUE_COLLECTION = Pattern.compile(
        "ASPECT_EXTRACTION_QUEUE\\s*\\.\\s*COLLECTION\\s*\\.\\s*eq\\s*\\("
            + "|\\.set\\s*\\(\\s*ASPECT_EXTRACTION_QUEUE\\s*\\.\\s*COLLECTION\\b");
    private static final Pattern QUEUE_ROW_KEY = Pattern.compile(
        "ASPECT_EXTRACTION_QUEUE\\s*\\.\\s*SOURCE_PATH\\s*\\.\\s*eq\\s*\\(");
    private static final Pattern REGISTRY_LOOP = Pattern.compile(
        "\\bfor\\s*\\([^;{}]*:\\s*COLLECTION_SCOPED_TABLES\\s*\\)"
            + "|\\bCOLLECTION_SCOPED_TABLES\\s*\\.\\s*(stream|forEach|iterator|parallelStream)\\s*\\(");
    private static final Pattern HELPER_CALL = Pattern.compile("\\bAspectTaskRefs\\s*\\.\\s*(\\w+)\\s*\\(");
    private static final Pattern HELPER_MUTATION = Pattern.compile(
        "\\bAspectTaskRefs\\s*\\.\\s*(rewriteCollection\\w*|deleteNaming)\\s*\\(");
    private static final Pattern ASPECTS_TABLE = Pattern.compile("\\bDOCUMENT_ASPECTS\\b|\\bCOLLECTION_SCOPED_TABLES\\b");

    /** A site: where it is and what kind. */
    private record Site(String method, String kind, int at) {
    }

    private record Method(String name, String params, String body) {
        String key() {
            return name + "(" + params + ")";
        }
    }

    // ── the real tree ───────────────────────────────────────────────────────

    @Test
    void everyCollectionKeyedSiteCallsTheTaskHelper_andTheScanExaminedAllTen() throws IOException {
        List<String> uncovered = new ArrayList<>();
        List<String> lockOrder = new ArrayList<>();
        TreeSet<String> methodsWithSites = new TreeSet<>();
        int sites = 0;
        for (String file : SCANNED) {
            Path p = DB.resolve(file + ".java");
            assertThat(p).as("scanned file exists").exists();
            Examined e = examine(file, Files.readString(p));
            sites += e.sites();
            methodsWithSites.addAll(e.methodsWithSites());
            uncovered.addAll(e.uncovered());
            lockOrder.addAll(e.lockOrder());
        }
        assertThat(sites).as("non-vacuity: sites examined on the real source (the census names ten)")
            .isGreaterThanOrEqualTo(MIN_SITES);
        assertThat(methodsWithSites).as("the ten census methods are each examined").containsAll(EXPECTED_METHODS);
        assertThat(uncovered).as("collection-keyed sites with no AspectTaskRefs call in the same method").isEmpty();
        assertThat(lockOrder).as("task lock after a document_aspects touch (lock order: tuples first)").isEmpty();
    }

    @Test
    void theHelperItselfExists_soASourceScanForItsCallsIsNotScanningAGhost() throws IOException {
        String helper = Files.readString(DB.resolve("AspectTaskRefs.java"));
        for (String verb : List.of("countNaming", "rewriteCollection", "rewriteCollectionForDoc", "deleteNaming")) {
            assertThat(helper).as("AspectTaskRefs declares " + verb).containsPattern("static\\s+\\S+\\s+" + verb + "\\s*\\(");
        }
    }

    // ── the scan can fail: planted sources ──────────────────────────────────

    @Test
    void aPlantedRegistryLoopWithNoHelperCall_isReported() {
        String planted = """
            class Planted {
                private boolean holds(DSLContext ctx, String tenant, String name) {
                    for (CollectionScopedTable t : COLLECTION_SCOPED_TABLES) {
                        if (ctx.fetchExists(ctx.selectOne().from(t.table()).where(t.collection().eq(name)))) {
                            return true;
                        }
                    }
                    return false;
                }
            }
            """;
        Examined e = examine("Planted", planted);
        assertThat(e.sites()).isEqualTo(1);
        assertThat(e.uncovered()).containsExactly("Planted#holds: registry-loop");
    }

    @Test
    void aPlantedRegistryLoopWithTheHelperCall_isCovered() {
        String planted = """
            class Planted {
                private boolean holds(DSLContext ctx, String tenant, String name) {
                    for (CollectionScopedTable t : COLLECTION_SCOPED_TABLES) {
                        if (ctx.fetchExists(ctx.selectOne().from(t.table()).where(t.collection().eq(name)))) {
                            return true;
                        }
                    }
                    return AspectTaskRefs.countNaming(ctx, tenant, name) > 0;
                }
            }
            """;
        Examined e = examine("Planted", planted);
        assertThat(e.sites()).isEqualTo(1);
        assertThat(e.uncovered()).isEmpty();
    }

    @Test
    void aPlantedCollectionKeyedQueueStatementWithNoHelperCall_isReported() {
        String planted = """
            class Planted {
                int purge(DSLContext ctx, String name) {
                    return ctx.deleteFrom(ASPECT_EXTRACTION_QUEUE)
                        .where(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(name)).execute();
                }
                int rename(DSLContext ctx, String from, String to) {
                    return ctx.update(ASPECT_EXTRACTION_QUEUE)
                        .set(ASPECT_EXTRACTION_QUEUE.COLLECTION, to)
                        .where(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(from)).execute();
                }
            }
            """;
        Examined e = examine("Planted", planted);
        assertThat(e.sites()).isEqualTo(2);
        assertThat(e.uncovered()).containsExactly("Planted#purge: queue-statement", "Planted#rename: queue-statement");
    }

    @Test
    void aRowKeyedQueueStatement_isNotASite() {
        String planted = """
            class Planted {
                int markDone(DSLContext ctx, String collection, String path) {
                    return ctx.deleteFrom(ASPECT_EXTRACTION_QUEUE)
                        .where(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(collection)
                            .and(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH.eq(path))).execute();
                }
                int byDocOnly(DSLContext ctx, String docId) {
                    return ctx.deleteFrom(ASPECT_EXTRACTION_QUEUE).where(ASPECT_EXTRACTION_QUEUE.DOC_ID.eq(docId)).execute();
                }
            }
            """;
        Examined e = examine("Planted", planted);
        assertThat(e.sites()).isZero();
        assertThat(e.uncovered()).isEmpty();
    }

    @Test
    void aCommentOrStringNamingASiteIsNotASite() {
        String planted = """
            class Planted {
                // for (CollectionScopedTable t : COLLECTION_SCOPED_TABLES) { }
                String doc() {
                    return "ctx.deleteFrom(ASPECT_EXTRACTION_QUEUE).where(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(x))";
                }
            }
            """;
        assertThat(examine("Planted", planted).sites()).isZero();
    }

    @Test
    void aTaskLockAfterADocumentAspectsTouch_isReported() {
        String planted = """
            class Planted {
                int purge(DSLContext ctx, String tenant, String name) {
                    ctx.deleteFrom(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.COLLECTION.eq(name)).execute();
                    return AspectTaskRefs.deleteNaming(ctx, tenant, name);
                }
                int rename(DSLContext ctx, String tenant, String from, String to) {
                    AspectTaskRefs.rewriteCollection(ctx, tenant, from, to);
                    for (CollectionScopedTable t : COLLECTION_SCOPED_TABLES) { }
                    return 0;
                }
            }
            """;
        Examined e = examine("Planted", planted);
        assertThat(e.uncovered()).isEmpty();
        assertThat(e.lockOrder()).containsExactly("Planted#purge");
    }

    // ── the scan ────────────────────────────────────────────────────────────

    private record Examined(int sites, TreeSet<String> methodsWithSites, List<String> uncovered, List<String> lockOrder) {
    }

    private static Examined examine(String file, String source) {
        String code = blank(source);
        int sites = 0;
        TreeSet<String> withSites = new TreeSet<>();
        List<String> uncovered = new ArrayList<>();
        List<String> lockOrder = new ArrayList<>();
        for (Method m : methodsOf(code)) {
            List<Site> found = sitesOf(m);
            String id = file + "#" + m.name();
            Matcher mutation = HELPER_MUTATION.matcher(m.body());
            if (mutation.find()) {
                Matcher aspects = ASPECTS_TABLE.matcher(m.body());
                if (aspects.find() && aspects.start() < mutation.start()) {
                    lockOrder.add(id);
                }
            }
            if (found.isEmpty()) {
                continue;
            }
            sites += found.size();
            withSites.add(id);
            if (!HELPER_CALL.matcher(m.body()).find()) {
                for (Site s : found) {
                    uncovered.add(id + ": " + s.kind());
                }
            }
        }
        return new Examined(sites, withSites, uncovered, lockOrder);
    }

    private static List<Site> sitesOf(Method m) {
        List<Site> out = new ArrayList<>();
        String body = m.body();
        Matcher verb = QUEUE_VERB.matcher(body);
        while (verb.find()) {
            String window = statementFrom(body, verb.start());
            if (QUEUE_COLLECTION.matcher(window).find() && !QUEUE_ROW_KEY.matcher(window).find()) {
                out.add(new Site(m.name(), "queue-statement", verb.start()));
            }
        }
        Matcher loop = REGISTRY_LOOP.matcher(body);
        while (loop.find()) {
            out.add(new Site(m.name(), "registry-loop", loop.start()));
        }
        return out;
    }

    /** The text from {@code from} to the end of the statement: the next {@code ;} outside any parentheses. */
    private static String statementFrom(String code, int from) {
        int depth = 0;
        for (int i = from; i < code.length(); i++) {
            char c = code.charAt(i);
            if (c == '(') depth++;
            else if (c == ')') depth--;
            else if (c == ';' && depth <= 0) return code.substring(from, i);
        }
        return code.substring(from);
    }

    // ── a small Java reader (comments and string literals blanked; methods by brace matching) ──

    /** Blanks comments, string literals, text blocks and char literals; keeps length and newlines. */
    static String blank(String s) {
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
                out.append("\"\"\"");
                for (int k = i + 3; k < end - 3; k++) out.append(s.charAt(k) == '\n' ? '\n' : ' ');
                out.append("\"\"\"");
                i = end;
            } else if (c == '"' || c == '\'') {
                int k = i + 1;
                while (k < n && s.charAt(k) != c) k += (s.charAt(k) == '\\') ? 2 : 1;
                int stop = Math.min(k, n - 1);
                out.append(c);
                for (int j = i + 1; j < Math.min(k, n); j++) out.append(' ');
                out.append(c);
                i = stop + 1;
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }

    /** The methods declared directly in the top-level class body (depth 1). */
    private static List<Method> methodsOf(String code) {
        List<Method> out = new ArrayList<>();
        int depth = 0;
        int headerStart = 0;
        int n = code.length();
        for (int i = 0; i < n; i++) {
            char c = code.charAt(i);
            if (c == '{') {
                if (depth == 1) {
                    int close = matching(code, i);
                    Method m = asMethod(code.substring(headerStart, i).trim(), code.substring(i + 1, close));
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

    private static int matching(String code, int open) {
        int depth = 0;
        for (int i = open; i < code.length(); i++) {
            char c = code.charAt(i);
            if (c == '{') depth++;
            else if (c == '}' && --depth == 0) return i;
        }
        return code.length() - 1;
    }

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
